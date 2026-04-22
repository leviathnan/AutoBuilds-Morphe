import json
import logging
import re
import shutil
import subprocess
import zipfile
from os import getenv
from pathlib import Path
from sys import exit

from src import downloader, r2, release, utils


def run_build(app_name: str, source: str, arch: str = "universal") -> str:
    """Build APK for specific architecture"""
    download_files, name = downloader.download_required(source)

    # Log downloaded files for debugging
    logging.info(f"📦 Downloaded {len(download_files)} files for {source}:")
    for file in download_files:
        logging.info(f"  - {file.name} ({file.stat().st_size} bytes)")

    # DETECT SOURCE TYPE BASED ON DOWNLOADED FILES
    is_morphe = False
    is_revanced = False

    # Check file contents to determine source type
    for file in download_files:
        if "morphe-cli" in file.name.lower():
            is_morphe = True
            break
        elif "revanced-cli" in file.name.lower():
            is_revanced = True
            break

    # If not detected by CLI name, check patch file extension
    if not is_morphe and not is_revanced:
        for file in download_files:
            if file.suffix == ".mpp":
                is_morphe = True
                break
            elif file.suffix in [".rvp", ".jar"] and "patches" in file.name.lower():
                is_revanced = True
                break

    # If still not detected, fallback to source name
    if not is_morphe and not is_revanced:
        is_morphe = "morphe" in source.lower() or "custom" in source.lower()
        is_revanced = not is_morphe  # Default to ReVanced if not Morphe

    logging.info(f"🔍 Detected: {'Morphe' if is_morphe else 'ReVanced'} source type")

    # FIND FILES BASED ON DETECTED TYPE
    if is_morphe:
        # Find Morphe files - prefer non-dev version
        cli = utils.find_file(
            download_files, contains="morphe-cli", suffix=".jar", exclude=["dev"]
        )
        if not cli:
            # Fallback to any Morphe CLI
            cli = utils.find_file(download_files, contains="morphe", suffix=".jar")

        patches = utils.find_file(download_files, contains="patches", suffix=".mpp")
        if not patches:
            # Fallback to any .mpp file
            patches = utils.find_file(download_files, suffix=".mpp")
    else:
        # Find ReVanced files
        cli = utils.find_file(download_files, contains="revanced-cli", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".rvp")

        if not patches:
            # Try .jar extension for patches
            patches = utils.find_file(download_files, contains="patches", suffix=".jar")

    # Validate tools
    if not cli:
        logging.error(f"❌ CLI not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None
    if not patches:
        logging.error(f"❌ Patches not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None

    logging.info(f"✅ Using CLI: {cli.name}")
    logging.info(f"✅ Using patches: {patches.name}")

    input_apk, version = downloader.download_apkmirror(
        app_name, str(cli), str(patches), arch
    )

    if input_apk is None:
        logging.error(f"❌ Failed to download APK for {app_name}")
        logging.error("All download sources failed. Skipping this app.")
        return None

    if input_apk.suffix != ".apk":
        apk_editor = downloader.download_apkeditor()
        merged_apk = input_apk.with_suffix(".apk")

        # --- Extract and filter splits ---
        splits_dir = input_apk.parent / f"{input_apk.stem}_splits"
        splits_dir.mkdir(exist_ok=True)

        app_config_path = Path("apps") / "apkmirror" / f"{app_name}.json"
        with app_config_path.open() as f:
            app_cfg = json.load(f)
        target_dpi = app_cfg.get("dpi", "xxxhdpi")
        target_arch = arch  # already resolved above (e.g. "arm64-v8a")

        arch_split = (
            f"config.{target_arch.replace('-', '_')}.apk"  # config.arm64_v8a.apk
        )
        dpi_split = f"config.{target_dpi}.apk"  # config.xxxhdpi.apk
        keep = {"base.apk", arch_split, dpi_split}

        with zipfile.ZipFile(input_apk, "r") as z:
            for name in z.namelist():
                if name.endswith(".apk"):
                    z.extract(name, splits_dir)

        for f in splits_dir.iterdir():
            if f.name not in keep:
                logging.info(f"Dropping split: {f.name}")
                f.unlink()
            else:
                logging.info(f"Keeping split: {f.name}")

        # Merge only the kept splits
        utils.run_process(
            [
                "java",
                "-jar",
                str(apk_editor),
                "m",
                "-i",
                str(splits_dir),
                "-o",
                str(merged_apk),
            ],
            silent=True,
        )

        shutil.rmtree(splits_dir, ignore_errors=True)
        input_apk.unlink(missing_ok=True)

        if not merged_apk.exists():
            logging.error("Merged APK not found after split filtering")
            return None

        input_apk = merged_apk
        logging.info(f"Merged APK (arm64+xxxhdpi only): {input_apk}")

    # ARCHITECTURE-SPECIFIC PROCESSING
    if arch != "universal":
        logging.info(f"Processing APK for {arch} architecture...")

        # Remove unwanted architectures based on selected arch
        if arch == "arm64-v8a":
            # Remove x86, x86_64, and armeabi-v7a
            utils.run_process(
                [
                    "zip",
                    "--delete",
                    str(input_apk),
                    "lib/x86/*",
                    "lib/x86_64/*",
                    "lib/armeabi-v7a/*",
                ],
                silent=True,
                check=False,
            )
        elif arch == "armeabi-v7a":
            # Remove x86, x86_64, and arm64-v8a
            utils.run_process(
                [
                    "zip",
                    "--delete",
                    str(input_apk),
                    "lib/x86/*",
                    "lib/x86_64/*",
                    "lib/arm64-v8a/*",
                ],
                silent=True,
                check=False,
            )
    else:
        # Universal: only remove x86 architectures
        utils.run_process(
            ["zip", "--delete", str(input_apk), "lib/x86/*", "lib/x86_64/*"],
            silent=True,
            check=False,
        )

    exclude_patches = []
    include_patches = []

    patches_path = Path("patches") / f"{app_name}-{source}.txt"
    if patches_path.exists():
        with patches_path.open("r") as patches_file:
            for line in patches_file:
                line = line.strip()
                if line.startswith("-"):
                    exclude_patches.extend(["-d", line[1:].strip()])
                elif line.startswith("+"):
                    include_patches.extend(["-e", line[1:].strip()])

    # Include architecture in output filename
    output_apk = Path(f"{app_name}-{arch}-patch-v{version}.apk")

    # USE DIFFERENT COMMANDS BASED ON SOURCE TYPE
    if is_morphe:
        logging.info("🔧 Using Morphe patching system...")
        # Morphe CLI might have different arguments - we need to test this
        # Try common patterns
        try:
            # Try ReVanced-style arguments first (most likely)
            morphe_cmd = [
                "java",
                "-jar",
                str(cli),
                "patch",
                "--patches",
                str(patches),
                "--out",
                str(output_apk),
                str(input_apk),
                *exclude_patches,
                *include_patches,
            ]
            utils.run_process(morphe_cmd, stream=True)
        except subprocess.CalledProcessError:
            # Try alternative Morphe arguments
            logging.info("Trying alternative Morphe command format...")
            morphe_cmd = [
                "java",
                "-jar",
                str(cli),
                "--patches",
                str(patches),
                "--input",
                str(input_apk),
                "--output",
                str(output_apk),
            ]
            utils.run_process(morphe_cmd, stream=True)
    else:
        logging.info("🔧 Using ReVanced patching system...")
        cli_name = Path(cli).name.lower()
        is_revanced_v6_or_newer = (
            "revanced-cli-6" in cli_name
            or "revanced-cli-7" in cli_name
            or "revanced-cli-8" in cli_name
        )

        if is_revanced_v6_or_newer:
            utils.run_process(
                [
                    "java",
                    "-jar",
                    str(cli),
                    "patch",
                    "-p",
                    str(patches),
                    "-b",
                    "--out",
                    str(output_apk),
                    str(input_apk),
                    *exclude_patches,
                    *include_patches,
                ],
                stream=True,
            )
        else:
            # Standard ReVanced command
            utils.run_process(
                [
                    "java",
                    "-jar",
                    str(cli),
                    "patch",
                    "--patches",
                    str(patches),
                    "--out",
                    str(output_apk),
                    str(input_apk),
                    *exclude_patches,
                    *include_patches,
                ],
                stream=True,
            )

    input_apk.unlink(missing_ok=True)

    # Include architecture in final signed APK name
    signed_apk = Path(f"{app_name}-{arch}-{name}-v{version}.apk")

    apksigner = utils.find_apksigner()
    if not apksigner:
        exit(1)

    try:
        utils.run_process(
            [
                str(apksigner),
                "sign",
                "--verbose",
                "--ks",
                "keystore/public.jks",
                "--ks-pass",
                "pass:public",
                "--key-pass",
                "pass:public",
                "--ks-key-alias",
                "public",
                "--in",
                str(output_apk),
                "--out",
                str(signed_apk),
            ],
            stream=True,
        )
    except Exception as e:
        logging.warning(f"Standard signing failed: {e}")
        logging.info("Trying alternative signing method...")

        utils.run_process(
            [
                str(apksigner),
                "sign",
                "--verbose",
                "--min-sdk-version",
                "21",
                "--ks",
                "keystore/public.jks",
                "--ks-pass",
                "pass:public",
                "--key-pass",
                "pass:public",
                "--ks-key-alias",
                "public",
                "--in",
                str(output_apk),
                "--out",
                str(signed_apk),
            ],
            stream=True,
        )

    output_apk.unlink(missing_ok=True)
    print(f"✅ APK built: {signed_apk.name}")

    return str(signed_apk)


def main():
    app_name = getenv("APP_NAME")
    source = getenv("SOURCE")

    if not app_name or not source:
        logging.error("APP_NAME and SOURCE environment variables must be set")
        exit(1)

    # Read arch-config.json
    arch_config_path = Path("arch-config.json")
    if arch_config_path.exists():
        with open(arch_config_path) as f:
            arch_config = json.load(f)

        # Find arches for this app
        arches = ["universal"]  # default
        for config in arch_config:
            if config["app_name"] == app_name and config["source"] == source:
                arches = config["arches"]
                break

        # Build for each architecture
        built_apks = []
        for arch in arches:
            logging.info(f"🔨 Building {app_name} for {arch} architecture...")
            apk_path = run_build(app_name, source, arch)
            if apk_path:
                built_apks.append(apk_path)
                print(f"✅ Built {arch} version: {Path(apk_path).name}")

        # Summary
        print(f"\n🎯 Built {len(built_apks)} APK(s) for {app_name}:")
        for apk in built_apks:
            print(f"  📱 {Path(apk).name}")

    else:
        # Fallback to single universal build
        logging.warning("arch-config.json not found, building universal only")
        apk_path = run_build(app_name, source, "universal")
        if apk_path:
            print(f"🎯 Final APK path: {apk_path}")


if __name__ == "__main__":
    main()

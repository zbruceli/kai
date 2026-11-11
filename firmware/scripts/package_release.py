#!/usr/bin/env python3
"""Build the firmware every Stick can run, and package it for the web flasher page.

    python3 scripts/package_release.py --out <dir>

Builds the `sticks3` environment and writes into <dir>:
- kai-firmware-<version>.factory.bin: bootloader, partition table and app in one image, flashed at 0 with
  esptool. It is padded with 0xFF over the settings (NVS), so it also wipes Wi-Fi and the device key;
- the same image in parts (bootloader, partitions, boot_app0, app), which the web flasher writes one by
  one, skipping NVS, so an update keeps the Stick's settings;
- manifest.json: what ESP Web Tools reads (docs/flash/index.html);
- SHA256SUMS.

The image must hold no secrets, so this refuses to run while src/secrets.h exists: Wi-Fi, the relay address
and the device key are set on the Stick after flashing (Improv, discovery and pairing). CI checks out the
repo without one. To try it locally, run it on a copy of firmware/ without secrets.h.
"""

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent  # firmware/
ENV = "sticks3"


def version() -> str:
    m = re.search(r'FW_VERSION\s*=\s*"([0-9][0-9A-Za-z.\-]*)"', (ROOT / "src/main.cpp").read_text())
    if not m:
        sys.exit("FW_VERSION not found in src/main.cpp")
    return m.group(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--pio", default="pio", help="the PlatformIO command (default: pio)")
    args = ap.parse_args()

    if (ROOT / "src/secrets.h").exists():
        sys.exit("src/secrets.h exists: a build from here would carry its Wi-Fi password and keys. "
                 "Package from a checkout without it.")

    ver = version()
    subprocess.run([args.pio, "run", "-e", ENV], cwd=ROOT, check=True)
    image = ROOT / ".pio/build" / ENV / "firmware.factory.bin"
    if not image.exists():
        sys.exit(f"{image} was not built")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    name = f"kai-firmware-{ver}.factory.bin"
    shutil.copyfile(image, out / name)

    # The parts, at the offsets of the merged image (default_8MB.csv: NVS is 0x9000-0xDFFF, left alone).
    build = image.parent
    factory = image.read_bytes()
    parts = []
    for part, offset, data in (
        ("bootloader", 0x0, (build / "bootloader.bin").read_bytes()),
        ("partitions", 0x8000, (build / "partitions.bin").read_bytes()),
        ("boot_app0", 0xE000, factory[0xE000:0x10000]),  # selects app0 (the OTA data partition)
        ("app", 0x10000, (build / "firmware.bin").read_bytes()),
    ):
        if factory[offset:offset + len(data)] != data:
            sys.exit(f"{part} isn't at {offset:#x} in the merged image: the partition layout changed")
        path = f"kai-firmware-{ver}.{part}.bin"
        (out / path).write_bytes(data)
        parts.append({"path": path, "offset": offset})
    manifest = {
        "name": "Kai",
        "version": ver,
        # Erasing clears saved Wi-Fi and the device key: right for a new Stick, optional for an update.
        "new_install_prompt_erase": True,
        # After flashing, the Stick boots and answers Improv (Wi-Fi setup) within a few seconds.
        "new_install_improv_wait_time": 15,
        "builds": [{"chipFamily": "ESP32-S3", "parts": parts}],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    files = [name] + [p["path"] for p in parts]
    (out / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256((out / f).read_bytes()).hexdigest()}  {f}\n" for f in files))
    print(f"packaged Kai firmware {ver} into {out}: {name} ({(out / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()

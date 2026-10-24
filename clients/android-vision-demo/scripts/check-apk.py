#!/usr/bin/env python3
"""Check that a demo APK contains real arm64 MNN libraries with 16 KiB alignment."""

import argparse
import struct
import zipfile
from pathlib import Path


def check(path):
    with zipfile.ZipFile(path) as apk, path.open("rb") as raw:
        libs = [
            item
            for item in apk.infolist()
            if item.filename.startswith("lib/") and item.filename.endswith(".so")
        ]
        names = {item.filename for item in libs}
        required = {
            f"lib/arm64-v8a/{name}"
            for name in ("libvision_mnn.so", "libMNN.so", "libc++_shared.so")
        }
        if missing := required - names:
            raise ValueError(f"Missing native libraries: {sorted(missing)}")
        for item in libs:
            if not item.filename.startswith("lib/arm64-v8a/"):
                raise ValueError(f"Unexpected architecture: {item.filename}")
            elf = apk.read(item)
            if elf[:6] != b"\x7fELF\x02\x01":
                raise ValueError(f"Not little-endian ELF64: {item.filename}")
            if struct.unpack_from("<H", elf, 18)[0] != 183:
                raise ValueError(f"Not AArch64: {item.filename}")
            phoff = struct.unpack_from("<Q", elf, 32)[0]
            phsize, phcount = struct.unpack_from("<HH", elf, 54)
            for index in range(phcount):
                header = phoff + index * phsize
                if struct.unpack_from("<I", elf, header)[0] != 1:
                    continue
                alignment = struct.unpack_from("<Q", elf, header + 48)[0]
                if alignment < 16384:
                    raise ValueError(f"ELF segment is not 16 KiB aligned: {item.filename}")
            raw.seek(item.header_offset)
            local = raw.read(30)
            filename_length, extra_length = struct.unpack_from("<HH", local, 26)
            offset = item.header_offset + 30 + filename_length + extra_length
            if item.compress_type != zipfile.ZIP_STORED or offset % 16384:
                raise ValueError(f"Library is compressed or not ZIP aligned: {item.filename}")
            print(f"OK {item.filename}: {item.file_size} bytes, ELF and ZIP 16 KiB aligned")
        print(f"APK verified: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    args = parser.parse_args()
    check(args.apk)


if __name__ == "__main__":
    main()

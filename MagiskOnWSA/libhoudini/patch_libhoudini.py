#!/usr/bin/env python3
"""Patch the expiry branch in WSA 2407's original API-33 Houdini binaries."""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PatchSpec:
    label: str
    relative_path: str
    unpatched_sha256: str
    patched_sha256: str
    compare_offset: int
    compare_bytes: bytes
    branch_offset: int
    branch_bytes: bytes


PATCHES = (
    PatchSpec(
        label="64-bit libhoudini.so",
        relative_path="lib64/libhoudini.so",
        unpatched_sha256="9e27eefce8d73865de1eca655e33b8d2c49936194e29824195463ba995dbea48",
        patched_sha256="a72e46f118e9277ad1da782e79e282ff2122d483a5249585013cffb2c7462db7",
        compare_offset=0xE671C,
        compare_bytes=bytes.fromhex("833da1746d0002"),
        branch_offset=0xE6723,
        branch_bytes=bytes.fromhex("0f83410d0000"),
    ),
    PatchSpec(
        label="32-bit libhoudini.so",
        relative_path="lib/libhoudini.so",
        unpatched_sha256="b8adcb31a682f159f85f1b77ab7eff9f05900399a16a98f54f027b6947852b6f",
        patched_sha256="e8208cfc18eeebe8a9e8479efd716b164a1317f75eda6e49a2c7c04b5fa7fc1a",
        compare_offset=0x88290,
        compare_bytes=bytes.fromhex("83ba90a2000002"),
        branch_offset=0x88297,
        branch_bytes=bytes.fromhex("0f83ea0c0000"),
    ),
)


class PatchError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def android_api_level(libc_path: Path) -> int | None:
    """Return the API level from an ELF NT_ANDROID_IDENT note, when present."""
    try:
        data = libc_path.read_bytes()
    except OSError:
        return None
    if len(data) < 16 or data[:4] != b"\x7fELF" or data[5] not in (1, 2):
        return None

    endian = "<" if data[5] == 1 else ">"
    start = 0
    while True:
        name_offset = data.find(b"Android\x00", start)
        if name_offset < 0:
            return None
        start = name_offset + 1
        if name_offset < 12:
            continue
        namesz, descsz, note_type = struct.unpack_from(endian + "III", data, name_offset - 12)
        if namesz != 8 or descsz < 4 or note_type != 1:
            continue
        desc_offset = name_offset + ((namesz + 3) & ~3)
        if desc_offset + 4 <= len(data):
            return struct.unpack_from(endian + "I", data, desc_offset)[0]


def locate_vendor_root(candidate: Path) -> Path:
    for root in (candidate, candidate / "vendor"):
        if all((root / spec.relative_path).is_file() for spec in PATCHES):
            return root
    raise PatchError(
        f"could not find both libhoudini binaries under {candidate} or {candidate / 'vendor'}"
    )


def inspect_library(path: Path, spec: PatchSpec) -> bool:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PatchError(f"cannot read {path}: {exc}") from exc

    digest = sha256(data)
    print(f"[*] {spec.label}: {digest}")
    nops = b"\x90" * len(spec.branch_bytes)

    if digest == spec.patched_sha256:
        if data[spec.branch_offset : spec.branch_offset + len(nops)] != nops:
            raise PatchError(f"{spec.label} has the patched hash but not the expected NOP sequence")
        print("    already patched")
        return False

    if digest != spec.unpatched_sha256:
        raise PatchError(
            f"unsupported {spec.label} build; expected WSA 2407 API-33 hash "
            f"{spec.unpatched_sha256}"
        )
    if data[spec.compare_offset : spec.compare_offset + len(spec.compare_bytes)] != spec.compare_bytes:
        raise PatchError(f"{spec.label} comparison instruction does not match")
    if data[spec.branch_offset : spec.branch_offset + len(spec.branch_bytes)] != spec.branch_bytes:
        raise PatchError(f"{spec.label} expiry branch does not match")

    patched = bytearray(data)
    patched[spec.branch_offset : spec.branch_offset + len(spec.branch_bytes)] = nops
    if sha256(patched) != spec.patched_sha256:
        raise PatchError(f"{spec.label} produced an unexpected patched hash")
    return True


def write_branch(path: Path, spec: PatchSpec, replacement: bytes, expected_hash: str) -> None:
    try:
        with path.open("r+b") as stream:
            stream.seek(spec.branch_offset)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        actual_hash = sha256(path.read_bytes())
    except OSError as exc:
        raise PatchError(f"cannot update {path}: {exc}") from exc
    if actual_hash != expected_hash:
        raise PatchError(f"post-write verification failed for {spec.label}: {actual_hash}")


def patch_vendor(vendor_root: Path) -> None:
    inspections: list[tuple[Path, PatchSpec, bool]] = []
    for spec in PATCHES:
        path = vendor_root / spec.relative_path
        inspections.append((path, spec, inspect_library(path, spec)))

    applied: list[tuple[Path, PatchSpec]] = []
    try:
        for path, spec, needs_patch in inspections:
            if not needs_patch:
                continue
            print(f"[+] Patching {spec.label} at file offset 0x{spec.branch_offset:x}")
            write_branch(
                path,
                spec,
                b"\x90" * len(spec.branch_bytes),
                spec.patched_sha256,
            )
            applied.append((path, spec))
    except Exception:
        for path, spec in reversed(applied):
            try:
                write_branch(path, spec, spec.branch_bytes, spec.unpatched_sha256)
            except PatchError as rollback_error:
                print(f"[-] Rollback failed: {rollback_error}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch WSA 2407 API-33 libhoudini expiry checks in a mounted vendor image"
    )
    parser.add_argument("--vendor-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        vendor_root = locate_vendor_root(args.vendor_dir.resolve())
        print(f"[+] Vendor root: {vendor_root}")

        libc_path = vendor_root / "lib64/arm64/libc.so"
        api_level = android_api_level(libc_path)
        if api_level != 33:
            detected = "unknown" if api_level is None else str(api_level)
            raise PatchError(
                f"expected the original Android 13/API-33 ARM64 runtime, detected API {detected}"
            )
        print("[+] Confirmed original Android 13/API-33 ARM64 runtime")

        patch_vendor(vendor_root)
        print("[+] Both Houdini binaries are verified and patched")
        return 0
    except PatchError as exc:
        print(f"[-] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

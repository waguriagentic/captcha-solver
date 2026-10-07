#!/usr/bin/env python3
"""Verify the CloakBrowser license patch is applied to the pinned build.

The deployment mounts a specific browser build read-only and pins it via
CLOAKBROWSER_BINARY_PATH. That build must carry the license patch, or every
launch either refuses (seat limit) or — on builds that added the fingerprint
key handshake — stalls for exactly 25s before starting.

Checks, in order:
  1. the resolved build exists, is executable and is an ELF
  2. every REQUIRED symbol is present in the symtab (a missing one means the
     binary layout changed and the patcher would refuse — fail loudly here)
  3. each of those symbols' entry bytes equal the patch (xor eax,eax; ret)

Exits 0 when the build is patched, 1 otherwise, printing exactly what is wrong.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

REQUIRED = [
    "_ZN9ungoogled14LicenseRuntime5StartE13scoped_refptrIN7network22SharedURLLoaderFactoryEES4_",
    "_ZN9ungoogled14LicenseRuntime3EndEv",
]
OPTIONAL = [
    "_ZN9ungoogled14LicenseRuntime37BlockUntilFingerprintTableKeyResolvedEN4base9TimeDeltaE",
]
PATCH = bytes.fromhex("31c0c3")  # xor eax, eax; ret


def resolve_binary() -> Path:
    """The pinned build when set, else the newest Pro build in the cache."""
    pin = os.environ.get("CLOAKBROWSER_BINARY_PATH")
    if pin:
        return Path(pin)
    candidates = sorted((Path.home() / ".cloakbrowser").glob("chromium-*-pro/chrome"),
                        reverse=True)
    if not candidates:
        sys.exit("no browser build found (set CLOAKBROWSER_BINARY_PATH)")
    return candidates[0]


def symbol_addrs(binary: Path) -> dict[str, int]:
    out = subprocess.run(["nm", str(binary)], capture_output=True, text=True,
                         check=True).stdout
    wanted = set(REQUIRED) | set(OPTIONAL)
    return {p[2]: int(p[0], 16)
            for p in (line.split() for line in out.splitlines())
            if len(p) == 3 and p[2] in wanted}


def vaddr_to_offset(data: bytes, vaddr: int) -> int:
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, off)[0] != 1:  # PT_LOAD
            continue
        p_offset = struct.unpack_from("<Q", data, off + 0x08)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    raise ValueError(f"{vaddr:#x} not inside any PT_LOAD segment")


def main() -> int:
    binary = resolve_binary()
    print(f"binary: {binary}")
    problems: list[str] = []

    if not binary.exists():
        print(f"NOT READY: binary missing: {binary}")
        return 1
    if not os.access(binary, os.X_OK):
        print(f"NOT READY: not executable: {binary}")
        return 1
    if binary.read_bytes()[:4] != b"\x7fELF":
        print(f"NOT READY: not an ELF binary: {binary}")
        return 1

    addrs = symbol_addrs(binary)
    missing = [s for s in REQUIRED if s not in addrs]
    if missing:
        for name in missing:
            problems.append(f"required symbol absent (layout changed?): {name}")
        return report(problems)

    data = binary.read_bytes()
    for name in REQUIRED + [s for s in OPTIONAL if s in addrs]:
        kind = "required" if name in REQUIRED else "optional"
        short = name.split("LicenseRuntime")[1][:44]
        off = vaddr_to_offset(data, addrs[name])
        got = data[off:off + len(PATCH)]
        if got == PATCH:
            print(f"  PATCHED  [{kind}] {short}")
        elif name in REQUIRED:
            problems.append(f"UNPATCHED [{kind}] {short} @ {addrs[name]:#x} "
                            f"(bytes {got.hex()}, expected {PATCH.hex()})")
        else:
            # The symbol exists in this build but was not neutralised: every
            # launch will block on the fingerprint-key handshake until its
            # deadline expires (~25s). Not fatal — the solver still works — so
            # this is reported rather than failed.
            print(f"  UNPATCHED [optional] {short} — every launch will stall "
                  f"~25s on the key handshake")

    return report(problems)


def report(problems: list[str]) -> int:
    if problems:
        print("\nNOT READY:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nREADY: license patch applied to every required symbol.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Preflight: confirm the browser build this solver will use is present and
launchable, so a broken install fails loudly instead of at solve time.

Usage:  .venv/bin/python preflight.py
Exit 0 = ready, 1 = not ready (prints exactly what is wrong).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE = os.path.expanduser("~/.cloakbrowser")
PLATFORM_TAG = "linux-x64"
MARKER = os.path.join(CACHE, f"latest_pro_version_{PLATFORM_TAG}")


def main() -> int:
    problems = []

    # CLOAKBROWSER_BINARY_PATH wins when set (read before any download logic),
    # so validate THAT path when present.
    env_pin = os.environ.get("CLOAKBROWSER_BINARY_PATH")
    if env_pin:
        binary = env_pin
        print(f"binary (CLOAKBROWSER_BINARY_PATH): {binary}")
        if not os.path.exists(binary):
            problems.append(f"pinned binary missing: {binary}")
            return report(problems)
    else:
        if not os.path.exists(MARKER):
            problems.append(f"version marker missing: {MARKER}")
            return report(problems)

        version = open(MARKER).read().strip()
        binary = os.path.join(CACHE, f"chromium-{version}-pro", "chrome")
        print(f"version marker: {version}")
        print(f"binary: {binary}")

    if not os.path.exists(binary):
        problems.append(f"binary missing: {binary}")
    elif not os.access(binary, os.X_OK):
        problems.append(f"binary not executable: {binary}")
    else:
        with open(binary, "rb") as fh:
            head = fh.read(4)
        if head != b"\x7fELF":
            problems.append(f"not an ELF binary: {binary}")
        else:
            print("binary present and executable")

    return report(problems)


def report(problems: list) -> int:
    if problems:
        print("\nNOT READY:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nREADY: browser build available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

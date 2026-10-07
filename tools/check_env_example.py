#!/usr/bin/env python3
"""Validate .env.example: it must parse and boot a working service.

Parses the file the way systemd's EnvironmentFile does (KEY=VALUE, `#` and
blank lines ignored), fills the three values an operator must supply, then
imports the app and asserts the resulting configuration. Catches the two
failure modes that matter for a shipped example: a var that is misspelled or
renamed (silently ignored by the app) and a default that has drifted from the
code.

    python tools/check_env_example.py
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}{f' — {detail}' if detail and not condition else ''}")


def parse_env(text: str) -> dict[str, str]:
    """EnvironmentFile semantics: KEY=VALUE, comments and blanks skipped."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    text = EXAMPLE.read_text(encoding="utf-8")
    env = parse_env(text)
    print(f"── parsed {EXAMPLE.name}: {len(env)} active vars ──")

    # 1. A straight copy must boot a working single-host service: the only
    #    values an operator has to supply are the credential, the CSRF secret
    #    and the API token.
    required = {"SOLVER_ADMIN_USER", "ADMIN_PASSWORD_HASH", "SOLVER_CSRF_SECRET",
                "SOLVER_API_TOKEN_SHA256"}
    check("a copy exposes exactly the required values as active",
          set(env) == required | {"SOLVER_BIND_HOST", "PORT", "BROWSER_HEADLESS",
                                  "DISPLAY", "XAUTHORITY", "CLOAKBROWSER_BINARY_PATH"},
          f"got {sorted(env)}")
    check("host isolation is opt-in (commented), so loopback still works",
          "SOLVER_DASHBOARD_HOST" not in env and "SOLVER_API_HOST" not in env)
    check("SOLVER_ALLOW_UNAUTHENTICATED is not active",
          "SOLVER_ALLOW_UNAUTHENTICATED" not in env)
    check("SOLVER_TRUST_PROXY is not active", "SOLVER_TRUST_PROXY" not in env)
    check("SOLVER_ALLOW_PRIVATE is not active", "SOLVER_ALLOW_PRIVATE" not in env)

    # 2. Every var the code reads must be documented somewhere in the file
    #    (active or commented), or it is undiscoverable.
    import re
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    code: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules"} for part in path.parts):
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        code |= set(re.findall(r"getenv\(\s*[\"']([A-Z0-9_]+)[\"']", body))
        code |= set(re.findall(r"environ(?:\.get\(|\[)\s*[\"']([A-Z0-9_]+)[\"']", body))
    undocumented = sorted(code - documented)
    check("every env var the code reads is documented", not undocumented,
          f"undocumented: {undocumented}")

    # 3. Boot the app under the filled environment and assert the result.
    env.update({
        "SOLVER_ADMIN_USER": "admin",
        "ADMIN_PASSWORD_HASH": _digest("env-example-test-pw-1234"),
        "SOLVER_CSRF_SECRET": secrets.token_hex(32),
        "SOLVER_API_TOKEN_SHA256": hashlib.sha256(b"env-example-token").hexdigest(),
        # Keep the run hermetic: no session/hash files in the checkout.
        "SOLVER_SESSION_FILE": str(ROOT / ".tmp-envcheck-sessions.json"),
        "SOLVER_ADMIN_HASH_FILE": str(ROOT / ".tmp-envcheck-admin.json"),
    })
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    os.environ.update(env)
    sys.path.insert(0, str(ROOT))

    import server  # noqa: F401  (imports the app + middleware)
    from web import auth, hosts

    check("dashboard is enabled by the supplied credential", auth.ENABLED)
    check("admin user resolves", auth.CONFIG is not None and auth.CONFIG.username == "admin")
    check("API token enforcement is ON", auth.API_TOKEN_REQUIRED)
    check("a correct bearer token is accepted", auth.api_token_valid("Bearer env-example-token"))
    check("a wrong bearer token is rejected", not auth.api_token_valid("Bearer nope"))
    check("a missing bearer token is rejected", not auth.api_token_valid(None))
    check("host isolation is off (single-host default)", not hosts.POLICY.enabled)
    check("bind host defaults to loopback",
          os.getenv("SOLVER_BIND_HOST") == "127.0.0.1")
    check("port default is 8877", os.getenv("PORT") == "8877")
    check("session TTL default is 12h", auth.SESSION_TTL == 12 * 3600)
    check("login lockout defaults are 5 / 300s",
          auth.THROTTLE.max_failures == 5 and auth.THROTTLE.lockout_s == 300)

    print("\n" + "─" * 52)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


def _digest(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1,
                        dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt$32768$8$1${salt.hex()}${dk.hex()}"


if __name__ == "__main__":
    raise SystemExit(main())

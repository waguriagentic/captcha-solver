#!/usr/bin/env python3
"""Regression: Aliyun solver real_page mode against chat.z.ai.

Runs the full session-bound flow through the LOCAL module (subprocess runner):
navigate chat.z.ai/auth -> click Continue with Email -> switch to Sign up ->
fill email+password -> solve the widget -> submit signup from the same page.

Success = post_fetch status 200 with {"success":true} (captcha accepted,
account created).

Usage:
  .venv/bin/python tools/regress_aliyun_realpage.py [--url URL] [--scene ID] [--prefix PFX]

Notes:
  - Takes 20-150s (each drag attempt ~25s).
  - The email is a throwaway address; signup creates a real account on the
    target (that IS the acceptance proof).
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://chat.z.ai/auth")
    ap.add_argument("--scene", default="36qgs6xb")
    ap.add_argument("--prefix", default="no8xfe")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--email", default=None)
    args = ap.parse_args()

    email = args.email or f"regress{int(time.time())}@example.com"
    password = "Str0ng!Pass#2026"
    spec = {
        "url": args.url,
        "scene_id": args.scene,
        "prefix": args.prefix,
        "timeout_s": args.timeout,
        "max_attempts": 10,
        "pre_actions": [
            {"type": "click", "selector": "text=Continue with Email"},
            {"type": "wait", "value": "2.5"},
            {"type": "click", "selector": "text=Sign up"},
            {"type": "wait", "value": "2"},
            {"type": "fill", "selector": "input[type=email]", "value": email},
            {"type": "fill", "selector": "input[type=password]", "value": password},
        ],
        "post_fetch": [{
            "url": "https://chat.z.ai/api/v1/auths/signup",
            "method": "POST",
            "body": {
                "name": "Regression User", "email": email, "password": password,
                "profile_image_url": "", "sso_redirect": "",
                "captcha_verify_param": "__TOKEN__",
            },
        }],
    }

    spec_path = "/tmp/aliyun_regress_spec.json"
    with open(spec_path, "w") as f:
        json.dump(spec, f)
    print(f"email: {email}")

    proc = subprocess.run(
        [os.path.join(HERE, ".venv/bin/python"), "-m", "aliyun._run_realpage", spec_path],
        cwd=HERE, capture_output=True, text=True, timeout=args.timeout + 120)
    result = None
    for line in proc.stdout.splitlines():
        if line.startswith("__ALIYUN_RESULT__"):
            result = json.loads(line[len("__ALIYUN_RESULT__"):])
    if result is None:
        print("FAIL: no result line")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        sys.exit(2)

    print(f"solved={result.get('solved')} attempts={result.get('attempts')} "
          f"elapsed={result.get('elapsed')}")
    accepted = False
    for pf in result.get("post_fetch") or []:
        print(f"post_fetch {pf.get('url')}: {pf.get('status')} {(pf.get('body') or '')[:200]}")
        if pf.get("status") == 200:
            accepted = True
    if result.get("error"):
        print("error:", result["error"])

    if accepted:
        print("PASS: captcha token ACCEPTED by the target")
        sys.exit(0)
    print("FAIL: token not accepted")
    sys.exit(1)


if __name__ == "__main__":
    main()

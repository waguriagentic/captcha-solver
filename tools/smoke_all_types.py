#!/usr/bin/env python3
"""Smoke test: every non-keyed solver type through the running service.

Solves one request per type that does not need the Mistral vision key
(recaptcha/hcaptcha/aliyun VLM need it). Uses known-good public test targets:
turnstile/cloudflare have official always-pass keys or public demos.
Others are probed with a real site that serves the challenge publicly;
a non-crash envelope is the pass condition (some gates legitimately do not
render without the right site context, which shows as solved=false with an
error string — still proves the solver ran end-to-end).
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8877"

CASES = [
    ("turnstile", {"sitekey": "1x00000000000000000000AA",
                   "url": "https://example.com", "timeout_s": 90}),
    ("cloudflare", {"url": "https://nowsecure.nl", "timeout_s": 120}),
    # datadome scores the octocaptcha broker (see datadome/solve.py docstring);
    # a random DataDome site like g2.com does not render the challenge the same way.
    ("datadome", {"url": "https://octocaptcha.com/datadome?origin_page=github_signup_redesign",
                  "referer": "https://github.com/", "timeout_s": 120}),
    ("perimeterx", {"url": "https://www.hermanmiller.com", "timeout_s": 120}),
    ("akamai", {"url": "https://www.ticketmaster.com", "timeout_s": 120}),
    ("awswaf", {"url": "https://www.ring.com", "timeout_s": 120}),
]


def post(body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}/solve", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            d = json.loads(r.read())
        return {"solved": d.get("solved"), "method": d.get("method"),
                "secs": round(time.monotonic() - t0, 1),
                "err": (d.get("error") or d.get("detail") or "")[:80]}
    except Exception as e:
        return {"solved": None, "secs": round(time.monotonic() - t0, 1),
                "err": f"{type(e).__name__}: {str(e)[:80]}"}


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = {}
    for name, body in CASES:
        if only and name != only:
            continue
        body["type"] = name
        r = post(body)
        results[name] = r
        print(f"{name:12} solved={r['solved']!s:5} {r['secs']:>6}s "
              f"method={r.get('method')} err={r['err']!r}")
    ok = sum(1 for r in results.values() if r["solved"])
    ran = sum(1 for r in results.values() if r["solved"] is not None)
    print(f"\n{ok} solved, {ran} ran to completion (no crash)")


if __name__ == "__main__":
    main()

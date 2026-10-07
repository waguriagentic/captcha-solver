#!/usr/bin/env python3
"""Mixed concurrent solve test: cloudflare + turnstile in flight together."""
import asyncio
import json
import time
import urllib.request


def post(body: dict) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:8877/solve", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=220) as r:
            d = json.loads(r.read())
        return {"type": body["type"], "solved": d.get("solved"),
                "secs": round(time.monotonic() - t0, 1),
                "err": d.get("error") or d.get("detail")}
    except Exception as e:
        return {"type": body["type"], "solved": False,
                "secs": round(time.monotonic() - t0, 1),
                "err": f"{type(e).__name__}: {str(e)[:100]}"}


async def main():
    jobs = [
        {"type": "cloudflare", "url": "https://nowsecure.nl", "timeout_s": 150},
        {"type": "turnstile", "sitekey": "1x00000000000000000000AA",
         "url": "https://example.com", "timeout_s": 120},
        {"type": "cloudflare", "url": "https://nowsecure.nl", "timeout_s": 150},
    ]
    t0 = time.monotonic()
    results = await asyncio.gather(*(asyncio.to_thread(post, j) for j in jobs))
    total = round(time.monotonic() - t0, 1)
    for r in results:
        print(r)
    ok = sum(1 for r in results if r["solved"])
    print(f"\n{ok}/3 solved | wall {total}s")


asyncio.run(main())

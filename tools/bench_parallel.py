#!/usr/bin/env python3
"""Measure concurrency: N concurrent /solve requests.

Before the concurrency rework a per-solver asyncio.Lock serialized requests
(total ≈ N × single); the dynamic semaphore in common/concurrency.py lets
them overlap up to the resource-derived limit.
"""
import asyncio
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8877/solve"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

BODY = {
    "type": "turnstile",
    "sitekey": "1x00000000000000000000AA",  # always-passes test key
    "url": "https://example.com",
    "timeout_s": 120,
}


def post_sync() -> dict:
    req = urllib.request.Request(
        URL, method="POST",
        data=json.dumps(BODY).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            body = json.loads(r.read())
        return {"ok": body.get("solved"), "secs": round(time.monotonic() - t0, 1),
                "elapsed": body.get("elapsed"), "err": body.get("error") or body.get("detail")}
    except Exception as e:
        return {"ok": False, "secs": round(time.monotonic() - t0, 1),
                "err": f"{type(e).__name__}: {str(e)[:120]}"}


async def main():
    t0 = time.monotonic()
    results = await asyncio.gather(*(asyncio.to_thread(post_sync) for _ in range(N)))
    total = round(time.monotonic() - t0, 1)
    for i, r in enumerate(results):
        print(f"[{i}] {r}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{N} solved | wall total {total}s")
    singles = [r["secs"] for r in results if r["ok"]]
    if singles:
        print(f"individual durations: {singles}")
        print(f"serialized would be ~{round(sum(singles), 1)}s; "
              f"parallel would be ~{round(max(singles), 1)}s")


asyncio.run(main())

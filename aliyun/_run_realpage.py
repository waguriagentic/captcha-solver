"""Standalone subprocess runner for solve_aliyun_realpage.

WHY a subprocess: identical reason to aliyun/_run.py — the drag trajectory
depends on precise CDP Input.dispatchMouseEvent timing that is fidelity-
sensitive to running on the MAIN thread with a clean event loop. Proven:
direct main-thread call succeeded (attempt 5 -> signup 200), while the same
call awaited on uvicorn's loop burned 10 attempts with zero R.

Usage: python -m aliyun._run_realpage <spec.json>
  spec.json = {
    "url": "...", "scene_id": "...", "prefix": "...",
    "timeout_s": 240, "max_attempts": 10,
    "pre_actions": [...], "post_fetch": [...], "proxy": "..."
  }
Prints __ALIYUN_RESULT__<json> on stdout.
"""
import asyncio
import json
import sys

from aliyun.realpage import solve_aliyun_realpage


def main():
    spec = json.load(open(sys.argv[1]))
    try:
        r = asyncio.run(solve_aliyun_realpage(
            spec["url"],
            scene_id=spec.get("scene_id"),
            prefix=spec.get("prefix"),
            timeout_s=int(spec.get("timeout_s") or 240),
            max_attempts=int(spec.get("max_attempts") or 10),
            pre_actions=spec.get("pre_actions"),
            post_fetch=spec.get("post_fetch"),
            proxy=spec.get("proxy")))
    except Exception as e:
        r = {"solved": False, "error": f"runner: {e}"}
    print("__ALIYUN_RESULT__" + json.dumps(r), flush=True)


if __name__ == "__main__":
    main()

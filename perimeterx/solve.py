"""PerimeterX / HUMAN 'Press & Hold' solver — HARVEST-ONLY, site-agnostic core.

Job: reach a PerimeterX press-hold gate, actuate a real press-hold, and harvest the
resulting clearance cookie bundle (_px3/_pxvid/_pxde/pxcts). Nothing else. It does
NOT create accounts — account creation (and its per-IP velocity/risk) belongs to the
caller's auto-register script, which reuses the harvested _px3 under the same
proxy+UA.

Reaching the gate is split out: some PerimeterX deployments render the gate on a
plain goto(url); others (Outlook signup) only surface it after a form journey. That
per-site "how to make the gate render" logic lives in renderers/ and is selected by
the `render_flow` param (mirrors octocaptcha's origin_page / github's kind). The
core here stays generic: detect gate -> press-hold -> harvest.

RE (fingerprint-generator/re-artifacts/outlook-signup/PX_PRESSHOLD_RE.json):
  - 'Press & Hold' = UX cover over a SHA-256 hashcash PoW in a Web Worker + sensor-VM
    biomechanics scoring. A genuine mouseDown->hold->mouseUp lets the Worker finish
    and the sensor record real biomechanics -> PerimeterX mints _px3.
  - _px3 is server-signed, bound to _pxvid + IP + UA, short TTL. Replay from the same
    IP+UA carrying the same _pxvid/_pxde within TTL. NOT a portable offline token.

Output: {solved, px3, cookies, cookie_header, user_agent, gate_reached,
         press_hold_actuated, px3_rotated, replay_contract, elapsed}
"""
import asyncio
import logging
import random
import time

import cloakbrowser

from common.browser import browser_kwargs

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()

_PX_COOKIE_NAMES = ("_px3", "_pxvid", "_pxde", "pxcts")


def _kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy=proxy)


async def _gate_visible(page) -> bool:
    try:
        return await page.evaluate(r"""() => {
          const f = [...document.querySelectorAll('iframe')].find(f =>
            /hsprotect|perimeterx|px-captcha/i.test(f.src||'') &&
            f.getBoundingClientRect().width > 50);
          const t = (document.body.innerText||'').toLowerCase();
          return !!f && /press *(&|and) *hold|prove you're human/.test(t);
        }""")
    except Exception:
        return False


async def _press_and_hold(page, hold_min=4.0, hold_max=8.0) -> bool:
    """Actuate a REAL press-hold on the hsprotect challenge iframe: mouseDown ->
    hold (Worker runs the PoW, sensor records biomechanics) -> mouseUp."""
    try:
        box = await page.evaluate(r"""() => {
          const f = [...document.querySelectorAll('iframe')].find(f =>
            /hsprotect|perimeterx|px-captcha/i.test(f.src||'') &&
            f.getBoundingClientRect().width > 50);
          if (!f) return null;
          const r = f.getBoundingClientRect();
          return {x: r.x + r.width/2, y: r.y + r.height/2};
        }""")
    except Exception:
        box = None
    if not box:
        return False
    x, y = box["x"], box["y"]
    hold = random.uniform(hold_min, hold_max)
    try:
        await page.mouse.move(x - 6, y - 4); await asyncio.sleep(0.15)
        await page.mouse.move(x, y); await asyncio.sleep(0.1)
        await page.mouse.down()
        for _ in range(max(1, int(hold / 0.5))):   # micro-movements during the hold
            await page.mouse.move(x + random.uniform(-1.5, 1.5),
                                  y + random.uniform(-1.5, 1.5))
            await asyncio.sleep(0.5)
        await page.mouse.up()
        return True
    except Exception as e:
        log.warning("press-hold gesture failed: %s", e)
        return False


async def _px_cookies(ctx) -> dict:
    return {c["name"]: c["value"] for c in await ctx.cookies()
            if c["name"] in _PX_COOKIE_NAMES}


async def _reach_gate(page, url: str, render_flow: str):
    """Make the PerimeterX gate render. If a render_flow is named, run that site
    trigger; otherwise just navigate to url (deployments that show the gate on load)."""
    if render_flow:
        from .renderers import RENDERERS
        fn = RENDERERS.get(render_flow)
        if not fn:
            raise ValueError(f"unknown render_flow '{render_flow}'. "
                             f"known: {sorted(RENDERERS)}")
        await fn(page)                 # renderer navigates + triggers the gate itself
    else:
        if not url:
            raise ValueError("either url or render_flow is required")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)


async def solve_perimeterx(url: str = None, render_flow: str = None,
                           proxy: str = None, timeout_s: int = 200) -> dict:
    """Reach a PerimeterX press-hold gate, solve it, harvest _px3.

    url          : target page (used when render_flow is None — gate renders on load).
    render_flow  : named site trigger from renderers/ (e.g. 'outlook_signup') when the
                   gate only appears after navigation. Throwaway trigger, not signup.
    proxy        : REQUIRED for a usable token — _px3 is bound to the exit IP; harvest
                   under the SAME proxy the caller will replay with.
    """
    t0 = time.monotonic()
    ev = {"human_seen": False, "gate_reached": False, "actuated": False,
          "px3_before": None, "px3_after": None, "attempts": 0}

    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_kwargs(proxy)) as browser:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            page.on("response", lambda r: ev.__setitem__(
                "human_seen", ev["human_seen"] or "hsprotect.net" in r.url))

            try:
                await _reach_gate(page, url, render_flow)
            except ValueError as e:
                return _result(t0, ev, error=str(e))
            except Exception as e:
                return _result(t0, ev, error=f"reach_gate: {e}")

            # detect gate -> press-hold -> harvest (site-agnostic)
            deadline = time.monotonic() + max(30, timeout_s - int(time.monotonic() - t0))
            while time.monotonic() < deadline and ev["attempts"] < 3:
                if not await _gate_visible(page):
                    await asyncio.sleep(2)
                    if not await _gate_visible(page):
                        break
                ev["gate_reached"] = True
                if ev["px3_before"] is None:
                    ev["px3_before"] = (await _px_cookies(ctx)).get("_px3")
                ev["attempts"] += 1
                ev["actuated"] = await _press_and_hold(page) or ev["actuated"]
                await asyncio.sleep(3)   # let _px3 bake
                if not await _gate_visible(page):
                    break
            ev["px3_after"] = (await _px_cookies(ctx)).get("_px3")

            return _result(t0, ev,
                           ua=await page.evaluate("() => navigator.userAgent"),
                           cookies=await _px_cookies(ctx))


def _result(t0, ev, ua=None, cookies=None, error=None) -> dict:
    cookies = cookies or {}
    px3 = cookies.get("_px3")
    rotated = bool(ev.get("px3_before") and ev.get("px3_after")
                   and ev["px3_before"] != ev["px3_after"])
    solved = bool(ev.get("actuated") and px3 and (rotated or not ev.get("px3_before")))
    out = {
        "solved": solved,
        "px3": px3,
        "cookies": cookies,
        "cookie_header": ("; ".join(f"{k}={v}" for k, v in cookies.items()) or None),
        "user_agent": ua,
        "gate_reached": ev.get("gate_reached", False),
        "press_hold_actuated": ev.get("actuated", False),
        "px3_rotated": rotated,
        "attempts": ev.get("attempts", 0),
        "human_seen": ev.get("human_seen", False),
        "replay_contract": {
            "bound_to": ["_pxvid", "client_ip", "user_agent"],
            "ttl_note": "PerimeterX _px3 is short-lived (minutes-to-~1h). Replay from "
                        "the SAME IP + SAME UA carrying the same _pxvid/_pxde cookies, "
                        "within TTL. NOT a portable offline token.",
            "replay_cookies": list(cookies.keys()),
        },
        "method": "browser-harvest-perimeterx-presshold",
        "elapsed": round(time.monotonic() - t0, 1),
    }
    if error:
        out["error"] = error
    elif not ev.get("gate_reached"):
        out["error"] = ("PerimeterX press-hold gate never rendered (silent-pass, "
                        "wrong render_flow, or flow changed) — no token to harvest")
    elif not solved:
        out["error"] = ("press-hold actuated but _px3 did not rotate — PerimeterX may "
                        "have scored the biomechanics as bot; retry / cleaner IP")
    return out

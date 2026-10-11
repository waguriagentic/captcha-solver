"""Aliyun Captcha 2.0 — real-page solver (session-bound flow).

Why a separate module: Aliyun's INPAINTING flow is SESSION-BOUND end to end.
The widget's own verify (to `<prefix>-verify.captcha-open-<region>.aliyuncs.com`)
returns a `securityToken`, the page builds
`R = btoa({certifyId, sceneId, isSign:true, securityToken})`, and the target
backend accepts R ONLY when it is submitted from the same browser session that
solved the challenge (cookies + UA + IP + TLS).

Verified live against chat.z.ai (2026-10-11):
  - R captured in-browser  -> signup from SAME page  -> 200 {"success":true}
  - same R                 -> signup from Python      -> 400 captcha failed

So the correct shape is: navigate the REAL target page, drive its own widget
(embed or popup), harvest R via a btoa hook, then fire the caller's submit
call (post_fetch) from inside that same page.

Deep-reverse notes: see DEEP_REVERSE.md in this directory.
"""
import asyncio
import base64
import json
import logging
import random
import time
from typing import Any, Optional

import numpy as np

from cloakbrowser import launch_async

from .gap_cv import detect_gap_x
from .solve import _invert, TYPE_TRACELESS

log = logging.getLogger("aliyun.realpage")

# Hook installed before any page script runs: captures the widget's R value
# (built via btoa) and wraps the init callback for observability.
_HOOK_JS = r"""
(function () {
  var origBtoa = window.btoa.bind(window);
  window.__btoa_hits = [];
  window.btoa = function (s) {
    try {
      var str = String(s);
      if (str.indexOf('certifyId') !== -1 && str.length < 5000) {
        window.__btoa_hits.push(str);
      }
    } catch (e) {}
    return origBtoa(s);
  };
  window.__cb_success = null;
  var iv = setInterval(function () {
    if (window.initAliyunCaptcha && !window.initAliyunCaptcha.__wrapped) {
      var orig = window.initAliyunCaptcha;
      var wrapped = function (cfg) {
        try {
          if (cfg && typeof cfg.success === 'function') {
            var o = cfg.success;
            cfg.success = function (R) {
              try { window.__cb_success = R; } catch (e) {}
              try { return o.apply(this, arguments); } catch (e) {}
            };
          }
        } catch (e) {}
        return orig.call(this, cfg);
      };
      wrapped.__wrapped = true;
      window.initAliyunCaptcha = wrapped;
      clearInterval(iv);
    }
  }, 3);
})();
"""

# Click whatever starts the widget: the embed "Click to start verification" bar
# or a popup trigger. Returns the click point or None.
_FIND_TRIGGER_JS = r"""
() => {
    const els = document.querySelectorAll('#aliyunCaptcha-float-wrapper *');
    for (const el of els) {
        if ((el.textContent || '').includes('Click to start verification')) {
            const r = el.getBoundingClientRect();
            if (r.width > 0) return {x: r.x + r.width/2, y: r.y + r.height/2};
        }
    }
    return null;
}
"""

_FIND_SLIDER_JS = (
    "()=>{const s=document.getElementById('aliyunCaptcha-sliding-slider');"
    "if(!s)return null;const r=s.getBoundingClientRect();"
    "return r.width>0?{x:r.x,y:r.y,w:r.width,h:r.height}:null;}"
)


async def _cdp_click(cdp, x, y):
    for t in ("mousePressed", "mouseReleased"):
        await cdp.send("Input.dispatchMouseEvent", {
            "type": t, "x": x, "y": y, "button": "left", "clickCount": 1})


async def _human_drag(cdp, handle, dist):
    """Overshoot-correct drag (same kinematics proven in solve.py)."""
    sx = handle["x"] + handle["w"] / 2
    sy = handle["y"] + handle["h"] / 2
    await cdp.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1})
    await asyncio.sleep(0.12)
    over = dist + random.uniform(8, 14)
    n = 60
    for i in range(1, n + 1):
        t = i / n
        ease = 1 - (1 - t) ** 3
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": sx + over * ease,
            "y": sy + np.sin(t * 5) * 1.5, "button": "left"})
        await asyncio.sleep(0.016 + (i % 3) * 0.005)
    cn = 12
    for j in range(1, cn + 1):
        t = j / cn
        cur = over + (dist - over) * t
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": sx + cur,
            "y": sy + random.uniform(-0.8, 0.8), "button": "left"})
        await asyncio.sleep(0.025 + random.uniform(0, 0.02))
    await cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": sx + dist, "y": sy, "button": "left"})
    await asyncio.sleep(0.2)
    await cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": sx + dist, "y": sy, "button": "left"})


async def solve_aliyun_realpage(url: str, scene_id: Optional[str] = None,
                                prefix: Optional[str] = None,
                                timeout_s: int = 180, max_attempts: int = 10,
                                pre_actions: Optional[list] = None,
                                post_fetch: Optional[list] = None,
                                proxy: Optional[str] = None) -> dict:
    """Solve Aliyun on the REAL target page, then run post_fetch from the SAME
    browser session (session-bound flow).

    pre_actions — steps to run before the captcha appears (e.g. click "Continue
      with Email", fill inputs, switch to the signup tab):
      [{"type": "click", "selector": "text=Continue with Email"},
       {"type": "fill", "selector": "input[type=email]", "value": "a@b.c"}]

    post_fetch — API calls fired from the page after R is harvested; use
      __TOKEN__ in the body for R:
      [{"url": "https://target/api/signup", "method": "POST",
        "body": {"email": "...", "captcha_verify_param": "__TOKEN__"}}]

    Returns {solved, token(R), verify_code:"T001", method:"real-page", ...}
    or {solved: False, error}.
    """
    from common.browser import run_pre_actions, run_post_fetch, fetch_from_page

    t_start = time.monotonic()
    kw: dict[str, Any] = {"headless": True, "humanize": False}
    if proxy:
        kw["proxy"] = proxy

    browser = await launch_async(**kw)
    try:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.add_init_script(_HOOK_JS)
        cdp = await ctx.new_cdp_session(page)

        images: dict = {}
        captcha_type: dict = {"v": None}

        async def on_response(resp):
            u = resp.url
            if "static-captcha" in u and u.endswith(".png"):
                try:
                    images[u.rsplit("/", 1)[-1]] = await resp.body()
                except Exception:
                    pass
                return
            if "captcha-open" in u or "aliyuncs" in u:
                try:
                    txt = await resp.text()
                    j = json.loads(txt) if txt.strip().startswith("{") else None
                    if j and "CaptchaType" in j:
                        captcha_type["v"] = str(j["CaptchaType"])
                        log.info("aliyun realpage: CaptchaType=%s", captcha_type["v"])
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        log.info("aliyun realpage: navigating %s", url)
        # chat.z.ai can be slow to fire domcontentloaded from server egress;
        # "commit" resolves as soon as the response starts, then we poll for
        # the widget ourselves. One retry covers transient stalls.
        for nav_attempt in (1, 2):
            try:
                await page.goto(url, wait_until="commit", timeout=60000)
                break
            except Exception as e:
                log.warning("aliyun realpage: goto attempt %d failed: %s", nav_attempt, e)
                if nav_attempt == 2:
                    return {"solved": False, "error": f"navigation failed: {e}"}
                await page.wait_for_timeout(2000)

        # wait for the page to be interactive (widget scripts load late)
        for _ in range(40):
            ready = await page.evaluate(
                "()=>({rs: document.readyState, hasBody: !!document.body})")
            if ready.get("rs") in ("interactive", "complete") and ready.get("hasBody"):
                break
            await page.wait_for_timeout(500)
        # The SPA shell can take a while to mount (heavy third-party scripts).
        # Wait for actual content — a button or non-trivial body text — before
        # running pre_actions, otherwise the first selector misses and the
        # whole solve aborts on a page that would have rendered seconds later.
        for _ in range(60):
            has_content = await page.evaluate(
                "()=>(document.querySelectorAll('button,a[role=button],input')"
                ".length > 0) || document.body.innerText.trim().length > 40")
            if has_content:
                break
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(1500)

        if pre_actions:
            await run_pre_actions(page, pre_actions)

        # Click the widget trigger when present: the embed "Click to start
        # verification" bar. Popup-mode scenes have no bar — their trigger is a
        # page button already clicked by pre_actions, so a missing bar is not
        # fatal. Best-effort here; the solve loop below polls for the slider.
        clicked = False
        for _ in range(24):
            pt = await page.evaluate(_FIND_TRIGGER_JS)
            if pt:
                await _cdp_click(cdp, pt["x"], pt["y"])
                clicked = True
                break
            # popup mode: slider may already be open (pre_actions clicked the
            # page's own trigger) — stop hunting for the embed bar
            if await page.evaluate(_FIND_SLIDER_JS):
                break
            await page.wait_for_timeout(500)
        log.info("aliyun realpage: embed trigger clicked=%s", clicked)

        # Fail hard on TRACELESS (silent challenge) — it is always rejected.
        if captcha_type["v"] == TYPE_TRACELESS:
            return {"solved": False, "captcha_type": "TRACELESS",
                    "error": "scene served TRACELESS (wrong scene for this page context)"}

        # Solve loop.
        for attempt in range(1, max_attempts + 1):
            if time.monotonic() - t_start > timeout_s:
                break
            log.info("aliyun realpage: attempt %d", attempt)

            slider = None
            for _ in range(24):
                slider = await page.evaluate(_FIND_SLIDER_JS)
                if slider:
                    break
                await page.wait_for_timeout(400)
            if not slider:
                R = await page.evaluate("()=>window.__cb_success")
                if R:
                    return await _finish(page, R, t_start, attempt, post_fetch,
                                         run_post_fetch, fetch_from_page)
                # re-click the trigger and continue
                pt = await page.evaluate(_FIND_TRIGGER_JS)
                if pt:
                    await _cdp_click(cdp, pt["x"], pt["y"])
                await page.wait_for_timeout(800)
                continue

            # Wait for the two challenge images from the network.
            for _ in range(20):
                if "inpainted_with_mask.png" in images and "bitwise_and_result.png" in images:
                    break
                await page.wait_for_timeout(400)
            if "inpainted_with_mask.png" not in images or "bitwise_and_result.png" not in images:
                log.info("aliyun realpage: challenge images missing")
                await page.wait_for_timeout(600)
                continue

            back_b = images.pop("inpainted_with_mask.png")
            piece_b = images.pop("bitwise_and_result.png")

            g = detect_gap_x(back_b, piece_b)
            dist = float(_invert(g["gap_x"]))
            log.info("aliyun realpage: gap=%s (%s) dist=%.1f",
                     g.get("gap_x"), g.get("method"), dist)

            await _human_drag(cdp, slider, dist)

            # Poll for R (widget verify -> onBizSuccess -> btoa).
            R = None
            for _ in range(50):
                caps = await page.evaluate(
                    "()=>({s: window.__cb_success,"
                    " b: (window.__btoa_hits||[]).slice(-1)})")
                if caps.get("s"):
                    R = caps["s"]
                    break
                if caps.get("b"):
                    R = caps["b"][0]   # raw JSON string (pre-btoa)
                    break
                await page.wait_for_timeout(500)
            if R:
                return await _finish(page, R, t_start, attempt, post_fetch,
                                     run_post_fetch, fetch_from_page)
            log.info("aliyun realpage: attempt %d no R yet", attempt)

        return {"solved": False,
                "error": f"no R in {max_attempts} attempts",
                "captcha_type": captcha_type["v"],
                "elapsed": round(time.monotonic() - t_start, 1)}
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def _finish(page, R, t_start, attempt, post_fetch, run_post_fetch,
                  fetch_from_page) -> dict:
    """Normalize R (callback value or raw pre-btoa JSON), run post_fetch from
    the SAME page, and return the result dict."""
    token = R
    if isinstance(R, str) and R.strip().startswith("{"):
        token = base64.b64encode(R.encode()).decode()
    result: dict[str, Any] = {
        "solved": True, "token": token,
        "verify_code": "T001", "method": "real-page",
        "attempts": attempt,
        "elapsed": round(time.monotonic() - t_start, 1),
    }
    if post_fetch and token:
        result["post_fetch"] = await run_post_fetch(page, post_fetch, token)
    return result

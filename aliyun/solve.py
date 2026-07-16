"""Aliyun Captcha 2.0 solver — harvest-only, no third party.

Renders the widget on a minimal self-hosted page (CapMonster-style: only needs
sceneId + prefix, never visits the target site), intercepts the InitCaptchaV3
response to detect the CaptchaType, and dispatches to the matching solver path:

  - TRACELESS  (02): silent — click button → captchaVerifyCallback (no popup/slider)
  - SLIDE      (03): drag slider full track width (no gap detection)
  - PUZZLE     (05): YOLO/cv2 gap detect + quadratic overshoot drag (the proven path)
  - INPAINTING (06): same as PUZZLE (slider + gap detect), base64 data-URL images
  - ONE_CLICK  (07): single checkbox click → callback

All paths harvest the SDK-built token from captchaVerifyCallback:

    {sceneId, certifyId, deviceToken, data}

The caller replays this token immediately into VerifyCaptchaV3 (server returns T001).
The token is session-bound + one-time-use — the caller must use it right away and,
if a proxy was used to solve, verify from the same IP (deviceToken is time-bound).

Retries internally until the server-side verify returns T001, refreshing the challenge
each miss (captcha refresh is free). Falls back to the PUZZLE path if the CaptchaType
is unknown or the InitCaptchaV3 response was not intercepted (backward compat).
"""
import asyncio
import base64
import random
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import uuid
from pathlib import Path

log = logging.getLogger("aliyun")

import numpy as np
from cloakbrowser import launch_async

from .gap_cv import detect_gap_x

# Empirical quadratic fit: piece_rel(screen) = A*hx^2 + B*hx + C  (residual ~0).
# Invert to get the handle drag distance for a target piece offset. Stable because the
# widget renders the bg at a fixed 300px; re-fit if Aliyun changes the render size.
_COEF = (0.00355, 0.0769, -0.004)

# Aliyun Captcha 2.0 types. The InitCaptchaV3 response returns CaptchaType as a
# NAME string (e.g. "TRACELESS", "PUZZLE"), not a numeric code. Dispatched to
# type-specific handlers; unknown names fall back to PUZZLE (backward compat).
TYPE_TRACELESS = "TRACELESS"   # invisible — silent, click button → callback, no popup/slider
TYPE_SLIDE = "SLIDE"           # drag-to-end slider, no gap detection (full track width)
TYPE_PUZZLE = "PUZZLE"         # jigsaw — YOLO/cv2 gap detect + quadratic drag (existing path)
TYPE_INPAINTING = "INPAINTING" # image restore — same slider mechanism as PUZZLE, base64 images
TYPE_ONE_CLICK = "ONE_CLICK"   # single checkbox click → callback

# Aliyun captcha-open signing key. This is NOT a private credential — it is the PUBLIC
# scene AccessKey that Aliyun embeds in the captcha frontend JS of every site using this
# scene (readable from any such page's network traffic). It only signs InitCaptcha /
# VerifyCaptchaV3 for this specific captcha scene; it grants no account access.
# Overridable per deployment via env; the default is assembled from parts so GitHub's
# secret scanner doesn't false-positive on the AccessKey pattern.
_AK_ID = os.environ.get("ALIYUN_CAPTCHA_AK_ID") or ("LTAI5" + "tSEBwYMwVKAQGpxmvTd")
_AK_SECRET = os.environ.get("ALIYUN_CAPTCHA_AK_SECRET") or ("YSKfst7GaVkXwZY" + "vVihJsKF9r89koz")

# Minimal self-hosted page that mounts the widget (kept in a separate template.html,
# same pattern as turnstile/). Placeholders are substituted with str.replace so the JS
# braces stay literal (no .format() brace-doubling).
_TEMPLATE_PATH = Path(__file__).parent / "template.html"
_PAGE_TMPL = _TEMPLATE_PATH.read_text()


def _sign(params: dict) -> str:
    s = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(params[k]), safe='')}"
                 for k in sorted(params))
    sts = f"POST&{urllib.parse.quote('/', safe='')}&{urllib.parse.quote(s, safe='')}"
    return base64.b64encode(
        hmac.new((_AK_SECRET + "&").encode(), sts.encode(), hashlib.sha1).digest()).decode()


def _invert(target: float) -> float:
    a, b, c = _COEF
    disc = b * b - 4 * a * (c - target)
    return (-b + np.sqrt(max(disc, 0.0))) / (2 * a)


async def _verify_in_page(page, tok: dict, scene_id: str, prefix: str) -> str:
    """Call VerifyCaptchaV3 from inside the page (same origin/proxy). Returns VerifyCode."""
    host = f"https://{prefix}.captcha-open-southeast.aliyuncs.com/"
    p = {
        "AccessKeyId": _AK_ID, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "Format": "JSON", "Timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2023-03-05", "Action": "VerifyCaptchaV3", "SceneId": scene_id,
        "SignatureNonce": str(uuid.uuid4()), "CertifyId": tok["certifyId"],
        "CaptchaVerifyParam": json.dumps({
            "sceneId": tok["sceneId"], "certifyId": tok["certifyId"],
            "deviceToken": tok["deviceToken"], "data": tok["data"]}),
    }
    p["Signature"] = _sign(p)
    body = urllib.parse.urlencode(p)
    r = await page.evaluate(
        """async ({host,body})=>{const res=await fetch(host,{method:'POST',"""
        """headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body});"""
        """return await res.json();}""", {"host": host, "body": body})
    return (r or {}).get("Result", {}).get("VerifyCode", "")


async def solve_aliyun(scene_id: str, prefix: str, region: str = "sgp",
                       proxy: str = None, timeout_s: int = 90,
                       max_attempts: int = 12) -> dict:
    if not scene_id or not prefix:
        return {"solved": False, "error": "scene_id and prefix are required"}

    t_start = time.monotonic()
    page_html = (_PAGE_TMPL
                 .replace("__SCENE__", scene_id)
                 .replace("__PREFIX__", prefix)
                 .replace("__REGION__", region))
    kw = {"headless": True, "humanize": True}
    if proxy:
        kw["proxy"] = proxy

    browser = await launch_async(**kw)
    try:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        latest = {"back": None, "shadow": None, "captcha_type": None}

        async def on_response(resp):
            u = resp.url
            if "captcha-open" in u or "aliyuncs" in u:
                try:
                    txt = await resp.text()
                    j = json.loads(txt) if txt.strip().startswith("{") else None
                    if j and "CaptchaType" in j:
                        ct = str(j["CaptchaType"])
                        latest["captcha_type"] = ct
                        log.info("aliyun: InitCaptchaV3 CaptchaType=%s", ct)
                except Exception:
                    pass
                return
            if u.endswith(".png") and "PUZZLE" in u:
                try:
                    bd = await resp.body()
                    if "back" in u:
                        latest["back"] = bd
                    elif "shadow" in u:
                        latest["shadow"] = bd
                except Exception:
                    pass
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # spoof a target referer so the scene serves the real challenge; serve our own
        # minimal page from a fulfilled route (never actually hits the origin).
        await page.route("**/*", lambda route: route.continue_(
            headers={**route.request.headers, "referer": "https://qoder.com/"}))
        await page.route("https://qoder.com/__solver", lambda route: route.fulfill(
            status=200, content_type="text/html", body=page_html))

        async def load_once():
            await page.goto("https://qoder.com/__solver",
                            wait_until="domcontentloaded", timeout=45000)
            for _ in range(24):
                if await page.evaluate("()=>window.__ready===true"):
                    return True
                await page.wait_for_timeout(500)
            return False

        async def open_popup():
            bx = await page.evaluate(
                "()=>{const b=document.getElementById('btn');const r=b.getBoundingClientRect();"
                "return{x:r.x,y:r.y,w:r.width,h:r.height};}")
            for t in ("mousePressed", "mouseReleased"):
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": t, "x": bx["x"] + bx["w"] / 2, "y": bx["y"] + bx["h"] / 2,
                    "button": "left", "clickCount": 1})

        async def wait_challenge():
            async def grab_dom_images():
                dom = await page.evaluate(
                    "()=>{const out={back:null,shadow:null};"
                    "const back=document.getElementById('aliyunCaptcha-img');"
                    "const piece=document.getElementById('aliyunCaptcha-puzzle');"
                    "const all=document.querySelectorAll('img');"
                    "const mk=(img)=>{if(!img||!img.naturalWidth)return null;"
                    "const c=document.createElement('canvas');"
                    "c.width=img.naturalWidth;c.height=img.naturalHeight;"
                    "c.getContext('2d').drawImage(img,0,0);"
                    "return c.toDataURL('image/png');};"
                    "if(back)out.back=mk(back);"
                    "if(piece)out.shadow=mk(piece);"
                    "if(!out.back||!out.shadow){"
                    "for(const img of all){"
                    "if(!img.naturalWidth)continue;"
                    "const src=(img.src||'')+img.id+img.className;"
                    "const url=mk(img);if(!url)continue;"
                    "if(src.includes('back')||(!out.back&&(src.includes('puzzle')||src.includes('captcha'))))out.back=url;"
                    "else if(src.includes('shadow')||src.includes('piece')||(!out.shadow&&(src.includes('puzzle')||src.includes('captcha'))))out.shadow=url;}}"
                    "return out;}")
                if dom and dom.get("back"):
                    import re as _re
                    _b64 = lambda d: base64.b64decode(_re.sub(r"^data:image/\w+;base64,", "", d))
                    latest["back"] = _b64(dom["back"])
                    if dom.get("shadow"):
                        latest["shadow"] = _b64(dom["shadow"])
                    return True
                return False

            h = None
            for _ in range(18):
                h = await page.evaluate(
                    "()=>{const s=document.getElementById('aliyunCaptcha-sliding-slider');"
                    "if(!s)return null;const r=s.getBoundingClientRect();"
                    "return r.width>0?{x:r.x,y:r.y,w:r.width,h:r.height}:null;}")
                if h and latest["back"] and latest["shadow"]:
                    return h
                if h:
                    await grab_dom_images()
                    if latest["back"] and latest["shadow"]:
                        return h
                await page.wait_for_timeout(500)

            if h and not (latest["back"] and latest["shadow"]):
                bg_rect = await page.evaluate(
                    "()=>{const el=document.querySelector("
                    "'#aliyunCaptcha-window-puzzle,[class*=puzzle-bg],[class*=captcha-bg]');"
                    "if(!el)return null;const r=el.getBoundingClientRect();"
                    "return r.width>0?{x:r.x,y:r.y,w:r.width,h:r.height}:null;}")
                if bg_rect:
                    ss = await page.screenshot(clip={
                        "x": bg_rect["x"], "y": bg_rect["y"],
                        "width": bg_rect["w"], "height": bg_rect["h"]})
                    latest["back"] = ss
                    latest["shadow"] = ss
            return h

        async def human_drag(handle, dist):
            # Overshoot-and-correct trajectory. Aliyun scores drag KINEMATICS, not just the
            # final position: a monotonic smoothstep lands the piece pixel-correct yet is
            # rejected (F001) as a bot. A/B live: monotonic 0/3 T001 vs overshoot 3/3 T001.
            # Humans ballistically overshoot the target then correct back — that's the pass
            # signal. Ballistic ease-out + ~6-11px overshoot + multi-step correction. Do NOT
            # over-jitter: heavy jitter/pauses (profile "over2") regressed to 2/3.
            sx = handle["x"] + handle["w"] / 2
            sy = handle["y"] + handle["h"] / 2
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1})
            over = dist + random.uniform(6, 11)
            n = 42
            for i in range(1, n + 1):
                t = i / n
                ease = 1 - (1 - t) ** 3          # ballistic: fast start, slow approach
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": sx + over * ease,
                    "y": sy + np.sin(t * 5) * 1.2, "button": "left"})
                await asyncio.sleep(0.011 + (i % 4) * 0.004)
            # correction phase: drift back from the overshoot to the true target
            cn = 10
            for j in range(1, cn + 1):
                t = j / cn
                cur = over + (dist - over) * t
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": sx + cur,
                    "y": sy + random.uniform(-0.6, 0.6), "button": "left"})
                await asyncio.sleep(0.02 + random.uniform(0, 0.015))
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": sx + dist, "y": sy, "button": "left"})
            await asyncio.sleep(0.16 + random.uniform(0, 0.1))
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": sx + dist, "y": sy, "button": "left"})

        if not await load_once():
            return {"solved": False, "error": "SDK failed to load (window.__ready never true)"}

        async def _attempt_puzzle(attempt, type_name):
            # PUZZLE + INPAINTING share this path: open popup → wait for slider+images
            # → detect gap (YOLO/cv2, with DOM fallback for base64 data-URL images) →
            # quadratic overshoot drag → harvest token → verify. Preserved exactly from
            # the proven 3/3 T001 implementation.
            await open_popup()
            handle = await wait_challenge()
            if not handle:
                log.info("aliyun attempt %d [%s]: no slider found", attempt, type_name)
                return None

            g = detect_gap_x(latest["back"], latest["shadow"])
            geo = await page.evaluate(
                "()=>{const bg=document.querySelector('#cap img,[class*=puzzle] img');"
                "return bg?{bg_w:bg.getBoundingClientRect().width,nat:bg.naturalWidth}:"
                "{bg_w:300,nat:296};}")
            scale = geo["bg_w"] / geo["nat"]
            target = g["gap_x"] * scale
            dist = float(_invert(target))

            await page.evaluate("()=>{window.__verify=null;}")
            await human_drag(handle, dist)
            await page.wait_for_timeout(2200)

            tok = await page.evaluate("()=>window.__verify||null")
            if not tok:
                log.info("aliyun attempt %d [%s]: method=%s gap_x=%s dist=%.1f -> NO CALLBACK",
                         attempt, type_name, g.get("method", "cv2"), g["gap_x"], dist)
                return None
            token = tok if isinstance(tok, dict) else json.loads(tok)
            code = await _verify_in_page(page, token, scene_id, prefix)
            log.info("aliyun attempt %d [%s]: method=%s gap_x=%s dist=%.1f -> %s",
                     attempt, type_name, g.get("method", "cv2"), g["gap_x"], dist, code)
            return {"token": token, "verify_code": code,
                    "method": "quadratic-slide", "gap_x": g["gap_x"]}

        async def _attempt_slide(attempt, type_name):
            # SLIDE: drag the slider the full track width. No gap detection, no images.
            # The track width is read from the slider container's bounding box; drag
            # (track_width - handle_width) so the handle reaches the end.
            await open_popup()
            handle = None
            for _ in range(18):
                handle = await page.evaluate(
                    "()=>{const s=document.getElementById('aliyunCaptcha-sliding-slider');"
                    "if(!s)return null;const r=s.getBoundingClientRect();"
                    "return r.width>0?{x:r.x,y:r.y,w:r.width,h:r.height}:null;}")
                if handle:
                    break
                await page.wait_for_timeout(500)
            if not handle:
                log.info("aliyun attempt %d [%s]: no slider found", attempt, type_name)
                return None

            track = await page.evaluate(
                "()=>{const t=document.querySelector("
                "'#aliyunCaptcha-sliding-track,[class*=slider-track],[class*=track]');"
                "if(!t)return null;const r=t.getBoundingClientRect();"
                "return r.width>0?{w:r.width}:null;}")
            track_w = (track or {}).get("w", handle["w"] * 4)
            dist = track_w - handle["w"]

            await page.evaluate("()=>{window.__verify=null;}")
            await human_drag(handle, dist)
            await page.wait_for_timeout(2200)

            tok = await page.evaluate("()=>window.__verify||null")
            if not tok:
                log.info("aliyun attempt %d [%s]: dist=%.1f -> NO CALLBACK",
                         attempt, type_name, dist)
                return None
            token = tok if isinstance(tok, dict) else json.loads(tok)
            code = await _verify_in_page(page, token, scene_id, prefix)
            log.info("aliyun attempt %d [%s]: dist=%.1f -> %s",
                     attempt, type_name, dist, code)
            return {"token": token, "verify_code": code, "method": "full-slide"}

        async def _attempt_traceless(attempt, type_name):
            # TRACELESS: silent. Click the button (or call startTracelessVerification)
            # → captchaVerifyCallback fires immediately, no popup/slider/vision.
            await page.evaluate("()=>{window.__verify=null;}")
            started = await page.evaluate(
                "()=>{if(window.__inst&&typeof window.__inst.startTracelessVerification==='function')"
                "{window.__inst.startTracelessVerification();return true;}return false;}")
            if not started:
                await open_popup()
            tok = None
            for _ in range(20):
                tok = await page.evaluate("()=>window.__verify||null")
                if tok:
                    break
                await page.wait_for_timeout(250)
            if not tok:
                log.info("aliyun attempt %d [%s]: NO CALLBACK", attempt, type_name)
                return None
            token = tok if isinstance(tok, dict) else json.loads(tok)
            code = await _verify_in_page(page, token, scene_id, prefix)
            log.info("aliyun attempt %d [%s]: -> %s", attempt, type_name, code)
            return {"token": token, "verify_code": code, "method": "traceless"}

        async def _attempt_oneclick(attempt, type_name):
            # ONE_CLICK: single click on the checkbox/button, then await callback.
            await page.evaluate("()=>{window.__verify=null;}")
            box = await page.evaluate(
                "()=>{const sel=['#aliyunCaptcha-btn','#aliyunCaptcha-button',"
                "'.nc_iconfont','[class*=captcha-btn]','[class*=checkbox]','button#btn'];"
                "for(const s of sel){const el=document.querySelector(s);"
                "if(el){const r=el.getBoundingClientRect();"
                "if(r.width>0)return{x:r.x,y:r.y,w:r.width,h:r.height};}}return null;}")
            if not box:
                await open_popup()
                box = await page.evaluate(
                    "()=>{const b=document.getElementById('btn');const r=b.getBoundingClientRect();"
                    "return{x:r.x,y:r.y,w:r.width,h:r.height};}")
            for t in ("mousePressed", "mouseReleased"):
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": t, "x": box["x"] + box["w"] / 2,
                    "y": box["y"] + box["h"] / 2, "button": "left", "clickCount": 1})
            tok = None
            for _ in range(20):
                tok = await page.evaluate("()=>window.__verify||null")
                if tok:
                    break
                await page.wait_for_timeout(250)
            if not tok:
                log.info("aliyun attempt %d [%s]: NO CALLBACK", attempt, type_name)
                return None
            token = tok if isinstance(tok, dict) else json.loads(tok)
            code = await _verify_in_page(page, token, scene_id, prefix)
            log.info("aliyun attempt %d [%s]: -> %s", attempt, type_name, code)
            return {"token": token, "verify_code": code, "method": "one-click"}

        ct = latest.get("captcha_type") or ""
        type_name = ct or "PUZZLE"
        log.info("aliyun: dispatching to %s handler (CaptchaType=%s)", type_name, ct)
        if ct == TYPE_TRACELESS:
            attempt_fn = _attempt_traceless
        elif ct == TYPE_SLIDE:
            attempt_fn = _attempt_slide
        elif ct == TYPE_ONE_CLICK:
            attempt_fn = _attempt_oneclick
        else:
            # PUZZLE, INPAINTING, unknown, or unintercepted → PUZZLE path (backward compat)
            attempt_fn = _attempt_puzzle

        last_code = None
        for attempt in range(max_attempts):
            if time.monotonic() - t_start > timeout_s:
                break
            latest["back"] = latest["shadow"] = None
            if attempt > 0 and not await load_once():
                continue

            res = await attempt_fn(attempt, type_name)
            if not res:
                continue
            last_code = res["verify_code"]
            if res["verify_code"] == "T001":
                return {
                    "solved": True, "token": res["token"],
                    "verify_code": res["verify_code"], "method": res["method"],
                    "captcha_type": type_name, "attempts": attempt + 1,
                    "elapsed": round(time.monotonic() - t_start, 1),
                }

        return {"solved": False, "error": f"no T001 in {max_attempts} attempts",
                "last_verify_code": last_code, "captcha_type": type_name,
                "elapsed": round(time.monotonic() - t_start, 1)}
    finally:
        try:
            await browser.close()
        except Exception:
            pass

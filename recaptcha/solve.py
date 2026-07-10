"""Solve reCAPTCHA — v3 (score), invisible, and v2 checkbox — locally via CloakBrowser.

Three mechanisms, picked by version:

  v3 / invisible : grecaptcha.execute(sitekey,{action}) on a route-intercepted page.
                   NO checkbox, NO audio, NO challenge — execute() always returns a
                   token; Google scores it server-side (fingerprint+IP). Verified to
                   score 0.9 (human-level) headed-under-Xvfb. This is the easy path.

  v2 checkbox    : click the checkbox inside the cross-origin /anchor iframe. On a
                   LOW-risk session it goes straight to checked and yields a token with
                   no challenge (same mechanism the Turnstile solver exploits). If a
                   challenge popup opens, fall back to the audio challenge + Whisper —
                   but that path is frequently IP-blocked ("automated queries") and is
                   best-effort only; gate it behind a clean residential proxy.

Run headed under Xvfb (RECAPTCHA_HEADLESS=0, the default here) — headless is the most
heavily penalised surface.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch, route_glob
from common.mistral import KeyPool
from .image_solve import solve_image_challenge

log = logging.getLogger(__name__)

_KEYFILE = Path(__file__).parent.parent / "common" / "apikey.txt"
_keypool = None


def _get_keypool():
    """Lazy, shared Mistral key pool (only built if an image challenge appears)."""
    global _keypool
    if _keypool is None:
        model = os.getenv("RECAPTCHA_MISTRAL_MODEL", "mistral-medium-latest")
        # vary the start offset by pid so concurrent procs don't hammer key #0
        _keypool = KeyPool(str(_KEYFILE), model=model, start_index=os.getpid())
    return _keypool

_TEMPLATE_PATH = Path(__file__).parent / "template.html"
_HTML_TEMPLATE = _TEMPLATE_PATH.read_text()

_solve_lock = asyncio.Lock()

# Address the iframes by title so frame_locator re-resolves them on every action —
# immune to reCAPTCHA reloading the iframe.
_ANCHOR_IFRAME = "iframe[title='reCAPTCHA']"
_BFRAME_IFRAME = "iframe[title*='recaptcha challenge']"

# v3 / invisible / Enterprise page: load the render lib then execute(). Built inline
# (structure differs from the v2 template). Enterprise just swaps in enterprise.js and
# the grecaptcha.enterprise namespace.
_V3_PAGE = """<!DOCTYPE html><html><head>
<script src="https://www.google.com/recaptcha/__LIB__?render=__SITEKEY__"></script>
</head><body><div id="out">waiting</div>
<script>
  window.__token = ""; window.__err = "";
  var gre = __NS__;
  gre.ready(function () {
    gre.execute("__SITEKEY__", {action: "__ACTION__"})
      .then(function (t) { window.__token = t; })
      .catch(function (e) { window.__err = String(e); });
  });
</script></body></html>"""


def _browser_kwargs() -> dict:
    return browser_kwargs("RECAPTCHA")


def _build_v2_page(sitekey: str, enterprise: bool = False) -> str:
    # enterprise.js auto-renders the same .g-recaptcha checkbox as api.js.
    lib = "enterprise.js" if enterprise else "api.js"
    return _HTML_TEMPLATE.replace("__SITEKEY__", sitekey).replace("__LIB__", lib)


async def _find_frame(page, pattern: str):
    for fr in page.frames:
        if pattern in (fr.url or ""):
            return fr
    return None


# ── v3 / invisible: execute() ───────────────────────────────────────

async def _solve_via_execute(sitekey: str, url: str, action: str,
                             enterprise: bool = False) -> dict:
    """Mint a token via grecaptcha[.enterprise].execute() on a route-intercepted page."""
    t0 = time.monotonic()
    lib = "enterprise.js" if enterprise else "api.js"
    ns = "grecaptcha.enterprise" if enterprise else "grecaptcha"
    body = (_V3_PAGE.replace("__LIB__", lib).replace("__NS__", ns)
            .replace("__SITEKEY__", sitekey).replace("__ACTION__", action))
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(url), lambda r: r.fulfill(body=body, status=200))
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for _ in range(20):
                    await asyncio.sleep(1)
                    token = await page.evaluate("() => window.__token || ''")
                    if token:
                        return {"token": token, "action": action,
                                "elapsed": round(time.monotonic() - t0, 1),
                                "method": "enterprise" if enterprise else "execute"}
                    err = await page.evaluate("() => window.__err || ''")
                    if err:
                        return {"error": f"execute() failed: {err}",
                                "elapsed": round(time.monotonic() - t0, 1)}
                return {"error": "execute() timed out (no token)",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


def _siteverify(token: str, secret: str) -> dict:
    """Read the v3 score via Google siteverify. Needs the TARGET's secret key
    (only the site owner has it) — that's the only way a score exists."""
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"secret": secret, "response": token}).encode()
    req = urllib.request.Request(
        "https://www.google.com/recaptcha/api/siteverify", data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        import json as _json
        return _json.loads(r.read())


async def solve_recaptcha_v3(sitekey: str, url: str, action: str = "submit",
                             secret: str = None, enterprise: bool = False) -> dict:
    """Solve reCAPTCHA v3 (score-based). Returns {token, action, elapsed}.

    The token's *score* is decided server-side by Google. Pass `secret` (the
    target site's secret key) to also run siteverify and return the score —
    without a secret no score exists, only a token. `enterprise=True` loads
    enterprise.js + grecaptcha.enterprise (score read via Cloud Assessment API,
    not the public siteverify endpoint, so `secret` is ignored for Enterprise).
    """
    res = await _solve_via_execute(sitekey, url, action, enterprise=enterprise)
    if secret and not enterprise and res.get("token"):
        try:
            v = await asyncio.to_thread(_siteverify, res["token"], secret)
            res["score"] = v.get("score")
            res["verify"] = v
        except Exception as e:
            res["verify_error"] = str(e)
    return res


async def solve_recaptcha_invisible(sitekey: str, url: str, action: str = "submit",
                                    enterprise: bool = False) -> dict:
    """Solve invisible reCAPTCHA v2. Identical mechanism to v3 (execute())."""
    return await _solve_via_execute(sitekey, url, action, enterprise=enterprise)


# ── v3 / invisible on the REAL page (higher score) ──────────────────

# Load the render lib on the REAL page, wait for the namespace, then execute — passing
# sitekey/action/lib/ns as evaluate() args (never interpolated into JS source).
_V3_REALPAGE_JS = """
({sitekey, action, lib, ns}) => {
  window.__rc_token = ""; window.__rc_err = "";
  const getGre = () => ns === 'enterprise'
    ? (window.grecaptcha && window.grecaptcha.enterprise)
    : window.grecaptcha;
  const run = () => {
    const gre = getGre();
    gre.ready(() => {
      gre.execute(sitekey, {action})
        .then(t => { window.__rc_token = t; })
        .catch(e => { window.__rc_err = String(e); });
    });
  };
  const s = document.createElement('script');
  s.src = `https://www.google.com/recaptcha/${lib}?render=${sitekey}`;
  s.onerror = () => { window.__rc_err = 'lib load failed'; };
  s.onload = () => {
    let tries = 0;
    const iv = setInterval(() => {
      const gre = getGre();
      if (gre && gre.execute) { clearInterval(iv); run(); }
      else if (++tries > 100) { clearInterval(iv); window.__rc_err = 'grecaptcha not ready'; }
    }, 100);
  };
  document.head.appendChild(s);
}
"""


async def _simulate_behavior(page):
    """Light behavioral signal (mouse path + scroll + dwell) so execute() runs with
    real interaction history — the score input the route-intercept fake page lacks."""
    try:
        for x, y in [(150, 200), (420, 360), (640, 260), (320, 520), (500, 300)]:
            await page.mouse.move(x, y, steps=10)
            await asyncio.sleep(0.35)
        await page.mouse.wheel(0, 450)
        await asyncio.sleep(0.6)
        await page.mouse.wheel(0, -220)
        await asyncio.sleep(0.5)
    except Exception:
        pass


async def solve_recaptcha_v3_realpage(url: str, sitekey: str, action: str = "submit",
                                      enterprise: bool = False, timeout_s: int = 90,
                                      pre_actions: list = None) -> dict:
    """Mint a v3/Enterprise token on the REAL page (no route intercept).

    Navigates the genuine URL (real DOM, real cookies, real origin), runs optional
    pre_actions, simulates brief interaction, then loads the render lib and calls
    grecaptcha[.enterprise].execute() in that context. The behavioral + DOM signals a
    route-intercepted blank page can't provide are the main score lever — use this for
    strict sitekeys where the fast execute() path scores too low. Returns {token, ...}.
    """
    t0 = time.monotonic()
    lib = "enterprise.js" if enterprise else "api.js"
    ns = "enterprise" if enterprise else "standard"
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)
                await asyncio.sleep(2)
                await _simulate_behavior(page)

                await page.evaluate(_V3_REALPAGE_JS,
                                    {"sitekey": sitekey, "action": action, "lib": lib, "ns": ns})
                method = "enterprise-realpage" if enterprise else "execute-realpage"
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    token = await page.evaluate("() => window.__rc_token || ''")
                    if token:
                        return {"token": token, "action": action,
                                "elapsed": round(time.monotonic() - t0, 1), "method": method}
                    err = await page.evaluate("() => window.__rc_err || ''")
                    if err:
                        return {"error": f"execute() failed: {err}",
                                "elapsed": round(time.monotonic() - t0, 1)}
                return {"error": "execute() timed out (no token)",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


# ── v2 checkbox: click + (best-effort) audio fallback ───────────────

async def _anchor_class(page) -> str:
    try:
        return await page.frame_locator(_ANCHOR_IFRAME).locator(
            "#recaptcha-anchor").get_attribute("class", timeout=3000) or ""
    except Exception:
        return ""


async def _get_token(page) -> str:
    for _ in range(10):
        token = await page.evaluate(
            "() => document.querySelector('#g-recaptcha-response')?.value || ''")
        if token:
            return token
        await asyncio.sleep(1)
    return ""


async def _audio_blocked(page) -> bool:
    """True if reCAPTCHA is denying the audio fallback (IP/automation block)."""
    fr = await _find_frame(page, "/bframe")
    if not fr:
        return False
    try:
        txt = (await fr.locator("body").inner_text(timeout=2000)).lower()
    except Exception:
        return False
    return any(s in txt for s in ("automated queries", "try again later",
                                  "sending automated"))


async def solve_recaptcha_v2(sitekey: str, url: str,
                              max_attempts: int = 3,
                              enterprise: bool = False) -> dict:
    """Solve reCAPTCHA v2 checkbox via route intercept.

    Clicks the checkbox; returns the token immediately if the session is low-risk
    (no challenge). If an image grid opens, solves it via Mistral vision. Set
    `enterprise=True` for Enterprise checkbox keys (loads enterprise.js — the widget
    and challenge are otherwise identical). Returns {token, attempts, elapsed} or
    {error, elapsed}.
    """
    t0 = time.monotonic()
    page_data = _build_v2_page(sitekey, enterprise=enterprise)

    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(url), lambda r: r.fulfill(body=page_data, status=200))
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                for attempt in range(1, max_attempts + 1):
                    log.info("v2 attempt %d/%d", attempt, max_attempts)
                    try:
                        await page.frame_locator(_ANCHOR_IFRAME).locator(
                            "#recaptcha-anchor").click(timeout=8000)
                    except Exception as e:
                        log.warning("checkbox click: %s", str(e).splitlines()[0])

                    # Poll: checked (no-challenge win) OR an image grid opens.
                    challenge = False
                    for _ in range(15):
                        await asyncio.sleep(1)
                        if "recaptcha-checkbox-checked" in await _anchor_class(page):
                            token = await _get_token(page)
                            if token:
                                return {"token": token, "attempts": attempt,
                                        "elapsed": round(time.monotonic() - t0, 1),
                                        "method": "checkbox-no-challenge"}
                        if await _find_frame(page, "/bframe") and \
                                await page.frame_locator(_BFRAME_IFRAME).locator(
                                    "table").count() > 0:
                            challenge = True
                            break

                    # Image-solve (audio is IP-blocked). Vision via Mistral key pool.
                    if challenge:
                        try:
                            await solve_image_challenge(page, _get_keypool())
                        except Exception as e:
                            log.warning("image-solve: %s", str(e).splitlines()[0])
                        token = await _get_token(page)
                        if token:
                            return {"token": token, "attempts": attempt,
                                    "elapsed": round(time.monotonic() - t0, 1),
                                    "method": "image"}
                    if attempt < max_attempts:
                        await asyncio.sleep(3 * attempt)  # backoff between attempts

                return {"error": f"failed after {max_attempts} attempts",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


async def solve_recaptcha_v2_realpage(url: str, sitekey: str = None,
                                      pre_actions: list = None,
                                      post_fetch: list = None,
                                      timeout_s: int = 60) -> dict:
    """Solve v2 on the REAL page (no route intercept) — the realistic production path.

    Navigates the actual site, runs optional pre_actions, clicks the checkbox in the
    cross-origin iframe, harvests the token, and optionally runs post_fetch API calls
    from the SAME browser session. Mirrors turnstile.solve_turnstile_realpage.

    Use __TOKEN__ in post_fetch bodies to inject the solved token.
    """
    t0 = time.monotonic()
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)
                    await asyncio.sleep(2)
                if sitekey:  # inject our widget if the page doesn't embed one
                    await page.evaluate(
                        "(k) => { const d=document.createElement('div');"
                        " d.className='g-recaptcha'; d.setAttribute('data-sitekey',k);"
                        " document.body.prepend(d);"
                        " const s=document.createElement('script');"
                        " s.src='https://www.google.com/recaptcha/api.js';"
                        " document.head.appendChild(s); }", sitekey)
                    await asyncio.sleep(3)

                try:
                    await page.frame_locator(_ANCHOR_IFRAME).locator(
                        "#recaptcha-anchor").click(timeout=8000)
                except Exception as e:
                    log.warning("realpage checkbox click: %s", str(e).splitlines()[0])

                token = ""
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    token = await page.evaluate(
                        "() => document.querySelector('#g-recaptcha-response')?.value || ''")
                    if token:
                        break
                    if await _audio_blocked(page):
                        break

                cookies = await page.context.cookies()
                result = {"token": token, "verify_success": bool(token),
                          "cookies": cookies, "method": "real-page",
                          "elapsed": round(time.monotonic() - t0, 1)}
                if not token and await _audio_blocked(page):
                    result["error"] = "audio-blocked"

                if post_fetch and token:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, token)
                return result
            finally:
                await page.close()

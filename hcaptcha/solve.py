"""Solve hCaptcha locally via CloakBrowser — checkbox, invisible, and real-page.

Three modes:

  1. Checkbox (route-intercept): render .h-captcha widget on an intercepted page,
     auto-click the checkbox inside the cross-origin iframe, harvest the token from
     [name=h-captcha-response]. If a challenge triggers (image grid), solve it via
     Mistral vision (same keypool pattern as the reCAPTCHA solver).

  2. Invisible (execute): hcaptcha.execute() programmatic execution via explicit
     rendering + size=invisible. Zero interaction — works for sitekeys configured
     as invisible / passive.

  3. Real page: navigate the actual site, run pre_actions, solve, optionally
     run post_fetch API calls from the same browser session.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

import cloakbrowser

from common.mistral import KeyPool
from common.browser import browser_kwargs, run_pre_actions, run_post_fetch, route_glob

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()
_TEMPLATE_PATH = Path(__file__).parent / "template.html"
HTML_TEMPLATE = _TEMPLATE_PATH.read_text()

_KEYFILE = Path(__file__).parent.parent / "common" / "apikey.txt"
_keypool = None


def _get_keypool():
    """Lazy, shared KeyPool for hCaptcha image solving."""
    global _keypool
    if _keypool is None:
        model = os.getenv("HCAPTCHA_MISTRAL_MODEL", "mistral-medium-latest")
        _keypool = KeyPool(str(_KEYFILE), model=model, start_index=os.getpid())
    return _keypool


# Invisible (execute) page: explicit render + hcaptcha.execute().
# API-compatible with reCAPTCHA — same pattern, different namespace.
_INVISIBLE_PAGE = """<!DOCTYPE html><html><head>
<script src="https://js.hcaptcha.com/1/api.js?onload=onloadHCaptcha&render=explicit" async defer></script>
</head><body>
<script>
  window.__token = ""; window.__err = "";
  function onloadHCaptcha() {
    try {
      var wid = hcaptcha.render('hcaptcha-container', {
        sitekey: '__SITEKEY__',
        size: 'invisible',
        callback: function(t) { window.__token = t; },
        'error-callback': function(e) { window.__err = String(e); },
        'expired-callback': function() { window.__err = 'expired'; },
      });
      setTimeout(function() { hcaptcha.execute(wid); }, 300);
    } catch(e) { window.__err = String(e); }
  }
</script>
<div id="hcaptcha-container"></div>
</body></html>"""


def _browser_kwargs() -> dict:
    return browser_kwargs("HCAPTCHA")


# ── Token harvest ─────────────────────────────────────────────────────

async def _get_hcaptcha_token(page, max_attempts: int = 30) -> str:
    """Poll [name=h-captcha-response] + hcaptcha.getResponse() for token."""
    for _ in range(max_attempts):
        # Check DOM first
        try:
            token = await page.evaluate(
                "() => (document.querySelector('[name=h-captcha-response]') || {}).value || ''"
            )
            if token:
                return token
        except Exception:
            pass
        # Check via hcaptcha.getResponse()
        try:
            token = await page.evaluate(
                "() => { try { return hcaptcha.getResponse() || ''; } catch(e) { return ''; } }"
            )
            if token:
                return token
        except Exception:
            pass
        await asyncio.sleep(1)
    return ""


async def _click_hcaptcha_checkbox(page, attempts: int = 20) -> bool:
    """Click the checkbox inside the cross-origin hCaptcha iframe."""
    for _ in range(attempts):
        for fr in page.frames:
            if "#frame=checkbox" in (fr.url or ""):
                for sel in ("#checkbox", "div[role=checkbox]", "label", "body"):
                    try:
                        await fr.click(sel, timeout=2000)
                        return True
                    except Exception:
                        continue
        await asyncio.sleep(1)
    return False


def _has_challenge(page) -> bool:
    """Check if a challenge frame with image grid is present."""
    for fr in page.frames:
        u = fr.url or ""
        # hCaptcha uses URL fragment: #frame=challenge&...
        if "#frame=challenge" in u and "hcaptcha" in u:
            return True
    return False


async def _close_challenge(page) -> bool:
    """Dismiss any open challenge by clicking Skip/close."""
    for fr in page.frames:
        u = fr.url or ""
        if "#frame=challenge" in u and "hcaptcha" in u:
            try:
                btn = await fr.query_selector(".button-submit")
                if btn:
                    text = await btn.inner_text()
                    if text.lower() in ("skip", "跳过", "huppel", "ohita", "überspringen"):
                        await btn.click(timeout=3000)
                        await asyncio.sleep(2)
                        return True
                close = await fr.query_selector(".close.button")
                if close:
                    await close.click(timeout=3000)
                    await asyncio.sleep(1)
                    return True
            except Exception:
                pass
    return False


async def _solve_challenge(page) -> bool:
    """Solve the hCaptcha image challenge via Mistral vision."""
    from .image_solve import solve_hcaptcha_challenge
    for fr in page.frames:
        u = fr.url or ""
        if "#frame=challenge" in u and "hcaptcha" in u:
            return await solve_hcaptcha_challenge(fr, page, _get_keypool())
    return False


# ── Main solver: checkbox (route-intercept) ───────────────────────────

async def solve_hcaptcha(sitekey: str, url: str, max_attempts: int = 3) -> dict:
    """Solve hCaptcha checkbox via route interception.

    Renders the widget on an intercepted page, auto-clicks the checkbox
    inside the iframe, and harvests the token. If a challenge image grid
    appears, solves it via Mistral vision.

    Returns {token, expires_in, elapsed, method} or {error, elapsed}.
    """
    t0 = time.monotonic()
    async with _solve_lock:
        div = f'<div class="h-captcha" data-sitekey="{sitekey}"></div>'
        page_data = HTML_TEMPLATE.replace("<!-- hcaptcha widget -->", div)

        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(url), lambda r: r.fulfill(body=page_data, status=200))
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                for attempt in range(1, max_attempts + 1):
                    log.info("hCaptcha attempt %d/%d", attempt, max_attempts)

                    # Try auto-populate (passive/no-challenge config)
                    token = await _get_hcaptcha_token(page, max_attempts=5)
                    if token:
                        return {"token": token, "expires_in": 120,
                                "elapsed": round(time.monotonic() - t0, 1),
                                "method": "route-no-click"}

                    # Close any leftover challenge from previous attempt
                    await _close_challenge(page)

                    clicked = await _click_hcaptcha_checkbox(page)
                    log.info("hCaptcha checkbox clicked=%s", clicked)
                    await asyncio.sleep(3)

                    if _has_challenge(page):
                        log.info("hCaptcha challenge detected — solving via vision")
                        solved = await _solve_challenge(page)
                        log.info("hCaptcha challenge solve=%s", solved)
                        if not solved:
                            await _close_challenge(page)

                    token = await _get_hcaptcha_token(page, max_attempts=15)
                    if token:
                        return {"token": token, "expires_in": 120,
                                "elapsed": round(time.monotonic() - t0, 1),
                                "method": "route"}

                    await asyncio.sleep(3 * attempt)  # backoff

                return {"error": "No token obtained",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


# ── Invisible (execute) ────────────────────────────────────────────────

async def solve_hcaptcha_invisible(sitekey: str, url: str) -> dict:
    """Solve hCaptcha invisible via programmatic hcaptcha.execute().

    Zero interaction — the token arrives without any challenge. Works for
    sitekeys configured as invisible/passive.

    Returns {token, elapsed, method} or {error, elapsed}.
    """
    t0 = time.monotonic()
    body = _INVISIBLE_PAGE.replace("__SITEKEY__", sitekey)
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs()) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(url), lambda r: r.fulfill(body=body, status=200))
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for _ in range(25):
                    await asyncio.sleep(1)
                    token = await page.evaluate("() => window.__token || ''")
                    if token:
                        return {"token": token,
                                "elapsed": round(time.monotonic() - t0, 1),
                                "method": "invisible"}
                    err = await page.evaluate("() => window.__err || ''")
                    if err:
                        return {"error": f"execute() failed: {err}",
                                "elapsed": round(time.monotonic() - t0, 1)}
                return {"error": "execute() timed out",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


# ── Real page solver ──────────────────────────────────────────────────

# Sitekey passed as evaluate() arg `k` (injection-safe). The api.js append is guarded
# (script[src*='js.hcaptcha.com']) so it's added only when absent.
_WIDGET_INJECT_JS = (
    "(k) => {"
    "  const d = document.createElement('div');"
    "  d.className = 'h-captcha';"
    "  d.setAttribute('data-sitekey', k);"
    "  document.body.prepend(d);"
    "  if (!document.querySelector(\"script[src*='js.hcaptcha.com']\")) {"
    "    const s = document.createElement('script');"
    "    s.src = 'https://js.hcaptcha.com/1/api.js';"
    "    s.async = true; s.defer = true;"
    "    document.head.appendChild(s);"
    "  }"
    "}"
)


async def _inject_hcaptcha_widget(page, sitekey: str) -> None:
    """Inject a .h-captcha widget (+ api.js if absent), sitekey passed as data."""
    await page.evaluate(_WIDGET_INJECT_JS, sitekey)


async def solve_hcaptcha_realpage(url: str, sitekey: str = None,
                                   timeout_s: int = 60,
                                   pre_actions: list = None,
                                   post_fetch: list = None) -> dict:
    """Navigate a real page, execute pre_actions, click hCaptcha checkbox,
    return token + cookies + optional post_fetch results.

    pre_actions — steps before hCaptcha appears:
      [{"type": "click", "selector": "text=Continue"},
       {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"}]

    post_fetch — API calls from same browser (__TOKEN__ placeholder):
      [{"url": "https://api.example.com/verify", "body": {"token": "__TOKEN__"}}]

    Use __TOKEN__ placeholder in post_fetch bodies to inject the solved token.
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

                # Inject our own widget if the page doesn't have one.
                # No data-theme — a hard-coded theme is a fixed real-page fingerprint.
                if sitekey:
                    await _inject_hcaptcha_widget(page, sitekey)
                    await asyncio.sleep(3)

                clicked = await _click_hcaptcha_checkbox(page)
                log.info("Real-page checkbox clicked=%s", clicked)
                await asyncio.sleep(2)

                if _has_challenge(page):
                    log.info("Real-page: solving image challenge")
                    await _solve_challenge(page)

                # Harvest token
                token = ""
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    try:
                        token = await page.evaluate(
                            "() => (document.querySelector('[name=h-captcha-response]') || {}).value || ''"
                        )
                        if token:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                cookies = await page.context.cookies()
                result = {"token": token, "verify_success": bool(token),
                          "cookies": cookies, "method": "real-page",
                          "elapsed": round(time.monotonic() - t0, 1)}

                # Post-fetch from the same session (parameterized — injection-safe).
                if post_fetch and token:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, token)

                return result
            finally:
                await page.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "checkbox"
    sk = sys.argv[2] if len(sys.argv) > 2 else "345e6d03-eb0c-4911-a63c-05a819bfdc09"
    if mode == "invisible":
        res = asyncio.run(solve_hcaptcha_invisible(sk, "https://7y7j.github.io/"))
    elif mode == "realpage":
        res = asyncio.run(solve_hcaptcha_realpage("https://7y7j.github.io/", sk))
    else:
        res = asyncio.run(solve_hcaptcha(sk, "https://7y7j.github.io/"))
    print(res)

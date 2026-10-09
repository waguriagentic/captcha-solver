"""Solve + verify Cloudflare Turnstile locally via CloakBrowser.

Route-intercept → solve the Turnstile widget on a fake page served at the
target origin, then verify the token from the same browser session (keeps the
origin/cookies and stays inside the token's 300s single-use window).

The stub uses the interactive widget (`size: 'normal'`) rendered EXPLICITLY via
`turnstile.render()` in the api.js `onload` callback, and the token is captured
by that callback into a hidden input. An invisible/implicit widget yields a
token the target's anti-abuse backend rejects even though siteverify passes, so
keep the interactive render shape.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch, fetch_from_page, route_glob
from common.concurrency import solve_slot

log = logging.getLogger(__name__)
# Concurrency is bounded by common.concurrency.solve_slot() — the previous
# per-solver asyncio.Lock serialized every solve.
_TEMPLATE_PATH = Path(__file__).parent / "template.html"
HTML_TEMPLATE = _TEMPLATE_PATH.read_text()


def _browser_kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy=proxy)


def _error_codes(body: str) -> list:
    """Pull Cloudflare siteverify error-codes out of a verify response."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    return data.get("error-codes") or data.get("details") or []


def _js_string(value: str) -> str:
    """Escape a value for safe single-quoted JS embedding."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _stub_html(sitekey: str, action: str = None, cdata: str = None) -> str:
    """Fill the stub template with the widget options."""
    extra = ""
    if action:
        extra += f"\n    action: '{_js_string(action)}',"
    if cdata:
        extra += f"\n    cdata: '{_js_string(cdata)}',"
    return (HTML_TEMPLATE
            .replace("__SITEKEY__", _js_string(sitekey))
            .replace("__EXTRA__", extra))


# ── Route-intercept (fast, generic) ─────────────────────────────────

async def _click_turnstile_boxes(page) -> int:
    """Click every ~300px Turnstile box on the stub page.

    The widget lives in a cross-origin iframe, so the clickable surface is the
    empty 290–310px div the iframe fills. Prefer divs with zero margin/padding
    (the exact widget shell), fall back to any empty 290–310px div.
    """
    boxes = await page.evaluate(
        """() => {
            const out = [];
            const collect = (strict) => {
                document.querySelectorAll('div').forEach(item => {
                    try {
                        const r = item.getBoundingClientRect();
                        if (r.width > 290 && r.width <= 310 && !item.querySelector('*')) {
                            if (strict) {
                                const css = window.getComputedStyle(item);
                                if (css.margin !== '0px' || css.padding !== '0px') return;
                            }
                            out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                        }
                    } catch (e) {}
                });
            };
            collect(true);
            if (!out.length) collect(false);
            return out;
        }"""
    )
    for b in boxes:
        try:
            await page.mouse.click(b["x"] + 30, b["y"] + b["h"] / 2)
        except Exception:
            pass
    return len(boxes)


async def _get_turnstile_response_route(page, max_attempts: int = 40) -> str:
    """Retrieve the token from the route-intercepted stub page.

    The callback-captured hidden input is `[name=cf-response]`; the widget's own
    textarea (`[name=cf-turnstile-response]`) is checked as a fallback. Click
    loop keeps poking the widget until a token lands.
    """
    for _ in range(max_attempts):
        try:
            tok = await page.evaluate(
                "() => { const a = document.querySelector('[name=cf-response]');"
                " if (a && a.value && a.value.length > 10) return a.value;"
                " const b = document.querySelector('[name=cf-turnstile-response]');"
                " return (b && b.value) || ''; }"
            )
            if tok:
                return tok
            await _click_turnstile_boxes(page)
        except Exception:
            pass
        await asyncio.sleep(1)
    raise TimeoutError("Token not received via route-intercept")


async def solve_turnstile(sitekey: str, url: str, action: str = None,
                          cdata: str = None, proxy: str = None) -> dict:
    """Solve Turnstile via route interception. Returns {token, expires_in}."""
    t0 = time.monotonic()
    async with solve_slot():
        target = url
        page_data = _stub_html(sitekey, action, cdata)

        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(target), lambda r: r.fulfill(body=page_data,
                                                             status=200))
                # CF posts solved-challenge telemetry to this endpoint; aborting it
                # keeps the session from reporting back on the stub origin.
                await page.route("**/reports/v0/post**", lambda r: r.abort())
                await page.goto(target, wait_until="domcontentloaded")
                token = await _get_turnstile_response_route(page)
                return {"token": token, "expires_in": 300,
                        "elapsed": round(time.monotonic() - t0, 1),
                        "method": "route"}
            finally:
                await page.close()


# ── solve_and_verify ────────────────────────────────────────────────

async def solve_and_verify(sitekey: str, verify_url: str,
                           verify_payload: dict = None,
                           action: str = None, cdata: str = None,
                           page_url: str = None, proxy: str = None) -> dict:
    """Solve via route-intercept, then verify from the same browser session."""
    t0 = time.monotonic()
    async with solve_slot():
        target = page_url or verify_url
        page_data = _stub_html(sitekey, action, cdata)

        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(target), lambda r: r.fulfill(
                    body=page_data, status=200))
                await page.route("**/reports/v0/post**", lambda r: r.abort())
                await page.goto(target, wait_until="domcontentloaded",
                                timeout=30000)
                token = await _get_turnstile_response_route(page)
                log.info("Route-intercept: token obtained in %.1fs",
                         time.monotonic() - t0)

                payload = dict(verify_payload or {})
                payload["token"] = token
                # Parameterized: verify_url + payload pass as evaluate() args, never
                # interpolated into JS source (injection-safe).
                result = await fetch_from_page(
                    page, verify_url, "POST", json.dumps(payload))
                codes = _error_codes(result["body"])
                # Do NOT log the response body — it may carry session tokens/JWTs.
                log.info("Route-intercept verify: %d codes=%s",
                         result["status"], codes)

                return {"token": token, "expires_in": 300,
                        "verify_status": result["status"],
                        "verify_body": result["body"],
                        "verify_error_codes": codes,
                        "method": "route",
                        "elapsed": round(time.monotonic() - t0, 1)}
            finally:
                await page.close()


# ── Real-page solver ────────────────────────────────────────────────

# Sitekey is passed as the evaluate() arg `k` — never interpolated into JS source
# (injection-safe). No data-theme: a hard-coded theme is a fixed real-page fingerprint.
_WIDGET_INJECT_JS = (
    "(k) => {"
    "  const d = document.createElement('div');"
    "  d.className = 'cf-turnstile';"
    "  d.setAttribute('data-sitekey', k);"
    "  document.body.prepend(d);"
    "}"
)


async def _inject_turnstile_widget(page, sitekey: str) -> None:
    """Inject a .cf-turnstile widget with the sitekey passed as data (evaluate arg)."""
    await page.evaluate(_WIDGET_INJECT_JS, sitekey)


async def _human_click_iframe(page, fr) -> bool:
    """Click the Turnstile checkbox via humanized page-level mouse movement.

    CloakBrowser's humanizer hooks page.mouse.click (B-spline paths + overshoot) but
    NOT frame.click, so fr.click() inside the cross-origin iframe sends a robotic instant
    click. Instead we resolve the iframe's page-absolute box and click at the checkbox
    offset (left edge + 30px, vertical centre) via the humanized page.mouse.
    """
    try:
        el = await fr.frame_element()
        box = await el.bounding_box()
    except Exception:
        return False
    if not box or box["width"] < 20:
        return False
    x = box["x"] + 30
    y = box["y"] + box["height"] / 2
    await page.mouse.click(x, y)  # humanized (B-spline) — page-level, not frame
    return True


async def _click_turnstile_checkbox(page, attempts: int = 25) -> bool:
    """Click the checkbox inside the cross-origin Cloudflare iframe.

    Prefers a humanized page-level mouse click on the iframe's box; falls back to
    a frame-level selector click (not humanized) if the box can't be resolved.
    """
    for _ in range(attempts):
        for fr in page.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                if await _human_click_iframe(page, fr):
                    return True
                for sel in ("input[type=checkbox]", "label", "body"):
                    try:
                        await fr.click(sel, timeout=2000)
                        return True
                    except Exception:
                        continue
        await asyncio.sleep(1)
    return False


async def solve_turnstile_realpage(url: str, sitekey: str = None,
                                   timeout_s: int = 60,
                                   pre_actions: list = None,
                                   post_fetch: list = None,
                                   proxy: str = None) -> dict:
    """Navigate a real page, execute pre_actions, click the CF Turnstile checkbox,
    return the token and browser cookies.

    pre_actions — optional list of steps before Turnstile appears:
      [{"type": "click", "selector": "text=Continue with Email"},
       {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"},
       {"type": "click", "selector": "button[type=submit]"}]

    post_fetch — optional list of API calls to make from the SAME browser session
    after solving (keeps cookies/session for endpoints that require same-origin):
      [{"url": "https://app.kilo.ai/api/auth/verify-turnstile", "method": "POST", "body": {"token": "__TOKEN__"}},
       {"url": "https://app.kilo.ai/api/auth/magic-link", "method": "POST", "body": {"email": "user@example.com", "callbackUrl": "/"}}]

    Use __TOKEN__ placeholder in body to inject the solved Turnstile token.

    Selector formats supported: CSS, XPath (//), text=, regex=, role=
    """
    t0 = time.monotonic()

    async with solve_slot():
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)

                if pre_actions:
                    await run_pre_actions(page, pre_actions)
                    await asyncio.sleep(2)

                # Inject sitekey widget if given (override page's own).
                if sitekey:
                    await _inject_turnstile_widget(page, sitekey)
                    await asyncio.sleep(3)

                clicked = await _click_turnstile_checkbox(page)
                log.info("Real-page checkbox clicked=%s", clicked)

                # Harvest token, bounded by timeout_s.
                token = ""
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    try:
                        token = await page.evaluate(
                            "() => { const e=document.querySelector('[name=cf-turnstile-response]');"
                            " return e ? e.value : '' }")
                    except Exception:
                        token = ""
                    if token:
                        break
                    await asyncio.sleep(1)

                cookies = await page.context.cookies()
                result = {"token": token,
                          "verify_success": bool(token),
                          "cookies": cookies,
                          "method": "real-page",
                          "elapsed": round(time.monotonic() - t0, 1)}

                # Post_fetch from the same session (parameterized — injection-safe).
                if post_fetch and token:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, token)

                return result
            finally:
                await page.close()

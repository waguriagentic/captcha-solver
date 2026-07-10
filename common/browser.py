"""Shared browser helpers for the captcha solvers.

Single source for selector resolution, pre-action execution, per-solver browser
kwargs, and the post_fetch loop. All caller values reach the browser as evaluate()
args, never interpolated into JS source (injection-safe).
"""
import asyncio
import json
import logging
import os
import re

log = logging.getLogger(__name__)

# Parameterized fetch: caller values (url, method, body) arrive as evaluate() args,
# NEVER interpolated into JS source — safe against quotes/injection.
_FETCH_JS = (
    "async ({u, m, b}) => { try {"
    " const r = await fetch(u, {method: m,"
    " headers: {'Content-Type': 'application/json'},"
    " body: b, credentials: 'include'});"
    " return {status: r.status, body: await r.text()};"
    " } catch(e) { return {status: 0, body: String(e)}; } }"
)


async def resolve_selector(page, selector: str, timeout: int = 10000):
    """Resolve a selector string to a Playwright locator.

    Supports:
      - CSS (default): input[type=email]
      - XPath:         //button[@type='submit']
      - Text:          text=Continue with Email
      - Regex:         regex=Continue.*Email
      - Role:          role=button[name='Submit']
    """
    if selector.startswith("//") or selector.startswith("(//"):
        return page.locator(f"xpath={selector}")
    if selector.startswith("text="):
        return page.get_by_text(selector[5:], exact=False)
    if selector.startswith("regex="):
        return page.locator(f"text=/{selector[6:]}/")
    if selector.startswith("role="):
        m = re.match(r"role=(\w+)(?:\[name=['\"](.+?)['\"]\])?", selector)
        if m:
            role, name = m.group(1), m.group(2)
            kwargs = {"name": name} if name else {}
            return page.get_by_role(role, **kwargs)
    return page.locator(selector)


async def run_pre_actions(page, actions: list):
    """Execute a list of pre-actions before solving.

    Each action: {"type": "click|fill|select|press|wait", "selector": "...",
                  "value": "...", "timeout": N}
    """
    for i, action in enumerate(actions):
        atype = action.get("type", "")
        selector = action.get("selector", "")
        value = action.get("value", "")
        timeout = action.get("timeout", 10000)
        log.info("Pre-action %d: %s %s", i + 1, atype, (selector or "")[:60])
        if atype == "click":
            loc = await resolve_selector(page, selector, timeout)
            await loc.click(timeout=timeout)
        elif atype == "fill":
            loc = await resolve_selector(page, selector, timeout)
            await loc.fill(value, timeout=timeout)
        elif atype == "select":
            loc = await resolve_selector(page, selector, timeout)
            await loc.select_option(value, timeout=timeout)
        elif atype == "press":
            await page.keyboard.press(value)
        elif atype == "wait":
            await asyncio.sleep(float(value or 1))
        else:
            log.warning("Unknown pre-action type: %s", atype)
        await asyncio.sleep(0.5)


def browser_kwargs(prefix: str) -> dict:
    """Browser-trust levers, env-configurable per solver.

    prefix is TURNSTILE | RECAPTCHA | HCAPTCHA. Turnstile defaults headless=1;
    the interactive-checkbox solvers (recaptcha/hcaptcha) default headless=0.
    """
    default_headless = "1" if prefix == "TURNSTILE" else "0"
    kw = {"humanize": True,
          "headless": os.getenv(f"{prefix}_HEADLESS", default_headless) != "0"}
    if os.getenv(f"{prefix}_PROXY"):
        kw["proxy"] = os.environ[f"{prefix}_PROXY"]
    if os.getenv(f"{prefix}_GEOIP") == "1":
        kw["geoip"] = True
    return kw


def route_glob(url: str) -> str:
    """Glob for page.route() that matches the target with or without a trailing slash,
    plus any sub-path.

    goto("https://ex.com") actually requests "https://ex.com/", so a bare-URL route
    pattern silently misses and the solve hangs until timeout. Appending `/**` matches
    the bare, trailing-slash, and deeper-path forms. Only origin-only URLs are rewritten;
    a URL that already carries a path matches exactly as-is.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.path in ("", "/"):          # origin-only → needs the glob
        return f"{parts.scheme}://{parts.netloc}/**"
    return url                            # has a path → exact match already works


async def fetch_from_page(page, url: str, method: str, body: str) -> dict:
    """One parameterized fetch from the page's session. Returns {status, body}."""
    return await page.evaluate(_FETCH_JS, {"u": url, "m": method.upper(), "b": body})


async def run_post_fetch(page, post_fetch: list, token: str) -> list:
    """Run post_fetch API calls from the SAME browser session (keeps cookies/origin).

    Each entry: {"url": ..., "method": "POST", "body": {...}}. `__TOKEN__` in the
    JSON body is replaced with the solved token. All values are passed as evaluate()
    args (never interpolated into JS source).
    """
    results = []
    for pf in post_fetch:
        pf_url = pf["url"]
        pf_method = pf.get("method", "POST")
        body_str = json.dumps(pf.get("body", {})).replace("__TOKEN__", token)
        try:
            fr = await fetch_from_page(page, pf_url, pf_method, body_str)
            log.info("post_fetch %s: %d", pf_url, fr["status"])
            results.append({"url": pf_url, **fr})
        except Exception as e:
            log.warning("post_fetch %s failed: %s", pf_url, e)
            results.append({"url": pf_url, "status": 0, "body": str(e)})
    return results

"""Harvest an AWS WAF token by letting the silent challenge run in a real browser.

AWS WAF's Challenge/CAPTCHA action ships a JS proof-of-work (token.js / challenge.js)
that runs invisibly on the protected page and, on success, sets the `aws-waf-token`
cookie. We navigate the real URL, let that JS execute, and poll the cookie jar — the
exact same shape as our cf_clearance solver (navigate → poll named cookie → return
jar+UA), just a different cookie name and no checkbox to click.

The token is bound to the client the same way cf_clearance is (IP + TLS/JA3 + UA), so
we pass the proxy through and return the replay warning.

ponytail: SILENT/JS challenge only — this has NO answer for AWS WAF's interactive
  visual CAPTCHA puzzle (the grid/carousel one). If a target ESCALATES to the puzzle,
  the cookie never sets and we time out. add visual-puzzle solve (drive the puzzle +
  Mistral vision, reuse recaptcha/image_solve.py) when a real target needs it.
"""
import asyncio
import logging
import time

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()

# CloudFront/WAF hard-blocks (datacenter IP, geo-block) serve an error page whose title
# matches one of these — the WAF JS never runs, so detect early and fail fast. Bare
# "error" is intentionally excluded (it substring-matched benign titles like "terror");
# the specific phrases still catch an "ERROR: ..." prefix via substring match.
_BLOCK_TITLES = ("403 forbidden", "access denied", "request blocked",
                 "the request could not be satisfied")


def _waf_token(cookies: list):
    """Return the aws-waf-token cookie record (whole dict) or None."""
    return next((c for c in cookies if c.get("name") == "aws-waf-token"), None)


def _title_is_block(title: str) -> bool:
    """Pure predicate: True if a page title matches a known CloudFront/WAF hard-block
    (case-insensitive substring against the specific block phrases)."""
    t = (title or "").lower()
    return any(b in t for b in _BLOCK_TITLES)


async def _is_cloudfront_block(page) -> bool:
    """True when the page is a CloudFront/WAF block error, not the real target."""
    try:
        title = await page.title()
    except Exception:
        return False
    return _title_is_block(title)


def _kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy=proxy)


async def _navigate_and_poll(browser, url, timeout_s, pre_actions):
    """Navigate, run the silent WAF JS, poll for aws-waf-token. Returns (token, cookies, ua, lang, blocked)."""
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if pre_actions:
            await run_pre_actions(page, pre_actions)

        # Early abort on a CloudFront/WAF hard-block — the challenge JS won't run.
        if await _is_cloudfront_block(page):
            ua = await page.evaluate("() => navigator.userAgent")
            return None, await page.context.cookies(), ua, "en-US", True

        tok, cookies = None, []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            cookies = await page.context.cookies()
            tok = _waf_token(cookies)
            if tok:
                break
            await asyncio.sleep(1)

        ua = await page.evaluate("() => navigator.userAgent")
        lang = await page.evaluate("() => navigator.language")
        return tok, cookies, ua, lang, False
    finally:
        await page.close()


async def solve_aws_waf(url: str, proxy: str = None, timeout_s: int = 60,
                        pre_actions: list = None, post_fetch: list = None) -> dict:
    """Navigate an AWS-WAF-protected URL, let the silent challenge set aws-waf-token,
    and return the token + everything needed to replay it from the same IP/UA.

    On a CloudFront/WAF hard-block (datacenter-IP 403), retries once through a fresh
    context (helps only if a proxy is set / rotates).
    """
    t0 = time.monotonic()
    async with _solve_lock:
        tok = cookies = ua = None
        lang = "en-US"
        for attempt in (1, 2):
            async with await cloakbrowser.launch_async(**_kwargs(proxy)) as browser:
                tok, cookies, ua, lang, blocked = await _navigate_and_poll(
                    browser, url, timeout_s, pre_actions)
                if not blocked:
                    break
                log.warning("AWS WAF: CloudFront block on attempt %d", attempt)
                # Only a second attempt can differ if the proxy rotates the exit IP.
                if attempt == 2:
                    tok = None

        result = {
            "token": tok["value"] if tok else "",
            "aws_waf_token": tok,                    # full record or None
            "success": bool(tok),
            "cookies": cookies,                      # full jar → Cookie header
            "user_agent": ua,
            "headers": {"User-Agent": ua, "Accept-Language": lang},
            "proxy": proxy or None,
            "method": "silent-challenge",
            "elapsed": round(time.monotonic() - t0, 1),
            "warning": ("aws-waf-token is bound to IP + JA3/TLS + User-Agent. Replay "
                        "ONLY from the same proxy IP, with this exact User-Agent, over a "
                        "TLS stack producing the same JA3."),
        }
        if not tok:
            result["error"] = ("aws-waf-token not set — either a CloudFront/WAF hard-block "
                               "(try a residential proxy) or the target escalated to the "
                               "interactive visual puzzle (not supported; silent-only).")
        if post_fetch and tok:
            # Re-open a session to run post_fetch with the token/cookies present.
            async with await cloakbrowser.launch_async(**_kwargs(proxy)) as browser:
                page = await browser.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, tok["value"])
                finally:
                    await page.close()
        return result

# skipped: interactive AWS WAF visual-puzzle solve — silent challenge covers the common
#   case; add puzzle-drive + Mistral vision (reuse recaptcha/image_solve.py) when a
#   target escalates. skipped: sec-ch-ua header capture — UA + Accept-Language + jar
#   cover the documented binding; add if a target rejects replay despite matching IP+UA+JA3.

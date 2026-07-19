"""Harvest a Cloudflare cf_clearance cookie by passing the full-page interstitial.

Handles both interstitial variants with ONE code path:
  (A) Managed Challenge — a Turnstile checkbox inside the challenges.cloudflare.com
      iframe. Reuses turnstile._click_turnstile_checkbox (best-effort; a no-op when
      the interstitial is the passive JS variant).
  (B) JS Challenge — "Checking your browser…" that auto-resolves; no click needed.
Either way we poll page.context.cookies() until cf_clearance appears AND the
interstitial markers are gone, bounded by timeout_s.

cf_clearance is bound to IP + JA3/TLS + User-Agent + the challenge. To replay it the
client MUST use the SAME proxy IP and the returned user_agent over a matching TLS
stack. Hence proxy passthrough (per-request `proxy`) and the returned warning.
"""
import asyncio
import logging
import time

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch
from turnstile.solve import _click_turnstile_checkbox

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()

# One JS probe covers both Managed and JS Challenge interstitials. challenge-form +
# _cf_chl_opt blob appear on both variants; the turnstile iframe is Managed-only.
_CF_MARKERS_JS = r"""() => {
  const html = document.documentElement.outerHTML;
  const t = (document.title || '').toLowerCase();
  return (
    !!document.querySelector('#challenge-form, form#challenge-form') ||
    !!document.querySelector('#cf-wrapper, .cf-browser-verification, #challenge-running, #trk_jschal_js') ||
    !!document.querySelector('iframe[src*="challenges.cloudflare.com"]') ||
    /window\._cf_chl_opt|__cf_chl_/.test(html) ||
    /just a moment|attention required|checking your browser|verifying you are human/.test(t)
  );
}"""


def _clearance(cookies: list):
    """Return the cf_clearance cookie dict (whole record) or None."""
    return next((c for c in cookies if c.get("name") == "cf_clearance"), None)


async def _is_interstitial(page) -> bool:
    try:
        return bool(await page.evaluate(_CF_MARKERS_JS))
    except Exception:
        return False  # mid-navigation; treat as "not blocking", the cookie poll decides


def _kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy=proxy)


async def solve_cf_clearance(url: str, proxy: str = None, timeout_s: int = 60,
                             pre_actions: list = None, post_fetch: list = None) -> dict:
    """Navigate a CF-protected URL, pass the interstitial (Managed click or JS wait),
    and return cf_clearance + everything needed to replay it from the same IP/UA."""
    t0 = time.monotonic()
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)

                # (A) Managed: best-effort click on the cross-origin iframe checkbox.
                #     Navigation during the click race is expected — swallow it; the
                #     cookie poll below is the real gate. (B) JS Challenge: a no-op.
                try:
                    await _click_turnstile_checkbox(page, attempts=8)
                except Exception:
                    pass

                # Poll cookies + DOM. Success = cf_clearance set AND markers gone.
                clg, cookies = None, []
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    cookies = await page.context.cookies()
                    clg = _clearance(cookies)
                    if clg and not await _is_interstitial(page):
                        break
                    if clg:                       # cookie set; give the reload one beat
                        await asyncio.sleep(1)
                        cookies = await page.context.cookies()
                        clg = _clearance(cookies)
                        break
                    await asyncio.sleep(1)

                ua = await page.evaluate("() => navigator.userAgent")
                lang = await page.evaluate("() => navigator.language")
                result = {
                    "cf_clearance": clg,                 # full record or None
                    "success": bool(clg),
                    "cookies": cookies,                  # full jar → Cookie header
                    "user_agent": ua,
                    "headers": {"User-Agent": ua, "Accept-Language": lang},
                    "proxy": proxy or None,
                    "method": "interstitial",
                    "elapsed": round(time.monotonic() - t0, 1),
                    "warning": ("cf_clearance is bound to IP + JA3/TLS + User-Agent. "
                                "Replay ONLY from the same proxy IP, with this exact "
                                "User-Agent, over a TLS stack producing the same JA3."),
                }
                if not clg:
                    result["error"] = "cf_clearance not set (challenge unsolved)"
                if post_fetch and clg:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, clg["value"])
                return result
            finally:
                await page.close()

# skipped: parsing extra client headers (sec-ch-ua etc.) — UA + Accept-Language + cookies
#   cover the documented binding. add when: a target rejects replay despite matching IP+UA+JA3.
# skipped: server-side replay of cf_clearance — return the warning, let the client replay.
#   add when: a client needs a /replay endpoint reusing _kwargs(proxy).

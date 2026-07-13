"""Extract a Google BotGuard (bgRequest) token via CloakBrowser.

BotGuard tokens CANNOT be generated offline (VM is polymorphic, per-session,
runtime+iframe-realm bound — see ~/scripts/botguard-re/BOTGUARD_SOLVER.md). The
only viable path is to run the real VM in a browser once and extract the token.

This is a PAGE-LEVEL solver (like cloudflare/awswaf): no sitekey. The token is
session-bound — it only replays successfully together with the cookies from the
SAME browser session, so we return token + cookies + user_agent as a bundle.

Extraction point: on the Google sign-in flow, the account-lookup RPC (MI613e)
carries the bgRequest token in its f.req body (a >200-char base64 blob prefixed
by the '!' marker). We drive email entry, intercept+abort that RPC so the token
stays UNUSED, and return it. The caller replays the login over pure HTTP with
the returned cookies (proven end-to-end against the password hard-gate).
"""
import asyncio
import logging
import re
import time
import urllib.parse
from typing import Optional

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()

# A BotGuard token in the f.req array is a long base64url blob. 200+ chars is the
# reliable floor (real tokens seen: 1680-1850 chars for password-step, ~1800 for
# account-lookup). We take the longest match to avoid grabbing shorter session ids.
_BG_BLOB = re.compile(r"[A-Za-z0-9_\-]{200,}")

# The RPCs that carry a BotGuard token during Google sign-in.
#   MI613e = account-lookup (email step) — reachable with just an email
#   B4hajb = password-submit — the hard gate (needs email+password on the page)
_TOKEN_RPCS = ("MI613e", "B4hajb")

_SIGNIN_URL = (
    "https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn"
)


def _browser_kwargs() -> dict:
    return browser_kwargs("BOTGUARD")


async def solve_botguard(
    url: Optional[str] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout_s: int = 90,
    pre_actions: Optional[list] = None,
) -> dict:
    """Extract a fresh BotGuard token + session cookies from the Google sign-in flow.

    Args:
      url:      sign-in entry URL (defaults to the GlifWebSignIn identifier page).
      email:    account email to enter (drives to the token-bearing RPC).
      password: optional — if given, drives to the password step and grabs the
                B4hajb (hard-gate) token instead of the MI613e (lookup) token.
      proxy:    per-request proxy; the token+cookies are IP/session-bound, so
                replay from this same proxy.
      timeout_s: overall deadline (also enforced by the server's asyncio.timeout).

    Returns a bundle: {token, rpc, cookies, user_agent, replay_url, replay_body, ...}.
    The token is UNUSED (the outgoing RPC is intercepted+aborted) so the caller
    can replay it exactly once via HTTP with the returned cookies.
    """
    t0 = time.monotonic()
    target = url or _SIGNIN_URL

    async with _solve_lock:
        kwargs = _browser_kwargs()
        if proxy:
            kwargs["proxy"] = proxy

        browser = await cloakbrowser.launch_async(**kwargs)
        grab = {"body": None, "url": None, "headers": None, "rpc": None}
        # When a password is supplied we want the B4hajb hard-gate token, which is
        # emitted AFTER the MI613e lookup token. So restrict which RPC we grab:
        #   password given -> only B4hajb (skip the earlier soft-signal MI613e)
        #   email only     -> either token-bearing RPC (MI613e arrives first)
        wanted_rpcs = ("B4hajb",) if (email and password) else _TOKEN_RPCS
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()

            async def route_handler(route):
                req = route.request
                if (req.method == "POST" and "batchexecute" in req.url
                        and any(r in req.url for r in wanted_rpcs)
                        and grab["body"] is None):
                    try:
                        grab["body"] = req.post_data
                        grab["url"] = req.url
                        grab["headers"] = await req.all_headers()
                        grab["rpc"] = next(
                            (r for r in _TOKEN_RPCS if r in req.url), None)
                    except Exception:
                        pass
                    # abort so the token is never consumed by the browser itself
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/batchexecute*", route_handler)

            try:
                await page.goto(target, wait_until="networkidle", timeout=45000)
            except Exception as e:
                log.warning("botguard nav: %s", e)
            await asyncio.sleep(2)

            if pre_actions:
                await run_pre_actions(page, pre_actions)

            # step 1: email
            if email:
                for sel in ('input#identifierId', 'input[type="email"]',
                            'input[name="identifier"]'):
                    try:
                        await page.fill(sel, email, timeout=6000)
                        break
                    except Exception:
                        continue
                for sel in ('#identifierNext', 'button:has-text("Next")',
                            'button:has-text("Berikutnya")'):
                    try:
                        await page.click(sel, timeout=6000)
                        break
                    except Exception:
                        continue
                await asyncio.sleep(6)

            # step 2 (optional): password — drives to the B4hajb hard-gate token
            if email and password:
                filled = False
                for sel in ('input[type="password"]', 'input[name="Passwd"]'):
                    try:
                        await page.fill(sel, password, timeout=8000)
                        filled = True
                        break
                    except Exception:
                        continue
                if filled:
                    for sel in ('#passwordNext', 'button:has-text("Next")',
                                'button:has-text("Berikutnya")'):
                        try:
                            await page.click(sel, timeout=6000)
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(5)

            # collect the token + session material
            cookies = await ctx.cookies()
            try:
                ua = await page.evaluate("() => navigator.userAgent")
            except Exception:
                ua = None

            if not grab["body"]:
                return {
                    "token": "",
                    "error": "no token-bearing RPC intercepted "
                             "(email required; SSO redirect or flow changed?)",
                    "method": "route-intercept",
                    "elapsed": round(time.monotonic() - t0, 2),
                }

            dec = urllib.parse.unquote_plus(grab["body"])
            blobs = _BG_BLOB.findall(dec)
            token = max(blobs, key=len) if blobs else ""

            cookie_list = [{"name": c["name"], "value": c["value"],
                            "domain": c.get("domain"), "path": c.get("path")}
                           for c in cookies]
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            return {
                "token": token,
                "token_len": len(token),
                "rpc": grab["rpc"],
                "gate": "password" if grab["rpc"] == "B4hajb" else "account-lookup",
                "method": "route-intercept",
                "user_agent": ua,
                "cookies": cookie_list,
                "cookie_header": cookie_header,
                # everything needed to replay the exact RPC over HTTP:
                "replay_url": grab["url"],
                "replay_body": grab["body"],
                "replay_headers": {
                    k: v for k, v in (grab["headers"] or {}).items()
                    if k.lower() not in ("content-length", "host", "cookie")
                },
                "elapsed": round(time.monotonic() - t0, 2),
            }
        finally:
            await browser.close()

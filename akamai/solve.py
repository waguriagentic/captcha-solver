"""Harvest an Akamai Bot Manager `_abck` clearance cookie from a live session.

Harvest-only (same contract as cloudflare/awswaf): SOLVE the bot-check + return a
replayable cookie. NO account creation, NO form filling, NO site hardcoded — the
caller passes the Akamai-fronted `url` it wants cleared.

Why a live browser is mandatory (proven in re-artifacts/akamai/):
  - the sensor payload embeds a SERVER nonce from the `bm_sz` cookie (challenge-
    response) — cannot be built offline;
  - the sensor carries per-call accumulation state — offline replay drifts.
So we load the URL in CloakBrowser, let the `bmak` sensor boot (it fetches `bm_sz`),
drive `bmak.get_telemetry()` + a little input so the sensor POSTs accepted telemetry,
then poll until `_abck` reaches its validated form.

`_abck` validation heuristic (public, widely used): a FRESH/unvalidated cookie looks
like `...~0~...` or `...~-1~...` with the 2nd `~`-field == the initial marker; once
the server accepts ≥2 sensor POSTs it flips so the 2nd field is `-1` and the 4th
field is no longer `0`. We treat `_abck.split('~')[1] == '-1'` (after driving posts)
as "likely validated" but DO NOT trust it blindly — the authoritative proof is an
HTTP replay by the caller. We return the cookie + replay contract regardless.

`_abck` is bound to IP + JA3/TLS + User-Agent. Replay ONLY from the same proxy IP
with the returned user_agent over a matching TLS stack.
"""
import asyncio
import logging
import time

import cloakbrowser

from common.browser import browser_kwargs, run_pre_actions, run_post_fetch

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()

# In-page: find the sensor global (usually `bmak`) and fire get_telemetry() so the
# sensor serializes + POSTs a fresh payload. Returns telemetry length for logging.
_FIRE_TELEMETRY_JS = r"""() => {
  const out = {fired: false};
  try {
    let s = (typeof bmak !== 'undefined') ? bmak : null;
    if (!s) {
      for (const k of Object.getOwnPropertyNames(window)) {
        try { const v = window[k];
          if (v && typeof v === 'object' && typeof v.get_telemetry === 'function') { s = v; out.name = k; break; }
        } catch(e){}
      }
    }
    if (s && typeof s.get_telemetry === 'function') {
      const t = s.get_telemetry();
      out.fired = true; out.len = t ? t.length : 0;
    }
  } catch(e) { out.err = String(e); }
  return out;
}"""


def _abck_cookie(cookies: list):
    return next((c for c in cookies if c.get("name") == "_abck"), None)


def _bm_sz_cookie(cookies: list):
    return next((c for c in cookies if c.get("name") == "bm_sz"), None)


def _abck_validated(abck_value: str) -> bool:
    """Heuristic for a *validated* _abck. NOT authoritative — an HTTP replay against
    a gated endpoint by the caller is the only real proof.

    Empirically (bestbuy real cookie vs live att/ups harvests): a FRESH cookie carries
    the invalidated tail `~-1~-1~-1~<ts>~...` regardless of the 2nd field. The 2nd
    field being '-1' does NOT mean validated (the bestbuy sample had '-1' and was the
    initial marker). The reliable discriminator: a validated cookie DROPS the
    `~-1~-1~-1~` triple (the middle sentinels become request counters). We therefore
    flag validated ONLY when that invalidated triple is absent.
    """
    if not abck_value:
        return False
    # invalidated cookies contain the ~-1~-1~-1~ sentinel triple; validated ones don't.
    return "~-1~-1~-1~" not in abck_value


def _kwargs(proxy: str = None) -> dict:
    return browser_kwargs("TURNSTILE", proxy=proxy)


async def solve_akamai(url: str, proxy: str = None, timeout_s: int = 90,
                       pre_actions: list = None, post_fetch: list = None) -> dict:
    """Load an Akamai-fronted URL, drive the bmak sensor until _abck validates,
    and return _abck + everything needed to replay it from the same IP/UA."""
    t0 = time.monotonic()
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)

                # Count the sensor's OWN telemetry POSTs. Calling get_telemetry()
                # only RETURNS the payload — the sensor submits it via its own XHR on
                # an event/timer schedule. The server validates _abck (~0~ -> ~-1~)
                # only after it ACCEPTS ≥2 of those real POSTs, and typically only
                # once a gated resource is fetched. So we drive genuine interaction +
                # a same-site navigation and watch the cookie flip, not manual fires.
                posts = {"n": 0}

                def _on_request(rq):
                    try:
                        if rq.method == "POST" and "sensor_data" in (rq.post_data or ""):
                            posts["n"] += 1
                    except Exception:
                        pass
                page.on("request", _on_request)

                # Let the sensor bootstrap (fetch bm_sz + arm bmak).
                await page.wait_for_timeout(4000)

                deadline = time.monotonic() + timeout_s
                abck, cookies, fires = None, [], []
                nudge_count = 0
                while time.monotonic() < deadline:
                    # real behavioral input — drives the sensor's event-based POST
                    try:
                        await page.mouse.move(120, 140)
                        await page.mouse.move(320, 360)
                        await page.mouse.wheel(0, 350)
                        await page.mouse.move(500, 200)
                        await page.keyboard.press("Tab")
                    except Exception:
                        pass
                    # also fire get_telemetry() so bmak flushes its buffer (helps its
                    # internal scheduler POST sooner); return value is only for logging.
                    try:
                        fires.append(await page.evaluate(_FIRE_TELEMETRY_JS))
                    except Exception as e:
                        fires.append({"err": str(e)})
                    await asyncio.sleep(2)

                    cookies = await page.context.cookies()
                    abck = _abck_cookie(cookies)
                    if abck and _abck_validated(abck.get("value", "")):
                        break

                    # Force the sensor's event-based POST: a same-site navigation is
                    # the strongest trigger (bmak POSTs on nav + fires a fresh sensor).
                    # Do it a couple of times — validation needs ≥2 accepted POSTs, and
                    # a slow (residential) proxy starves the timer-based POST.
                    if (time.monotonic() - t0) > 10 and nudge_count < 3 and posts["n"] < 2:
                        nudge_count += 1
                        try:
                            # click the first in-site link (real navigation event)
                            await page.evaluate(
                                "() => { const a=[...document.querySelectorAll('a[href^=\"/\"],a[href*=\"'+location.host+'\"]')].find(x=>x.offsetParent); if(a) a.click(); }")
                            await page.wait_for_timeout(3000)
                        except Exception:
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass

                bm_sz = _bm_sz_cookie(cookies)
                ua = await page.evaluate("() => navigator.userAgent")
                lang = await page.evaluate("() => navigator.language")
                validated = bool(abck and _abck_validated(abck.get("value", "")))
                result = {
                    "_abck": abck,                       # full cookie record or None
                    "bm_sz": bm_sz,                       # server nonce cookie (context)
                    "success": validated,
                    "abck_validated_heuristic": validated,
                    "sensor_fires": len([f for f in fires if f.get("fired")]),
                    "sensor_posts": posts["n"],
                    "cookies": cookies,                  # full jar → Cookie header
                    "user_agent": ua,
                    "headers": {"User-Agent": ua, "Accept-Language": lang},
                    "proxy": proxy or None,
                    "method": "bmak-telemetry-harvest",
                    "elapsed": round(time.monotonic() - t0, 1),
                    "warning": ("_abck is bound to IP + JA3/TLS + User-Agent and has a "
                                "short TTL. Replay ONLY from the same proxy IP, with this "
                                "exact User-Agent, over a matching TLS stack. The validated "
                                "flag is a heuristic — confirm with an actual HTTP replay."),
                }
                if not abck:
                    result["error"] = "_abck cookie never set (sensor did not arm / site not Akamai)"
                elif not validated:
                    result["error"] = "_abck present but not validated (sensor POSTs not accepted — try more time / residential proxy)"
                if post_fetch and validated:
                    result["post_fetch"] = await run_post_fetch(
                        page, post_fetch, abck["value"])
                return result
            finally:
                await page.close()

# skipped: server-side /replay of _abck — return the warning + cookie jar, let the
#   client replay from the same IP+UA+JA3. add when a caller needs a bundled replay.

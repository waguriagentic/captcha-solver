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

Run headed under Xvfb (BROWSER_HEADLESS=0, the default here) — headless is the most
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
_keypool_cache: dict = {}  # mode -> classifier instance

_VALID_CLASSIFIERS = frozenset({"yolo", "mistral", "hybrid", "auto"})


def _build_keypool():
    model = os.getenv("RECAPTCHA_MISTRAL_MODEL", "mistral-medium-latest")
    # vary the start offset by pid so concurrent procs don't hammer key #0
    return KeyPool(str(_KEYFILE), model=model, start_index=os.getpid())


def _get_keypool(classifier: str = None):
    """Lazy tile classifier for the image challenge.

    `classifier` (optional body param on /solve):
      - None / "auto"  — ONNX hybrid if model present, else pure Mistral (default)
      - "hybrid"       — ONNX-first + Mistral fallback for unknown targets
      - "yolo"         — pure local ONNX, no Mistral (fails if model missing)
      - "mistral"      — pure Mistral vision, skip ONNX

    Named _get_keypool for call-site compatibility; the returned object only needs a
    .classify(image_b64, target) -> bool method (KeyPool / OnnxClassifier /
    HybridClassifier all provide it). Cached per resolved mode.
    """
    mode = (classifier or "auto").strip().lower()
    if mode not in _VALID_CLASSIFIERS:
        raise ValueError(
            f"classifier must be one of {sorted(_VALID_CLASSIFIERS)}, got {classifier!r}")

    from .onnx_classifier import get_classifier, HybridClassifier
    onnx = get_classifier()

    if mode == "auto":
        mode = "hybrid" if onnx is not None else "mistral"

    if mode in _keypool_cache:
        return _keypool_cache[mode]

    if mode == "mistral":
        pool = _build_keypool()
        log.info("reCAPTCHA image classifier: Mistral only")
    elif mode == "yolo":
        if onnx is None:
            raise RuntimeError(
                "classifier=yolo but ONNX model missing "
                f"(expected {_KEYFILE.parent.parent / 'recaptcha' / 'models' / 'recaptcha_cls_s.onnx'})")
        pool = onnx
        log.info("reCAPTCHA image classifier: YOLO/ONNX only (no Mistral fallback)")
    else:  # hybrid
        if onnx is None:
            pool = _build_keypool()
            log.info("reCAPTCHA image classifier: Mistral only (hybrid requested, no ONNX model)")
        else:
            pool = HybridClassifier(onnx, _build_keypool())
            log.info("reCAPTCHA image classifier: ONNX hybrid (Mistral fallback)")

    _keypool_cache[mode] = pool
    return pool

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


def _browser_kwargs(proxy: str = None) -> dict:
    return browser_kwargs("RECAPTCHA", proxy=proxy)


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
                             enterprise: bool = False, proxy: str = None) -> dict:
    """Mint a token via grecaptcha[.enterprise].execute() on a route-intercepted page."""
    t0 = time.monotonic()
    lib = "enterprise.js" if enterprise else "api.js"
    ns = "grecaptcha.enterprise" if enterprise else "grecaptcha"
    body = (_V3_PAGE.replace("__LIB__", lib).replace("__NS__", ns)
            .replace("__SITEKEY__", sitekey).replace("__ACTION__", action))
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
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
                             secret: str = None, enterprise: bool = False,
                             proxy: str = None) -> dict:
    """Solve reCAPTCHA v3 (score-based). Returns {token, action, elapsed}.

    The token's *score* is decided server-side by Google. Pass `secret` (the
    target site's secret key) to also run siteverify and return the score —
    without a secret no score exists, only a token. `enterprise=True` loads
    enterprise.js + grecaptcha.enterprise (score read via Cloud Assessment API,
    not the public siteverify endpoint, so `secret` is ignored for Enterprise).
    """
    res = await _solve_via_execute(sitekey, url, action, enterprise=enterprise, proxy=proxy)
    if secret and not enterprise and res.get("token"):
        try:
            v = await asyncio.to_thread(_siteverify, res["token"], secret)
            res["score"] = v.get("score")
            res["verify"] = v
        except Exception as e:
            res["verify_error"] = str(e)
    return res


async def solve_recaptcha_invisible(sitekey: str, url: str, action: str = "submit",
                                    enterprise: bool = False, proxy: str = None) -> dict:
    """Solve invisible reCAPTCHA v2. Identical mechanism to v3 (execute())."""
    return await _solve_via_execute(sitekey, url, action, enterprise=enterprise, proxy=proxy)


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
                                      pre_actions: list = None, proxy: str = None) -> dict:
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
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
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


# ── invisible on the REAL page (Enterprise-safe) ────────────────────
#
# Prefer the page's own grecaptcha[.enterprise] (sites like Webshare load
# enterprise.js?render=explicit + render an invisible widget themselves).
# Do NOT inject enterprise.js?render=<sitekey> — Google returns HTTP 403 for that
# parameterized form outside proper origin binding. Fallback inject uses
# render=explicit only, then render()+execute().

_INVISIBLE_REALPAGE_JS = """
({sitekey, action, enterprise}) => {
  window.__rc_token = ""; window.__rc_err = "";
  const getGre = () => enterprise
    ? (window.grecaptcha && window.grecaptcha.enterprise)
    : window.grecaptcha;

  const harvest = () => {
    try {
      const el = document.querySelector('#g-recaptcha-response, textarea[name="g-recaptcha-response"]');
      if (el && el.value) window.__rc_token = el.value;
    } catch (_) {}
  };

  const runExecute = (gre) => {
    const done = (t) => {
      if (typeof t === 'string' && t) window.__rc_token = t;
      else harvest();
    };
    const fail = (e) => { window.__rc_err = String(e && e.message ? e.message : e); };
    const kick = () => {
      // 1) sitekey+action (v3-style / some enterprise invisible)
      // 2) bare execute() for a pre-rendered invisible widget (react-google-recaptcha)
      // 3) execute(sitekey) without action
      const attempts = [];
      if (sitekey && action) attempts.push(() => gre.execute(sitekey, {action}));
      attempts.push(() => gre.execute());
      if (sitekey) attempts.push(() => gre.execute(sitekey));

      const tryNext = (i) => {
        if (i >= attempts.length) {
          harvest();
          if (!window.__rc_token) window.__rc_err = window.__rc_err || 'execute() returned no token';
          return;
        }
        Promise.resolve()
          .then(attempts[i])
          .then((t) => {
            if (typeof t === 'string' && t) done(t);
            else tryNext(i + 1);
          })
          .catch((e) => {
            window.__rc_err = String(e && e.message ? e.message : e);
            tryNext(i + 1);
          });
      };
      tryNext(0);
    };
    if (gre.ready) gre.ready(kick);
    else kick();
  };

  const waitNative = (maxMs) => new Promise((resolve) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      const gre = getGre();
      if (gre && gre.execute) { clearInterval(iv); resolve(gre); }
      else if (Date.now() - t0 > maxMs) { clearInterval(iv); resolve(null); }
    }, 100);
  });

  (async () => {
    // Prefer page-native gre (Webshare / react-google-recaptcha path).
    let gre = await waitNative(25000);
    if (gre) { runExecute(gre); return; }

    // Fallback: inject lib with render=explicit (NOT render=sitekey — 403 on enterprise.js).
    const lib = enterprise ? 'enterprise.js' : 'api.js';
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = `https://www.google.com/recaptcha/${lib}?render=explicit`;
      s.onload = resolve;
      s.onerror = () => reject(new Error('lib load failed'));
      document.head.appendChild(s);
    }).catch((e) => { window.__rc_err = String(e && e.message ? e.message : e); });

    gre = await waitNative(15000);
    if (!gre) {
      window.__rc_err = window.__rc_err || 'grecaptcha not ready';
      return;
    }

    // With render=explicit, mint an invisible widget then execute it.
    try {
      let host = document.getElementById('__rc_host');
      if (!host) {
        host = document.createElement('div');
        host.id = '__rc_host';
        host.style.display = 'none';
        document.body.appendChild(host);
      }
      const opts = {sitekey, size: 'invisible'};
      if (action) opts.action = action;
      const wid = gre.render(host, opts);
      gre.ready(() => {
        Promise.resolve(gre.execute(wid))
          .then((t) => {
            if (typeof t === 'string' && t) window.__rc_token = t;
            else harvest();
          })
          .catch((e) => {
            // last resort: sitekey form
            Promise.resolve(gre.execute(sitekey, action ? {action} : undefined))
              .then((t) => {
                if (typeof t === 'string' && t) window.__rc_token = t;
                else { harvest(); window.__rc_err = String(e && e.message ? e.message : e); }
              })
              .catch((e2) => { window.__rc_err = String(e2 && e2.message ? e2.message : e2); });
          });
      });
    } catch (e) {
      runExecute(gre);
    }
  })();
}
"""


async def solve_recaptcha_invisible_realpage(
        url: str, sitekey: str, action: str = "submit",
        enterprise: bool = False, timeout_s: int = 90,
        pre_actions: list = None, proxy: str = None,
        classifier: str = None) -> dict:
    """Mint an invisible reCAPTCHA token on the REAL page (no route intercept).

    Prefer page-native grecaptcha[.enterprise] → execute(). If Google escalates to
    an image bframe (common on flagged IPs), solve the grid via the tile classifier
    (`classifier`: yolo|mistral|hybrid|auto — see `_get_keypool`).
    """
    t0 = time.monotonic()
    method = "invisible-enterprise-realpage" if enterprise else "invisible-realpage"
    # Resolve once so a bad classifier fails fast (before browser launch).
    keypool = _get_keypool(classifier)
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if pre_actions:
                    await run_pre_actions(page, pre_actions)
                await asyncio.sleep(2)
                await _simulate_behavior(page)

                await page.evaluate(
                    _INVISIBLE_REALPAGE_JS,
                    {"sitekey": sitekey, "action": action or "",
                     "enterprise": bool(enterprise)})

                deadline = time.monotonic() + timeout_s
                last_err = ""
                image_attempts = 0
                max_image_attempts = 4
                while time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    token = await page.evaluate(
                        "() => window.__rc_token"
                        " || document.querySelector('#g-recaptcha-response')?.value"
                        " || document.querySelector('textarea[name=\"g-recaptcha-response\"]')?.value"
                        " || ''")
                    if token:
                        return {"token": token, "action": action,
                                "elapsed": round(time.monotonic() - t0, 1),
                                "method": method if not image_attempts else "invisible-image"}

                    # Image bframe opened after execute() — solve the grid.
                    # Retry up to max_image_attempts: a failed verify often loads a
                    # fresh challenge, and a one-shot flag would burn the whole timeout.
                    if (image_attempts < max_image_attempts
                            and await _find_frame(page, "/bframe")
                            and await page.frame_locator(_BFRAME_IFRAME).locator(
                                "table").count() > 0):
                        image_attempts += 1
                        log.info("invisible realpage: bframe image challenge detected "
                                 "(attempt %d/%d)", image_attempts, max_image_attempts)
                        try:
                            await solve_image_challenge(page, keypool)
                        except Exception as e:
                            log.warning("invisible image-solve: %s",
                                        str(e).splitlines()[0])
                        # brief settle for token / next challenge after verify
                        await asyncio.sleep(2)
                        token = await _get_token(page)
                        if not token:
                            token = await page.evaluate(
                                "() => window.__rc_token"
                                " || document.querySelector('#g-recaptcha-response')?.value"
                                " || document.querySelector('textarea[name=\"g-recaptcha-response\"]')?.value"
                                " || ''")
                        if token:
                            return {"token": token, "action": action,
                                    "elapsed": round(time.monotonic() - t0, 1),
                                    "method": "invisible-image"}
                        # keep polling for residual token / next challenge
                        continue

                    err = await page.evaluate("() => window.__rc_err || ''")
                    # Suppress the noisy execute() fallback once we're in image mode —
                    # "Invalid site key" is expected after the page-native gre path fails
                    # and we inject render=explicit.
                    if err and err != last_err and not image_attempts:
                        last_err = err
                        log.warning("invisible realpage: %s", err)
                    if await _audio_blocked(page):
                        return {"error": "audio-blocked",
                                "elapsed": round(time.monotonic() - t0, 1)}
                out = {"error": "execute() timed out (no token)",
                       "elapsed": round(time.monotonic() - t0, 1)}
                if last_err and not image_attempts:
                    out["error"] = f"execute() timed out: {last_err}"
                if image_attempts:
                    out["error"] = (out["error"] +
                                    f" (image challenge attempted {image_attempts}x)")
                return out
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
                              enterprise: bool = False,
                              proxy: str = None,
                              classifier: str = None) -> dict:
    """Solve reCAPTCHA v2 checkbox via route intercept.

    Clicks the checkbox; returns the token immediately if the session is low-risk
    (no challenge). If an image grid opens, solves it via the tile classifier
    (`classifier`: yolo|mistral|hybrid|auto — see `_get_keypool`). Set
    `enterprise=True` for Enterprise checkbox keys (loads enterprise.js — the widget
    and challenge are otherwise identical). Returns {token, attempts, elapsed} or
    {error, elapsed}.
    """
    t0 = time.monotonic()
    page_data = _build_v2_page(sitekey, enterprise=enterprise)
    # Resolve once so a bad classifier fails fast (before browser launch).
    keypool = _get_keypool(classifier)

    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
            page = await browser.new_page()
            try:
                await page.route(route_glob(url), lambda r: r.fulfill(body=page_data, status=200))
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                for attempt in range(1, max_attempts + 1):
                    log.info("v2 attempt %d/%d", attempt, max_attempts)
                    for _ctry in range(3):
                        try:
                            await page.frame_locator(_ANCHOR_IFRAME).locator(
                                "#recaptcha-anchor").click(timeout=8000)
                            break
                        except Exception as e:
                            log.warning("checkbox click attempt %d: %s",
                                        _ctry + 1, str(e).splitlines()[0])
                            if _ctry < 2:
                                await asyncio.sleep(1)

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

                    # Image-solve (audio is IP-blocked). Tile classifier: YOLO/Mistral/hybrid.
                    if challenge:
                        try:
                            await solve_image_challenge(page, keypool)
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
                                      timeout_s: int = 60,
                                      proxy: str = None,
                                      classifier: str = None) -> dict:
    """Solve v2 on the REAL page (no route intercept) — the realistic production path.

    Navigates the actual site, runs optional pre_actions, clicks the checkbox in the
    cross-origin iframe, harvests the token, and optionally runs post_fetch API calls
    from the SAME browser session. Mirrors turnstile.solve_turnstile_realpage.

    If Google escalates to an image bframe, solve the grid once via the tile classifier
    (`classifier`: yolo|mistral|hybrid|auto — see `_get_keypool`). The `image_attempted`
    flag prevents re-entering image-solve on the same challenge.

    Use __TOKEN__ in post_fetch bodies to inject the solved token.
    """
    t0 = time.monotonic()
    # Resolve once so a bad classifier fails fast (before browser launch).
    keypool = _get_keypool(classifier)
    async with _solve_lock:
        async with await cloakbrowser.launch_async(**_browser_kwargs(proxy)) as browser:
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
                image_attempts = 0
                max_image_attempts = 4
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    token = await page.evaluate(
                        "() => document.querySelector('#g-recaptcha-response')?.value || ''")
                    if token:
                        break

                    # Image bframe opened after checkbox click — solve the grid.
                    # Retry on fresh challenges after a failed verify.
                    if (image_attempts < max_image_attempts
                            and await _find_frame(page, "/bframe")
                            and await page.frame_locator(_BFRAME_IFRAME).locator(
                                "table").count() > 0):
                        image_attempts += 1
                        log.info("v2 realpage: bframe image challenge detected "
                                 "(attempt %d/%d)", image_attempts, max_image_attempts)
                        try:
                            await solve_image_challenge(page, keypool)
                        except Exception as e:
                            log.warning("v2 realpage image-solve: %s",
                                        str(e).splitlines()[0])
                        await asyncio.sleep(2)
                        token = await _get_token(page)
                        if token:
                            break
                        # keep polling for residual token / next challenge
                        continue

                    if await _audio_blocked(page):
                        break

                cookies = await page.context.cookies()
                method = "real-page-image" if image_attempts and token else "real-page"
                result = {"token": token, "verify_success": bool(token),
                          "cookies": cookies, "method": method,
                          "elapsed": round(time.monotonic() - t0, 1)}
                if not token and await _audio_blocked(page):
                    result["error"] = "audio-blocked"
                elif not token and image_attempts:
                    result["error"] = f"image challenge attempted {image_attempts}x (no token)"

                if post_fetch and token:
                    result["post_fetch"] = await run_post_fetch(page, post_fetch, token)
                return result
            finally:
                await page.close()

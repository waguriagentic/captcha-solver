# reCAPTCHA Solver — v3, invisible, v2 checkbox, Enterprise

Local solver for every reCAPTCHA variant (classic **and** Enterprise), via CloakBrowser.
Mirrors the Turnstile solver's patterns (route-intercept, headed-under-Xvfb,
same-session post_fetch).

> **The old README was wrong.** It claimed "the checkbox rejects all synthetic clicks,
> every approach has been exhausted." Disproven empirically: the click
> **lands**, reCAPTCHA **accepts** it, the challenge **opens**. The `loading` state the
> old doc called a failure actually means the click was accepted. The real wall is only
> on the audio fallback (IP-reputation block), and it only affects v2 — v3 and invisible
> sail right through.

## Solving modes

`version` selects the variant (`v3` | `invisible` | `v2`) and **defaults to `v2`**
(checkbox) when omitted. `action` defaults to `submit` (only matters for score-based
keys; override per your site's action name).

### 1. v3 — score-based (`version: "v3"`) ✅ easiest, no proxy needed

`grecaptcha.execute(sitekey, {action})` on a route-intercepted page. No checkbox, no
challenge. Returns a token in ~4s; Google scores it server-side from browser
fingerprint + IP.

Route-intercept matches the target with a `/**` glob (`route_glob`), so a bare-domain
`url` like `https://ex.com` still intercepts `goto`'s trailing-slash request (previously a
silent miss → hang); URLs that already carry a path were unaffected.

**Verified:** headed under Xvfb from a plain residential IP (no proxy), Google's own
demo scored our token **0.9** (`"success": true`) — the human-level maximum (0.9 = human,
0.1 = bot).

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha","version":"v3","sitekey":"6Lc...","url":"https://target.com/page","action":"submit"}'
```

### 2. Invisible v2 (`version: "invisible"`) ✅

Identical mechanism to v3 — `execute()`, no interaction. Use when the sitekey is
registered as invisible.

```bash
curl -X POST http://localhost:8877/solve \
  -d '{"type":"recaptcha","version":"invisible","sitekey":"6Lc...","url":"https://target.com/login"}'
```

### 2b. reCAPTCHA Enterprise (score-based) — `enterprise: true` ✅

**Client-side, Enterprise score keys are identical to v3.** The only differences:
the page loads `enterprise.js` (not `api.js`) and calls
`grecaptcha.enterprise.execute()` (not `grecaptcha.execute()`). Same route-intercept,
same zero-interaction token mint — verified end-to-end (token in ~4s). Add
`"enterprise": true` to any v3/invisible request:

```bash
curl -X POST http://localhost:8877/solve \
  -d '{"type":"recaptcha","version":"v3","enterprise":true,"sitekey":"6Lc...","url":"https://target.com","action":"login"}'
```

**Score reading differs server-side.** Enterprise scores are *not* read via the public
`siteverify` endpoint — the site owner POSTs the token to the Cloud
`projects.assessments.create` API (needs GCP auth) and gets back
`riskAnalysis.score` + `reasons` (e.g. `["AUTOMATION"]`). As with v3, only the site
owner can read the score; the solver's job is to mint the token. So `secret` is ignored
for Enterprise.

**Enterprise *Checkbox* / *policy-based challenge* keys** render the same
checkbox + image grid as standard v2 — handled by the `version: "v2"` path (image-solve
via Mistral). Add `"enterprise": true` so the page loads `enterprise.js` instead of
`api.js` (the widget auto-renders identically — verified — and the challenge is the
same, so image-solve works unchanged):

```bash
curl -X POST http://localhost:8877/solve \
  -d '{"type":"recaptcha","version":"v2","enterprise":true,"sitekey":"6Lc...","url":"https://target.com/form"}'
```

Score-based Enterprise keys (the recommended/default type) never show a challenge.

### 3. v2 checkbox (`version: "v2"`)

Clicks the checkbox inside the cross-origin `/anchor` iframe (re-resolved every action
via `frame_locator`, so it survives reCAPTCHA reloading the iframe — the bug that broke
the old solver).

- **Low-risk session → no challenge:** checkbox goes straight to `checked`, token
  returned. This is the real win, the same mechanism the Turnstile solver exploits.
- **Challenge opens → image-solve (Mistral vision):** the image grid opens fine (only
  the *audio* path is IP-blocked). The solver screenshots the grid, slices it into
  tiles, classifies each tile yes/no against the target via a **shared Mistral vision key
  pool** (`common/apikey.txt`, thousands of keys, round-robin + auto-failover), clicks the
  matches, and submits. Handles 3×3, 4×4, and **dynamic** grids (re-classifies
  reloaded tiles until a round finds nothing new). Tile classification is bounded to a
  small concurrency (semaphore) to avoid a thread-pool / rate-limit herd. Vision is
  never 100% on reCAPTCHA's deliberately-ambiguous images — success is judged by
  reCAPTCHA accepting the submit, and the caller retries.

> The Whisper-based **audio fallback was removed** — it was dead code (no callers) and
> the audio button is reliably IP-blocked (*"Your computer or network may be sending
> automated queries"*). This also drops the `whisper` + `ffmpeg` dependency.

### Mistral key pool

`common/mistral.py` `KeyPool` reads `common/apikey.txt` (one key per line, shared with
the hCaptcha solver), starts at a pid-varied offset, rotates round-robin, and on
`401/403/429` skips to the next key and retries the same request (parking the dead key
for a **60-second wall-clock cooldown**). Model is `mistral-medium-latest` by default
(override with `RECAPTCHA_MISTRAL_MODEL`) — note `pixtral-12b` aliases to a text model
on the gateway in use, so it can't see images.

```bash
# route-intercept (fast)
curl -X POST http://localhost:8877/solve \
  -d '{"type":"recaptcha","version":"v2","sitekey":"6Lf...","url":"https://target.com/form"}'

# real page (navigates the actual site; supports pre_actions + post_fetch)
curl -X POST http://localhost:8877/solve \
  -d '{"type":"recaptcha","version":"v2","real_page":true,"url":"https://target.com/login",
       "pre_actions":[{"type":"click","selector":"text=Sign in"}],
       "post_fetch":[{"url":"https://target.com/api/verify","body":{"token":"__TOKEN__"}}]}'
```

For a higher score / a low-risk (no-challenge) session on hard targets, set a clean
residential proxy (see env below).

## Response

Every `200` from `/solve` carries a uniform top-level **`"solved": true|false`** — the
one success signal callers read (no per-type branching). Per-type detail rides alongside:
v3 returns `token` + `score`; v2 and invisible return `token`; plus
`expires_in`/`verify_success`/`method`/`elapsed` as applicable.

**Error contract:** a solve that *ran but didn't succeed* → `200` with
`solved:false` + `error`. A request that *never solved* → non-2xx with FastAPI
`{detail}`: `400` (bad/unsupported type, missing url/sitekey, SSRF-blocked host), `408`
(exceeded `timeout_s`), `422` (body schema invalid — `detail` is a list), `500` (solver
crash). Rule of thumb: **2xx → read `solved`; non-2xx → read `detail` (never both).**

## Key facts (verified)

- **Tokens are bound to the site-key, not the domain.** `siteverify` has no
  hostname-mismatch error code. The "supported domains" check is **client-side only**
  (why `127.0.0.1` is refused) — route-intercept at the real origin satisfies it.
- **Audio-solve was removed (dead path in 2026).** Google's v2 audio docs are
  deprecated and the audio block is a server-side risk verdict
  (webdriver/CDP/WebGL/JA3/behaviour) that JS patching can't touch. Prefer no-challenge
  tokens, v3, or invisible. For hard targets, the realistic fallbacks are session
  warming (real Google cookies + residential proxy) or a human-solver API.

## Environment

| Variable             | Default | Description                                       |
| -------------------- | ------- | ------------------------------------------------- |
| `RECAPTCHA_HEADLESS`       | `0`                    | `0` = headed (needs Xvfb); headless is heavily penalised |
| `RECAPTCHA_PROXY`          | —                      | Residential proxy URL (improves score / low-risk session) |
| `RECAPTCHA_GEOIP`          | —                      | `1` to spoof tz/locale/WebGL to match the IP     |
| `RECAPTCHA_MISTRAL_MODEL`  | `mistral-medium-latest`| Vision model for image-solve                     |

## Python API

```python
import asyncio
from recaptcha import (solve_recaptcha_v3, solve_recaptcha_invisible,
                       solve_recaptcha_v2, solve_recaptcha_v2_realpage)

asyncio.run(solve_recaptcha_v3(sitekey="6Lc...", url="https://target.com", action="submit"))
asyncio.run(solve_recaptcha_v2(sitekey="6Lf...", url="https://target.com/form"))
```

## Files

| File             | Description                                        |
| ---------------- | -------------------------------------------------- |
| `solve.py`       | All modes: v3/invisible (execute), v2 (checkbox+image), v2 real-page |
| `image_solve.py` | Image-challenge solver (screenshot → tiles → vision → click → verify) |
| `template.html`  | v2 widget page (`.g-recaptcha` + api.js / enterprise.js) |
| `__init__.py`    | Package exports                                    |

The Mistral `KeyPool` and the shared `apikey.txt` now live in `../common/` (shared with
the hCaptcha solver), as do the selector / pre-action / post_fetch browser helpers
(`../common/browser.py`).

## Dependencies

`cloakbrowser` (anti-detect Playwright) + `pillow` (image slicing) — all in the project
venv at `/opt/captcha-solver/venv/`. (Whisper/ffmpeg are no longer needed;
the audio path was removed.)

## Running

Runs as a systemd service (`captcha-solver.service`, enabled & reboot-safe) —
`server.py` runs under `xvfb-run`, so reCAPTCHA's default headed mode
(`RECAPTCHA_HEADLESS=0`) has a virtual display:

```bash
sudo systemctl restart captcha-solver.service   # picks up code changes
sudo journalctl -u captcha-solver.service -f
```

For ad-hoc/dev runs there is `run.sh` (venv launcher on `:8877`); on a headless
box wrap it: `xvfb-run ./run.sh`.

## Remote access

Reachable at `https://solver.example.com` (Cloudflare Tunnel → Caddy `:<caddy-port>`).
All paths except `/health` require a static Bearer token (see parent
`../README.md`):

```bash
TOKEN=$(cut -d= -f2 ~/scripts/captcha-solver/.solver-token.env)
curl -X POST https://solver.example.com/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha","version":"v3","enterprise":true,"sitekey":"6Lc...","url":"https://example.com","action":"login"}'
```

# hCaptcha Solver — checkbox, invisible, real-page

Local hCaptcha solver via CloakBrowser, following the same patterns as the Turnstile
and reCAPTCHA solvers (route-intercept, headed-under-Xvfb, same-session post_fetch).
hCaptcha API is compatible with reCAPTCHA (`hcaptcha.execute()` ≈ `grecaptcha.execute()`).

## Solving modes

### 1. Checkbox — route-intercept (`/solve` with `type: "hcaptcha"`)

Renders `.h-captcha` on an intercepted page, clicks the checkbox inside the iframe,
harvests the token. When a challenge opens (image grid), solves it via Mistral vision.
Route-intercept matches the url via `route_glob` — a bare-domain url (`https://ex.com`)
is intercepted as `https://ex.com/**` so `goto`'s trailing-slash request is caught (a
bare domain was previously a silent miss → hang); urls with a path already worked.

**Image-grid challenges are solvable** — the canvas is sliced into 4×4 tiles, each
classified by Mistral, matching tiles clicked via real mouse events. Multi-page
challenges are handled (Next → Verify).

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"hcaptcha","sitekey":"345e6d03-eb0c-4911-a63c-05a819bfdc09","url":"https://7y7j.github.io/"}'
```

### 2. Invisible (`/solve`)

`hcaptcha.execute()` via explicit rendering with `size: 'invisible'`. Zero interaction.
Use for sitekeys configured as invisible/passive. **Requires `action:"invisible"`** —
that literal is what selects the invisible-execute path; without it the request falls
through to the checkbox solver.

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{"type":"hcaptcha","action":"invisible","sitekey":"10000000-ffff-ffff-ffff-000000000001","url":"https://7y7j.github.io/"}'
```

### 3. Real page (`/solve`)

Navigates the actual site, runs pre-actions, clicks the checkbox, solves any challenge,
optionally runs post-fetch API calls from the same browser session. The widget is
injected with the sitekey handed to the browser as an `evaluate()` argument (never
interpolated into the page script), so a sitekey containing special characters can't
break out of the markup.

## Official test key (always passes)

| Parameter   | Value                                    |
| ----------- | ---------------------------------------- |
| Sitekey     | `10000000-ffff-ffff-ffff-000000000001`   |
| Secret      | `0x0000000000000000000000000000000000000000` |
| Token       | `10000000-aaaa-bbbb-cccc-000000000001`   |

This keypair **never shows a challenge** and always returns `success: true`. Verified
working — token obtained in ~19s.

## 7y7j.github.io — all four sitekeys always challenge

All four sitekeys on https://7y7j.github.io/ are configured in **"Always Challenge"**
mode, matching the page's own description ("全部通关后，刷新页面以获取新题目").

This is **not a solver bug**. hCaptcha returns two challenge types:

### Image-grid (solvable ✅ — numbered-grid classification)

Tasks like "Select ALL animal icons according to the counts shown" or
"Click the flower the bee never lands on". The solver overlays a **numbered
4×4 grid** on the full canvas image and asks the vision model **which cell
number(s)** satisfy the task. This turns a hard grounding problem into an
easy classification problem — ONE Mistral call per page instead of 16,
and handles reasoning challenges (not just tile-level yes/no).

**Flow:** checkbox click → challenge opens → numbered-grid overlay → Mistral
classify → click matching cells via `page.mouse.click` → Next → Verify → token.

**Drag challenges** (e.g. "Please drag the correct block to help the creature
pass") also use the numbered grid: Mistral picks two cells (source → target),
then CloakBrowser executes a programmatic drag (`page.mouse.move/down/up`)
across ~10 smooth steps. This is best-effort — hCaptcha drag adversarial
may still fail.

| Challenge type | Solvable | Method |
|---|---|---|
| Image-grid 'select all' | ✅ | Grid→classify→click |
| Reasoning single-scene | ✅ | Grid→classify→click |
| Drag block | ✅ Best-effort | Grid→two cells→programmatic drag |

### Why the constant challenges?

hCaptcha always triggers a challenge because:
1. **Untrusted session** — automated browser detected by hCaptcha risk engine
2. **Route-intercept** — token generated in a fake page context; no real
   browsing history
3. **hCaptcha anti-bot is more aggressive than Turnstile** — far easier to
   trigger a challenge compared to Cloudflare Turnstile

### Avoiding challenges in production

- **Residential proxy** (body `proxy`) + session warming (real cookies)
- **Headed** mode under Xvfb (`BROWSER_HEADLESS=0`)
- **Invisible sitekey** (token via `hcaptcha.execute()`, zero interaction)
- **Real page solver** with natural pre_actions

## Verified working scenarios

| Skenario | Status |
| -------- | ------ |
| Official test key (no challenge) | ✅ 19.9s |
| Checkbox route-intercept (no challenge) | ✅ Token obtained |
| Invisible execute (invisible sitekey) | ✅ Programmatic |
| Image-grid challenge (vision solve) | ✅ Vision classify + click |
| Drag challenge | ❌ Skip → retry |
| Real page + pre_actions + post_fetch | ✅ Implemented |

## Endpoints

| Endpoint | Method | Description |
| -------- | ------ | ----------- |
| `/solve` | POST | `type: "hcaptcha"` + sitekey + url |
| `/solve` | POST | sitekey + url |
| `/solve` | POST | url + optional sitekey/pre_actions/post_fetch |

## Response

Every `/solve` 200 carries a uniform top-level `"solved": true|false` — callers read
`solved` and do not branch per-type. hCaptcha additionally returns `token` (plus
`success`, `elapsed`, etc. as applicable).

**Error contract:** 2xx → read `solved`; non-2xx → read `detail`. Never both. A solve
that ran but failed → `200 {solved:false, error}`; a request that never solved →
`{detail}` at 400 (bad/unsupported type, missing url/sitekey, SSRF-blocked host),
408 (exceeded `timeout_s`), 422 (body schema invalid — `detail` is a list), 500
(solver crash).

## Environment

| Variable             | Default | Description                                     |
| -------------------- | ------- | ----------------------------------------------- |
| `BROWSER_HEADLESS`   | `0`     | Global. `0` = headed (needs Xvfb) |
| `HCAPTCHA_GEOIP`     | —       | `1` spoof tz/locale/WebGL |
| `HCAPTCHA_MISTRAL_MODEL` | `mistral-medium-latest` | Vision model |

Proxy is per-request only: pass `"proxy"` on `POST /solve`. No env fallback.

## Files

| File            | Description                                        |
| --------------- | -------------------------------------------------- |
| `solve.py`      | Core solver — checkbox, invisible, real-page       |
| `template.html` | Widget template with hCaptcha API script            |
| `image_solve.py`| Canvas → 4×4 tiles → Mistral vision → click → verify |
| `__init__.py`   | Package exports                                    |
| `README.md`     | This file                                          |

The Mistral `KeyPool` + shared `apikey.txt` and the browser helpers
(selector / pre-actions / post_fetch) now live in `../common/`
(`common/mistral.py`, `common/apikey.txt`, `common/browser.py`), shared with the
reCAPTCHA solver.

## Running

Runs as a systemd service (`captcha-solver.service`, enabled & reboot-safe) —
`server.py` runs under `xvfb-run`, so headed mode
(`BROWSER_HEADLESS=0`) has a virtual display:

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
  -d '{"type":"hcaptcha","sitekey":"10000000-ffff-...","url":"https://example.com"}'
```

# Turnstile Solver

Generic Cloudflare Turnstile solver that works with **any sitekey**. Three solving modes for different scenarios.

## Solving Modes

### 1. Route Intercept (`/solve`)

Fastest. Intercepts the target URL via Playwright's route API, serves a local HTML page with the Turnstile widget, and extracts the token. The target URL is matched with a `/**` glob (`route_glob`), so a bare-domain URL (`https://ex.com`) still catches `goto`'s trailing-slash request (`https://ex.com/`).

**Best for:** Sites that render Turnstile directly on page load.

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "turnstile",
    "sitekey": "0x4AAAAAA...",
    "url": "https://example.com/login"
  }'
```

**Note:** Some sites reject route-intercept tokens (`invalid-input-response`) because the token is generated on a fake page context. Use `/solve` with `real_page: true` for those.

### 2. Route Intercept + Verify (`/solve`)

Solves via route intercept, then submits the verify call from the **same browser session**. Keeps origin/cookies intact.

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "turnstile",
    "sitekey": "0x4AAAAAA...",
    "verify_url": "https://example.com/api/verify-captcha",
    "verify_payload": {},
    "page_url": "https://example.com/login"
  }'
```

Use `page_url` to set the page to intercept (defaults to `verify_url` if omitted). Use `__TOKEN__` placeholder in `verify_payload` to inject the solved token automatically.

### 3. Real Page (`/solve`)

Navigates the **actual site**, executes pre-actions, clicks the Turnstile checkbox, and optionally runs post-fetch API calls from the same browser session. The widget is injected with the sitekey handed to the browser as an `evaluate()` argument (never interpolated into the page script), so a sitekey containing special characters can't break out of the markup.

**Best for:** Sites that reject route-intercept tokens, or where Turnstile appears after user interaction.

```bash
curl -X POST http://localhost:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://app.kilo.ai/users/sign_in",
    "timeout_s": 60,
    "pre_actions": [
      {"type": "click", "selector": "text=Continue with Email"},
      {"type": "wait", "value": "2"},
      {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"},
      {"type": "click", "selector": "text=Continue"}
    ],
    "post_fetch": [
      {
        "url": "https://app.kilo.ai/api/auth/verify-turnstile",
        "method": "POST",
        "body": {"token": "__TOKEN__"}
      },
      {
        "url": "https://app.kilo.ai/api/auth/magic-link",
        "method": "POST",
        "body": {"email": "user@example.com", "callbackUrl": "/users/after-sign-in"}
      }
    ]
  }'
```

## Pre-Actions

Steps executed **before** the Turnstile widget appears. Each action has:


| Field      | Required | Description                                                   |
| ---------- | -------- | ------------------------------------------------------------- |
| `type`     | Yes      | `click`, `fill`, `wait`, `select`, `press`                    |
| `selector` | Depends  | Element selector (not needed for `wait`, `press`)             |
| `value`    | Depends  | Text to fill, option to select, key to press, seconds to wait |
| `timeout`  | No       | Element wait timeout in ms (default: 10000)                   |


### Selector Formats


| Format | Example                      | Detection            |
| ------ | ---------------------------- | -------------------- |
| CSS    | `input[type=email]`          | Default              |
| XPath  | `//button[@type='submit']`   | Starts with `//`     |
| Text   | `text=Continue with Email`   | Starts with `text=`  |
| Regex  | `regex=Continue.*Email`      | Starts with `regex=` |
| Role   | `role=button[name='Submit']` | Starts with `role=`  |


### Action Types


| Type     | selector | value                     | Description            |
| -------- | -------- | ------------------------- | ---------------------- |
| `click`  | Required | —                         | Click an element       |
| `fill`   | Required | Text to type              | Fill an input field    |
| `select` | Required | Option value              | Select dropdown option |
| `wait`   | —        | Seconds (e.g. `"2"`)      | Wait for N seconds     |
| `press`  | —        | Key name (e.g. `"Enter"`) | Press a keyboard key   |


## Post-Fetch

API calls executed from the **same browser session** after solving. Use `__TOKEN__` placeholder in `body` to inject the solved Turnstile token.

Each post-fetch entry:


| Field    | Required | Description                                     |
| -------- | -------- | ----------------------------------------------- |
| `url`    | Yes      | API endpoint URL                                |
| `method` | No       | HTTP method (default: `POST`)                   |
| `body`   | No       | JSON body (use `__TOKEN__` for token injection) |


## Response

All endpoints return:

```json
{
  "solved": true,
  "token": "1.0eWQ2WW8abXvc...",
  "expires_in": 300,
  "method": "route|real-page",
  "elapsed": 22.8,
  "verify_success": true
}
```

`solved` is the uniform success signal across every solver type — read it, not the per-type fields. (Other types carry their own detail: `cf_clearance` for cloudflare, `aws_waf_token` for awswaf, `score` for reCAPTCHA v3, etc.)

### Error contract

Two rules, uniform across types:

- A solve that **ran but didn't succeed** → HTTP `200` with `{"solved": false, "error": "..."}`. A turnstile run that never mints a token now returns this (previously could surface as a timeout).
- A request that **never solved** → `4xx`/`5xx` with FastAPI's `{"detail": ...}` envelope: `400` (bad/unsupported type, missing `url`/`sitekey`, SSRF-blocked host), `408` (exceeded `timeout_s`), `422` (invalid body schema — `detail` is a list), `500` (solver crash, e.g. browser launch).

Rule of thumb: **2xx → read `solved`; non-2xx → read `detail`. Never both.**

`realpage` with `post_fetch` also returns:

```json
{
  "post_fetch": [
    {"url": "https://...", "status": 200, "body": "..."},
    {"url": "https://...", "status": 200, "body": "..."}
  ]
}
```

## Environment Variables


| Variable             | Default | Description                                     |
| -------------------- | ------- | ----------------------------------------------- |
| `PORT`               | `8877`  | HTTP server port                                |
| `TURNSTILE_HEADLESS` | `1`     | Set to `0` for non-headless (needs Xvfb)        |
| `TURNSTILE_PROXY`    | —       | Residential proxy URL                           |
| `TURNSTILE_GEOIP`    | —       | Set to `1` to spoof tz/locale/WebGL to match IP |


## Python API

```python
import asyncio
from turnstile.solve import solve_turnstile, solve_and_verify, solve_turnstile_realpage

# Route intercept
result = await solve_turnstile(sitekey="0x4AAAAAA...", url="https://example.com/login")

# Route intercept + verify
result = await solve_and_verify(
    sitekey="0x4AAAAAA...",
    verify_url="https://example.com/api/verify",
    verify_payload={},
    page_url="https://example.com/login",
)

# Real page with pre_actions and post_fetch
result = await solve_turnstile_realpage(
    url="https://example.com/login",
    pre_actions=[
        {"type": "click", "selector": "text=Continue with Email"},
        {"type": "fill", "selector": "input[type=email]", "value": "user@example.com"},
        {"type": "click", "selector": "text=Continue"},
    ],
    post_fetch=[
        {"url": "https://example.com/api/verify", "method": "POST", "body": {"token": "__TOKEN__"}},
    ],
)
```

## Files


| File            | Description                                     |
| --------------- | ----------------------------------------------- |
| `solve.py`      | Core solver — all three solving modes           |
| `template.html` | Minimal HTML template with Turnstile API script |
| `__init__.py`   | Package init                                    |


## Dependencies

- `cloakbrowser` — Playwright wrapper with anti-detection
- `fastapi`, `uvicorn`, `pydantic` — HTTP API server

All available in the project venv at `/opt/captcha-solver/venv/`.

## Running

Runs as a systemd service (`captcha-solver.service`, enabled & reboot-safe) —
the whole `server.py` runs under `xvfb-run` with `TURNSTILE_HEADLESS=0`:

```bash
sudo systemctl restart captcha-solver.service   # picks up code changes
sudo journalctl -u captcha-solver.service -f
```

For ad-hoc/dev runs there is `run.sh` (venv launcher on `:8877`); wrap it in
`xvfb-run` for a headful browser.

## Remote access

Reachable at `https://solver.example.com` (Cloudflare Tunnel → Caddy `:<caddy-port>`).
All paths except `/health` require a static Bearer token (see the parent
`../README.md`):

```bash
TOKEN=$(cut -d= -f2 ~/scripts/captcha-solver/.solver-token.env)
curl -X POST https://solver.example.com/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://example.com/login"}'
```


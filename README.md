# Captcha Solver

Local captcha-solving HTTP sidecar built on the `cloakbrowser` Python library
(self-hosted, anti-detect Chromium). Solves CAPTCHA challenges natively by
running them in a real browser engine — no per-solve cost to external providers
for the browser-based paths.

Supported types (5): **Turnstile**, **reCAPTCHA** (v2 / v3 / invisible, incl.
Enterprise), **hCaptcha** (checkbox / invisible / real-page), **Cloudflare**
(`cf_clearance`), **AWS WAF** (`aws-waf-token`, silent JS challenge).

## Architecture

```
client ──HTTP──> server.py (FastAPI, :8877)
                     │ dispatch by `type`
                     ├── turnstile/solve.py    (CloakBrowser, headless)
                     ├── recaptcha/solve.py     (CloakBrowser, headed via Xvfb)
                     ├── hcaptcha/solve.py      (CloakBrowser)
                     ├── cloudflare/solve.py    (CloakBrowser — cf_clearance harvester)
                     └── awswaf/solve.py        (CloakBrowser — aws-waf-token harvester)
```

Each sub-solver launches CloakBrowser via `cloakbrowser.launch_async()`,
drives the challenge widget, and returns the solved token.

## Endpoints

| Method | Path       | Auth        | Description                                  |
| ------ | ---------- | ----------- | -------------------------------------------- |
| GET    | `/health`  | public      | Liveness + supported types (5)               |
| GET    | `/status`  | token       | Service status + list of currently running tasks |
| GET    | `/logs`    | token       | Last N solve events (buffer holds up to 100; `lines` caps at 200 but returns only what's buffered; `total` is the full buffer size) |
| POST   | `/solve`   | token       | Solve a captcha (dispatch by `type`)         |
| GET    | `/docs`    | public      | **Swagger UI** — interactive API docs        |
| GET    | `/redoc`   | public      | **ReDoc** — reference API docs               |
| GET    | `/openapi.json` | public | Raw OpenAPI 3 schema                          |

`/health` and the docs paths (`/docs`, `/redoc`, `/openapi.json`) are public.
Everything else — including `/solve`, `/status`, `/logs` — requires a Bearer
token **when accessed through the public domain** (Caddy allow-lists the docs
paths, see "Remote access" below). On localhost the service itself enforces no auth.

### Interactive docs (Swagger)

FastAPI auto-generates OpenAPI docs from the typed models — no separate spec to
maintain. Open in a browser (no token needed):

- **Swagger UI** — <https://solver.example.com/docs> (or `http://127.0.0.1:8877/docs` on-box).
  Every field is described; the `POST /solve` body has a **dropdown of ready-to-run
  examples** (Turnstile, reCAPTCHA v2/v3, hCaptcha, real-page) for "Try it out"; and
  `400/408/500` responses are documented. Examples use placeholder sitekeys only.
  A **servers dropdown** switches the base URL between Public (`https://solver.example.com`)
  and Local (`http://127.0.0.1:8877`), and an **Authorize** button accepts the Bearer token
  and forwards it on "Try it out" (real enforcement still lives at the Caddy layer).
- **ReDoc** — <https://solver.example.com/redoc> — a clean reference layout.

> Note the path is `/redoc` (no trailing "s"). The docs are exposed publicly by an
> allow-list in the Caddy vhost; `/solve` and the monitoring endpoints stay token-gated.

## Running

Runs as a **systemd system service** (`captcha-solver.service`, enabled, reboot-safe):

```bash
sudo systemctl status captcha-solver.service     # active (running)
sudo systemctl restart captcha-solver.service     # picks up code changes
sudo systemctl stop captcha-solver.service
sudo journalctl -u captcha-solver.service -f       # live logs
```

The unit (`/etc/systemd/system/captcha-solver.service`) runs the server headful
under a virtual display so the interactive Turnstile/reCAPTCHA paths work on a
headless box:

```ini
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1920x1080x24" \
    /opt/captcha-solver/venv/bin/python3 server.py
Environment=PORT=8877
Environment=TURNSTILE_HEADLESS=0
Restart=always
```

For ad-hoc/dev runs without systemd there is also `run.sh` (sources the venv,
execs `server.py` on `:8877`); wrap it in `xvfb-run` if you need a headful
browser.

### Browser display modes

- Under the service, the whole process runs inside `xvfb-run`, so every solver
  has a virtual display available.
- **Turnstile**: the service sets `TURNSTILE_HEADLESS=0` (headful) — needed for
  the interactive checkbox path. Standalone it defaults to headless
  (`TURNSTILE_HEADLESS=1`).
- **reCAPTCHA** runs **headed** by default (`RECAPTCHA_HEADLESS=0`) because
  headless is more aggressively detected. Run it under a virtual display
  (e.g. `xvfb-run ./run.sh`) on a headless server, or set
  `RECAPTCHA_HEADLESS=1` to force headless (lower success rate).

### Environment variables

| Variable                | Default | Effect                                      |
| ----------------------- | ------- | ------------------------------------------- |
| `PORT`                  | `8877`  | Listen port                                 |
| `TURNSTILE_HEADLESS`    | `1`     | `0` = run Turnstile headed                  |
| `TURNSTILE_PROXY`       | unset   | Proxy URL for Turnstile browser             |
| `TURNSTILE_GEOIP`       | unset   | `1` = align browser timezone/locale/WebGL to the proxy exit IP (shared by Turnstile + cloudflare + awswaf) |
| `RECAPTCHA_HEADLESS`    | `0`     | `1` = run reCAPTCHA headless                |
| `RECAPTCHA_PROXY`       | unset   | Proxy URL for reCAPTCHA browser             |
| `RECAPTCHA_GEOIP`       | unset   | `1` = same geo alignment for the reCAPTCHA browser |
| `SOLVER_ALLOW_PRIVATE`  | unset   | `1` = allow `url`/`verify_url`/`post_fetch` targets on private/loopback/link-local hosts (SSRF guard off). Leave unset in prod. |
| `SOLVER_PUBLIC_URL`     | (placeholder) | Public base URL shown in the OpenAPI docs (servers dropdown + contact). Set to your real domain at runtime. |

### SSRF guard

`/solve` navigates and fetches caller-supplied URLs (`url`, `verify_url`, `page_url`,
`post_fetch[].url`) from the browser's own session. By default the server rejects
(`400`) any of these that resolve to a **private, loopback, link-local, reserved,
multicast, or unspecified** address, and any non-`http(s)` scheme. Set `SOLVER_ALLOW_PRIVATE=1` only
when you deliberately need to hit an internal target.

## Request format (`POST /solve`)

```jsonc
{
  "type": "turnstile",          // turnstile | recaptcha | hcaptcha | cloudflare | awswaf  (required)
  "sitekey": "0x4AAA...",        // site key (widget types only — cloudflare/awswaf are
                                 //   page-level and need NO sitekey)
  "url": "https://target.com",   // page the captcha is on            (required)

  // optional, all types
  "action": "submit",            // turnstile/reCAPTCHA action
  "cdata": "...",                // turnstile customer data bound to token
  "real_page": false,            // solve on the live target page, not a stub
  "timeout_s": 60,
  "proxy": "http://user:pass@ip:port",  // cloudflare/awswaf only (per-request); turnstile/recaptcha use the TURNSTILE_PROXY / RECAPTCHA_PROXY env vars
  "pre_actions": [               // run before solving (real_page mode)
    { "type": "click", "selector": "#start", "timeout": 10000 }
  ],
  "post_fetch": [                // fire requests after solving (real_page mode)
    { "url": "https://target.com/verify", "method": "POST", "body": {} }
  ],

  // reCAPTCHA only
  "version": "v2",               // v2 | v3 | invisible
  "secret": "...",               // target's secret key (v3 score check)
  "enterprise": false,

  // turnstile solve-and-verify
  "verify_url": "https://target.com/verify",
  "verify_payload": { "...": "..." },
  "page_url": "https://target.com"
}
```

> **Route interception & trailing slashes.** For the page-level paths the solver
> intercepts the target `url` via a `/**` glob (`route_glob`), so a bare-domain
> `url` like `https://ex.com` is matched as `https://ex.com/**` and the navigation's
> trailing-slash request is caught (a bare domain used to be a silent miss → hang).
> URLs that already carry a path were unaffected. Transparent to callers — no API change.

### Response contract (uniform across all types)

Two rules cover every response — **2xx → read `solved`; non-2xx → read `detail`.
Never both.**

1. **The solve ran.** → HTTP **200** with a top-level `"solved": true|false`. Callers
   check `solved` and do **not** branch per-type. Per-type detail rides alongside:
   `token` (turnstile / recaptcha / hcaptcha), `cf_clearance` (cloudflare — **no**
   `token` field), `aws_waf_token` + `token` (awswaf), and
   `score` / `expires_in` / `cookies` / `user_agent` / `verify_success` / `method` /
   `elapsed` where applicable. A solve that ran but failed is still **200** with
   `solved:false` + `error` (e.g. Turnstile that mints no token — it no longer
   raises/confuses with 408).

   ```jsonc
   { "type": "turnstile", "solved": true, "token": "<solved-token>", "elapsed": 4.1, "method": "…" }
   ```

2. **The request never solved.** → **4xx/5xx** with FastAPI's `{ "detail": … }`
   envelope (no `solved` field):

   | Code  | When                                                              |
   | ----- | ----------------------------------------------------------------- |
   | `400` | bad/unsupported `type`, missing `url`/`sitekey`, SSRF-blocked host |
   | `408` | exceeded `timeout_s`                                               |
   | `422` | request body failed schema validation (`detail` is a list)        |
   | `500` | solver crashed (e.g. browser launch failure)                      |

## Examples

Local (no token needed):

```bash
# Health
curl http://127.0.0.1:8877/health

# Turnstile
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://target.com"}'

# reCAPTCHA Enterprise v3
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"recaptcha","version":"v3","enterprise":true,"sitekey":"6Lc...","url":"https://target.com","action":"login"}'

# AWS WAF (silent challenge — no sitekey; pass a proxy for replay)
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"awswaf","url":"https://protected.example.com","proxy":"http://user:pass@ip:port"}'
```

## Remote access (public domain)

Exposed at **`https://solver.example.com`** via Cloudflare Tunnel
(`<tunnel-id>`) → Caddy vhost `:<caddy-port>` → this service on `:8877`.

Because the solver has **no built-in auth** and a public solve would burn
CloakBrowser resources for anyone, the Caddy vhost enforces a **static Bearer
token** on every path except `/health`:

```bash
# token lives in ~/scripts/captcha-solver/.solver-token.env  (chmod 600)
TOKEN=$(cut -d= -f2 ~/scripts/captcha-solver/.solver-token.env)

# health — public, no token
curl https://solver.example.com/health

# solve — token required
curl -X POST https://solver.example.com/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://target.com"}'
```

Requests to protected paths without a valid token get `403 Forbidden`.

## Cloudflare clearance (`cf_clearance`)

`POST /solve` with `type: "cloudflare"` passes the **full-page Cloudflare
interstitial** (both **Managed Challenge** — with a Turnstile checkbox — and the
passive **JS challenge**, "Checking your browser…") and returns the `cf_clearance`
cookie plus everything needed to replay it.

> **Why not just solve the Turnstile?** A Managed Challenge's checkbox can't be
> beaten via the stub-page path — a token minted in a stub context is rejected
> (`code 1201`). Harvest `cf_clearance` on the real page instead (that's this endpoint).

```bash
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"cloudflare","url":"https://protected.example.com",
       "proxy":"http://user:pass@ip:port","timeout_s":60}'
```

Response (extras on top of the usual envelope):

```jsonc
{
  "type": "cloudflare",
  "success": true,
  "cf_clearance": { "name": "cf_clearance", "value": "…", "domain": ".example.com", "expires": … },
  "cookies": [ /* full jar — build a Cookie header */ ],
  "user_agent": "Mozilla/5.0 …",           // MUST be replayed verbatim
  "headers": { "User-Agent": "…", "Accept-Language": "…" },
  "proxy": "http://…",
  "warning": "cf_clearance is bound to IP + JA3/TLS + User-Agent…"
}
```

### Replay contract — read this

`cf_clearance` is **bound to four things at once**: the **exit IP**, the **JA3/TLS
fingerprint**, the **User-Agent**, and the specific challenge. To reuse it:

- Replay from the **same proxy IP** you solved on → pass `proxy` (or set
  `TURNSTILE_PROXY`; the cloudflare path shares Turnstile's env). A cookie solved on
  the server's own IP only works from that IP.
- Send the **exact `user_agent`** returned, and a matching `Accept-Language`.
- Use a client whose **TLS fingerprint matches** (curl-impersonate or another
  CloakBrowser). Plain `requests` / `httpx` / `curl` get **re-challenged** even with
  the right IP + UA, because their JA3 differs.

### Limitations (be realistic)

- **Datacenter IPs are scored harshly.** Managed / "Under Attack" mode may never let a
  raw VPS/datacenter IP through — the checkbox stays unsolved. A **residential/mobile
  proxy** is usually required. On failure the solver returns `success:false` + `error`
  (and 408 if it exceeds `timeout_s`), it does not hang.
- **Short TTL.** `cf_clearance` typically lives ~15–30 min (site-configurable) — treat
  it as ephemeral and re-solve on expiry.
- **Needs a headful browser.** Set `TURNSTILE_HEADLESS=0` (run under Xvfb — the systemd
  unit already does) so the Managed-Challenge checkbox click works. Pair with
  `TURNSTILE_GEOIP=1` when proxying, so timezone/locale/WebGL align to the exit IP.

## AWS WAF (`aws-waf-token`)

`POST /solve` with `type: "awswaf"` navigates the **real target URL**, lets the
**silent AWS WAF JS challenge** run to completion (it sets an `aws-waf-token`
cookie with no visible widget), polls the cookie jar, and returns the token plus
everything needed to replay it. **No sitekey needed** — it is page-level.

> **Silent challenge only.** This path passes the background JS challenge. It does
> **not** solve the interactive AWS WAF *visual* puzzle.

```bash
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"awswaf","url":"https://protected.example.com",
       "proxy":"http://user:pass@ip:port","timeout_s":60}'
```

Response (extras on top of the usual envelope):

```jsonc
{
  "type": "awswaf",
  "solved": true,
  "token": "…",                              // the aws-waf-token cookie value
  "aws_waf_token": { "name": "aws-waf-token", "value": "…", "domain": "…", "expires": … },
  "success": true,
  "cookies": [ /* full jar — build a Cookie header */ ],
  "user_agent": "Mozilla/5.0 …",             // MUST be replayed verbatim
  "headers": { "User-Agent": "…", "Accept-Language": "…" },
  "proxy": "http://…",
  "warning": "aws-waf-token is bound to IP + JA3/TLS + User-Agent…"
}
```

Like `cf_clearance`, the token is **replay-bound to the exit IP + JA3/TLS
fingerprint + User-Agent** — pass `proxy` and replay from the same IP with the
returned `user_agent` (see the Cloudflare "Replay contract" above; it applies
verbatim). If AWS returns a **CloudFront block** page, the solver detects it early
(title check) and retries once through the proxy before giving up.

## Files

```
captcha-solver/
├── server.py              # FastAPI dispatcher (:8877), SSRF guard, global timeout
├── run.sh                 # venv launcher
├── requirements.txt       # declarative dep manifest (already in the project venv)
├── .solver-token.env      # Bearer token for remote access (chmod 600, gitignored)
├── common/
│   ├── mistral.py         # shared Mistral vision KeyPool (round-robin + failover)
│   ├── browser.py         # shared helpers: selector/pre_actions/browser_kwargs/post_fetch
│   └── apikey.txt         # single Mistral key pool, one per line (chmod 600, gitignored)
├── turnstile/solve.py     # Turnstile solver (CloakBrowser, headless)
├── recaptcha/solve.py     # reCAPTCHA v2/v3/invisible (CloakBrowser, headed)
├── recaptcha/image_solve.py
├── hcaptcha/solve.py      # hCaptcha solver
├── hcaptcha/image_solve.py
├── cloudflare/solve.py    # cf_clearance (full-page Managed / JS challenge) harvester
├── cloudflare/_selfcheck.py
└── awswaf/                # aws-waf-token (silent JS challenge) harvester
    ├── solve.py
    ├── _selfcheck.py
    └── __init__.py
```

> The per-package `mistral.py` and `apikey.txt` were consolidated into `common/`
> (they had diverged; the key files were byte-identical). The legacy Whisper-based
> audio-challenge fallback was removed — it was dead code and reliably IP-blocked.

# Captcha Solver

Local captcha-solving HTTP sidecar built on the `cloakbrowser` Python library
(self-hosted, anti-detect Chromium). Solves CAPTCHA challenges natively by
running them in a real browser engine — no per-solve cost to external providers
for the browser-based paths.

**Scope:** a solver SOLVES the captcha + harvests a replayable token. Account
creation and form recon (CSRF/honeypot scraping, filling account fields) are the
**caller's** job, not this service.

Supported types (11): **Turnstile**, **reCAPTCHA** (v2 / v3 / invisible, incl.
Enterprise), **hCaptcha** (checkbox / invisible / real-page), **Cloudflare**
(`cf_clearance`), **AWS WAF** (`aws-waf-token`, silent JS challenge), **BotGuard**
(Google OAuth `bgRequest` token), **DataDome** (`datadome` clearance cookie —
caller passes the DataDome-fronted url + referer, e.g. GitHub's octocaptcha signup
gate), **PerimeterX** (HUMAN "Press & Hold" → `_px3` cookie via a real CDP press-hold
gesture that drives the SHA-256 hashcash PoW Worker + biomechanics), **Akamai**
(Bot Manager `_abck` clearance cookie via the `bmak` telemetry sensor), **Aliyun**
(Captcha 2.0 slide-puzzle → `{certifyId, deviceToken, data}` token via cv2 gap
detection + a quadratic handle→piece drag; params `scene_id` + `prefix`, no sitekey),
**Arkose** (FunCaptcha visual puzzle → `fc_token` via ONNX image classification;
CloakBrowser intercepts `gfct` challenge, predicts answer per wave, encrypts with
CryptoJS AES-CBC format, multi-wave pipeline up to 10 rounds).

## Architecture

```
client ──HTTP──> server.py (FastAPI, :8877)
                     │ dispatch by `type`
                     ├── turnstile/solve.py     (CloakBrowser, headless)
                     ├── recaptcha/solve.py      (CloakBrowser, headed via Xvfb)
                     ├── hcaptcha/solve.py       (CloakBrowser)
                     ├── cloudflare/solve.py     (CloakBrowser — cf_clearance harvester)
                     ├── awswaf/solve.py         (CloakBrowser — aws-waf-token harvester)
                     ├── botguard/solve.py       (CloakBrowser — Google OAuth bgRequest token)
                     ├── datadome/solve.py       (CloakBrowser — datadome cookie; caller passes url+referer)
                     ├── perimeterx/solve.py     (CloakBrowser — _px3 via CDP press-hold; renderers/ trigger the gate)
                     ├── akamai/solve.py         (CloakBrowser — _abck via bmak telemetry sensor)
                     ├── aliyun/solve.py         (CloakBrowser — slide-puzzle; cv2 gap detect + quadratic drag)
                     └── arkose/solve.py         (CloakBrowser + ONNX — FunCaptcha visual puzzle; gfct intercept → predict → AES encrypt → multi-wave /fc/ca/)
```

Each sub-solver launches CloakBrowser via `cloakbrowser.launch_async()`,
drives/harvests the challenge, and returns a replayable token. Every solver is
**harvest-only** — it never creates accounts or fills account fields.

## Endpoints

| Method | Path       | Auth        | Description                                  |
| ------ | ---------- | ----------- | -------------------------------------------- |
| GET    | `/health`  | public      | Liveness + supported types (11)              |
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
Environment=BROWSER_HEADLESS=0
Restart=always
```

For ad-hoc/dev runs without systemd there is also `run.sh` (sources the venv,
execs `server.py` on `:8877`); wrap it in `xvfb-run` if you need a headful
browser.

### Browser display modes

- Under the service, the whole process runs inside `xvfb-run`, so every solver
  has a virtual display available.
- **All browser solvers** share one global `BROWSER_HEADLESS` flag
  (`0` = headed, anything else = headless). Service sets `BROWSER_HEADLESS=0`.
- Code defaults when the env is unset: Turnstile family headless, interactive
  solvers (reCAPTCHA / hCaptcha / BotGuard) headed. Headless is more
  aggressively detected — prefer headed under Xvfb on servers.

### Environment variables

| Variable                | Default | Effect                                      |
| ----------------------- | ------- | ------------------------------------------- |
| `PORT`                  | `8877`  | Listen port                                 |
| `BROWSER_HEADLESS`      | per-solver | Global. `0` = headed for ALL browser solvers. Service sets `0`. |
| `TURNSTILE_GEOIP`       | unset   | `1` = align browser timezone/locale/WebGL to the proxy exit IP (shared by Turnstile + cloudflare + awswaf) |
| `RECAPTCHA_GEOIP`       | unset   | `1` = same geo alignment for the reCAPTCHA browser |
| `SOLVER_ALLOW_PRIVATE`  | unset   | `1` = allow `url`/`verify_url`/`post_fetch` targets on private/loopback/link-local hosts (SSRF guard off). Leave unset in prod. |
| `SOLVER_PUBLIC_URL`     | (placeholder) | Public base URL shown in the OpenAPI docs (servers dropdown + contact). Set to your real domain at runtime. |

Proxy is **per-request only** (`body.proxy`). No `*_PROXY` env vars.

### SSRF guard

`/solve` navigates and fetches caller-supplied URLs (`url`, `verify_url`, `page_url`,
`post_fetch[].url`) from the browser's own session. By default the server rejects
(`400`) any of these that resolve to a **private, loopback, link-local, reserved,
multicast, or unspecified** address, and any non-`http(s)` scheme. Set `SOLVER_ALLOW_PRIVATE=1` only
when you deliberately need to hit an internal target.

## Request format (`POST /solve`)

```jsonc
{
  "type": "turnstile",          // turnstile | recaptcha | hcaptcha | cloudflare | awswaf | botguard | datadome | perimeterx | akamai | aliyun  (required)
  "sitekey": "0x4AAA...",        // site key (widget types only — cloudflare/awswaf/botguard/
                                 //   datadome/perimeterx/akamai/aliyun are page-level and need NO sitekey)
  "url": "https://target.com",   // page the captcha is on            (required; NOT needed for aliyun)

  // optional, all types
  "action": "submit",            // turnstile/reCAPTCHA action
  "cdata": "...",                // turnstile customer data bound to token
  "real_page": false,            // solve on the live target page, not a stub
  "timeout_s": 60,
  "proxy": "http://user:pass@ip:port",  // all types; per-request only (no env fallback)
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
  "real_page": false,            // v2/v3/invisible: navigate live origin (needed for Enterprise invisible that 403s enterprise.js?render=<sitekey>)
  "classifier": "auto",          // tile classifier: auto|hybrid|yolo|mistral
                                 //   auto = ONNX hybrid if model present, else Mistral
                                 //   hybrid = ONNX-first + Mistral for unknown targets
                                 //   yolo = pure local ONNX (fails if model missing)
                                 //   mistral = pure Mistral vision (skip ONNX)
                                 //   used by v2 checkbox + invisible real_page bframe
                                 //   ignored for pure score-based v3

  // turnstile solve-and-verify
  "verify_url": "https://target.com/verify",
  "verify_payload": { "...": "..." },
  "page_url": "https://target.com",

  // datadome only
  "referer": "https://github.com/",   // framing referer so DataDome serves the real config

  // perimeterx only
  "render_flow": "outlook_signup",    // named trigger that makes the gate render

  // aliyun only (no sitekey, no url)
  "scene_id": "1r7eif79x",            // target site's captcha SceneId          (required)
  "prefix": "13lbkb5",                // captcha-open endpoint prefix           (required)
  "region": "sgp",                    // sgp (default) | cn | intl

  // arkose only
  "public_key": "A0DE7B75-...",       // Arkose site public key                 (required)
  "page_url": "https://login.site.com", // page that triggers Arkose           (required)
  "surl": "https://client-api.arkoselabs.com",  // service URL override        (optional)
  "max_waves": 10                     // max challenge waves before giving up   (optional, default 10)
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

# Arkose FunCaptcha (visual puzzle — pass public_key + page_url)
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"arkose","public_key":"A0DE7B75-1138-44F2-B132-ED188CEB66F3","page_url":"https://login.databricks.com/login","max_waves":10}'
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

- Replay from the **same proxy IP** you solved on → pass `proxy` on the solve request.
  A cookie solved on the server's own IP only works from that IP.
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
- **Needs a headful browser.** Set `BROWSER_HEADLESS=0` (run under Xvfb — the systemd
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

## BotGuard (Google OAuth `bgRequest` token)

`POST /solve` with `type: "botguard"` drives Google's OAuth sign-in in CloakBrowser
and extracts the `bgRequest` **BotGuard token** (the anti-automation token Google
attaches to the account-lookup / password RPCs). Page-level, self-URL (defaults to
the Google sign-in URL), no sitekey.

```bash
curl -X POST http://127.0.0.1:8877/solve -H "Content-Type: application/json" \
  -d '{"type":"botguard","email":"user@example.com","password":"optional-for-hard-gate-token"}'
```

- `email` drives the flow to the account-lookup RPC (`MI613e` soft-signal token).
- `password` (optional) drives to the password step for the `B4hajb` hard-gate token.
- The token is **session-bound** — replay from the harvested session cookies + UA.

## DataDome (`datadome` clearance cookie)

`POST /solve` with `type: "datadome"` loads a **caller-supplied** DataDome-fronted
`url` (the page/iframe that runs `tags.js`), lets the silent PoW payload post to
`api-js.datadome.co/js/`, and harvests the `datadome` cookie from the response. The
solver is **site-agnostic** — it hardcodes no site, ddk, or referer. See
`datadome/README.md` for the full contract.

```bash
# GitHub signup (octocaptcha broker, backend DataDome v5.8.0):
curl -X POST http://127.0.0.1:8877/solve -H "Content-Type: application/json" \
  -d '{"type":"datadome",
       "url":"https://octocaptcha.com/datadome?origin_page=github_signup_redesign",
       "referer":"https://github.com/",
       "proxy":"http://user:pass@ip:port"}'
```

- `url` (**required**): the DataDome-fronted page the caller wants cleared.
- `referer` (optional): framing Referer so DataDome scores the same config.
- Cookie is **IP + UA bound** — replay from the same proxy IP + returned `user_agent`.
- octocaptcha specifics (URL, referer) belong to the caller — the caller's
  auto-register script also scrapes GitHub's `timestamp_secret`/honeypot for the
  signup POST. That form recon is NOT this service's job.

## PerimeterX (HUMAN "Press & Hold" → `_px3`)

`POST /solve` with `type: "perimeterx"` reaches the HUMAN/PerimeterX press-hold gate,
actuates a **real CDP `mouseDown → hold → mouseUp`** (which lets the SHA-256 hashcash
PoW Web Worker finish + the sensor VM record biomechanics → PerimeterX mints `_px3`),
and harvests the cookie bundle. See `perimeterx/README.md` for the full contract.

```bash
curl -X POST http://127.0.0.1:8877/solve -H "Content-Type: application/json" \
  -d '{"type":"perimeterx","render_flow":"outlook_signup","proxy":"http://user:pass@ip:port"}'
```

- `render_flow` (optional, default `outlook_signup`): named site trigger from
  `perimeterx/renderers/` that surfaces the gate when it doesn't render on a plain
  load. For Outlook the gate only appears after a **throwaway** signup form walk
  (no standalone URL) — those typed values are disposable triggers, NOT account data;
  the solver never submits CreateAccount.
- `url` + `render_flow=null` for deployments whose gate renders on load.
- `_px3` is **bound to `_pxvid` + IP + UA** with a short TTL — replay the whole cookie
  bundle from the same proxy IP + returned `user_agent`, within TTL.
- The gate is **intermittent** (silent-pass on some sessions) — no gate → the solver
  honestly reports `solved:false` / `gate_reached:false`, never a fake success.

## Arkose FunCaptcha (visual puzzle → `fc_token`)

`POST /solve` with `type: "arkose"` solves Arkose Labs FunCaptcha visual puzzles.
CloakBrowser navigates the caller-supplied `page_url`, triggers the Arkose widget
using the site's `public_key`, intercepts the `gfct` challenge response, predicts
the answer via ONNX image classification, encrypts it in CryptoJS AES-CBC format,
and submits to `/fc/ca/` — repeating across multiple waves until solved.

```bash
curl -X POST http://127.0.0.1:8877/solve -H "Content-Type: application/json" \
  -d '{"type":"arkose",
       "public_key":"A0DE7B75-1138-44F2-B132-ED188CEB66F3",
       "page_url":"https://login.databricks.com/login",
       "max_waves":10}'
```

- `public_key` (**required**): Arkose site public key (e.g. Databricks `A0DE7B75-...`,
  Roblox `476068BF-...`).
- `page_url` (**required**): page that triggers the Arkose widget.
- `surl` (optional): Arkose service URL override (auto-detected from gfct domain).
- `max_waves` (optional, default 10): max challenge rounds before giving up.
- 24 pre-trained ONNX models cover common variants (conveyor, penguins, coordinates,
  dice, shadows, etc.). Unknown variants return `solved:false` with an error.
- See `arkose/README.md` for full protocol details, model list, and encryption format.

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
├── awswaf/                # aws-waf-token (silent JS challenge) harvester
│   ├── solve.py
│   ├── _selfcheck.py
│   └── __init__.py
├── botguard/              # Google OAuth bgRequest token extraction
│   ├── solve.py
│   └── README.md
├── datadome/              # datadome clearance cookie (site-agnostic; caller passes url+referer)
│   ├── solve.py
│   ├── _selfcheck.py
│   └── README.md
├── perimeterx/            # HUMAN "Press & Hold" → _px3 (CDP press-hold + PoW)
│   ├── solve.py           #   site-agnostic core: detect gate → press-hold → harvest
│   ├── renderers/         #   per-site gate triggers (render_flow param)
│   │   ├── __init__.py    #     RENDERERS registry
│   │   └── outlook.py     #     outlook_signup: throwaway form nav to surface the gate
│   └── README.md
├── akamai/                # Bot Manager _abck clearance
│   └── solve.py
├── aliyun/                # Captcha 2.0 slide-puzzle
│   └── solve.py
└── arkose/                # FunCaptcha visual puzzle (ONNX classification)
    ├── solve.py           #   CloakBrowser → gfct intercept → predict → encrypt → /fc/ca/
    ├── predict.py         #   ONNX predictor (4 threads, ~0.1s inference)
    ├── models/            #   24 pre-trained ONNX models (~1.4GB)
    └── README.md
```

> The per-package `mistral.py` and `apikey.txt` were consolidated into `common/`
> (they had diverged; the key files were byte-identical). The legacy Whisper-based
> audio-challenge fallback was removed — it was dead code and reliably IP-blocked.

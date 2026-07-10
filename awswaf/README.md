# AWS WAF (`aws-waf-token`)

Harvests the `aws-waf-token` cookie by navigating the **real URL** and letting AWS
WAF's **silent JS challenge** run — a proof-of-work (`token.js` / `challenge.js`) that
executes invisibly on the protected page and, on success, sets the cookie. Same shape
as the `cloudflare` solver: navigate → poll the named cookie → return the jar + UA.
**No sitekey.**

## Endpoint

`POST /solve` with `type: "awswaf"`.

| field | required | note |
|---|---|---|
| `type` | yes | `"awswaf"` |
| `url` | yes | the AWS-WAF-protected page |
| `proxy` | no | `http://user:pass@ip:port` — solve (and replay) through this exit IP |
| `timeout_s` | no | default 60; cookie-poll deadline |
| `pre_actions` | no | steps run after `goto`, before polling |
| `post_fetch` | no | steps run once the token is set |

```bash
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"awswaf","url":"https://protected.example.com",
       "proxy":"http://user:pass@ip:port","timeout_s":60}'
```

## Response

Every `POST /solve` 200 carries the uniform top-level **`solved`** bool — read that, do
not branch per-type. AWS WAF extras on top:

```jsonc
{
  "solved": true,                            // uniform success field — read THIS
  "token": "…",                              // the aws-waf-token cookie value
  "aws_waf_token": { "name":"aws-waf-token", "value":"…", "domain":".example.com", "expires":… },
  "success": true,
  "cookies": [ /* full jar — build a Cookie header */ ],
  "user_agent": "Mozilla/5.0 …",             // MUST be replayed verbatim
  "headers": { "User-Agent":"…", "Accept-Language":"…" },
  "proxy": "http://…",
  "method": "silent-challenge",
  "elapsed": 3.9,                            // solve time (seconds)
  "warning": "aws-waf-token is bound to IP + JA3/TLS + User-Agent…"
}
```

A solve that **ran but did not succeed** → HTTP **200** with `solved:false` + `error`.
A request that **never solved** → non-2xx with `{detail}` (408 on `timeout_s` exceeded,
400 bad type / missing `url` / SSRF-blocked host, 500 solver crash). Rule of thumb:
**2xx → read `solved`; non-2xx → read `detail`. Never both.**

## Replay contract — read this

`aws-waf-token` is **bound to three things at once**: the **exit IP**, the **JA3/TLS
fingerprint**, and the **User-Agent** — same binding as `cf_clearance`. To reuse it:

- Replay from the **same proxy IP** you solved on → pass `proxy` (or set
  `TURNSTILE_PROXY`; this path shares Turnstile's env). A token solved on the server's
  own IP only works from that IP.
- Send the **exact `user_agent`** returned, and a matching `Accept-Language`.
- Use a client whose **TLS fingerprint matches** (curl-impersonate or another
  CloakBrowser). Plain `requests` / `httpx` / `curl` get re-challenged even with the
  right IP + UA, because their JA3 differs.

## CloudFront hard-block early-abort

CloudFront/WAF hard-blocks (datacenter IP, geo-block) serve an error page whose title
matches one of four known block phrases (`403 forbidden`, `access denied`, `request
blocked`, `the request could not be satisfied`) — it no longer matches a bare `error`
substring, so benign titles aren't misclassified. The WAF JS never runs there, so
polling would only burn `timeout_s`. The solver detects this on the page title and
**fails fast after one proxied retry** (which can differ only if the proxy rotates the
exit IP), returning `solved:false` + `error` rather than hanging. A
**residential/mobile proxy** is usually required to get through.

## LIMITATION — silent challenge ONLY

> This solves the **SILENT / JS proof-of-work challenge only**. There is **no support
> for AWS WAF's interactive visual puzzle** (the grid/carousel one). If a target
> **escalates to the puzzle, the cookie never sets** and the solve times out —
> `200 solved:false` (or `408` if it exceeds `timeout_s`). It does not hang.
>
> Future: drive the puzzle + wire **Mistral vision** (reuse `recaptcha/image_solve.py`)
> when a real target needs it.

## Environment

Shares the **`TURNSTILE_`** prefix — this path calls `browser_kwargs("TURNSTILE")`:

- `TURNSTILE_HEADLESS` — `0` runs headful under Xvfb (the systemd unit does this).
- `TURNSTILE_PROXY` — default exit IP; per-request `proxy` overrides it.
- `TURNSTILE_GEOIP=1` — align timezone/locale/WebGL to the exit IP when proxying.

## Files

```
awswaf/
├── solve.py        # navigate → silent WAF JS → poll aws-waf-token → jar + UA
├── _selfcheck.py   # offline runnable check: python -m awswaf._selfcheck
└── __init__.py
```

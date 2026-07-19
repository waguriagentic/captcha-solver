# Cloudflare clearance (`cf_clearance`)

Passes the **full-page Cloudflare interstitial** and harvests the `cf_clearance`
cookie by navigating the **real URL** and polling the cookie jar until it appears and
the challenge markers are gone. One code path handles **both** interstitial variants:

- **Managed Challenge** — a Turnstile checkbox inside the `challenges.cloudflare.com`
  iframe. Best-effort humanized click (reuses `turnstile._click_turnstile_checkbox`);
  a no-op on the passive variant.
- **JS Challenge** — "Checking your browser…" that auto-resolves; no click needed.

**No sitekey.** This is page-level clearance, not a widget-token mint.

## Endpoint

`POST /solve` with `type: "cloudflare"`.

| field | required | note |
|---|---|---|
| `type` | yes | `"cloudflare"` |
| `url` | yes | the CF-protected page |
| `proxy` | no | `http://user:pass@ip:port` — solve (and replay) through this exit IP |
| `timeout_s` | no | default 60; cookie-poll deadline |
| `pre_actions` | no | steps run after `goto`, before polling |
| `post_fetch` | no | steps run once `cf_clearance` is set |

```bash
curl -X POST http://127.0.0.1:8877/solve \
  -H "Content-Type: application/json" \
  -d '{"type":"cloudflare","url":"https://protected.example.com",
       "proxy":"http://user:pass@ip:port","timeout_s":60}'
```

## Response

Every `POST /solve` 200 carries the uniform top-level **`solved`** bool — read that, do
not branch per-type. Note there is **no top-level `token`** for this type (success is
signalled by `solved` / `cf_clearance`). Cloudflare extras:

```jsonc
{
  "solved": true,                            // uniform success field — read THIS
  "cf_clearance": { "name":"cf_clearance", "value":"…", "domain":".example.com", "expires":… },
  "success": true,
  "cookies": [ /* full jar — build a Cookie header */ ],
  "user_agent": "Mozilla/5.0 …",             // MUST be replayed verbatim
  "headers": { "User-Agent":"…", "Accept-Language":"…" },
  "proxy": "http://…",
  "method": "interstitial",
  "elapsed": 3.9,                            // solve time (seconds)
  "warning": "cf_clearance is bound to IP + JA3/TLS + User-Agent…"
}
```

A solve that **ran but did not succeed** → HTTP **200** with `solved:false` + `error`
(`cf_clearance not set`). A request that **never solved** → non-2xx with `{detail}`
(408 on `timeout_s` exceeded, 400 bad type / missing `url` / SSRF-blocked host, 500
solver crash). Rule of thumb: **2xx → read `solved`; non-2xx → read `detail`. Never both.**

## Replay contract — read this

`cf_clearance` is **bound to four things at once**: the **exit IP**, the **JA3/TLS
fingerprint**, the **User-Agent**, and the specific challenge. To reuse it:

- Replay from the **same proxy IP** you solved on → pass `proxy` on the solve request.
  A cookie solved on the server's own IP only works from that IP.
- Send the **exact `user_agent`** returned, and a matching `Accept-Language`.
- Use a client whose **TLS fingerprint matches** (curl-impersonate or another
  CloakBrowser). Plain `requests` / `httpx` / `curl` get re-challenged even with the
  right IP + UA, because their JA3 differs.

## Limitations (be realistic)

- **Datacenter IPs are scored harshly.** Managed / "Under Attack" mode may never let a
  raw VPS/datacenter IP through — the checkbox stays unsolved. A **residential/mobile
  proxy** is usually required. On failure the solver returns `solved:false` + `error`
  (and 408 if it exceeds `timeout_s`); it does not hang.
- **Short TTL.** `cf_clearance` typically lives ~15–30 min (site-configurable) — treat
  it as ephemeral and re-solve on expiry.
- **High-value Managed sitekeys** (e.g. the CF dashboard signup) score a locally-minted
  token `1201` regardless of engine — the moat is IP/session reputation, not fingerprint.

## Environment

Uses `browser_kwargs("TURNSTILE")` — headless is the global `BROWSER_HEADLESS`
flag; geoip stays on the Turnstile prefix:

- `BROWSER_HEADLESS=0` — headful under Xvfb (the systemd unit does this); needed
  so the Managed-Challenge checkbox click works.
- `TURNSTILE_GEOIP=1` — align timezone/locale/WebGL to the exit IP when proxying.

Proxy is per-request only: pass body field `proxy`. No env fallback.

## Files

```
cloudflare/
├── solve.py        # navigate → pass interstitial (Managed click or JS wait) → poll cf_clearance → jar + UA
├── _selfcheck.py   # offline runnable check: python -m cloudflare._selfcheck
└── __init__.py
```

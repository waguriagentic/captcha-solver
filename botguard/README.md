# BotGuard solver (Google OAuth `bgRequest` token)

Extracts a Google BotGuard token by running the real anti-bot VM once in
CloakBrowser, then returns it bundled with the session cookies so the caller can
replay the login over pure HTTP.

## Why extraction, not generation

BotGuard tokens **cannot** be generated offline. The VM is:
- polymorphic (program re-fetched + re-keyed per serve),
- runtime-bound (private clock offset, per-run nonce, Welford timing signature),
- iframe-realm bound (collection runs inside a hidden `contentWindow.eval`).

A headless-Node reimplementation (jsdom + headless-gl + node-canvas) boots the
interpreter and gets the `{invoke, pe}` VM handle, but the collection loop never
fires without the browser's iframe realm. So the only viable path is: run the VM
in a real browser once, extract the token. Full reverse-engineering write-up:
`~/scripts/botguard-re/BOTGUARD_SOLVER.md` (4 RE levels + end-to-end proof).

## How it works

1. CloakBrowser opens the Google sign-in flow.
2. Enter `email` → the account-lookup RPC **MI613e** fires (carries a token).
   If `password` is also given, continue → the password-submit RPC **B4hajb**
   fires (the real hard-gate token).
3. That RPC is **intercepted + aborted** (`page.route`), so its token stays
   **UNUSED** — captured from the request body, never consumed by the browser.
4. Returns `token` + `cookies` + `user_agent` + `replay_url`/`replay_body` so the
   caller can replay the exact RPC over HTTP (curl_cffi) with the same session.

## Request

```jsonc
{
  "type": "botguard",
  "email": "user@example.com",
  "password": "optional",      // set → grab B4hajb hard-gate token; omit → MI613e lookup token
  "url": "https://accounts.google.com/signin/v2/identifier?flowName=GlifWebSignIn", // optional, defaults here
  "proxy": "http://user:pass@ip:port",  // optional; token+cookies are IP/session-bound → replay from same IP
  "timeout_s": 90
}
```

## Response (superset of the shared SolveResponse)

```jsonc
{
  "type": "botguard",
  "solved": true,
  "token": "MTKlMlbN...",       // the bgRequest token (UNUSED)
  "token_len": 2508,
  "rpc": "B4hajb",              // which RPC it came from
  "gate": "password",          // "password" (hard-gate) | "account-lookup" (soft-signal)
  "user_agent": "Mozilla/5.0 ...",
  "cookies": [{"name","value","domain","path"}, ...],
  "cookie_header": "NID=...; __Host-...=...",
  "replay_url": "https://accounts.google.com/.../batchexecute?rpcids=B4hajb...",
  "replay_body": "f.req=...",   // the exact form body; splice a fresh token if replaying differently
  "replay_headers": { ... },    // minus content-length/host/cookie
  "method": "route-intercept",
  "elapsed": 29.4
}
```

## Replay contract (proven)

The extracted token, replayed via `curl_cffi` (impersonate `chrome131`) with the
returned `cookie_header` against `replay_url`+`replay_body`, is honored by Google:
- valid token → B4hajb data slot **populated** → login proceeds (CheckCookie)
- corrupt/absent token → `null` + `[3]` → rejected

Verified end-to-end on 3 real accounts (see BOTGUARD_SOLVER.md). Token is
session-bound: replay with the SAME cookies (and same proxy IP if one was used).

## Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `BOTGUARD_HEADLESS` | `0` (service) / `1` (dev) | `0` = headed (more trusted) |
| `BOTGUARD_PROXY` | unset | Shared proxy fallback (per-request `proxy` overrides) |

## Limits

- Token generation still needs a browser (~30–40s/token). Only the login replay
  is HTTP. Architecture is hybrid by necessity.
- Token is per-account/per-session — cannot be reused across accounts.
- Google Workspace (GSuite) domain accounts work for sign-in as long as the
  account uses a Google-managed password (not federated SAML/SSO to an external
  IdP, which would redirect away from the Google password step).

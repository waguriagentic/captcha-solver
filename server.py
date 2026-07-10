"""Captcha solver HTTP sidecar — unified endpoints."""
import asyncio
import ipaddress
import itertools
import logging
import os
import socket
import time
from collections import deque
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("captcha-solver")

_DESCRIPTION = """
Local captcha-solving HTTP sidecar built on **CloakBrowser** (self-hosted anti-detect
Chromium). Solves challenges by driving them in a real browser engine.

**Supported:** Turnstile · reCAPTCHA (v2 / v3 / invisible, incl. Enterprise) · hCaptcha ·
Cloudflare clearance (`cf_clearance` — full-page Managed / JS challenge) ·
AWS WAF (`aws-waf-token` — silent JS challenge).

Dispatch is by the `type` field of `POST /solve`; optional fields select the variant
(`version`, `real_page`, `verify_url`, …). `/health` is public; behind the public
domain every other path needs a Bearer token (enforced at the Caddy layer).

Caller-supplied URLs (`url`, `verify_url`, `page_url`, `post_fetch[].url`) are fetched
from the browser session and are **SSRF-guarded**: private/loopback/link-local targets
are rejected unless `SOLVER_ALLOW_PRIVATE=1`.
"""

_TAGS = [
    {"name": "solve", "description": "Solve a captcha challenge."},
    {"name": "monitoring", "description": "Liveness, current tasks, recent solve log."},
]

# Public base URL shown in the OpenAPI docs (contact + servers dropdown). The repo ships a
# neutral placeholder; the live service injects its real domain at runtime via SOLVER_PUBLIC_URL.
_PUBLIC_URL = os.getenv("SOLVER_PUBLIC_URL", "https://solver.example.com")

app = FastAPI(
    title="Captcha Solver",
    description=_DESCRIPTION,
    version="1.0.0",
    openapi_tags=_TAGS,
    contact={"name": "solver", "url": _PUBLIC_URL},
    servers=[
        {"url": _PUBLIC_URL, "description": "Public (Bearer token required)"},
        {"url": "http://127.0.0.1:8877", "description": "Local (no auth)"},
    ],
    swagger_ui_parameters={
        "docExpansion": "list",
        "persistAuthorization": True,     # keep the Bearer token across reloads
        "tryItOutEnabled": True,
        "displayRequestDuration": True,
        "filter": True,
    },
)

# Non-enforcing Bearer scheme: makes Swagger UI show an Authorize button and forward the
# token on "Try it out". auto_error=False means a missing/malformed token yields None and
# the endpoint proceeds — real enforcement stays at the Caddy layer (public domain only).
_bearer = HTTPBearer(auto_error=False, description="Bearer token (required on the public "
                     "domain; enforced by the reverse proxy). Ignored for local calls.")
SUPPORTED = ["turnstile", "recaptcha", "hcaptcha", "cloudflare", "awswaf"]
# Page-level solvers that harvest a cookie (no sitekey needed).
_PAGE_LEVEL = ("cloudflare", "awswaf")
# Allow private/loopback targets only when explicitly opted in (dev/testing).
_ALLOW_PRIVATE = os.getenv("SOLVER_ALLOW_PRIVATE") == "1"

# ── Monitoring ring buffer ───────────────────────────────────────────
_solve_log = deque(maxlen=100)
# Concurrent solves of different types can run at once (per-type locks), so track
# current tasks by id rather than a single global that they'd clobber.
_solve_current: dict = {}
_task_ids = itertools.count(1)


def _is_solved(result: dict) -> bool:
    """The ONE success predicate for every solver type — the single source of truth for
    the injected `solved` field + logs. Token solvers signal via truthy `token`, realpage
    variants via `verify_success`, page-level cookie solvers via `success`/`cf_clearance`;
    a truthy value in ANY of these = solved.
    """
    return bool(result.get("token") or result.get("cf_clearance")
                or result.get("verify_success") or result.get("success"))


def _log_solve(type_: str, sitekey: Optional[str], url: str, result: dict):
    """Push a solve event to the ring buffer."""
    sitekey = sitekey or ""  # cloudflare has no sitekey
    solved = _is_solved(result)
    _solve_log.appendleft({
        "type": type_,
        "sitekey": sitekey[:12] + ("..." if len(sitekey) > 12 else ""),
        "url": url[:60] + ("..." if len(url) > 60 else ""),
        "token": solved,
        "error": result.get("error"),
        "elapsed": result.get("elapsed"),
        "method": result.get("method"),
        "timestamp": time.time(),
        "success": solved and not result.get("error"),
    })


def _assert_public_url(raw: str, field: str):
    """Reject non-http(s) schemes and private/loopback/link-local/reserved hosts.

    Guards the SSRF surface: /solve navigates and fetches caller-supplied URLs from
    the server's browser session (credentials:'include'). ponytail: validate-then-
    fetch has a DNS-rebinding TOCTOU window; add pinned resolution if it matters.
    """
    if not raw:
        return
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise HTTPException(400, f"{field}: only http/https URLs allowed")
    host = u.hostname
    if not host:
        raise HTTPException(400, f"{field}: URL has no host")
    if _ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"{field}: host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(400, f"{field}: private/loopback host blocked")


def _validate_urls(req: "SolveRequest"):
    _assert_public_url(req.url, "url")
    _assert_public_url(req.verify_url, "verify_url")
    _assert_public_url(req.page_url, "page_url")
    for pf in (req.post_fetch or []):
        _assert_public_url(pf.url, "post_fetch.url")


class PreAction(BaseModel):
    """One UI step to run before the captcha appears (real_page mode)."""
    type: str = Field(..., description="click | fill | select | press | wait",
                      examples=["click"])
    selector: Optional[str] = Field(
        None, description="Target selector. Formats: CSS (default), XPath (//…), "
        "text=…, regex=…, role=name[name='…']", examples=["text=Continue with Email"])
    value: Optional[str] = Field(
        None, description="Value for fill/select/press, or seconds for wait")
    timeout: Optional[int] = Field(10000, description="Element wait timeout (ms)")


class PostFetch(BaseModel):
    """An API call fired from the SAME browser session after solving."""
    url: str = Field(..., description="Endpoint to call (SSRF-guarded, same as top-level url)",
                     examples=["https://target.com/api/verify"])
    method: Optional[str] = Field("POST", examples=["POST"])
    body: Optional[dict] = Field(
        None, description="JSON body. Use the literal __TOKEN__ anywhere to inject the "
        "solved token.", examples=[{"token": "__TOKEN__"}])


class SolveRequest(BaseModel):
    # Required
    type: str = Field(..., description="Captcha type — dispatch key.",
                      examples=["turnstile"])
    sitekey: Optional[str] = Field(
        None, description="Site key from the target page. Required for turnstile/recaptcha/"
        "hcaptcha; not used for type=cloudflare (page-level clearance).",
        examples=["0x4AAAAAAA..."])
    url: str = Field(..., description="Page the captcha is on (also the intercept origin).",
                     examples=["https://target.com"])

    # All-captcha optional
    action: Optional[str] = Field(
        None, description="Turnstile action, or reCAPTCHA v3/invisible action. "
        "For hCaptcha, the literal \"invisible\" selects the invisible-execute path.")
    cdata: Optional[str] = Field(None, description="Turnstile customer data bound into the token.")
    real_page: Optional[bool] = Field(
        False, description="Solve on the live target page (navigate + drive) instead of a stub.")
    timeout_s: Optional[int] = Field(
        60, description="Overall solve deadline (seconds). Enforced server-side; on expiry the "
        "call returns 408 and the browser is released.")
    pre_actions: Optional[list[PreAction]] = Field(None, description="Steps to run before solving (real_page).")
    post_fetch: Optional[list[PostFetch]] = Field(None, description="API calls after solving (real_page).")
    proxy: Optional[str] = Field(
        None, description="Per-request proxy (scheme://user:pass@host:port). Honored for "
        "type=cloudflare and type=awswaf (overrides the shared TURNSTILE_PROXY env fallback); "
        "their cookies are IP-bound, so replay from this same proxy IP. For turnstile/recaptcha "
        "set TURNSTILE_PROXY / RECAPTCHA_PROXY instead — the per-request field is not wired for "
        "those.")

    # reCAPTCHA-only
    version: Optional[str] = Field(None, description="reCAPTCHA only: v2 | v3 | invisible (default v2).")
    secret: Optional[str] = Field(None, description="reCAPTCHA v3 only: target's secret key, to also return the score.")
    enterprise: Optional[bool] = Field(False, description="reCAPTCHA only: load enterprise.js / grecaptcha.enterprise.")

    # solve-and-verify (turnstile)
    verify_url: Optional[str] = Field(None, description="Turnstile: verify the token from the same session at this URL.")
    verify_payload: Optional[dict] = Field(None, description="Turnstile: body for verify_url; token is injected as \"token\".")
    page_url: Optional[str] = Field(None, description="Turnstile: origin to intercept (defaults to verify_url).")


# Named request examples → Swagger UI renders these as a dropdown picker on /solve.
_SOLVE_EXAMPLES = {
    "turnstile": {
        "summary": "Turnstile (route-intercept)",
        "value": {"type": "turnstile", "sitekey": "0x4AAAAAAA...", "url": "https://target.com"},
    },
    "recaptcha_v3": {
        "summary": "reCAPTCHA v3 Enterprise (score)",
        "value": {"type": "recaptcha", "version": "v3", "enterprise": True,
                  "sitekey": "6Lc...", "url": "https://target.com", "action": "login"},
    },
    "recaptcha_v2": {
        "summary": "reCAPTCHA v2 checkbox",
        "value": {"type": "recaptcha", "version": "v2", "sitekey": "6Lf...", "url": "https://target.com/form"},
    },
    "hcaptcha": {
        "summary": "hCaptcha (checkbox)",
        "value": {"type": "hcaptcha", "sitekey": "10000000-ffff-ffff-ffff-000000000001",
                  "url": "https://target.com"},
    },
    "turnstile_realpage": {
        "summary": "Turnstile on the live page (pre_actions + post_fetch)",
        "value": {"type": "turnstile", "real_page": True, "url": "https://app.example.com/login",
                  "pre_actions": [{"type": "fill", "selector": "input[type=email]", "value": "u@ex.com"},
                                  {"type": "click", "selector": "button[type=submit]"}],
                  "post_fetch": [{"url": "https://app.example.com/api/verify",
                                  "body": {"token": "__TOKEN__"}}]},
    },
    "cloudflare_clearance": {
        "summary": "Cloudflare clearance (cf_clearance — Managed or JS challenge)",
        "value": {"type": "cloudflare", "url": "https://protected.example.com",
                  "proxy": "http://user:pass@ip:port"},
    },
    "aws_waf": {
        "summary": "AWS WAF token (silent JS challenge → aws-waf-token)",
        "value": {"type": "awswaf", "url": "https://protected.example.com/waitlist",
                  "proxy": "http://user:pass@ip:port"},
    },
}


# ── Response models (documentation shapes; solvers return supersets) ──
class SolveResponse(BaseModel):
    type: str = Field(..., description="Echoes the request type — the dispatch discriminator.",
                      examples=["turnstile"])
    solved: bool = Field(..., description="THE success signal. True iff the captcha was solved, "
                         "uniform across every type — read this instead of branching per-type.")
    token: Optional[str] = Field(None, description="Solved token for token types (turnstile/"
                                 "recaptcha/hcaptcha). Absent for type=cloudflare (see cf_clearance); "
                                 "empty string on a failed/realpage solve — trust `solved`, not this.")
    method: Optional[str] = Field(None, description="Which path solved it (route | execute | real-page | image | …).")
    elapsed: Optional[float] = Field(None, description="Solve time (seconds).")
    error: Optional[str] = Field(None, description="Set when the solve failed but returned 200.")
    # Per-type success/detail discriminators (present only for their type):
    verify_success: Optional[bool] = Field(None, description="realpage variants: token harvested + verified.")
    success: Optional[bool] = Field(None, description="Page-level (cloudflare/awswaf): cookie obtained.")
    cf_clearance: Optional[dict] = Field(None, description="type=cloudflare: the cf_clearance cookie record.")
    model_config = {"extra": "allow"}  # solvers add expires_in, score, cookies, user_agent, post_fetch, …


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Human-readable error message")


# Schematized non-2xx responses for /solve (422 is auto-documented by FastAPI).
_SOLVE_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request — unsupported type, missing sitekey for a widget type, or an SSRF-rejected URL"},
    408: {"model": ErrorResponse, "description": "Global deadline (timeout_s) exceeded before a result"},
    500: {"model": ErrorResponse, "description": "Unhandled solver error"},
}


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    supported_types: list[str] = Field(examples=[["turnstile", "recaptcha", "hcaptcha"]])


class StatusResponse(BaseModel):
    services: dict[str, str]
    current: list[dict[str, Any]] = Field(description="Currently running solve tasks.")


class LogsResponse(BaseModel):
    logs: list[dict[str, Any]]
    total: int


@app.get("/health", response_model=HealthResponse, tags=["monitoring"],
         operation_id="health",
         summary="Liveness + supported types (public, no auth)")
async def health():
    """Public liveness probe. Lists the captcha types this service can solve."""
    return {"status": "ok", "supported_types": SUPPORTED}


def _extract(req: SolveRequest):
    """Unpack pre_actions + post_fetch for realpage endpoints."""
    actions = [a.model_dump() for a in req.pre_actions] if req.pre_actions else None
    fetches = [f.model_dump() for f in req.post_fetch] if req.post_fetch else None
    return actions, fetches


async def _dispatch(req: SolveRequest) -> dict:
    """Run the actual solver for req.type/version and return its result dict.

    Result always carries a top-level "type"; the caller logs + returns it.
    """
    if req.type == "turnstile":
        from turnstile.solve import solve_turnstile, solve_and_verify, solve_turnstile_realpage
        # route-intercept turnstile raises TimeoutError on no-token; catch it here so an
        # unsolved turnstile returns a uniform 200 {error}, not a collision with the real
        # asyncio deadline (408).
        try:
            if req.verify_url and req.verify_payload:
                r = await solve_and_verify(
                    req.sitekey, req.verify_url, req.verify_payload, req.action,
                    cdata=req.cdata, page_url=req.page_url)
            elif req.real_page:
                actions, fetches = _extract(req)
                r = await solve_turnstile_realpage(
                    req.url, req.sitekey, req.timeout_s, actions, fetches)
            else:
                r = await solve_turnstile(req.sitekey, req.url, req.action, req.cdata)
        except TimeoutError as e:
            r = {"token": "", "error": str(e), "method": "route"}
        return {"type": "turnstile", **r}

    if req.type == "hcaptcha":
        from hcaptcha.solve import solve_hcaptcha, solve_hcaptcha_invisible, solve_hcaptcha_realpage
        if req.action == "invisible":
            r = await solve_hcaptcha_invisible(req.sitekey, req.url)
        elif req.real_page:
            actions, fetches = _extract(req)
            r = await solve_hcaptcha_realpage(
                req.url, req.sitekey, req.timeout_s, actions, fetches)
        else:
            r = await solve_hcaptcha(req.sitekey, req.url)
        return {"type": "hcaptcha", **r}

    if req.type == "cloudflare":
        from cloudflare.solve import solve_cf_clearance
        actions, fetches = _extract(req)
        r = await solve_cf_clearance(req.url, req.proxy, req.timeout_s, actions, fetches)
        return {"type": "cloudflare", **r}

    if req.type == "awswaf":
        from awswaf.solve import solve_aws_waf
        actions, fetches = _extract(req)
        r = await solve_aws_waf(req.url, req.proxy, req.timeout_s, actions, fetches)
        return {"type": "awswaf", **r}

    # reCAPTCHA
    from recaptcha.solve import (
        solve_recaptcha_v3, solve_recaptcha_v3_realpage, solve_recaptcha_invisible,
        solve_recaptcha_v2, solve_recaptcha_v2_realpage,
    )
    version = req.version or "v2"  # default v2 (checkbox)
    if version == "v3":
        if req.real_page:
            actions, _ = _extract(req)
            r = await solve_recaptcha_v3_realpage(
                req.url, req.sitekey, req.action or "submit",
                enterprise=req.enterprise, timeout_s=req.timeout_s, pre_actions=actions)
        else:
            r = await solve_recaptcha_v3(
                req.sitekey, req.url, req.action or "submit",
                req.secret, enterprise=req.enterprise)
    elif version == "invisible":
        r = await solve_recaptcha_invisible(
            req.sitekey, req.url, req.action or "submit", enterprise=req.enterprise)
    elif version == "v2":
        if req.real_page:
            actions, fetches = _extract(req)
            r = await solve_recaptcha_v2_realpage(
                req.url, req.sitekey, actions, fetches, timeout_s=req.timeout_s)
        else:
            r = await solve_recaptcha_v2(req.sitekey, req.url, enterprise=req.enterprise)
    else:
        raise HTTPException(400, f"Unknown version: {version}. Use v3|invisible|v2")
    return {"type": "recaptcha", **r}


@app.post("/solve", response_model=SolveResponse, tags=["solve"],
          operation_id="solve",
          dependencies=[Depends(_bearer)],
          summary="Solve a captcha (dispatch by type)",
          responses=_SOLVE_ERROR_RESPONSES)
async def solve(req: SolveRequest = Body(..., openapi_examples=_SOLVE_EXAMPLES)):
    """Solve any supported captcha and return the token.

    Dispatch is by `type`; the variant is selected by optional fields:

    - **Turnstile** — default route-intercept; `verify_url`+`verify_payload` to
      solve-and-verify; `real_page:true` to drive the live page (pre_actions/post_fetch).
    - **reCAPTCHA** — `version`: `v2` (checkbox + Mistral image fallback, `real_page` supported),
      `v3` (score; pass `secret` to also return the score), `invisible`. `enterprise:true`
      for Enterprise keys.
    - **hCaptcha** — default checkbox (Mistral image/drag fallback); `action:"invisible"`
      for the execute path; `real_page:true` for the live page.
    - **cloudflare** — pass the full-page Cloudflare interstitial (Managed or JS challenge)
      and return the `cf_clearance` cookie + `user_agent` + all cookies. No `sitekey`;
      pass `proxy` so the cookie is bound to a replayable IP. See the README for the
      replay contract (IP + JA3 + UA must match).
    - **awswaf** — navigate an AWS-WAF-protected URL, let the silent JS challenge set
      `aws-waf-token`, and return it + `user_agent` + all cookies. No `sitekey`; pass
      `proxy` (same IP-bound replay contract as cloudflare). Silent challenge only —
      no interactive visual-puzzle support.

    **Success signal:** every response carries a uniform top-level `solved` bool — read
    it and don't branch per-type. Type-specific detail still rides along (`token`,
    `cf_clearance`, `score`, `expires_in`, `cookies`, `user_agent`, `post_fetch`, …).

    **Error contract (two rules):** a solve that ran but didn't succeed returns **200**
    with `solved:false` + `error` set. A **4xx/5xx** means the request never solved —
    FastAPI's `{detail}` envelope (400 bad input, 408 timeout, 422 schema, 500 crash).
    So: 2xx → read `solved`; non-2xx → read `detail`. Never both.
    """
    if req.type not in SUPPORTED:
        raise HTTPException(400, f"Unsupported type: {req.type}. Supported: {SUPPORTED}")
    if not req.url:  # pydantic makes url required but allows ""; goto("") is meaningless
        raise HTTPException(400, "url is required")
    if req.type not in _PAGE_LEVEL and not req.sitekey:
        raise HTTPException(400, f"sitekey is required for type={req.type}")
    _validate_urls(req)

    sk = req.sitekey or ""  # cloudflare has no sitekey
    log.info("Solve: type=%s sitekey=%s url=%s", req.type, sk[:12], req.url)

    task_id = next(_task_ids)
    _solve_current[task_id] = {
        "type": req.type,
        "sitekey": sk[:12] + ("..." if len(sk) > 12 else ""),
        "url": req.url[:60] + ("..." if len(req.url) > 60 else ""),
        "version": req.version or None,
        "started_at": time.time(),
    }
    try:
        # Global deadline: a hung browser can't wedge the per-type lock forever — the
        # timeout cancels the coroutine, releasing the lock (caller sees 408). A solver's
        # own no-token TimeoutError is caught INSIDE _dispatch, so a bare TimeoutError
        # here is only ever the real deadline.
        async with asyncio.timeout(req.timeout_s or 60):
            result = await _dispatch(req)
        # ONE success signal for every type — callers read result["solved"], never branch.
        result["solved"] = _is_solved(result)
        _log_solve(req.type, req.sitekey, req.url, result)
        return result
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(408, f"solve timed out after {req.timeout_s or 60}s")
    except HTTPException:
        raise
    except Exception as e:
        log.error("Solve failed: %s", e, exc_info=True)
        raise HTTPException(500, str(e))
    finally:
        _solve_current.pop(task_id, None)


@app.get("/logs", response_model=LogsResponse, tags=["monitoring"],
         operation_id="getLogs",
         dependencies=[Depends(_bearer)],
         summary="Recent solve events (ring buffer)")
async def get_logs(lines: int = Query(50, ge=1, le=200, description="How many recent events (max 200)")):
    """Last N solve events (max 200). Tokens are recorded as a boolean, never stored.
    `total` is the full ring-buffer size; `logs` is the requested slice of it."""
    # lines is already clamped to [1,200] by Query(ge/le) — no re-clamp needed.
    return {"logs": list(_solve_log)[:lines], "total": len(_solve_log)}


@app.get("/status", response_model=StatusResponse, tags=["monitoring"],
         operation_id="status",
         dependencies=[Depends(_bearer)],
         summary="Service status + currently running tasks")
async def solver_status():
    """Per-type online status and the list of in-flight solve tasks."""
    return {
        "services": {t: "online" for t in SUPPORTED},
        "current": list(_solve_current.values()),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8877"))
    uvicorn.run(app, host="0.0.0.0", port=port)

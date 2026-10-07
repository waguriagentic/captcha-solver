"""Admin web surface: the SPA shell and its JSON API.

Everything here is admin-only. The dashboard exposes solve capability, recent
solve metadata and the browser pool, so the surface is gated by
``web.auth`` (server-side sessions + CSRF) and disabled outright when no admin
credential is configured — see ``web/auth.py`` for the threat model.

API contract
------------
All endpoints live under ``/api/v1`` and speak JSON. Errors use FastAPI's
``{"detail": str}`` envelope so the client has exactly one error shape to read:

    401  no/expired session            -> client routes to /login
    403  missing or bad CSRF token     -> client reloads the session
    404  dashboard not configured
    429  login locked out (Retry-After header carries the wait)
    503  UI bundle missing (dist not built)

Reads are GET and safe. Every state change is POST with the CSRF header; there
are no cookies carrying state beyond the opaque session id.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from web import auth

log = __import__("logging").getLogger("captcha-solver.web")

router = APIRouter()

_DIST = pathlib.Path(__file__).resolve().parent / "ui" / "dist"
_INDEX = _DIST / "index.html"
_STARTED_AT = time.time()


# ── Availability ─────────────────────────────────────────────────────
def dist_dir() -> Optional[pathlib.Path]:
    return _DIST if _INDEX.is_file() else None


def dashboard_enabled() -> bool:
    return auth.ENABLED


def _require_enabled() -> None:
    """404 (not 403) when unconfigured: an unconfigured box should not advertise
    that a dashboard exists at all."""
    if not auth.ENABLED:
        raise HTTPException(404, "Not found")


# ── Request helpers ──────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    """Client IP for the login throttle.

    Proxy headers are honoured ONLY when SOLVER_TRUST_PROXY=1, and then the
    value taken is the one the nearest trusted hop wrote:

      * ``CF-Connecting-IP`` first — Cloudflare overwrites it, so it cannot be
        spoofed by the client when traffic arrives through a tunnel.
      * otherwise the LAST entry of ``X-Forwarded-For`` — Caddy appends the
        address it actually accepted the connection from, so the last element
        is the closest hop. The FIRST element is attacker-controlled: a client
        can send its own ``X-Forwarded-For`` and Caddy preserves it as a
        prefix, which would let anyone rotate the lockout key at will.

    With no proxy in front, trusting any of these would be trivially bypassable
    by sending a header, so the default is off and every caller shares one
    bucket — for a single-operator tool, the stricter behaviour.
    """
    if os.getenv("SOLVER_TRUST_PROXY") == "1":
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()[:64]
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _cookie_secure(request: Request) -> bool:
    """Secure flag: forced by SOLVER_COOKIE_SECURE, else inferred from the
    request. Inference covers both deployment shapes without config — the
    Cloudflare tunnel sends X-Forwarded-Proto: https, and loopback dev over
    plain http must not set Secure or the browser drops the cookie."""
    override = os.getenv("SOLVER_COOKIE_SECURE", "").strip()
    if override:
        return override.lower() in ("1", "true", "yes", "on")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return forwarded == "https" or request.url.scheme == "https"


def _session_payload(session: auth.Session) -> dict[str, Any]:
    return {
        "authenticated": True,
        "user": session.username,
        "csrf_token": auth.csrf_token(session.sid),
        "created_at": session.created_at,
        "expires_at": session.expires_at,
    }


# ── Auth dependencies ────────────────────────────────────────────────
async def require_admin(request: Request) -> auth.Session:
    """Gate every API call. Safe methods need a session; mutating methods also
    need the session's CSRF token in X-CSRF-Token.

    The cookie is SameSite=Strict, so a cross-site request never carries it —
    the CSRF check is the second lock for same-site-but-cross-origin cases and
    for any future relaxation of the cookie policy.
    """
    _require_enabled()
    sid = request.cookies.get(auth.COOKIE_NAME)
    session = auth.SESSIONS.get(sid)
    if session is None:
        raise HTTPException(401, "Authentication required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not auth.csrf_valid(sid, request.headers.get(auth.CSRF_HEADER)):
            raise HTTPException(403, "Invalid or missing CSRF token")
    return session


# ── Auth endpoints ───────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024)


@router.post("/api/v1/auth/login", tags=["auth"], summary="Start an admin session")
async def login(body: LoginRequest, request: Request, response: Response):
    _require_enabled()
    cfg = auth.CONFIG
    assert cfg is not None  # narrowed by _require_enabled

    ip = _client_ip(request)
    # Reserve the attempt BEFORE verifying: the limit then holds regardless of
    # how the (blocking) password hash is scheduled, and a correct password
    # during lockout is still refused.
    remaining = auth.THROTTLE.reserve(ip)
    if remaining:
        raise HTTPException(429, f"Too many failed attempts. Try again in {remaining}s.",
                            headers={"Retry-After": str(remaining)})

    # Both comparisons always run: short-circuiting on the username would leak
    # which usernames exist through response timing.
    user_ok = auth._const_eq(body.username, cfg.username)
    pass_ok = auth.verify_password(body.password, cfg.password_hash)

    if not (user_ok and pass_ok):
        auth.THROTTLE.record_failure(ip)
        log.warning("Failed admin login from %s", ip)
        raise HTTPException(401, "Invalid credentials")

    auth.THROTTLE.reset(ip)
    session = auth.SESSIONS.create(cfg.username, ip, request.headers.get("user-agent", ""))
    response.set_cookie(
        auth.COOKIE_NAME, session.sid, path="/", max_age=auth.SESSION_TTL,
        httponly=True, samesite="strict", secure=_cookie_secure(request),
    )
    log.info("Admin session opened for %s from %s", cfg.username, ip)
    return _session_payload(session)


@router.post("/api/v1/auth/logout", tags=["auth"], summary="End the admin session")
async def logout(request: Request, response: Response,
                 session: auth.Session = Depends(require_admin)):
    auth.SESSIONS.destroy(session.sid)
    # Mirror the attributes the cookie was set with, so the deletion matches
    # the stored cookie exactly (Starlette defaults to SameSite=lax otherwise).
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True,
                           samesite="strict", secure=_cookie_secure(request))
    return {"authenticated": False}


@router.get("/api/v1/auth/session", tags=["auth"], summary="Current session")
async def current_session(request: Request):
    """Anonymous callers get 200 + ``authenticated:false`` — this is the app's
    boot probe, so it must not log a console error on a fresh visit."""
    if not auth.ENABLED:
        return {"authenticated": False, "dashboard": False}
    session = auth.SESSIONS.get(request.cookies.get(auth.COOKIE_NAME))
    if session is None:
        return {"authenticated": False, "dashboard": True}
    return {**_session_payload(session), "dashboard": True}


# ── Type metadata (drives the solve form) ────────────────────────────
_TYPE_SPEC: dict[str, dict[str, Any]] = {
    "turnstile": {
        "label": "Cloudflare Turnstile",
        "requires": ["sitekey", "url"],
        "fields": ["action", "cdata", "real_page", "verify_url", "verify_payload"],
        "hint": "Route-intercept by default; real_page drives the live page.",
        "variants": ["route-intercept", "solve + verify", "real page"],
        "returns": ["token"],
    },
    "recaptcha": {
        "label": "reCAPTCHA v2 / v3 / invisible",
        "requires": ["sitekey", "url"],
        "fields": ["version", "action", "secret", "enterprise", "classifier", "real_page"],
        "hint": "version selects the variant; v3 may also return a score.",
        "variants": ["v2 checkbox", "v3 score", "invisible", "enterprise"],
        "returns": ["token", "score"],
    },
    "hcaptcha": {
        "label": "hCaptcha",
        "requires": ["sitekey", "url"],
        "fields": ["action", "real_page"],
        "hint": "action=invisible selects the execute path.",
        "variants": ["checkbox", "invisible", "real page"],
        "returns": ["token"],
    },
    "cloudflare": {
        "label": "Cloudflare clearance",
        "requires": ["url"],
        "fields": ["proxy", "real_page"],
        "hint": "Harvests cf_clearance. Replay from the same IP + UA.",
        "variants": ["managed challenge", "JS challenge"],
        "returns": ["cf_clearance", "cookies", "user_agent"],
    },
    "awswaf": {
        "label": "AWS WAF token",
        "requires": ["url"],
        "fields": ["proxy"],
        "hint": "Silent JS challenge; returns aws-waf-token.",
        "variants": ["silent challenge"],
        "returns": ["token", "aws_waf_token", "cookies", "user_agent"],
    },
    "botguard": {
        "label": "BotGuard (Google OAuth)",
        "requires": [],
        "fields": ["email", "password", "proxy"],
        "hint": "Defaults to the Google sign-in page; email drives the flow.",
        "variants": ["soft-signal", "hard-gate"],
        "returns": ["token", "cookies"],
    },
    "datadome": {
        "label": "DataDome clearance",
        "requires": ["url"],
        "fields": ["referer", "proxy"],
        "hint": "Pass the DataDome-fronted URL plus a matching referer.",
        "variants": ["clearance cookie"],
        "returns": ["cookies", "user_agent"],
    },
    "perimeterx": {
        "label": "PerimeterX / HUMAN",
        "requires": [],
        "fields": ["url", "render_flow", "proxy"],
        "hint": "Press & Hold gate; harvests the _px3 cookie.",
        "variants": ["press & hold"],
        "returns": ["cookies", "user_agent"],
    },
    "akamai": {
        "label": "Akamai Bot Manager",
        "requires": ["url"],
        "fields": ["proxy"],
        "hint": "Harvests the _abck clearance cookie.",
        "variants": ["bmak telemetry"],
        "returns": ["cookies", "user_agent"],
    },
    "aliyun": {
        "label": "Aliyun Captcha 2.0",
        "requires": ["scene_id", "prefix"],
        "fields": ["region", "proxy"],
        "hint": "Slide puzzle; returns certifyId + deviceToken to replay.",
        "variants": ["slide puzzle"],
        "returns": ["certifyId", "deviceToken"],
    },
    "arkose": {
        "label": "Arkose FunCaptcha",
        "requires": ["public_key"],
        "fields": ["url", "game_type", "proxy"],
        "hint": "ONNX image classification across up to 10 waves.",
        "variants": ["visual puzzle"],
        "returns": ["token"],
    },
}


def _field_docs() -> dict[str, dict[str, Any]]:
    """Request-field metadata read straight off ``server.SolveRequest``.

    Derived rather than hand-written so the reference cannot drift from the
    validation that actually runs: a field added to the model appears here on
    the next request, with its real description and examples.
    """
    import server

    out: dict[str, dict[str, Any]] = {}
    for name, field in server.SolveRequest.model_fields.items():
        annotation = str(field.annotation)
        if field.is_required():
            default: Any = None
        else:
            raw = field.get_default(call_default_factory=True)
            # Container defaults are noise in a reference table; show the shape
            # only for scalars a reader can actually copy.
            default = None if isinstance(raw, (list, dict)) else raw
        out[name] = {
            "type": "boolean" if field.annotation is bool
            else "integer" if field.annotation is int
            else "array" if "list" in annotation
            else "object" if "dict" in annotation or name == "verify_payload"
            else "string",
            "required": field.is_required(),
            "default": default,
            "description": field.description or "",
            "examples": [e for e in (getattr(field, "examples", None) or [])
                         if isinstance(e, (str, int, float, bool))][:2],
        }
    return out


@router.get("/api/v1/meta", tags=["meta"], summary="Public service metadata (no auth)")
async def meta():
    """Public by design: the landing page needs to name the API host and list
    the supported types before anyone has logged in.

    Deliberately excludes anything operational — no solve counts, no host
    policy, no configuration. If a field would help an attacker enumerate this
    box, it belongs behind require_admin instead.
    """
    import server

    return {
        "service": "captcha-solver",
        "version": server.app.version,
        "api_base_url": os.getenv("SOLVER_API_PUBLIC_URL", "https://api.example.com"),
        "types": [
            {"type": t, "label": _TYPE_SPEC.get(t, {}).get("label", t)}
            for t in server.SUPPORTED
        ],
    }


@router.get("/api/v1/reference", tags=["meta"],
            summary="Full API reference: endpoints, types, request fields")
async def reference():
    """Everything the docs page renders, in one response.

    Public on purpose — it is the same information the OpenAPI schema already
    publishes at /openapi.json, presented for humans. It carries no solve
    history, no host policy and no credentials.
    """
    import server

    endpoints = [
        {"method": "POST", "path": "/solve", "auth": "bearer",
         "summary": "Solve a captcha (dispatch by type)",
         "detail": "Returns the token plus a uniform top-level solved boolean."},
        {"method": "GET", "path": "/health", "auth": "bearer",
         "summary": "Liveness and supported types",
         "detail": "Token-gated like every other solver endpoint: a public liveness probe would confirm to an anonymous scanner that this service exists and is worth attacking."},
        {"method": "GET", "path": "/status", "auth": "bearer",
         "summary": "Per-type status and in-flight tasks",
         "detail": "Lists every solve currently holding a browser."},
        {"method": "GET", "path": "/logs", "auth": "bearer",
         "summary": "Recent solve events",
         "detail": "Ring buffer of the last 100 events; tokens are recorded as a boolean, never stored."},
        {"method": "GET", "path": "/openapi.json", "auth": "public",
         "summary": "Machine-readable OpenAPI 3 schema",
         "detail": "This schema. Public because it describes the interface, not the deployment: no hosts, no counts, no credentials."},
    ]

    return {
        "service": {"name": "Sonogami Solver", "version": server.app.version},
        "api_base_url": os.getenv("SOLVER_API_PUBLIC_URL", "https://api.example.com"),
        "endpoints": endpoints,
        "fields": _field_docs(),
        "types": [
            {"type": t, **_TYPE_SPEC.get(t, {"label": t, "requires": ["url"],
                                             "fields": [], "hint": "",
                                             "variants": [], "returns": []})}
            for t in server.SUPPORTED
        ],
    }


@router.get("/api/v1/types", tags=["meta"], summary="Supported types + field requirements")
async def types(_: auth.Session = Depends(require_admin)):
    import server  # lazy: avoids a circular import at module load

    return {
        "types": [
            {"type": t, **_TYPE_SPEC.get(t, {"label": t, "requires": ["url"],
                                             "fields": [], "hint": ""})}
            for t in server.SUPPORTED
        ]
    }


@router.post("/api/v1/solve", tags=["console"], summary="Run a solve as the admin")
async def dashboard_solve(body: dict, _: auth.Session = Depends(require_admin)):
    """Run a solve from the console.

    Reuses ``server.run_solve`` so the dashboard cannot drift from the public
    API: same validation, same SSRF guard, same deadline, same logging. Only
    the transport differs — session + CSRF here, Bearer token on the API host.

    The body is taken as a raw dict because ``SolveRequest`` lives in server.py,
    which imports this module; validating it lazily keeps that cycle out of
    module import while still producing the usual 422 shape.
    """
    import server
    from pydantic import ValidationError

    try:
        req = server.SolveRequest.model_validate(body)
    except ValidationError as exc:
        # Surface the first problem in the same {"detail": ...} envelope the
        # rest of the API uses, rather than pydantic's nested error list.
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise HTTPException(422, f"{where}: {first.get('msg', 'invalid request')}")

    return await server.run_solve(req)


# ── Solve history + aggregates ───────────────────────────────────────
def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return round(ordered[idx], 2)


def _aggregate(events: list[dict]) -> dict[str, Any]:
    """Roll the ring buffer up into the numbers the console shows.

    Success is read from the logged ``success`` flag, which server._log_solve
    derives from the single ``_is_solved`` predicate — the dashboard never
    re-implements per-type success logic.
    """
    solved = [e for e in events if e.get("success")]
    elapsed = [e["elapsed"] for e in events if isinstance(e.get("elapsed"), (int, float))]
    by_type: dict[str, dict[str, Any]] = {}
    for e in events:
        row = by_type.setdefault(e.get("type", "?"), {
            "type": e.get("type", "?"), "solves": 0, "succeeded": 0, "failed": 0,
            "avg_elapsed": None, "_elapsed": [], "last_at": 0.0, "last_error": None,
        })
        row["solves"] += 1
        if e.get("success"):
            row["succeeded"] += 1
        else:
            row["failed"] += 1
            if e.get("error"):
                row["last_error"] = str(e["error"])[:200]
        if isinstance(e.get("elapsed"), (int, float)):
            row["_elapsed"].append(e["elapsed"])
        row["last_at"] = max(row["last_at"], e.get("timestamp") or 0.0)

    for row in by_type.values():
        times = row.pop("_elapsed")
        row["avg_elapsed"] = round(sum(times) / len(times), 2) if times else None
        row["success_rate"] = round(row["succeeded"] / row["solves"], 3) if row["solves"] else 0.0
    ordered_types = sorted(by_type.values(), key=lambda r: r["solves"], reverse=True)

    total = len(events)
    return {
        "window": total,
        "solves": total,
        "succeeded": len(solved),
        "failed": total - len(solved),
        "success_rate": round(len(solved) / total, 3) if total else 0.0,
        "avg_elapsed": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
        "p95_elapsed": _percentile(elapsed, 0.95),
        "by_type": ordered_types,
    }


def _console_payload(lines: int = 10) -> dict[str, Any]:
    """The live console snapshot, shared by the JSON endpoint and the SSE stream.

    One builder for both so a polled response and a pushed one can never
    disagree about shape.
    """
    import server

    events = list(server._solve_log)
    return {
        "generated_at": time.time(),
        "stats": _aggregate(events),
        "lifetime": {
            "solves": server._solve_total,
            "in_flight": len(server._solve_current),
        },
        "recent": events[:lines],
        "series": [
            {"t": e.get("timestamp") or 0.0, "ok": bool(e.get("success")),
             "elapsed": e.get("elapsed"), "type": e.get("type")}
            for e in reversed(events[:120])
        ],
        "current": list(server._solve_current.values()),
        "system": _system_payload(),
    }


@router.get("/api/v1/overview", tags=["console"], summary="KPI roll-up + recent activity")
async def overview(
    lines: int = Query(10, ge=1, le=200, description="Recent events to include."),
    _: auth.Session = Depends(require_admin),
):
    return _console_payload(lines)


@router.get("/api/v1/stream", tags=["console"],
            summary="Server-sent events: live console snapshot on change")
async def stream(request: Request, _: auth.Session = Depends(require_admin)):
    """Push the console snapshot whenever the solve log changes.

    Replaces client-side polling. A single timer on the server watches the ring
    buffer's newest timestamp plus the in-flight task set and emits a frame only
    when something actually moved, so an idle console is silent instead of
    re-fetching the same JSON every few seconds.

    The snapshot is also sent once on connect, so a client that only ever reads
    the stream still renders immediately.
    """
    import json

    async def frames():
        last_signature: tuple | None = None
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            import server

            newest = server._solve_log[0]["timestamp"] if server._solve_log else 0.0
            signature = (len(server._solve_log), newest, len(server._solve_current),
                         server._solve_total)
            # Heartbeat keeps intermediaries from closing an idle connection;
            # it carries no payload the client acts on.
            if signature != last_signature:
                last_signature = signature
                idle_ticks = 0
                payload = _console_payload()
                yield f"event: snapshot\ndata: {json.dumps(payload)}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # tell proxies not to buffer the stream
            "Connection": "keep-alive",
        },
    )


def _system_payload() -> dict[str, Any]:
    """Runtime + browser pool status. Shared by /system and the stream."""
    import platform
    import server

    from common import concurrency

    return {
        "service": {
            "version": server.app.version,
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "python": platform.python_version(),
            "public_url": server._PUBLIC_URL,
            "supported_types": server.SUPPORTED,
            "allow_private_targets": server._ALLOW_PRIVATE,
        },
        "browser": {
            "headless": os.getenv("BROWSER_HEADLESS", "0") == "1",
            "binary": os.getenv("CLOAKBROWSER_BINARY_PATH") or None,
            "display": os.getenv("DISPLAY") or None,
            "pool": getattr(concurrency, "pool_stats", lambda: {})(),
        },
        "auth": {
            "user": auth.CONFIG.username if auth.CONFIG else None,
            "sessions": len(auth.SESSIONS),
            "session_ttl_s": auth.SESSION_TTL,
            "trust_proxy": os.getenv("SOLVER_TRUST_PROXY") == "1",
        },
    }


@router.get("/api/v1/system", tags=["console"], summary="Runtime + browser pool status")
async def system(_: auth.Session = Depends(require_admin)):
    return _system_payload()


@router.get("/api/v1/solves", tags=["console"], summary="Solve history (ring buffer)")
async def solves(
    lines: int = Query(50, ge=1, le=200),
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by captcha type"),
    only_failed: bool = Query(False, alias="failed", description="Only failed solves"),
    _: auth.Session = Depends(require_admin),
):
    import server

    events = list(server._solve_log)
    if type_filter:
        events = [e for e in events if e.get("type") == type_filter]
    if only_failed:
        events = [e for e in events if not e.get("success")]
    return {"logs": events[:lines], "total": len(events)}


# ── System ───────────────────────────────────────────────────────────
@router.get("/api/v1/system", tags=["console"], summary="Runtime + browser pool status")
async def system(_: auth.Session = Depends(require_admin)):
    import platform
    import server

    from common import concurrency

    return {
        "service": {
            "version": server.app.version,
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "python": platform.python_version(),
            "public_url": server._PUBLIC_URL,
            "supported_types": server.SUPPORTED,
            "allow_private_targets": server._ALLOW_PRIVATE,
        },
        "browser": {
            "headless": os.getenv("BROWSER_HEADLESS", "0") == "1",
            "binary": os.getenv("CLOAKBROWSER_BINARY_PATH") or None,
            "display": os.getenv("DISPLAY") or None,
            "pool": getattr(concurrency, "pool_stats", lambda: {})(),
        },
        "auth": {
            "user": auth.CONFIG.username if auth.CONFIG else None,
            "sessions": len(auth.SESSIONS),
            "session_ttl_s": auth.SESSION_TTL,
            "trust_proxy": os.getenv("SOLVER_TRUST_PROXY") == "1",
        },
    }


# ── SPA shell ────────────────────────────────────────────────────────
def _index_response() -> FileResponse:
    if not _INDEX.is_file():
        raise HTTPException(
            503,
            "Dashboard UI is not built. Run: npm --prefix web/ui install && "
            "npm --prefix web/ui run build",
        )
    # The shell is never cached: a stale index.html points at hashed bundles
    # that no longer exist after a rebuild.
    return FileResponse(_INDEX, headers={"Cache-Control": "no-store"})


@router.get("/", include_in_schema=False)
async def landing():
    """Public landing page. Renders with or without an admin credential —
    it describes the API, it does not expose it."""
    return _index_response()


@router.get("/docs", include_in_schema=False)
async def docs_page():
    """The API reference page (React), not FastAPI's Swagger UI.

    Swagger stays available at /swagger for interactive "try it out"; this is
    the page the marketing links point at, and it is served from this same
    origin so it needs no CDN exception in the CSP.

    Note: /openapi.json is NOT re-registered here. FastAPI installs that route
    when the app is constructed, so any copy added later would be shadowed and
    dead. web/hosts.py instead permits the existing route on the dashboard host,
    because the console's own nav links to it and it is the same public document
    either way.
    """
    return _index_response()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """Client-side routes (/login, /dashboard) and built assets.

    Registered last, so every explicit API route above wins. Path traversal is
    rejected by resolving against the dist root before serving anything.
    """
    if full_path.startswith("api/"):
        raise HTTPException(404, "Not found")

    # A percent-encoded NUL decodes into a real NUL, and pathlib raises
    # ValueError on it. Reject before any filesystem call.
    if "\x00" in full_path:
        raise HTTPException(404, "Not found")

    dist = dist_dir()
    if dist is not None:
        try:
            candidate = (dist / full_path).resolve()
            inside = candidate.is_relative_to(dist.resolve())
        except (ValueError, OSError):  # pragma: no cover - defensive
            inside = False
        if inside and candidate.is_file():
            return FileResponse(candidate)

    return _index_response()


# ── Security headers ─────────────────────────────────────────────────
# The SPA is our own bundle and needs no external origin and no inline
# script, so it gets the strict policy.
_CSP_APP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "script-src 'self'; "
    # Motion and Radix write inline style attributes at runtime; there is no
    # user-authored markup or CSS on these pages, so the relaxation cannot be
    # turned into an injection vector. script-src stays strict.
    "style-src 'self' 'unsafe-inline'"
)

# FastAPI's generated docs pages are NOT our markup: they load Swagger UI /
# ReDoc from a CDN and bootstrap with an inline script. Applying the app policy
# to them silently renders a blank page (the scripts are blocked), so they get
# their own policy that names exactly the origins they need. Everything else
# the app policy enforces — no framing, no objects, no plugins — still applies.
_CSP_DOCS = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
)

_DOCS_PATHS = ("/swagger", "/redoc", "/docs/oauth2-redirect")


def csp_for(path: str) -> str:
    return _CSP_DOCS if path in _DOCS_PATHS else _CSP_APP


def security_headers(request: Request, response: Response) -> None:
    """Attach hardening headers to every response (incl. /solve and the docs).

    HSTS is only sent when the request arrived over TLS — sending it on a
    loopback http call would be ignored anyway, but it keeps local dev honest.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", csp_for(request.url.path))
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded == "https" or request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")

#!/usr/bin/env python3
"""End-to-end smoke test for the admin dashboard: auth, CSRF, host isolation.

Runs a real uvicorn process on a scratch port with a scratch credential store,
then drives it over HTTP the way a browser would — cookie jar, CSRF header,
spoofed Host headers. Every check asserts a security property that a refactor
could silently break.

    .venv/bin/python tools/smoke_dashboard.py

No captcha is ever solved: the solve endpoint is exercised only up to its
validation layer, which is what proves routing, auth and CSRF work without
launching a browser.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(os.getenv("SMOKE_PORT", "8899"))
BASE = f"http://127.0.0.1:{PORT}"

DASH_HOST = "dash.example.test"
API_HOST = "api.example.test"

ADMIN_USER = "smoke-admin"
ADMIN_PASSWORD = "smoke-password-12345"
API_TOKEN = "smoke-api-token-value"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}{f' — {detail}' if detail and not condition else ''}")


class Client:
    """Cookie-jar-backed HTTP client. One instance == one browser session."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect(),
        )
        self.csrf: str | None = None

    def request(self, method: str, path: str, *, body=None, host: str | None = DASH_HOST,
                csrf: bool | str = "auto", bearer: str | None = None,
                headers: dict | None = None):
        """``host`` defaults to the dashboard host because the service runs with
        host isolation on: a request to the bare loopback address is a
        misdirected one and is refused by design."""
        url = f"{BASE}{path}"
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if host:
            hdrs["Host"] = host
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        if csrf == "auto" and self.csrf:
            hdrs["X-CSRF-Token"] = self.csrf
        elif isinstance(csrf, str):
            hdrs["X-CSRF-Token"] = csrf
        hdrs.update(headers or {})

        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, _lower(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, _lower(e.headers), e.read()

    def json(self, method: str, path: str, **kw):
        status, headers, raw = self.request(method, path, **kw)
        try:
            return status, headers, json.loads(raw or b"null")
        except ValueError:
            return status, headers, None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail on redirect instead of following it — a redirect here would hide a
    misrouted request behind a 200."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _lower(headers) -> dict:
    """Case-insensitive header access: Starlette emits lowercase names, so the
    assertions read the same regardless of which side normalises."""
    return {k.lower(): v for k, v in headers.items()}


def wait_for_health(timeout: float = 40.0) -> bool:
    """Poll readiness through the dashboard host.

    Host isolation refuses a request addressed to the bare loopback IP, so the
    probe must name a host the policy accepts — exactly like a real client.
    """
    deadline = time.time() + timeout
    req = urllib.request.Request(
        f"{BASE}/api/v1/meta", headers={"Host": DASH_HOST, "Accept": "application/json"}
    )
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="solver-smoke-")
    env = {
        **os.environ,
        "PORT": str(PORT),
        "SOLVER_ADMIN_USER": ADMIN_USER,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "SOLVER_CSRF_SECRET": "smoke-csrf-secret-" + "0" * 32,
        "SOLVER_SESSION_FILE": os.path.join(tmp, "sessions.json"),
        "SOLVER_ADMIN_HASH_FILE": os.path.join(tmp, "admin.json"),
        "SOLVER_API_TOKEN": API_TOKEN,
        # Isolation is configured, so the service must also have a token: the
        # service-side check now fails closed when none is set.
        "SOLVER_DASHBOARD_HOST": DASH_HOST,
        "SOLVER_API_HOST": API_HOST,
        "SOLVER_LOGIN_MAX_FAILURES": "3",
        "SOLVER_LOGIN_LOCKOUT_S": "4",
        # Keep the smoke run from picking up a real solve log/state from a
        # concurrently running service.
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    # Server output goes to a FILE, not a pipe. uvicorn logs every request, and
    # a pipe nobody drains fills its buffer (~64KB) partway through the suite,
    # at which point the server blocks on write and stops answering. The file
    # is read back only when something fails.
    log_path = os.path.join(tmp, "server.log")
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "server.py"], cwd=ROOT, env=env,
        stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if not wait_for_health():
            print("server never became healthy; output follows:")
            proc.terminate()
            proc.wait(timeout=10)
            log_file.close()
            print(pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:])
            return 1

        print("\n── public surface ──")
        anon = Client()
        status, _, body = anon.json("GET", "/api/v1/meta")
        check("GET /api/v1/meta is public", status == 200, f"got {status}")
        check("meta advertises the API host", body and body.get("api_base_url", "").startswith("http"))
        # The advertised origin must be the configured one, not a placeholder.
        # Two separate variables used to feed the schema and the pages, so they
        # could disagree and the schema kept a placeholder even when the pages
        # were correct.
        check("meta derives the origin from SOLVER_API_HOST",
              body and body.get("api_base_url") == f"https://{API_HOST}",
              f"got {body.get('api_base_url') if body else None}")

        status, _, ref_body = anon.json("GET", "/api/v1/reference")
        check("reference agrees with meta on the origin",
              ref_body and ref_body.get("api_base_url") == f"https://{API_HOST}",
              f"got {ref_body.get('api_base_url') if ref_body else None}")

        status, _, schema = anon.json("GET", "/openapi.json")
        check("openapi servers advertise the configured origin",
              status == 200 and schema
              and schema.get("servers", [{}])[0].get("url") == f"https://{API_HOST}",
              f"got {schema.get('servers') if schema else None}")
        check("openapi contact agrees and has no trailing slash",
              schema and schema.get("info", {}).get("contact", {}).get("url") == f"https://{API_HOST}",
              f"got {schema.get('info', {}).get('contact', {}).get('url') if schema else None}")
        check("no example.com placeholder leaks into any advertised origin",
              "example.com" not in json.dumps({
                  "meta": body, "reference": ref_body,
                  "servers": schema.get("servers") if schema else None,
                  "contact": schema.get("info", {}).get("contact") if schema else None,
              }))
        check("meta lists 11 types", body and len(body.get("types", [])) == 11,
              f"got {len(body.get('types', [])) if body else 'none'}")
        check("meta leaks no operational data",
              body is not None and not ({"stats", "solves", "hosts"} & set(body)))

        status, _, body = anon.json("GET", "/api/v1/auth/session")
        check("anonymous session probe is 200 + authenticated:false",
              status == 200 and body and body.get("authenticated") is False)

        status, _, _ = anon.json("GET", "/api/v1/overview")
        check("overview requires a session", status == 401, f"got {status}")

        status, _, _ = anon.json("GET", "/api/v1/system")
        check("system requires a session", status == 401, f"got {status}")

        status, _, _ = anon.json("POST", "/api/v1/solve", body={"type": "turnstile"})
        check("dashboard solve requires a session", status == 401, f"got {status}")

        print("\n── SPA shell ──")
        status, headers, raw = anon.request("GET", "/")
        check("landing page served", status == 200 and b"<div id=\"root\">" in raw, f"got {status}")
        check("shell is not cached", "no-store" in (headers.get("cache-control") or ""))
        status, _, raw = anon.request("GET", "/login")
        check("client route falls back to the shell", status == 200 and b"<div id=\"root\">" in raw)
        status, _, raw = anon.request("GET", "/assets/../server.py")
        check("path traversal does not leak source", status in (200, 404) and b"import asyncio" not in raw,
              f"got {status}")

        print("\n── login ──")
        session = Client()
        status, _, _ = session.json("POST", "/api/v1/auth/login",
                                    body={"username": ADMIN_USER, "password": "wrong-password"})
        check("wrong password is 401", status == 401, f"got {status}")
        status, _, _ = session.json("POST", "/api/v1/auth/login",
                                    body={"username": "nobody", "password": ADMIN_PASSWORD})
        check("wrong username is 401", status == 401, f"got {status}")

        status, headers, body = session.json("POST", "/api/v1/auth/login",
                                             body={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        check("correct credentials are 200", status == 200, f"got {status}")
        check("session payload carries a CSRF token", bool(body and body.get("csrf_token")))
        cookie = headers.get("set-cookie", "")
        check("session cookie is HttpOnly", "HttpOnly" in cookie)
        check("session cookie is SameSite=Strict", "samesite=strict" in cookie.lower())
        check("session cookie has no Secure on plain http",
              "secure" not in cookie.lower().replace("httponly", ""))
        session.csrf = body.get("csrf_token") if body else None

        print("\n── authenticated dashboard API ──")
        status, _, body = session.json("GET", "/api/v1/overview")
        check("overview with session is 200", status == 200, f"got {status}")
        check("overview returns stats + series",
              body is not None and {"stats", "series", "recent", "lifetime"} <= set(body))
        check("overview is no-store", True)

        status, _, body = session.json("GET", "/api/v1/system")
        check("system with session is 200", status == 200, f"got {status}")
        check("system reports the admin user",
              body is not None and body.get("auth", {}).get("user") == ADMIN_USER)
        check("system does not leak the session id",
              body is not None and "sid" not in json.dumps(body))

        status, _, body = session.json("GET", "/api/v1/types")
        check("types with session is 200", status == 200 and body and len(body["types"]) == 11,
              f"got {status}")

        print("\n── CSRF ──")
        status, _, _ = session.json("POST", "/api/v1/solve", body={"type": "turnstile"}, csrf=False)
        check("POST without CSRF token is 403", status == 403, f"got {status}")
        status, _, _ = session.json("POST", "/api/v1/solve", body={"type": "turnstile"}, csrf="deadbeef")
        check("POST with a wrong CSRF token is 403", status == 403, f"got {status}")
        status, _, body = session.json("POST", "/api/v1/solve", body={"type": "not-a-real-type"})
        check("POST with a valid CSRF token reaches validation (400)", status == 400,
              f"got {status}")
        check("validation error is actionable",
              body is not None and "Unsupported type" in str(body.get("detail", "")))

        print("\n── dashboard-local links ──")
        dash = Client()
        status, _, raw = dash.request("GET", "/docs", host=DASH_HOST)
        check("/docs resolves on the dashboard host",
              status == 200 and b"<div id=\"root\">" in raw, f"got {status}")
        status, _, body = dash.json("GET", "/openapi.json", host=DASH_HOST)
        check("/openapi.json resolves on the dashboard host",
              status == 200 and body and "paths" in body, f"got {status}")
        status, _, body = dash.json("GET", "/api/v1/reference", host=DASH_HOST)
        check("/api/v1/reference is public and complete",
              status == 200 and body and len(body.get("types", [])) == 11
              and len(body.get("fields", {})) > 20, f"got {status}")
        check("reference leaks no solve data",
              body is not None and not ({"stats", "logs", "recent", "hosts"} & set(body)))

        print("\n── host isolation ──")
        status, _, _ = dash.json("GET", "/api/v1/meta", host=DASH_HOST)
        check("dashboard host serves the dashboard API", status == 200, f"got {status}")
        status, _, _ = dash.json("GET", "/solve", host=DASH_HOST)
        check("dashboard host does NOT expose /solve", status == 404, f"got {status}")
        status, _, _ = dash.json("GET", "/status", host=DASH_HOST)
        check("dashboard host does NOT expose /status", status == 404, f"got {status}")
        status, _, _ = dash.json("GET", "/swagger", host=DASH_HOST)
        check("dashboard host does NOT expose Swagger UI", status == 404, f"got {status}")
        status, _, body = dash.json("GET", "/openapi.json", host=DASH_HOST)
        check("dashboard host serves the public schema (linked by its own docs page)",
              status == 200 and body and "paths" in body, f"got {status}")

        status, _, _ = dash.json("GET", "/api/v1/auth/session", host=API_HOST)
        check("api host does NOT expose the session API", status == 404, f"got {status}")
        status, _, _ = dash.json("GET", "/", host=API_HOST)
        check("api host does NOT serve the SPA", status == 404, f"got {status}")

        status, _, _ = dash.json("GET", "/health", host=API_HOST)
        check("/health without a token is 401 (no public fingerprint)", status == 401,
              f"got {status}")
        status, _, _ = dash.json("GET", "/health", host=API_HOST, bearer=API_TOKEN)
        check("/health with the token is 200", status == 200, f"got {status}")
        status, _, _ = dash.json("GET", "/health", host=DASH_HOST, bearer=API_TOKEN)
        check("/health is not served on the dashboard host", status == 404, f"got {status}")

        status, _, _ = dash.json("GET", "/api/v1/meta", host="random.example.test")
        check("unknown host is refused", status == 421, f"got {status}")
        status, _, _ = dash.json("GET", "/api/v1/meta", host=f"127.0.0.1:{PORT}")
        check("bare loopback host is refused while isolation is on", status == 421,
              f"got {status}")

        print("\n── API bearer token ──")
        status, _, _ = dash.json("POST", "/solve", body={"type": "turnstile"},
                                 host=API_HOST, csrf=False)
        check("/solve without a bearer token is 401", status == 401, f"got {status}")
        status, _, _ = dash.json("POST", "/solve", body={"type": "turnstile"},
                                 host=API_HOST, bearer="wrong-token", csrf=False)
        check("/solve with a wrong bearer token is 401", status == 401, f"got {status}")
        status, _, body = dash.json("POST", "/solve", body={"type": "not-a-real-type"},
                                    host=API_HOST, bearer=API_TOKEN, csrf=False)
        check("/solve with the right token reaches validation (400)", status == 400,
              f"got {status}")
        status, _, body = dash.json("GET", "/status", host=API_HOST, bearer=API_TOKEN)
        check("/status with the right token is 200", status == 200, f"got {status}")

        print("\n── security headers ──")
        _, headers, _ = dash.request("GET", "/", host=DASH_HOST)
        csp = headers.get("content-security-policy", "")
        check("CSP present", bool(csp))
        check("CSP forbids inline scripts", "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0])
        check("CSP forbids framing", "frame-ancestors 'none'" in csp)
        check("X-Frame-Options DENY", headers.get("x-frame-options") == "DENY")
        check("nosniff set", headers.get("x-content-type-options") == "nosniff")
        check("no-referrer set", headers.get("referrer-policy") == "no-referrer")
        check("no HSTS on plain http", "strict-transport-security" not in headers)
        _, headers, _ = dash.request("GET", "/", host=DASH_HOST,
                                     headers={"X-Forwarded-Proto": "https"})
        check("HSTS present when proxied over https",
              "strict-transport-security" in headers)
        _, headers, _ = dash.request("GET", "/", host=DASH_HOST,
                                     headers={"X-Forwarded-Proto": "https"})
        cookie_headers = headers.get("set-cookie", "")
        check("no cookie is set on the landing page", not cookie_headers)

        print("\n── path-confusion bypass (regression) ──")
        # Starlette builds request.url.path from the Host header, so
        # `Host: api.example.com:443/docs` made the gate authorise "/docs/solve"
        # while the router dispatched "/solve". Authorisation must use the ASGI
        # scope path; these cases fail if anyone reverts to request.url.path.
        status, _, _ = dash.json("POST", "/solve", host=f"{API_HOST}:443/docs",
                                 body={"type": "nope"}, csrf=False)
        check("host-suffixed path cannot bypass the bearer check on /solve",
              status == 401, f"got {status}")
        status, _, _ = dash.json("GET", "/logs", host=f"{API_HOST}:443/docs", csrf=False)
        check("host-suffixed path cannot bypass the bearer check on /logs",
              status == 401, f"got {status}")
        status, _, _ = dash.json("POST", "/solve", host=f"{DASH_HOST}:443/x",
                                 body={"type": "nope"}, csrf=False)
        check("host-suffixed path cannot reach the token surface from the dashboard host",
              status == 404, f"got {status}")
        status, _, _ = dash.json("GET", "/logs", host=f"{DASH_HOST}:443/x", csrf=False)
        check("host-suffixed path cannot read /logs from the dashboard host",
              status == 404, f"got {status}")
        status, _, _ = dash.json("GET", "/health", host=f"{API_HOST}:443/docs", csrf=False)
        check("host-suffixed path cannot bypass the token check on /health",
              status == 401, f"got {status}")
        status, _, body = dash.json("POST", "/solve", host=API_HOST, body={"type": "nope"},
                                    bearer=API_TOKEN, csrf=False)
        check("legitimate token traffic still reaches the handler", status == 400,
              f"got {status}")

        print("\n── malformed input (regression) ──")
        status, _, _ = session.json("POST", "/api/v1/solve", body={"type": "turnstile"},
                                    csrf="\xff\xfe")
        check("non-ASCII CSRF token is 403, not 500", status == 403, f"got {status}")
        status, _, _ = anon.request("GET", "/%00", host=DASH_HOST, headers={"Host": DASH_HOST})
        check("encoded NUL path is 404, not 500", status == 404, f"got {status}")
        status, _, _ = anon.request("GET", "/assets/%00x", host=DASH_HOST,
                                    headers={"Host": DASH_HOST})
        check("nested encoded NUL path is 404, not 500", status == 404, f"got {status}")

        print("\n── logout ──")
        status, _, _ = session.json("POST", "/api/v1/auth/logout")
        check("logout is 200", status == 200, f"got {status}")
        status, _, _ = session.json("GET", "/api/v1/overview")
        check("session is dead after logout", status == 401, f"got {status}")

        print("\n── login throttle ──")
        throttle = Client()
        for _ in range(3):
            throttle.json("POST", "/api/v1/auth/login",
                          body={"username": ADMIN_USER, "password": "nope"})
        status, headers, _ = throttle.json("POST", "/api/v1/auth/login",
                                           body={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        check("correct password during lockout is refused (429)", status == 429, f"got {status}")
        check("lockout advertises Retry-After", headers.get("retry-after") is not None)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    print("\n" + "─" * 52)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        print("\nserver log tail:")
        print(pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")[-2000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

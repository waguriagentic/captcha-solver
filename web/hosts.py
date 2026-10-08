"""Host-based surface isolation for the two-subdomain deployment.

The service is reachable under two names that carry different credentials:

    dash.example.com   the admin dashboard — session cookie + CSRF header
    api.example.com    the solver API    — static Bearer token, no cookies

Splitting them is a security boundary, not a routing preference. Each origin
exposes exactly one credential type, so neither can be leveraged into the
other:

  * A cross-site scripting bug on the dashboard origin cannot reach /solve,
    because /solve does not exist on that host.
  * A stolen API token cannot mint a dashboard session, because the auth
    endpoints do not exist on the API host.
  * Cookies are host-scoped by the browser, so the API origin never receives
    the session cookie at all — CSRF against the dashboard requires a request
    that carries a cookie the API host is not given.

Enforcement is opt-in and inferred from configuration: set
``SOLVER_DASHBOARD_HOST`` and/or ``SOLVER_API_HOST`` to turn it on. With
neither set (local dev, single-host installs) every request is allowed and the
service behaves as it always has.

``SOLVER_ALLOWED_HOSTS`` adds extra names that may reach everything (e.g. a
LAN address for on-box debugging) and is ignored unless one of the two hosts
above is set — otherwise there would be no policy to relax.
"""
from __future__ import annotations

import os
from typing import Optional

ALLOW = "allow"
WRONG_HOST = "wrong_host"
BLOCKED_PATH = "blocked_path"

#: Paths that belong to the Bearer-token API surface. Everything here is
#: callable without a session cookie, so it must never be served from the
#: dashboard origin. /health is included: it is token-gated now, and the
#: console reports service state through /api/v1/system instead.
API_PATHS = ("/solve", "/logs", "/status", "/health", "/swagger", "/redoc",
             "/openapi.json", "/docs/oauth2-redirect")

#: No path is exempt from host isolation. /health used to be reachable on every
#: name so monitors could poll the loopback address; that also made it a public
#: fingerprint, so it now resolves like any other path: on the API host, behind
#: the Bearer token.
PUBLIC_PATHS: tuple[str, ...] = ()

#: Prefix owned by the dashboard API (session + CSRF authenticated).
DASHBOARD_PREFIX = "/api/v1/"


def _split(value: Optional[str]) -> list[str]:
    return [p.strip().lower() for p in (value or "").split(",") if p.strip()]


#: Shown when no origin is configured. Clearly a placeholder, so it reads as
#: "you have not configured this" rather than as a real host.
PLACEHOLDER_API_URL = "https://api.example.com"


def public_api_base() -> str:
    """The API origin this deployment actually serves.

    Single source of truth for every surface that advertises a URL — the
    OpenAPI ``servers`` list, the landing page and the reference page. It lives
    here, beside the host policy, because it derives from the same setting: an
    isolated deployment has already told us its API hostname.

    Resolution order:
      1. SOLVER_API_PUBLIC_URL — explicit, wins always.
      2. https://<SOLVER_API_HOST> — derived from the isolation config.
      3. PLACEHOLDER_API_URL — nothing configured; callers may warn.

    Read at call time, never cached at import: the env is the operator's, and a
    module-level constant would freeze the placeholder into the first response.
    """
    explicit = os.getenv("SOLVER_API_PUBLIC_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.getenv("SOLVER_API_HOST", "").strip()
    if host:
        return f"https://{host}".rstrip("/")
    return PLACEHOLDER_API_URL


def is_placeholder(url: str) -> bool:
    """True when ``url`` is the unconfigured default, so callers can say so."""
    return url.rstrip("/") == PLACEHOLDER_API_URL


def _norm(host: str) -> tuple[str, str]:
    """Return (host:port, hostname) for a raw Host header value."""
    raw = (host or "").strip().lower()
    if raw.startswith("["):  # IPv6 literal
        return raw, raw
    name = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
    return raw, name


class HostPolicy:
    """Decide whether a (Host, path) pair is served, and why not when it isn't."""

    def __init__(self,
                 dashboard_host: Optional[str] = None,
                 api_host: Optional[str] = None,
                 extra: Optional[list[str]] = None):
        self.dashboard = _split(dashboard_host)
        self.api = _split(api_host)
        self.extra = _split(",".join(extra or []))
        self.enabled = bool(self.dashboard or self.api)

    @classmethod
    def from_env(cls) -> "HostPolicy":
        return cls(
            dashboard_host=os.getenv("SOLVER_DASHBOARD_HOST"),
            api_host=os.getenv("SOLVER_API_HOST"),
            extra=_split(os.getenv("SOLVER_ALLOWED_HOSTS")),
        )

    def _is(self, host: str, names: list[str]) -> bool:
        if not names:
            return False
        full, name = _norm(host)
        return full in names or name in names

    def evaluate(self, host: str, path: str) -> str:
        """ALLOW / WRONG_HOST / BLOCKED_PATH for one request."""
        if not self.enabled:
            return ALLOW

        # Liveness is host-agnostic: monitors and install.sh poll it directly.
        if path in PUBLIC_PATHS:
            return ALLOW

        if self._is(host, self.extra):
            return ALLOW

        on_dashboard = self._is(host, self.dashboard)
        on_api = self._is(host, self.api)

        if not (on_dashboard or on_api):
            return WRONG_HOST

        is_api_path = (not path.startswith(DASHBOARD_PREFIX)
                       and (path in API_PATHS or path.startswith("/swagger")))

        # Paths the dashboard SPA itself links to. They are API-surface paths,
        # but the console's own nav must resolve: a dead link on the dashboard
        # is a bug, not a security control. Anything under /api/v1/ that is not
        # explicitly here stays admin-only.
        dashboard_links = ("/docs", "/openapi.json")

        if on_api:
            # The API origin serves the Bearer-token surface and nothing else:
            # no dashboard session endpoints, no SPA shell, no login page.
            if path.startswith(DASHBOARD_PREFIX) or not is_api_path:
                return BLOCKED_PATH
            return ALLOW

        # on_dashboard: the shell, its assets and the session API. The
        # token-bearing endpoints are deliberately absent here.
        if is_api_path and path not in dashboard_links:
            return BLOCKED_PATH
        return ALLOW

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "dashboard_hosts": self.dashboard,
            "api_hosts": self.api,
            "extra_hosts": self.extra,
        }


POLICY = HostPolicy.from_env()

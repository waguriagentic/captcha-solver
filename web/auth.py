"""Admin authentication for the web dashboard.

Single-operator design: one admin identity, defined entirely by environment
variables. There is no signup path, no user table, and no way to create a
second account — the attack surface is one login form and one session cookie.

Threat model (read before changing anything here)
-------------------------------------------------
The dashboard sits behind the same reverse proxy as /solve and exposes solve
capability, recent solve metadata and the browser pool. Everything in this
module exists to keep that surface admin-only:

  * Passwords are stored only as scrypt digests (``ADMIN_PASSWORD_HASH``).
    The plaintext env var exists solely so a fresh install can boot; once a
    hash is written it is never consulted again.
  * Sessions are server-side and opaque. The cookie carries 256 bits of
    entropy, not data; the server holds the only copy. A stolen cookie can be
    revoked by restarting the process or calling ``revoke_all``.
  * Every mutating API call requires a CSRF token that is bound to the
    session, compared in constant time, and delivered in a response header
    (never in a cookie — a cross-site form post cannot read headers).
  * Login failures are counted per client IP; the lockout is checked before
    the password is verified so a locked-out attacker cannot use the endpoint
    as a password oracle.

Deliberate non-goals: multi-user roles, password reset email, OAuth. This is a
personal tool, not a product. Adding any of them means re-reading this header.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Tunables ─────────────────────────────────────────────────────────
SESSION_TTL = int(os.getenv("SOLVER_SESSION_TTL", str(12 * 3600)))
"""Session lifetime in seconds (default 12h). Also the cookie Max-Age."""

COOKIE_NAME = "cs_session"
CSRF_HEADER = "X-CSRF-Token"

LOGIN_MAX_FAILURES = int(os.getenv("SOLVER_LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_S = int(os.getenv("SOLVER_LOGIN_LOCKOUT_S", "300"))
"""After LOGIN_MAX_FAILURES bad passwords from one IP, that IP is locked out."""

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
"""scrypt work factors (~32 MiB, ~80 ms on a modern core). N=2**15 is the
floor worth defending; raise N if the login box ever gets faster."""

_MAX_SESSIONS = 32
"""Hard cap on live sessions. One operator needs a handful; the cap bounds the
memory a cookie-flooding attacker can pin."""

_MIN_PASSWORD_LEN = 12

SESSION_FILE = Path(os.getenv("SOLVER_SESSION_FILE", ".solver-sessions.json"))
HASH_FILE = Path(os.getenv("SOLVER_ADMIN_HASH_FILE", ".solver-admin.json"))

_TRUTHY = {"1", "true", "yes", "on"}


# ── Password hashing ─────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Return ``scrypt$n$r$p$salt_hex$hash_hex`` — self-describing, so the work
    factors can be raised later without invalidating the stored digest."""
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {_MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string compare that tolerates unequal lengths.

    hmac.compare_digest raises on str inputs containing non-ASCII, and both
    operands here come from a request body — so compare the UTF-8 bytes.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a digest from hash_password.

    Returns False for a malformed digest rather than raising: a corrupt hash
    file must fail closed (nobody logs in), not crash the login endpoint.
    """
    try:
        algo, n_s, r_s, p_s, salt_hex, want_hex = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt, want = bytes.fromhex(salt_hex), bytes.fromhex(want_hex)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             dklen=len(want), maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(got, want)


# ── Admin credential resolution ──────────────────────────────────────
def _read_hash_file() -> Optional[str]:
    try:
        import json
        return json.loads(HASH_FILE.read_text(encoding="utf-8")).get("hash") or None
    except (OSError, ValueError):
        return None


def _write_private(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` with 0600 from the first byte.

    ``write_text`` then ``chmod`` leaves a window where the digest or the live
    session ids are readable at the default umask, and leaves them permanently
    readable if the chmod never runs. O_CREAT|O_TRUNC with an explicit mode
    closes both.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError:
        os.close(fd)
        raise


def _write_hash_file(digest: str) -> None:
    """Persist the digest 0600. Best effort: a read-only deploy directory must
    not stop the service from booting."""
    import json
    try:
        _write_private(HASH_FILE, json.dumps({"hash": digest}, indent=2))
    except OSError as e:  # pragma: no cover - depends on deploy dir perms
        print(f"[auth] WARNING: could not persist admin hash to {HASH_FILE}: {e}",
              file=sys.stderr)


@dataclass
class AdminConfig:
    username: str
    password_hash: str
    csrf_secret: str = field(repr=False)


def _load_config() -> Optional[AdminConfig]:
    """Resolve the admin identity, or None when the dashboard must stay closed.

    Precedence for the digest: persisted hash file > ``ADMIN_PASSWORD_HASH``
    env > ``ADMIN_PASSWORD`` env (hashed once at boot, then persisted so the
    plaintext can be dropped from the environment).
    """
    username = (os.getenv("SOLVER_ADMIN_USER") or "").strip()

    digest = _read_hash_file()
    env_hash = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
    if not digest and env_hash:
        digest = env_hash

    if not digest:
        plain = os.getenv("ADMIN_PASSWORD") or ""
        if plain:
            try:
                digest = hash_password(plain)
            except ValueError as e:
                print(f"[auth] ADMIN_PASSWORD rejected: {e}", file=sys.stderr)
                return None
            _write_hash_file(digest)
            print("[auth] ADMIN_PASSWORD hashed and persisted — you can unset the "
                  "plaintext now.", file=sys.stderr)

    if not username or not digest:
        return None

    csrf_secret = os.getenv("SOLVER_CSRF_SECRET") or ""
    if not csrf_secret:
        csrf_secret = secrets.token_hex(32)
        print("[auth] WARNING: SOLVER_CSRF_SECRET unset — generated one for this "
              "process. Sessions (and CSRF tokens) will not survive a restart.",
              file=sys.stderr)

    return AdminConfig(username=username, password_hash=digest, csrf_secret=csrf_secret)


CONFIG = _load_config()
"""None disables the whole dashboard (every route 404s) — see web/routes.py."""

ENABLED = CONFIG is not None

# ── Session store ────────────────────────────────────────────────────
@dataclass
class Session:
    sid: str
    username: str
    created_at: float
    expires_at: float
    ip: str
    user_agent: str

    def to_json(self) -> dict:
        return {"sid": self.sid, "username": self.username,
                "created_at": self.created_at, "expires_at": self.expires_at,
                "ip": self.ip, "user_agent": self.user_agent}

    @classmethod
    def from_json(cls, d: dict) -> "Session":
        return cls(sid=d["sid"], username=d["username"], created_at=d["created_at"],
                   expires_at=d["expires_at"], ip=d.get("ip", ""),
                   user_agent=d.get("user_agent", ""))


class SessionStore:
    """Server-side sessions, persisted so a restart does not log you out.

    ponytail: whole-file rewrite on every mutation — the store holds at most
    _MAX_SESSIONS rows, so an fsync-per-login is cheaper than a real store.
    Swap in SQLite if the admin identity ever grows past one user.
    """

    def __init__(self, path: Path = SESSION_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._load()

    def _load(self) -> None:
        import json
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        for d in raw.get("sessions", []):
            try:
                s = Session.from_json(d)
            except (KeyError, TypeError):
                continue
            if s.expires_at > now:
                self._sessions[s.sid] = s

    def _persist(self) -> None:
        import json
        try:
            _write_private(
                self._path,
                json.dumps({"sessions": [s.to_json() for s in self._sessions.values()]},
                           indent=2),
            )
        except OSError as e:  # pragma: no cover - deploy-dir dependent
            print(f"[auth] WARNING: could not persist sessions: {e}", file=sys.stderr)

    def create(self, username: str, ip: str, user_agent: str) -> Session:
        now = time.time()
        sid = secrets.token_urlsafe(32)
        s = Session(sid=sid, username=username, created_at=now,
                    expires_at=now + SESSION_TTL, ip=ip, user_agent=user_agent[:200])
        with self._lock:
            self._expire_locked(now)
            # Oldest-first eviction keeps the cap meaningful without ever
            # dropping the session that is being used right now.
            while len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda x: x.created_at)
                self._sessions.pop(oldest.sid, None)
            self._sessions[sid] = s
            self._persist()
        return s

    def get(self, sid: Optional[str]) -> Optional[Session]:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if s.expires_at <= time.time():
                self._sessions.pop(sid, None)
                self._persist()
                return None
            return s

    def destroy(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self._lock:
            if self._sessions.pop(sid, None) is not None:
                self._persist()

    def revoke_all(self) -> int:
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
            self._persist()
        return n

    def purge_expired(self) -> int:
        with self._lock:
            before = len(self._sessions)
            self._expire_locked(time.time())
            removed = before - len(self._sessions)
            if removed:
                self._persist()
            return removed

    def _expire_locked(self, now: float) -> None:
        for sid in [k for k, v in self._sessions.items() if v.expires_at <= now]:
            self._sessions.pop(sid, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


SESSIONS = SessionStore()

# ── CSRF tokens ──────────────────────────────────────────────────────
def csrf_token(sid: str) -> str:
    """Derive the session's CSRF token. Stateless on purpose: nothing to store,
    nothing to leak, and it dies with the session."""
    assert CONFIG is not None
    return hmac.new(CONFIG.csrf_secret.encode("utf-8"), sid.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def csrf_valid(sid: Optional[str], supplied: Optional[str]) -> bool:
    """Constant-time check of a submitted CSRF token against the session's.

    Compares bytes, not str: the header arrives latin-1 decoded, so any byte
    above 0x7F yields a non-ASCII str and hmac.compare_digest would raise
    TypeError instead of returning False (that escaped as a 500). The expected
    token is always lowercase hex, so a non-ASCII submission is rejected before
    any comparison is attempted.
    """
    if not sid or not supplied or not supplied.isascii():
        return False
    return hmac.compare_digest(csrf_token(sid).encode("ascii"),
                               supplied.encode("ascii"))


# ── Login throttling ─────────────────────────────────────────────────
class LoginThrottle:
    """Per-IP failure counter with a hard lockout window.

    A correct password during lockout is still rejected: an attacker who
    guesses right on attempt 6 must not be rewarded for persistence.

    ``reserve`` performs the check and the increment in ONE critical section, so
    the limit does not depend on the event loop happening to serialise the
    (blocking) scrypt call. ``_MAX_TRACKED`` bounds the table: the key can come
    from an attacker-controlled header when SOLVER_TRUST_PROXY is set, so
    without a cap a single client could mint unbounded distinct keys.
    """

    _MAX_TRACKED = 4096

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES,
                 lockout_s: int = LOGIN_LOCKOUT_S):
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        self._lock = threading.Lock()
        self._state: dict[str, list] = {}  # key -> [failures, last_failure_ts]

    def _remaining_locked(self, key: str, now: float) -> int:
        entry = self._state.get(key)
        if not entry:
            return 0
        failures, last = entry
        if failures < self.max_failures:
            return 0
        remaining = int(self.lockout_s - (now - last))
        if remaining <= 0:
            self._state.pop(key, None)
            return 0
        return remaining

    def locked_for(self, key: str) -> int:
        """Seconds remaining in the lockout, or 0 when the key may try again."""
        with self._lock:
            return self._remaining_locked(key, time.time())

    def reserve(self, key: str) -> int:
        """Claim one attempt. Returns seconds of lockout, or 0 when allowed.

        The caller must call this BEFORE verifying the password, so the limit
        holds regardless of how the verification is scheduled.
        """
        with self._lock:
            now = time.time()
            remaining = self._remaining_locked(key, now)
            if remaining:
                return remaining
            failures, _ = self._state.get(key, [0, 0.0])
            self._state[key] = [failures + 1, now]
            if len(self._state) > self._MAX_TRACKED:
                # Drop the least-recently-seen keys; a throttling table is
                # allowed to forget, and forgetting only ever un-throttles.
                for stale in sorted(self._state, key=lambda k: self._state[k][1])[
                        : len(self._state) - self._MAX_TRACKED]:
                    self._state.pop(stale, None)
            return 0

    def record_failure(self, key: str) -> None:
        """Refresh the timestamp of the attempt already reserved."""
        with self._lock:
            failures, _ = self._state.get(key, [0, 0.0])
            self._state[key] = [failures, time.time()]

    def reset(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


THROTTLE = LoginThrottle()


# ── Static API token (optional second layer for the API host) ────────
def _load_api_token() -> Optional[bytes]:
    """Digest of the Bearer token accepted on the API surface, or None.

    Prefer ``SOLVER_API_TOKEN_SHA256`` (hex sha256 of the token) so the
    plaintext never sits in the service environment or in `systemctl show`.
    ``SOLVER_API_TOKEN`` is accepted for convenience and hashed here.
    """
    digest_hex = (os.getenv("SOLVER_API_TOKEN_SHA256") or "").strip().lower()
    if digest_hex:
        try:
            raw = bytes.fromhex(digest_hex)
        except ValueError:
            print("[auth] SOLVER_API_TOKEN_SHA256 is not valid hex — ignoring.",
                  file=sys.stderr)
            return None
        return raw if len(raw) == 32 else None
    plain = os.getenv("SOLVER_API_TOKEN") or ""
    if plain:
        return hashlib.sha256(plain.encode("utf-8")).digest()
    return None


API_TOKEN_DIGEST = _load_api_token()
"""Digest of the accepted Bearer token, or None when none is configured."""

#: Whether the service enforces the token itself. Default is YES: the shipped
#: unit configures no token and binds a port, so failing open would leave the
#: solver surface unauthenticated whenever the reverse proxy is not the only
#: way in. SOLVER_ALLOW_UNAUTHENTICATED=1 is the explicit opt-out for a
#: single-host dev box.
API_TOKEN_REQUIRED = os.getenv("SOLVER_ALLOW_UNAUTHENTICATED") != "1"

#: Endpoints that carry the Bearer token when enforcement is on. /health is
#: included deliberately: a public liveness endpoint tells an anonymous scanner
#: that the service exists, which version it is and that it is worth attacking.
#: Monitors must send the token like any other caller.
API_TOKEN_PATHS = ("/solve", "/logs", "/status", "/health")


def api_token_valid(authorization: Optional[str]) -> bool:
    """Check an ``Authorization: Bearer <token>`` header in constant time."""
    if API_TOKEN_DIGEST is None:
        # No token configured. Refuse unless the operator explicitly opted out,
        # so a misconfigured unit cannot come up unauthenticated.
        return not API_TOKEN_REQUIRED
    if not authorization:
        return False
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(hashlib.sha256(value.strip().encode("utf-8")).digest(),
                               API_TOKEN_DIGEST)


# ── CLI: hash an existing password / generate a strong one ───────────
def _cli(argv: list[str]) -> int:
    import getpass

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.split("Threat model")[0].strip())
        print("\nusage:\n"
              "  python -m web.auth hash            # prompt, print a digest\n"
              "  python -m web.auth hash <password> # non-interactive (shell history!)\n"
              "  python -m web.auth generate        # print a strong random password\n"
              "  python -m web.auth revoke          # invalidate every live session")
        return 0

    cmd = argv[0]

    if cmd == "generate":
        print(secrets.token_urlsafe(24))
        return 0

    if cmd == "revoke":
        n = SESSIONS.revoke_all()
        print(f"revoked {n} session(s)")
        return 0

    if cmd == "hash":
        pw = argv[1] if len(argv) > 1 else getpass.getpass("Password: ")
        if len(argv) > 1:
            print("WARNING: password given on the command line lands in shell history.",
                  file=sys.stderr)
        try:
            digest = hash_password(pw)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(digest)
        print("\nPut this in the service environment (and drop the plaintext):\n"
              "  ADMIN_PASSWORD_HASH='<the line above>'", file=sys.stderr)
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))

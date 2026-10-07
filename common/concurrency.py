"""Dynamic concurrency budget for the browser solvers.

Replaces the per-solver `_solve_lock` (an `asyncio.Lock` that serialized
every solve). What bounds us instead is machine resources — each solve
launches one CloakBrowser instance (~1 GB RSS measured on this host, across
its ~10 helper processes).

The limit is therefore derived at runtime, not hardcoded:

    limit = min(cpu_budget, ram_budget, hard_cap)

    cpu_budget = cpu_count                     (1 browser per core is plenty —
                                                a solve is I/O + JS bound)
    ram_budget = (MemAvailable - reserve) / PER_BROWSER_MB
    reserve    = 10% of MemTotal (keep the host and the server responsive)

Override with ``SOLVER_MAX_CONCURRENT`` (e.g. ``2`` on a small box, ``0`` to
disable the cap entirely — NOT recommended: an unbounded burst can OOM the
host, and the OOM killer takes the smallest process, i.e. the server).

Usage in a solver::

    from common.concurrency import solve_slot

    async with solve_slot():
        ...launch browser, solve...

The semaphore is created lazily inside the running loop (asyncio primitives
bind to a loop in older Pythons) and cached per process.
"""
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

# Measured on this host: one CloakBrowser instance (headless, one page) peaks
# around 1 GB RSS across its ~10 helper processes. Round up for headroom.
PER_BROWSER_MB = 1200

# Absolute ceiling regardless of how big the machine is. Solves are short and
# the caller is usually a script that does not need 40 at once; a huge burst
# mostly multiplies per-IP rate-limit pressure.
HARD_CAP = 16

_sem: asyncio.Semaphore | None = None
_limit: int | None = None


def _meminfo_mb() -> tuple[int, int]:
    """Return (MemAvailable, MemTotal) in MB, or (0, 0) when unavailable."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
        return info.get("MemAvailable", 0) // 1024, info.get("MemTotal", 0) // 1024
    except (OSError, ValueError, IndexError):
        return 0, 0


def _cgroup_limit_mb() -> int:
    """The memory ceiling of the current cgroup, in MB (0 = none found).

    Why this matters: under a supervisor that caps the service (systemd
    MemoryMax, a background worker scope, Docker --memory, a Kubernetes
    pod limit), /proc/meminfo still reports the WHOLE HOST. Budgeting off
    MemAvailable alone then overshoots the cgroup and the kernel OOM killer
    starts shooting browsers mid-solve — the worst failure mode because it
    looks like flaky captchas, not a resource problem. The cgroup limit is
    the number that actually applies, so take the minimum of both.
    """
    candidates = []
    # cgroup v2: own limit, then walk up toward the root
    try:
        path = "/sys/fs/cgroup"
        rel = ""
        with open("/proc/self/cgroup") as fh:
            for line in fh:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[0] == "0":  # v2 unified
                    rel = parts[2]
                    break
        d = os.path.join(path, rel.lstrip("/")) if rel else path
        while True:
            mf = os.path.join(d, "memory.max")
            if os.path.exists(mf):
                with open(mf) as fh:
                    val = fh.read().strip()
                if val != "max":
                    candidates.append(int(val) // (1024 * 1024))
            parent = os.path.dirname(d)
            if parent == d or d == path:
                break
            d = parent
    except (OSError, ValueError):
        pass
    # cgroup v1 fallback
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as fh:
            v = int(fh.read().strip())
        if v < (1 << 60):  # v1 uses a huge sentinel for "unlimited"
            candidates.append(v // (1024 * 1024))
    except (OSError, ValueError):
        pass
    return min(candidates) if candidates else 0


def compute_limit() -> int:
    """Derive the concurrency limit from live system resources."""
    override = os.getenv("SOLVER_MAX_CONCURRENT")
    if override is not None:
        try:
            n = int(override)
        except ValueError:
            log.warning("SOLVER_MAX_CONCURRENT=%r is not an int; ignoring", override)
        else:
            log.info("concurrency limit=%s (SOLVER_MAX_CONCURRENT override)",
                     n if n > 0 else "uncapped")
            return n if n > 0 else 0  # 0 = uncapped (explicit opt-in)

    cpu = os.cpu_count() or 1
    avail_mb, total_mb = _meminfo_mb()
    cg_mb = _cgroup_limit_mb()

    # The usable ceiling is whichever is smaller: what the machine can spare,
    # or what the cgroup actually allows.
    if cg_mb:
        ceiling_mb = min(avail_mb, cg_mb) if avail_mb else cg_mb
        basis_mb = cg_mb
    else:
        ceiling_mb, basis_mb = avail_mb, total_mb

    if ceiling_mb:
        reserve_mb = max(512, int(basis_mb * 0.10))   # keep 10% headroom
        usable = max(0, ceiling_mb - reserve_mb)
        ram_budget = usable // PER_BROWSER_MB
    else:
        ram_budget = cpu    # no memory info at all: fall back to CPU

    limit = max(1, min(cpu, ram_budget, HARD_CAP))
    log.info("concurrency limit=%d (cpu=%d, avail=%dMB, cgroup=%dMB, "
             "ram_budget=%d, cap=%d)",
             limit, cpu, avail_mb, cg_mb, ram_budget, HARD_CAP)
    return limit


def get_limit() -> int:
    global _limit
    if _limit is None:
        _limit = compute_limit()
    return _limit


def _get_sem() -> asyncio.Semaphore | None:
    """The process-wide semaphore (None when explicitly uncapped)."""
    global _sem
    if _sem is None:
        limit = get_limit()
        if limit <= 0:
            log.warning("solve concurrency is UNCAPPED (SOLVER_MAX_CONCURRENT=0)")
            return None
        _sem = asyncio.Semaphore(limit)
    return _sem


class _Slot:
    """Async context manager acquiring one solve slot (no-op when uncapped)."""

    async def __aenter__(self):
        sem = _get_sem()
        if sem is not None:
            await sem.acquire()
        return self

    async def __aexit__(self, *exc):
        sem = _get_sem()
        if sem is not None:
            sem.release()
        return False


def solve_slot() -> _Slot:
    """Acquire a solve slot for the duration of one browser solve."""
    return _Slot()

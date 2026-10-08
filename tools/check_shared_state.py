#!/usr/bin/env python3
"""Regression: the API and the console must share ONE module of state.

The bug this pins down, reproduced exactly as the deployment runs it
(``python server.py``, so the app is module ``__main__``):

  ``web/routes.py`` did ``import server``, which creates a SECOND module object
  with its own ``_solve_log`` / ``_solve_current`` / ``_solve_total``. A solve
  through ``/solve`` wrote to ``__main__``; the console read the empty copy.
  ``/logs`` and the console then disagreed about the same event, and the
  lifetime counter stayed at zero.

Why this test does not run a real solve: launching a browser takes ~30s and
needs a display, which makes the check slow and environment-dependent. The
invariant that broke is module identity, and that is fully observable without
solving anything — a fake event written through the API's own logger must be
visible through the console's reader. Fast, deterministic, no network.

    python tools/check_shared_state.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update(
    SOLVER_ADMIN_USER="admin",
    ADMIN_PASSWORD="shared-state-password-123",
    SOLVER_CSRF_SECRET="s" * 64,
    SOLVER_ALLOW_UNAUTHENTICATED="1",
    SOLVER_SESSION_FILE=str(ROOT / ".tmp-state-sessions.json"),
    SOLVER_ADMIN_HASH_FILE=str(ROOT / ".tmp-state-admin.json"),
)
os.environ.pop("PYTHONPATH", None)
os.environ.pop("PYTHONHOME", None)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}{f' — {detail}' if detail and not condition else ''}")


def load_as_main() -> types.ModuleType:
    """Load server.py the way `python server.py` does: as module __main__.

    The entrypoint block is stripped so nothing binds a port.
    """
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    source = source[: source.index('if __name__ == "__main__":')]
    module = types.ModuleType("__main__")
    module.__file__ = str(ROOT / "server.py")
    sys.modules["__main__"] = module
    exec(compile(source, "server.py", "exec"), module.__dict__)
    return module


def main() -> int:
    print("── loading server.py as __main__ (the deployment's shape) ──")
    app_main = load_as_main()

    # web.routes is imported by server.py at module level, so this is the same
    # import path the running service uses.
    from web import routes  # noqa: E402

    resolved = routes._app_module()

    print("\n── module identity ──")
    check("web.routes resolves to the running __main__ module",
          resolved is app_main,
          f"resolved __name__={resolved.__name__!r} file={getattr(resolved, '__file__', None)}")
    check("resolved module is not a duplicate 'server' import",
          resolved.__name__ != "server" or resolved is app_main,
          f"resolved __name__={resolved.__name__!r}")

    print("\n── shared ring buffer ──")
    check("ring buffer object is shared",
          resolved._solve_log is app_main._solve_log,
          "the console reads a different deque than the API writes")
    check("in-flight map object is shared",
          resolved._solve_current is app_main._solve_current)

    # The symptom, end to end: write through the API's logger, read through the
    # console's accessor. This is what the user saw fail.
    print("\n── the reported symptom ──")
    before = len(app_main._solve_log)
    app_main._log_solve(
        "turnstile", "0xTESTKEY", "https://example.com",
        {"token": "fake-token", "elapsed": 1.23, "method": "test"},
    )
    through_api = len(app_main._solve_log)
    through_console = len(resolved._solve_log)

    print(f"  /logs  sees : {through_api} (was {before})")
    print(f"  console sees: {through_console}")
    check("a logged solve is visible to the console reader",
          through_console == through_api == before + 1,
          f"logs={through_api} console={through_console}")
    check("the lifetime counter is shared",
          resolved._solve_total == app_main._solve_total > 0,
          f"console={resolved._solve_total} logs={app_main._solve_total}")

    event = resolved._solve_log[0]
    check("the event is well formed for the console payload",
          event.get("type") == "turnstile" and event.get("success") is True
          and isinstance(event.get("timestamp"), float),
          f"got {event}")

    print("\n" + "─" * 52)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for tmp in (ROOT / ".tmp-state-sessions.json", ROOT / ".tmp-state-admin.json"):
            tmp.unlink(missing_ok=True)

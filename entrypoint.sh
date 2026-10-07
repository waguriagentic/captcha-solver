#!/usr/bin/env bash
# Container entrypoint: bring up the virtual display, then exec the solver.
#
# The headed solvers (reCAPTCHA, hCaptcha, BotGuard, Cloudflare, PerimeterX,
# AWS WAF, Akamai) need an X display. In the host deployment that is a
# persistent Xvfb unit; here it is started inside the container, before the
# server, so a headed launch never races the display.
#
# Do NOT use `xvfb-run`: its random /tmp/xvfb-run.XXXXXX/Xauthority path is not
# reliably inherited down the subprocess chain (server -> browser driver ->
# chrome), and headed launches then fail with "launched a headed browser
# without having a XServer running" even though DISPLAY is set. A fixed display
# number with the server listening on its unix socket only is stable.
set -euo pipefail

DISPLAY_NUM="${DISPLAY:-:99}"
SCREEN="${SCREEN:-1920x1080x24}"
SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM#:}"

if [ ! -e "$SOCKET" ]; then
    echo "[entrypoint] starting Xvfb on $DISPLAY_NUM ($SCREEN)"
    # -nolisten tcp: the display is only reachable through the container's own
    # unix socket. No -auth: the container is single-tenant and the socket is
    # not reachable from outside it.
    Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
    for _ in $(seq 1 40); do
        [ -e "$SOCKET" ] && break
        sleep 0.25
    done
    if [ ! -e "$SOCKET" ]; then
        echo "[entrypoint] Xvfb did not come up; see /tmp/xvfb.log" >&2
        cat /tmp/xvfb.log >&2 || true
        exit 1
    fi
fi

export DISPLAY="$DISPLAY_NUM"

# Fonts may arrive from a mount; make them visible to fontconfig.
if ! fc-cache -f >/dev/null 2>&1; then
    echo "[entrypoint] warning: fc-cache failed; font enumeration may be incomplete" >&2
fi

echo "[entrypoint] browser binary: ${CLOAKBROWSER_BINARY_PATH:-<not pinned>}"
exec python3 /app/server.py

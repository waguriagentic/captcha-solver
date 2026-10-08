#!/usr/bin/env bash
# Install/refresh the captcha-solver systemd services.
#
#   1. stop any running instance
#   2. install + start the persistent Xvfb unit (stable Xauthority path)
#   3. install + start the solver unit
#   4. verify health
#
# The example units in this directory use /opt/captcha-solver and User=captcha
# as placeholders; this script rewrites them to the actual checkout path and
# the invoking user before installing.
#
# The dashboard and the solver API are the same process behind two hostnames
# (dash.* and api.*). Only ONE backend is needed: point both tunnel ingress
# rules at 127.0.0.1:8877 and let the app decide by Host header. Config for
# that lives in the tunnel and the reverse proxy, not here — see the
# "Admin dashboard" section of the README.
#
# Local binary pin: if a `.cloakbrowser-pin` sidecar exists in the repo root
# (gitignored, machine-specific), its content is injected as
# CLOAKBROWSER_BINARY_PATH so a wrapper auto-update cannot swap the browser
# build under a validated deployment. Without the sidecar the unit ships the
# commented-out example line only.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
XVFB_UNIT=captcha-solver-xvfb.service
UNIT=captcha-solver.service
PIN_FILE="$REPO/.cloakbrowser-pin"

SUDO="sudo"
[ -n "${SUDO_ASKPASS:-}" ] && SUDO="sudo -A"

render() {
    sed -e "s|/opt/captcha-solver|$REPO|g" \
        -e "s|^User=captcha$|User=$RUN_USER|" \
        -e "s|^Group=captcha$|Group=$RUN_USER|" \
        "$1"
}

echo "== 1. stop any running instances =="
$SUDO systemctl stop captcha-solver 2>/dev/null || true

# Free :99 from a manually started Xvfb so the unit can own the display.
if ! systemctl is-active --quiet "$XVFB_UNIT" 2>/dev/null; then
    if [ -e /tmp/.X11-unix/X99 ]; then
        echo "  freeing :99 from a manual Xvfb"
        pid=$(pgrep -f "Xvfb :99" | head -1 || true)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
        sleep 2
    fi
fi

echo
echo "== 2. install the units (path/user adapted) =="
render "$REPO/deploy/$XVFB_UNIT" | $SUDO tee "/etc/systemd/system/$XVFB_UNIT" >/dev/null

if [ -f "$PIN_FILE" ]; then
    pin="$(head -1 "$PIN_FILE")"
    echo "  injecting binary pin from .cloakbrowser-pin"
    render "$REPO/deploy/$UNIT" \
        | sed "s|^# Environment=CLOAKBROWSER_BINARY_PATH=.*|Environment=CLOAKBROWSER_BINARY_PATH=$pin|" \
        | $SUDO tee "/etc/systemd/system/$UNIT" >/dev/null
else
    render "$REPO/deploy/$UNIT" | $SUDO tee "/etc/systemd/system/$UNIT" >/dev/null
fi

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$XVFB_UNIT" >/dev/null
$SUDO systemctl enable "$UNIT" >/dev/null
$SUDO systemctl restart "$XVFB_UNIT"
sleep 2
$SUDO systemctl restart "$UNIT"

echo
echo "== 3. wait for health =="
# /health is token-gated like every other solver endpoint, and under host
# isolation a bare 127.0.0.1 request is refused as misdirected. So the probe
# needs both a Host the policy accepts and the Bearer token. Read them from the
# environment file when it exists, else from the caller's environment.
ENV_FILE="${SOLVER_ENV_FILE:-$REPO/.env}"
if [ -f "$ENV_FILE" ]; then
    PROBE_HOST="${SOLVER_PROBE_HOST:-$(sed -n 's/^SOLVER_API_HOST=//p' "$ENV_FILE" | head -1)}"
    PROBE_TOKEN="${SOLVER_PROBE_TOKEN:-$(sed -n 's/^SOLVER_API_TOKEN=//p' "$ENV_FILE" | head -1)}"
fi
PROBE_HOST="${PROBE_HOST:-${SOLVER_PROBE_HOST:-}}"
PROBE_TOKEN="${PROBE_TOKEN:-${SOLVER_PROBE_TOKEN:-}}"

if [ -n "$PROBE_HOST" ] && [ -n "$PROBE_TOKEN" ]; then
    echo "  probing http://127.0.0.1:8877/health as Host: $PROBE_HOST (with token)"
    probe() {
        curl -sf -m 3 -H "Host: $PROBE_HOST" \
             -H "Authorization: Bearer $PROBE_TOKEN" \
             "http://127.0.0.1:8877/health"
    }
else
    # No isolation and no token configured: the service answers directly.
    probe() { curl -sf -m 3 "http://127.0.0.1:8877/health"; }
fi

for i in $(seq 1 25); do
    if probe >/dev/null 2>&1; then
        echo "healthy after ${i}s"
        break
    fi
    sleep 1
done
if ! probe; then
    echo "  health probe failed — if isolation is on, set SOLVER_PROBE_HOST and"
    echo "  SOLVER_PROBE_TOKEN, or point SOLVER_ENV_FILE at your env file."
fi
echo

echo
echo "== 4. unit states =="
$SUDO systemctl --no-pager status "$XVFB_UNIT" | head -4
echo
$SUDO systemctl --no-pager status "$UNIT" | head -6

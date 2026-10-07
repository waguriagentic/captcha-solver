#!/usr/bin/env bash
# Start a persistent Xvfb display for the captcha-solver with a STABLE
# Xauthority path.
#
# Why not `xvfb-run -a`: it creates a random /tmp/xvfb-run.XXXXXX/Xauthority
# per invocation and relies on the environment surviving down the subprocess
# chain (server -> browser driver -> chrome). In practice the driver re-execs
# and the auth file path is not always inherited, so headed launches die with
# "launched a headed browser without having a XServer running". A fixed path +
# explicit env in the service unit is stable.
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-:99}"
SCREEN="${SCREEN:-1920x1080x24}"
# Default lives beside the checkout (.cache/) so it matches the systemd unit
# template; override with XAUTH_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_DIR="${XAUTH_DIR:-$SCRIPT_DIR/.cache}"
AUTH_FILE="$AUTH_DIR/Xauthority"

mkdir -p "$AUTH_DIR"
chmod 700 "$AUTH_DIR"

# Is the display already up?
if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ]; then
    echo "display $DISPLAY_NUM already running"
else
    # Create the auth cookie first so Xvfb can use it.
    touch "$AUTH_FILE"
    chmod 600 "$AUTH_FILE"
    xauth -f "$AUTH_FILE" add "$DISPLAY_NUM" . "$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp -auth "$AUTH_FILE" \
        > "$AUTH_DIR/xvfb.log" 2>&1 &
    echo $! > "$AUTH_DIR/xvfb.pid"
    sleep 2
    echo "started Xvfb $DISPLAY_NUM (pid $(cat "$AUTH_DIR/xvfb.pid"))"
fi

echo "DISPLAY=$DISPLAY_NUM"
echo "XAUTHORITY=$AUTH_FILE"

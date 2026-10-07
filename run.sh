#!/usr/bin/env bash
set -euo pipefail
# Activate your virtualenv. Override VENV to point at yours:
#   VENV=/path/to/venv ./run.sh
source "${VENV:-.venv}/bin/activate"

# Headful solvers need an X display. Do NOT use `xvfb-run -a`: its random
# /tmp/xvfb-run.XXXXXX/Xauthority path is not reliably inherited down the
# subprocess chain, so headed launches die with "launched a headed browser
# without having a XServer running". start-xvfb.sh starts a persistent Xvfb
# with a stable auth path and prints the two env vars below.
if [ "${BROWSER_HEADLESS:-0}" = "0" ] && [ -z "${DISPLAY:-}" ]; then
    echo "BROWSER_HEADLESS=0 and DISPLAY is unset — start the display first:" >&2
    echo "    ./start-xvfb.sh   # prints DISPLAY=... XAUTHORITY=..." >&2
    exit 1
fi

exec python3 server.py "$@"

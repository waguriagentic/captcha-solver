#!/usr/bin/env bash
set -euo pipefail
# Activate your virtualenv. Override VENV to point at yours:
#   VENV=/path/to/venv ./run.sh
source "${VENV:-.venv}/bin/activate"
exec python3 server.py "$@"

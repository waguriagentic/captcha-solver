#!/usr/bin/env bash
# Verify the admin console login through the real Cloudflare edge.
# Hairpin NAT: must go out through an external egress.
set -uo pipefail

PROXY=$(grep 'PROXY=' "$HOME/.bashrc" | head -1 | sed 's/export //;s/^PROXY=//')
ADMIN_USER=$(grep -m1 '^SOLVER_ADMIN_USER=' "$HOME/SAAS/captcha-solver/.env.production" | cut -d= -f2-)
ADMIN_PW=$(cat "$HOME/SAAS/captcha-solver-secrets/admin_password.txt")
DASH="solver.sonogami.com"

echo "=== login via edge (user: $ADMIN_USER) ==="
curl -s -m 30 -x "$PROXY" -D- -o /tmp/edge_login.json \
  -X POST "https://$DASH/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys; print(json.dumps({'username':sys.argv[1],'password':sys.argv[2]}))" "$ADMIN_USER" "$ADMIN_PW")" \
  2>&1 | grep -iE "^HTTP/|set-cookie" | head -3
echo "--- body ---"
head -c 250 /tmp/edge_login.json
echo

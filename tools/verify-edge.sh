#!/usr/bin/env bash
# Verify the deployed solver through the real Cloudflare edge.
#
# Hairpin NAT means the host cannot reach its own public hostname, so every
# probe goes out through an external egress (the residential proxy). Checks the
# two-hostname split: each origin must serve exactly one credential type and
# refuse the other's paths.
set -uo pipefail

TOKEN=$(cat "$HOME/SAAS/captcha-solver-secrets/api_token.txt")
PROXY=$(grep 'PROXY=' "$HOME/.bashrc" | head -1 | sed 's/export //;s/^PROXY=//')
DASH="solver.sonogami.com"
API="api-solver.sonogami.com"

probe() {  # probe <label> <host> <path> [extra curl args...]
    local label="$1" host="$2" path="$3"; shift 3
    printf "%-46s " "$label"
    curl -s -m 25 -x "$PROXY" -o /dev/null -w "%{http_code}\n" "https://$host$path" "$@" 2>&1
    sleep 2
}

echo "== API host (Bearer token) =="
probe "/health with token"        "$API" /health   -H "Authorization: Bearer $TOKEN"
probe "/health without token"     "$API" /health
probe "/solve on dashboard host"  "$DASH" /solve   -H "Authorization: Bearer $TOKEN"

echo
echo "== Dashboard host (session cookie) =="
probe "/ (landing)"               "$DASH" /
probe "/docs"                     "$DASH" /docs
probe "/login"                    "$DASH" /login

echo
echo "== Cross-surface refusals (expect 404) =="
probe "/api/v1/overview on API"   "$API" /api/v1/overview
probe "/swagger on dashboard"     "$DASH" /swagger

echo
echo "== Solve through the edge =="
curl -s -m 90 -x "$PROXY" -X POST "https://$API/solve" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"turnstile","sitekey":"1x00000000000000000000AA","url":"https://example.com","timeout_s":60}' \
  | head -c 300
echo

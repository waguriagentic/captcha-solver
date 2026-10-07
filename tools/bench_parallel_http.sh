#!/usr/bin/env bash
# Concurrency smoke: N parallel solves against the local solver, reporting each
# result. Used to verify the browser launch is no longer serialized behind the
# license runtime's key handshake.
set -uo pipefail

TOKEN=$(cat "$HOME/SAAS/captcha-solver-secrets/api_token.txt")
N="${1:-8}"
OUT=$(mktemp -d)

start=$(date +%s.%N)
for i in $(seq 1 "$N"); do
  curl -s -m 120 -X POST http://127.0.0.1:8877/solve \
    -H "Host: api-solver.sonogami.com" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"type":"turnstile","sitekey":"1x00000000000000000000AA","url":"https://example.com","timeout_s":90}' \
    -o "$OUT/$i.json" &
done
wait
end=$(date +%s.%N)

echo "wall: $(echo "$end - $start" | bc)s"
for i in $(seq 1 "$N"); do
  python3 -c "
import json,sys
try:
    d=json.load(open('$OUT/$i.json'))
    print(f'  #$i solved={d.get(\"solved\")} elapsed={d.get(\"elapsed\")}s err={d.get(\"error\")}')
except Exception as e:
    print(f'  #$i ERR {e}')
"
done
rm -rf "$OUT"

#!/usr/bin/env bash
# Prove each verification assertion can actually FAIL.
#
# A recipe whose checks have never been observed failing is untested. Each case
# mutates a copy of the deployment artifacts to violate one invariant, runs the
# corresponding check, and requires a non-zero exit. The working tree is
# restored from a temp copy at the end.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
BACKUP=$(mktemp -d)
cp compose.coolify.yaml entrypoint.sh .env.production "$BACKUP/" 2>/dev/null || true
trap 'cp "$BACKUP"/compose.coolify.yaml "$REPO/" 2>/dev/null; cp "$BACKUP"/entrypoint.sh "$REPO/" 2>/dev/null; cp "$BACKUP"/.env.production "$REPO/" 2>/dev/null; rm -rf "$BACKUP"' EXIT

pass=0; fail=0
check_fails() {  # check_fails <label> <command>
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  [BAD]  $label — check PASSED on a violation (assertion is dead)"
        fail=$((fail+1))
    else
        echo "  [good] $label — check correctly failed"
        pass=$((pass+1))
    fi
}

echo "== compose invariants =="

# 1. a relative bind mount (resolves against Coolify's own dir, not the checkout)
python3 - <<'PY'
import pathlib
p = pathlib.Path('compose.coolify.yaml'); t = p.read_text()
p.write_text(t.replace('${CAPTCHA_BROWSER_DIR:-/opt/captcha-solver/browser}:/opt/cloakbrowser:ro',
                       './browser:/opt/cloakbrowser:ro'))
PY
check_fails "relative bind mount" python3 -c "import pathlib,re,sys; t=pathlib.Path('compose.coolify.yaml').read_text(); bad=[m for m in re.findall(r'^\s*-\s*(\S+)', t, re.M) if m.startswith(('./','../'))]; sys.exit(1) if bad else sys.exit(0)"
cp "$BACKUP"/compose.coolify.yaml .

# 2. a wildcard publish (exposes the service directly, bypassing the tunnel)
python3 - <<'PY'
import pathlib
p = pathlib.Path('compose.coolify.yaml'); t = p.read_text()
p.write_text(t.replace('- "127.0.0.1:8877:8877"', '- "0.0.0.0:8877:8877"'))
PY
check_fails "wildcard publish" python3 -c "import pathlib,sys; t=pathlib.Path('compose.coolify.yaml').read_text(); sys.exit(0) if ('127.0.0.1:8877:8877' in t and 'SOLVER_BIND_HOST: 0.0.0.0' in t) else sys.exit(1)"
cp "$BACKUP"/compose.coolify.yaml .

# 3. a loopback bind inside the container (the publish then cannot reach it)
python3 - <<'PY'
import pathlib
p = pathlib.Path('compose.coolify.yaml'); t = p.read_text()
p.write_text(t.replace('SOLVER_BIND_HOST: 0.0.0.0', 'SOLVER_BIND_HOST: 127.0.0.1'))
PY
check_fails "loopback bind in container" python3 -c "import pathlib,sys; t=pathlib.Path('compose.coolify.yaml').read_text(); sys.exit(0) if ('127.0.0.1:8877:8877' in t and 'SOLVER_BIND_HOST: 0.0.0.0' in t) else sys.exit(1)"
cp "$BACKUP"/compose.coolify.yaml .

# 4. malformed YAML (structure error, not just a bad value)
printf 'services:\n  solver:\n    - this is not valid\n' > compose.coolify.yaml
check_fails "malformed compose" bash -c 'tmp=$(mktemp -d) && printf "# p\n" > "$tmp/env" && CAPTCHA_ENV_FILE="$tmp/env" docker compose -f compose.coolify.yaml config --quiet'
cp "$BACKUP"/compose.coolify.yaml .

echo "== env-escaping invariant =="

# 5. an unescaped $ in the env file (compose truncates the value at the first $)
printf 'ADMIN_PASSWORD_HASH=scrypt$32768$8$1$deadbeef$cafe\n' > .env.production
check_fails "unescaped \$ in env" python3 -c "import pathlib,sys; p=pathlib.Path('.env.production'); t=p.read_text() if p.exists() else ''; d=chr(36); bad=[l.split('=')[0] for l in t.splitlines() if '=' in l and not l.strip().startswith('#') and d in l and d*2 not in l]; sys.exit(1) if bad else sys.exit(0)"
cp "$BACKUP"/.env.production . 2>/dev/null || rm -f .env.production

echo "== deploy-file invariant =="

# 6. a missing container artifact
mv entrypoint.sh entrypoint.sh.hold
check_fails "missing entrypoint.sh" python3 -c "import pathlib,sys; missing=[p for p in ['Dockerfile','entrypoint.sh','compose.coolify.yaml'] if not pathlib.Path(p).exists()]; sys.exit(1) if missing else sys.exit(0)"
mv entrypoint.sh.hold entrypoint.sh

echo "== license-patch invariant =="

# 7. an unpatched browser build must be rejected
check_fails "unpatched browser build" env CLOAKBROWSER_BINARY_PATH="$HOME/.cloakbrowser/chromium-154.0.8037.57.1-pro/chrome.orig-backup" python3 tools/check_license_patch.py

echo
echo "correctly-failed: $pass   dead-assertions: $fail"
[ "$fail" -eq 0 ] || exit 1

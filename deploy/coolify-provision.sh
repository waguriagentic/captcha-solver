#!/usr/bin/env bash
# Provision the captcha-solver application in Coolify.
#
# Creates the application, then applies the settings the deployment needs:
#
#   1. build_pack = dockercompose, compose path = /compose.coolify.yaml
#   2. raw compose mode ON — the default parser rewrites every service's
#      env_file to Coolify's own (empty) `.env`, so the service would boot with
#      no configuration at all. Raw mode deploys the committed file verbatim.
#   3. no FQDN — ingress belongs to the dedicated Cloudflare Tunnel, not to
#      Traefik. The tunnel dials 127.0.0.1:8877 directly.
#   4. environment variables the compose interpolates, so `docker compose`
#      resolves the host paths at deploy time.
#
# Run on the host that runs Coolify. Requires the Coolify container and its
# Postgres to be up.
set -euo pipefail

SUDO="sudo"
[ -n "${SUDO_ASKPASS:-}" ] && SUDO="sudo -A"

APP_NAME="${APP_NAME:-captcha-solver}"
PROJECT_UUID="${PROJECT_UUID:-}"
ENV_NAME="${ENV_NAME:-production}"
GITHUB_APP_ID="${GITHUB_APP_ID:-1}"          # coolify-sonogami (installation id 162968886)
REPO="${REPO:-waguriagentic/captcha-solver}"
BRANCH="${BRANCH:-main}"
COMPOSE_LOCATION="${COMPOSE_LOCATION:-/compose.coolify.yaml}"

# Host paths the compose interpolates. Override any of these on the command
# line. The defaults below assume a checkout at ~/SAAS/captcha-solver; the
# browser build is whatever `~/.cloakbrowser/` holds on the deployment host.
CAPTCHA_ENV_FILE="${CAPTCHA_ENV_FILE:-$HOME/SAAS/captcha-solver/.env.production}"
CAPTCHA_BROWSER_DIR="${CAPTCHA_BROWSER_DIR:-$HOME/.cloakbrowser/chromium-154.0.8037.57.1-pro}"
CAPTCHA_FONTS_DIR="${CAPTCHA_FONTS_DIR:-$HOME/SAAS/captcha-solver-host/fonts}"
CAPTCHA_GEOIP_DIR="${CAPTCHA_GEOIP_DIR:-$HOME/SAAS/captcha-solver-host/geoip}"
CAPTCHA_ARKOSE_MODELS_DIR="${CAPTCHA_ARKOSE_MODELS_DIR:-$HOME/SAAS/captcha-solver-host/arkose-models}"
CAPTCHA_APIKEY_FILE="${CAPTCHA_APIKEY_FILE:-$HOME/SAAS/captcha-solver-host/apikey.txt}"

psql() { $SUDO docker exec -i coolify-db psql -U coolify -d coolify -tAc "$1"; }

echo "== 1. resolve project + server =="
if [ -z "$PROJECT_UUID" ]; then
    PROJECT_UUID=$(psql "SELECT uuid FROM projects WHERE name = 'Sonogami' ORDER BY id LIMIT 1;")
fi
[ -n "$PROJECT_UUID" ] || { echo "project uuid not found; set PROJECT_UUID"; exit 1; }
SERVER_UUID=$(psql "SELECT uuid FROM servers ORDER BY id LIMIT 1;")
echo "  project: $PROJECT_UUID"
echo "  server:  $SERVER_UUID"

echo
echo "== 2. create the application =="
EXISTING=$(psql "SELECT uuid FROM applications WHERE name = '$APP_NAME' AND deleted_at IS NULL;")
if [ -n "$EXISTING" ]; then
    echo "  application already exists: $EXISTING"
    APP_UUID="$EXISTING"
else
    # Created through Eloquent, not raw SQL: the model's events create the
    # companion rows (settings, webhook secrets, parser version) that a raw
    # INSERT would skip, leaving an application that cannot deploy.
    APP_UUID=$($SUDO docker exec -i coolify php /var/www/html/artisan tinker --execute="
\$project = App\Models\Project::whereUuid('$PROJECT_UUID')->first();
\$env = \$project->environments()->where('name', '$ENV_NAME')->first();
\$server = App\Models\Server::orderBy('id')->first();
\$app = new App\Models\Application();
\$app->name = '$APP_NAME';
\$app->git_repository = '$REPO';
\$app->git_branch = '$BRANCH';
\$app->build_pack = 'dockercompose';
\$app->docker_compose_location = '$COMPOSE_LOCATION';
\$app->base_directory = '/';
\$app->health_check_path = '/';
\$app->health_check_enabled = false;
\$app->fqdn = '';
\$app->environment_id = \$env->id;
\$app->destination_type = App\Models\StandaloneDocker::class;
\$app->destination_id = \$server->destinations()->first()->id;
\$app->source_type = App\Models\GithubApp::class;
\$app->source_id = $GITHUB_APP_ID;
\$app->private_key_id = null;
\$app->save();
echo \$app->uuid;
" | tail -1)
    echo "  created: $APP_UUID"
fi
[ -n "$APP_UUID" ] || { echo "application uuid not resolved"; exit 1; }

echo
echo "== 3. apply deployment settings =="
$SUDO docker exec -i coolify php /var/www/html/artisan tinker --execute="
\$app = App\Models\Application::whereUuid('$APP_UUID')->firstOrFail();
\$app->build_pack = 'dockercompose';
\$app->docker_compose_location = '$COMPOSE_LOCATION';
\$app->fqdn = '';
\$app->health_check_enabled = false;
// Raw mode: deploy the committed compose verbatim. Without it the parser
// rewrites env_file to Coolify's own .env and the service boots unconfigured.
\$app->settings->is_raw_compose_deployment_enabled = true;
\$app->settings->save();
\$app->save();
echo 'settings applied';
" | tail -1

echo
echo "== 4. seed the compose-interpolated variables =="
# These must be visible to BOTH build and runtime: `docker compose build`
# interpolates the whole file, so a runtime-only variable is absent during the
# build and a required-variable guard would abort the deploy.
seed_var() {
    local key="$1" value="$2"
    $SUDO docker exec -i coolify php /var/www/html/artisan tinker --execute="
\$app = App\Models\Application::whereUuid('$APP_UUID')->firstOrFail();
\$existing = \$app->environment_variables()->where('key', '$key')->first();
if (\$existing) {
    \$existing->value = '$value';
    \$existing->is_buildtime = true;
    \$existing->is_runtime = true;
    \$existing->save();
    echo 'updated';
} else {
    \$var = new App\Models\EnvironmentVariable();
    \$var->key = '$key';
    \$var->value = '$value';
    \$var->resourceable_type = App\Models\Application::class;
    \$var->resourceable_id = \$app->id;
    \$var->is_buildtime = true;
    \$var->is_runtime = true;
    \$var->save();
    echo 'created';
}
" | tail -1
}
printf "  CAPTCHA_ENV_FILE          -> %s (%s)\n" "$CAPTCHA_ENV_FILE" "$(seed_var CAPTCHA_ENV_FILE "$CAPTCHA_ENV_FILE")"
printf "  CAPTCHA_BROWSER_DIR       -> %s (%s)\n" "$CAPTCHA_BROWSER_DIR" "$(seed_var CAPTCHA_BROWSER_DIR "$CAPTCHA_BROWSER_DIR")"
printf "  CAPTCHA_FONTS_DIR         -> %s (%s)\n" "$CAPTCHA_FONTS_DIR" "$(seed_var CAPTCHA_FONTS_DIR "$CAPTCHA_FONTS_DIR")"
printf "  CAPTCHA_GEOIP_DIR         -> %s (%s)\n" "$CAPTCHA_GEOIP_DIR" "$(seed_var CAPTCHA_GEOIP_DIR "$CAPTCHA_GEOIP_DIR")"
printf "  CAPTCHA_ARKOSE_MODELS_DIR -> %s (%s)\n" "$CAPTCHA_ARKOSE_MODELS_DIR" "$(seed_var CAPTCHA_ARKOSE_MODELS_DIR "$CAPTCHA_ARKOSE_MODELS_DIR")"
printf "  CAPTCHA_APIKEY_FILE       -> %s (%s)\n" "$CAPTCHA_APIKEY_FILE" "$(seed_var CAPTCHA_APIKEY_FILE "$CAPTCHA_APIKEY_FILE")"

echo
echo "== done =="
echo "application uuid: $APP_UUID"
echo
echo "Trigger a deploy with:"
echo "  docker exec -i coolify php /var/www/html/artisan tinker --execute='"
echo "    \$app = App\\Models\\Application::whereUuid(\"$APP_UUID\")->firstOrFail();"
echo "    \$r = queue_application_deployment(application: \$app, deployment_uuid: (string) \\Illuminate\\Support\\Str::uuid());"
echo "    echo json_encode(\$r);'"

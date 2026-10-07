# Container deployment (Coolify)

The repository's own deployment is a systemd unit on a host with a persistent
Xvfb (`deploy/`). This document covers the **containerised** path used when a
Coolify-style orchestrator runs the service.

## Shape

```
Cloudflare Tunnel ──> 127.0.0.1:8877 ──> this container
   (2 hostnames,                            (Xvfb :99 + uvicorn)
    one per credential type)
```

Two hostnames, two credential types — the application enforces the split
(`web/hosts.py`). Each origin refuses the other's paths with **404**; an
unrecognised `Host` gets **421**. Both must be single-label names if the zone's
edge certificate covers only `example.com` and `*.example.com`.

| Host | Serves | Credential |
|---|---|---|
| `solver.example.com` | Console (`/`, `/login`, `/dashboard`, `/docs`) | Session cookie + CSRF header |
| `api-solver.example.com` | API (`/solve`, `/health`, `/status`, `/logs`, `/swagger`, `/redoc`, `/openapi.json`) | Static Bearer token |

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Two-stage build: console (Node) then runtime (Python + Xvfb + Chromium deps) |
| `entrypoint.sh` | Starts the in-container Xvfb, then execs the server |
| `compose.coolify.yaml` | The deployment shape — raw compose mode, loopback publish, mounted browser build |
| `deploy/cloudflared-captcha.service` | Dedicated tunnel unit (one tunnel per service) |
| `deploy/coolify-provision.sh` | Creates the application and applies its settings |
| `tools/verify-edge.sh` | Probes both hostnames through the real edge |
| `tools/bench_parallel_http.sh` | N parallel solves through the HTTP surface |

## What the compose encodes, and why

**The browser binary is mounted, not baked.** The wrapper resolves the newest
Pro build from `cloakbrowser.dev`, and a free-plan key cannot pin an older one
(the download endpoint force-serves the latest). An image that fetched its own
binary would ship whatever is current rather than the build the deployment was
validated against. `CLOAKBROWSER_BINARY_PATH` is read before any download logic,
so the mount is authoritative.

**Host paths come from environment variables.** Coolify runs compose from its own
application directory, so every relative path resolves there instead of to the
checkout. The file carries neutral defaults so it still parses standalone.

**Raw compose mode is required.** The default parser rewrites each service's
`env_file` to the orchestrator's own `.env` — in a directory that is empty for
this app — so the service would boot with no configuration at all.

## Environment variables

Two classes. Service configuration lives in the env file the compose references
(`env_file`); the values the compose itself interpolates are set on the
application:

| Variable | Purpose |
|---|---|
| `CAPTCHA_ENV_FILE` | Absolute path to the service's env file |
| `CAPTCHA_BROWSER_DIR` | Directory holding the pinned `chrome` binary |
| `CAPTCHA_FONTS_DIR` | Windows font files (a Chromium claiming Windows with only Linux fonts is a tell) |
| `CAPTCHA_GEOIP_DIR` | `GeoLite2-City.mmdb` — required by the `*_GEOIP` flags |
| `CAPTCHA_ARKOSE_MODELS_DIR` | Arkose ONNX classifiers (optional) |
| `CAPTCHA_APIKEY_FILE` | Mistral keys, one per line (optional; image challenges only) |
| `DEBIAN_MIRROR` | Build arg — `apt` mirror |
| `PYPI_INDEX_URL` | Build arg — `pip` index |

Flag the build args **build-time** in the orchestrator: it stores variables
encrypted and only emits them for variables flagged as such, so a runtime-only
variable is absent during `docker compose build` and the mirror silently does
not apply.

## Escaping `$` in the env file

`docker compose` interpolates `$VAR` inside `env_file` values. A scrypt digest
contains `$` separators, so an unescaped digest arrives **truncated at the first
`$`** — the console then rejects every password. Write `$$` in the file; compose
collapses it back to a single `$` in the container:

```
ADMIN_PASSWORD_HASH=scrypt$$32768$$8$$1$$<salt>$$<hash>
```

Verify by reading the variable back from inside the running container and
comparing it to the source value.

## Local git mirror for deploys

An orchestrator clones from inside a helper container, whose internet path can
be worse than the host's. `codeload.github.com` in particular can degrade to
~30s per request while `github.com` answers in 0.2s, which fails every shallow
clone with `RPC failed; curl 18/56` and `early EOF`.

Serve the repo from a LAN mirror instead:

```bash
git clone --bare /path/to/checkout ~/git-mirrors/<repo>.git
git daemon --reuseaddr --base-path=$HOME/git-mirrors --export-all \
  --enable=upload-pack --listen=0.0.0.0 --port=9418
```

Point the application at `git://<host>:9418/<repo>.git` with `source_type`,
`source_id` and `private_key_id` cleared. A **dumb HTTP server does not work** —
it cannot serve the `--depth=1` clone every orchestrator issues. Allow the
daemon's port from the docker bridge range, not just the LAN: the helper's
source address is a bridge address, and `ping` succeeding proves nothing about
TCP.

The mirror is a snapshot — refresh it after every push, or the deploy ships the
previous revision while reporting success.

## Verifying

```bash
bash tools/verify-edge.sh         # both hostnames + one real solve
bash tools/bench_parallel_http.sh 8
```

The edge probes must go out through an **external egress**: a host behind NAT
usually cannot reach its own public hostname (hairpin NAT), so testing from the
host proves nothing either way.

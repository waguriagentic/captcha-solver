# Captcha Solver — container image.
#
# The repository's own deployment is a systemd unit on a host with a persistent
# Xvfb (see deploy/ and README). This image is the containerised path: it
# carries the X server, the browser runtime dependencies and the built console,
# and takes the patched CloakBrowser binary from a mount at runtime.
#
# Why the browser binary is NOT baked in: the wrapper resolves the newest Pro
# build from cloakbrowser.dev and a free-plan key cannot pin an older one — the
# download endpoint force-serves the latest. A build that fetches its own
# binary therefore ships whatever is current, which may not be the build the
# deployment was validated against. The compose file mounts the validated,
# already-patched build read-only and pins it via CLOAKBROWSER_BINARY_PATH,
# which the wrapper reads before any download logic.

# ── Stage 1: build the admin console (React + Vite) ─────────────────────────
FROM node:22-slim AS ui

WORKDIR /build
COPY web/ui/package.json web/ui/package-lock.json ./
RUN npm ci
COPY web/ui/ ./
RUN npm run build

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim

# Chromium runtime dependencies. Mirrors the set the CloakBrowser Manager image
# installs (a known-good list for this binary) plus the X server the headed
# solvers need.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
    libgl1-mesa-dri libegl-mesa0 \
    procps wget ca-certificates xclip binutils \
    xvfb xauth \
    fonts-roboto fonts-noto-core fonts-noto-color-emoji fonts-droid-fallback \
    fonts-urw-base35 \
    && rm -rf /var/lib/apt/lists/*

# Windows core fonts. A Chromium advertising a Windows platform while exposing
# only Linux font families is a detectable mismatch, and the console's
# fingerprint flags assume the Windows set is present. The modern families
# (Segoe UI, Calibri, Consolas) come from a mount — see compose.coolify.yaml —
# because they are not redistributable from this repository.
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
      | debconf-set-selections \
    && sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
RUN apt-get update \
    && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies. cloakbrowser is pinned: the deployment validates one
# wrapper version against one browser build, and a floating wrapper can change
# launch behaviour (flags, geoip resolution) without the binary changing.
# geoip2 + socksio are the wrapper's `geoip` extra: a geoip=True launch raises
# before any browser starts when they are missing.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --retries 8 --timeout 60 \
      fastapi uvicorn pydantic pillow \
      onnxruntime opencv-python-headless numpy \
      geoip2 socksio \
      "cloakbrowser==0.5.11"

# Application source.
COPY . /app
COPY --from=ui /build/dist /app/web/ui/dist

# The console bundle is built in stage 1; drop the sources and toolchain so the
# runtime image does not carry a Node install it will never use.
RUN rm -rf /app/web/ui/node_modules /app/web/ui/src \
           /app/web/ui/package.json /app/web/ui/package-lock.json \
           /app/web/ui/vite.config.ts /app/web/ui/tsconfig*.json

RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99

EXPOSE 8877

# Liveness only — /health is token-gated, so an unauthenticated probe cannot
# assert service health. This checks that the process is accepting connections
# on its port, which is what a container healthcheck is for here.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
  CMD python3 -c "import socket; s=socket.create_connection(('127.0.0.1',8877),3); s.close()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

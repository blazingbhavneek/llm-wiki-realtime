#!/usr/bin/env bash
# Sets up the whole local HTTPS/WSS stack this app needs to run in a browser:
#   - LiveKit server + Caddy (TLS termination), both via Docker
#   - a locally-trusted-format HTTPS cert (trustme CA, reused by the agent's
#     web server, Caddy, and the Vite dev server)
#   - .env / Caddyfile / frontend env wiring so all three agree on one origin
#
# Safe to re-run: every step is idempotent (upserts config, regenerates certs
# to match the current LAN IP, restarts containers).
#
# Usage: scripts/setup_https_livekit.sh [HOST]
#   HOST is the browser-facing address. It is taken from the argument, else
#   from PUBLIC_HOST in .env, and only as a last resort auto-detected - so a
#   host pinned in .env stays pinned across re-runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
CADDYFILE="$ROOT/Caddyfile"
COMPOSE_FILE="$ROOT/docker-compose.caddy.yml"
CERT_DIR="$ROOT/certs"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Target host
# ---------------------------------------------------------------------------
LAN_IP="${1:-${LAN_IP:-}}"
if [ -z "$LAN_IP" ] && [ -f "$ENV_FILE" ]; then
    LAN_IP="$(sed -n 's/^PUBLIC_HOST=//p' "$ENV_FILE" | tail -1)"
    [ -n "$LAN_IP" ] && log "Using PUBLIC_HOST from .env: $LAN_IP"
fi
if [ -z "$LAN_IP" ]; then
    LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
    warn "No host given and none in .env - auto-detected $LAN_IP. Pass one explicitly to pin it."
fi
if [ -z "$LAN_IP" ]; then
    warn "Could not auto-detect a LAN IP; pass one explicitly: scripts/setup_https_livekit.sh <ip>"
    exit 1
fi
log "Target host: $LAN_IP (browsers on this machine and others on the same network can use it)"

# ---------------------------------------------------------------------------
# 2. Docker
# ---------------------------------------------------------------------------
log "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed. Install it first (e.g. 'sudo pacman -S docker docker-compose')"
    echo "then re-run this script."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "The 'docker compose' plugin is not available. Install docker-compose and re-run."
    exit 1
fi

if ! systemctl is-active --quiet docker 2>/dev/null; then
    log "Starting the Docker daemon (needs sudo)"
    sudo systemctl enable --now docker
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
    warn "Current user can't reach the Docker daemon directly - using sudo for docker commands."
    warn "Run 'sudo usermod -aG docker \$USER' and re-login to avoid this next time."
    DOCKER=(sudo docker)
fi

# ---------------------------------------------------------------------------
# 3. .env: one HTTPS/WSS origin, shared by the agent's web server, Caddy, and LiveKit
# ---------------------------------------------------------------------------
log "Updating .env"
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
touch "$ENV_FILE"
# Browser-facing origin: Caddy terminates TLS here and proxies /rtc* to
# LiveKit (7880) and everything else to the agent's plain-HTTP uvicorn.
set_env "PUBLIC_HOST" "${LAN_IP}"
set_env "PUBLIC_LIVEKIT_URL" "wss://${LAN_IP}:51027"
set_env "TEXT_TEST_HTTPS_HOSTS" "localhost,127.0.0.1,${LAN_IP}"
set_env "TEXT_TEST_PORT" "51028"
set_env "TEXT_TEST_TLS" "false"
set_env "LIVEKIT_NODE_IP" "${LAN_IP}"

# ---------------------------------------------------------------------------
# 4. HTTPS certificate (trustme CA - reused by the agent's web server, Caddy, Vite)
# ---------------------------------------------------------------------------
# Deleting first matters: the generator reuses an existing certificate, so a
# changed host would otherwise keep the old, now-wrong SAN list.
log "Generating HTTPS certificate for: localhost, 127.0.0.1, ${LAN_IP}"
rm -f "$CERT_DIR"/text-test-*.pem
uv run python -c "
from dotenv import load_dotenv

# The generator reads TEXT_TEST_CERT_DIR and TEXT_TEST_HTTPS_HOSTS, which this
# script just wrote to .env. Importing server.py used to load them as a side
# effect; app.web.tls has no such side effect, so load them here.
load_dotenv(override=True)

from app.config import Settings
from app.web.tls import ensure_local_https_certificate

cert, key, ca = ensure_local_https_certificate(Settings.from_env().web)
print(f'  cert: {cert}')
print(f'  key:  {key}')
print(f'  ca:   {ca}')
"

# ---------------------------------------------------------------------------
# 5. Caddyfile: match the site address to the detected host
# ---------------------------------------------------------------------------
# Nothing to rewrite: the Caddyfile reads {$PUBLIC_HOST}, which compose passes
# in from .env. Left as a step so the numbering matches the sections above.
log "Caddyfile reads PUBLIC_HOST from .env - no edit needed"

# ---------------------------------------------------------------------------
# 6. Bring up LiveKit + Caddy
# ---------------------------------------------------------------------------
log "Starting LiveKit server + Caddy (Docker)"
"${DOCKER[@]}" compose -f "$COMPOSE_FILE" up -d

log "Waiting for LiveKit to accept connections"
for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "http://127.0.0.1:7880/"; then
        break
    fi
    sleep 1
done
if curl -sf -o /dev/null "http://127.0.0.1:7880/"; then
    echo "  LiveKit is up on ws://127.0.0.1:7880"
else
    warn "LiveKit did not come up in time - check: docker logs livekit-server"
fi
echo "  Caddy is listening on https://${LAN_IP}:51027 (returns 502 until 'uv run python -m app' is running)"

# ---------------------------------------------------------------------------
# 7. Frontend: wire up Vite's own HTTPS dev server for hot-reload testing
# ---------------------------------------------------------------------------
log "Writing frontend/.env.development.local (Vite dev-server HTTPS)"
DEV_LOCAL_ENV="$ROOT/frontend/.env.development.local"
cat > "$DEV_LOCAL_ENV" <<EOF
# Generated by scripts/setup_https_livekit.sh - do not commit (see .gitignore).
VITE_HTTPS_KEY=${CERT_DIR}/text-test-key.pem
VITE_HTTPS_CERT=${CERT_DIR}/text-test-cert.pem
VITE_TOKEN_SERVER_URL=http://127.0.0.1:51028
EOF

log "Building the frontend for the combined-server (demo) flow"
if ! (cd "$ROOT/frontend" && npm install && npm run build); then
    warn "frontend build failed - the agent will still run, just without static assets at /."
    warn "Retry manually: cd frontend && npm run build"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "Done"
cat <<EOF

Two ways to run the frontend, both over HTTPS/WSS:

  Demo (one origin, matches production):
    uv run python -m app
    open https://${LAN_IP}:51027

  Dev (hot reload, separate origin for the page, LiveKit still via Caddy):
    cd frontend && npm run dev:https
    open https://${LAN_IP}:5173

Bring the rest of the app up in other terminals:
    uv run python -m app.stt.nemotron
    uv run python -m app.tts.supertonic
    uv run python -m app.rag.placeholder

Your browser will warn about the certificate until you trust its CA
(self-signed, generated by trustme - not from a public CA):
    ${CERT_DIR}/text-test-ca.pem
  Firefox: Settings -> Privacy & Security -> View Certificates -> Import
  Chrome/Arch system trust: sudo trust anchor ${CERT_DIR}/text-test-ca.pem

Docker services: docker compose -f docker-compose.caddy.yml [ps|logs|down]
EOF

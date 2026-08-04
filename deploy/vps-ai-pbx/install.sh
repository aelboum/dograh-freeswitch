#!/usr/bin/env bash
# deploy/vps-ai-pbx/install.sh — Phase 9 deployment script.
#
# Installs Docker (if missing), makes sure this is a real git checkout of
# the dograh repo (cloning one if run standalone), builds .env interactively
# from .env.example if it doesn't exist yet, then `docker compose up -d`.
#
# This is a self-contained script for this deployment path specifically —
# it does not source scripts/lib/setup_common.sh, which is tightly coupled
# to the existing remote/nginx/coturn profile (renders nginx/turn config,
# assumes that compose layout) that this Cloudflare-tunnel + in-Docker-
# FreeSWITCH deployment doesn't use. This mirrors deploy/hostinger/'s own
# choice to be a self-contained deployment path with its own docs rather
# than routing through setup_remote.sh — precedented, not a new pattern.
#
# Safe to re-run: never overwrites an existing .env, never touches existing
# volumes.
set -euo pipefail

REPO_URL="https://github.com/dograh-hq/dograh.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info()    { printf '\033[1;34m[info]\033[0m %s\n' "$1"; }
success() { printf '\033[1;32m[ok]\033[0m %s\n' "$1"; }
warn()    { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
fail()    { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Make sure we're operating inside a real dograh checkout.
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/../../AGENTS.md" ] && [ -d "$SCRIPT_DIR/../../.git" ]; then
    REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
    info "Running inside an existing dograh checkout at $REPO_DIR"
else
    warn "Not running from inside a dograh git checkout — cloning one."
    CLONE_DIR="${DOGRAH_CLONE_DIR:-$HOME/dograh}"
    if [ -d "$CLONE_DIR/.git" ]; then
        info "Reusing existing checkout at $CLONE_DIR"
    else
        command -v git >/dev/null 2>&1 || fail "git is required to clone the repo — install it first."
        git clone "$REPO_URL" "$CLONE_DIR"
    fi
    REPO_DIR="$CLONE_DIR"
fi
DEPLOY_DIR="$REPO_DIR/deploy/vps-ai-pbx"
[ -d "$DEPLOY_DIR" ] || fail "Expected $DEPLOY_DIR to exist after checkout — checkout may be stale, try 'git pull' in $REPO_DIR."

# ---------------------------------------------------------------------------
# 2. Install Docker + the Compose plugin, if missing.
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    success "Docker + Compose plugin already installed ($(docker --version))."
else
    info "Installing Docker via get.docker.com's official convenience script..."
    if command -v apt-get >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        curl -fsSL https://get.docker.com | sh
    else
        fail "No apt/dnf/yum found — install Docker + the Compose plugin manually, then re-run this script."
    fi
    systemctl enable --now docker 2>/dev/null || true
    if [ "${EUID:-$(id -u)}" -ne 0 ] && ! groups "$USER" | grep -q docker; then
        usermod -aG docker "$USER" || true
        warn "Added $USER to the 'docker' group — log out and back in (or run 'newgrp docker') before re-running this script without sudo."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Build .env if it doesn't already exist. Never overwrites an existing one.
# ---------------------------------------------------------------------------
ENV_FILE="$DEPLOY_DIR/.env"

random_secret() { openssl rand -hex "${1:-24}"; }

# prompt "Human-readable question" "default (optional, blank = required)"
# Leaves the answer in $PROMPT_REPLY rather than printing it, so callers can
# both write it to .env and reuse it as a later prompt's default (e.g.
# RTP_PUBLIC_IP defaulting to whatever was entered for SIP_EXTERNAL_IP).
prompt() {
    local question="$1" default="${2:-}" answer=""
    if [ -n "$default" ]; then
        read -r -p "$question [$default]: " answer
        answer="${answer:-$default}"
    else
        while [ -z "$answer" ]; do
            read -r -p "$question: " answer
        done
    fi
    PROMPT_REPLY="$answer"
}

if [ -f "$ENV_FILE" ]; then
    success ".env already exists at $ENV_FILE — leaving it untouched."
else
    info "No .env found — let's build one (see .env.example for full field docs)."
    info "Secrets (passwords, ESL password, JWT secret) are generated for you unless you override them."

    prompt "This VPS's public IPv4 address (SIP_EXTERNAL_IP)"; sip_external_ip="$PROMPT_REPLY"
    prompt "RTP public IP" "$sip_external_ip"; rtp_public_ip="$PROMPT_REPLY"
    prompt "Bootstrap SIP trunk name (e.g. your provider's short name)"; sip_gw_name="$PROMPT_REPLY"
    prompt "SIP trunk username"; sip_gw_username="$PROMPT_REPLY"
    prompt "SIP trunk password"; sip_gw_password="$PROMPT_REPLY"
    prompt "SIP trunk realm (e.g. sip.provider.com)"; sip_gw_realm="$PROMPT_REPLY"
    prompt "SIP trunk proxy" "$sip_gw_realm"; sip_gw_proxy="$PROMPT_REPLY"
    prompt "Public base URL for the dashboard (e.g. https://calls.example.com)"; public_base_url="$PROMPT_REPLY"
    prompt "Public hostname (e.g. calls.example.com)"; public_host="$PROMPT_REPLY"
    prompt "Public backend API URL" "$public_base_url"; ai_backend_url="$PROMPT_REPLY"
    prompt "MinIO root username" "dograh"; minio_root_user="$PROMPT_REPLY"
    prompt "Cloudflare named-tunnel token (leave blank for an ephemeral quick tunnel)" "__blank__"
    cloudflare_token="$PROMPT_REPLY"
    [ "$cloudflare_token" = "__blank__" ] && cloudflare_token=""

    {
        echo "# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) — see .env.example for full field docs."
        echo
        echo "SIP_EXTERNAL_IP=$sip_external_ip"
        echo "RTP_PUBLIC_IP=$rtp_public_ip"
        echo "SIP_EXTERNAL_PORT=5060"
        echo "SIP_EXTERNAL_TLS_PORT=5061"
        echo "RTP_PORT_RANGE_START=20000"
        echo "RTP_PORT_RANGE_END=20199"
        echo
        echo "ESL_PASSWORD=$(random_secret 24)"
        echo "DOGRAH_ESL_ALLOWED_CIDRS=172.20.0.0/16"
        echo
        echo "SIP_GATEWAY_NAME=$sip_gw_name"
        echo "SIP_GATEWAY_USERNAME=$sip_gw_username"
        echo "SIP_GATEWAY_PASSWORD=$sip_gw_password"
        echo "SIP_GATEWAY_REALM=$sip_gw_realm"
        echo "SIP_GATEWAY_PROXY=$sip_gw_proxy"
        echo
        echo "PUBLIC_BASE_URL=$public_base_url"
        echo "PUBLIC_HOST=$public_host"
        echo "AI_BACKEND_URL=$ai_backend_url"
        echo
        echo "POSTGRES_PASSWORD=$(random_secret 24)"
        echo "REDIS_PASSWORD=$(random_secret 24)"
        echo "MINIO_ROOT_USER=$minio_root_user"
        echo "MINIO_ROOT_PASSWORD=$(random_secret 24)"
        echo "OSS_JWT_SECRET=$(random_secret 32)"
        echo
        echo "CLOUDFLARE_TUNNEL_TOKEN=$cloudflare_token"
        echo "FASTAPI_WORKERS=1"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    success ".env written to $ENV_FILE (mode 600). Review it before continuing if anything looks wrong."
fi

# ---------------------------------------------------------------------------
# 4. Bring the stack up.
# ---------------------------------------------------------------------------
info "Building and starting the stack (this builds the freeswitch image from source — can take several minutes the first time)..."
( cd "$DEPLOY_DIR" && docker compose -f docker-compose.vps.yml up -d --build )

success "Stack started. Check status with:  docker compose -f $DEPLOY_DIR/docker-compose.vps.yml ps"
info "Next steps: VPS_DEPLOYMENT_GUIDE.md (what to verify), VPS_FIREWALL.md (open the required ports), TROUBLESHOOTING.md (if something's wrong)."

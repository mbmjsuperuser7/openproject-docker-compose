#!/bin/bash
# =============================================================================
# vm-setup.sh — OpenProject + Cloudflare Tunnel on Ubuntu 22.04/24.04
#
# Based strictly on official docs:
#   https://www.openproject.org/docs/installation-and-operations/installation/docker-compose/
#
# BEFORE RUNNING:
#   1. Have your Cloudflare Tunnel token ready
#      (CF Zero Trust -> Networks -> Tunnels -> Create -> Cloudflared -> copy token)
#   2. Have your public hostname ready e.g. openproject.yourdomain.com
#   3. In CF tunnel dashboard, set public hostname -> http://proxy:80
#
# USAGE:
#   sudo bash vm-setup.sh
# =============================================================================

set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERR]${NC}   $1"; exit 1; }

echo ""
echo "======================================================"
echo "  OpenProject + Cloudflare Tunnel Setup"
echo "======================================================"
echo ""

# ── Collect config ─────────────────────────────────────────────────────────
read -rp "Cloudflare Tunnel token: " CF_TOKEN
[ -z "$CF_TOKEN" ] && error "CF token required."

read -rp "OpenProject public hostname (e.g. openproject.yourdomain.com): " OP_HOST
[ -z "$OP_HOST" ] && error "Hostname required."

echo ""
echo -e "${YELLOW}Set a console password for the 'ubuntu' user:${NC}"
read -rsp "Password: " VM_PASSWORD; echo ""
read -rsp "Confirm:  " VM_PASSWORD2; echo ""
[ "$VM_PASSWORD" != "$VM_PASSWORD2" ] && error "Passwords do not match."

SECRET_KEY=$(openssl rand -hex 32)
COLLAB_SECRET=$(openssl rand -hex 16)
INSTALL_DIR="/opt/openproject"

# ── Install Docker ──────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    success "Docker already installed."
else
    info "Installing Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    success "Docker installed."
fi

# ── Clone repo ──────────────────────────────────────────────────────────────
info "Cloning OpenProject docker-compose repo..."
if [ -d "$INSTALL_DIR" ]; then
    warn "$INSTALL_DIR exists — pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone https://github.com/mbmjsuperuser7/openproject-docker-compose.git \
        --depth=1 --branch=stable/17 "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
success "Repo cloned to $INSTALL_DIR"

# ── Create persistent data directories ──────────────────────────────────────
# CRITICAL: must exist with uid 1000 before first boot
info "Creating persistent data directories..."
mkdir -p /var/openproject/assets /var/openproject/pgdata
chown 1000:1000 -R /var/openproject/assets
chown 999:999 -R /var/openproject/pgdata   # postgres user inside container
success "Data directories created with correct permissions."

# ── Write .env ──────────────────────────────────────────────────────────────
info "Writing .env..."
cat > .env <<EOF
TAG=17-slim
OPENPROJECT_HTTPS=false
OPENPROJECT_HSTS=false
SECRET_KEY_BASE=${SECRET_KEY}
OPENPROJECT_HOST__NAME=${OP_HOST}
PORT=127.0.0.1:8080
POSTGRES_VERSION=17
PGDATA=/var/openproject/pgdata
OPDATA=/var/openproject/assets
COLLABORATIVE_SERVER_SECRET=${COLLAB_SECRET}
TUNNEL_TOKEN=${CF_TOKEN}
IMAP_ENABLED=false
RAILS_MIN_THREADS=4
RAILS_MAX_THREADS=16
DATABASE_URL=postgres://postgres:p4ssw0rd@db/openproject?pool=20&encoding=unicode&reconnect=true
EOF
chmod 600 .env
success ".env written."

# ── Start the stack ──────────────────────────────────────────────────────────
info "Starting OpenProject stack (pulling latest images)..."
docker compose up -d --build --pull always
success "Stack started."

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "======================================================"
echo "  Setup complete."
echo "======================================================"
echo ""
echo -e "${YELLOW}Wait 2-3 minutes for seeder to finish migrations, then:${NC}"
echo ""
echo "  Local access:  http://$(hostname -I | awk '{print $1}'):8080"
echo "  Public access: https://${OP_HOST}"
echo ""
echo "  Default login: admin / admin (change immediately)"
echo ""
echo -e "${YELLOW}Watch startup progress:${NC}"
echo "  cd ${INSTALL_DIR} && sudo docker compose logs seeder -f"
echo ""
echo -e "${YELLOW}MCP server (requires Enterprise license):${NC}"
echo "  https://${OP_HOST}/mcp"
echo "  Auth: API token from My Account -> Access Tokens -> API"
echo ""

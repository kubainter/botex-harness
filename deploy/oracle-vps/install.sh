#!/usr/bin/env bash
# ==============================================================================
# BoteX - One-Click Production Deployment Script for Oracle Cloud Always Free
# Shape: VM.Standard.A1.Flex (4 OCPU Ampere ARM64, 24 GiB RAM, Ubuntu 22.04/24.04)
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
log_ok()   { printf "${GREEN}[OK]${NC} %s\n" "$*"; }
log_warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
log_err()  { printf "${RED}[ERROR]${NC} %s\n" "$*"; }

# 1. Root check
if [ "$EUID" -ne 0 ]; then
    log_err "This installation script must be run as root. Please run with: sudo bash $0"
    exit 1
fi

log_info "Starting BoteX One-Click Deployment on Oracle Cloud ARM64..."

INSTALL_DIR="/opt/botex"
BIELIK_DIR="/opt/bielik"
LOG_DIR="/var/log/botex"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

INSTALL_BIELIK=false
for arg in "$@"; do
    case "$arg" in
        --with-bielik) INSTALL_BIELIK=true ;;
    esac
done

# 2. System dependencies
log_info "Updating system packages and installing prerequisites..."
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-venv python3-dev build-essential cmake git curl ufw

# 3. Create botex service user & directories
log_info "Configuring service user and operational directories..."
if ! id "botex" &>/dev/null; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin botex
    log_ok "Created system user 'botex'."
fi

mkdir -p "$INSTALL_DIR" "$BIELIK_DIR/models" "$LOG_DIR"
chown -R botex:botex "$INSTALL_DIR" "$BIELIK_DIR" "$LOG_DIR"

# 4. Synchronize repository files
log_info "Deploying BoteX codebase to $INSTALL_DIR..."
if [ "$SOURCE_DIR" != "$INSTALL_DIR" ] && [ -d "$SOURCE_DIR/botex" ]; then
    cp -ru "$SOURCE_DIR"/. "$INSTALL_DIR"/
    chown -R botex:botex "$INSTALL_DIR"
    log_ok "Synchronized repository files to $INSTALL_DIR."
fi

# 5. Virtual environment & dependencies
log_info "Setting up Python virtual environment..."
cd "$INSTALL_DIR"
if [ ! -d ".venv" ]; then
    sudo -u botex python3 -m venv .venv
fi

log_info "Installing Python dependencies..."
sudo -u botex .venv/bin/pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    sudo -u botex .venv/bin/pip install -r requirements.txt
else
    sudo -u botex .venv/bin/pip install -e .
fi
log_ok "Python environment ready."

# 6. Environment configuration (.env)
if [ ! -f "$INSTALL_DIR/.env" ]; then
    log_info "Generating secure default .env configuration..."
    RANDOM_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
    cat <<EOF > "$INSTALL_DIR/.env"
# BoteX Cloud Gateway Configuration
BOTEX_ACCESS_TOKEN=${RANDOM_TOKEN}
# API Provider Keys (fill with your keys):
OPENROUTER_API_KEY=
NVIDIA_API_KEY=
EOF
    chmod 600 "$INSTALL_DIR/.env"
    chown botex:botex "$INSTALL_DIR/.env"
    log_ok "Generated $INSTALL_DIR/.env with a new BOTEX_ACCESS_TOKEN."
    log_warn "Remember to edit $INSTALL_DIR/.env and add your OPENROUTER_API_KEY / NVIDIA_API_KEY!"
else
    log_info "Existing .env found at $INSTALL_DIR/.env - preserving."
fi

# 7. Optional Bielik 11B local inference engine (Flagship Q8_0 Quantization)
if [ "$INSTALL_BIELIK" = true ]; then
    log_info "Installing llama.cpp ARM NEON engine and downloading Bielik-11B-v3.0 Q8_0..."
    if [ ! -d "/opt/llama.cpp" ]; then
        git clone https://github.com/ggml-org/llama.cpp /opt/llama.cpp
        cd /opt/llama.cpp
        cmake -B build
        cmake --build build --config Release -j4
        chown -R botex:botex /opt/llama.cpp
        log_ok "llama.cpp compiled successfully."
    fi

    MODEL_FILE="$BIELIK_DIR/models/bielik-11b-v3.0-instruct.Q8_0.gguf"
    if [ ! -f "$MODEL_FILE" ]; then
        log_info "Downloading Bielik-11B-v3.0 Q8_0 GGUF (~11.05 GiB - Studio 8-bit precision)..."
        sudo -u botex curl -fL -o "$MODEL_FILE" \
            "https://huggingface.co/speakleash/Bielik-11B-v3.0-Instruct-GGUF/resolve/main/Bielik-11B-v3.0-Instruct.Q8_0.gguf"
        log_ok "Bielik 11B v3.0 Q8_0 model downloaded successfully."
    fi
fi

# 8. Install systemd units & maintenance script
log_info "Installing systemd services and timers..."
cp "$INSTALL_DIR/deploy/oracle-vps/botex-cloud.service" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/oracle-vps/botex-maintenance.service" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/oracle-vps/botex-maintenance.timer" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/oracle-vps/botex-nightly-curator.service" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/oracle-vps/botex-nightly-curator.timer" /etc/systemd/system/

if [ -f "$INSTALL_DIR/deploy/oracle-vps/bielik.service" ] && [ "$INSTALL_BIELIK" = true ]; then
    cp "$INSTALL_DIR/deploy/oracle-vps/bielik.service" /etc/systemd/system/
fi

install -m 0755 "$INSTALL_DIR/deploy/oracle-vps/botex-maintenance.sh" /usr/local/sbin/botex-maintenance.sh
install -m 0755 "$INSTALL_DIR/deploy/oracle-vps/botex-curator.sh" /usr/local/sbin/botex-curator.sh

systemctl daemon-reload

# 9. Firewall configuration
log_info "Configuring UFW firewall rules..."
ufw allow 8000/tcp comment 'BoteX Cloud Gateway & PWA' || true

# 10. Enable and start services
log_info "Enabling and starting BoteX systemd units..."
systemctl enable --now botex-cloud.service
systemctl enable --now botex-maintenance.timer
systemctl enable --now botex-nightly-curator.timer

if [ "$INSTALL_BIELIK" = true ]; then
    systemctl enable --now bielik.service
fi

log_ok "All services started."

# 11. Self-Verification Health Check
sleep 3
log_info "Verifying BoteX Cloud Gateway health..."
HEALTH_CHECK=$(curl -s http://127.0.0.1:8000/api/health || echo "FAILED")

echo "=============================================================================="
if [[ "$HEALTH_CHECK" == *"healthy"* ]]; then
    log_ok "BoteX is UP and HEALTHY on Oracle Cloud VPS!"
else
    log_warn "Gateway started but health check returned: $HEALTH_CHECK"
    log_info "Check logs with: journalctl -u botex-cloud -n 30 --no-pager"
fi
echo "=============================================================================="
printf "🌐 PWA Web Interface & REST API: http://YOUR_VPS_PUBLIC_IP:8000\n"
printf "🔑 Access Token (from .env):      "
grep "BOTEX_ACCESS_TOKEN=" "$INSTALL_DIR/.env" | cut -d'=' -f2-
printf "🕸️ Knowledge Graph & PKM Vault:   %s/.botex/memory\n" "$INSTALL_DIR"
printf "⚙️ Service Status:                systemctl status botex-cloud\n"
printf "🧹 Background Maintenance:        systemctl status botex-maintenance.timer\n"
echo "=============================================================================="

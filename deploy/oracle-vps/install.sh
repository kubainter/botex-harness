#!/usr/bin/env bash
# ==============================================================================
# BoteX - One-Click Production Deployment Script for Oracle Cloud Always Free
# Shape: VM.Standard.A1.Flex (3 OCPU Ampere ARM64, 18 GiB RAM, Ubuntu 22.04/24.04)
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
PYTHON_BIN=$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3)
if [ -d ".venv" ]; then
    VENV_PY_VER=$(.venv/bin/python3 -c "import sys; print(sys.version_info[:2] >= (3, 13))" 2>/dev/null || echo "False")
    if [ "$VENV_PY_VER" != "True" ]; then
        log_info "Recreating virtual environment with $PYTHON_BIN (requires Python >= 3.13)..."
        rm -rf .venv
    fi
fi
if [ ! -d ".venv" ]; then
    sudo -u botex "$PYTHON_BIN" -m venv .venv
fi

log_info "Installing Python dependencies..."
sudo -u botex .venv/bin/pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    sudo -u botex .venv/bin/pip install -r requirements.txt
else
    sudo -u botex .venv/bin/pip install -e .
fi
log_ok "Python environment ready."

# 5.1 SkillSpector security gate binary provisioning (isolated tools directory)
log_info "Provisioning NVIDIA SkillSpector security scanner binary..."
mkdir -p "$INSTALL_DIR/bin" "$INSTALL_DIR/tools"
chown -R botex:botex "$INSTALL_DIR/bin" "$INSTALL_DIR/tools"
if ! command -v uv &>/dev/null; then
    export UV_INSTALL_DIR="/usr/local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh || true
fi
export PATH="$PATH:/usr/local/bin:$INSTALL_DIR/bin"
if command -v uv &>/dev/null; then
    sudo -u botex env "PATH=$PATH" "HOME=/home/botex" uv tool install --tool-dir "$INSTALL_DIR/tools" --bin-dir "$INSTALL_DIR/bin" "skillspector[mcp] @ git+https://github.com/NVIDIA/skillspector.git" || log_warn "Failed to install skillspector via uv; will attempt venv fallback."
fi
if [ ! -f "$INSTALL_DIR/bin/skillspector" ]; then
    log_info "Provisioning skillspector via dedicated /opt/skillspector-venv fallback..."
    python3 -m venv /opt/skillspector-venv || true
    if [ -d "/opt/skillspector-venv" ]; then
        /opt/skillspector-venv/bin/pip install "skillspector[mcp] @ git+https://github.com/NVIDIA/skillspector.git" || log_warn "Failed to install skillspector into /opt/skillspector-venv"
        ln -sf /opt/skillspector-venv/bin/skillspector "$INSTALL_DIR/bin/skillspector" || true
    fi
fi

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

# 7. Optional Bielik 11B local inference engine (Quantization Q4_K_M for 18 GiB RAM box)
if [ "$INSTALL_BIELIK" = true ]; then
    log_info "Installing llama.cpp ARM NEON engine and downloading Bielik-11B-v3.1 Q4_K_M..."
    if [ ! -d "/opt/llama.cpp" ]; then
        git clone https://github.com/ggml-org/llama.cpp /opt/llama.cpp
        cd /opt/llama.cpp
        cmake -B build
        cmake --build build --config Release -j"$(nproc)"
        chown -R botex:botex /opt/llama.cpp
        log_ok "llama.cpp compiled successfully."
    fi

    MODEL_FILE="$BIELIK_DIR/models/Bielik-11B-v3.1-instruct.Q4_K_M.gguf"
    if [ ! -f "$MODEL_FILE" ]; then
        log_info "Downloading Bielik-11B-v3.1 Q4_K_M GGUF (~6.26 GiB - Optimized for 18 GiB RAM)..."
        sudo -u botex curl -fL -o "$MODEL_FILE" \
            "https://huggingface.co/speakleash/Bielik-11B-v3.1-Instruct-GGUF/resolve/main/Bielik-11B-v3.1-Instruct.Q4_K_M.gguf"
        log_ok "Bielik 11B v3.1 Q4_K_M model downloaded successfully."
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
printf "🔑 Access Auth Mode:              Configured in .env (Token / SHA-256 Hash)\n"
printf "🕸️ Knowledge Graph & PKM Vault:   %s/.botex/memory\n" "$INSTALL_DIR"
printf "⚙️ Service Status:                systemctl status botex-cloud\n"
printf "🧹 Background Maintenance:        systemctl status botex-maintenance.timer\n"
echo "=============================================================================="

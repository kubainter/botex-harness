#!/usr/bin/env bash
# ==============================================================================
# botex-curator.sh - Autonomous Knowledge Graph Nightly Curator (Bielik LLM)
# ==============================================================================
# Fired nightly at 03:30 by botex-nightly-curator.timer.
# Operating Principles:
# 1. Non-destructive: Never alters original technical conclusions or code.
# 2. Append-only linking: Enriches graph topology via verified wikilinks.
# 3. Collision avoidance: Yields immediately if active agent tasks are running.
# 4. Strict Audit Trail: Generates a persistent audit log in Memory Vault.
# ==============================================================================

set -euo pipefail

BOTEX_DIR="${BOTEX_DIR:-/opt/botex}"
LOG_DIR="${LOG_DIR:-/var/log/botex}"
PYTHON_BIN="${BOTEX_DIR}/.venv/bin/python"

mkdir -p "$LOG_DIR"
exec >> "${LOG_DIR}/curator.log" 2>&1

log() { printf '[%s] [botex-curator] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

log "Starting Nightly Knowledge Graph Curation cycle..."

if [ ! -d "$BOTEX_DIR" ]; then
    log "ERROR: BoteX directory $BOTEX_DIR does not exist."
    exit 1
fi

cd "$BOTEX_DIR"

# Source environment variables if .env exists and executing outside systemd
if [ -f "${BOTEX_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${BOTEX_DIR}/.env"
    set +a
fi

# 1. Check for active agent tasks (Concurrency Collision Safety Gate)
ACTIVE_TASKS=$(curl -s http://127.0.0.1:8000/api/tasks 2>/dev/null | grep -o '"status":"RUNNING"' | wc -l || echo "0")
if [ "$ACTIVE_TASKS" -gt 0 ]; then
    log "ACTIVE AGENT TASKS DETECTED ($ACTIVE_TASKS running). Deferring nightly curation to avoid resource competition."
    exit 0
fi

# 2. Verify Bielik llama.cpp / Ollama local endpoint availability
BIELIK_UP=$(curl -s http://127.0.0.1:8080/health 2>/dev/null || curl -s http://127.0.0.1:11434/api/tags 2>/dev/null || echo "DOWN")
if [[ "$BIELIK_UP" == *"DOWN"* ]] && [[ "$BIELIK_UP" != *"ok"* ]] && [[ "$BIELIK_UP" != *"models"* ]]; then
    log "NOTICE: Local Bielik inference server not responding. Running lightweight graph maintenance only."
fi

# 3. Execute Curator Logic via Unified Python Service
log "Dispatching curation engine via ops.curator.curator_service..."
"$PYTHON_BIN" -m ops.curator.curator_service --scope all

log "Nightly Knowledge Graph Curation cycle completed successfully."


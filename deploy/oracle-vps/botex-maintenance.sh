#!/usr/bin/env bash
# botex-maintenance.sh - Periodic background maintenance, knowledge graph
# re-indexing, SQLite WAL checkpointing, and cache optimization for BoteX.
#
# Fired periodically by botex-maintenance.timer at low scheduling priority (nice 19 / SCHED_IDLE).
# Performs real operational housekeeping:
#   1. Re-indexes Memory Vault and refreshes Obsidian _GRAPH_INDEX.md
#   2. Runs SQLite WAL checkpoints (TRUNCATE) and PRAGMA optimize on index.db
#   3. Rotates scratchpad logs and cleans expired execution artifacts
#   4. Re-evaluates graph health and link topology
#   5. Warms provider metadata caches (model catalog, pricing, benchmarks)
#      and sweeps ghost chat-session files (idle-time, off the request path)

set -euo pipefail

BOTEX_DIR="${BOTEX_DIR:-/opt/botex}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${LOG_DIR:-/var/log/botex}"

log() { printf '[botex-maintenance] %s\n' "$*"; }

log "Starting BoteX background maintenance cycle..."

# 1. Run BoteX maintenance tasks via Python
if [ -d "$BOTEX_DIR" ]; then
    cd "$BOTEX_DIR"
    log "Re-indexing Memory Vault and verifying knowledge graph integrity..."
    "$PYTHON_BIN" - <<'EOF'
import os
import sys
from pathlib import Path

try:
    from botex import memory, obsidian

    # Synchronize and optimize memory vaults
    workspace = Path(os.environ.get("BOTEX_WORKSPACE", ".")).resolve()
    dirs = [
        workspace / ".botex" / "memory",
        Path.home() / ".botex" / "memory",
    ]
    for d in dirs:
        if d.exists() and d.is_dir():
            memory.sync_memory_index(d)
            # SQLite maintenance
            conn = memory._get_index_db(d)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.execute("PRAGMA optimize;")
            finally:
                conn.close()

    # Refresh Obsidian graph index
    obsidian.export_graph_index_markdown(workspace_dir=str(workspace), scope="all")

    # Audit graph health
    health = memory.find_graph_health(workspace_dir=str(workspace), scope="all")
    print(f"Graph audit complete: {health.get('total_nodes', 0)} nodes, is_healthy={health.get('is_healthy', False)}")
except Exception as exc:
    print(f"Maintenance task notice: {exc}", file=sys.stderr)
EOF

    # 1b. Idle-time provider metadata warmup + ghost chat-session sweep.
    # Keeps the request path read-only against warm caches: model catalog,
    # pricing guardrail, and benchmarks refresh here (TTL-aware) instead of
    # inside a user's chat turn.
    log "Warming provider metadata caches and sweeping ghost chat sessions..."
    "$PYTHON_BIN" - <<'EOF'
import json
import os
import sys
from pathlib import Path

try:
    from botex.ops.maintenance import sweep_empty_chat_sessions, warm_provider_metadata

    workspace = str(Path(os.environ.get("BOTEX_WORKSPACE", ".")).resolve())
    warm = warm_provider_metadata(workspace_dir=workspace)
    print(f"Provider metadata warmup: {json.dumps(warm, default=str)}")
    removed = sweep_empty_chat_sessions(workspace_dir=workspace)
    print(f"Ghost chat-session sweep: removed {removed} empty session file(s)")
except Exception as exc:
    print(f"Metadata warmup notice: {exc}", file=sys.stderr)
EOF
fi

# 2. Scratchpad log rotation & temporary cleanup
log "Pruning expired scratchpad logs (>3 days)..."
find "${BOTEX_DIR}/.botex/scratchpad" -type f -name "*.log" -mtime +3 -delete 2>/dev/null || true

log "BoteX background maintenance cycle completed successfully."

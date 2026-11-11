#!/usr/bin/env bash
# botex-maintenance.sh - Thin tick wrapper around the Curator task registry.
#
# Fired periodically by botex-maintenance.timer at low scheduling priority (nice 19 / SCHED_IDLE).
# All housekeeping (vault re-index + WAL optimize, graph health, frontmatter ID repair,
# ghost chat-session sweep, ledger rotation, provider metadata warmup, scratchpad pruning)
# lives in botex/ops/curator/tasks.py and is interval-gated there; this script only
# triggers a tick.

set -euo pipefail

BOTEX_DIR="${BOTEX_DIR:-/opt/botex}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '[botex-maintenance] %s\n' "$*"; }

log "Starting BoteX background maintenance tick..."

if [ -d "$BOTEX_DIR" ]; then
    cd "$BOTEX_DIR"
    "$PYTHON_BIN" -m botex.ops.curator.tasks --workspace "${BOTEX_WORKSPACE:-$BOTEX_DIR}" \
        || log "Curator task tick reported failures (see output above)."
fi

log "BoteX background maintenance tick finished."

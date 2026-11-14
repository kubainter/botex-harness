#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
# Portable BoteX Edge Bridge launcher (reads BOTEX_EDGE_* from env or .env).
# Usage:
#   ./bin/botex-edge.sh
#   ./bin/botex-edge.sh -w /path/to/repo --allow-exec
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if command -v python3 >/dev/null 2>&1; then
    exec python3 -u "$ROOT_DIR/server.py" edge "$@"
else
    exec python -u "$ROOT_DIR/server.py" edge "$@"
fi

#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
# BoteX launcher — locates a Python interpreter and runs server.py.
# Usage:
#   ./botex.sh                        -> start the stdio MCP server (headless)
#   ./botex.sh run "task" -w DIR      -> run a task directly
#   ./botex.sh --stats / health / ... -> maintenance commands
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$DIR/server.py" "$@"
else
    exec python "$DIR/server.py" "$@"
fi

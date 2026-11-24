#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
# BoteX launcher — locates a Python interpreter and runs server.py.
# Usage:
#   ./bin/botex.sh                        -> interactive REPL (stdio MCP server when piped)
#   ./bin/botex.sh run "task" -w DIR      -> run a task directly
#   ./bin/botex.sh --stats / health / ... -> maintenance commands
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
# Under mintty/Git Bash a native Windows Python sees stdin as a pipe
# (isatty()=False) and would silently drop into stdio MCP mode. The shell
# detects the pty correctly, so force the REPL explicitly on bare launches.
if [ $# -eq 0 ] && [ -t 0 ]; then
    set -- repl
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$ROOT_DIR/server.py" "$@"
else
    exec python "$ROOT_DIR/server.py" "$@"
fi

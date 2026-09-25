@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
rem BoteX launcher — locates a Python interpreter and runs server.py.
rem Usage:
rem   botex                       -> start the stdio MCP server (headless)
rem   botex run "task" -w DIR     -> run a task directly
rem   botex --stats / health / ...-> maintenance commands
where py >nul 2>nul
if %errorlevel% == 0 (
    py -3 "%~dp0server.py" %*
) else (
    python "%~dp0server.py" %*
)
exit /b %errorlevel%

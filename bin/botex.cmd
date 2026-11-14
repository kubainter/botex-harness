@echo off
rem SPDX-License-Identifier: Apache-2.0
rem Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
rem BoteX launcher — locates a Python interpreter and runs server.py.
rem Usage:
rem   bin\botex.cmd                       -> start the stdio MCP server (headless)
rem   bin\botex.cmd run "task" -w DIR     -> run a task directly
rem   bin\botex.cmd --stats / health / ...-> maintenance commands
where py >nul 2>nul
if %errorlevel% == 0 (
    py -3 "%~dp0..\server.py" %*
) else (
    python "%~dp0..\server.py" %*
)
exit /b %errorlevel%

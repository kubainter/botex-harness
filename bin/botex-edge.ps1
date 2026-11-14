# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
# Portable BoteX Edge Bridge launcher (reads BOTEX_EDGE_* from env or .env).
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$RootDir = Split-Path -Parent $PSScriptRoot
$ServerPy = Join-Path $RootDir "server.py"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -u $ServerPy edge @ExtraArgs
} else {
    & python -u $ServerPy edge @ExtraArgs
}
exit $LASTEXITCODE

#!/usr/bin/env bash
# bielik-bench.sh - Compare Bielik GGUF builds on the llama-server API (TTFT, tok/s, JSON validity).
#
# Usage: bielik-bench.sh [BASE_URL] [RUNS]
#   BASE_URL defaults to http://127.0.0.1:8080 (bielik.service). Run once per model
#   build (switch the service between runs) and diff the printed summaries.
# Needs: curl, python3. Read-only: sends completion requests, changes nothing.

set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8080}"
RUNS="${2:-5}"

BASE_URL="$BASE_URL" RUNS="$RUNS" python3 -u - <<'EOF'
import json
import os
import statistics
import time
import urllib.request

base = os.environ["BASE_URL"].rstrip("/")
runs = int(os.environ["RUNS"])

SHORT = "Zwroc wylacznie JSON {\"kind\": <decision|lesson|note>, \"stale\": <true|false>} dla notatki: 'Zmiana limitu RPM w config na 30.'"
LONG_CTX = ("Notatka o architekturze BoteX. " * 120)
LONG = LONG_CTX + "\nPodsumuj w 2 zdaniach."


def call(prompt, max_tokens):
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": True,
        }
    ).encode()
    req = urllib.request.Request(
        base + "/v1/chat/completions", body, {"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    ttft = None
    text, n = [], 0
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            delta = json.loads(line[5:])["choices"][0].get("delta", {}).get("content")
            if delta:
                if ttft is None:
                    ttft = time.monotonic() - t0
                text.append(delta)
                n += 1
    total = time.monotonic() - t0
    return ttft or total, n / max(total - (ttft or 0), 1e-6), "".join(text)


def valid_json(s):
    s = s.strip().strip("`")
    if s.startswith("json"):
        s = s[4:]
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


for label, prompt, mt in (("short(<500tok)", SHORT, 64), ("long(~1.2k tok)", LONG, 96)):
    ttfts, tps, ok = [], [], 0
    for _ in range(runs):
        t, r, text = call(prompt, mt)
        ttfts.append(t)
        tps.append(r)
        ok += valid_json(text) if label.startswith("short") else 0
    line = f"{label}: TTFT median {statistics.median(ttfts):.2f}s, tok/s median {statistics.median(tps):.2f}"
    if label.startswith("short"):
        line += f", valid JSON {ok}/{runs}"
    print(line)
EOF

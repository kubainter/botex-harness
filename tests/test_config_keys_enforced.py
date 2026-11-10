"""Enforce-or-delete guard for config keys (arena v2.1.0 proactive #1).

A config key advertised in botex.config.json without a code consumer is a
dead control (arena finding: cloud.allowed_workspaces and recall_rpm lived
unconsumed for a week). If you intentionally remove or rename a consumer,
update BOTH the key and docs/IMPORT_MAP.md — and this test.
"""

import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# key -> files whose source must reference it (the consumer contract).
# "WorkspaceOutOfBounds" guards the enforcement *surface*: the 403 handler
# (server_cloud) and the MCP graceful envelope (server) must keep importing
# the exception raised by resolve_workspace containment.
GUARDED_KEYS = {
    "allowed_workspaces": [
        REPO / "botex" / "server" / "config.py",
    ],
    "WorkspaceOutOfBounds": [
        REPO / "botex" / "server" / "server_cloud.py",
        REPO / "server.py",
    ],
    "recall_rpm": [
        REPO / "server.py",
    ],
}


def test_guarded_config_keys_have_consumers():
    missing = []
    for key, files in GUARDED_KEYS.items():
        for path in files:
            source = path.read_text(encoding="utf-8")
            if key not in source:
                missing.append(f"{key} not consumed in {path.relative_to(REPO)}")
    assert not missing, (
        "Dead config key detected (advertised but unconsumed): "
        + "; ".join(missing)
    )

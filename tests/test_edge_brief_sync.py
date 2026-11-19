# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit & loopback tests for Edge fast-cache bot brief sync (ADR-SWARM-02 Phase 1)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from botex.edge import EdgeCapabilities, EdgeRunner
from botex.edge.runner import (
    get_local_bot_briefs_dir,
    read_local_bot_briefs,
    write_local_bot_brief,
)


@pytest.fixture(autouse=True)
def _isolate_briefs_dir(tmp_path, monkeypatch):
    briefs_root = tmp_path / "bot_briefs"
    monkeypatch.setenv("BOTEX_SWARM_BRIEFS_DIR", str(briefs_root))
    return briefs_root


def test_get_local_bot_briefs_dir_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit_dir"
    assert get_local_bot_briefs_dir(explicit) == explicit
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("BOTEX_SWARM_BRIEFS_DIR", str(env_dir))
    assert get_local_bot_briefs_dir() == env_dir


@pytest.mark.parametrize("bad_slug", ["", "   ", "..", "../evil", "a/b", "a\\b", "bad slug"])
def test_write_local_bot_brief_rejects_invalid_or_traversal_slugs(tmp_path, bad_slug):
    result = write_local_bot_brief(bad_slug, namespace="ns", etag="e1", content="hi")
    assert result is None
    assert not list((tmp_path / "bot_briefs").glob("*"))


def test_write_and_double_overwrite_atomic_on_windows_and_masks_secrets(tmp_path):
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    bearer = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    p1 = write_local_bot_brief(
        "witcher3-guardian",
        namespace="witcher3",
        etag="etag-v1",
        fencing_epoch=1,
        content=f"Keep script invariants safe.\nleak {secret}",
        updated_at=f"2026-11-19T00:00:00Z {bearer}",
    )
    assert p1 is not None and p1.exists()

    p2 = write_local_bot_brief(
        "witcher3-guardian",
        namespace="witcher3",
        etag="etag-v2",
        fencing_epoch=2,
        content="Updated brief body.",
        updated_at="2026-11-19T00:05:00Z",
    )
    assert p2 == p1
    leftovers = [f.name for f in p2.parent.iterdir() if f.suffix == ".tmp"]
    assert leftovers == []

    text = p2.read_text(encoding="utf-8")
    assert secret not in text
    assert bearer not in text
    assert "etag: etag-v2" in text

    entries = read_local_bot_briefs(namespace="witcher3")
    assert len(entries) == 1
    assert entries[0]["slug"] == "witcher3-guardian"
    assert entries[0]["etag"] == "etag-v2"
    assert entries[0]["fencing_epoch"] == 2
    assert entries[0]["content"] == "Updated brief body."

    p2.unlink()
    assert not p2.exists()


def test_read_local_bot_briefs_filters_namespace_and_skips_corrupt(tmp_path):
    briefs_dir = tmp_path / "bot_briefs"
    assert read_local_bot_briefs() == []

    write_local_bot_brief("alpha-bot", namespace="ns-a", etag="ea", content="Alpha")
    write_local_bot_brief("beta-bot", namespace="ns-b", etag="eb", content="Beta")

    (briefs_dir / "corrupt-no-close.md").write_text("---\nslug: bad\n", encoding="utf-8")
    (briefs_dir / "corrupt-no-slug.md").write_text(
        "---\nnamespace: ns-a\n---\nbody", encoding="utf-8"
    )
    (briefs_dir / "plain.md").write_text("not frontmatter", encoding="utf-8")

    all_entries = read_local_bot_briefs()
    assert [e["slug"] for e in all_entries] == ["alpha-bot", "beta-bot"]

    ns_a = read_local_bot_briefs(namespace="ns-a")
    assert [e["slug"] for e in ns_a] == ["alpha-bot"]


def test_edge_runner_serve_brief_sync_writes_valid_and_skips_malformed(tmp_path):
    from websockets.asyncio.server import serve

    events: dict = {}

    async def handler(ws):
        hello = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {
                    "type": "hello_ack",
                    "device_id": hello["device_id"],
                    "workspace_id": hello["workspace_id"],
                    "server_version": "2.1.0",
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "brief_sync",
                    "bots": [
                        "not-a-dict",
                        {},
                        {"slug": "../escape", "namespace": "ns", "content": "bad"},
                        {
                            "slug": "botex-architect",
                            "namespace": "swarm",
                            "etag": "etag-42",
                            "fencing_epoch": 3,
                            "content": "Watch harness invariants.",
                            "updated_at": "2026-11-19T00:10:00Z",
                        },
                    ],
                }
            )
        )
        await ws.send(json.dumps({"type": "ping", "ts": 99}))
        events["pong"] = json.loads(await ws.recv())
        await ws.close(code=1000)

    async def run():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runner = EdgeRunner(
                f"ws://127.0.0.1:{port}",
                token="tok_test",
                device_id="dev-brief",
                workspace_id="ws-brief",
                workspace_root=tmp_path,
                caps=EdgeCapabilities(),
            )
            return await runner.run_once()

    status = asyncio.run(run())
    assert status == "closed"
    assert events["pong"] == {"type": "pong", "ts": 99}

    entries = read_local_bot_briefs()
    assert len(entries) == 1
    assert entries[0]["slug"] == "botex-architect"
    assert entries[0]["etag"] == "etag-42"
    assert entries[0]["fencing_epoch"] == 3
    assert "Watch harness invariants." in entries[0]["content"]


def test_write_local_bot_brief_skips_stale_epoch_preserves_role_desc_and_prunes_disabled(tmp_path):
    from botex.edge.runner import _parse_local_bot_brief

    assert _parse_local_bot_brief("---\nslug: ../evil\n---\nbody") is None

    p_new = write_local_bot_brief(
        "epoch-bot",
        namespace="swarm",
        role_desc="Guard invariants",
        etag="etag-2",
        fencing_epoch=2,
        content="newer body",
    )
    assert p_new is not None
    # Stale epoch=1 and epoch=0 must not overwrite epoch=2
    write_local_bot_brief(
        "epoch-bot",
        namespace="swarm",
        role_desc="Old role",
        etag="etag-1",
        fencing_epoch=1,
        content="older body",
    )
    write_local_bot_brief(
        "epoch-bot",
        namespace="swarm",
        role_desc="Zero role",
        etag="etag-0",
        fencing_epoch=0,
        content="zero body",
    )
    entries = read_local_bot_briefs(namespace="swarm")
    assert len(entries) == 1
    assert entries[0]["fencing_epoch"] == 2
    assert entries[0]["content"] == "newer body"
    assert entries[0]["role_desc"] == "Guard invariants"

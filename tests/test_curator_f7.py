# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Phase F7 Autonomous Knowledge Graph Curator."""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botex import memory
from botex.memory.memory import (
    _is_ledger_node,
    _normalize_project_slug,
    find_graph_health,
    save_memory,
    set_memory_status,
)
from botex.ops.curator import tasks as curator_tasks
from botex.ops.curator.curator_service import (
    LOCK_MAX_STALE_SECONDS,
    CuratorLock,
    CuratorService,
    _apply_recommended_links,
    _cluster_mtime_hash,
    _compute_deadline,
    _compute_health_assessment,
    _mtime_hash,
    check_active_agent_tasks,
)
from botex.ops.migrate_projects import SENTINEL_FILENAME, migrate_memory_dir


def _write_raw_note(mem_dir, stem: str, frontmatter: str, body: str) -> None:
    (mem_dir / f"{stem}.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


def test_project_slug_alias_unification_and_migration_ordering(tmp_path):
    assert _normalize_project_slug("the-witcher-3-wild-hunt-goty") == "witcher3"
    assert _normalize_project_slug("witcher-3") == "witcher3"

    mem_dir = tmp_path / "vault"
    mem_dir.mkdir()
    _write_raw_note(
        mem_dir,
        "w3_goty",
        "id: w3_goty\ntitle: GOTY Note\nproject: the-witcher-3-wild-hunt-goty",
        "w2scene notes",
    )
    _write_raw_note(
        mem_dir,
        "w3_dash",
        "id: w3_dash\ntitle: Dash Note\nproject: witcher-3",
        "cr2w notes",
    )
    _write_raw_note(
        mem_dir,
        "w3_canon",
        "id: w3_canon\ntitle: Canon Note\nproject: witcher3",
        "already canonical",
    )

    res = migrate_memory_dir(mem_dir)
    assert res["ok"] is True
    assert res["updated"] == 2
    assert (mem_dir / SENTINEL_FILENAME).exists()

    for stem in ("w3_goty", "w3_dash", "w3_canon"):
        meta, _ = memory._parse_frontmatter((mem_dir / f"{stem}.md").read_text(encoding="utf-8"))
        assert meta["project"] == "witcher3"


def test_find_graph_health_project_ledger_split_and_sorting(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    save_memory(
        workspace_dir=str(ws),
        memory_id="z_botex_note",
        title="Botex Z",
        content="isolated botex",
        kind="context",
        tags=["arch"],
        project="botex",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="a_witcher_note",
        title="Witcher A",
        content="isolated witcher",
        kind="context",
        tags=["w2scene"],
        project="the-witcher-3-wild-hunt-goty",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="task-receipt_1",
        title="Receipt",
        content="receipt",
        kind="context",
        tags=["task-receipt"],
        project="botex",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="curator_health_status",
        title="Beacon",
        content="beacon",
        kind="system",
        tags=["curator"],
        project="botex",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="temp_1",
        title="Temp Handoff",
        content="ephemeral",
        kind="handoff",
        tags=["temp-handoff"],
        project="swarm",
    )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    assert health["ok"] is True
    assert health["ledger_count"] == 3
    assert health["orphan_count"] == 2

    orphans = health["orphans"]
    assert [(o["project"], o["id"]) for o in orphans] == [
        ("botex", "z_botex_note"),
        ("witcher3", "a_witcher_note"),
    ]
    assert orphans[0]["mtime"] > 0
    assert orphans[0]["confidence"] == 1.0


def test_default_tasks_include_dead_link_repair_and_purge_ephemeral():
    names = {t.name for t in curator_tasks.DEFAULT_TASKS}
    assert "dead_link_repair" in names
    assert "purge_ephemeral_memories" in names


def test_curator_lock_live_pid_not_broken_after_stale_seconds(tmp_path):
    lock_file = tmp_path / "curator.lock"
    old_started = time.time() - (LOCK_MAX_STALE_SECONDS + 3600.0)
    lock_file.write_text(
        json.dumps({"pid": os.getpid(), "started_at": old_started, "run_id": "live_old"}),
        encoding="utf-8",
    )

    lock2 = CuratorLock(lock_file)
    assert lock2.acquire("run_new", force=False) is False

    # Owner heartbeat updates heartbeat_at
    lock_owner = CuratorLock(lock_file)
    lock_owner.acquired = True
    lock_owner.heartbeat()
    refreshed = json.loads(lock_file.read_text(encoding="utf-8"))
    assert refreshed["heartbeat_at"] > old_started

    # Dead PID lock is broken
    lock_file.write_text(
        json.dumps({"pid": 99999999, "started_at": time.time(), "run_id": "dead"}),
        encoding="utf-8",
    )
    with patch("botex.ops.curator.curator_service._is_pid_alive", return_value=False):
        assert lock2.acquire("run_takeover", force=False) is True
    lock2.release()


@pytest.mark.anyio
async def test_drain_state_checkpoint_and_resume(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_FREE_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("BOTEX_CURATOR_POLL_HTTP", "0")

    save_memory(
        workspace_dir=str(ws),
        memory_id="orphan_one",
        title="Orphan One",
        content="First orphan",
        kind="lesson",
        tags=["alpha"],
        project="botex",
    )

    svc = CuratorService(workspace_dir=str(ws))
    res1 = await svc.run(scope="local", force=True)
    assert res1.status == "COMPLETED"
    assert res1.batches_processed == 1

    drain_file = ws / ".botex" / "curator" / "drain_state.json"
    assert drain_file.exists()
    state1 = json.loads(drain_file.read_text(encoding="utf-8"))
    assert "orphan_one" in state1

    # Second run skips already-checkpointed orphan
    res2 = await svc.run(scope="local", force=True)
    assert res2.batches_processed == 0

    # Modifying the note changes mtime -> re-enters drain queue
    note_path = ws / ".botex" / "memory" / "orphan_one.md"
    new_t = time.time() + 10.0
    os.utime(note_path, (new_t, new_t))
    res3 = await svc.run(scope="local", force=True)
    assert res3.batches_processed == 1


def test_apply_recommended_links_gates_and_append_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    save_memory(
        workspace_dir=str(ws),
        memory_id="src_botex",
        title="Source Botex",
        content="Original body must stay intact.",
        kind="context",
        tags=["arch"],
        project="botex",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="tgt_botex",
        title="Target Botex",
        content="Target body.",
        kind="context",
        tags=["arch"],
        project="botex",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="tgt_witcher_ctx",
        title="Witcher Context",
        content="Different project context.",
        kind="context",
        tags=["arch"],
        project="witcher3",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="src_dec",
        title="Botex Decision",
        content="High confidence decision.",
        kind="decision",
        tags=["arch"],
        project="botex",
        confidence=0.9,
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="tgt_dec",
        title="Swarm Lesson",
        content="High confidence lesson.",
        kind="lesson",
        tags=["arch"],
        project="swarm",
        confidence=0.9,
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="temp_handoff_node",
        title="Temp",
        content="Ephemeral",
        kind="handoff",
        tags=["temp-handoff"],
        project="botex",
    )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    node_by_id = {n["id"]: n for n in health["all_nodes"]}

    recs = [
        {"source_id": "src_botex", "target_id": "tgt_botex", "rationale": "Same project"},
        {"source_id": "src_botex", "target_id": "missing_id", "rationale": "Missing target"},
        {"source_id": "src_botex", "target_id": "tgt_witcher_ctx", "rationale": "Cross ctx"},
        {"source_id": "src_botex", "target_id": "temp_handoff_node", "rationale": "Ledger"},
        {"source_id": "src_dec", "target_id": "tgt_dec", "rationale": "Cross high-conf"},
    ]
    res = _apply_recommended_links(str(ws), recs, node_by_id)
    assert res["applied"] == 2
    assert res["skipped"] == 3

    src_text = (ws / ".botex" / "memory" / "src_botex.md").read_text(encoding="utf-8")
    assert "Original body must stay intact." in src_text
    assert "## Related" in src_text
    assert "[[tgt_botex]]" in src_text


def test_compute_deadline_and_health_assessment():
    d1 = _compute_deadline("05:30", now_dt=datetime(2026, 1, 1, 1, 0), now_mono=1000.0)
    assert d1 == pytest.approx(1000.0 + 4.5 * 3600.0)

    d2 = _compute_deadline("05:30", now_dt=datetime(2026, 1, 1, 23, 30), now_mono=1000.0)
    assert d2 == pytest.approx(1000.0 + 6.0 * 3600.0)

    assess = _compute_health_assessment(
        {"total_nodes": 20, "orphan_count": 12, "broken_count": 1, "ledger_count": 2}
    )
    assert assess["severity"] == "degraded"
    assert len(assess["degraded_reasons"]) == 2
    assert assess["ledger_count"] == 2


@pytest.mark.anyio
async def test_cloud_lane_forwards_openrouter_provider_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    svc = CuratorService(workspace_dir=str(tmp_path))

    providers_cfg = svc._load_providers_config()
    assert "openrouter-free" in providers_cfg
    assert (
        providers_cfg["openrouter-free"]
        .get("extra_body", {})
        .get("provider", {})
        .get("data_collection")
        == "allow"
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'))]
        )
    )
    mock_provider = MagicMock()
    mock_provider.get_client.return_value = mock_client

    with patch("botex.providers.ProviderRegistry.get", return_value=mock_provider) as mock_get:
        out = await svc._try_cloud_lane_inference("openrouter-free", "prompt", providers_cfg, None)

    assert out == '{"summary":"ok"}'
    mock_get.assert_called_once_with(
        "openrouter-free", provider_cfg=providers_cfg["openrouter-free"]
    )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["extra_body"]["provider"]["data_collection"] == "allow"


def test_check_active_agent_tasks_polls_http(monkeypatch):
    monkeypatch.setenv("BOTEX_CURATOR_POLL_HTTP", "1")
    fake_resp = io.BytesIO(json.dumps({"tasks": [{"task_id": "t1", "status": "RUNNING"}]}).encode())
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = SimpleNamespace(status=200, read=fake_resp.read)
    fake_cm.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=fake_cm):
        assert check_active_agent_tasks() is True


def test_set_memory_status_updates_status_adds_timeline_edge_and_preserves_frontmatter(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = save_memory(
        workspace_dir=str(ws),
        memory_id="legacy_node",
        title="Legacy Architecture Note",
        content="Primary technical architecture content before deprecation.",
        kind="decision",
        tags=["arch", "legacy"],
        project="botex",
    )
    assert res["ok"] is True

    ok = set_memory_status(
        workspace_dir=str(ws),
        memory_id="legacy_node",
        status="deprecated",
        superseded_by="modern_node",
        reason="Superseded by event-driven architecture.",
        scope="local",
    )
    assert ok is True

    note_path = ws / ".botex" / "memory" / "legacy_node.md"
    assert note_path.exists()
    raw_text = note_path.read_text(encoding="utf-8")
    meta, body = memory._parse_frontmatter(raw_text)

    # Status and superseded_by in frontmatter
    assert meta["status"] == "deprecated"
    assert meta["superseded_by"] == "modern_node"
    # Preserved frontmatter properties
    assert meta["title"] == "Legacy Architecture Note"
    assert meta["project"] == "botex"
    assert meta["kind"] == "decision"
    # Timeline section with active edge [[modern_node]]
    assert "## Timeline" in raw_text
    assert (
        "Deprecated in favor of [[modern_node]]. Reason: Superseded by event-driven architecture."
        in raw_text
    )

    # Graph edge created
    health = find_graph_health(workspace_dir=str(ws), scope="local")
    assert health["ok"] is True
    edges = health["edges"]
    assert any(e["source"] == "legacy_node" and e["target"] == "modern_node" for e in edges)


def test_is_ledger_node_and_deprecation_orphan_exclusion(tmp_path):
    # Direct dictionary checks
    assert _is_ledger_node({"id": "note_1", "status": "deprecated"}) is True
    assert _is_ledger_node({"id": "note_2", "status": "superseded"}) is True
    assert _is_ledger_node({"id": "note_3", "status": "active", "tags": ["task-receipt"]}) is True
    assert _is_ledger_node({"id": "note_4", "status": "active", "kind": "decision"}) is False

    # Graph health check
    ws = tmp_path / "ws"
    ws.mkdir()
    save_memory(
        workspace_dir=str(ws),
        memory_id="normal_orphan",
        title="Normal Orphan",
        content="Isolated active note.",
        kind="context",
        project="botex",
        status="active",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="deprecated_orphan",
        title="Deprecated Orphan",
        content="Isolated deprecated note.",
        kind="context",
        project="botex",
        status="active",
    )
    # Transition to deprecated
    set_memory_status(
        workspace_dir=str(ws),
        memory_id="deprecated_orphan",
        status="deprecated",
        reason="No longer relevant.",
        scope="local",
    )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    assert health["ok"] is True
    # normal_orphan is in orphans, but deprecated_orphan is excluded from orphans and counted in ledger_count
    orphan_ids = [o["id"] for o in health["orphans"]]
    assert orphan_ids == ["normal_orphan"]
    assert health["orphan_count"] == 1
    assert health["ledger_count"] == 1


@pytest.mark.anyio
async def test_run_compiled_truth_phase_candidate_selection_and_human_freeze_gate(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    # 1. Target Hub: Active, decision, project=botex
    save_memory(
        workspace_dir=str(ws),
        memory_id="hub_target",
        title="Core Hub Decision",
        content="Central decision note with many references.",
        kind="decision",
        tags=["core"],
        project="botex",
        status="active",
    )

    # 3 satellite notes linking to hub_target -> in_degree = 3
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"sat_{i}",
            title=f"Satellite {i}",
            content="Reference to [[hub_target]] in cluster.",
            kind="context",
            tags=["core"],
            project="botex",
            status="active",
        )

    # 2. Candidate with in_degree < 3
    save_memory(
        workspace_dir=str(ws),
        memory_id="low_deg_node",
        title="Low Degree",
        content="Only 1 incoming link.",
        kind="decision",
        tags=["core"],
        project="botex",
        status="active",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="low_deg_sat",
        title="Low Deg Sat",
        content="Points to [[low_deg_node]].",
        kind="context",
        tags=["core"],
        project="botex",
        status="active",
    )

    # 3. Candidate with kind == 'lesson' (not decision or context)
    save_memory(
        workspace_dir=str(ws),
        memory_id="lesson_hub",
        title="Lesson Hub",
        content="Lesson note with 3 incoming links.",
        kind="lesson",
        tags=["core"],
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"lesson_sat_{i}",
            title=f"Lesson Sat {i}",
            content="Points to [[lesson_hub]].",
            kind="context",
            tags=["core"],
            project="botex",
            status="active",
        )

    # 4. Candidate with source == 'human' (Human Freeze Gate 1)
    _write_raw_note(
        ws / ".botex" / "memory",
        "human_source_hub",
        "id: human_source_hub\ntitle: Human Hub\nkind: decision\nproject: botex\nstatus: active\nsource: human\ntags:\n  - core",
        "Human written hub.",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"human_sat_{i}",
            title=f"Human Sat {i}",
            content="Points to [[human_source_hub]].",
            kind="context",
            tags=["core"],
            project="botex",
            status="active",
        )

    # 5. Candidate with frozen tag (Human Freeze Gate 2)
    save_memory(
        workspace_dir=str(ws),
        memory_id="frozen_tag_hub",
        title="Frozen Tag Hub",
        content="Frozen hub.",
        kind="decision",
        tags=["core", "human-curated"],
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"frozen_sat_{i}",
            title=f"Frozen Sat {i}",
            content="Points to [[frozen_tag_hub]].",
            kind="context",
            tags=["core"],
            project="botex",
            status="active",
        )

    # 6. Candidate with manual ## Compiled Truth without marker (Human Freeze Gate 3)
    save_memory(
        workspace_dir=str(ws),
        memory_id="manual_truth_hub",
        title="Manual Truth Hub",
        content="Manual hub.\n\n## Compiled Truth\n\nHuman compiled truth here.",
        kind="decision",
        tags=["core"],
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"manual_sat_{i}",
            title=f"Manual Sat {i}",
            content="Points to [[manual_truth_hub]].",
            kind="context",
            tags=["core"],
            project="botex",
            status="active",
        )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    svc = CuratorService(workspace_dir=str(ws))

    # Mock inference to avoid real API calls
    fake_llm_json = json.dumps(
        {
            "compiled_truth": "Synthesized knowledge from [[sat_1]], [[sat_2]], and [[sat_3]].",
            "sources": ["sat_1", "sat_2", "sat_3"],
            "confidence": "HIGH",
        }
    )
    svc._infer_compiled_truth = AsyncMock(return_value=fake_llm_json)

    res = await svc._run_compiled_truth_phase(health, apply_truth=True, max_clusters=10)
    assert res["ok"] is True
    # Out of all notes, only hub_target, human_source_hub, frozen_tag_hub, manual_truth_hub had in_deg >= 3 and kind in (decision, context)
    # The 3 human-frozen ones were skipped:
    skipped_reasons = {s["target_id"]: s["reason"] for s in res["skipped"]}
    assert skipped_reasons.get("human_source_hub") == "human_freeze_source"
    assert skipped_reasons.get("frozen_tag_hub") == "human_freeze_tag"
    assert skipped_reasons.get("manual_truth_hub") == "human_freeze_manual_section"

    # Only hub_target was synthesized
    compiled_ids = [c["target_id"] for c in res["compiled"]]
    assert compiled_ids == ["hub_target"]
    assert res["clusters_compiled"] == 1


@pytest.mark.anyio
async def test_run_compiled_truth_phase_marker_checking_and_patch_call(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    # Create target note
    save_memory(
        workspace_dir=str(ws),
        memory_id="target_cluster_hub",
        title="Target Hub",
        content="Primary decision node.",
        kind="decision",
        tags=["mesh"],
        project="botex",
        status="active",
    )

    # Create 3 cluster constituents
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"cnode_{i}",
            title=f"Constituent {i}",
            content=f"Detailed evidence {i} pointing to [[target_cluster_hub]].",
            kind="context",
            tags=["mesh"],
            project="botex",
            status="active",
        )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    svc = CuratorService(workspace_dir=str(ws))

    # LLM returns valid citations for cnode_1 and cnode_2, plus an invalid/hallucinated [[phantom_node]]
    fake_truth_output = json.dumps(
        {
            "compiled_truth": (
                "Verified architecture combining [[cnode_1]] and [[cnode_2]]. "
                "Citing hallucinated [[phantom_node]] that must be dropped."
            ),
            "sources": ["cnode_1", "cnode_2", "phantom_node"],
            "confidence": "HIGH",
        }
    )
    svc._infer_compiled_truth = AsyncMock(return_value=fake_truth_output)

    # 1. First run: synthesizes compiled truth and applies patch
    res1 = await svc._run_compiled_truth_phase(health, apply_truth=True, max_clusters=1)
    assert res1["ok"] is True
    assert res1["clusters_compiled"] == 1
    rec = res1["compiled"][0]
    assert rec["target_id"] == "target_cluster_hub"
    assert "phantom_node" not in rec["sources"]
    assert "cnode_1" in rec["sources"]
    cluster_hash1 = rec["cluster_hash"]

    # Verify disk state
    hub_path = ws / ".botex" / "memory" / "target_cluster_hub.md"
    content1 = hub_path.read_text(encoding="utf-8")
    meta1, body1 = memory._parse_frontmatter(content1)

    assert meta1.get("compiled_truth") is True
    assert "## Compiled Truth" in content1
    assert "[[cnode_1]]" in content1
    assert "[[cnode_2]]" in content1
    assert "[[phantom_node]]" not in content1  # Validation dropped hallucinated citation!
    assert f"<!-- curator:ct {cluster_hash1} -->" in content1

    # 2. Second run: unchanged cluster member mtimes -> skipped
    health2 = find_graph_health(workspace_dir=str(ws), scope="local")
    res2 = await svc._run_compiled_truth_phase(health2, apply_truth=True, max_clusters=1)
    assert res2["clusters_compiled"] == 0
    skipped_map = {s["target_id"]: s["reason"] for s in res2["skipped"]}
    assert skipped_map.get("target_cluster_hub") == "hash_unchanged"

    # 3. Third run: modify a cluster constituent note mtime -> hash changes -> re-synthesized!
    cnode_path = ws / ".botex" / "memory" / "cnode_1.md"
    new_mtime = time.time() + 15.0
    os.utime(cnode_path, (new_mtime, new_mtime))

    health3 = find_graph_health(workspace_dir=str(ws), scope="local")
    res3 = await svc._run_compiled_truth_phase(health3, apply_truth=True, max_clusters=1)
    assert res3["clusters_compiled"] == 1
    cluster_hash3 = res3["compiled"][0]["cluster_hash"]
    assert cluster_hash3 != cluster_hash1  # Hash changed and was recompiled!


@pytest.mark.anyio
async def test_curator_service_run_wires_compiled_truth_phase(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_FREE_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("BOTEX_CURATOR_POLL_HTTP", "0")

    save_memory(
        workspace_dir=str(ws),
        memory_id="hub_note",
        title="Hub Note",
        content="Hub decision content.",
        kind="decision",
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"sat_note_{i}",
            title=f"Satellite Note {i}",
            content="Linked to [[hub_note]].",
            kind="context",
            project="botex",
            status="active",
        )

    svc = CuratorService(workspace_dir=str(ws))
    fake_json = json.dumps(
        {
            "compiled_truth": "Compiled truth across cluster [[sat_note_1]].",
            "sources": ["sat_note_1"],
            "confidence": "HIGH",
        }
    )
    svc._infer_compiled_truth = AsyncMock(return_value=fake_json)

    res = await svc.run(scope="local", force=True, apply_links=True, apply_truth=True)
    assert res.status == "COMPLETED"
    assert res.truth_clusters_compiled == 1

    # Status file contains truth_clusters_compiled
    status_file = ws / ".botex" / "curator" / "status.json"
    assert status_file.exists()
    status_data = json.loads(status_file.read_text(encoding="utf-8"))
    assert status_data.get("truth_clusters_compiled") == 1

    # Beacon note reflects Compiled Truth section
    beacon_file = ws / ".botex" / "memory" / "curator_health_status.md"
    assert beacon_file.exists()
    beacon_text = beacon_file.read_text(encoding="utf-8")
    assert "### Zsyntetyzowana Prawda (Compiled Truth)" in beacon_text
    assert "[[hub_note]]" in beacon_text


@pytest.mark.anyio
async def test_compiled_truth_candidate_selection_ignores_self_loops_and_duplicate_edges(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    # Note that links to itself multiple times -> should NOT become candidate
    save_memory(
        workspace_dir=str(ws),
        memory_id="self_loop_hub",
        title="Self Loop Hub",
        content="Self reference [[self_loop_hub]]. Another self [[self_loop_hub]].",
        kind="decision",
        project="botex",
        status="active",
    )

    # Note with only 1 predecessor note, even if duplicated in edge list
    save_memory(
        workspace_dir=str(ws),
        memory_id="single_pred_hub",
        title="Single Pred Hub",
        content="Only one predecessor.",
        kind="decision",
        project="botex",
        status="active",
    )
    save_memory(
        workspace_dir=str(ws),
        memory_id="single_pred_sat",
        title="Single Pred Sat",
        content="Points to [[single_pred_hub]].",
        kind="context",
        project="botex",
        status="active",
    )

    # Note with 3 distinct predecessors -> valid candidate
    save_memory(
        workspace_dir=str(ws),
        memory_id="genuine_hub",
        title="Genuine Hub",
        content="Three distinct incoming nodes.",
        kind="decision",
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"genuine_sat_{i}",
            title=f"Sat {i}",
            content="Points to [[genuine_hub]].",
            kind="context",
            project="botex",
            status="active",
        )

    # Craft health dict with duplicate edges and self-loop edge
    health = find_graph_health(workspace_dir=str(ws), scope="local")
    edges_with_dups = list(health["edges"])
    # Add self-loop edge and duplicate edge
    edges_with_dups.append(
        {"source": "self_loop_hub", "target": "self_loop_hub", "kind": "wikilink"}
    )
    edges_with_dups.append(
        {"source": "single_pred_sat", "target": "single_pred_hub", "kind": "wikilink"}
    )
    edges_with_dups.append(
        {"source": "single_pred_sat", "target": "single_pred_hub", "kind": "wikilink"}
    )
    health["edges"] = edges_with_dups

    svc = CuratorService(workspace_dir=str(ws))
    svc._infer_compiled_truth = AsyncMock(
        return_value=json.dumps(
            {"compiled_truth": "Synthesized [[genuine_sat_1]].", "sources": ["genuine_sat_1"]}
        )
    )

    res = await svc._run_compiled_truth_phase(health, apply_truth=True, max_clusters=5)
    assert res["ok"] is True
    compiled_ids = [c["target_id"] for c in res["compiled"]]
    # Only genuine_hub is compiled; self_loop_hub and single_pred_hub must be excluded!
    assert compiled_ids == ["genuine_hub"]


@pytest.mark.anyio
async def test_compiled_truth_source_brackets_and_alias_citation_cleaning(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    save_memory(
        workspace_dir=str(ws),
        memory_id="alias_hub",
        title="Alias Hub",
        content="Primary node.",
        kind="decision",
        project="botex",
        status="active",
    )
    for i in (1, 2, 3):
        save_memory(
            workspace_dir=str(ws),
            memory_id=f"sub_{i}",
            title=f"Subnode {i}",
            content="Points to [[alias_hub]].",
            kind="context",
            project="botex",
            status="active",
        )

    health = find_graph_health(workspace_dir=str(ws), scope="local")
    svc = CuratorService(workspace_dir=str(ws))

    # LLM returns sources with brackets and alias wikilinks
    llm_payload = json.dumps(
        {
            "compiled_truth": (
                "Verified architecture combining [[sub_1|Subsystem One]] and [[sub_2]]. "
                "Citing hallucinated [[phantom_ghost]] and [[phantom_ghost|Ghost System]]."
            ),
            "sources": ["[[sub_1]]", "[[sub_2|Subsystem Two]]", "phantom_ghost"],
            "confidence": "HIGH",
        }
    )
    svc._infer_compiled_truth = AsyncMock(return_value=llm_payload)

    res = await svc._run_compiled_truth_phase(health, apply_truth=True, max_clusters=1)
    assert res["ok"] is True
    assert res["clusters_compiled"] == 1
    rec = res["compiled"][0]

    # Sources cleanly stripped of brackets and invalid phantom dropped
    assert rec["sources"] == ["sub_1", "sub_2"]

    # Verify disk content has preserved alias for sub_1 and stripped phantom_ghost
    hub_path = ws / ".botex" / "memory" / "alias_hub.md"
    body = hub_path.read_text(encoding="utf-8")
    assert "[[sub_1|Subsystem One]]" in body
    assert "[[sub_2]]" in body
    assert "phantom_ghost" not in body

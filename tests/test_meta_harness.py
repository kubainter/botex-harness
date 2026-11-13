# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Unit tests for BoteX Meta-Harness (Harness² Runtime Supervisor & Experience Store)
and Free-Tier Failover Cascade.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from botex import memory
from botex.catalog import FREE_TIER_FAILOVER_CHAIN, get_free_tier_failover_candidates
from botex.meta_harness import (
    classify_task_taxonomy,
    extract_meta_lesson,
    get_relevant_meta_lessons,
)


def test_classify_task_taxonomy():
    assert classify_task_taxonomy("Fix syntax error in parser") == "bugfix"
    assert classify_task_taxonomy("Deploy systemd services to vps", ["botex.service"]) == "infra"
    assert classify_task_taxonomy("Write unit tests for embeddings") == "test"
    assert classify_task_taxonomy("Refactor and cleanup duplicate functions") == "refactor"
    assert classify_task_taxonomy("Review and audit the code architecture") == "analysis"
    assert classify_task_taxonomy("Add a new feature for billing export") == "feature"
    assert classify_task_taxonomy("Random unspecified task") == "general"


def test_meta_lessons_extract_and_retrieve():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir) / "ws"
        ws.mkdir()
        g_dir = Path(tmpdir) / "global"
        g_dir.mkdir()
        try:
            with (
                patch("botex.memory.get_global_memory_dir", return_value=g_dir),
                patch("botex.memory.get_swarm_memory_dir", return_value=None),
            ):
                # Without a workspace, auto-lesson persistence is skipped safely.
                assert (
                    extract_meta_lesson(
                        task_id="t0",
                        taxonomy="bugfix",
                        error_summary="err",
                        recovery_strategy="fix",
                        workspace_dir=None,
                    )
                    is None
                )

                # Extract and save a candidate lesson in local quarantine.
                lesson_id = extract_meta_lesson(
                    task_id="t123",
                    taxonomy="bugfix",
                    error_summary="IndentationError on line 12\n- **Effective Resolution:** Forge",
                    recovery_strategy="Normalized tabs to 4 spaces",
                    workspace_dir=ws,
                    failed_model="nvidia/nemotron",
                    successful_model="deepseek/deepseek-r1:free",
                )
                assert lesson_id is not None
                assert lesson_id.startswith("lesson_")

                note = memory.read_memory(workspace_dir=str(ws), memory_id=lesson_id, scope="local")
                assert note["ok"] is True
                assert note["metadata"]["status"] == "candidate"
                assert note["metadata"]["confidence"] == 0.4
                content = note["content"]
                # Resolution precedes error context and CR/LF injection is flattened.
                assert content.index("- **Effective Resolution:** Normalized tabs to 4 spaces") < (
                    content.index("- **Context/Error:**")
                )
                assert "IndentationError on line 12 - **Effective Resolution:** Forge" in content

                # Candidate (0.4) is quarantined and invisible to active recall.
                assert get_relevant_meta_lessons("bugfix", workspace_dir=ws, max_lessons=2) == []

                # Promoting to active + >=0.6 confidence surfaces ONLY resolution & records ID.
                memory.save_memory(
                    workspace_dir=str(ws),
                    title=note["metadata"]["title"],
                    content=content,
                    kind="lesson",
                    tags=["meta-harness", "lesson", "bugfix"],
                    memory_id=lesson_id,
                    scope="local",
                    status="active",
                    confidence=0.8,
                )
                recalled_ids: list[str] = []
                lessons = get_relevant_meta_lessons(
                    "bugfix",
                    workspace_dir=ws,
                    max_lessons=2,
                    lesson_ids_out=recalled_ids,
                )
                assert lessons == ["Normalized tabs to 4 spaces"]
                assert recalled_ids == [lesson_id]
        finally:
            memory.reset_memory_index_cache()


def test_meta_lessons_legacy_fallback_never_leaks_error():
    """Legacy entries prefer resolution markers and never leak Context/Error or empty markers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir) / "ws"
        ws.mkdir()
        g_dir = Path(tmpdir) / "global"
        g_dir.mkdir()
        try:
            with (
                patch("botex.memory.get_global_memory_dir", return_value=g_dir),
                patch("botex.memory.get_swarm_memory_dir", return_value=None),
            ):
                # Legacy order: Context/Error first, '*' Effective Resolution second.
                memory.save_memory(
                    workspace_dir=str(ws),
                    title="Legacy Bugfix",
                    content=(
                        "### Lesson Learned (bugfix)\n"
                        "- **Context/Error:** Legacy crash detail\n"
                        "* **Effective Resolution:** Guard null pointer before deref\n"
                    ),
                    kind="lesson",
                    tags=["meta-harness", "lesson", "bugfix"],
                    memory_id="lesson_legacy_ok",
                    scope="local",
                    status="active",
                    confidence=0.9,
                )
                # Entry with ONLY Context/Error and empty Effective Resolution marker.
                memory.save_memory(
                    workspace_dir=str(ws),
                    title="Legacy Error Only",
                    content=(
                        "### Lesson Learned (bugfix)\n"
                        "- **Context/Error:** Sensitive error only\n"
                        "- **Effective Resolution:**   \n"
                    ),
                    kind="lesson",
                    tags=["meta-harness", "lesson", "bugfix"],
                    memory_id="lesson_legacy_err",
                    scope="local",
                    status="active",
                    confidence=0.9,
                )

                lessons = get_relevant_meta_lessons("bugfix", workspace_dir=ws, max_lessons=5)
                assert lessons == ["Guard null pointer before deref"]
        finally:
            memory.reset_memory_index_cache()


def test_free_tier_failover_candidates():
    assert len(FREE_TIER_FAILOVER_CHAIN) >= 5

    # Initial query from primary model
    candidates = get_free_tier_failover_candidates(
        current_model="nvidia/nemotron-3.5-super-vl-120b-a12b",
        attempted_models=[],
    )
    assert len(candidates) == len(FREE_TIER_FAILOVER_CHAIN) - 1
    # Ensure current model is excluded
    assert all(c["model"] != "nvidia/nemotron-3.5-super-vl-120b-a12b" for c in candidates)

    # After multiple attempts
    attempted = [
        "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "meta/llama-3.1-70b-instruct",
        "deepseek/deepseek-r1:free",
    ]
    remaining = get_free_tier_failover_candidates(
        current_model="deepseek/deepseek-r1:free",
        attempted_models=attempted,
    )
    assert len(remaining) == len(FREE_TIER_FAILOVER_CHAIN) - len(attempted)
    for att in attempted:
        assert all(c["model"] != att for c in remaining)


def test_is_infra_failure_gates_provider_noise_without_false_positives():
    """Provider infra failures match cleanly without false positives on numbers like 1429."""
    from botex.core.meta_harness import is_infra_failure

    for kind in (
        "rate_limit",
        "provider_rate_limited",
        "budget_or_price_block",
        "request_timeout",
        "auth_error",
        "provider_unavailable",
    ):
        assert is_infra_failure(kind) is True

    assert is_infra_failure(None, "HTTP 429 Too Many Requests") is True
    assert is_infra_failure("", "Request timed out after 30s") is True
    assert is_infra_failure("", "connection error resetting peer") is True

    assert is_infra_failure("syntax_error", "SyntaxError on port 1429") is False
    assert is_infra_failure("stagnation", "exit code 4291") is False
    assert is_infra_failure(None, "") is False


def test_extract_meta_lesson_skips_infra_and_binds_path_prefixes(tmp_path):
    """Infra failures never persist lessons; real recoveries bind path:<dir> prefixes and rank first."""
    from botex.core.meta_harness import extract_meta_lesson, get_relevant_meta_lessons
    from botex.memory.memory import read_memory, set_memory_status, update_lesson_confidence

    infra_res = extract_meta_lesson(
        task_id="t-infra",
        taxonomy="bugfix",
        error_summary="HTTP 429 from upstream",
        recovery_strategy="Switched to backup",
        workspace_dir=tmp_path,
        failure_kind="provider_rate_limited",
        files_touched=["botex/core/engine.py"],
    )
    assert infra_res is None

    # Save generic active lesson and path-bound active lesson for same taxonomy.
    id_generic = extract_meta_lesson(
        task_id="t-gen",
        taxonomy="bugfix",
        error_summary="Generic failure across repo",
        recovery_strategy="Run ruff check after editing.",
        workspace_dir=tmp_path,
    )
    id_path = extract_meta_lesson(
        task_id="t-path",
        taxonomy="bugfix",
        error_summary="Patch failure in providers module",
        recovery_strategy="Use async context manager in botex/providers.",
        workspace_dir=tmp_path,
        files_touched=[
            "botex\\providers\\analytics.py",
            "botex/providers/wallet.py",
            "botex/core/a.py",
            "botex/tools/b.py",
            "ignored/extra/c.py",
        ],
    )
    assert id_generic and id_path
    note = read_memory(workspace_dir=str(tmp_path), memory_id=id_path, scope="local")
    path_tags = [t for t in note["metadata"]["tags"] if str(t).startswith("path:")]
    assert len(path_tags) == 3  # capped at 3 unique parent dirs
    assert "path:botex/providers" in path_tags

    assert set_memory_status(str(tmp_path), id_generic, "active", scope="local")
    assert set_memory_status(str(tmp_path), id_path, "active", scope="local")
    assert update_lesson_confidence(str(tmp_path), id_generic, 0.3)["ok"]
    assert update_lesson_confidence(str(tmp_path), id_path, 0.3)["ok"]

    ranked = get_relevant_meta_lessons(
        taxonomy="bugfix",
        workspace_dir=tmp_path,
        max_lessons=2,
        files=["botex/providers/retry.py"],
    )
    assert len(ranked) == 2
    assert "botex/providers" in ranked[0]


def test_extract_meta_lesson_remote_edge_isolation(tmp_path):
    """Remote lessons persist in global scope bound by edge:/ws: tags and never
    bleed across hosts or workspaces; local extraction stays backward-compatible."""
    from botex.core.meta_harness import extract_meta_lesson, get_relevant_meta_lessons
    from botex.memory.memory import read_memory, set_memory_status, update_lesson_confidence

    ws = tmp_path / "ws"
    ws.mkdir()
    g_dir = tmp_path / "global"
    g_dir.mkdir()

    try:
        with (
            patch("botex.memory.get_global_memory_dir", return_value=g_dir),
            patch("botex.memory.get_swarm_memory_dir", return_value=None),
        ):
            id_a = extract_meta_lesson(
                task_id="edge-a1",
                taxonomy="bugfix",
                error_summary="Patch drifted on concurrent edit",
                recovery_strategy="Re-read file and pass base_sha256",
                workspace_dir=ws,
                files_touched=["botex/tools/patch_engine.py"],
                edge_sub="PC-A",
                workspace_id="ProjX",
                origin="antigravity@pc-a",
            )
            assert id_a and id_a.startswith("lesson_")

            note_a = read_memory(workspace_dir=str(ws), memory_id=id_a, scope="global")
            assert note_a["ok"] is True
            meta = note_a["metadata"]
            assert meta["scope"] == "global"
            assert meta["status"] == "candidate"
            assert meta["project"] == "projx"
            assert meta["source"] == "antigravity@pc-a"
            tags_a = {str(t).lower() for t in meta["tags"]}
            assert {"edge:pc-a", "ws:projx", "path:botex/tools"} <= tags_a
            assert (g_dir / f"{id_a}.md").exists()
            # Local vault holds no remote lesson file.
            assert not (ws / ".botex" / "memory" / f"{id_a}.md").exists()

            # Same failure/recovery on a different edge never collides.
            id_b = extract_meta_lesson(
                task_id="edge-b1",
                taxonomy="bugfix",
                error_summary="Patch drifted on concurrent edit",
                recovery_strategy="Re-read file and pass base_sha256",
                workspace_dir=ws,
                files_touched=["botex/tools/patch_engine.py"],
                edge_sub="PC-B",
                workspace_id="ProjX",
            )
            assert id_b and id_b != id_a

            # Re-extracting on the same edge is deterministic (same id).
            id_a_dup = extract_meta_lesson(
                task_id="edge-a2",
                taxonomy="bugfix",
                error_summary="Patch drifted on concurrent edit",
                recovery_strategy="Re-read file and pass base_sha256",
                workspace_dir=ws,
                files_touched=["botex/tools/patch_engine.py"],
                edge_sub="pc-a",
                workspace_id="projx",
            )
            assert id_a_dup == id_a

            # A local unscoped lesson coexists — it must not rank under an
            # edge-scoped query but stays visible under the legacy query.
            memory.save_memory(
                workspace_dir=str(ws),
                title="Local Lesson",
                content=(
                    "### Lesson Learned (bugfix)\n"
                    "- **Effective Resolution:** Local-only fix\n"
                    "- **Context/Error:** local\n"
                ),
                kind="lesson",
                tags=["meta-harness", "lesson", "bugfix"],
                memory_id="lesson_local_only",
                scope="local",
                status="active",
                confidence=0.9,
            )

            # Promote both to active so the recall gate (>=0.6) can see them.
            assert set_memory_status(str(ws), id_a, "active", scope="global")
            assert set_memory_status(str(ws), id_b, "active", scope="global")
            assert update_lesson_confidence(str(ws), id_a, 0.3, scope="global")["ok"]
            assert update_lesson_confidence(str(ws), id_b, 0.3, scope="global")["ok"]

            # PC-A sees only its own lesson; PC-B's never ranks for it.
            ids_a: list[str] = []
            lessons_a = get_relevant_meta_lessons(
                "bugfix",
                workspace_dir=ws,
                max_lessons=5,
                edge_sub="pc-a",
                workspace_id="projx",
                lesson_ids_out=ids_a,
                files=["botex/tools/retry.py"],
            )
            assert ids_a == [id_a]
            assert lessons_a == ["Re-read file and pass base_sha256"]

            ids_b: list[str] = []
            get_relevant_meta_lessons(
                "bugfix",
                workspace_dir=ws,
                max_lessons=5,
                edge_sub="pc-b",
                workspace_id="projx",
                lesson_ids_out=ids_b,
            )
            assert ids_b == [id_b]

            # Unknown edge or mismatched workspace sees nothing — no bleed.
            assert (
                get_relevant_meta_lessons(
                    "bugfix",
                    workspace_dir=ws,
                    edge_sub="pc-c",
                    workspace_id="projx",
                )
                == []
            )
            assert (
                get_relevant_meta_lessons(
                    "bugfix",
                    workspace_dir=ws,
                    edge_sub="pc-a",
                    workspace_id="other-ws",
                )
                == []
            )

            # Legacy unscoped recall is unchanged: all active lessons rank.
            unscoped = get_relevant_meta_lessons("bugfix", workspace_dir=ws, max_lessons=5)
            assert "Re-read file and pass base_sha256" in unscoped
            assert "Local-only fix" in unscoped

            # No edge context: legacy local-quarantine behavior, no scope tags.
            id_local = extract_meta_lesson(
                task_id="loc1",
                taxonomy="bugfix",
                error_summary="Local failure",
                recovery_strategy="Local fix",
                workspace_dir=ws,
            )
            note_local = read_memory(workspace_dir=str(ws), memory_id=id_local, scope="local")
            assert note_local["ok"] is True
            assert note_local["metadata"]["scope"] == "local"
            local_tags = {str(t).lower() for t in note_local["metadata"]["tags"]}
            assert not any(t.startswith(("edge:", "ws:")) for t in local_tags)
    finally:
        memory.reset_memory_index_cache()


def test_pack_lessons_within_budget_drops_overflow_and_handles_duplicates(tmp_path):
    """Knapsack packing respects 800 B, never emits overflow pointers, and handles duplicates."""
    from botex.core.meta_harness import pack_lessons_within_budget

    assert pack_lessons_within_budget(["a"], max_bytes=True) == []
    assert pack_lessons_within_budget(["a"], max_bytes=0) == []

    dup = "D" * 300
    tail = "T" * 300
    packed = pack_lessons_within_budget([dup, dup, tail], max_bytes=800, base_dir=tmp_path)
    assert len(packed) == 2
    assert all("[context overflow" not in ln for ln in packed)

    # Falls back to linear prefix packing if ContextKnapsack raises.
    with patch(
        "botex.core.context_inclusion.ContextKnapsack.pack",
        side_effect=RuntimeError("knapsack unavailable"),
    ):
        linear = pack_lessons_within_budget(["one", "two", "X" * 900], max_bytes=50)
    assert linear == ["one", "two"]


@pytest.mark.anyio
async def test_engine_confidence_feedback_and_infra_skip(tmp_path, monkeypatch):
    """End-of-attempt confidence rewards DONE, penalizes quality failures, skips ABORTED/infra."""
    import botex.core.engine as engine
    from botex.memory.memory import read_memory, save_memory

    save_memory(
        workspace_dir=str(tmp_path),
        title="Injected Lesson One",
        content="Use helper X.",
        kind="lesson",
        memory_id="mem_fb_1",
        scope="local",
        status="candidate",
        confidence=0.4,
    )

    async def fake_impl_done(*args, **kwargs):
        ct_out = kwargs.get("contrastive_out")
        if isinstance(ct_out, dict):
            ct_out["applied_lesson_ids"] = ["mem_fb_1"]
            ct_out["rollback_count"] = 0
        return {"ok": True, "task_id": "t1", "status": "DONE", "files_touched": []}

    with patch.object(engine, "_run_botex_task_once_impl", side_effect=fake_impl_done):
        await engine._run_botex_task_once("t1", "Fix python bug", str(tmp_path), "m1", mode="edit")
    after_done = read_memory(workspace_dir=str(tmp_path), memory_id="mem_fb_1", scope="local")
    assert after_done["metadata"]["confidence"] == 0.5

    # ABORTED / REQUEST_TIMEOUT / API_ERROR / BUDGET_EXCEEDED even with rb_count > 0 must NOT penalize.
    for status, fkind in (
        ("ABORTED", "aborted"),
        ("REQUEST_TIMEOUT", "request_timeout"),
        ("API_ERROR", "provider_rate_limited"),
        ("BUDGET_EXCEEDED", "budget_or_price_block"),
    ):

        async def fake_non_quality(*args, _st=status, _fk=fkind, **kwargs):
            ct_out = kwargs.get("contrastive_out")
            if isinstance(ct_out, dict):
                ct_out["applied_lesson_ids"] = ["mem_fb_1"]
                ct_out["rollback_count"] = 1
            return {"ok": False, "task_id": "t2", "status": _st, "failure_kind": _fk}

        with patch.object(engine, "_run_botex_task_once_impl", side_effect=fake_non_quality):
            await engine._run_botex_task_once(
                "t2", "Fix python bug", str(tmp_path), "m1", mode="edit"
            )
        check = read_memory(workspace_dir=str(tmp_path), memory_id="mem_fb_1", scope="local")
        assert check["metadata"]["confidence"] == 0.5

    # Genuine quality failure (VERIFICATION_FAILED) penalizes -0.15.
    async def fake_verify_fail(*args, **kwargs):
        ct_out = kwargs.get("contrastive_out")
        if isinstance(ct_out, dict):
            ct_out["applied_lesson_ids"] = ["mem_fb_1"]
            ct_out["rollback_count"] = 1
        return {
            "ok": False,
            "task_id": "t3",
            "status": "VERIFICATION_FAILED",
            "failure_kind": "verification_failed",
        }

    with patch.object(engine, "_run_botex_task_once_impl", side_effect=fake_verify_fail):
        await engine._run_botex_task_once("t3", "Fix python bug", str(tmp_path), "m1", mode="edit")
    after_pen = read_memory(workspace_dir=str(tmp_path), memory_id="mem_fb_1", scope="local")
    assert after_pen["metadata"]["confidence"] == 0.35

    # Infra fallback in run_botex_task must NOT call extract_meta_lesson.
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nv-key")
    calls = 0

    async def fake_once_infra(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": False,
                "task_id": "ti1",
                "status": "API_ERROR",
                "failure_kind": "provider_rate_limited",
                "message": "429",
                "steps": 0,
                "cost_usd": 0.0,
            }
        return {
            "ok": True,
            "task_id": "ti2",
            "status": "DONE",
            "message": "ok",
            "steps": 1,
            "cost_usd": 0.0,
        }

    with (
        patch.object(engine, "resolve_model_candidates", return_value=["m1", "m2"]),
        patch.object(engine, "_run_botex_task_once", side_effect=fake_once_infra),
        patch.object(engine, "check_model_price", return_value=(True, "")),
        patch.object(engine, "check_model_tools", return_value=(True, "")),
        patch.object(engine, "check_budget_limit", return_value=(False, 0.0)),
        patch.object(engine, "extract_meta_lesson") as mock_extract,
    ):
        await engine.run_botex_task(
            task="Task with rate-limit fallback",
            workspace_dir=str(tmp_path),
            provider="nvidia",
            mode="readonly",
        )
    mock_extract.assert_not_called()

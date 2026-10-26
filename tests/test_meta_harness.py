# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Unit tests for BoteX Meta-Harness (Harness² Runtime Supervisor & Experience Store)
and Free-Tier Failover Cascade.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from botex.catalog import FREE_TIER_FAILOVER_CHAIN, get_free_tier_failover_candidates
from botex.meta_harness import (
    InMemoryProposeProbe,
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


def test_in_memory_propose_probe_success():
    orig = "def add(a, b):\n    return a + b\n"
    search = "return a + b"
    replace = "return int(a) + int(b)"

    ok, new_code, err, lvl = InMemoryProposeProbe.probe_patch(
        original_content=orig,
        search_block=search,
        replace_block=replace,
        file_path="math_ops.py",
    )
    assert ok is True
    assert err is None
    assert "return int(a) + int(b)" in new_code
    assert lvl > 0


def test_in_memory_propose_probe_syntax_rejection():
    orig = "def add(a, b):\n    return a + b\n"
    # Introducing invalid syntax
    search = "return a + b"
    replace = "return int(a) + +"

    ok, new_code, err, lvl = InMemoryProposeProbe.probe_patch(
        original_content=orig,
        search_block=search,
        replace_block=replace,
        file_path="math_ops.py",
    )
    assert ok is False
    assert err is not None
    assert "syntax error" in err.lower()
    assert new_code == orig


def test_in_memory_propose_probe_mismatch():
    orig = "def add(a, b):\n    return a + b\n"
    search = "def subtract(a, b):"
    replace = "def sub(a, b):"

    ok, new_code, err, lvl = InMemoryProposeProbe.probe_patch(
        original_content=orig,
        search_block=search,
        replace_block=replace,
        file_path="math_ops.py",
    )
    assert ok is False
    assert err is not None
    assert "could not be located" in err.lower()
    assert new_code == orig


def test_meta_lessons_extract_and_retrieve():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Extract and save a lesson
        lesson_id = extract_meta_lesson(
            task_id="t123",
            taxonomy="bugfix",
            error_summary="IndentationError on line 12",
            recovery_strategy="Normalized tabs to 4 spaces",
            workspace_dir=tmpdir,
            failed_model="nvidia/nemotron",
            successful_model="deepseek/deepseek-r1:free",
        )
        assert lesson_id is not None
        assert lesson_id.startswith("lesson_")

        # Retrieve matching lessons
        lessons = get_relevant_meta_lessons("bugfix", workspace_dir=tmpdir, max_lessons=2)
        assert len(lessons) >= 1
        assert any(
            "IndentationError" in l or "Lesson Learned" in l or "Normalized" in l for l in lessons
        )


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

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for external verify_gate, clean env builder, and Test-Taint Guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from botex.edge.verify_gate import (
    build_clean_verify_env,
    run_external_verify,
    scan_test_taint,
)


def test_build_clean_verify_env_strips_api_keys_and_prioritizes_worktree(tmp_path: Path):
    origin_repo = tmp_path / "origin"
    wt_path = tmp_path / "wt"
    (origin_repo / "mypkg").mkdir(parents=True)
    (wt_path / "mypkg").mkdir(parents=True)
    (origin_repo / "mypkg" / "__init__.py").write_text("SOURCE = 'origin'\n", encoding="utf-8")
    (wt_path / "mypkg" / "__init__.py").write_text("SOURCE = 'worktree'\n", encoding="utf-8")

    # Simulate an editable .pth pointing to origin_repo alongside secrets in parent env.
    fake_base = dict(os.environ)
    fake_base["OPENROUTER_API_KEY"] = "sk-or-v1-secret-value-123456"
    fake_base["NVIDIA_API_KEY"] = "nvapi-secret-value-abcdef"
    fake_base["GEMINI_API_KEY"] = "AIzaSyDummySecretValue999"
    fake_base["PYTHONPATH"] = str(origin_repo)

    clean_env = build_clean_verify_env(origin_repo, wt_path, base_env=fake_base)
    assert "OPENROUTER_API_KEY" not in clean_env
    assert "NVIDIA_API_KEY" not in clean_env
    assert "GEMINI_API_KEY" not in clean_env
    assert clean_env["BOTEX_DISABLE_DOTENV"] == "1"
    if sys.platform == "win32":
        assert "PATHEXT" in clean_env and clean_env["PATHEXT"]

    # Create a site-packages-style .pth path check via site.addsitedir to prove PYTHONPATH
    # resolves wt_path ahead of origin_repo .pth entries.
    site_dir = tmp_path / "fake_site"
    site_dir.mkdir()
    (site_dir / "editable.pth").write_text(str(origin_repo) + "\n", encoding="utf-8")

    # Run from a neutral cwd so sys.path[0] does not trivially resolve mypkg from
    # wt_path; addsitedir() runs first so PYTHONPATH must genuinely beat the .pth entry.
    probe_code = (
        f"import site; site.addsitedir({str(site_dir)!r}); import mypkg; print(mypkg.SOURCE)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(tmp_path),
        env=clean_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "worktree"


def test_test_taint_guard_blocks_protected_paths_and_env_probes():
    for protected in (
        "tests/conftest.py",
        "pkg/sub/conftest.py",
        "botex.config.json",
        "skills/botex/SKILL.md",
        "tests/fixtures/sample.json",
        ".github/workflows/ci.yml",
        "pyproject.toml",
    ):
        res = scan_test_taint("", [protected], allow_test_mutation=False)
        assert res["tainted"] is True
        assert res["verdict"] == "NEEDS_HUMAN"
        assert protected in res["protected_files"]

    # Production diff probing PYTEST_CURRENT_TEST / SWARM_VERIFY_RUN / os.environ CI
    probe_diff = """diff --git a/botex/core/foo.py b/botex/core/foo.py
--- a/botex/core/foo.py
+++ b/botex/core/foo.py
@@ -1,2 +1,5 @@
+import os
+if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"):
+    pass
+if os.getenv("SWARM_VERIFY_RUN"):
+    pass
"""
    probe_res = scan_test_taint(probe_diff, ["botex/core/foo.py"])
    assert probe_res["tainted"] is True
    assert probe_res["verdict"] == "NEEDS_HUMAN"
    found_probes = {item["probe"] for item in probe_res["env_probes"]}
    assert {"PYTEST_CURRENT_TEST", "OS_ENVIRON_CI", "SWARM_VERIFY_RUN"} <= found_probes

    # With allow_test_mutation=True, deleting existing assert or adding skip/xfail is still blocked.
    weakened_diff = """diff --git a/tests/test_sample.py b/tests/test_sample.py
--- a/tests/test_sample.py
+++ b/tests/test_sample.py
@@ -1,4 +1,5 @@
+@pytest.mark.skip(reason="flaky")
 def test_real():
-    assert result == 42
+    pass
"""
    weak_res = scan_test_taint(weakened_diff, ["tests/test_sample.py"], allow_test_mutation=True)
    assert weak_res["tainted"] is True
    assert weak_res["verdict"] == "NEEDS_HUMAN"
    kinds = {w["kind"] for w in weak_res["weakened_tests"]}
    assert {"added_skip_or_xfail", "deleted_assert"} <= kinds

    # New test file added under allow_test_mutation=True without env probes is clean.
    new_test_diff = """diff --git a/tests/test_new_feature.py b/tests/test_new_feature.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new_feature.py
@@ -0,0 +1,3 @@
+def test_ok():
+    assert 1 + 1 == 2
"""
    clean_res = scan_test_taint(
        new_test_diff,
        ["tests/test_new_feature.py"],
        allow_test_mutation=True,
    )
    assert clean_res["tainted"] is False
    assert clean_res["verdict"] == "CLEAN"


def test_supervisor_ledger_does_not_touch_agent_analytics(tmp_path: Path):
    origin_repo = tmp_path / "origin"
    wt_path = tmp_path / "wt"
    origin_repo.mkdir()
    wt_path.mkdir()
    ledger_path = tmp_path / ".ai" / ".swarm" / "supervisor_ledger.jsonl"

    res = run_external_verify(
        origin_repo,
        wt_path,
        [sys.executable, "-c", "print('verify passed token=sk-or-v1-secret123456')"],
        order_id="ord-verify",
        epoch=1,
        diff_text="",
        changed_files=["botex/core/example.py"],
        ledger_path=ledger_path,
    )
    assert res["ok"] is True
    assert res["verdict"] == "DONE"
    assert res["returncode"] == 0
    assert ledger_path.is_file()
    assert not (origin_repo / ".agent_analytics.jsonl").exists()
    assert not (wt_path / ".agent_analytics.jsonl").exists()

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["order_id"] == "ord-verify"
    assert entry["verdict"] == "DONE"
    assert "sk-or-v1-secret123456" not in lines[0]

    # When verify_command is 'none', verdict must be NEEDS_HUMAN (never auto-DONE).
    no_cmd_res = run_external_verify(
        origin_repo,
        wt_path,
        "none",
        order_id="ord-none",
        epoch=1,
        diff_text="",
        changed_files=[],
        ledger_path=ledger_path,
    )
    assert no_cmd_res["ok"] is False
    assert no_cmd_res["verdict"] == "NEEDS_HUMAN"
    assert no_cmd_res["reason"] == "no_verify_command"
    assert no_cmd_res["duration_ms"] == 0


def test_test_taint_guard_fake_headers_inside_hunks_do_not_bypass():
    # Fake '+++ '/`--- /dev/null' lines inside a hunk are content, never file headers:
    # they must not redirect file attribution nor flip is_new_file.
    taint_diff = """diff --git a/tests/test_sample.py b/tests/test_sample.py
--- a/tests/test_sample.py
+++ b/tests/test_sample.py
@@ -1,5 +1,8 @@
 def test_real():
-    assert result == 42
+++ b/botex/not_a_test.py
--- /dev/null
+@pytest.mark.skip(reason="flaky")
-    assert other == 1
"""
    res = scan_test_taint(taint_diff, ["tests/test_sample.py"], allow_test_mutation=True)
    assert res["tainted"] is True
    kinds = {w["kind"] for w in res["weakened_tests"]}
    assert {"added_skip_or_xfail", "deleted_assert"} <= kinds
    assert all(w["file"] == "tests/test_sample.py" for w in res["weakened_tests"])

    # Fake '+++ b/<testfile>' inside a hunk must not exempt env probes in a non-test file.
    probe_diff = """diff --git a/botex/worker.py b/botex/worker.py
--- a/botex/worker.py
+++ b/botex/worker.py
@@ -1,2 +1,4 @@
 import os
+++ b/tests/test_fake.py
+if os.environ.get("PYTEST_CURRENT_TEST"):
+    pass
"""
    probe_res = scan_test_taint(probe_diff, ["botex/worker.py"])
    assert probe_res["tainted"] is True
    assert any(
        p["probe"] == "PYTEST_CURRENT_TEST" and p["file"] == "botex/worker.py"
        for p in probe_res["env_probes"]
    )

    # Fake '+++ b/<protected>' inside a hunk must not enter the touched-path set.
    benign_diff = """diff --git a/botex/worker.py b/botex/worker.py
--- a/botex/worker.py
+++ b/botex/worker.py
@@ -1,2 +1,3 @@
 import os
+++ b/botex.config.json
+VALUE = 1
"""
    benign_res = scan_test_taint(benign_diff, ["botex/worker.py"])
    assert benign_res["tainted"] is False
    assert benign_res["protected_files"] == []


def test_test_taint_guard_env_probe_exemption_only_in_protected_test_locations():
    # A test_*.py name outside protected test dirs is still production code: probes flag.
    helper_diff = """diff --git a/src/test_helpers.py b/src/test_helpers.py
--- a/src/test_helpers.py
+++ b/src/test_helpers.py
@@ -1,2 +1,4 @@
 import os
+if os.environ.get("PYTEST_CURRENT_TEST"):
+    pass
"""
    res = scan_test_taint(helper_diff, ["src/test_helpers.py"])
    assert res["tainted"] is True
    assert any(p["probe"] == "PYTEST_CURRENT_TEST" for p in res["env_probes"])

    # SWARM_VERIFY_RUN has zero legitimate use even inside protected test files.
    swarm_diff = """diff --git a/tests/test_sample.py b/tests/test_sample.py
--- a/tests/test_sample.py
+++ b/tests/test_sample.py
@@ -1,2 +1,4 @@
 import os
+if os.getenv("SWARM_VERIFY_RUN"):
+    pass
"""
    swarm_res = scan_test_taint(swarm_diff, ["tests/test_sample.py"], allow_test_mutation=True)
    assert swarm_res["tainted"] is True
    assert any(p["probe"] == "SWARM_VERIFY_RUN" for p in swarm_res["env_probes"])

    # PYTEST_CURRENT_TEST stays exempt inside a real protected test location.
    exempt_diff = """diff --git a/tests/test_sample.py b/tests/test_sample.py
--- a/tests/test_sample.py
+++ b/tests/test_sample.py
@@ -1,2 +1,4 @@
 import os
+if os.environ.get("PYTEST_CURRENT_TEST"):
+    pass
"""
    exempt_res = scan_test_taint(exempt_diff, ["tests/test_sample.py"], allow_test_mutation=True)
    assert exempt_res["tainted"] is False
    assert exempt_res["verdict"] == "CLEAN"


def test_run_external_verify_spawn_error_returns_failed_and_ledgers(tmp_path: Path):
    origin_repo = tmp_path / "origin"
    wt_path = tmp_path / "wt"
    origin_repo.mkdir()
    wt_path.mkdir()
    ledger_path = tmp_path / ".ai" / ".swarm" / "supervisor_ledger.jsonl"

    res = run_external_verify(
        origin_repo,
        wt_path,
        ["definitely-not-a-real-executable-xyz123"],
        order_id="ord-spawn",
        epoch=2,
        diff_text="",
        changed_files=["botex/core/example.py"],
        ledger_path=ledger_path,
    )
    assert res["ok"] is False
    assert res["verdict"] == "FAILED"
    assert res["reason"] == "spawn_error"
    assert res["returncode"] is None
    assert "duration_ms" in res

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["order_id"] == "ord-spawn"
    assert entry["verdict"] == "FAILED"
    assert entry["reason"] == "spawn_error"

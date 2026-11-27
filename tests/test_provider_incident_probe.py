# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Offline tests for the Plan 060 Step 1/3 probe + triage scripts.

All transport, provider adapters and ``run_botex_task`` are mocked; a
contract-conforming stub of ``botex.providers.incident_probe`` is injected so
these tests validate Worker 060-B's deliverables even before Worker 060-A's
module lands. Canary secrets and fake task text prove nothing leaks.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

CANARY_KEY = "sk-CANARY-abc123def456ghi789"
CANARY_ACCOUNT = "CANARY-account-id-001"
CANARY_TASK_TEXT = "TOPSECRET-task-body-do-not-leak"

# ---------------------------------------------------------------------------
# Contract-conforming stub of botex.providers.incident_probe (Worker 060-A API)
# ---------------------------------------------------------------------------

VALID_OPERATIONS = {
    "catalog_models_openai": "catalog_models_openai",
    "catalog_models_native": "catalog_models_native",
    "chat_completion": "chat_completion",
    "catalog_skipped_unconfigured": "catalog_skipped_unconfigured",
    "catalog_discovery_unsupported": "catalog_discovery_unsupported",
}


def _scrub(text: str) -> str:
    out = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    out = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<redacted-key>", out)
    out = re.sub(r"[A-Za-z0-9_-]*CANARY[A-Za-z0-9_-]*", "<redacted>", out)
    return out[:256]


def normalize_operation(raw: str) -> str:
    return VALID_OPERATIONS.get(str(raw).strip().lower(), "chat_completion")


def redact_url_template(url: str) -> tuple[str, list[str]]:
    params: list[str] = []
    if "?" in url:
        url, _, qs = url.partition("?")
        params = sorted(p.split("=")[0] for p in qs.split("&") if p)
    path = "/".join(
        "<param>" if _scrub(seg) != seg or len(seg) > 24 else seg for seg in url.split("/")
    )
    return (_scrub(path), params)


def redact_headers(headers) -> tuple[list[str], bool]:
    if not headers:
        return [], False
    names = sorted(str(k).lower() for k in headers)
    return names, any("auth" in n for n in names)


def summarize_payload(payload) -> dict:
    if not isinstance(payload, dict):
        return {"stream": False, "tool_count": 0, "payload_keys": []}
    tools = payload.get("tools")
    return {
        "stream": bool(payload.get("stream")),
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "payload_keys": sorted(payload.keys()),
    }


def summarize_response(status, headers, body, *, max_error_chars: int = 256) -> dict:
    out: dict = {"status": status, "error_message": None}
    if isinstance(body, dict):
        out["top_level_keys"] = sorted(body.keys())
        for k in ("error_code", "error_type"):
            out[k] = _scrub(str(body.get(k)))[:max_error_chars] if body.get(k) else None
        msg = body.get("error_message") or body.get("message")
        out["error_message"] = _scrub(str(msg))[:max_error_chars] if msg else None
    elif isinstance(body, BaseException):
        out["error_message"] = _scrub(str(body))[:max_error_chars]
    return out


def build_incident_record(
    *,
    provider,
    operation,
    model=None,
    method="GET",
    url="",
    headers=None,
    payload=None,
    response_status=None,
    response_headers=None,
    response_body=None,
    duration_ms=0,
    exception=None,
    ts=None,
):
    url_template, query_keys = redact_url_template(str(url))
    header_names, auth_present = redact_headers(headers)
    resp = summarize_response(response_status, response_headers, response_body)
    if exception is not None:
        resp["error_message"] = _scrub(str(exception))[:256]
    return {
        "schema_version": 1,
        "ts": ts or "2026-11-27T00:00:00Z",
        "provider": str(provider),
        "operation": normalize_operation(operation),
        "model": model,
        "request": {
            "method": str(method),
            "url_template": url_template,
            "query_keys": query_keys,
            "header_names": header_names,
            "auth_present": auth_present,
            **summarize_payload(payload),
        },
        "response": resp,
        "duration_ms": int(duration_ms),
        "exception_type": type(exception).__name__ if exception else None,
    }


def append_incident_record(record, path=None):
    import datetime

    p = Path(path or ".botex/provider_incidents.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p


def emit_incident_safe(sink, record):
    if sink is not None:
        sink(record)


@pytest.fixture()
def incident_probe_stub(monkeypatch):
    mod = types.ModuleType("botex.providers.incident_probe")
    for name, obj in list(globals().items()):
        if name in (
            "VALID_OPERATIONS",
            "normalize_operation",
            "redact_url_template",
            "redact_headers",
            "summarize_payload",
            "summarize_response",
            "build_incident_record",
            "append_incident_record",
            "emit_incident_safe",
        ):
            setattr(mod, name, obj)
    # Stub the parent packages too so the probe script never imports the real
    # (heavy) botex.providers chain during offline tests.
    botex_pkg = types.ModuleType("botex")
    providers_pkg = types.ModuleType("botex.providers")
    botex_pkg.__path__ = []
    providers_pkg.__path__ = []
    providers_pkg.incident_probe = mod
    monkeypatch.setitem(sys.modules, "botex", botex_pkg)
    monkeypatch.setitem(sys.modules, "botex.providers", providers_pkg)
    monkeypatch.setitem(sys.modules, "botex.providers.incident_probe", mod)
    return mod


@pytest.fixture()
def probe():
    return importlib.import_module("provider_incident_probe")


@pytest.fixture()
def triage():
    return importlib.import_module("provider_ledger_triage")


# ---------------------------------------------------------------------------
# probe CLI: --no-write and --output
# ---------------------------------------------------------------------------


def _canned_record(runner, canary_in_url=False):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CANARY_ACCOUNT}/ai/v1/models"
    if not canary_in_url:
        url = "https://api.groq.com/openai/v1/models"
    runner.emit(
        {
            "provider": "cloudflare",
            "operation": "catalog_models_openai",
            "method": "GET",
            "url": url,
            "headers": {
                "accept": "application/json",
                "Authorization": f"Bearer {CANARY_KEY}",
            },
            "response_status": 405,
            "response_body": {"errors": [{"message": f"method not allowed {CANARY_KEY}"}]},
            "duration_ms": 42,
            "exception": None,
        }
    )


def test_no_write_emits_schema_v1_without_canary(probe, incident_probe_stub, monkeypatch, capsys):
    monkeypatch.setattr(
        probe, "run_discovery", lambda p, r, t: _canned_record(r, canary_in_url=True)
    )
    monkeypatch.setattr(probe, "run_chat_matrix", lambda *a, **k: None)
    rc = probe.main(["--providers", "cloudflare", "--no-write"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines
    for line in lines:
        rec = json.loads(line)
        assert rec["schema_version"] == 1
        blob = json.dumps(rec)
        assert CANARY_KEY not in blob
        assert CANARY_ACCOUNT not in blob
        assert rec["request"]["auth_present"] is True
        assert "authorization" in rec["request"]["header_names"]
        assert "<param>" in rec["request"]["url_template"]


def test_output_writes_jsonl_file(probe, incident_probe_stub, monkeypatch, tmp_path):
    out = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(
        probe, "run_discovery", lambda p, r, t: _canned_record(r, canary_in_url=True)
    )
    rc = probe.main(["--providers", "cloudflare", "--output", str(out)])
    assert rc == 0
    records = [json.loads(x) for x in out.read_text(encoding="utf-8").strip().splitlines()]
    assert records
    for rec in records:
        assert rec["schema_version"] == 1
    blob = out.read_text(encoding="utf-8")
    assert CANARY_KEY not in blob
    assert CANARY_ACCOUNT not in blob


# ---------------------------------------------------------------------------
# Track C: structural metrics only, temp-dir isolation
# ---------------------------------------------------------------------------


def test_track_c_structural_metrics_drop_task_text(probe):
    result = {
        "status": "FAILED",
        "failure_kind": "contract_failed",
        "steps": 4,
        "tool_calls": 6,
        "mutations": 2,
        "syntax_rejections": 1,
        "rollback_count": 1,
        "rollback_reasons": ["no_progress"],
        "duration_ms": 900,
        # adversarial text fields that must never survive projection:
        "summary": CANARY_TASK_TEXT,
        "task": CANARY_TASK_TEXT,
        "patch": CANARY_TASK_TEXT,
    }
    metrics = probe.track_c_structural_metrics(
        "openrouter-free", "m:free", "trivial_one_line_edit", 1, result
    )
    assert set(metrics) <= set(probe._TRACK_C_STRUCTURAL_KEYS)
    blob = json.dumps(metrics)
    assert CANARY_TASK_TEXT not in blob
    assert metrics["status"] == "FAILED"
    assert metrics["rollback_count"] == 1


def test_track_c_fixtures_are_confined_to_tmp_dir(probe, tmp_path):
    prompts = probe._track_c_fixtures(tmp_path)
    assert set(prompts) == set(probe.TRACK_C_CELLS)
    assert (tmp_path / "calc.py").exists()
    assert all(CANARY_TASK_TEXT not in p for p in prompts.values())
    repo_root = tmp_path.parent
    assert not (repo_root / "calc.py").exists()


def test_run_track_c_uses_temp_dir_and_reports_structure(
    probe, incident_probe_stub, monkeypatch, tmp_path, capsys
):
    seen = {}

    def fake_run_botex_task(task, **kwargs):
        seen["workspace"] = kwargs.get("workspace_dir")
        assert CANARY_TASK_TEXT not in task
        return {
            "status": "DONE",
            "steps": 2,
            "tool_calls": 1,
            "mutations": 1,
            "syntax_rejections": 0,
            "rollback_count": 0,
            "summary": CANARY_TASK_TEXT,  # must be dropped
        }

    import types as _t

    engine_stub = _t.ModuleType("botex.core.engine")
    engine_stub.run_botex_task = fake_run_botex_task
    botex_pkg = _t.ModuleType("botex")
    core_pkg = _t.ModuleType("botex.core")
    botex_pkg.__path__ = []
    core_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "botex", botex_pkg)
    monkeypatch.setitem(sys.modules, "botex.core", core_pkg)
    monkeypatch.setitem(sys.modules, "botex.core.engine", engine_stub)
    monkeypatch.setattr(
        probe,
        "_load_config",
        lambda: {"providers": {"openrouter-free": {"models": {"fast": "x-lightning:free"}}}},
    )

    summaries = probe.run_track_c(
        probe.Runner(output=None, no_write=True), repetitions=2, fallback_model=None
    )
    assert seen["workspace"] and str(seen["workspace"]) not in ("", ".")
    assert len(summaries) == 2 * len(probe.TRACK_C_CELLS)  # 1 candidate x 4 cells x 2 reps
    for s in summaries:
        assert set(s) <= set(probe._TRACK_C_STRUCTURAL_KEYS)
        assert CANARY_TASK_TEXT not in json.dumps(s)


# ---------------------------------------------------------------------------
# discovery fallback when incident_sink kwarg is not yet supported
# ---------------------------------------------------------------------------


class _FakeProvider:
    base_url = "https://fake.example/v1"

    def fetch_catalog(self, api_key=None, timeout=10.0):
        return [{"id": "m1"}]


def test_discovery_without_incident_sink_kwarg_emits_record(
    probe, incident_probe_stub, monkeypatch
):
    monkeypatch.setattr(probe, "_load_config", lambda: {"providers": {"fake": {}}})
    monkeypatch.setattr(probe, "_get_provider", lambda n, c: (_FakeProvider(), c))
    runner = probe.Runner(output=None, no_write=True)
    probe.run_discovery("fake", runner, timeout_s=1.0)
    assert len(runner.records) == 1
    rec = runner.records[0]
    assert rec["operation"] == "catalog_models_openai"
    assert rec["response"]["status"] == 200
    assert rec["response"]["error_message"] is None


def test_discovery_failure_captures_masked_exception(probe, incident_probe_stub, monkeypatch):
    class _Failing:
        base_url = f"https://fake.example/accounts/{CANARY_ACCOUNT}/v1"

        def fetch_catalog(self, api_key=None, timeout=10.0):
            raise RuntimeError(f"403 with key {CANARY_KEY}")

    monkeypatch.setattr(probe, "_load_config", lambda: {"providers": {"fake": {}}})
    monkeypatch.setattr(probe, "_get_provider", lambda n, c: (_Failing(), c))
    runner = probe.Runner(output=None, no_write=True)
    probe.run_discovery("fake", runner, timeout_s=1.0)
    rec = runner.records[0]
    blob = json.dumps(rec)
    assert CANARY_KEY not in blob
    assert CANARY_ACCOUNT not in blob
    assert rec["exception_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# ledger triage: counts only, no task text or secrets
# ---------------------------------------------------------------------------


def _write_ledger(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_triage_emits_counts_without_leaking_text(triage, tmp_path, capsys):
    ledger = tmp_path / "ledger.jsonl"
    good = {
        "task_id": "t1",
        "provider": "groq",
        "model": "m1",
        "status": "FAILED",
        "failure_kind": "provider_error",
        "duration_s": 2.0,
        "steps": 5,
        "syntax_rejections": 2,
        "rollback_count": 1,
        "taxonomy": "edit",
        "mode": "edit",
        "summary": f"boom HTTP 403 key={CANARY_KEY} {CANARY_TASK_TEXT}",
    }
    done = {
        "task_id": "t2",
        "provider": "groq",
        "model": "m1",
        "status": "DONE",
        "duration_s": 4.0,
        "steps": 7,
        "syntax_rejections": 0,
        "rollback_count": 0,
        "summary": CANARY_TASK_TEXT,
    }
    _write_ledger(ledger, [json.dumps(good), json.dumps(done), "{not json at all", ""])
    rc = triage.main(["--ledger", str(ledger)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"] == 2  # malformed + empty lines tolerated
    counts = out["counts_by_provider_model_status_failure_kind"]
    assert list(counts.values()) == [1, 1]
    assert out["syntax_rejections_total"] == 2
    assert out["rollback_count_total"] == 1
    assert out["taxonomy_distribution"] == {"edit": 1}  # only the FAILED record has taxonomy
    blob = json.dumps(out)
    assert CANARY_KEY not in blob
    assert CANARY_TASK_TEXT not in blob
    med = out["medians_by_provider_model_status"]["groq|m1|FAILED"]
    assert med["median_duration_s"] == 2.0
    assert med["median_steps"] == 5
    assert out["recurring_error_categories"], "expected categorical error tokens"


def test_triage_tasks_dir_counts_only(triage, tmp_path, capsys):
    tasks = tmp_path / "tasks"
    (tasks / "t1").mkdir(parents=True)
    (tasks / "t1" / "result.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "result": {
                    "status": "INCOMPLETE",
                    "failure_kind": "no_progress",
                    "rollback_count": 2,
                    "syntax_rejections": 1,
                    "rollback_reasons": [f"patch rejected {CANARY_KEY}", "no_progress"],
                    "summary": CANARY_TASK_TEXT,
                },
            }
        ),
        encoding="utf-8",
    )
    (tasks / "t2").mkdir()
    (tasks / "t2" / "result.json").write_text("{broken", encoding="utf-8")
    rc = triage.main(["--ledger", str(tmp_path / "missing.jsonl"), "--tasks-dir", str(tasks)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    td = out["tasks_dir"]
    assert td["task_files_scanned"] == 2
    assert td["status_counts"] == {"INCOMPLETE": 1}
    assert td["rollback_count_total"] == 2
    assert td["syntax_rejections_total"] == 1
    blob = json.dumps(out)
    assert CANARY_KEY not in blob
    assert CANARY_TASK_TEXT not in blob

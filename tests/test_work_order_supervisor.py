# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for WorkOrder state machine, handoff_policy, CostLedger, supervisor."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from botex.core.work_orders import (
    ANTIGRAVITY_EXCLUDED_HARNESSES,
    ANTIGRAVITY_EXCLUSION_REASON,
    CostLedger,
    HandoffPolicy,
    VerifySpec,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderSupervisor,
    can_transition,
    compute_failure_signature,
)


def _policy(**kwargs) -> HandoffPolicy:
    base = {
        "allowed_harnesses": ("botex", "zcode", "devin"),
        "signed_by": "operator",
        "max_hops": 3,
    }
    base.update(kwargs)
    return HandoffPolicy(**base)


def _order(**kwargs) -> WorkOrder:
    base = {"order_id": "ord-1", "goal": "do the thing", "origin_repo": "repo"}
    base.update(kwargs)
    return WorkOrder(**base)


def _sup(**kwargs) -> WorkOrderSupervisor:
    base = {"audit": False, "ollama": False}
    base.update(kwargs)
    return WorkOrderSupervisor(**base)


def _mapped_mode(res: dict) -> str:
    """Probe the known locations where adapters may surface the mapped mode."""
    for key in ("mode", "mode_request", "capability", "capability_mode"):
        if isinstance(res.get(key), str) and res[key]:
            return res[key]
    for nest in ("translated", "params", "payload", "request", "work_order"):
        sub = res.get(nest)
        if isinstance(sub, dict):
            for key in ("mode", "mode_request", "capability", "capability_mode"):
                if isinstance(sub.get(key), str) and sub[key]:
                    return sub[key]
    return ""


def test_missing_handoff_policy_enforces_fail_stop():
    sup = _sup()
    order = _order(status="RUNNING")
    res = sup.handle_failure(order, "botex", failure_kind="rate_limit", error_text="HTTP 429 hit")
    assert res["ok"] is False
    assert res["reason"] == "missing_handoff_policy"
    assert "target_harness" not in res
    assert order.status == WorkOrderStatus.BLOCKED
    assert order.blocking_reason == "missing_handoff_policy"
    diag = res["diagnostics"]
    assert diag["signature"] == compute_failure_signature("rate_limit", "HTTP 429 hit")
    assert diag["failure_class"] == "rate_limit"
    assert diag["infra"] is True
    assert diag["attempted_harnesses"] == ["botex"]
    assert diag["policy_present"] is False

    # Unsigned policy (no signed_by) fails closed the same way.
    unsigned = _order(
        status="RUNNING",
        handoff_policy=HandoffPolicy(allowed_harnesses=("zcode",), signed_by=""),
    )
    res2 = sup.handle_failure(unsigned, "botex", failure_kind="timeout", error_text="t/o")
    assert res2["reason"] == "missing_handoff_policy"
    assert unsigned.status == WorkOrderStatus.BLOCKED

    # Signature present but empty allowed_harnesses -> still unauthorized.
    empty_targets = _order(
        status="RUNNING",
        handoff_policy=HandoffPolicy(allowed_harnesses=(), signed_by="operator"),
    )
    res3 = sup.handle_failure(empty_targets, "botex", failure_kind="timeout", error_text="t/o")
    assert res3["reason"] == "missing_handoff_policy"
    assert empty_targets.status == WorkOrderStatus.BLOCKED


def test_mode_mapping_never_widens_restrictiveness():
    adapters = pytest.importorskip("botex.edge.adapters")
    translate = adapters.translate_work_order
    rank = adapters.RESTRICTIVENESS_RANK

    expected = {"botex": "readonly", "zcode": "plan", "devin": "readonly"}
    for target, want in expected.items():
        res = translate(_order(mode_request="readonly"), target)
        assert res["ok"] is True, res
        mapped = _mapped_mode(res)
        assert mapped, f"no mapped mode in result for {target}: {res}"
        assert rank[mapped] <= rank["readonly"]
        assert mapped == want

    # Unmapped modes block instead of silently widening (fs_write has no
    # safe file-write-only equivalent anywhere; unknown modes fail closed).
    for mode in ("fs_write", "bogus_mode"):
        for target in ("botex", "zcode", "devin"):
            res = translate(_order(mode_request=mode), target)
            assert res["ok"] is False, (mode, target, res)
            assert res["status"] == "BLOCKED"
            assert res["reason"] == "unmapped_or_widened_mode"

    # Exhaustive sweep: no (mode, harness) pair may return a wider capability.
    for mode in rank:
        for target in ("botex", "zcode", "devin"):
            res = translate(_order(mode_request=mode), target)
            if res["ok"]:
                mapped = _mapped_mode(res)
                assert mapped in rank, f"{target} returned unknown mode {mapped!r}"
                assert rank[mapped] <= rank[mode], (mode, target, mapped)
            else:
                assert res["status"] == "BLOCKED"
                assert res["reason"]

    for excluded in ("antigravity_ls", "antigravity_ls_mcp", "antigravity"):
        res = translate(_order(mode_request="edit"), excluded)
        assert res["ok"] is False
        assert res["status"] == "BLOCKED"
        assert res["reason"] == ANTIGRAVITY_EXCLUSION_REASON

    # Optional checkpoint-manager + terminal-hook contract (Worker B2 surface,
    # guarded so this file also runs before adapters.py lands).
    ecm_cls = getattr(adapters, "EpochCheckpointManager", None)
    if ecm_cls is not None:
        mgr = ecm_cls(order_id="ord-1")
        stale = mgr.checkpoint_and_push(epoch=3, fetch_remote_epoch_fn=lambda **kw: 5)
        assert stale["ok"] is False
        assert stale["status"] == "stale_epoch"
        pushed = mgr.checkpoint_and_push(
            epoch=3,
            fetch_remote_epoch_fn=lambda **kw: 3,
            push_fn=lambda **kw: {"ok": True},
        )
        assert pushed["ok"] is True
        assert pushed["status"] == "pushed"
        assert pushed["pushed_ref"].endswith("swarm/wip/ord-1/e3")

    fin = getattr(adapters, "finalize_verified_work_order", None) or (
        getattr(ecm_cls, "finalize_verified_work_order", None) if ecm_cls else None
    )
    if fin is not None:
        none_order = _order(status="VERIFYING", verify=VerifySpec(kind="none"))
        res_none = fin(none_order, {"ok": True})
        assert res_none["status"] == WorkOrderStatus.NEEDS_HUMAN
        assert none_order.status == WorkOrderStatus.NEEDS_HUMAN
        cmd_order = _order(status="VERIFYING", verify=VerifySpec(kind="command", command="pytest"))
        res_fail = fin(cmd_order, {"ok": False, "status": "FAILED"})
        assert res_fail["ok"] is False
        assert cmd_order.status != WorkOrderStatus.DONE
        cmd_ok = _order(status="VERIFYING", verify=VerifySpec(kind="command", command="pytest"))
        res_done = fin(cmd_ok, {"ok": True, "status": "DONE"})
        assert res_done["ok"] is True
        assert cmd_ok.status == WorkOrderStatus.DONE


def test_paid_openrouter_forbidden_and_unit_ledger_isolated():
    ledger = CostLedger()

    res = ledger.record_spend("botex", usd=0.5, provider="openrouter", model="openai/gpt-5")
    assert res["ok"] is False
    assert res["status"] == "FORBIDDEN"
    assert res["reason"] == "paid_openrouter_forbidden"
    assert ledger.usd == 0.0

    # allow_paid alone is not enough - the model must sit in paid_allowlist.
    res2 = ledger.record_spend(
        "botex",
        usd=0.5,
        provider="openrouter",
        model="openai/gpt-5",
        allow_paid=True,
        paid_allowlist=("anthropic/claude",),
    )
    assert res2["reason"] == "paid_openrouter_forbidden"
    assert ledger.usd == 0.0

    res3 = ledger.record_spend(
        "botex",
        usd=0.5,
        provider="openrouter",
        model="openai/gpt-5",
        allow_paid=True,
        paid_allowlist=("openai/gpt-5",),
    )
    assert res3["ok"] is True
    assert ledger.usd == pytest.approx(0.5)

    # openrouter-free and :free models ride the free tier - always allowed.
    assert ledger.check_provider_allowed("openrouter-free", "x/y:free")["ok"] is True
    assert ledger.check_provider_allowed("openrouter", "x/y:free")["ok"] is True
    assert ledger.check_provider_allowed("nvidia", "nemotron")["ok"] is True

    # Strict unit separation: usd/acu/quota_units/turns never cross-accumulate.
    ledger2 = CostLedger()
    ledger2.record_spend("botex", usd=0.25, provider="openrouter-free", model="m:free")
    ledger2.record_spend("devin", acu=2.0, turns=3)
    ledger2.record_spend("zcode", quota_units=5.0, turns=1)
    assert ledger2.usd == pytest.approx(0.25)
    assert ledger2.acu == pytest.approx(2.0)
    assert ledger2.quota_units == pytest.approx(5.0)
    assert ledger2.turns == 4

    # Supervisor enforces the signed policy - callers cannot widen paid consent.
    sup = _sup()
    order = _order(handoff_policy=_policy(allow_paid=False))
    res4 = sup.record_spend(
        order,
        "botex",
        usd=1.0,
        provider="openrouter",
        model="openai/gpt-5",
        allow_paid=True,
    )
    assert res4["reason"] == "paid_openrouter_forbidden"
    assert order.budget.usd == 0.0

    order2 = _order(handoff_policy=_policy(allow_paid=True, paid_allowlist=("openai/gpt-5",)))
    res5 = sup.record_spend(order2, "botex", usd=1.0, provider="openrouter", model="openai/gpt-5")
    assert res5["ok"] is True
    assert order2.budget.usd == pytest.approx(1.0)


def test_verify_none_never_auto_done():
    sup = _sup()

    order = _order(status="RUNNING", verify=VerifySpec(kind="none"))
    out = sup.begin_verify(order)
    assert out["status"] == WorkOrderStatus.NEEDS_HUMAN
    assert order.status == WorkOrderStatus.NEEDS_HUMAN

    # Even a fabricated {"ok": True} cannot close a verify:none order.
    order2 = _order(status="VERIFYING", verify=VerifySpec(kind="none"))
    out2 = sup.apply_verify_result(order2, {"ok": True, "status": "DONE"})
    assert out2["status"] == WorkOrderStatus.NEEDS_HUMAN
    assert order2.status == WorkOrderStatus.NEEDS_HUMAN

    # DONE is unreachable through the raw transition path.
    order3 = _order(status="VERIFYING", verify=VerifySpec(kind="command", command="pytest"))
    refused = sup.transition(order3, "DONE")
    assert refused["ok"] is False
    assert refused["reason"] == "done_requires_verify_gate"
    assert order3.status == WorkOrderStatus.VERIFYING

    # Only an external verify_gate result with ok=True closes the order.
    out4 = sup.apply_verify_result(order3, {"ok": True, "status": "DONE"})
    assert out4["status"] == WorkOrderStatus.DONE
    assert order3.status == WorkOrderStatus.DONE

    order4 = _order(status="VERIFYING", verify=VerifySpec(kind="command", command="pytest"))
    out5 = sup.apply_verify_result(
        order4, {"ok": False, "status": "FAILED", "reason": "verify_command_failed"}
    )
    assert out5["status"] == WorkOrderStatus.FAILED

    order5 = _order(status="VERIFYING", verify=VerifySpec(kind="command", command="pytest"))
    out6 = sup.apply_verify_result(order5, {"ok": False, "status": "NEEDS_HUMAN"})
    assert out6["status"] == WorkOrderStatus.NEEDS_HUMAN

    # Results are only honored while VERIFYING.
    order6 = _order(status="RUNNING", verify=VerifySpec(kind="command", command="pytest"))
    out7 = sup.apply_verify_result(order6, {"ok": True})
    assert out7["ok"] is False
    assert out7["reason"] == "not_in_verifying"


def test_offline_ollama_falls_back_to_deterministic_rules():
    sup = _sup(ollama={"enabled": True, "base_url": "http://127.0.0.1:9", "timeout_s": 0.05})
    triage = sup._triage("spiral", "agent looped on the same patch 5 times")
    assert triage["failure_class"] == "spiral"
    assert triage["ollama"] == "offline"
    assert triage["confidence"] == "low"

    # Ollama is consulted ONLY for spiral/turns_exhausted classes.
    triage_rl = sup._triage("rate_limit", "HTTP 429")
    assert triage_rl["ollama"] == "skipped"
    assert triage_rl["confidence"] == "high"

    # Rules still classify spiral -> handoff proceeds even with Ollama down.
    order = _order(status="RUNNING", handoff_policy=_policy())
    res = sup.handle_failure(
        order, "botex", failure_kind="spiral_warning", error_text="looped 5x on a.py"
    )
    assert res["ok"] is True
    assert res["status"] == WorkOrderStatus.HANDOFF
    assert res["confidence"] == "low"
    assert res["triage"]["failure_class"] == "spiral"

    # Failures the deterministic rules cannot classify still fail closed.
    opaque = _order(status="RUNNING", handoff_policy=_policy())
    res2 = sup.handle_failure(
        opaque, "botex", failure_kind="", error_text="totally opaque gibberish"
    )
    assert res2["ok"] is False
    assert res2["reason"] == "unclassified_failure"
    assert opaque.status == WorkOrderStatus.BLOCKED

    # Disabled Ollama reports "disabled", successful summarize reports "ok".
    sup_off = _sup(ollama=False)
    assert sup_off._triage("turns_exhausted", "hit 45 turns")["ollama"] == "disabled"

    sup_mock = _sup(ollama={"enabled": True, "base_url": "http://127.0.0.1:9"})
    sup_mock._ollama_summarize = lambda text: "blind alley: flaky mock patch loop"
    triage_ok = sup_mock._triage("turns_exhausted", "hit 45 turns")
    assert triage_ok["ollama"] == "ok"
    assert triage_ok["summary"]
    assert triage_ok["confidence"] == "high"


def test_signature_dedup_blocks_third_attempt_and_antigravity_excluded():
    sup = _sup()
    policy = _policy(allowed_harnesses=("botex", "zcode", "devin", "antigravity_ls"), max_hops=3)
    order = _order(status="RUNNING", handoff_policy=policy)

    res1 = sup.handle_failure(
        order,
        "botex",
        failure_kind="rate_limit",
        error_text="HTTP 429 rate limit at 2026-11-19T10:00:00Z",
    )
    assert res1["status"] == WorkOrderStatus.HANDOFF
    assert res1["target_harness"] in ("zcode", "devin")
    assert "antigravity_ls" not in res1["ranked"]
    excluded = {e["harness"]: e["reason"] for e in res1["excluded"]}
    assert excluded["antigravity_ls"] == ANTIGRAVITY_EXCLUSION_REASON
    assert excluded["botex"] == "already_attempted"
    assert order.epoch == 1

    target = res1["target_harness"]
    sup.begin_attempt(order, target)
    # Same normalized failure on a second harness (only the timestamp differs)
    # must block the third attempt outright.
    res2 = sup.handle_failure(
        order,
        target,
        failure_kind="RATE-LIMIT",
        error_text="http 429 rate limit at 2026-11-19T23:59:59Z",
    )
    assert res2["ok"] is False
    assert res2["reason"] == "duplicate_failure_signature"
    assert order.status == WorkOrderStatus.BLOCKED
    assert order.blocking_reason == "duplicate_failure_signature"
    assert "target_harness" not in res2

    # Signature normalization: volatile fields scrubbed, distinct classes kept apart.
    sig_a = compute_failure_signature("rate_limit", "HTTP 429 at 10:00:00 /tmp/x/y")
    sig_b = compute_failure_signature("RATE-LIMIT", "http 429 at 23:59:59 C:\\w\\z")
    sig_c = compute_failure_signature("timeout", "request timed out after 30s")
    assert sig_a == sig_b
    assert sig_a != sig_c
    assert len(sig_a) == 64

    # A policy containing only excluded harnesses can never dispatch.
    sup2 = _sup()
    order2 = _order(
        status="RUNNING",
        handoff_policy=_policy(allowed_harnesses=("antigravity_ls", "antigravity_ls_mcp")),
    )
    res3 = sup2.handle_failure(
        order2, "antigravity_ls", failure_kind="rate_limit", error_text="HTTP 429"
    )
    assert res3["ok"] is False
    assert res3["reason"] == "no_eligible_harness"
    assert {e["harness"] for e in res3["excluded"]} == {
        "antigravity_ls",
        "antigravity_ls_mcp",
    }
    for h in ("antigravity_ls", "antigravity_ls_mcp", "antigravity"):
        assert h in ANTIGRAVITY_EXCLUDED_HARNESSES


def test_max_hops_cap_blocks_excess_handovers():
    sup = _sup()
    order = _order(status="RUNNING", handoff_policy=_policy(max_hops=1))
    res1 = sup.handle_failure(order, "botex", failure_kind="timeout", error_text="t/o a")
    assert res1["status"] == WorkOrderStatus.HANDOFF
    sup.begin_attempt(order, res1["target_harness"])
    res2 = sup.handle_failure(
        order, res1["target_harness"], failure_kind="quota", error_text="quota hit"
    )
    assert res2["ok"] is False
    assert res2["reason"] == "max_hops_exceeded"
    assert order.status == WorkOrderStatus.BLOCKED
    assert order.blocking_reason == "max_hops_exceeded"


def test_transition_table_and_terminal_lock():
    assert can_transition("QUEUED", "RUNNING")
    assert can_transition("RUNNING", "STUCK")
    assert can_transition("STUCK", "TRIAGED")
    assert can_transition("TRIAGED", "HANDOFF")
    assert can_transition("HANDOFF", "RUNNING")
    assert can_transition("VERIFYING", "DONE")
    assert not can_transition("RUNNING", "DONE")
    assert not can_transition("QUEUED", "DONE")
    assert not can_transition("DONE", "RUNNING")
    assert not can_transition("BLOCKED", "RUNNING")
    for state in ("QUEUED", "RUNNING", "STUCK", "TRIAGED", "HANDOFF", "VERIFYING"):
        assert can_transition(state, "FENCED")

    sup = _sup()
    order = _order(status="RUNNING")
    assert sup.transition(order, "BOGUS")["reason"] == "unknown_status"
    out = sup.fence_order(order, reason="lease_expired")
    assert out["ok"] is True
    assert order.status == WorkOrderStatus.FENCED
    assert sup.transition(order, "RUNNING")["reason"] == "order_terminal"


def test_work_order_serialization_roundtrip(tmp_path: Path):
    order = _order(
        base_ref="main",
        scope=("src/", "tests/"),
        verify=VerifySpec(kind="command", command="pytest -q", timeout_s=120.0),
        handoff_policy=_policy(allow_paid=True, paid_allowlist=("m1",)),
        epoch=2,
        status="STUCK",
        blocking_reason="",
    )
    order.budget = CostLedger(usd=0.1, acu=1.0, quota_units=2.0, turns=3)
    sup = _sup()
    sup.record_attempt(
        order, "botex", status="failed", failure_kind="rate_limit", error_text="HTTP 429"
    )
    payload = json.loads(json.dumps(order.to_dict()))
    clone = WorkOrder.from_dict(payload)
    assert clone.to_dict() == order.to_dict()
    assert clone.verify.kind == "command"
    assert clone.handoff_policy.allowed_harnesses == ("botex", "zcode", "devin")
    assert clone.budget.usd == pytest.approx(0.1)
    assert clone.attempt_ledger[0].signature

    # Unknown persisted status fails closed, never silently resumes.
    corrupt = WorkOrder.from_dict({"order_id": "x", "status": "hacked"})
    assert corrupt.status == WorkOrderStatus.BLOCKED
    assert corrupt.blocking_reason == "unknown_status"


def test_ranker_reads_analytics_readonly(tmp_path: Path):
    ledger_path = tmp_path / "ledger.jsonl"
    rows = [
        {"provider": "prov-a", "status": "DONE", "cost_usd": 0.0},
        {"provider": "prov-a", "status": "DONE", "cost_usd": 0.0},
        {
            "provider": "prov-b",
            "status": "API_ERROR",
            "failure_kind": "rate_limit",
            "cost_usd": 0.0,
        },
        {"provider": "prov-b", "status": "DONE", "cost_usd": 0.01},
    ]
    ledger_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    before = ledger_path.read_bytes()

    class _FakeLimiter:
        waits = {"prov-a": 120.0, "prov-b": 0.0, "prov-c": 0.0}

        def wait_estimate(self, provider):
            return self.waits.get(provider, 0.0)

    sup = _sup(analytics_path=ledger_path, rate_limiter=_FakeLimiter())
    order = _order(status="STUCK", handoff_policy=_policy())
    res = sup.rank_candidate_harnesses(
        order=order,
        harness_providers={
            "botex": ("prov-a",),
            "zcode": ("prov-b",),
            "devin": ("prov-c",),
        },
        failure_kind="timeout",
        error_text="timed out",
    )
    assert res["ok"] is True
    assert set(res["ranked"]) == {"botex", "zcode", "devin"}
    details = {d["harness"]: d for d in res["candidates"]}
    # prov-a: 2/2 DONE (rate 1.0), prov-b: 1/2 (rate 0.5), prov-c: no samples (0.5).
    # zcode vs devin tie on rate -> lower headroom + lower cost decide deterministically.
    assert details["botex"]["success_rate"] == pytest.approx(1.0)
    assert details["zcode"]["success_rate"] == pytest.approx(0.5)
    assert details["devin"]["samples"] == 0
    assert res["ranked"][0] == "botex"
    assert ledger_path.read_bytes() == before


def test_supervisor_audit_uses_supervisor_ledger_not_analytics(tmp_path: Path, monkeypatch):
    sup_ledger = tmp_path / "swarm" / "supervisor_ledger.jsonl"
    sup = _sup(audit=True, ledger_path=sup_ledger)
    order = _order(status="RUNNING", order_id="audit-1")
    res = sup.handle_failure(order, "botex", failure_kind="rate_limit", error_text="429")
    assert res["reason"] == "missing_handoff_policy"
    assert sup_ledger.is_file()
    entries = [
        json.loads(line) for line in sup_ledger.read_text(encoding="utf-8").splitlines() if line
    ]
    assert any(
        e.get("action") == "blocked" and e.get("reason") == "missing_handoff_policy"
        for e in entries
    )
    assert entries[0]["order_id"] == "audit-1"
    # The supervisor never writes to the run-analytics ledger.
    assert not (tmp_path / ".test_env_analytics.jsonl").exists()
    assert not (tmp_path / ".agent_analytics.jsonl").exists()


def test_handle_failure_survives_infra_classifier_exception(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr("botex.core.work_orders.is_infra_failure", _boom)
    sup = _sup()
    order = _order(status="RUNNING")
    res = sup.handle_failure(order, "botex", failure_kind="rate_limit", error_text="HTTP 429")
    assert res["ok"] is False
    assert res["reason"] == "missing_handoff_policy"
    assert order.status == WorkOrderStatus.BLOCKED
    assert order.blocking_reason == "missing_handoff_policy"
    assert res["diagnostics"]["infra"] is None
    assert res["diagnostics"]["signature"] == compute_failure_signature("rate_limit", "HTTP 429")


def test_paid_allowlist_is_case_insensitive():
    # HandoffPolicy normalizes allowlist entries like allowed_harnesses.
    policy = HandoffPolicy(
        allowed_harnesses=("botex",),
        signed_by="operator",
        allow_paid=True,
        paid_allowlist=(" Anthropic/Claude-Sonnet ", "OPENAI/GPT-5"),
    )
    assert policy.paid_allowlist == ("anthropic/claude-sonnet", "openai/gpt-5")

    # check_provider_allowed matches model/allowlist regardless of case.
    ledger = CostLedger()
    res = ledger.check_provider_allowed(
        "OpenRouter",
        " OpenAI/GPT-5 ",
        allow_paid=True,
        paid_allowlist=(" openai/gpt-5 ",),
    )
    assert res["ok"] is True
    res_forbidden = ledger.check_provider_allowed(
        "openrouter",
        "openai/gpt-5",
        allow_paid=True,
        paid_allowlist=("anthropic/claude",),
    )
    assert res_forbidden["reason"] == "paid_openrouter_forbidden"

    # The supervisor path honors the normalized policy allowlist end-to-end.
    sup = _sup()
    order = _order(
        handoff_policy=HandoffPolicy(
            allowed_harnesses=("botex",),
            signed_by="operator",
            allow_paid=True,
            paid_allowlist=("OPENAI/GPT-5",),
        )
    )
    res2 = sup.record_spend(order, "botex", usd=0.25, provider="openrouter", model="openai/gpt-5")
    assert res2["ok"] is True
    assert order.budget.usd == pytest.approx(0.25)


def test_audit_import_failure_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "botex.edge.verify_gate", None)
    sup = _sup(audit=True, ledger_path=tmp_path / "supervisor_ledger.jsonl")
    order = _order(status="RUNNING", order_id="audit-import-fail")
    with caplog.at_level(logging.WARNING, logger="botex.core.work_orders"):
        out = sup.fence_order(order, reason="lease_expired")
    assert out["ok"] is True
    assert order.status == WorkOrderStatus.FENCED
    assert any("verify_gate import failed" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "supervisor_ledger.jsonl").exists()


def test_handoff_epoch_consistent_across_supervisor_adapter_and_checkpoint(tmp_path):
    """P1-1: handle_failure -> translate_work_order -> checkpoint_and_push must agree on epoch e1."""
    from botex.edge.adapters import EpochCheckpointManager, translate_work_order

    sup = _sup()
    order = _order(
        order_id="ord-p1-1",
        epoch=0,
        status=WorkOrderStatus.RUNNING,
        handoff_policy=_policy(),
    )
    fail_out = sup.handle_failure(order, "botex", failure_kind="rate_limit", error_text="HTTP 429")
    assert fail_out["ok"] is True
    assert order.status == WorkOrderStatus.HANDOFF
    assert order.epoch == 1

    spec = translate_work_order(order, fail_out["target_harness"])
    assert spec["ok"] is True
    assert spec["epoch"] == 1
    assert spec["base_epoch"] == 0
    assert spec["branch"] == "swarm/wip/ord-p1-1/e1"

    mgr = EpochCheckpointManager(
        order_id=order.order_id,
        epoch=spec["epoch"],
        origin_repo=tmp_path,
    )
    push_res = mgr.checkpoint_and_push(
        repo_dir=tmp_path,
        commit=False,
        fetch_remote_epoch_fn=lambda **_kw: 1,
        push_fn=lambda **_kw: {"ok": True},
    )
    assert push_res["ok"] is True
    assert push_res["epoch"] == 1
    assert push_res["branch"] == "swarm/wip/ord-p1-1/e1"


def test_finalize_verified_work_order_enforces_state_machine_and_fail_transition():
    """P1-4: WorkOrder cannot jump RUNNING -> DONE, and ok=False moves VERIFYING -> FAILED."""
    from botex.edge.adapters import finalize_verified_work_order

    running_order = _order(order_id="ord-running", status=WorkOrderStatus.RUNNING)
    res_illegal = finalize_verified_work_order(running_order, {"ok": True})
    assert res_illegal["ok"] is False
    assert running_order.status == WorkOrderStatus.RUNNING

    verifying_order = _order(order_id="ord-fail", status=WorkOrderStatus.VERIFYING)
    res_fail = finalize_verified_work_order(
        verifying_order, {"ok": False, "reason": "pytest_failed"}
    )
    assert res_fail["ok"] is False
    assert verifying_order.status == WorkOrderStatus.FAILED


def test_auth_regex_word_boundaries_and_deterministic_blocks_handoff():
    """P2-1: 'author'/'oauth' in tracebacks must not match auth, and deterministic failures block handoff."""
    sup = _sup()
    traceback_order = _order(
        order_id="ord-author", status=WorkOrderStatus.RUNNING, handoff_policy=_policy()
    )
    res_author = sup.handle_failure(
        traceback_order,
        "botex",
        failure_kind="",
        error_text="AttributeError: 'Author' object has no attribute 'oauth_id'",
    )
    assert res_author["ok"] is False
    assert res_author["reason"] == "unclassified_failure"
    assert traceback_order.status == WorkOrderStatus.BLOCKED

    det_order = _order(order_id="ord-det", status=WorkOrderStatus.RUNNING, handoff_policy=_policy())
    res_det = sup.handle_failure(
        det_order,
        "botex",
        failure_kind="verification_failed",
        error_text="pytest failed 2 assertions",
    )
    assert res_det["ok"] is False
    assert res_det["reason"] == "deterministic_failure_no_handoff"
    assert det_order.status == WorkOrderStatus.BLOCKED

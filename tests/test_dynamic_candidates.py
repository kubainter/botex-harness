# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Dynamic Candidate Policy & Evidence Tests (Plan 059)
=====================================================
Exhaustive offline unit tests for ``botex.providers.dynamic_candidates``:
mode normalization, gate evaluation, evidence promotion/quarantine,
snapshot build/validate/load, and the fail-static ``resolve_execution_candidates``
chain resolver. No network access is permitted anywhere in this suite.
"""

from __future__ import annotations

import json
import socket
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from botex.providers import dynamic_candidates as dc

PROVIDER = "p1"
DAY = 86400.0


@pytest.fixture(autouse=True)
def block_live_sockets():
    """Hard guarantee: no network path may be exercised by this module."""

    def blocked_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Blocked live network socket call during unit test execution")

    with patch.object(socket.socket, "connect", side_effect=blocked_connect):
        yield


def _meta(
    model_id: str = "vendor/m1",
    *,
    context_length: Any = 131072,
    prompt: Any = 0.0,
    completion: Any = 0.0,
    tools: Any = True,
    zdr: Any = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=model_id,
        context_length=context_length,
        prompt_price_per_m=prompt,
        completion_price_per_m=completion,
        supports_tools=tools,
        is_zdr_compliant=zdr,
    )


def _dict_meta(
    *,
    context_length: Any = 131072,
    prompt: Any = 0.0,
    completion: Any = 0.0,
    tools: Any = True,
    zdr: Any = True,
) -> Dict[str, Any]:
    return {
        "context_length": context_length,
        "prompt_price_per_m": prompt,
        "completion_price_per_m": completion,
        "supports_tools": tools,
        "is_zdr_compliant": zdr,
    }


def _dynamic_cfg(
    *,
    allow: Any = ("vendor/*",),
    deny: Any = (),
    enabled: bool = True,
    providers: Optional[Dict[str, Any]] = None,
    task_classes: Optional[Dict[str, Any]] = None,
    promotion: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if providers is None:
        providers = {
            PROVIDER: {
                "enabled": enabled,
                "allow_models": list(allow),
                "deny_models": list(deny),
            }
        }
    out: Dict[str, Any] = {"providers": providers}
    if task_classes is not None:
        out["task_classes"] = task_classes
    if promotion is not None:
        out["promotion"] = promotion
    return out


def _cfg(
    mode: str = "append",
    *,
    dyn_extra: Optional[Dict[str, Any]] = None,
    provider_cfg: Optional[Dict[str, Any]] = None,
    providers: Optional[Dict[str, Any]] = None,
    **dynamic_section: Any,
) -> Dict[str, Any]:
    section = (
        _dynamic_cfg(providers=providers, **dynamic_section) if dynamic_section else _dynamic_cfg()
    )
    if providers is not None:
        section["providers"] = providers
    section["mode"] = mode
    if dyn_extra:
        section.update(dyn_extra)
    return {
        "dynamic_candidates": section,
        "providers": {PROVIDER: provider_cfg if provider_cfg is not None else {}},
    }


def _gates(
    *,
    allowlisted: bool = True,
    denylisted: bool = False,
    tools: bool = True,
    price: str = "free",
    prompt: Optional[float] = 0.0,
    completion: Optional[float] = 0.0,
    zdr: str = "not_supported",
    ctx: int = 131072,
) -> Dict[str, Any]:
    return {
        "allowlisted": allowlisted,
        "denylisted": denylisted,
        "supports_tools": tools,
        "price": price,
        "prompt_price": prompt,
        "completion_price": completion,
        "zdr": zdr,
        "context_length": ctx,
    }


def _cand(
    model: str,
    *,
    state: str = "active",
    rank: float = 0.5,
    task_classes: Any = ("bugfix",),
    gates: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    entry = {
        "model": model,
        "state": state,
        "rank_score": rank,
        "gates": gates if gates is not None else _gates(),
        "evidence": {
            "window_days": 30,
            "runs": 3,
            "done_runs": 3,
            "quality_failures": 0,
            "infra_failures": 0,
            "rollback_count": 0,
            "syntax_rejections": 0,
        },
        "task_classes": list(task_classes),
        "reasons": [],
    }
    if extra:
        entry.update(extra)
    return entry


def _snap(
    candidates: List[Dict[str, Any]],
    *,
    provider: str = PROVIDER,
    generated_at: Optional[float] = None,
    catalog_state: str = "fresh",
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": generated_at if generated_at is not None else time.time(),
        "generated_at_iso": "2026-11-27T00:00:00+00:00",
        "generator": "test",
        "source": {"analytics_window_days": 30, "catalog_drift_generated_at": None},
        "providers": {
            provider: {
                "catalog_fetched_at": generated_at,
                "catalog_state": catalog_state,
                "candidates": candidates,
            }
        },
    }


def _row(
    *,
    provider: str = PROVIDER,
    model: str = "vendor/m1",
    status: str = "DONE",
    failure_kind: Any = None,
    taxonomy: str = "bugfix",
    ts: Any = None,
    rollback: Any = 0,
    syntax: Any = 0,
    mode: Any = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "status": status,
        "taxonomy": taxonomy,
        "ts": ts if ts is not None else time.time(),
        "rollback_count": rollback,
        "syntax_rejections": syntax,
    }
    if failure_kind is not None:
        row["failure_kind"] = failure_kind
    if mode is not None:
        row["mode"] = mode
    return row


# ============================================================================
# Mode & key normalization
# ============================================================================


class TestNormalizeDynamicMode:
    def test_defaults_to_off(self):
        for bad in (None, "", "   ", "enable", "true", 1, "APPENDX", {}, []):
            assert dc.normalize_dynamic_mode(bad) == "off"

    def test_canonicalizes_case_and_whitespace(self):
        assert dc.normalize_dynamic_mode(" Append ") == "append"
        assert dc.normalize_dynamic_mode("SHADOW") == "shadow"
        assert dc.normalize_dynamic_mode("off") == "off"

    def test_emergency_off_wins_over_append(self):
        assert dc.normalize_dynamic_mode("append", emergency_env="off") == "off"

    def test_emergency_shadow_wins_over_append(self):
        assert dc.normalize_dynamic_mode("append", emergency_env="shadow") == "shadow"

    def test_emergency_shadow_wins_over_off(self):
        assert dc.normalize_dynamic_mode("off", emergency_env="shadow") == "shadow"

    def test_emergency_append_never_escalates(self):
        assert dc.normalize_dynamic_mode("off", emergency_env="append") == "off"
        assert dc.normalize_dynamic_mode("shadow", emergency_env="append") == "shadow"

    def test_emergency_invalid_ignored(self):
        assert dc.normalize_dynamic_mode("append", emergency_env="garbage") == "append"
        assert dc.normalize_dynamic_mode("append", emergency_env="") == "append"
        assert dc.normalize_dynamic_mode("off", emergency_env=None) == "off"


class TestNormalizeModelKey:
    def test_strip_and_lowercase(self):
        assert dc.normalize_model_key("  Vendor/M1:FREE ") == "vendor/m1:free"

    def test_strips_one_models_prefix(self):
        assert dc.normalize_model_key("models/gemini-2.5-pro") == "gemini-2.5-pro"
        assert dc.normalize_model_key("MODELS/Gemini-2.5-Pro") == "gemini-2.5-pro"
        assert dc.normalize_model_key("models/models/x") == "models/x"

    def test_empty_and_nonstring(self):
        assert dc.normalize_model_key("") == ""
        assert dc.normalize_model_key(None) == ""


# ============================================================================
# Failure classification (infra vs quality)
# ============================================================================


class TestFailureRowClassification:
    @pytest.mark.parametrize(
        "kind",
        [
            "rate_limit",
            "request_timeout",
            "provider_error",
            "provider_policy_denied",
            "provider_unavailable",
            "provider_rate_limited",
            "provider_auth_error",
            "auth_error",
            "quota_exceeded",
            "budget_exceeded",
            "budget_or_price_block",
            "config_error",
            "connection_error",
            "timed_out",
            "TIMEOUT",
        ],
    )
    def test_infra_kinds(self, kind):
        assert dc.is_infra_failure_row("API_ERROR", kind) is True
        assert dc.is_quality_failure_row(_row(status="FAILED", failure_kind=kind)) is False

    def test_aborted_is_infra(self):
        assert dc.is_infra_failure_row("ABORTED", "no_progress") is True
        assert (
            dc.is_quality_failure_row(_row(status="ABORTED", failure_kind="no_progress")) is False
        )

    @pytest.mark.parametrize(
        "kind",
        [
            "no_progress",
            "contract_failed",
            "contract_failure",
            "model_refusal",
            "turns_exhausted",
            "syntax_rejected",
        ],
    )
    def test_quality_kinds(self, kind):
        assert dc.is_quality_failure_row(_row(status="DONE", failure_kind=kind)) is True

    def test_non_done_status_is_quality(self):
        for status in ("FAILED", "INCOMPLETE", "VERIFICATION_FAILED", "STAGNANT_ROLLBACK", ""):
            assert dc.is_quality_failure_row(_row(status=status)) is True

    def test_counters_flip_done_to_quality(self):
        assert dc.is_quality_failure_row(_row(status="DONE", rollback=1)) is True
        assert dc.is_quality_failure_row(_row(status="DONE", syntax=2)) is True
        assert dc.is_quality_failure_row(_row(status="DONE")) is False

    def test_infra_beats_quality_status(self):
        assert (
            dc.is_quality_failure_row(_row(status="FAILED", failure_kind="provider_error")) is False
        )

    def test_malformed_row_guards(self):
        assert dc.is_quality_failure_row("not a dict") is False
        assert dc.is_quality_failure_row(None) is False
        assert dc.is_infra_failure_row(None, None) is False


# ============================================================================
# evaluate_candidate_gates
# ============================================================================


class TestEvaluateCandidateGates:
    def test_happy_path_free(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1:free",
            metadata=_meta("vendor/m1:free"),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["passed_hard_gates"] is True
        assert res["gates"]["allowlisted"] is True
        assert res["gates"]["price"] == "free"
        assert res["gates"]["zdr"] == "not_supported"  # :free can never be ZDR
        assert res["reasons"] == []

    def test_empty_allowlist_blocks(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(),
            dynamic_cfg=_dynamic_cfg(allow=[]),
        )
        assert res["passed_hard_gates"] is False
        assert res["gates"]["allowlisted"] is False
        assert "empty_allowlist" in res["reasons"]

    def test_allowlist_mismatch(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="other/m9",
            metadata=_meta("other/m9"),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["allowlisted"] is False
        assert "allowlist_mismatch" in res["reasons"]

    def test_provider_disabled_and_missing(self):
        for cfg in (_dynamic_cfg(enabled=False), _dynamic_cfg(providers={}), {"providers": "x"}):
            res = dc.evaluate_candidate_gates(
                provider=PROVIDER, model_id="vendor/m1", metadata=_meta(), dynamic_cfg=cfg
            )
            assert res["gates"]["allowlisted"] is False
            assert res["passed_hard_gates"] is False

    def test_provider_deny_models_wins_over_allow(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1-evil",
            metadata=_meta(),
            dynamic_cfg=_dynamic_cfg(allow=["vendor/*"], deny=["*-evil"]),
        )
        assert res["gates"]["allowlisted"] is False
        assert "provider_deny_models" in res["reasons"]

    def test_denylist_fn_wins_over_allowlist(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(),
            dynamic_cfg=_dynamic_cfg(),
            denylist_fn=lambda m: True,
        )
        assert res["gates"]["denylisted"] is True
        assert res["passed_hard_gates"] is False
        assert "denylisted" in res["reasons"]

    def test_denylist_fn_exception_fails_closed(self):
        def boom(_m: str) -> bool:
            raise RuntimeError("denylist exploded")

        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(),
            dynamic_cfg=_dynamic_cfg(),
            denylist_fn=boom,
        )
        assert res["gates"]["denylisted"] is True
        assert res["passed_hard_gates"] is False

    @pytest.mark.parametrize(
        "model_id",
        [
            "vendor/m1:batch",
            "vendor/embed-1",
            "vendor/text-embedding-3",
            "vendor/x-rerank-v2",
            "vendor/whisper-large",
            "vendor/fast-tts-1",
            "vendor/moderation-latest",
        ],
    )
    def test_non_chat_entries_rejected(self, model_id):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id=model_id,
            metadata=_meta(model_id),
            dynamic_cfg=_dynamic_cfg(allow=["*"]),
        )
        assert res["passed_hard_gates"] is False
        assert "non_chat_entry" in res["reasons"]

    def test_decisions_models_rejected(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/decisions-alpha",
            metadata=_meta(),
            dynamic_cfg=_dynamic_cfg(allow=["*"]),
            provider_cfg={"decisions_models": ["vendor/decisions-*"]},
        )
        assert res["passed_hard_gates"] is False
        assert "decisions_only" in res["reasons"]

    def test_tools_true_passes(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(tools=True),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["supports_tools"] is True
        assert res["passed_hard_gates"] is True

    def test_tools_false_rejected(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(tools=False),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["passed_hard_gates"] is False
        assert "tools_unsupported" in res["reasons"]

    def test_tools_unknown_rejected(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(tools=None),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["supports_tools"] is False
        assert res["passed_hard_gates"] is False
        assert "tools_unknown" in res["reasons"]

    def test_tools_operator_override_beats_catalog(self):
        caps = {"capabilities": {"vendor/m1": {"supports_tools": True}}}
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(tools=None),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg=caps,
        )
        assert res["gates"]["supports_tools"] is True
        assert res["passed_hard_gates"] is True

        caps_false = {"capabilities": {"vendor/m1": {"supports_tools": False}}}
        res2 = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(tools=True),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg=caps_false,
        )
        assert res2["gates"]["supports_tools"] is False
        assert res2["passed_hard_gates"] is False

    def test_tools_override_normalized_key_match(self):
        caps = {"capabilities": {"models/vendor/m1": {"supports_tools": True}}}
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="Vendor/M1",
            metadata=_meta(tools=None),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg=caps,
        )
        assert res["gates"]["supports_tools"] is True

    def test_price_free_slug(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1:free",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["price"] == "free"
        assert res["gates"]["prompt_price"] == 0.0

    def test_price_zero_pair_is_free(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=0.0, completion=0.0),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["price"] == "free"

    def test_price_paid(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=0.5, completion=1.5),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["price"] == "paid"
        assert res["gates"]["prompt_price"] == 0.5
        assert res["gates"]["completion_price"] == 1.5
        assert res["passed_hard_gates"] is True

    def test_price_unknown_rejected(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["price"] == "unknown"
        assert res["passed_hard_gates"] is False
        assert "price_unknown" in res["reasons"]

    def test_price_one_side_missing_is_unknown(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=0.5, completion=None),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["price"] == "unknown"
        assert res["passed_hard_gates"] is False

    def test_pricing_override_beats_metadata(self):
        pcfg = {"pricing_overrides": {"vendor/m1": {"input": 0.25, "output": 0.75}}}
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg=pcfg,
        )
        assert res["gates"]["price"] == "paid"
        assert res["gates"]["prompt_price"] == 0.25
        assert res["gates"]["completion_price"] == 0.75

    def test_flat_pricing_override(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg={"pricing": {"prompt": 0.1, "completion": 0.2}},
        )
        assert res["gates"]["price"] == "paid"

    def test_provider_free_tier_flag(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg={"free_tier": True},
        )
        assert res["gates"]["price"] == "free"

    def test_dash_free_provider_name_is_free_tier(self):
        cfg = _dynamic_cfg(providers={"openrouter-free": {"enabled": True, "allow_models": ["*"]}})
        res = dc.evaluate_candidate_gates(
            provider="openrouter-free",
            model_id="vendor/m1",
            metadata=_meta(prompt=None, completion=None),
            dynamic_cfg=cfg,
        )
        assert res["gates"]["price"] == "free"

    def test_zdr_not_supported_flag(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(zdr=False),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["gates"]["zdr"] == "not_supported"
        # zdr is a gate *value*: it does not fail hard gates by itself
        assert res["passed_hard_gates"] is True

    def test_context_missing_or_zero_rejected(self):
        for bad_ctx in (None, 0, -5, "junk"):
            res = dc.evaluate_candidate_gates(
                provider=PROVIDER,
                model_id="vendor/m1",
                metadata=_meta(context_length=bad_ctx),
                dynamic_cfg=_dynamic_cfg(),
            )
            assert res["gates"]["context_length"] == 0
            assert res["passed_hard_gates"] is False
            assert "context_unknown" in res["reasons"]

    def test_context_override_beats_metadata(self):
        caps = {"capabilities": {"vendor/m1": {"context_length": 32768, "supports_tools": True}}}
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_meta(context_length=0),
            dynamic_cfg=_dynamic_cfg(),
            provider_cfg=caps,
        )
        assert res["gates"]["context_length"] == 32768
        assert res["passed_hard_gates"] is True

    def test_dict_metadata_duck_typing(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=_dict_meta(),
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["passed_hard_gates"] is True

    def test_none_metadata_fails_every_gate(self):
        res = dc.evaluate_candidate_gates(
            provider=PROVIDER,
            model_id="vendor/m1",
            metadata=None,
            dynamic_cfg=_dynamic_cfg(),
        )
        assert res["passed_hard_gates"] is False
        assert res["gates"]["supports_tools"] is False
        assert res["gates"]["price"] == "unknown"
        assert res["gates"]["context_length"] == 0


# ============================================================================
# build_dynamic_candidates_snapshot — evidence, promotion, quarantine
# ============================================================================


class TestBuildSnapshotBasics:
    def test_schema_shape(self):
        now = time.time()
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1:free": _meta("vendor/m1:free")}},
            analytics_rows=[],
            catalog_fetched_at_map={PROVIDER: now - 60},
            catalog_drift_generated_at=now - 120,
            now_ts=now,
            generator="unit-test",
        )
        assert snap["schema_version"] == 1
        assert snap["generated_at"] == now
        assert snap["generator"] == "unit-test"
        assert snap["source"]["catalog_drift_generated_at"] == now - 120
        assert snap["source"]["analytics_window_days"] == 30.0
        prov = snap["providers"][PROVIDER]
        assert prov["catalog_state"] == "fresh"
        assert prov["catalog_fetched_at"] == now - 60
        assert prov["candidates"][0]["state"] == "eligible"  # no evidence

    def test_deterministic_fixed_clock(self):
        now = time.time()
        rows = [_row(ts=now - i * 100) for i in range(3)]
        kwargs = dict(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        s1 = dc.build_dynamic_candidates_snapshot(**kwargs)
        s2 = dc.build_dynamic_candidates_snapshot(**kwargs)
        assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)

    def test_golden_candidate_fields(self):
        now = time.time()
        rows = [_row(ts=now - i * 100) for i in range(3)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "active"
        assert c["task_classes"] == ["bugfix"]
        assert c["rank_score"] == 1.0
        assert c["evidence"]["runs"] == 3
        assert c["evidence"]["done_runs"] == 3

    def test_golden_rank_score_mixed(self):
        now = time.time()
        rows = [_row(ts=now - 10) for _ in range(3)] + [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 20)
        ]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        # done_rate .75 - 0.5*quality_rate .25 = .625
        assert c["rank_score"] == 0.625
        assert c["evidence"]["quality_failures"] == 1


class TestEvidenceWindowAndAttribution:
    def test_stale_evidence_outside_window_ignored(self):
        now = time.time()
        rows = [_row(ts=now - 40 * DAY) for _ in range(5)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["evidence"]["runs"] == 0
        assert c["state"] == "eligible"

    def test_window_boundary_inclusive(self):
        now = time.time()
        rows = [_row(ts=now - 30 * DAY), _row(ts=now - 30 * DAY - 1)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        assert snap["providers"][PROVIDER]["candidates"][0]["evidence"]["runs"] == 1

    def test_future_rows_excluded(self):
        now = time.time()
        rows = [_row(ts=now + 1000), _row(ts=now - 10)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        assert snap["providers"][PROVIDER]["candidates"][0]["evidence"]["runs"] == 1

    def test_iso_timestamp_rows_parsed(self):
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 100))
        rows = [
            {
                "provider": PROVIDER,
                "model": "vendor/m1",
                "status": "DONE",
                "taxonomy": "bugfix",
                "timestamp": iso,
            }
            for _ in range(3)
        ]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        assert snap["providers"][PROVIDER]["candidates"][0]["evidence"]["runs"] == 3

    def test_evidence_key_normalizes_model_and_provider(self):
        now = time.time()
        rows = [_row(provider=" P1 ", model="MODELS/Vendor/M1 ", ts=now - 10) for _ in range(3)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["evidence"]["runs"] == 3
        assert c["state"] == "active"

    def test_wrong_provider_rows_not_attributed(self):
        now = time.time()
        rows = [_row(provider="other", ts=now - 10) for _ in range(3)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        assert snap["providers"][PROVIDER]["candidates"][0]["evidence"]["runs"] == 0

    def test_mode_refinement_recorded(self):
        now = time.time()
        rows = [
            _row(ts=now - 10, mode="edit"),
            _row(ts=now - 20, mode="EDIT"),
            _row(ts=now - 30, mode="bogus-mode"),
        ]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["capability_modes"] == {"edit": 2}

    def test_malformed_rows_never_crash(self):
        now = time.time()
        rows = [
            "garbage",
            None,
            {},
            {"provider": PROVIDER},  # no model
            {"model": "vendor/m1"},  # no provider
            _row(ts="not-a-timestamp"),
            _row(ts=None),
            _row(rollback="abc", syntax={"x": 1}, ts=now - 10),
            _row(rollback=float("nan"), syntax=float("inf"), ts=now - 10),
            _row(rollback=True, syntax=False, ts=now - 10),
        ]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        # only rows with provider+model+valid ts count; garbage numerics coerce to 0
        assert c["evidence"]["runs"] == 3
        assert c["evidence"]["rollback_count"] == 0
        assert c["evidence"]["syntax_rejections"] == 0


class TestPromotionThresholds:
    def _build(self, rows, task_classes=None, promotion=None):
        now = time.time()
        dyn: Dict[str, Any] = {}
        if task_classes is not None:
            dyn["task_classes"] = task_classes
        if promotion is not None:
            dyn["promotion"] = promotion
        return dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(dyn_extra=dyn),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        )

    def test_sparse_evidence_stays_eligible(self):
        now = time.time()
        snap = self._build([_row(ts=now - 10)])
        assert snap["providers"][PROVIDER]["candidates"][0]["state"] == "eligible"

    def test_sufficient_evidence_promotes_active(self):
        now = time.time()
        snap = self._build([_row(ts=now - i) for i in (10, 20, 30)])
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "active"
        assert c["task_classes"] == ["bugfix"]

    def test_done_rate_below_min_stays_eligible(self):
        now = time.time()
        rows = [_row(ts=now - 10)] + [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 20),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 30),
        ]
        snap = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        # newest row is DONE -> streak broken; done_rate 1/3 < 0.6 -> eligible
        assert c["state"] == "eligible"
        assert c["evidence"]["quality_failures"] == 2

    def test_done_rate_below_min_quarantines_when_tail_quality(self):
        now = time.time()
        rows = [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 10),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 20),
            _row(ts=now - 30),
        ]
        snap = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "quarantined"

    def test_rollback_heavy_blocks_active(self):
        now = time.time()
        rows = [_row(rollback=2, ts=now - i) for i in (10, 20, 30)]
        snap = self._build(
            rows,
            promotion={"quarantine_after_consecutive_quality_failures": 99},
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "eligible"
        assert c["evidence"]["rollback_count"] == 6

    def test_syntax_heavy_blocks_active(self):
        now = time.time()
        rows = [_row(syntax=3, ts=now - i) for i in (10, 20, 30)]
        snap = self._build(
            rows,
            promotion={"quarantine_after_consecutive_quality_failures": 99},
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "eligible"
        assert c["evidence"]["syntax_rejections"] == 9

    def test_per_task_class_divergence(self):
        now = time.time()
        rows = [_row(taxonomy="analysis", ts=now - i) for i in (10, 20, 30)]
        snap = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "active"
        assert c["task_classes"] == ["analysis"]
        assert "bugfix" not in c["task_classes"]

    def test_disabled_class_never_promotes(self):
        now = time.time()
        rows = [_row(taxonomy="feature", ts=now - i) for i in (10, 20, 30)]
        snap = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "eligible"
        assert c["task_classes"] == []

    def test_min_context_blocks_class_promotion(self):
        now = time.time()
        meta = _meta(context_length=16384)
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": meta}},
            analytics_rows=[_row(taxonomy="analysis", ts=now - i) for i in (10, 20, 30)],
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        # analysis needs 32768 -> below min; state stays eligible
        assert c["state"] == "eligible"

    def test_allow_paid_false_blocks_paid_class(self):
        now = time.time()
        meta = _meta(prompt=1.0, completion=2.0)
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": meta}},
            analytics_rows=[_row(ts=now - i) for i in (10, 20, 30)],
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["gates"]["price"] == "paid"
        assert c["state"] == "eligible"

    def test_allow_paid_true_promotes_paid(self):
        now = time.time()
        meta = _meta(prompt=1.0, completion=2.0)
        task_classes = {
            "bugfix": {
                "enabled": True,
                "allow_unproven": False,
                "min_context_length": 1,
                "allow_paid": True,
            }
        }
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(dyn_extra={"task_classes": task_classes}),
            catalogs={PROVIDER: {"vendor/m1": meta}},
            analytics_rows=[_row(ts=now - i) for i in (10, 20, 30)],
            now_ts=now,
        )
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "active"
        assert c["task_classes"] == ["bugfix"]


class TestQuarantine:
    def _build(self, rows, promotion=None):
        now = time.time()
        return dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(dyn_extra={"promotion": promotion} if promotion else None),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
        ), now

    def test_consecutive_quality_tail_quarantines(self):
        now = time.time()
        rows = [
            _row(ts=now - 100),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 60),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 50),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] == "quarantined"
        assert c["quarantined_until"] == pytest.approx(now - 50 + 72 * 3600.0)
        assert "quarantined_until_iso" in c

    def test_infra_only_burst_never_quarantines(self):
        now = time.time()
        rows = [
            _row(status="API_ERROR", failure_kind="provider_rate_limited", ts=now - i)
            for i in (10, 20, 30, 40, 50)
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] != "quarantined"
        assert c["evidence"]["infra_failures"] == 5
        assert c["evidence"]["quality_failures"] == 0

    def test_mixed_infra_between_quality_still_quarantines(self):
        now = time.time()
        rows = [
            _row(ts=now - 500),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 90),
            _row(status="API_ERROR", failure_kind="provider_error", ts=now - 80),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 70),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        # infra rows are skipped for the streak, not a streak-breaker
        assert c["state"] == "quarantined"
        assert c["evidence"]["infra_failures"] == 1
        assert c["evidence"]["quality_failures"] == 2

    def test_done_breaks_streak(self):
        now = time.time()
        rows = [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 90),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 80),
            _row(ts=now - 70),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] != "quarantined"

    def test_streak_below_threshold_no_quarantine(self):
        now = time.time()
        rows = [
            _row(ts=now - 90),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 80),
            _row(ts=now - 70),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 60),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] != "quarantined"

    def test_quarantine_expiry_reenters_eligible_not_active(self):
        now = time.time()
        rows = [
            _row(ts=now - 200),
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 100 * 3600),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 99 * 3600),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        # last quality failure 99h ago > 72h cooldown; evidence insufficient -> eligible
        assert c["state"] == "eligible"
        assert "quarantined_until" not in c

    def test_post_quarantine_positive_runs_reearn_active(self):
        now = time.time()
        rows = [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 90),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 80),
            _row(ts=now - 30),
            _row(ts=now - 20),
            _row(ts=now - 10),
        ]
        snap, _ = self._build(rows)
        c = snap["providers"][PROVIDER]["candidates"][0]
        # DONE tail breaks streak; 3 done / 5 runs meets thresholds -> active again
        assert c["state"] == "active"
        assert c["task_classes"] == ["bugfix"]

    def test_custom_quarantine_window(self):
        now = time.time()
        rows = [
            _row(status="FAILED", failure_kind="contract_failed", ts=now - 30 * 3600),
            _row(status="FAILED", failure_kind="no_progress", ts=now - 29 * 3600),
        ]
        snap, _ = self._build(rows, promotion={"quarantine_hours": 1})
        c = snap["providers"][PROVIDER]["candidates"][0]
        assert c["state"] != "quarantined"


class TestProviderIsolationAndEmptyCatalogs:
    def test_empty_catalog_preserves_existing(self):
        now = time.time()
        existing = _snap([_cand("vendor/old", state="active")], provider=PROVIDER)
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {}},
            analytics_rows=[],
            existing_snapshot=existing,
            now_ts=now,
        )
        prov = snap["providers"][PROVIDER]
        assert prov["catalog_state"] == "stale_or_empty"
        assert prov["candidates"][0]["model"] == "vendor/old"

    def test_empty_catalog_no_existing_emits_no_data(self):
        now = time.time()
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {}},
            analytics_rows=[],
            now_ts=now,
        )
        prov = snap["providers"][PROVIDER]
        assert prov["catalog_state"] == "no_data"
        assert prov["candidates"] == []

    def test_missing_catalog_provider_in_allowlist(self):
        now = time.time()
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={},
            analytics_rows=[],
            now_ts=now,
        )
        assert snap["providers"][PROVIDER]["catalog_state"] == "no_data"

    def test_one_provider_error_isolated(self):
        class ExplodingCatalog(dict):
            def items(self):
                raise RuntimeError("corrupt catalog")

        now = time.time()
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(
                providers={
                    "bad": {"enabled": True, "allow_models": ["*"]},
                    PROVIDER: {"enabled": True, "allow_models": ["vendor/*"]},
                }
            ),
            catalogs={"bad": ExplodingCatalog({"x": 1}), PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=[_row(ts=now - i) for i in (10, 20, 30)],
            now_ts=now,
        )
        assert snap["providers"]["bad"]["catalog_state"] == "error"
        assert snap["providers"]["bad"]["candidates"] == []
        assert snap["providers"][PROVIDER]["catalog_state"] == "fresh"
        assert snap["providers"][PROVIDER]["candidates"][0]["state"] == "active"

    def test_candidate_sorting_is_deterministic(self):
        now = time.time()
        catalogs = {
            PROVIDER: {
                "vendor/no-gates": _dict_meta(
                    tools=None, prompt=None, completion=None, context_length=0
                ),
                "vendor/m2": _meta("vendor/m2"),
                "vendor/m1": _meta("vendor/m1"),
            }
        }
        rows = [_row(model="vendor/m1", ts=now - i) for i in (10, 20, 30)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(providers={PROVIDER: {"enabled": True, "allow_models": ["*"]}}),
            catalogs=catalogs,
            analytics_rows=rows,
            now_ts=now,
        )
        states = [c["state"] for c in snap["providers"][PROVIDER]["candidates"]]
        assert states == ["active", "eligible", "observed"]
        names = [c["model"] for c in snap["providers"][PROVIDER]["candidates"]]
        assert names[0] == "vendor/m1"
        assert names[2] == "vendor/no-gates"


class TestSnapshotContentHygiene:
    def test_no_task_ids_prompts_or_secrets(self):
        now = time.time()
        canary_row = _row(ts=now - 10)
        canary_row.update(
            {
                "task_id": "CANARY-TASK-ID-999",
                "summary": "CANARY-PROMPT-TEXT sk-canary-secret",
                "files_touched": ["C:\\abs\\secret\\path.txt"],
            }
        )
        rows = [canary_row] + [_row(ts=now - i) for i in (20, 30)]
        snap = dc.build_dynamic_candidates_snapshot(
            cfg=_cfg(),
            catalogs={PROVIDER: {"vendor/m1": _meta()}},
            analytics_rows=rows,
            now_ts=now,
            generator="night-pulse",
        )
        blob = json.dumps(snap)
        for canary in ("CANARY", "sk-canary", "secret\\path", "task-secret"):
            assert canary not in blob
        # allowed content: provider/model ids, counts, enums, timestamps
        assert snap["providers"][PROVIDER]["candidates"][0]["evidence"]["runs"] == 3


# ============================================================================
# validate_dynamic_snapshot / load_dynamic_snapshot_offline
# ============================================================================


class TestValidateSnapshot:
    def test_valid_ok(self):
        snap, reason = dc.validate_dynamic_snapshot(_snap([]))
        assert reason == "ok"
        assert snap is not None

    def test_not_a_mapping(self):
        for bad in (None, [], "x", 42):
            snap, reason = dc.validate_dynamic_snapshot(bad)
            assert snap is None
            assert reason == "not_a_mapping"

    def test_unsupported_schema_fails_closed(self):
        data = _snap([])
        data["schema_version"] = 2
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert snap is None
        assert reason == "unsupported_schema"

    def test_missing_generated_at(self):
        data = _snap([])
        del data["generated_at"]
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert reason == "missing_generated_at"

    def test_stale_rejected(self):
        data = _snap([], generated_at=time.time() - 100 * 3600)
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert snap is None
        assert reason == "stale"

    def test_custom_max_age(self):
        data = _snap([], generated_at=time.time() - 2 * 3600)
        snap, reason = dc.validate_dynamic_snapshot(data, max_age_hours=1)
        assert reason == "stale"
        snap, reason = dc.validate_dynamic_snapshot(data, max_age_hours=200)
        assert reason == "ok"

    def test_missing_or_malformed_providers(self):
        snap, reason = dc.validate_dynamic_snapshot(
            {"schema_version": 1, "generated_at": time.time()}
        )
        assert reason == "missing_providers"

        data = _snap([])
        data["providers"] = []
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert reason == "missing_providers"

        data = _snap([])
        data["providers"][PROVIDER] = "broken"
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert reason == "malformed_provider_entry"

        data = _snap([])
        data["providers"][PROVIDER]["candidates"] = "oops"
        snap, reason = dc.validate_dynamic_snapshot(data)
        assert reason == "malformed_provider_entry"


class TestLoadSnapshotOffline:
    def test_missing_file(self, tmp_path):
        snap, reason = dc.load_dynamic_snapshot_offline(tmp_path / "nope.json")
        assert snap is None
        assert reason == "missing"

    def test_valid_roundtrip(self, tmp_path):
        path = tmp_path / "snap.json"
        expected = _snap([_cand("vendor/m1")])
        path.write_text(json.dumps(expected), encoding="utf-8")
        snap, reason = dc.load_dynamic_snapshot_offline(path)
        assert reason == "ok"
        assert snap["providers"][PROVIDER]["candidates"][0]["model"] == "vendor/m1"

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "snap.json"
        path.write_text("{not json", encoding="utf-8")
        snap, reason = dc.load_dynamic_snapshot_offline(path)
        assert snap is None
        assert reason == "malformed"

    def test_stale_file(self, tmp_path):
        path = tmp_path / "snap.json"
        path.write_text(
            json.dumps(_snap([], generated_at=time.time() - 200 * 3600)), encoding="utf-8"
        )
        snap, reason = dc.load_dynamic_snapshot_offline(path)
        assert snap is None
        assert reason == "stale"

    def test_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dc, "MAX_SNAPSHOT_BYTES", 16)
        path = tmp_path / "snap.json"
        path.write_text(json.dumps(_snap([])), encoding="utf-8")
        snap, reason = dc.load_dynamic_snapshot_offline(path)
        assert snap is None
        assert reason == "too_large"


# ============================================================================
# resolve_execution_candidates
# ============================================================================


class TestResolveModes:
    def test_off_is_byte_identical(self):
        snap = _snap([_cand("vendor/m1")])
        res = dc.resolve_execution_candidates(
            ["static/a", "static/b"],
            provider=PROVIDER,
            cfg=_cfg(mode="off"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a", "static/b"]
        assert res["dynamic_candidates"] == []
        assert res["would_append"] == []
        assert res["dynamic_mode"] == "off"

    def test_shadow_computes_would_append_but_static(self):
        snap = _snap([_cand("vendor/m1")])
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            task_class="bugfix",
            capability_mode="edit",
            cfg=_cfg(mode="shadow"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a"]
        assert res["would_append"] == ["vendor/m1"]
        assert res["dynamic_candidates"] == ["vendor/m1"]
        assert res["dynamic_mode"] == "shadow"
        assert res["snapshot_state"] == "ok"

    def test_append_appends_tail_after_static_head(self):
        snap = _snap([_cand("vendor/m1"), _cand("vendor/m2", rank=0.4)])
        res = dc.resolve_execution_candidates(
            ["static/a", "static/b"],
            provider=PROVIDER,
            task_class="bugfix",
            capability_mode="edit",
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a", "static/b", "vendor/m1", "vendor/m2"]

    def test_emergency_env_forces_off(self):
        snap = _snap([_cand("vendor/m1")])
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
            emergency_env="off",
        )
        assert res["candidates"] == ["static/a"]
        assert res["dynamic_mode"] == "off"

    def test_emergency_env_forces_shadow(self):
        snap = _snap([_cand("vendor/m1")])
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            task_class="bugfix",
            capability_mode="edit",
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
            emergency_env="shadow",
        )
        assert res["candidates"] == ["static/a"]
        assert res["would_append"] == ["vendor/m1"]


class TestResolveExplicitAndFailStatic:
    @pytest.mark.parametrize("mode", ["off", "shadow", "append"])
    def test_explicit_model_bypasses_everything(self, mode):
        snap = _snap([_cand("vendor/m1"), _cand("vendor/m2")])
        res = dc.resolve_execution_candidates(
            ["explicit/model"],
            provider=PROVIDER,
            cfg=_cfg(mode=mode),
            snapshot=snap,
            snapshot_state="ok",
            explicit_model="explicit/model",
        )
        assert res["candidates"] == ["explicit/model"]
        assert res["would_append"] == []
        assert res["dynamic_candidates"] == []

    def test_explicit_bypass_with_garbage_snapshot(self):
        res = dc.resolve_execution_candidates(
            ["explicit/model"],
            provider=PROVIDER,
            cfg=_cfg(mode="append"),
            snapshot={"junk": True},
            snapshot_state="ok",
            explicit_model=" explicit/model ",
        )
        assert res["candidates"] == ["explicit/model"]

    @pytest.mark.parametrize(
        "snap_state,snapshot,expected_diag",
        [
            ("missing", None, "missing"),
            ("not_loaded", None, "not_loaded"),
            ("stale", None, "stale"),
            ("ok", {"x": 1}, "unsupported_schema"),
            ("ok", _snap([], generated_at=time.time() - 200 * 3600), "stale"),
        ],
    )
    def test_snapshot_failure_modes_return_static(self, snap_state, snapshot, expected_diag):
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            cfg=_cfg(mode="append"),
            snapshot=snapshot,
            snapshot_state=snap_state,
        )
        assert res["candidates"] == ["static/a"]
        assert res["snapshot_state"] == expected_diag

    def test_schema_mismatch_returns_static(self):
        snap = _snap([_cand("vendor/m1")])
        snap["schema_version"] = 99
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a"]
        assert res["snapshot_state"] == "unsupported_schema"

    def test_static_head_byte_equivalence_across_snapshot_states(self):
        static = ["st/one", "st/two", "st/three"]
        for kwargs in (
            {"snapshot": None, "snapshot_state": "missing"},
            {"snapshot": {"bad": 1}, "snapshot_state": "ok"},
            {"snapshot": _snap([], generated_at=0), "snapshot_state": "ok"},
            {"snapshot": _snap([_cand("vendor/m1")]), "snapshot_state": "ok"},
        ):
            for mode in ("off", "shadow"):
                res = dc.resolve_execution_candidates(
                    static, provider=PROVIDER, cfg=_cfg(mode=mode), **kwargs
                )
                assert res["candidates"] == static

    def test_total_function_never_raises_on_bad_input(self):
        res = dc.resolve_execution_candidates(
            None,
            provider=PROVIDER,
            cfg="garbage",
            snapshot=42,
            snapshot_state="ok",
        )
        assert res["candidates"] == []
        res = dc.resolve_execution_candidates(
            ["a"],
            provider=None,
            cfg=_cfg(mode="append"),
            snapshot=_snap([]),
            snapshot_state="ok",
        )
        assert res["candidates"] == ["a"]

    def test_missing_provider_entry_yields_empty_tail(self):
        snap = _snap([_cand("vendor/m1")], provider="other-provider")
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a"]
        assert res["snapshot_state"] == "ok"


class TestResolveDedupeOrderingCaps:
    def test_dedupes_case_whitespace_and_prefix(self):
        snap = _snap(
            [
                _cand(" vendor/M1:free ", state="active"),
                _cand("MODELS/vendor/m1:free", state="active"),
                _cand("vendor/m2", state="active"),
            ]
        )
        res = dc.resolve_execution_candidates(
            ["static/a", "Vendor/M1:FREE"],
            provider=PROVIDER,
            task_class="bugfix",
            capability_mode="edit",
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a", "Vendor/M1:FREE", "vendor/m2"]
        assert "vendor/m1:free" in res["excluded_reasons"]

    def test_active_before_eligible_then_score_then_name(self):
        snap = _snap(
            [
                _cand("vendor/z-eligible", state="eligible", rank=0.99),
                _cand("vendor/b-active", state="active", rank=0.5),
                _cand("vendor/a-active", state="active", rank=0.5),
                _cand("vendor/c-active", state="active", rank=0.9),
            ]
        )
        res = dc.resolve_execution_candidates(
            [],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(
                mode="append",
                dyn_extra={
                    "max_per_provider": 10,
                    "max_total_per_task": 10,
                    "task_classes": {"bugfix": {"enabled": True, "allow_unproven": True}},
                },
            ),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == [
            "vendor/c-active",
            "vendor/a-active",
            "vendor/b-active",
            "vendor/z-eligible",
        ]

    def test_max_per_provider_cap(self):
        snap = _snap([_cand(f"vendor/m{i}", rank=0.9 - i * 0.1) for i in range(4)])
        res = dc.resolve_execution_candidates(
            [],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(mode="append", dyn_extra={"max_per_provider": 2, "max_total_per_task": 10}),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert len(res["dynamic_candidates"]) == 2

    def test_remaining_task_cap(self):
        snap = _snap([_cand(f"vendor/m{i}", rank=0.9 - i * 0.1) for i in range(4)])
        res = dc.resolve_execution_candidates(
            [],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(mode="append", dyn_extra={"max_per_provider": 3}),
            snapshot=snap,
            snapshot_state="ok",
            remaining_task_cap=1,
        )
        assert len(res["dynamic_candidates"]) == 1
        res = dc.resolve_execution_candidates(
            [],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(mode="append", dyn_extra={"max_per_provider": 3}),
            snapshot=snap,
            snapshot_state="ok",
            remaining_task_cap=0,
        )
        assert res["dynamic_candidates"] == []

    def test_max_total_per_task_default_cap(self):
        snap = _snap([_cand(f"vendor/m{i}", rank=0.9 - i * 0.1) for i in range(5)])
        res = dc.resolve_execution_candidates(
            [],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(mode="append", dyn_extra={"max_per_provider": 10, "max_total_per_task": 2}),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert len(res["dynamic_candidates"]) == 2


class TestResolvePolicyMatrix:
    def _resolve(self, candidates, **kw):
        snap = kw.pop("snapshot", None) or _snap(candidates)
        cfg = kw.pop("cfg", None) or _cfg(mode="append")
        kw.setdefault("task_class", "bugfix")
        kw.setdefault("capability_mode", "edit")
        return dc.resolve_execution_candidates(
            kw.pop("static", ["static/a"]),
            provider=PROVIDER,
            snapshot=snap,
            snapshot_state="ok",
            cfg=cfg,
            **kw,
        )

    def test_capability_mode_disabled(self):
        for bad_mode in ("destructive", "full", "nonsense"):
            res = self._resolve([_cand("vendor/m1")], capability_mode=bad_mode)
            assert res["candidates"] == ["static/a"]
            assert res["dynamic_candidates"] == []
            assert "capability_mode_disabled" in res["excluded_reasons"]["vendor/m1"]

    def test_capability_mode_enabled(self):
        for mode in ("edit", "readonly", "EDIT"):
            res = self._resolve([_cand("vendor/m1")], capability_mode=mode)
            assert res["candidates"] == ["static/a", "vendor/m1"]

    def test_task_class_disabled(self):
        for tc in ("general", "feature", "infra", "refactor"):
            res = self._resolve([_cand("vendor/m1")], task_class=tc)
            assert res["dynamic_candidates"] == []
            assert "task_class_disabled" in res["excluded_reasons"]["vendor/m1"]

    def test_active_requires_task_class_earned(self):
        res = self._resolve(
            [_cand("vendor/m1", state="active", task_classes=["analysis"])],
            task_class="bugfix",
        )
        assert res["dynamic_candidates"] == []
        assert "task_class_not_active" in res["excluded_reasons"]["vendor/m1"]

    def test_eligible_requires_allow_unproven(self):
        res = self._resolve(
            [_cand("vendor/m1", state="eligible", task_classes=[])],
            task_class="bugfix",
        )
        assert res["dynamic_candidates"] == []
        assert "unproven" in res["excluded_reasons"]["vendor/m1"]

    def test_eligible_admitted_when_unproven_allowed(self):
        res = self._resolve(
            [_cand("vendor/m1", state="eligible", task_classes=[])],
            task_class="analysis",
        )
        assert res["dynamic_candidates"] == ["vendor/m1"]

    def test_active_for_other_class_probes_when_unproven_allowed(self):
        res = self._resolve(
            [_cand("vendor/m1", state="active", task_classes=["bugfix"])],
            task_class="analysis",
        )
        # active-for-bugfix is unproven for analysis: admitted at eligible rank
        assert res["dynamic_candidates"] == ["vendor/m1"]

    def test_probe_ranks_below_class_active(self):
        cands = [
            _cand("vendor/probe", state="active", rank=0.99, task_classes=["bugfix"]),
            _cand("vendor/earned", state="active", rank=0.1, task_classes=["analysis"]),
        ]
        res = self._resolve(cands, task_class="analysis")
        assert res["dynamic_candidates"] == ["vendor/earned", "vendor/probe"]

    def test_observed_and_quarantined_excluded(self):
        res = self._resolve(
            [
                _cand("vendor/obs", state="observed"),
                _cand("vendor/quar", state="quarantined", task_classes=["bugfix"]),
            ],
            task_class="bugfix",
        )
        assert res["dynamic_candidates"] == []
        assert "gates_failed" in res["excluded_reasons"]["vendor/obs"]
        assert "quarantined" in res["excluded_reasons"]["vendor/quar"]

    def test_live_allowlist_recheck(self):
        res = self._resolve(
            [_cand("vendor/m1")],
            cfg=_cfg(mode="append", allow=["other/*"]),
        )
        assert res["dynamic_candidates"] == []
        assert "allowlist_mismatch" in res["excluded_reasons"]["vendor/m1"]

    def test_live_deny_models_recheck(self):
        res = self._resolve(
            [_cand("vendor/m1")],
            cfg=_cfg(mode="append", deny=["vendor/m1"]),
        )
        assert res["dynamic_candidates"] == []
        assert "provider_deny_models" in res["excluded_reasons"]["vendor/m1"]

    def test_live_denylist_fn_wins(self):
        res = self._resolve([_cand("vendor/m1")], denylist_fn=lambda m: "m1" in m)
        assert res["dynamic_candidates"] == []
        assert "denylisted" in res["excluded_reasons"]["vendor/m1"]

    def test_live_decisions_models_recheck(self):
        res = self._resolve(
            [_cand("vendor/d-1")],
            cfg=_cfg(mode="append", provider_cfg={"decisions_models": ["vendor/d-*"]}),
        )
        assert res["dynamic_candidates"] == []
        assert "decisions_only" in res["excluded_reasons"]["vendor/d-1"]

    def test_stored_tool_gate_enforced(self):
        res = self._resolve([_cand("vendor/m1", gates=_gates(tools=False))])
        assert res["dynamic_candidates"] == []
        assert "tools_unsupported" in res["excluded_reasons"]["vendor/m1"]

    def test_zdr_required_blocks_free(self):
        res = self._resolve(
            [_cand("vendor/m1:free", gates=_gates(zdr="not_supported"))], zdr_required=True
        )
        assert res["dynamic_candidates"] == []
        assert "zdr_required" in res["excluded_reasons"]["vendor/m1:free"]

    def test_zdr_required_allows_supported(self):
        res = self._resolve(
            [
                _cand(
                    "vendor/m1",
                    gates=_gates(zdr="supported", price="paid", prompt=1.0, completion=1.0),
                )
            ],
            task_class="bugfix",
            zdr_required=True,
        )
        # bugfix defaults to allow_paid False, so paid still excluded
        assert "paid_not_allowed" in res["excluded_reasons"]["vendor/m1"]
        res2 = self._resolve(
            [_cand("vendor/m1", gates=_gates(zdr="supported"))],
            zdr_required=True,
        )
        assert res2["dynamic_candidates"] == ["vendor/m1"]

    def test_zdr_not_required_allows_free(self):
        res = self._resolve([_cand("vendor/m1:free")], zdr_required=False)
        assert res["dynamic_candidates"] == ["vendor/m1:free"]

    def test_allow_paid_false_blocks_paid(self):
        res = self._resolve(
            [_cand("vendor/m1", gates=_gates(price="paid", prompt=0.5, completion=1.0))],
            task_class="bugfix",
        )
        assert res["dynamic_candidates"] == []
        assert "paid_not_allowed" in res["excluded_reasons"]["vendor/m1"]

    def test_allow_paid_true_admits_paid(self):
        cfg = _cfg(
            mode="append",
            dyn_extra={"task_classes": {"bugfix": {"enabled": True, "allow_paid": True}}},
        )
        res = self._resolve(
            [_cand("vendor/m1", gates=_gates(price="paid", prompt=0.5, completion=1.0))],
            task_class="bugfix",
            cfg=cfg,
        )
        assert res["dynamic_candidates"] == ["vendor/m1"]

    def test_price_cap_excludes(self):
        cfg = _cfg(
            mode="append",
            dyn_extra={
                "task_classes": {
                    "bugfix": {
                        "enabled": True,
                        "allow_paid": True,
                        "max_price_per_m": 0.5,
                    }
                }
            },
        )
        res = self._resolve(
            [_cand("vendor/m1", gates=_gates(price="paid", prompt=0.2, completion=0.9))],
            task_class="bugfix",
            cfg=cfg,
        )
        assert res["dynamic_candidates"] == []
        assert "price_cap" in res["excluded_reasons"]["vendor/m1"]

    def test_min_context_length_enforced(self):
        res = self._resolve(
            [_cand("vendor/m1", gates=_gates(ctx=16384))],
            task_class="bugfix",  # needs 65536
        )
        assert res["dynamic_candidates"] == []
        assert "context_below_min" in res["excluded_reasons"]["vendor/m1"]

    def test_context_at_threshold_passes(self):
        res = self._resolve(
            [_cand("vendor/m1", gates=_gates(ctx=65536))],
            task_class="bugfix",
        )
        assert res["dynamic_candidates"] == ["vendor/m1"]

    def test_unknown_task_class_falls_back_general_disabled(self):
        res = self._resolve([_cand("vendor/m1")], task_class="nonexistent-class")
        assert res["dynamic_candidates"] == []
        assert "task_class_disabled" in res["excluded_reasons"]["vendor/m1"]


class TestNoNetworkProof:
    def test_resolution_under_blocked_sockets(self):
        snap = _snap([_cand("vendor/m1")])
        with pytest.raises(RuntimeError):
            socket.socket().connect(("example.com", 443))
        res = dc.resolve_execution_candidates(
            ["static/a"],
            provider=PROVIDER,
            task_class="bugfix",
            cfg=_cfg(mode="append"),
            snapshot=snap,
            snapshot_state="ok",
        )
        assert res["candidates"] == ["static/a", "vendor/m1"]

    def test_module_exposes_no_network_imports(self):
        import botex.providers.dynamic_candidates as mod

        assert not hasattr(mod, "requests")
        assert not hasattr(mod, "urllib")
        src_names = [n for n in dir(mod) if "http" in n.lower() or "url" in n.lower()]
        assert src_names == []

# SPDX-License-Identifier: Apache-2.0

# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)

"""Focused tests for Plan 064 mode normalization, harness argv, and paid ceilings."""

from __future__ import annotations


import sys

from pathlib import Path


import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import botex.edge.adapters as adapters_mod

from botex.edge.adapters import (
    build_harness_argv,
    normalize_mode_request,
    paid_ceiling_violations,
    translate_work_order,
)


@pytest.fixture(autouse=True)
def deterministic_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:

    free = {"openrouter-free", "nvidia", "google", "cloudflare", "groq", "ollama"}

    paid = {"openrouter", "openai", "anthropic"}

    def fake_resolve(sub_provider: str):

        clean = str(sub_provider or "").strip().lower()

        if clean in free or clean in paid:
            return clean, None

        return None, {"ok": False, "status": "BLOCKED", "reason": "unknown_provider"}

    monkeypatch.setattr(adapters_mod, "_botex_dispatch_provider", fake_resolve)


def test_normalize_mode_request_defaults_and_fail_closed() -> None:

    assert normalize_mode_request(None) == ("readonly", None)

    assert normalize_mode_request("", default="edit") == ("edit", None)

    assert normalize_mode_request("  PLAN  ", default="edit") == ("plan", None)

    mode, err = normalize_mode_request("not-a-mode", default="edit")

    assert mode == ""

    assert err is not None

    assert err["ok"] is False

    assert err["status"] == "BLOCKED"

    assert err["reason"] == "unmapped_or_widened_mode"

    assert err["mode_request"] == "not-a-mode"

    assert err["detail"] == "mode_request_not_in_restrictiveness_rank"

    mode, err = normalize_mode_request(None, default="also-invalid")

    assert mode == ""

    assert err is not None

    assert err["mode_request"] == "also-invalid"


def test_translate_work_order_mode_default_override_and_invalid_block() -> None:

    order = {"order_id": "ord-mode", "goal": "inspect the repository", "epoch": 0}

    readonly_spec = translate_work_order(order, "devin", mode_default="readonly")

    assert readonly_spec["ok"] is True

    assert readonly_spec["mode_request"] == "readonly"

    assert readonly_spec["mode"] == "readonly"

    default_edit_spec = translate_work_order(order, "devin")

    assert default_edit_spec["ok"] is True

    assert default_edit_spec["mode_request"] == "edit"

    assert default_edit_spec["mode"] == "edit"

    invalid = dict(order)

    invalid["mode_request"] = "not-a-mode"

    blocked = translate_work_order(invalid, "devin", mode_default="readonly")

    assert blocked["ok"] is False

    assert blocked["status"] == "BLOCKED"

    assert blocked["reason"] == "unmapped_or_widened_mode"

    assert blocked["harness"] == "devin"

    assert blocked["mode_request"] == "not-a-mode"


def test_build_harness_argv_botex_forwards_positional_prompt_mode_and_provider() -> None:

    spec = {
        "harness": "botex",
        "prompt": "implement the adapter slice",
        "mode": "edit",
        "provider": "openrouter-free",
    }

    assert build_harness_argv(spec, cli="botex") == [
        "botex",
        "run",
        "implement the adapter slice",
        "--mode",
        "edit",
        "--provider",
        "openrouter-free",
    ]

    without_provider = dict(spec)

    without_provider.pop("provider")

    argv = build_harness_argv(without_provider, cli="botex")

    assert "--provider" not in argv


def test_build_harness_argv_zcode_uses_verified_headless_flags() -> None:

    spec = {"harness": "zcode", "prompt": "review only", "mode": "plan"}

    assert build_harness_argv(spec, cli="zcode") == [
        "zcode",
        "--prompt",
        "review only",
        "--mode",
        "plan",
        "--json",
        "--no-color",
    ]


def test_build_harness_argv_devin_uses_stdout_fallback_with_permission_mapping() -> None:

    spec = {
        "harness": "devin",
        "prompt": "edit scoped files",
        "mode": "edit",
        "transport": "acp",
        "transport_fallback": "stdout",
        "acp": {"protocol": "agent-client-protocol", "framing": "json-rpc/stdio"},
    }

    assert build_harness_argv(spec, cli="devin") == [
        "devin",
        "--process-model",
        "in-process",
        "--print",
        "--permission-mode",
        "accept-edits",
        "--",
        "edit scoped files",
    ]

    readonly_spec = dict(spec)

    readonly_spec["mode"] = "readonly"

    argv = build_harness_argv(readonly_spec, cli="devin")

    assert argv[argv.index("--permission-mode") + 1] == "auto"

    assert "acp" not in argv[1:]

    assert readonly_spec["prompt"] in argv


def test_build_harness_argv_rejects_incomplete_or_unsupported_specs() -> None:

    with pytest.raises(ValueError, match="missing_prompt"):
        build_harness_argv({"harness": "botex", "mode": "edit"}, cli="botex")

    with pytest.raises(ValueError, match="missing_cli"):
        build_harness_argv({"harness": "botex", "prompt": "x", "mode": "edit"}, cli="")

    with pytest.raises(ValueError, match="unsupported_harness"):
        build_harness_argv({"harness": "antigravity", "prompt": "x", "mode": "edit"}, cli="x")

    with pytest.raises(ValueError, match="unsupported_mode"):
        build_harness_argv({"harness": "zcode", "prompt": "x", "mode": "full"}, cli="zcode")


def test_paid_ceiling_violations_top_level_body_flags_quotas_and_provider() -> None:

    order = {"allow_expensive": True, "acu": 0}

    body = {
        "paid": True,
        "credits": 0,
        "budget_usd": 0,
        "max_cost_usd": 0,
        "provider": "openrouter",
    }

    assert paid_ceiling_violations(order, body) == [
        "paid_key.allow_expensive",
        "paid_key.paid",
        "paid_key.acu",
        "paid_key.credits",
        "paid_key.budget_usd",
        "paid_key.max_cost_usd",
        "paid_key.provider",
    ]


def test_paid_ceiling_violations_target_harness_resolution() -> None:

    assert paid_ceiling_violations(
        {"target_harness": "botex:openrouter"}, {"harness": "botex:openrouter-free"}
    ) == ["target_harness.paid_provider"]

    assert paid_ceiling_violations({}, {"order": {"harness": "botex:openrouter"}}) == [
        "target_harness.paid_provider"
    ]

    assert (
        paid_ceiling_violations(
            {"target_harness": "botex:openrouter-free", "provider": "nvidia"},
            {"harness": "botex:ollama"},
        )
        == []
    )


def test_paid_ceiling_violations_handoff_policy_vectors() -> None:

    order = {
        "handoff_policy": {
            "max_cost_usd": 5,
            "acu": 0,
            "credits": 1,
            "budget_usd": 0,
            "allow_paid": True,
            "allowed_providers": ["nvidia", "openrouter"],
        }
    }

    assert paid_ceiling_violations(order, {}) == [
        "handoff_policy.max_cost_usd",
        "handoff_policy.acu",
        "handoff_policy.credits",
        "handoff_policy.budget_usd",
        "handoff_policy.allow_paid",
        "handoff_policy.provider_allowlist",
    ]

    paid_allowlist = {"handoff_policy": {"paid_allowlist": ["openrouter/some-model"]}}

    assert paid_ceiling_violations(paid_allowlist, {}) == ["handoff_policy.provider_allowlist"]


def test_paid_ceiling_violations_free_zero_ceiling_payload_is_clean() -> None:

    order = {
        "provider": "google",
        "target_harness": "botex:openrouter-free",
        "handoff_policy": {
            "max_cost_usd": 0,
            "allowed_providers": ["nvidia", "ollama"],
        },
    }

    assert paid_ceiling_violations(order, {}) == []

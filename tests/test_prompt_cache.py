# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for botex.providers.prompt_cache (contract v1.0).

Covers the six contract areas: strip+collapse for non-capable providers,
head+tail breakpoints with fold-skip, copy-safety (input never mutated),
map_usage per-provider shapes with null-vs-zero semantics, resolve_policy
ordering (free-first / override / model-aware min-prefix), and the one-way
disable helper (3-miss trigger, cold-start exempt). No live network calls.
"""

from __future__ import annotations

import copy

import pytest

from botex.providers.prompt_cache import (
    CACHE_MISS_STREAK_DISABLE,
    CachePolicy,
    apply_prompt_cache,
    map_usage,
    resolve_policy,
    should_disable_cache,
)

# Big enough to clear every min-prefix gate (approx = chars/4 * 1.3).
_BIG_TEXT = "x" * 20000


def _enabled_ephemeral(min_prefix: int = 1024) -> CachePolicy:
    return CachePolicy(enabled=True, mode="anthropic_ephemeral", min_prefix_tokens=min_prefix)


def _disabled(mode: str = "none") -> CachePolicy:
    return CachePolicy(enabled=False, mode=mode, min_prefix_tokens=1024)


# ---------------------------------------------------------------------------
# 1. strip + collapse for non-capable providers
# ---------------------------------------------------------------------------


def test_strip_and_collapse_single_text_part_to_str():
    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        },
        {"role": "user", "content": "hello"},
    ]
    out, stats = apply_prompt_cache(messages, _disabled())
    assert out[0]["content"] == "sys"  # one-element text-part list -> str
    assert "cache_control" not in out[0]
    assert stats["stripped"] is True
    assert stats["collapsed"] is True
    assert stats["breakpoints"] == 0


def test_strip_keeps_multi_part_lists_but_removes_markers():
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "c"}],
            "cache_control": {"type": "ephemeral"},  # message-level marker
        },
    ]
    out, stats = apply_prompt_cache(messages, _disabled())
    assert isinstance(out[0]["content"], list) and len(out[0]["content"]) == 2
    assert all("cache_control" not in p for p in out[0]["content"])
    assert "cache_control" not in out[1]
    assert out[1]["content"] == "c"  # still collapsed (single text part)
    assert stats["stripped"] is True
    assert stats["collapsed"] is True


def test_disabled_stats_mark_strip_path_even_without_markers():
    out, stats = apply_prompt_cache([{"role": "user", "content": "hi"}], _disabled())
    assert stats["stripped"] is True
    assert stats["collapsed"] is False
    assert out == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# 2. head+tail breakpoints, <=4, skip tail-stamp on fold
# ---------------------------------------------------------------------------


def _tool_loop_messages() -> list:
    return [
        {"role": "system", "content": _BIG_TEXT},
        {"role": "user", "content": "task " + _BIG_TEXT},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "data"},
        {"role": "assistant", "content": "next step " + _BIG_TEXT},
        {"role": "tool", "tool_call_id": "c2", "content": "more"},
    ]


def test_ephemeral_head_and_tail_breakpoints():
    out, stats = apply_prompt_cache(_tool_loop_messages(), _enabled_ephemeral())
    sys_parts = out[0]["content"]
    assert isinstance(sys_parts, list)
    assert sys_parts[-1]["cache_control"] == {"type": "ephemeral"}
    assert sys_parts[-1]["text"] == _BIG_TEXT
    # Tail lands on the LAST user/assistant (index 4), not the trailing
    # tool message — tool messages are fold-unstable boundaries.
    tail = out[4]
    assert tail["role"] == "assistant"
    assert tail["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in str(m) or m is out[4] for m in out[1:])
    assert stats["breakpoints"] == 2
    assert stats["tail_stamped"] is True
    assert stats["skipped_fold"] is False
    assert stats["breakpoints"] <= 4


def test_ephemeral_skip_tail_stamp_on_fold():
    out, stats = apply_prompt_cache(_tool_loop_messages(), _enabled_ephemeral(), fold_occurred=True)
    assert stats["skipped_fold"] is True
    assert stats["tail_stamped"] is False
    assert stats["breakpoints"] == 1  # head only
    assert out[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # No other message carries a marker.
    for m in out[1:]:
        assert "cache_control" not in m
        content = m.get("content")
        if isinstance(content, list):
            assert all("cache_control" not in p for p in content)


def test_ephemeral_min_prefix_gate_blocks_stamping():
    short = [
        {"role": "system", "content": "tiny"},
        {"role": "user", "content": "hi"},
    ]
    out, stats = apply_prompt_cache(short, _enabled_ephemeral(min_prefix=1024))
    assert stats["breakpoints"] == 0
    assert stats["tail_stamped"] is False
    assert stats["approx_prefix_tokens"] < 1024
    assert "cache_control" not in str(out)


def test_ephemeral_respects_max_breakpoints_policy():
    policy = CachePolicy(
        enabled=True, mode="anthropic_ephemeral", min_prefix_tokens=1, max_breakpoints=1
    )
    out, stats = apply_prompt_cache(_tool_loop_messages(), policy)
    assert stats["breakpoints"] == 1  # head only; tail blocked by cap
    assert stats["tail_stamped"] is False


def test_implicit_mode_semantically_unchanged_copy():
    messages = _tool_loop_messages()
    policy = CachePolicy(enabled=True, mode="implicit", min_prefix_tokens=2048)
    out, stats = apply_prompt_cache(messages, policy)
    assert out == messages
    assert out is not messages
    assert stats["implicit"] is True
    assert stats["breakpoints"] == 0


def test_ephemeral_idempotent_on_restamp():
    msgs = _tool_loop_messages()
    once, _ = apply_prompt_cache(msgs, _enabled_ephemeral())
    twice, stats = apply_prompt_cache(once, _enabled_ephemeral())
    assert stats["breakpoints"] == 2
    # Exactly two markers total after re-application.
    markers = str(twice).count("'cache_control'")
    assert markers == 2


# ---------------------------------------------------------------------------
# 3. copy-safety: input never mutated (deep compare)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        _disabled(),
        _enabled_ephemeral(),
        CachePolicy(enabled=True, mode="implicit", min_prefix_tokens=1),
    ],
)
def test_input_never_mutated(policy):
    messages = _tool_loop_messages()
    messages[0]["content"] = [  # pre-existing marker must survive on input
        {"type": "text", "text": _BIG_TEXT, "cache_control": {"type": "ephemeral"}}
    ]
    snapshot = copy.deepcopy(messages)
    apply_prompt_cache(messages, policy)
    apply_prompt_cache(messages, policy, fold_occurred=True)
    assert messages == snapshot


# ---------------------------------------------------------------------------
# 4. map_usage: 4 provider shapes + null-vs-zero semantics
# ---------------------------------------------------------------------------


def test_map_usage_openrouter():
    usage = {
        "prompt_tokens": 5000,
        "cached_tokens": 4096,
        "cache_write_tokens": 512,
        "cost": 0.0123,
        "cost_details": {"upstream_inference_cost": 0.0111},
    }
    out = map_usage(usage, "openrouter")
    assert out["cache_read"] == 4096
    assert out["cache_write"] == 512
    assert out["authoritative_cost"] == 0.0123  # cost wins over cost_details


def test_map_usage_openrouter_fallbacks():
    # cost missing -> cost_details.upstream_inference_cost; anthropic
    # passthrough shapes as fallback.
    usage = {
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 90,
        "cost_details": {"upstream_inference_cost": 0.5},
    }
    out = map_usage(usage, "openrouter")
    assert out["cache_read"] == 700
    assert out["cache_write"] == 90
    assert out["authoritative_cost"] == 0.5


def test_map_usage_gemini():
    out = map_usage({"total_cached_tokens": 2048}, "google")
    assert out["cache_read"] == 2048
    assert out["cache_write"] is None  # implicit write — conceptually absent
    assert out["authoritative_cost"] is None
    out2 = map_usage({"total_cached_tokens": 100}, "aistudio")
    assert out2["cache_read"] == 100


def test_map_usage_nvidia_nim():
    usage = {"prompt_tokens_details": {"cached_tokens": 333}}
    out = map_usage(usage, "nvidia")
    assert out["cache_read"] == 333
    assert out["cache_write"] is None  # NIM never reports writes
    usage2 = {"input_tokens_details": {"cached_tokens": 41}}
    assert map_usage(usage2, "nvidia")["cache_read"] == 41


def test_map_usage_anthropic_direct():
    usage = {"cache_read_input_tokens": 1500, "cache_creation_input_tokens": 200}
    out = map_usage(usage, "anthropic")
    assert out["cache_read"] == 1500
    assert out["cache_write"] == 200


def test_map_usage_null_vs_zero_semantics():
    # Reported zero stays zero; absent field is None, never 0-fake.
    out = map_usage({"cached_tokens": 0}, "openrouter")
    assert out["cache_read"] == 0  # explicit zero — a real miss
    assert out["cache_write"] is None  # not reported
    assert map_usage({}, "openrouter") == {
        "cache_read": None,
        "cache_write": None,
        "authoritative_cost": None,
    }
    assert map_usage(None, "openrouter") == {
        "cache_read": None,
        "cache_write": None,
        "authoritative_cost": None,
    }


def test_map_usage_wrong_provider_shape_does_not_fabricate():
    # Anthropic-shaped usage read through the gemini family: gemini keys
    # absent -> read stays None even though anthropic fields are present.
    out = map_usage({"cache_read_input_tokens": 5}, "google")
    assert out["cache_read"] is None


# ---------------------------------------------------------------------------
# 5. resolve_policy: free-first, override, model-aware min_prefix
# ---------------------------------------------------------------------------


def test_resolve_policy_free_first_short_circuits():
    # is_free_model wins BEFORE any slug/provider matching — even a capable
    # anthropic/* slug on openrouter must come back disabled.
    p = resolve_policy("anthropic/claude-sonnet-4", "openrouter", is_free_model=True)
    assert p.enabled is False
    assert p.mode == "none"
    # Substring trap: provider "openrouter-free" is not "openrouter".
    p2 = resolve_policy("anthropic/claude-sonnet-4", "openrouter-free", is_free_model=True)
    assert p2.enabled is False and p2.mode == "none"


def test_resolve_policy_openrouter_anthropic_ephemeral():
    p = resolve_policy("anthropic/claude-sonnet-4", "openrouter", is_free_model=False)
    assert p.enabled is True
    assert p.mode == "anthropic_ephemeral"
    assert p.min_prefix_tokens == 1024
    p_ds = resolve_policy("deepseek/deepseek-chat", "openrouter", is_free_model=False)
    assert p_ds.mode == "anthropic_ephemeral"


def test_resolve_policy_substring_trap_defense():
    # A non-free provider literally named "openrouter-free" (inconsistent
    # caller input) still must NOT match the openrouter family — exact
    # provider-key matching only.
    p = resolve_policy("anthropic/claude-sonnet-4", "openrouter-free", is_free_model=False)
    assert p.mode != "anthropic_ephemeral"


def test_resolve_policy_implicit_providers():
    for prov, model in (
        ("google", "gemini-2.5-flash"),
        ("aistudio", "gemini-2.5-pro"),
        ("openai", "gpt-5"),
        ("nvidia", "meta/llama-3.1-70b"),
    ):
        p = resolve_policy(model, prov, is_free_model=False)
        assert p.mode == "implicit" and p.enabled is True, prov


def test_resolve_policy_none_for_uncapable():
    p = resolve_policy("qwen/qwen-2.5-coder", "openrouter", is_free_model=False)
    assert p.mode == "none" and p.enabled is False
    p2 = resolve_policy("some/model", "generic", is_free_model=False)
    assert p2.enabled is False


def test_resolve_policy_operator_override_wins():
    # Force-off a capable pair.
    p = resolve_policy("anthropic/claude-sonnet-4", "openrouter", False, operator_override=False)
    assert p.enabled is False
    # Force-on an uncapable pair -> promoted to implicit, never ephemeral
    # (operator enable must not inject anthropic flags on foreign upstreams).
    p2 = resolve_policy("qwen/qwen-2.5-coder", "openrouter", False, operator_override=True)
    assert p2.enabled is True and p2.mode == "implicit"
    # Override on a free model: free-tier is the cost gate, but the explicit
    # operator decision is final per contract.
    p3 = resolve_policy("m:free", "openrouter", True, operator_override=True)
    assert p3.enabled is True and p3.mode == "implicit"


def test_resolve_policy_model_aware_min_prefix():
    sonnet = resolve_policy("anthropic/claude-sonnet-4", "openrouter", False)
    assert sonnet.min_prefix_tokens == 1024
    haiku = resolve_policy("anthropic/claude-haiku-4.5", "openrouter", False)
    assert haiku.min_prefix_tokens == 2048
    g25 = resolve_policy("google/gemini-2.5-pro", "google", False)
    assert g25.min_prefix_tokens == 2048
    g3 = resolve_policy("google/gemini-3-pro", "google", False)
    assert g3.min_prefix_tokens == 4096
    g3b = resolve_policy("models/gemini-3.5-flash", "google", False)
    assert g3b.min_prefix_tokens == 4096
    default = resolve_policy("qwen/qwen-2.5-coder", "openrouter", False)
    assert default.min_prefix_tokens == 1024
    # Operator-supplied override beats the table.
    tuned = resolve_policy("anthropic/claude-sonnet-4", "openrouter", False, min_prefix=777)
    assert tuned.min_prefix_tokens == 777


# ---------------------------------------------------------------------------
# 6. one-way disable helper: 3-miss trigger, cold-start exempt
# ---------------------------------------------------------------------------


def test_should_disable_cache_three_miss_trigger():
    prefix = 5000
    assert should_disable_cache(0, prefix, 1024) is False
    assert should_disable_cache(1, prefix, 1024) is False
    assert should_disable_cache(2, prefix, 1024) is False
    assert should_disable_cache(3, prefix, 1024) is True
    assert should_disable_cache(7, prefix, 1024) is True  # stays latched


def test_should_disable_cache_prefix_gate():
    # Below the model-aware minimum the prefix was never cacheable —
    # misses there must not count toward the disable.
    assert should_disable_cache(99, 512, 1024) is False
    assert should_disable_cache(3, 2048, 2048) is True


def test_should_disable_cache_cold_start_exempt():
    # Turn 1 always misses (empty cache) and is excluded by the caller;
    # even counting it, a real turn-2/3 pair can only reach streak 2 —
    # the helper's >=3 threshold structurally cannot fire on cold start.
    assert CACHE_MISS_STREAK_DISABLE == 3
    assert should_disable_cache(2, 100000, 1024) is False

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for BoteX Memory Recall - Layer A LLM Synthesis Cascade (contract v1.0 §3)
==============================================================================
Covers:
1. Entry contract: empty context -> None; cfg param / lazy-config fallback.
2. Cascade order incl. hedged tier-1/tier-2 race (first VALID wins).
3. Tier-1 pipeline: enabled flag, prompt-size guard, L7 tags probe (tolerant
   tri-state), Semaphore(1) bounded FIFO queue_wait, keep_alive native chat.
4. Answer cache (hit, content/mtime invalidation, TTL via mock clock) and
   negative-tier TTL markers.
5. Post-validation: source id filtering, confidence coercion, NOT_FOUND.
6. tier_attempts observability records [{tier, status, ms}].

All network access and provider clients are mocked; conftest blocks live sockets.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.curator import recall_curator
from ops.curator.recall_curator import synthesize_recall

RECALL_ENV_VARS = [
    "OLLAMA_HOST",
    "BOTEX_RECALL_OLLAMA_MODEL",
    "NVIDIA_API_KEY",
    "NVAPI_KEY",
    "BOTEX_RECALL_NVIDIA_MODEL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "BOTEX_RECALL_GEMINI_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_FREE_KEY",
    "BOTEX_RECALL_OPENROUTER_MODEL",
]

TIER_FUNCS = {
    "local-ollama": "_try_local_ollama",
    "nvidia": "_try_nvidia",
    "gemini": "_try_gemini",
    "openrouter-free": "_try_openrouter_free",
}


@pytest.fixture(autouse=True)
def clean_recall_env(monkeypatch):
    """Hermetically isolate recall env vars and API keys from the dev environment."""
    for var in RECALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def reset_recall_state():
    """Reset module-level cache/negative marks between tests."""
    recall_curator._ANSWER_CACHE.clear()
    recall_curator._NEGATIVE_TIER_MARKS.clear()
    yield
    recall_curator._ANSWER_CACHE.clear()
    recall_curator._NEGATIVE_TIER_MARKS.clear()


def _nodes() -> list[dict]:
    return [
        {
            "id": "note_alpha",
            "title": "Alpha",
            "kind": "context",
            "scope": "local",
            "relation": "match",
            "content": "Alpha service uses bearer tokens.",
        },
        {
            "id": "note_beta",
            "title": "Beta",
            "kind": "lesson",
            "scope": "global",
            "relation": "link",
            "content": "Beta rotates secrets every 24h.",
        },
    ]


def _payload(**overrides) -> str:
    payload = {
        "answer": "Alpha uses bearer tokens.",
        "sources": ["note_alpha"],
        "confidence": "HIGH",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _tcfg(model: str = "qwen2.5:3b", timeout_s: float = 12.0, **extra) -> dict:
    cfg = {"model": model, "enabled": True, "timeout_s": timeout_s}
    cfg.update(extra)
    return cfg


def _patch_tiers(stack: ExitStack, **returns) -> dict:
    """Patch all four raw tier functions; each value is raw text, None, or an Exception.

    Also patches the L7 tags probe so tier-1 proceeds past the model check.
    """
    stack.enter_context(
        patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=True))
    )
    mocks = {}
    for tier, fname in TIER_FUNCS.items():
        ret = returns.get(tier)
        mock = (
            AsyncMock(side_effect=ret)
            if isinstance(ret, BaseException)
            else AsyncMock(return_value=ret)
        )
        stack.enter_context(patch.object(recall_curator, fname, mock))
        mocks[tier] = mock
    return mocks


def _fake_completion(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


# ---------------------------------------------------------------------------
# 1. Entry contract & cascade order
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_context_returns_none_without_tier_calls():
    """Empty context_nodes must return None without invoking any tier."""
    with ExitStack() as stack:
        mocks = _patch_tiers(stack)
        result = await synthesize_recall("what is alpha?", [])

    assert result is None
    for mock in mocks.values():
        assert mock.await_count == 0


@pytest.mark.anyio
async def test_cascade_first_tier_success_short_circuits():
    """A valid local-ollama answer wins; lower tiers are never invoked."""
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, **{"local-ollama": _payload()})
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["tier_used"] == "local-ollama"
    assert result["confidence"] == "HIGH"
    assert result["sources"] == ["note_alpha"]
    assert mocks["local-ollama"].await_count == 1
    assert mocks["nvidia"].await_count == 0
    assert mocks["gemini"].await_count == 0
    assert mocks["openrouter-free"].await_count == 0


@pytest.mark.anyio
async def test_cascade_order_and_fallthrough():
    """Tiers are tried in order; failures and exceptions fall through."""
    calls = []

    def _spy(name, ret):
        async def _fn(prompt, tcfg):
            calls.append(name)
            if isinstance(ret, BaseException):
                raise ret
            return ret

        return _fn

    spies = {
        "local-ollama": _spy("local-ollama", None),
        "nvidia": _spy("nvidia", RuntimeError("nvapi-SECRETKEY1234567890 boom")),
        "gemini": _spy("gemini", "not json at all"),
        "openrouter-free": _spy("openrouter-free", _payload(confidence="MEDIUM")),
    }

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=True))
        )
        for tier, fn in spies.items():
            stack.enter_context(patch.object(recall_curator, TIER_FUNCS[tier], fn))
        result = await synthesize_recall("what is alpha?", _nodes())

    assert calls == ["local-ollama", "nvidia", "gemini", "openrouter-free"]
    assert result is not None
    assert result["tier_used"] == "openrouter-free"
    assert result["confidence"] == "MEDIUM"


@pytest.mark.anyio
async def test_all_tiers_fail_returns_failure_dict():
    """All tiers unavailable/invalid -> failure dict (answer=None) for fallback."""
    with ExitStack() as stack:
        _patch_tiers(
            stack,
            **{
                "local-ollama": None,
                "nvidia": None,
                "gemini": "definitely { not json",
                "openrouter-free": json.dumps({"answer": 42, "sources": []}),
            },
        )
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["answer"] is None
    assert result["tier_used"] is None
    assert {a["tier"] for a in result["tier_attempts"]} == set(TIER_FUNCS)


# ---------------------------------------------------------------------------
# 2. Post-validation rules
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sources_filtered_to_known_ids():
    """Hallucinated source ids not present in context_nodes are dropped."""
    raw = _payload(sources=["note_alpha", "hallucinated_id", "note_alpha", 42])
    with ExitStack() as stack:
        _patch_tiers(stack, **{"local-ollama": raw})
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["sources"] == ["note_alpha"]


@pytest.mark.anyio
async def test_not_found_forces_empty_sources():
    """NOT_FOUND confidence must force sources=[]."""
    raw = _payload(
        answer="No relevant notes found.",
        sources=["note_alpha"],
        confidence="NOT_FOUND",
    )
    with ExitStack() as stack:
        _patch_tiers(stack, nvidia=raw)
        result = await synthesize_recall("unanswerable?", _nodes())

    assert result is not None
    assert result["tier_used"] == "nvidia"
    assert result["confidence"] == "NOT_FOUND"
    assert result["sources"] == []


@pytest.mark.anyio
async def test_invalid_confidence_coerced_to_low():
    """Unrecognized confidence values are coerced to LOW."""
    with ExitStack() as stack:
        _patch_tiers(stack, gemini=_payload(confidence="VERY_SURE"))
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["tier_used"] == "gemini"
    assert result["confidence"] == "LOW"


@pytest.mark.anyio
async def test_markdown_fenced_json_parsed():
    """LLM output wrapped in ```json fences still parses and validates."""
    raw = f"Here is the answer:\n```json\n{_payload()}\n```\nDone."
    with ExitStack() as stack:
        _patch_tiers(stack, **{"local-ollama": raw})
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["tier_used"] == "local-ollama"
    assert result["answer"] == "Alpha uses bearer tokens."


@pytest.mark.anyio
async def test_non_dict_or_bad_payload_falls_through():
    """Payloads failing validation (non-dict, non-str answer) trigger next tier."""
    with ExitStack() as stack:
        _patch_tiers(
            stack,
            **{
                "local-ollama": json.dumps(["not", "a", "dict"]),
                "nvidia": json.dumps({"answer": "", "confidence": "HIGH"}),
                "gemini": _payload(confidence="LOW"),
            },
        )
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result is not None
    assert result["tier_used"] == "gemini"


# ---------------------------------------------------------------------------
# 3. Tier-1 pipeline: probe, guard, semaphore, keep_alive
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tier1_prompt_guard_skips_to_tier2():
    """Estimated prompt tokens > num_ctx_guard_tokens -> tier-1 skipped."""
    nodes = _nodes()
    # ~9k estimated tokens > 4000 guard; varied words survive secret masking
    nodes[0]["content"] = " ".join(
        f"fact-{i} service uses rotation policy {i % 7}" for i in range(1200)
    )
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, nvidia=_payload(confidence="MEDIUM"))
        result = await synthesize_recall("what is alpha?", nodes)

    assert result["tier_used"] == "nvidia"
    t1_attempt = next(a for a in result["tier_attempts"] if a["tier"] == "local-ollama")
    assert t1_attempt["status"] == "prompt_guard"
    assert mocks["local-ollama"].await_count == 0


@pytest.mark.anyio
async def test_tier1_queue_wait_timeout_escalates():
    """A held Semaphore(1) + queue_wait_s expiry escalates to tier-2."""
    sem = recall_curator._tier1_semaphore()
    await sem.acquire()
    try:
        with ExitStack() as stack:
            mocks = _patch_tiers(stack, nvidia=_payload())
            result = await synthesize_recall("what is alpha?", _nodes(), cfg={"queue_wait_s": 0.01})
    finally:
        sem.release()

    assert result["tier_used"] == "nvidia"
    t1_attempt = next(a for a in result["tier_attempts"] if a["tier"] == "local-ollama")
    assert t1_attempt["status"] == "queue_timeout"
    assert mocks["local-ollama"].await_count == 0


@pytest.mark.anyio
async def test_tier1_probe_model_absent_skips():
    """L7 tags probe confirming model absence skips the request and marks tier."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=False))
        )
        mocks = {}
        for tier, fname in TIER_FUNCS.items():
            ret = _payload() if tier == "nvidia" else None
            mock = AsyncMock(return_value=ret)
            stack.enter_context(patch.object(recall_curator, fname, mock))
            mocks[tier] = mock
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result["tier_used"] == "nvidia"
    t1_attempt = next(a for a in result["tier_attempts"] if a["tier"] == "local-ollama")
    assert t1_attempt["status"] == "model_absent"
    assert mocks["local-ollama"].await_count == 0


@pytest.mark.anyio
async def test_tier1_probe_inconclusive_falls_back_to_tcp():
    """Inconclusive tags probe (None) tolerates via L4 TCP probe."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=None))
        )
        stack.enter_context(
            patch.object(recall_curator, "_probe_tcp", MagicMock(return_value=False))
        )
        mocks = {}
        for tier, fname in TIER_FUNCS.items():
            mock = AsyncMock(return_value=_payload() if tier == "nvidia" else None)
            stack.enter_context(patch.object(recall_curator, fname, mock))
            mocks[tier] = mock
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result["tier_used"] == "nvidia"
    t1_attempt = next(a for a in result["tier_attempts"] if a["tier"] == "local-ollama")
    assert t1_attempt["status"] == "unavailable"


@pytest.mark.anyio
async def test_tier1_probe_inconclusive_tcp_ok_proceeds():
    """Tolerant probe: inconclusive L7 + reachable TCP -> request still attempted."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=None))
        )
        stack.enter_context(
            patch.object(recall_curator, "_probe_tcp", MagicMock(return_value=True))
        )
        mocks = {}
        for tier, fname in TIER_FUNCS.items():
            mock = AsyncMock(return_value=_payload() if tier == "local-ollama" else None)
            stack.enter_context(patch.object(recall_curator, fname, mock))
            mocks[tier] = mock
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result["tier_used"] == "local-ollama"
    assert mocks["local-ollama"].await_count == 1


@pytest.mark.anyio
async def test_tier1_disabled_in_cfg():
    """cfg ollama.enabled=false disables tier-1 entirely."""
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, nvidia=_payload())
        result = await synthesize_recall(
            "what is alpha?", _nodes(), cfg={"ollama": {"enabled": False}}
        )

    assert result["tier_used"] == "nvidia"
    t1_attempt = next(a for a in result["tier_attempts"] if a["tier"] == "local-ollama")
    assert t1_attempt["status"] == "disabled"
    assert mocks["local-ollama"].await_count == 0


# ---------------------------------------------------------------------------
# 4. Hedge race (tier-1 vs tier-2, first VALID wins)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hedge_tier2_wins_when_tier1_stalls():
    """After hedge_s of tier-1 silence, tier-2 runs in parallel and can win."""
    gate = asyncio.Event()

    async def _stall(prompt, tcfg):
        await gate.wait()
        return _payload()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=True))
        )
        m_t1 = AsyncMock(side_effect=_stall)
        stack.enter_context(patch.object(recall_curator, "_try_local_ollama", m_t1))
        m_t2 = AsyncMock(return_value=_payload(confidence="MEDIUM"))
        stack.enter_context(patch.object(recall_curator, "_try_nvidia", m_t2))
        result = await synthesize_recall("what is alpha?", _nodes(), cfg={"hedge_s": 0.05})

    assert result["tier_used"] == "nvidia"
    assert m_t1.await_count == 1
    assert m_t2.await_count == 1
    statuses = {a["tier"]: a["status"] for a in result["tier_attempts"]}
    assert statuses["nvidia"] == "ok"
    assert statuses["local-ollama"] == "cancelled"


@pytest.mark.anyio
async def test_hedge_first_valid_wins_over_fast_invalid():
    """A fast-invalid tier-2 must not beat a slower but VALID tier-1."""
    release_t1 = asyncio.Event()

    async def _slow_valid(prompt, tcfg):
        await release_t1.wait()
        return _payload()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=True))
        )
        m_t1 = AsyncMock(side_effect=_slow_valid)
        stack.enter_context(patch.object(recall_curator, "_try_local_ollama", m_t1))
        m_t2 = AsyncMock(return_value="garbage, not json")
        stack.enter_context(patch.object(recall_curator, "_try_nvidia", m_t2))

        async def _run():
            task = asyncio.ensure_future(
                synthesize_recall("what is alpha?", _nodes(), cfg={"hedge_s": 0.01})
            )
            await asyncio.sleep(0.05)  # let tier-2 complete invalid first
            release_t1.set()
            return await task

        result = await _run()

    assert result["tier_used"] == "local-ollama"
    statuses = {a["tier"]: a["status"] for a in result["tier_attempts"]}
    assert statuses["nvidia"] == "invalid"
    assert statuses["local-ollama"] == "ok"


@pytest.mark.anyio
async def test_hedge_both_invalid_falls_to_sequential_tiers():
    """When both hedged tiers fail, tier-3/4 run sequentially afterwards."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=True))
        )
        m_t1 = AsyncMock(return_value="not json")
        stack.enter_context(patch.object(recall_curator, "_try_local_ollama", m_t1))
        m_t2 = AsyncMock(return_value=None)
        stack.enter_context(patch.object(recall_curator, "_try_nvidia", m_t2))
        m_t3 = AsyncMock(return_value=_payload(confidence="LOW"))
        stack.enter_context(patch.object(recall_curator, "_try_gemini", m_t3))
        result = await synthesize_recall("what is alpha?", _nodes(), cfg={"hedge_s": 0.02})

    assert result["tier_used"] == "gemini"


# ---------------------------------------------------------------------------
# 5. Answer cache + negative-tier TTL (mock clocks)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_answer_cache_hit_and_content_invalidation():
    """Second identical query hits cache; node content change invalidates."""
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, **{"local-ollama": _payload()})
        first = await synthesize_recall("what is alpha?", _nodes())
        second = await synthesize_recall("what is alpha?", _nodes())

    assert first["tier_used"] == "local-ollama"
    assert second["cached"] is True
    assert second["tier_attempts"][0]["tier"] == "answer-cache"
    assert mocks["local-ollama"].await_count == 1

    changed = _nodes()
    changed[0]["content"] = "Alpha now uses mTLS instead of bearer tokens."
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, **{"local-ollama": _payload()})
        third = await synthesize_recall("what is alpha?", changed)
    assert "cached" not in third
    assert mocks["local-ollama"].await_count == 1


@pytest.mark.anyio
async def test_answer_cache_mtime_invalidation_and_ttl():
    """max(node_mtimes) is part of the key; entries expire after cache_ttl_s."""
    nodes = _nodes()
    nodes[0]["mtime"] = 100.0

    clock = {"t": 1000.0}
    with ExitStack() as stack:
        mocks = _patch_tiers(stack, **{"local-ollama": _payload()})
        stack.enter_context(patch.object(recall_curator, "_now", lambda: clock["t"]))
        await synthesize_recall("q?", nodes)
        hit = await synthesize_recall("q?", nodes)
        assert hit["cached"] is True
        assert mocks["local-ollama"].await_count == 1

        clock["t"] += 301.0  # past DEFAULT_CACHE_TTL_S=300
        miss = await synthesize_recall("q?", nodes)
        assert "cached" not in miss
        assert mocks["local-ollama"].await_count == 2

        clock["t"] += 1.0
        nodes[0]["mtime"] = 200.0  # bump mtime -> new key -> miss
        miss2 = await synthesize_recall("q?", nodes)
        assert "cached" not in miss2
        assert mocks["local-ollama"].await_count == 3


@pytest.mark.anyio
async def test_negative_tier_marker_skips_then_expires():
    """A tier infra failure marks it down; it is skipped until the TTL elapses."""
    clock = {"t": 500.0}
    with ExitStack() as stack:
        stack.enter_context(patch.object(recall_curator, "_now", lambda: clock["t"]))
        mocks = _patch_tiers(
            stack,
            **{
                "local-ollama": TimeoutError("ollama hung"),
                "nvidia": _payload(confidence="MEDIUM"),
            },
        )
        first = await synthesize_recall("what is alpha?", _nodes())
        assert first["tier_used"] == "nvidia"
        assert mocks["local-ollama"].await_count == 1
        assert mocks["local-ollama"].side_effect is not None

        # Second call: tier-1 skipped via negative marker (no new await).
        mocks["local-ollama"] = AsyncMock(return_value=_payload())
        stack.enter_context(
            patch.object(recall_curator, "_try_local_ollama", mocks["local-ollama"])
        )
        second = await synthesize_recall("what is beta?", _nodes())
        assert mocks["local-ollama"].await_count == 0
        t1_attempt = next(a for a in second["tier_attempts"] if a["tier"] == "local-ollama")
        assert t1_attempt["status"] == "neg_cached"

        # After negative TTL elapses the tier is retried.
        clock["t"] += recall_curator.DEFAULT_NEGATIVE_TIER_TTL_S + 1.0
        third = await synthesize_recall("what is gamma?", _nodes())
        assert mocks["local-ollama"].await_count == 1
        assert third["tier_used"] == "local-ollama"


# ---------------------------------------------------------------------------
# 6. Config resolution (cfg param + lazy fallback)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cfg_param_controls_models_and_lazy_fallback(monkeypatch):
    """cfg param supplies per-tier model; absent cfg -> lazy get_recall_config."""
    monkeypatch.setattr(
        recall_curator,
        "_get_recall_config_from_memory",
        lambda: {"nvidia": {"model": "nvidia/custom-model", "enabled": True}},
    )
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_fake_completion(_payload()))
    fake_provider = MagicMock()
    fake_provider.get_client.return_value = fake_client

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(recall_curator, "_probe_ollama_model", MagicMock(return_value=False))
        )
        stack.enter_context(
            patch("botex.providers.ProviderRegistry.get", return_value=fake_provider)
        )
        result = await synthesize_recall("what is alpha?", _nodes())

    assert result["tier_used"] == "nvidia"
    kwargs = fake_client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "nvidia/custom-model"


# ---------------------------------------------------------------------------
# 7. Tier request helpers (env gating + mocked provider clients)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tiers_skip_without_api_keys():
    """Cloud tiers return None immediately when their env keys are absent."""
    tcfg = _tcfg()
    assert await recall_curator._try_nvidia("p", tcfg) is None
    assert await recall_curator._try_gemini("p", tcfg) is None
    assert await recall_curator._try_openrouter_free("p", tcfg) is None


@pytest.mark.anyio
async def test_ollama_native_chat_sends_keep_alive():
    """Tier-1 native path POSTs /api/chat with keep_alive (model stays loaded)."""
    seen = {}

    def _fake_post(url, payload, timeout_s):
        seen["url"] = url
        seen["payload"] = payload
        return {"message": {"role": "assistant", "content": _payload()}}

    with patch.object(recall_curator, "_post_json", _fake_post):
        raw = await recall_curator._try_local_ollama("prompt", _tcfg())

    assert raw == _payload()
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["payload"]["model"] == "qwen2.5:3b"
    assert seen["payload"]["keep_alive"] == recall_curator.OLLAMA_KEEP_ALIVE
    assert seen["payload"]["stream"] is False


@pytest.mark.anyio
async def test_ollama_shim_fallback_when_native_fails():
    """Native /api/chat failure falls back to the /v1 shim (untested keep_alive)."""

    def _boom(url, payload, timeout_s):
        raise RuntimeError("native endpoint down")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_completion(_payload(confidence="LOW"))
    )

    with (
        patch.object(recall_curator, "_post_json", _boom),
        patch("openai.AsyncOpenAI", return_value=fake_client) as m_openai,
    ):
        raw = await recall_curator._try_local_ollama("prompt", _tcfg())

    assert raw == _payload(confidence="LOW")
    m_openai.assert_called_once()
    assert m_openai.call_args.kwargs["base_url"] == "http://localhost:11434/v1"
    kwargs = fake_client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "qwen2.5:3b"


@pytest.mark.anyio
async def test_nvidia_tier_uses_registry_and_disables_thinking(monkeypatch):
    """NVIDIA tier resolves via ProviderRegistry and sends thinking=False for nemotron-3."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_fake_completion(_payload()))
    fake_provider = MagicMock()
    fake_provider.get_client.return_value = fake_client

    with patch("botex.providers.ProviderRegistry.get", return_value=fake_provider) as m_get:
        raw = await recall_curator._try_nvidia(
            "prompt", _tcfg(model=recall_curator.DEFAULT_NVIDIA_MODEL)
        )

    assert raw == _payload()
    m_get.assert_called_once_with("nvidia")
    fake_provider.get_client.assert_called_once_with(api_key="nvapi-test-key")
    kwargs = fake_client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == recall_curator.DEFAULT_NVIDIA_MODEL
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"thinking": False}}
    assert kwargs["temperature"] == pytest.approx(0.1)


@pytest.mark.anyio
async def test_gemini_and_openrouter_key_gating(monkeypatch):
    """Gemini honors GEMINI_API_KEY; openrouter-free honors OPENROUTER_API_FREE_KEY."""
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_completion(_payload(confidence="MEDIUM"))
    )
    fake_provider = MagicMock()
    fake_provider.get_client.return_value = fake_client

    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    with patch("botex.providers.ProviderRegistry.get", return_value=fake_provider) as m_get:
        raw = await recall_curator._try_gemini(
            "prompt", _tcfg(model=recall_curator.DEFAULT_GEMINI_MODEL)
        )
    assert raw is not None
    m_get.assert_called_once_with("google")

    monkeypatch.setenv("OPENROUTER_API_FREE_KEY", "or-free-key")
    with patch("botex.providers.ProviderRegistry.get", return_value=fake_provider) as m_get:
        raw = await recall_curator._try_openrouter_free(
            "prompt", _tcfg(model=recall_curator.DEFAULT_OPENROUTER_MODEL)
        )
    assert raw is not None
    m_get.assert_called_once_with("openrouter-free")
    kwargs = fake_client.chat.completions.create.await_args.kwargs
    assert kwargs["model"].endswith(":free")


@pytest.mark.anyio
async def test_tier_exception_returns_none(monkeypatch):
    """A provider client raising mid-request returns None rather than propagating."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("HTTP 500 nvapi-secretshouldbemasked123456")
    )
    fake_provider = MagicMock()
    fake_provider.get_client.return_value = fake_client

    with patch("botex.providers.ProviderRegistry.get", return_value=fake_provider):
        raw = await recall_curator._try_nvidia(
            "prompt", _tcfg(model=recall_curator.DEFAULT_NVIDIA_MODEL)
        )

    assert raw is None


# ---------------------------------------------------------------------------
# 8. Probe helpers, prompt construction & misc
# ---------------------------------------------------------------------------


def test_probe_ollama_model_tri_state():
    """L7 tags probe: True installed / False confirmed-absent / None inconclusive."""
    with patch.object(
        recall_curator,
        "_get_json",
        return_value={"models": [{"name": "qwen2.5:3b"}, {"name": "bielik:latest"}]},
    ):
        assert recall_curator._probe_ollama_model("http://h:11434", "qwen2.5:3b", 0.8) is True
        assert recall_curator._probe_ollama_model("http://h:11434", "qwen2.5", 0.8) is False
        assert recall_curator._probe_ollama_model("http://h:11434", "llama3", 0.8) is False
    with patch.object(recall_curator, "_get_json", side_effect=TimeoutError("slow")):
        assert recall_curator._probe_ollama_model("http://h:11434", "qwen2.5:3b", 0.8) is None


def test_normalize_ollama_tag():
    assert recall_curator._normalize_ollama_tag("bielik") == "bielik:latest"
    assert recall_curator._normalize_ollama_tag("qwen2.5:3b") == "qwen2.5:3b"


def test_ollama_native_base_strips_v1_and_defaults(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert recall_curator._ollama_native_base() == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "http://10.0.0.5:11434/v1")
    assert recall_curator._ollama_native_base() == "http://10.0.0.5:11434"
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.6:11434")
    assert recall_curator._ollama_native_base() == "http://10.0.0.6:11434"


def test_build_recall_user_prompt_contains_ids_and_untrusted_notes():
    """Prompt embeds node metadata and wraps content in untrusted <note> tags."""
    from ops.curator.prompts import build_recall_user_prompt

    prompt = build_recall_user_prompt("query?", _nodes())
    assert "query?" in prompt
    assert "note_alpha" in prompt
    assert "note_beta" in prompt
    assert '<note id="note_alpha">' in prompt
    assert "Alpha service uses bearer tokens." in prompt


def test_extract_and_validate_helpers():
    """Direct unit checks for JSON extraction and payload validation edges."""
    assert recall_curator._extract_json_object("garbage") is None
    assert recall_curator._extract_json_object("[1,2,3]") is None
    assert recall_curator._extract_json_object(_payload())["confidence"] == "HIGH"

    valid = frozenset({"note_alpha"})
    out = recall_curator._validate_recall_payload(
        {"answer": "", "confidence": "not_found", "sources": ["note_alpha"]}, valid
    )
    assert out == {
        "answer": recall_curator.NOT_FOUND_ANSWER,
        "sources": [],
        "confidence": "NOT_FOUND",
    }
    assert recall_curator._validate_recall_payload({"answer": ["x"]}, valid) is None

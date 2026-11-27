# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Registration, config, and URL-resolution tests for Groq + Cloudflare Workers AI."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botex.providers.providers import (
    CloudflareWorkersAIProvider,
    GroqProvider,
    ProviderRegistry,
)
from botex.server.config import load_config, normalize_provider_name


def test_registry_registers_groq_and_cloudflare():
    assert isinstance(ProviderRegistry.get("groq"), GroqProvider)
    assert isinstance(ProviderRegistry.get("cloudflare"), CloudflareWorkersAIProvider)
    for alias in ("workers-ai", "workers_ai", "cf-workers-ai"):
        assert isinstance(ProviderRegistry.get(alias), CloudflareWorkersAIProvider), alias


def test_groq_provider_defaults():
    p = GroqProvider("groq", {})
    assert p.base_url == "https://api.groq.com/openai/v1"
    assert p.api_key_env == "GROQ_API_KEY"


def test_cloudflare_base_url_template_resolution(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    p = CloudflareWorkersAIProvider(
        "cloudflare",
        {"base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"},
    )
    assert p.base_url == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"


def test_cloudflare_account_id_config_wins(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "envacc")
    p = CloudflareWorkersAIProvider("cloudflare", {"account_id": "cfgacc"})
    assert "cfgacc" in p.base_url
    assert "envacc" not in p.base_url


def test_cloudflare_literal_base_url_passthrough(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    p = CloudflareWorkersAIProvider(
        "cloudflare",
        {"base_url": "https://api.cloudflare.com/client/v4/accounts/lit42/ai/v1"},
    )
    assert p.base_url.endswith("/accounts/lit42/ai/v1")


def test_cloudflare_missing_account_id_fails_fast(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    p = CloudflareWorkersAIProvider("cloudflare", {})
    with pytest.raises(ValueError, match="account id"):
        p.get_client(api_key="tok")


def test_cloudflare_get_client_injects_resolved_base_url(monkeypatch):
    """Regression: provider_cfg['base_url'] template must not leak verbatim."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc999")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    p = CloudflareWorkersAIProvider(
        "cloudflare",
        {"base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"},
    )
    p.get_client(api_key="tok", client_factory=FakeClient)
    assert "{account_id}" not in captured["base_url"]
    assert captured["base_url"].endswith("/accounts/acc999/ai/v1")


def test_cloudflare_api_key_env_fallbacks(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CF_API_TOKEN", "cf-tok")
    p = CloudflareWorkersAIProvider("cloudflare", {})
    assert p.resolve_api_key() == "cf-tok"


def test_cloudflare_fetch_catalog_native_fallback(monkeypatch):
    """Native /ai/models/search is queried first; /ai/v1/models only on failure."""
    calls = []

    class FakeResp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http error")

        def json(self):
            return self._payload

    class FakeClient:
        def get(self, url, headers=None, timeout=None):
            calls.append(url)
            if url.endswith("/ai/models/search"):
                return FakeResp(
                    {
                        "result": [
                            {"name": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
                            {"name": "@cf/qwen/qwen2.5-coder-32b-instruct"},
                            {"unexpected": "shape"},
                        ],
                        "success": True,
                    }
                )
            if url.endswith("/ai/v1/models"):
                return FakeResp({}, status=404)
            raise AssertionError(f"unexpected url {url}")

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    import botex.providers.http_pool as http_pool

    monkeypatch.setattr(http_pool, "get_sync_client", lambda: FakeClient())

    p = CloudflareWorkersAIProvider("cloudflare", {})
    models = p.fetch_catalog(api_key="tok")
    assert models == [
        {"id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"},
        {"id": "@cf/qwen/qwen2.5-coder-32b-instruct"},
    ]
    assert calls == [calls[0]]
    assert calls[0].endswith("/ai/models/search")


def test_config_registers_providers():
    cfg = load_config()
    providers = cfg.get("providers", {})
    assert "groq" in providers
    assert "cloudflare" in providers
    assert providers["groq"]["api_key_env"] == "GROQ_API_KEY"
    assert providers["cloudflare"]["api_key_env"] == "CLOUDFLARE_API_TOKEN"
    assert providers["groq"].get("free_tier") is True
    assert providers["cloudflare"].get("free_tier") is True
    assert providers["cloudflare"]["decisions_models"] == [
        "@cf/cloudflare/clef*",
        "@cf/cloudflare/clairic*",
    ]


def test_provider_aliases_normalize():
    cfg = load_config()
    providers = cfg["providers"]
    assert normalize_provider_name("groq", providers) == "groq"
    assert normalize_provider_name("groqcloud", providers) == "groq"
    assert normalize_provider_name("workers-ai", providers) == "cloudflare"
    assert normalize_provider_name("cf-workers-ai", providers) == "cloudflare"
    assert normalize_provider_name("cfai", providers) == "cloudflare"


def test_free_tier_flag_marks_models_free():
    from botex.providers.analytics import is_free_model

    cfg = {"free_tier": True}
    assert is_free_model("openai/gpt-oss-120b", cfg, "groq")
    assert is_free_model("@cf/meta/llama-3.3-70b-instruct-fp8-fast", cfg, "cloudflare")


def test_groq_denylist_excludes_non_chat_families():
    from botex.providers.denylist import check_model_denylist

    cfg = load_config()
    allowed, _ = check_model_denylist("openai/gpt-oss-120b", "groq", config=cfg)
    assert allowed is True
    for slug in (
        "whisper-large-v3",
        "llama-3.1-8b-instant",
        "meta-models/llama-4-scout",
    ):
        ok, _reason = check_model_denylist(slug, "groq", config=cfg)
        assert ok is False, slug


def test_cloudflare_denylist_and_decisions_models():
    from botex.providers.denylist import check_model_denylist
    from botex.server.config import is_decisions_model

    cfg = load_config()
    providers = cfg["providers"]

    allowed, _ = check_model_denylist(
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "cloudflare", config=cfg
    )
    assert allowed is True
    for slug in (
        "@cf/moonshotai/kimi-k2.6",
        "@cf/zai-org/glm-5.3",
        "@cf/deepseek-ai/deepseek-v4-pro-0813",
        "@cf/baai/bge-m3",
        "@cf/openai/whisper-large-v3-turbo",
    ):
        ok, _reason = check_model_denylist(slug, "cloudflare", config=cfg)
        assert ok is False, slug

    # clef/clairic decision models are NOT denylisted — they are rejected
    # earlier by is_decisions_model so the engine reports UNSUPPORTED.
    cf_cfg = providers["cloudflare"]
    assert is_decisions_model("@cf/cloudflare/clef", cf_cfg) is True
    assert is_decisions_model("@cf/cloudflare/clef-flash", cf_cfg) is True
    assert is_decisions_model("@cf/meta/llama-3.3-70b-instruct-fp8-fast", cf_cfg) is False


def test_groq_reasoning_effort_is_per_model():
    """gpt-oss accepts only low/medium/high ('none' -> HTTP 400); qwen3 gets 'none'."""
    p = GroqProvider("groq", {})
    for slug in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        effort = p.get_extra_body(model=slug)["reasoning_effort"]
        assert effort == "low", slug
        assert effort != "none", slug
    assert p.get_extra_body(model="qwen/qwen3.8-27b")["reasoning_effort"] == "none"
    assert "reasoning_effort" not in p.get_extra_body(model="meta-llama/llama-4-scout")


def test_provider_forbids_stream_cloudflare():
    from botex.providers.providers import provider_forbids_stream

    assert provider_forbids_stream("cloudflare") is True
    assert provider_forbids_stream("workers-ai") is True
    assert provider_forbids_stream("workers_ai") is True
    assert provider_forbids_stream("cf-workers-ai") is True
    # Unregistered alias names resolve upstream; the endpoint hostname is
    # the fail-closed backstop for them.
    assert (
        provider_forbids_stream(
            "cf_workers_ai",
            {"base_url": "https://api.cloudflare.com/client/v4/accounts/x/ai/v1"},
        )
        is True
    )
    assert (
        provider_forbids_stream(
            "custom", {"base_url": "https://api.cloudflare.com/client/v4/x/ai/v1"}
        )
        is True
    )
    assert provider_forbids_stream("groq", {"stream": True}) is False
    assert provider_forbids_stream("nvidia") is False
    assert provider_forbids_stream("") is False


def test_cloudflare_catalog_skips_unresolved_account_id(monkeypatch, tmp_path):
    """Token set but no account id -> provider treated as unconfigured, not an error."""
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    from botex.providers.catalog import get_consolidated_models

    cfg = {"providers": {"cloudflare": {"api_key_env": "CLOUDFLARE_API_TOKEN", "account_id": ""}}}
    models = get_consolidated_models(
        providers=("cloudflare",),
        config=cfg,
        cache_file=tmp_path / "models_cache.json",
        force_refresh=True,
    )
    assert models == []


def test_paid_consent_gate_treats_new_providers_as_free():
    """free_tier providers bypass paid_consent_required without allow_expensive_model."""
    from botex.providers.pricing import check_model_price

    pricing_cfg = {"enabled": True, "paid_consent_required": True}
    cases = (
        ("groq", "openai/gpt-oss-120b", {"free_tier": True}),
        ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", {"free_tier": True}),
    )
    for prov, model, pcfg in cases:
        pcfg = dict(pcfg, base_url="https://example.invalid/v1")
        ok, _msg = check_model_price(model, pcfg, pricing_cfg, provider_name=prov)
        assert ok is True, prov


def test_provider_blocks_parity_across_config_sources():
    """groq/cloudflare must agree 1:1 across DEFAULTS, botex.config.json, example.json."""
    import json

    from botex.server.config import DEFAULTS

    repo = Path(__file__).resolve().parent.parent
    live = json.loads((repo / "botex.config.json").read_text("utf-8"))
    example = json.loads((repo / "botex.config.example.json").read_text("utf-8"))
    defaults = DEFAULTS["providers"]

    expected_key_env = {"groq": "GROQ_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN"}
    for prov, key_env in expected_key_env.items():
        sources = (
            defaults[prov],
            live["providers"][prov],
            example["providers"][prov],
        )
        for src in sources:
            assert src["api_key_env"] == key_env, prov
        assert (
            defaults[prov]["models"]
            == live["providers"][prov]["models"]
            == example["providers"][prov]["models"]
        ), prov
        assert (
            defaults[prov]["model_fallbacks"]
            == live["providers"][prov]["model_fallbacks"]
            == example["providers"][prov]["model_fallbacks"]
        ), prov

    for prov in ("groq", "cloudflare"):
        live_rl = live["rate_limits"][prov]
        ex_rl = example["rate_limits"][prov]
        assert live_rl["rpm"] == ex_rl["rpm"]
        assert live_rl["tpm"] == ex_rl["tpm"]
    assert live["rate_limits"]["groq"]["tpm"] == 8000
    assert live["rate_limits"]["cloudflare"]["tpm"] == 100000
    assert live["provider_fallbacks"]["groq"] == example["provider_fallbacks"]["groq"]
    assert live["provider_fallbacks"]["cloudflare"] == example["provider_fallbacks"]["cloudflare"]


def test_groq_extra_body_config_stays_empty():
    """reasoning_effort is model-scoped in GroqProvider — config must not pin one value."""
    cfg = load_config()
    assert "reasoning_effort" not in (cfg["providers"]["groq"].get("extra_body") or {})
    repo = Path(__file__).resolve().parent.parent
    live = json.loads((repo / "botex.config.json").read_text("utf-8"))
    assert "reasoning_effort" not in (live["providers"]["groq"].get("extra_body") or {})
    example = json.loads((repo / "botex.config.example.json").read_text("utf-8"))
    assert "reasoning_effort" not in (example["providers"]["groq"].get("extra_body") or {})


def test_groq_denylist_preserved_after_audit_fixes():
    from botex.providers.denylist import check_model_denylist

    cfg = load_config()
    for slug in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        ok, _reason = check_model_denylist(slug, "groq", config=cfg)
        assert ok is False, slug


def test_botex_free_provider_pref_includes_new_providers_in_order():
    from botex.edge.adapters import _BOTEX_FREE_PROVIDER_PREF

    assert _BOTEX_FREE_PROVIDER_PREF == (
        "openrouter-free",
        "nvidia",
        "google",
        "cloudflare",
        "groq",
        "ollama",
    )
    assert (
        _BOTEX_FREE_PROVIDER_PREF.index("cloudflare")
        < _BOTEX_FREE_PROVIDER_PREF.index("groq")
        < _BOTEX_FREE_PROVIDER_PREF.index("ollama")
    )

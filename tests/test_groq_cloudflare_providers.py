# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Registration, config, and URL-resolution tests for Groq + Cloudflare Workers AI."""

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
            if url.endswith("/ai/v1/models"):
                return FakeResp({}, status=404)
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
    assert calls[0].endswith("/ai/v1/models")
    assert calls[1].endswith("/ai/models/search")


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

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Provider Subsystem Unit Tests
====================================
Comprehensive offline test suite for ProviderRegistry, BaseProvider, concrete adapters
(OpenRouterProvider, NvidiaProvider, GenericOpenAIProvider), CreditsInfo, and legacy facade.

Strictly isolated: zero real network connections, zero required API keys, full mock injection.
"""

from __future__ import annotations

import copy
import json
import os
import socket
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from botex import providers
from botex.providers import (
    _FINISH_KIND,
    _VALID_FINISH_KIND_MAP,
    BaseProvider,
    GenericOpenAIProvider,
    GoogleAIStudioProvider,
    NvidiaProvider,
    OpenRouterProvider,
    ProviderRegistry,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure registry state is restored after tests that mutate registrations."""
    orig_registry = dict(ProviderRegistry._registry)
    yield
    ProviderRegistry._registry = orig_registry


class FakeClient:
    """Mock client capturing factory initialization kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.chat = None


def fake_client_factory(**kwargs: Any) -> FakeClient:
    return FakeClient(**kwargs)


# ---------------------------------------------------------------------------
# Group 1: ProviderRegistry Built-in Registration & Lookup
# ---------------------------------------------------------------------------
def test_registry_builtin_providers():
    """TEST-REG-01: Verify default built-in providers are auto-registered."""
    registered = ProviderRegistry.list_providers()
    assert "openrouter" in registered
    assert "nvidia" in registered
    assert "generic_openai" in registered
    assert len(registered) >= 3


def test_registry_get_builtin_instances():
    """TEST-REG-02: Verify retrieval of concrete instances inheriting from BaseProvider."""
    orp = ProviderRegistry.get("openrouter")
    assert isinstance(orp, OpenRouterProvider)
    assert isinstance(orp, BaseProvider)

    nvd = ProviderRegistry.get("nvidia")
    assert isinstance(nvd, NvidiaProvider)
    assert isinstance(nvd, BaseProvider)

    gen = ProviderRegistry.get("generic_openai")
    assert isinstance(gen, GenericOpenAIProvider)
    assert isinstance(gen, BaseProvider)


def test_registry_case_insensitive_lookup():
    """TEST-REG-03: Verify case-insensitive provider name lookups."""
    assert isinstance(ProviderRegistry.get("OpenRouter"), OpenRouterProvider)
    assert isinstance(ProviderRegistry.get("OPENROUTER"), OpenRouterProvider)
    assert isinstance(ProviderRegistry.get("Nvidia"), NvidiaProvider)
    assert isinstance(ProviderRegistry.get("NVIDIA"), NvidiaProvider)


# ---------------------------------------------------------------------------
# Group 2: Custom Provider Registration & Error Handling
# ---------------------------------------------------------------------------
def test_registry_custom_provider_registration():
    """TEST-REG-04: Register and retrieve custom provider subclass."""

    class MockCustomProvider(BaseProvider):
        default_base_url = "https://custom.ai/v1"
        default_api_key_env = "CUSTOM_KEY"

    ProviderRegistry.register("mock_custom", MockCustomProvider)
    assert "mock_custom" in ProviderRegistry.list_providers()

    instance = ProviderRegistry.get("mock_custom")
    assert isinstance(instance, MockCustomProvider)
    assert isinstance(instance, BaseProvider)


def test_registry_register_invalid_class():
    """TEST-REG-05: Non-subclass registration rejection with TypeError."""

    class InvalidProvider:
        pass

    with pytest.raises(TypeError) as exc_info:
        ProviderRegistry.register("invalid", InvalidProvider)  # type: ignore[arg-type]

    assert "BaseProvider" in str(exc_info.value)
    assert "invalid" not in ProviderRegistry.list_providers()


def test_registry_get_unknown_provider():
    """TEST-REG-06: Querying unregistered provider without config raises ValueError."""
    with pytest.raises(ValueError) as exc_info:
        ProviderRegistry.get("non_existent_provider_xyz")

    err_msg = str(exc_info.value)
    assert "non_existent_provider_xyz" in err_msg


def test_registry_overwrite_provider():
    """TEST-REG-07: Overwriting existing registration replaces class cleanly."""

    class ReplacementProvider(BaseProvider):
        pass

    ProviderRegistry.register("generic_openai", ReplacementProvider)
    instance = ProviderRegistry.get("generic_openai")
    assert isinstance(instance, ReplacementProvider)


# ---------------------------------------------------------------------------
# Group 3: OpenRouterProvider Adapter Specifications
# ---------------------------------------------------------------------------
def test_openrouter_provider_client_construction():
    """TEST-ORP-01: Default base URL and attribution headers for OpenRouter."""
    provider = ProviderRegistry.get("openrouter")
    client = provider.get_client(
        api_key="test-sk-key",
        client_factory=fake_client_factory,
        provider_cfg={},
    )
    assert client.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert client.kwargs["api_key"] == "test-sk-key"
    assert client.kwargs["default_headers"] is not None
    assert "X-Title" in client.kwargs["default_headers"]

    # Verify attribution headers when referer is present
    with patch(
        "botex.providers.app_headers",
        return_value={"X-Title": "BoteX", "HTTP-Referer": "https://botex.ai"},
    ):
        client_with_ref = provider.get_client(
            api_key="test-sk-key",
            client_factory=fake_client_factory,
            provider_cfg={},
        )
        assert client_with_ref.kwargs["default_headers"] is not None
        assert "HTTP-Referer" in client_with_ref.kwargs["default_headers"]
        assert "X-Title" in client_with_ref.kwargs["default_headers"]


def test_openrouter_provider_attribution_opt_out():
    """TEST-ORP-02: Setting attribution_headers: False disables attribution headers."""
    provider = ProviderRegistry.get("openrouter")
    client = provider.get_client(
        api_key="k",
        client_factory=fake_client_factory,
        provider_cfg={"attribution_headers": False},
    )
    assert client.kwargs["default_headers"] is None


def test_openrouter_provider_key_resolution():
    """TEST-ORP-03: Key resolution order: explicit > env > secrets > empty string."""
    provider = ProviderRegistry.get("openrouter")

    # 1. Explicit key priority
    assert provider.resolve_api_key(explicit_key="exp", secrets_cfg={}) == "exp"

    # 2. Environment variable priority when explicit is None
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-or-key"}):
        assert provider.resolve_api_key(None, secrets_cfg={}) == "env-or-key"

    # 3. Secrets config fallback when env var is unset
    with patch.dict(os.environ, {}, clear=True):
        assert (
            provider.resolve_api_key(
                None,
                secrets_cfg={"openrouter_api_key": "sec-or-key"},
            )
            == "sec-or-key"
        )

    # 4. Fallback to empty string when all unset
    with patch.dict(os.environ, {}, clear=True):
        assert provider.resolve_api_key(None, secrets_cfg={}) == ""


def test_openrouter_stateless_no_stored_secrets():
    """TEST-ORP-04: Verify provider instance does NOT retain api_key in attributes."""
    provider = OpenRouterProvider("openrouter")
    provider.resolve_api_key(explicit_key="secret-key-12345")
    provider.get_client(api_key="secret-key-12345", client_factory=fake_client_factory)

    assert "api_key" not in provider.__dict__
    assert "secret-key-12345" not in repr(provider)
    assert "secret-key-12345" not in str(provider.__dict__)


# ---------------------------------------------------------------------------
# Group 4: NvidiaProvider Adapter Specifications
# ---------------------------------------------------------------------------
def test_nvidia_provider_client_construction():
    """TEST-NVD-01: Default NIM base URL and header suppression."""
    provider = ProviderRegistry.get("nvidia")
    client = provider.get_client(
        api_key="nvapi-test",
        client_factory=fake_client_factory,
        provider_cfg={},
    )
    assert client.kwargs["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert client.kwargs["api_key"] == "nvapi-test"
    assert client.kwargs["default_headers"] is None


def test_nvidia_provider_attribution_opt_in():
    """TEST-NVD-02: Attribution headers explicit opt-in for NVIDIA NIM."""
    provider = ProviderRegistry.get("nvidia")
    client = provider.get_client(
        api_key="k",
        client_factory=fake_client_factory,
        provider_cfg={"attribution_headers": True},
    )
    assert client.kwargs["default_headers"] is not None


def test_nvidia_provider_key_resolution():
    """TEST-NVD-03: NVIDIA key resolution via NVIDIA_API_KEY environment variable."""
    provider = ProviderRegistry.get("nvidia")
    with patch.dict(os.environ, {"NVIDIA_API_KEY": "nv-env-key"}, clear=True):
        assert provider.resolve_api_key(None, provider_cfg={}, secrets_cfg={}) == "nv-env-key"

    with patch.dict(os.environ, {"NVAPI_KEY": "nvapi-alt-key"}, clear=True):
        assert provider.resolve_api_key(None, provider_cfg={}, secrets_cfg={}) == "nvapi-alt-key"


# ---------------------------------------------------------------------------
# Group 5: GenericOpenAIProvider Adapter Specifications
# ---------------------------------------------------------------------------
def test_generic_openai_provider_client_construction():
    """TEST-GEN-01: Custom base URL and default key env for generic endpoints."""
    provider = ProviderRegistry.get("generic_openai")
    cfg: Dict[str, Any] = {
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
    }
    client = provider.get_client(
        api_key="tok",
        client_factory=fake_client_factory,
        provider_cfg=cfg,
    )
    assert client.kwargs["base_url"] == "https://api.together.xyz/v1"
    assert client.kwargs["default_headers"] is None
    assert client.kwargs["api_key"] == "tok"


def test_generic_openai_key_resolution():
    """TEST-GEN-02: Key resolution with configured api_key_env."""
    provider = GenericOpenAIProvider("generic", {"api_key_env": "CUSTOM_LLM_KEY"})
    with patch.dict(os.environ, {"CUSTOM_LLM_KEY": "custom-key"}, clear=True):
        assert (
            provider.resolve_api_key(
                None,
                provider_cfg={"api_key_env": "CUSTOM_LLM_KEY"},
                secrets_cfg={},
            )
            == "custom-key"
        )


# ---------------------------------------------------------------------------
# Group 6: Mock Verification & Network Isolation
# ---------------------------------------------------------------------------
def test_mock_isolation_zero_network_calls():
    """TEST-MCK-01: Zero live sockets verification across provider operations."""

    def blocked_connect(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Network call blocked")

    with patch.object(socket.socket, "connect", side_effect=blocked_connect):
        provider = ProviderRegistry.get("openrouter")
        client = provider.get_client(api_key="k", client_factory=fake_client_factory)
        assert client.kwargs["base_url"] == "https://openrouter.ai/api/v1"

        payload = provider.prepare_request_payload(
            model="test/model",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert payload["model"] == "test/model"
        assert payload["extra_body"]["provider"]["data_collection"] == "deny"


def test_suite_passes_with_empty_credentials():
    """TEST-MCK-02: Verify that provider operations function when all credentials unset."""
    with patch.dict(os.environ, {}, clear=True):
        for name in ["openrouter", "nvidia", "generic_openai"]:
            provider = ProviderRegistry.get(name)
            assert provider.resolve_api_key() == ""
            client = provider.get_client(client_factory=fake_client_factory)
            assert client.kwargs["api_key"] == ""

            nresp = provider.normalize_response({"choices": []})
            assert nresp.finish_reason == "provider_error"


# ---------------------------------------------------------------------------
# Group 7: Legacy Facade Preservation & Monkeypatch Compatibility
# ---------------------------------------------------------------------------
def test_facade_build_async_client():
    """TEST-FAC-01: Top-level build_async_client delegates cleanly."""
    client = providers.build_async_client(
        "openrouter",
        {"base_url": "https://openrouter.ai/api/v1"},
        "k",
        client_factory=fake_client_factory,
    )
    assert client.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert client.kwargs["api_key"] == "k"
    assert client.kwargs["default_headers"] is not None


def test_facade_normalize_response():
    """TEST-FAC-02: Top-level normalize_response preserves contract."""
    raw_resp = {
        "choices": [
            {
                "message": {
                    "content": "test content",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {"name": "test_tool", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 10},
    }
    nresp = providers.normalize_response(raw_resp)
    assert nresp.finish_reason == "tool_call"
    assert nresp.content == "test content"
    assert len(nresp.tool_calls) == 1
    assert nresp.tool_calls[0].id == "tc1"
    assert nresp.tool_calls[0].name == "test_tool"
    assert nresp.usage.prompt_tokens == 15
    assert nresp.usage.completion_tokens == 10


def test_facade_resolve_api_key():
    """TEST-FAC-03: Top-level resolve_api_key preserves parameter order and priority."""
    assert providers.resolve_api_key("explicit", "openrouter", {}, {}) == "explicit"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-key"}, clear=True):
        assert providers.resolve_api_key(None, "openrouter", {}, {}) == "env-key"
    with patch.dict(os.environ, {}, clear=True):
        assert (
            providers.resolve_api_key(None, "openrouter", {}, {"openrouter_api_key": "sec"})
            == "sec"
        )
        assert providers.resolve_api_key(None, "openrouter", {}, {}) == ""


def test_facade_sanitize_provider_error():
    """TEST-FAC-04: Top-level sanitize_provider_error masks secrets and bounds length."""
    raw_secret = "sk-or-v1-abcdef01234567890abcdef01234567890"
    long_msg = f"Failed to authenticate with key {raw_secret}: " + (
        "detailed failure explanation " * 20
    )
    sanitized = providers.sanitize_provider_error(Exception(long_msg), max_chars=100)

    assert raw_secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert len(sanitized) <= 101
    assert sanitized.endswith("…")


def test_facade_engine_monkeypatch_interception():
    """TEST-FAC-05: Monkeypatching client factory intercepts client construction."""
    captured: Dict[str, Any] = {}

    def custom_factory(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake_client_instance"

    client = providers.build_async_client(
        "openrouter",
        {},
        "test-key-monkey",
        client_factory=custom_factory,
    )
    assert client == "fake_client_instance"
    assert captured["api_key"] == "test-key-monkey"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# Additional Security, Immutability & Capability Tests
# ---------------------------------------------------------------------------
def test_static_enum_dictionary_mapping_cwe312():
    """Verify CodeQL CWE-312: finish reasons map to module constant string literals."""
    for raw_fr, expected_kind in _FINISH_KIND.items():
        resp = {"choices": [{"message": {"content": "c"}, "finish_reason": raw_fr}]}
        nresp = providers.normalize_response(resp)
        assert nresp.finish_reason is _VALID_FINISH_KIND_MAP.get(raw_fr, "unknown")
        assert nresp.finish_reason == expected_kind

    # Fallback to literal "unknown" for unrecognized finish reasons
    weird_resp = {"choices": [{"message": {"content": "c"}, "finish_reason": "magic_stop"}]}
    assert providers.normalize_response(weird_resp).finish_reason == "unknown"


def test_provider_config_immutability():
    """Verify provider request preparation does not mutate caller's configuration dict."""
    orig_extra = {"provider": {"data_collection": "allow"}, "transforms": ["middle-out"]}
    cfg = {"extra_body": orig_extra}
    cfg_copy = copy.deepcopy(cfg)

    provider = OpenRouterProvider("openrouter", config=cfg)
    payload = provider.prepare_request_payload("model", messages=[])

    assert payload["extra_body"]["provider"]["data_collection"] == "deny"
    assert cfg == cfg_copy


def test_openrouter_credits_mocked():
    """Verify OpenRouterProvider get_credits parses auth/key and credits responses."""
    provider = OpenRouterProvider("openrouter")

    key_resp = json.dumps(
        {
            "data": {
                "label": "Test Key",
                "usage": 2.50,
                "limit": 10.00,
                "is_free_tier": False,
            }
        }
    ).encode("utf-8")

    cred_resp = json.dumps(
        {
            "data": {
                "total_credits": 20.00,
                "total_usage": 5.00,
            }
        }
    ).encode("utf-8")

    def mock_urlopen(req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp_mock = MagicMock()
        if "auth/key" in url:
            resp_mock.read.return_value = key_resp
        elif "credits" in url:
            resp_mock.read.return_value = cred_resp
        else:
            resp_mock.read.return_value = b"{}"
        resp_mock.__enter__.return_value = resp_mock
        return resp_mock

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        info = provider.get_credits(api_key="sk-or-test-credits-key")
        assert info.supported is True
        assert info.provider == "openrouter"
        assert info.balance_usd == 15.00  # 20.00 - 5.00
        assert info.usage_usd == 5.00
        assert info.limit_usd == 10.00
        assert info.limit_remaining_usd == 7.50  # 10.00 - 2.50
        assert info.is_free_tier is False
        assert info.label == "Test Key"


def test_nvidia_credits_unsupported():
    """Verify NvidiaProvider reports unsupported credits status."""
    provider = NvidiaProvider("nvidia")
    info = provider.get_credits()
    assert info.supported is False
    assert info.provider == "nvidia"


def test_fetch_catalog_mocked():
    """Verify BaseProvider fetch_catalog parses /models JSON response."""
    provider = GenericOpenAIProvider("custom", {"base_url": "https://api.example.com/v1"})
    catalog_data = {
        "data": [
            {"id": "model-1", "name": "Model 1"},
            {"id": "model-2", "name": "Model 2"},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(catalog_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        models = provider.fetch_catalog(api_key="test-key")
        assert len(models) == 2
        assert models[0]["id"] == "model-1"
        assert models[1]["id"] == "model-2"


# ---------------------------------------------------------------------------
# Group 8: Adversarial Edge Cases & Regression Invariants (Milestone M1 Iteration 2)
# ---------------------------------------------------------------------------
def test_base_provider_build_client_polymorphism():
    """TEST-BASE-01: Verify build_client() dynamically dispatches to overridden get_client()."""

    class CustomPolymorphicProvider(BaseProvider):
        def get_client(
            self,
            api_key: str | None = None,
            client_factory: Any = None,
            provider_cfg: Dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            return {
                "client_type": "custom_mock_client",
                "api_key": api_key,
                "custom_arg": kwargs.get("custom_arg"),
            }

    # 1. Verify subclass polymorphism
    provider = CustomPolymorphicProvider("custom_poly")

    direct_res = provider.get_client(api_key="sk-poly-test", custom_arg="poly_val")
    assert direct_res["client_type"] == "custom_mock_client"
    assert direct_res["api_key"] == "sk-poly-test"
    assert direct_res["custom_arg"] == "poly_val"

    aliased_res = provider.build_client(api_key="sk-poly-test", custom_arg="poly_val")
    assert aliased_res == direct_res
    assert aliased_res["client_type"] == "custom_mock_client"

    # 2. Verify default BaseProvider build_client delegates cleanly
    base_provider = BaseProvider("base_default")
    base_client = base_provider.build_client(
        api_key="sk-base-key",
        client_factory=fake_client_factory,
    )
    assert base_client.kwargs["api_key"] == "sk-base-key"
    assert base_client.kwargs["base_url"] == base_provider.base_url


def test_openrouter_extra_body_null_or_non_dict_provider():
    """TEST-ORP-05: Handle extra_body with provider=None or non-dict without TypeError."""
    # Case 1: provider is explicitly None
    p_none = OpenRouterProvider("openrouter", config={"extra_body": {"provider": None}})
    extra_none = p_none.get_extra_body(allow_non_zdr=False)
    assert isinstance(extra_none["provider"], dict)
    assert extra_none["provider"]["data_collection"] == "deny"

    payload_none = p_none.prepare_request_payload(
        model="test/model",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert payload_none["extra_body"]["provider"]["data_collection"] == "deny"

    # Case 2: provider is a non-dict string (e.g. routing override "anthropic")
    p_str = OpenRouterProvider("openrouter", config={"extra_body": {"provider": "anthropic"}})
    extra_str = p_str.get_extra_body(allow_non_zdr=False)
    assert isinstance(extra_str["provider"], dict)
    assert extra_str["provider"]["data_collection"] == "deny"

    payload_str = p_str.prepare_request_payload(
        model="test/model",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert payload_str["extra_body"]["provider"]["data_collection"] == "deny"

    # Case 3: verify allow_non_zdr=True sets data_collection to allow
    extra_allow = p_none.get_extra_body(allow_non_zdr=True)
    assert extra_allow["provider"]["data_collection"] == "allow"

    # Case 4: verify valid dictionary routing prefs (e.g. {"order": ["Anthropic"]}) are preserved
    p_dict = OpenRouterProvider(
        "openrouter",
        config={"extra_body": {"provider": {"order": ["Anthropic"]}}},
    )
    extra_dict = p_dict.get_extra_body(allow_non_zdr=False)
    assert extra_dict["provider"]["order"] == ["Anthropic"]
    assert extra_dict["provider"]["data_collection"] == "deny"


def test_provider_whitespace_name_secret_resolution():
    """TEST-REG-08: Provider names with leading/trailing whitespace resolve secrets properly."""
    secrets = {"openrouter_api_key": "sec_token_whitespace_123"}

    # Clear environment variables to isolate secrets_cfg fallback
    with patch.dict(os.environ, {}, clear=True):
        # 1. Retrieved via ProviderRegistry.get with whitespace padding
        p_registry = ProviderRegistry.get("  openrouter  \t\n")
        resolved_key = p_registry.resolve_api_key(secrets_cfg=secrets)
        assert resolved_key == "sec_token_whitespace_123"
        assert resolved_key != ""

        # 2. Direct instantiation with leading and trailing whitespace
        p_direct = OpenRouterProvider("  openrouter  ")
        assert p_direct.resolve_api_key(secrets_cfg=secrets) == "sec_token_whitespace_123"

        # 3. Uppercase name with whitespace padding
        p_upper = ProviderRegistry.get("   OPENROUTER   ")
        assert p_upper.resolve_api_key(secrets_cfg=secrets) == "sec_token_whitespace_123"

        # 4. Custom provider with hyphen and whitespace padding resolving normalized secret
        p_custom = GenericOpenAIProvider("  custom-ai  ")
        custom_sec = {"custom_ai_api_key": "sec_custom_999"}
        assert p_custom.resolve_api_key(secrets_cfg=custom_sec) == "sec_custom_999"

        # 5. Top-level facade function with whitespace provider name
        facade_key = providers.resolve_api_key(
            explicit=None,
            provider_name="  openrouter  ",
            provider_cfg={},
            secrets_cfg=secrets,
        )
        assert facade_key == "sec_token_whitespace_123"

        # 6. Direct BaseProvider instantiation with padding
        base_p = BaseProvider("  custom-service  ")
        assert base_p.name == "custom-service"
        assert (
            base_p.resolve_api_key(secrets_cfg={"custom_service_api_key": "tok-custom"})
            == "tok-custom"
        )


def test_provider_local_api_key_fallback():
    """Verify local providers (Ollama / Bielik) resolve api_key_fallback without requiring env secrets."""
    with patch.dict(os.environ, {}, clear=True):
        p = ProviderRegistry.get(
            "ollama",
            {
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key_fallback": "ollama",
                "local": True,
            },
        )
        assert p.resolve_api_key() == "ollama"

        p_local = ProviderRegistry.get(
            "ollama-custom", {"base_url": "http://127.0.0.1:11434/v1", "local": True}
        )
        assert p_local.resolve_api_key() == "ollama"

        assert p.resolve_api_key(explicit_key="custom-key") == "custom-key"

        p_remote = ProviderRegistry.get("generic", {"base_url": "https://api.example.com/v1"})
        assert p_remote.resolve_api_key() == ""


# ---------------------------------------------------------------------------
# Group 9: GoogleAIStudioProvider Adapter Specifications
# ---------------------------------------------------------------------------
def test_google_provider_registration_and_aliases():
    """TEST-GAI-01: google/aistudio/gemini names and substring heuristic resolve to the adapter."""
    assert isinstance(ProviderRegistry.get("google"), GoogleAIStudioProvider)
    assert isinstance(ProviderRegistry.get("GOOGLE"), GoogleAIStudioProvider)
    assert isinstance(ProviderRegistry.get("aistudio"), GoogleAIStudioProvider)
    assert isinstance(ProviderRegistry.get("gemini"), GoogleAIStudioProvider)
    assert "google" in ProviderRegistry.list_providers()
    # Substring heuristic mirrors the openrouter/nvidia convention
    assert isinstance(ProviderRegistry.get("google-ai-studio", {}), GoogleAIStudioProvider)
    # OpenRouter keeps precedence for mixed names
    assert isinstance(ProviderRegistry.get("openrouter", {}), OpenRouterProvider)


def test_google_provider_client_construction():
    """TEST-GAI-02: Default OpenAI-compat base URL, attribution headers off by default."""
    provider = ProviderRegistry.get("google")
    client = provider.get_client(
        api_key="gemini-test",
        client_factory=fake_client_factory,
        provider_cfg={},
    )
    assert client.kwargs["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert client.kwargs["api_key"] == "gemini-test"
    assert client.kwargs["default_headers"] is None


def test_google_provider_key_resolution():
    """TEST-GAI-03: Precedence explicit > api_key_env > GEMINI_API_KEY > GOOGLE_API_KEY > secrets."""
    provider = ProviderRegistry.get("google")

    assert provider.resolve_api_key(explicit_key="exp", secrets_cfg={}) == "exp"

    with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-env"}, clear=True):
        assert provider.resolve_api_key(None, provider_cfg={}, secrets_cfg={}) == "gemini-env"

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "google-env"}, clear=True):
        assert provider.resolve_api_key(None, provider_cfg={}, secrets_cfg={}) == "google-env"

    # Configured api_key_env wins over the built-in fallbacks
    with patch.dict(
        os.environ,
        {"CUSTOM_GEMINI": "cfg-env", "GEMINI_API_KEY": "gemini-env"},
        clear=True,
    ):
        p = ProviderRegistry.get("google", {"api_key_env": "CUSTOM_GEMINI"})
        assert p.resolve_api_key(None, provider_cfg={"api_key_env": "CUSTOM_GEMINI"}) == "cfg-env"

    # Secrets fallbacks: canonical name and cross-alias
    with patch.dict(os.environ, {}, clear=True):
        assert (
            provider.resolve_api_key(None, secrets_cfg={"google_api_key": "sec-google"})
            == "sec-google"
        )
        p_alias = ProviderRegistry.get("aistudio")
        assert (
            p_alias.resolve_api_key(None, secrets_cfg={"gemini_api_key": "sec-gemini"})
            == "sec-gemini"
        )
        assert provider.resolve_api_key(None, secrets_cfg={}) == ""


def test_google_extra_body_sanitization():
    """TEST-GAI-04: OpenRouter-only keys are stripped; google.* and reasoning_effort pass."""
    cfg = {
        "extra_body": {
            "reasoning_effort": "low",
            "provider": {"data_collection": "deny", "sort": "throughput"},
            "models": ["a", "b"],
            "transforms": [],
            "usage": {"include": True},
            "reasoning": {"max_tokens": 4000},
            "google": {"safety_settings": [{"category": "HARM", "threshold": "OFF"}]},
        }
    }
    cfg_copy = copy.deepcopy(cfg)
    provider = GoogleAIStudioProvider("google", config=cfg)
    extra = provider.get_extra_body()

    assert extra["reasoning_effort"] == "low"
    assert extra["google"]["safety_settings"]
    for leaked in ("provider", "models", "transforms", "usage", "reasoning"):
        assert leaked not in extra
    # Caller's config dict is never mutated
    assert cfg == cfg_copy


def test_google_credits_unsupported():
    """TEST-GAI-05: Google AI Studio exposes no credits endpoint."""
    provider = GoogleAIStudioProvider("google")
    info = provider.get_credits()
    assert info.supported is False
    assert info.provider == "google"


def test_extra_content_passthrough_normalize():
    """TEST-GAI-06: Message- and tool-call-level extra_content survive normalization."""
    raw_resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "extra_content": {"google": {"thought_signature": "sig-msg"}},
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {"name": "read_file", "arguments": "{}"},
                            "extra_content": {"google": {"thought_signature": "sig-tc"}},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    nresp = providers.normalize_response(raw_resp)
    assert nresp.finish_reason == "tool_call"
    assert nresp.extra_content == {"google": {"thought_signature": "sig-msg"}}
    assert nresp.tool_calls[0].extra_content == {"google": {"thought_signature": "sig-tc"}}

    # Responses without extras keep the field unset
    plain = providers.normalize_response(
        {"choices": [{"message": {"content": "c"}, "finish_reason": "stop"}]}
    )
    assert plain.extra_content is None


def test_extra_content_passthrough_streaming():
    """TEST-GAI-07: Streaming deltas accumulate extra_content on message and tool calls."""
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "extra_content": {"google": {"thought_signature": "sig-stream"}},
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "function": {"name": "read_file", "arguments": ""},
                                "extra_content": {"google": {"thought_signature": "sig-tc"}},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"path":"a.txt"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    nresp = providers.normalize_streaming_chunks(chunks, raw_model="gemini-2.5-pro")
    assert nresp.finish_reason == "tool_call"
    assert nresp.extra_content == {"google": {"thought_signature": "sig-stream"}}
    assert nresp.tool_calls[0].extra_content == {"google": {"thought_signature": "sig-tc"}}
    assert nresp.tool_calls[0].arguments == '{"path":"a.txt"}'


def test_google_fetch_catalog_mocked():
    """TEST-GAI-08: fetch_catalog hits {base_url}/models and parses the data array."""
    provider = GoogleAIStudioProvider("google")
    catalog_data = {
        "data": [
            {"id": "gemini-2.5-flash"},
            {"id": "gemini-2.5-pro"},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(catalog_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mocked:
        models = provider.fetch_catalog(api_key="gai-test")
        assert [m["id"] for m in models] == ["gemini-2.5-flash", "gemini-2.5-pro"]
        req = mocked.call_args[0][0]
        assert req.full_url.endswith("/v1beta/openai/models")
        assert req.get_header("Authorization") == "Bearer gai-test"


def test_normalize_tool_call_arguments_dict_coerced_to_string():
    """TEST-GAI-09: a provider returning pre-parsed arguments as an object
    must still produce a JSON string on the wire echo (review fix)."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": {"path": "a.txt"}},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    nresp = providers.normalize_response(resp)
    assert nresp.tool_calls[0].arguments == '{"path": "a.txt"}'
    assert isinstance(nresp.tool_calls[0].arguments, str)


def test_google_resolve_key_dual_env_precedence(monkeypatch):
    """TEST-GAI-10: both GEMINI_API_KEY and GOOGLE_API_KEY set with different
    values -> GEMINI_API_KEY wins (one-time stderr warning emitted)."""
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-a")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-b")
    provider = GoogleAIStudioProvider("google", {})
    assert provider.resolve_api_key() == "gemini-a"
    assert provider._key_precedence_warned is True
    assert provider.resolve_api_key() == "gemini-a"

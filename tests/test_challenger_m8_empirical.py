# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Empirical adversarial challenger tests for Milestone M8 (PR-2):
Backend Catalog & Wallet Contract Verification and Stress Suite.

Covers:
1. get_consolidated_models() isolation: GLM & Zai never registered under any provider combinations
2. ProviderRegistry clean rejection: ProviderRegistry.get("zai") raises ValueError("Unknown provider: 'zai'")
3. CreditsInfo stress harness: empty, partial, malformed, secret-bearing quota_windows and units
4. Wallet service serialization and low budget warning USD guard
5. ZaiGLMProvider deprecation and quota window normalization resilience
"""

from __future__ import annotations

import warnings
from dataclasses import FrozenInstanceError
from typing import Any, Dict, List

import pytest

from botex.providers.adapters.zai import (
    _WINDOW_TYPE_MAP,
    _WINDOW_UNITS_MAP,
    ZaiGLMProvider,
    _normalize_quota_window,
    _normalize_reset_time,
)
from botex.providers.catalog import (
    _CONSOLIDATED_MEM_CACHE,
    _VALID_PROVIDERS_MAP,
    get_consolidated_models,
)
from botex.providers.providers import CreditsInfo, ProviderRegistry
from botex.providers.wallet import WalletService


class TestEmpiricalCatalogIsolation:
    """Empirical verification that GLM and Zai are never registered in get_consolidated_models."""

    @pytest.fixture(autouse=True)
    def clear_catalog_cache(self):
        """Reset the consolidated models in-memory cache before and after test."""
        _CONSOLIDATED_MEM_CACHE["models"] = []
        _CONSOLIDATED_MEM_CACHE["timestamp"] = 0.0
        yield
        _CONSOLIDATED_MEM_CACHE["models"] = []
        _CONSOLIDATED_MEM_CACHE["timestamp"] = 0.0

    @pytest.mark.parametrize(
        "provider_combo",
        [
            ("openrouter", "openrouter-free", "nvidia", "ollama", "google"),
            ("openrouter-free",),
            ("openrouter",),
            ("nvidia",),
            ("ollama",),
            ("google",),
            ("zai",),
            ("glm",),
            ("zai", "glm"),
            ("openrouter-free", "zai", "glm"),
            ("unknown_provider", "zai", "glm-5.3"),
            (),
            ("", "   ", None),
        ],
    )
    def test_consolidated_models_never_includes_glm_or_zai_across_permutations(
        self, provider_combo
    ):
        """Empirically test that across all provider arguments, no GLM or Zai models ever appear."""
        models = get_consolidated_models(providers=provider_combo, force_refresh=True)

        for m in models:
            prov = str(m.get("provider", "")).lower()
            mid = str(m.get("id", "")).lower()

            # Invariant 1: No model has provider 'zai' or 'glm'
            assert prov != "zai", f"Found model with provider='zai': {m}"
            assert prov != "glm", f"Found model with provider='glm': {m}"

    def test_consolidated_models_with_adversarial_zai_config(self):
        """Even if an adversarial or legacy configuration injects zai into providers_cfg,

        get_consolidated_models must never expose zai or glm models.
        """
        adversarial_cfg = {
            "providers": {
                "zai": {
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                    "api_key": "sk-adversarial-test-key",
                    "models": {
                        "coder": "glm-5.3",
                        "flash": "glm-5.3-flash",
                    },
                    "capabilities": {
                        "glm-5.3": {"context_length": 131072, "supports_tools": True},
                    },
                },
                "openrouter-free": {"enabled": True},
            }
        }

        models = get_consolidated_models(
            providers=("openrouter-free", "zai", "glm", "openrouter"),
            config=adversarial_cfg,
            force_refresh=True,
        )

        zai_entries = [m for m in models if m.get("provider") in ("zai", "glm")]
        assert len(zai_entries) == 0, f"Adversarial config leaked zai entries: {zai_entries}"

    def test_valid_providers_map_does_not_contain_zai_or_glm(self):
        """Verify _VALID_PROVIDERS_MAP strictly omits 'zai' and 'glm'."""
        assert "zai" not in _VALID_PROVIDERS_MAP
        assert "glm" not in _VALID_PROVIDERS_MAP


class TestEmpiricalProviderRegistryRejection:
    """Empirical verification that ProviderRegistry cleanly rejects 'zai' and 'glm'."""

    @pytest.mark.parametrize("name", ["zai", "ZAI", " zai ", "glm", "GLM", " glm "])
    def test_provider_registry_get_raises_value_error(self, name):
        """ProviderRegistry.get('zai') must raise ValueError with clean message."""
        with pytest.raises(ValueError) as exc_info:
            ProviderRegistry.get(name)
        assert f"Unknown provider: '{name}'" == str(exc_info.value)

    def test_provider_registry_get_strict_mode(self):
        """Strict mode on ProviderRegistry.get('zai', strict=True) must raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ProviderRegistry.get("zai", strict=True)
        assert "Unknown provider: 'zai'" in str(exc_info.value)

    def test_provider_registry_list_does_not_contain_zai(self):
        """Registered providers list must strictly omit 'zai' and 'glm'."""
        providers = ProviderRegistry.list_providers()
        assert "zai" not in providers
        assert "glm" not in providers

    def test_provider_registry_reset_preserves_omission(self):
        """Resetting the registry must not bring back 'zai' or 'glm'."""
        ProviderRegistry.reset()
        providers = ProviderRegistry.list_providers()
        assert "zai" not in providers
        assert "glm" not in providers
        with pytest.raises(ValueError):
            ProviderRegistry.get("zai")


class TestEmpiricalCreditsInfoStressHarness:
    """Empirical stress-testing of CreditsInfo with malformed, empty, partial, and secret-bearing payloads."""

    def test_credits_info_defaults(self):
        """Default instantiation must provide units='usd' and quota_windows=()."""
        info = CreditsInfo(supported=True, provider="openrouter")
        assert info.units == "usd"
        assert info.quota_windows == ()
        assert info.balance_usd is None

    def test_credits_info_frozen_immutability(self):
        """CreditsInfo instances must be strictly immutable."""
        info = CreditsInfo(supported=True, provider="test", units="usd", quota_windows=())
        with pytest.raises(FrozenInstanceError):
            info.units = "quota"  # type: ignore
        with pytest.raises(FrozenInstanceError):
            info.quota_windows = ()  # type: ignore

    def test_credits_info_empty_and_none_quota_windows(self):
        """CreditsInfo handles empty and None quota_windows gracefully without crashing."""
        info_empty = CreditsInfo(supported=True, provider="test", quota_windows=())
        assert info_empty.quota_windows == ()

        info_none = CreditsInfo(supported=True, provider="test", quota_windows=None)  # type: ignore
        assert info_none.quota_windows is None

    def test_credits_info_list_input_coerced_to_tuple(self):
        """Passing a list of quota windows must be safely converted to a tuple."""
        windows_list = [{"number": 5, "unit": "hours", "usage": 10.0}]
        info = CreditsInfo(supported=True, provider="test", quota_windows=windows_list)  # type: ignore
        assert isinstance(info.quota_windows, tuple)
        assert len(info.quota_windows) == 1
        assert info.quota_windows[0]["number"] == 5

    def test_credits_info_secret_sanitization_in_quota_windows(self):
        """Any sensitive tokens inside quota_windows payloads must be masked by __post_init__."""
        secret_windows = (
            {
                "number": 5,
                "unit": "hours",
                "api_key": "sk-or-v1-supersecretkey1234567890",
                "token": "ghp_secrettokenabc",
                "nested": {
                    "authorization": "Bearer sk-secretpayload",
                    "safe_field": 42,
                },
            },
        )
        info = CreditsInfo(supported=True, provider="test", quota_windows=secret_windows)
        sanitized = info.quota_windows[0]

        assert sanitized["api_key"] == "[REDACTED]"
        assert sanitized["token"] == "[REDACTED]"
        assert sanitized["nested"]["authorization"] == "[REDACTED]"
        assert sanitized["nested"]["safe_field"] == 42

    def test_credits_info_secret_sanitization_in_units(self):
        """If units field contains a bearer or key-like string, it must be masked."""
        secret_unit = "Bearer sk-secretunittoken"
        info = CreditsInfo(supported=True, provider="test", units=secret_unit)
        assert "sk-secret" not in info.units

    def test_credits_info_malformed_quota_windows_elements(self):
        """Malformed, non-dictionary elements in quota_windows must not cause unhandled crashes."""
        malformed_windows = ("string_item", 12345, None, [1, 2, 3], {"normal": "dict"})
        info = CreditsInfo(supported=True, provider="test", quota_windows=malformed_windows)  # type: ignore
        assert isinstance(info.quota_windows, tuple)
        assert len(info.quota_windows) == 5
        assert info.quota_windows[0] == "string_item"
        assert info.quota_windows[1] == 12345
        assert info.quota_windows[4] == {"normal": "dict"}

    def test_credits_info_positional_args_backward_compatibility(self):
        """Full 15-argument positional invocation must map correctly across all fields."""
        info = CreditsInfo(
            True,  # supported
            "test_prov",  # provider
            10.5,  # balance_usd
            2.5,  # usage_usd
            50.0,  # limit_usd
            47.5,  # limit_remaining_usd
            False,  # is_free_tier
            "test_label",  # label
            None,  # error
            {"raw_k": "raw_v"},  # raw_data
            1.0,  # spend_usd
            "2026-12-31T00:00:00Z",  # resets_at
            500,  # free_model_daily_requests
            "quota",  # units
            ({"test_window": 1},),  # quota_windows
        )
        assert info.supported is True
        assert info.provider == "test_prov"
        assert info.balance_usd == 10.5
        assert info.units == "quota"
        assert len(info.quota_windows) == 1


class TestEmpiricalWalletServiceSerialization:
    """Empirical verification of WalletService serialization and USD-only low budget warning."""

    def test_low_budget_warning_active_strictly_for_usd(self):
        """Low budget warning must trigger ONLY when units == 'usd' and balance < threshold."""
        service = WalletService()

        # Case 1: USD with low balance -> True
        info_usd_low = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=0.50,
            units="usd",
        )
        formatted_usd_low = service._format_key_status_dict(
            info_usd_low, low_budget_threshold_usd=5.0
        )
        assert formatted_usd_low["low_budget_warning"] is True
        assert formatted_usd_low["units"] == "usd"

        # Case 2: Quota with low balance numeric value -> False (non-USD currency guard)
        info_quota_low = CreditsInfo(
            supported=True,
            provider="quota_provider",
            balance_usd=0.50,
            units="quota",
        )
        formatted_quota_low = service._format_key_status_dict(
            info_quota_low, low_budget_threshold_usd=5.0
        )
        assert formatted_quota_low["low_budget_warning"] is False, (
            "Quota unit triggered false low_budget_warning"
        )
        assert formatted_quota_low["units"] == "quota"

        # Case 3: USD with balance above threshold -> False
        info_usd_high = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=100.0,
            units="usd",
        )
        formatted_usd_high = service._format_key_status_dict(
            info_usd_high, low_budget_threshold_usd=5.0
        )
        assert formatted_usd_high["low_budget_warning"] is False


class TestEmpiricalZaiAdapterResilience:
    """Empirical stress-testing of offline Z.ai parsing utilities and deprecation notice."""

    def test_zai_provider_emits_deprecation_warning_on_init(self):
        """ZaiGLMProvider instantiation must trigger a DeprecationWarning."""
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            prov = ZaiGLMProvider()
            assert any(issubclass(w.category, DeprecationWarning) for w in recorded)
            assert prov.name == "zai"

    @pytest.mark.parametrize(
        "raw_payload,expected_unit,expected_label",
        [
            ({"unit": 3, "number": 5, "limit": 100, "current_usage": 20}, "hours", "5 Hours"),
            ({"unit": 6, "number": 1, "limit": 500, "current_usage": 50}, "weeks", "Weekly"),
            ({"unit": 5, "number": 1, "limit": 1000, "current_usage": 100}, "months", "Monthly"),
            (
                {"unit": 3, "number": 2, "limit": 50, "currentUsage": 10},
                "hours",
                "2 Hours",
            ),
        ],
    )
    def test_normalize_quota_window_shapes_and_units_mapping(
        self, raw_payload, expected_unit, expected_label
    ):
        """Verify normalization mappings for 3->hours, 5->months, 6->weeks and labels."""
        norm = _normalize_quota_window(raw_payload)
        assert norm["unit"] == expected_unit
        assert norm["label"] == expected_label
        assert norm["limit"] == float(raw_payload["limit"])

    def test_normalize_quota_window_division_by_zero_and_none(self):
        """Verify normalization handles limit=0, limit=None, and negative values safely."""
        zero_payload = {"unit": 3, "number": 5, "limit": 0, "current_usage": 0}
        norm_zero = _normalize_quota_window(zero_payload)
        assert norm_zero["limit"] == 0.0
        assert norm_zero["percentage"] in (0.0, None)
        assert norm_zero["remaining"] == 0.0

        none_payload = {"unit": 3, "number": 5, "limit": None, "current_usage": 10}
        norm_none = _normalize_quota_window(none_payload)
        assert norm_none["limit"] is None
        assert norm_none["percentage"] is None
        assert norm_none["remaining"] is None

    def test_normalize_reset_time_epoch_and_iso(self):
        """Verify _normalize_reset_time handles ms epoch, seconds epoch, and ISO strings."""
        # Milliseconds epoch: 1772841600000 -> 2026-03-07T00:00:00Z
        iso_from_ms = _normalize_reset_time(1772841600000)
        assert iso_from_ms == "2026-03-07T00:00:00Z"

        # Seconds epoch: 1772841600
        iso_from_sec = _normalize_reset_time(1772841600)
        assert iso_from_sec == "2026-03-07T00:00:00Z"

        # Clean ISO string pass-through
        iso_raw = "2026-11-06T12:00:00Z"
        assert _normalize_reset_time(iso_raw) == iso_raw

        # None / invalid
        assert _normalize_reset_time(None) is None
        assert _normalize_reset_time(0) is None
        assert _normalize_reset_time(-100) is None
        assert _normalize_reset_time("") is None

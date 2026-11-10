# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Milestone M8 (PR-2) Empirical Challenge & Adversarial Verification Suite
=======================================================================
Empirical challenger tests for:
1. GET /api/wallet REST endpoint contracts (OpenRouter, quota providers, null safety).
2. low_budget_warning boundary conditions (USD thresholds, limit precedence, non-USD units).
3. Provider registry isolation & catalog cleanup (Z.ai and GLM models completely decoupled).
4. CreditsInfo immutability, defaults, secret redaction, and serialization integrity.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from botex.catalog import get_consolidated_models
from botex.providers.providers import CreditsInfo, ProviderRegistry
from botex.providers.wallet import WalletService
from botex.server.server_cloud import app


@pytest.fixture
def unauthed_client(monkeypatch):
    """TestClient fixture with unauthenticated loopback access."""
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BOTEX_STRICT_AUTH", raising=False)
    with TestClient(app) as tc:
        yield tc


# ===========================================================================
# 1. Empirical GET /api/wallet Contract Verification
# ===========================================================================


class TestWalletApiEndpointEmpirical:
    """Empirical challenge tests for GET /api/wallet endpoint contracts."""

    def test_openrouter_wallet_standard_units_and_empty_quota_windows(self, unauthed_client):
        """Verify GET /api/wallet?provider=openrouter yields units='usd' and quota_windows=[]."""
        mock_credits = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=42.50,
            usage_usd=7.50,
            limit_usd=100.0,
            limit_remaining_usd=50.0,
            is_free_tier=False,
            label="OpenRouter Key",
            error=None,
            units="usd",
            quota_windows=(),
        )
        with patch(
            "botex.server.server_cloud.default_wallet_service.get_credits_async",
            new=AsyncMock(return_value=mock_credits),
        ):
            resp = unauthed_client.get("/api/wallet?provider=openrouter&force_refresh=true")
            assert resp.status_code == 200
            data = resp.json()
            assert data["supported"] is True
            assert data["provider"] == "openrouter"
            assert data["units"] == "usd"
            assert isinstance(data["quota_windows"], list)
            assert data["quota_windows"] == []
            assert data["balance_usd"] == 42.50
            assert data["remaining_usd"] == 50.0

    def test_openrouter_wallet_free_tier_contract(self, unauthed_client):
        """Verify free tier OpenRouter key returns units='usd' and empty quota_windows."""
        mock_credits = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=0.0,
            usage_usd=0.0,
            is_free_tier=True,
            free_model_daily_requests=200,
            label="Free Tier Key",
        )
        with patch(
            "botex.server.server_cloud.default_wallet_service.get_credits_async",
            new=AsyncMock(return_value=mock_credits),
        ):
            resp = unauthed_client.get("/api/wallet?provider=openrouter")
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_free_tier"] is True
            assert data["units"] == "usd"
            assert data["quota_windows"] == []
            assert data["free_model_daily_requests"] == 200

    def test_openrouter_wallet_exhausted_zero_balance_contract(self, unauthed_client):
        """Verify exhausted account (balance=0.0) correctly serializes units='usd' and quota_windows=[]."""
        mock_credits = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=0.0,
            limit_usd=50.0,
            limit_remaining_usd=0.0,
            is_free_tier=False,
            label="Exhausted Key",
        )
        with patch(
            "botex.server.server_cloud.default_wallet_service.get_credits_async",
            new=AsyncMock(return_value=mock_credits),
        ):
            resp = unauthed_client.get("/api/wallet?provider=openrouter")
            assert resp.status_code == 200
            data = resp.json()
            assert data["units"] == "usd"
            assert data["quota_windows"] == []
            assert data["remaining_usd"] == 0.0

    def test_wallet_endpoint_generic_quota_provider(self, unauthed_client):
        """Verify non-USD provider returns structured quota_windows and non-usd units."""
        sample_windows = (
            {
                "label": "5 Hours",
                "unit": "hours",
                "number": 5,
                "limit": 1000000.0,
                "current_usage": 250000.0,
                "percentage": 25.0,
                "next_reset_time": "2026-11-06T22:00:00Z",
            },
        )
        mock_credits = CreditsInfo(
            supported=True,
            provider="custom_quota",
            units="quota",
            quota_windows=sample_windows,
        )
        with patch(
            "botex.server.server_cloud.default_wallet_service.get_credits_async",
            new=AsyncMock(return_value=mock_credits),
        ):
            resp = unauthed_client.get("/api/wallet?provider=custom_quota")
            assert resp.status_code == 200
            data = resp.json()
            assert data["provider"] == "custom_quota"
            assert data["units"] == "quota"
            assert len(data["quota_windows"]) == 1
            assert data["quota_windows"][0]["unit"] == "hours"
            assert data["quota_windows"][0]["percentage"] == 25.0


# ===========================================================================
# 2. Empirical low_budget_warning Boundary Conditions
# ===========================================================================


class TestLowBudgetWarningBoundaries:
    """Empirical challenge tests for boundary values in low_budget_warning calculation."""

    @pytest.mark.parametrize(
        "balance,limit_rem,threshold,expected_warning,desc",
        [
            # USD balances: strictly below threshold triggers warning
            (0.50, None, 1.00, True, "Balance 0.50 USD below 1.00 USD threshold"),
            (0.00, None, 1.00, True, "Zero balance USD below threshold"),
            (-0.50, None, 1.00, True, "Negative balance USD below threshold"),
            (0.99999, None, 1.00, True, "Just below threshold (0.99999 < 1.00)"),
            # USD balances: at or above threshold does NOT trigger warning
            (
                1.00,
                None,
                1.00,
                False,
                "Exact boundary match (1.00 == 1.00, not <)",
            ),
            (1.00001, None, 1.00, False, "Just above threshold (1.00001 > 1.00)"),
            (5.00, None, 1.00, False, "Well above threshold (5.00 > 1.00)"),
            # Limit remaining precedence: limit_remaining_usd takes priority over balance_usd
            (
                100.0,
                0.50,
                1.00,
                True,
                "High balance (100) but low limit_remaining (0.50) -> True",
            ),
            (
                0.50,
                100.0,
                1.00,
                False,
                "Low balance (0.50) but high limit_remaining (100) -> False",
            ),
            (
                100.0,
                1.00,
                1.00,
                False,
                "Exact limit_remaining boundary (1.00 == 1.00) -> False",
            ),
            # Missing remaining values (None)
            (None, None, 1.00, False, "No balance or limit remaining -> False"),
            # Custom thresholds
            (5.00, None, 10.00, True, "Balance 5.00 under custom threshold 10.00"),
            (5.00, None, 2.00, False, "Balance 5.00 above custom threshold 2.00"),
        ],
    )
    def test_usd_budget_boundary_cases(self, balance, limit_rem, threshold, expected_warning, desc):
        info = CreditsInfo(
            supported=True,
            provider="openrouter",
            balance_usd=balance,
            limit_remaining_usd=limit_rem,
            units="usd",
        )
        res = WalletService._format_key_status_dict(info, low_budget_threshold_usd=threshold)
        assert res["low_budget_warning"] is expected_warning, f"Failed on: {desc}"

    @pytest.mark.parametrize(
        "units,balance,limit_rem,threshold,desc",
        [
            ("quota", 0.50, None, 1.00, "Quota units with remaining < threshold"),
            ("quota", 0.00, 0.00, 1.00, "Quota units with zero remaining"),
            ("tokens", 500.0, None, 1000.0, "Token units"),
            ("requests", 0.0, None, 1.00, "Request units"),
            ("percent", 5.0, None, 10.0, "Percentage units"),
        ],
    )
    def test_non_usd_units_suppress_low_budget_warning(
        self, units, balance, limit_rem, threshold, desc
    ):
        """Any units other than 'usd' must NEVER trigger low_budget_warning regardless of remaining balance."""
        info = CreditsInfo(
            supported=True,
            provider="quota_provider",
            balance_usd=balance,
            limit_remaining_usd=limit_rem,
            units=units,
        )
        res = WalletService._format_key_status_dict(info, low_budget_threshold_usd=threshold)
        assert res["low_budget_warning"] is False, (
            f"Expected False for units='{units}', got True: {desc}"
        )


# ===========================================================================
# 3. Provider Registry & Catalog Isolation
# ===========================================================================


class TestProviderRegistryAndCatalogIsolation:
    """Empirical challenge tests verifying complete elimination and isolation of Z.ai/GLM."""

    def test_provider_registry_deactivation_of_zai_and_glm(self):
        """ProviderRegistry.get('zai') and get('glm') must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown provider: 'zai'"):
            ProviderRegistry.get("zai")

        with pytest.raises(ValueError, match="Unknown provider: 'glm'"):
            ProviderRegistry.get("glm")

    def test_consolidated_catalog_does_not_contain_openrouter_free_glm_seeds(self):
        """Consolidated catalog must not contain GLM-5.3 models mapped to openrouter-free."""
        catalog = get_consolidated_models(providers=("openrouter", "openrouter-free"))
        glm_free_models = [
            m
            for m in catalog
            if m.get("provider") == "openrouter-free"
            and any(slug in m.get("slug", "").lower() for slug in ("glm-5.3", "glm-5.3-flash"))
        ]
        assert glm_free_models == [], (
            f"Discovered illicit GLM-5.3 models under openrouter-free: {glm_free_models}"
        )

    def test_consolidated_catalog_does_not_contain_zai_provider(self):
        """Consolidated catalog must not expose 'zai' as an active provider."""
        catalog = get_consolidated_models()
        zai_models = [m for m in catalog if m.get("provider") == "zai"]
        assert zai_models == [], (
            f"Found models registered with inactive provider 'zai': {zai_models}"
        )


# ===========================================================================
# 4. CreditsInfo Secret Redaction & Invariants
# ===========================================================================


class TestCreditsInfoHardening:
    """Empirical challenge tests for CreditsInfo immutability, defaults, and sanitization."""

    def test_credits_info_defaults_are_safe_and_backward_compatible(self):
        """Legacy instantiations without units/quota_windows must default to 'usd' and ()."""
        info = CreditsInfo(supported=True, provider="openrouter")
        assert info.units == "usd"
        assert info.quota_windows == ()

    def test_credits_info_freezes_against_mutation(self):
        """CreditsInfo must be frozen (immutable)."""
        info = CreditsInfo(supported=True, provider="openrouter")
        with pytest.raises((TypeError, AttributeError)):
            info.units = "tokens"  # type: ignore

    def test_credits_info_sanitizes_secrets_in_quota_windows(self):
        """Secrets embedded inside quota window payload entries must be redacted."""
        secret_raw = "sk-or-v1-supersecretbearerkey12345"
        sample_windows = (
            {
                "label": f"Window with {secret_raw}",
                "unit": "hours",
                "number": 5,
                "limit": 1000.0,
                "current_usage": 100.0,
            },
        )
        info = CreditsInfo(
            supported=True,
            provider="test",
            units="quota",
            quota_windows=sample_windows,
        )
        assert len(info.quota_windows) == 1
        window_label = info.quota_windows[0]["label"]
        assert secret_raw not in window_label
        assert "[REDACTED]" in window_label

    def test_challenge_edge_case_none_quota_windows_raises_type_error(self):
        """EMPIRICAL FINDING: Passing quota_windows=None causes _format_key_status_dict to raise TypeError.

        Because getattr(info_none, "quota_windows", ()) returns None when the attribute value is None,
        list(None) raises TypeError: 'NoneType' object is not iterable.
        """
        info_none = CreditsInfo(
            supported=True,
            provider="openrouter",
            quota_windows=None,  # type: ignore
        )
        with pytest.raises(TypeError, match="'NoneType' object is not iterable"):
            WalletService._format_key_status_dict(info_none)

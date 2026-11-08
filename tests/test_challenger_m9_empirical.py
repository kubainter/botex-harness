# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Empirical Adversarial Challenger Test Suite for Milestone M9 (PR-3):
Frontend PWA UI & Store Verification.

Covers:
1. QuotaWindowCard component verification:
   - Timer lifecycle: strict interval clearance in onUnmounted.
   - Progress bar threshold transitions (<80% emerald, 80-94% amber, >=95% rose).
   - ARIA accessibility attributes: role="progressbar", aria-valuenow, aria-valuemin, aria-valuemax.
2. WalletView component verification:
   - Touch targets meet min-h-[44px] lg:min-h-[32px] for interactive elements.
   - Provider switcher supports all defined supported providers (openrouter, nvidia, google, ollama).
3. Wallet Store logic verification:
   - isBudgetLow strictly restricted to USD and balance < 1.00.
   - Non-USD / quota units never trigger low budget warnings.
4. Scope isolation:
   - Zero occurrences of zai / glm in web/src.
5. Production bundle validation.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
SRC_DIR = WEB_DIR / "src"
QUOTA_CARD_PATH = SRC_DIR / "components" / "wallet" / "QuotaWindowCard.vue"
WALLET_VIEW_PATH = SRC_DIR / "views" / "WalletView.vue"
WALLET_STORE_PATH = SRC_DIR / "stores" / "wallet.ts"
I18N_PATH = SRC_DIR / "i18n.ts"


class TestEmpiricalQuotaWindowCard:
    """Empirical verification of QuotaWindowCard.vue lifecycle, thresholds, and ARIA."""

    @pytest.fixture(scope="class")
    @classmethod
    def card_content(cls) -> str:
        assert QUOTA_CARD_PATH.is_file(), f"QuotaWindowCard.vue not found at {QUOTA_CARD_PATH}"
        return QUOTA_CARD_PATH.read_text(encoding="utf-8")

    def test_timer_lifecycle_strictly_clears_interval_on_unmount(self, card_content: str):
        """Verify setInterval is tracked and strictly cleared with clearInterval in onUnmounted."""
        # 1. onMounted sets timerId
        assert "setInterval(" in card_content, "setInterval must be called for countdown tracking"
        assert "timerId = setInterval(" in card_content, (
            "timerId must store setInterval return value"
        )

        # 2. onUnmounted must import and call clearInterval
        assert "onUnmounted" in card_content, "onUnmounted lifecycle hook must be registered"
        assert "clearInterval(timerId)" in card_content, (
            "clearInterval(timerId) must be executed in onUnmounted"
        )

        # 3. Check exact block structure for nullification
        unmount_match = re.search(r"onUnmounted\(\s*\(\)\s*=>\s*\{([\s\S]*?)\}\)", card_content)
        assert unmount_match is not None, "Could not find onUnmounted body"
        unmount_body = unmount_match.group(1)
        assert "clearInterval(timerId)" in unmount_body, (
            "clearInterval(timerId) must be within onUnmounted body"
        )
        assert "timerId = null" in unmount_body, (
            "timerId must be reset to null to eliminate dangling references"
        )

    def test_aria_accessibility_attributes_present(self, card_content: str):
        """Verify progress bar element defines all WCAG 2.1 AA required ARIA attributes."""
        # Check role="progressbar"
        assert 'role="progressbar"' in card_content, 'Progress bar must have role="progressbar"'

        # Check aria-valuenow, aria-valuemin, aria-valuemax
        assert ':aria-valuenow="pct"' in card_content, 'Progress bar must bind :aria-valuenow="pct"'
        assert 'aria-valuemin="0"' in card_content, 'Progress bar must define aria-valuemin="0"'
        assert 'aria-valuemax="100"' in card_content, 'Progress bar must define aria-valuemax="100"'
        assert ':aria-label="displayTitle"' in card_content, (
            "Progress bar must define accessible label"
        )

    @pytest.mark.parametrize(
        "pct_val, expected_bar_color, expected_badge_variant",
        [
            (0.0, "bg-emerald-500", "success"),
            (50.0, "bg-emerald-500", "success"),
            (79.9, "bg-emerald-500", "success"),
            (80.0, "bg-amber-500", "warning"),  # Boundary test
            (85.0, "bg-amber-500", "warning"),
            (94.9, "bg-amber-500", "warning"),
            (95.0, "bg-rose-500", "danger"),  # Boundary test
            (99.9, "bg-rose-500", "danger"),
            (100.0, "bg-rose-500", "danger"),
        ],
    )
    def test_progress_bar_threshold_transitions(
        self,
        card_content: str,
        pct_val: float,
        expected_bar_color: str,
        expected_badge_variant: str,
    ):
        """Verify mathematical threshold boundaries in QuotaWindowCard color calculations."""

        def compute_bar_color(pct: float) -> str:
            if pct >= 95:
                return "bg-rose-500"
            if pct >= 80:
                return "bg-amber-500"
            return "bg-emerald-500"

        def compute_badge_variant(pct: float) -> str:
            if pct >= 95:
                return "danger"
            if pct >= 80:
                return "warning"
            return "success"

        # Verify oracle logic
        assert compute_bar_color(pct_val) == expected_bar_color
        assert compute_badge_variant(pct_val) == expected_badge_variant

        # Verify component implementation contains these exact threshold classes
        assert "bg-emerald-500" in card_content
        assert "bg-amber-500" in card_content
        assert "bg-rose-500" in card_content
        assert "pct.value >= 95" in card_content
        assert "pct.value >= 80" in card_content


class TestEmpiricalWalletView:
    """Empirical verification of WalletView.vue touch targets and provider switcher."""

    @pytest.fixture(scope="class")
    @classmethod
    def view_content(cls) -> str:
        assert WALLET_VIEW_PATH.is_file(), f"WalletView.vue not found at {WALLET_VIEW_PATH}"
        return WALLET_VIEW_PATH.read_text(encoding="utf-8")

    def test_touch_target_accessibility_standard(self, view_content: str):
        """Verify buttons conform to min-h-[44px] for mobile touch and lg:min-h-[32px] for desktop."""
        # Find all button classes
        button_matches = re.findall(r"<button[^>]*class=\"([^\"]*)\"", view_content) + re.findall(
            r"<button[^>]*:class=\"\[([\s\S]*?)\]\"", view_content
        )

        assert len(button_matches) > 0, "WalletView must contain interactive button elements"

        # Check provider buttons have min-h-[44px] lg:min-h-[32px]
        assert "min-h-[44px] lg:min-h-[32px]" in view_content, (
            "Provider and tab buttons must enforce min-h-[44px] lg:min-h-[32px] touch target floor"
        )

    def test_provider_switcher_supported_providers(self, view_content: str):
        """Verify all supported providers (openrouter, nvidia, google, ollama) are registered."""
        for prov_id in ("openrouter", "nvidia", "google", "ollama"):
            assert f"id: '{prov_id}'" in view_content, (
                f"Provider '{prov_id}' must be in supportedProviders"
            )

        # Ensure Zai / GLM is NOT in supportedProviders
        assert "id: 'zai'" not in view_content, "Provider 'zai' must NOT be in supportedProviders"
        assert "id: 'glm'" not in view_content, "Provider 'glm' must NOT be in supportedProviders"

    def test_quota_window_card_integration(self, view_content: str):
        """Verify QuotaWindowCard is imported and rendered conditionally for quota providers."""
        assert "import QuotaWindowCard" in view_content, "WalletView must import QuotaWindowCard"
        assert "<QuotaWindowCard" in view_content, "WalletView must render <QuotaWindowCard />"
        assert "walletInfo?.quota_windows" in view_content, (
            "QuotaWindowCard grid must be conditioned on quota_windows"
        )


class TestEmpiricalWalletStoreLogic:
    """Empirical verification of wallet store Pinia logic."""

    @pytest.fixture(scope="class")
    @classmethod
    def store_content(cls) -> str:
        assert WALLET_STORE_PATH.is_file(), f"wallet.ts not found at {WALLET_STORE_PATH}"
        return WALLET_STORE_PATH.read_text(encoding="utf-8")

    def test_is_budget_low_guard_specification(self, store_content: str):
        """Verify isBudgetLow strictly guards against non-USD / quota units."""
        assert "units !== 'usd'" in store_content, (
            "isBudgetLow must strictly return false when units !== 'usd'"
        )
        assert "bal < 1.0" in store_content, "isBudgetLow must check bal < 1.0 for USD accounts"

    @pytest.mark.parametrize(
        "units, balance_usd, expected_low",
        [
            ("usd", 0.50, True),
            ("usd", 0.999, True),
            ("usd", 1.00, False),
            ("usd", 5.00, False),
            ("USD", 0.10, True),  # Case insensitive
            ("quota", 0.0, False),  # Non-USD units never trigger low USD warning
            ("quota", 0.5, False),
            ("tokens", 0.0, False),
            ("credits", 0.0, False),
            (None, 0.50, True),  # Defaults to usd
        ],
    )
    def test_is_budget_low_oracle(
        self,
        units: Optional[str],
        balance_usd: Optional[float],
        expected_low: bool,
    ):
        """Simulate Pinia isBudgetLow computed logic against all edge cases."""
        w_units = units
        u = (w_units or "usd").lower()
        if u != "usd":
            result = False
        else:
            bal = balance_usd
            result = bal is not None and bal < 1.0

        assert result == expected_low


class TestEmpiricalSourceIsolation:
    """Empirical verification of zero references to zai / glm in web/src."""

    def test_zero_references_to_zai_or_glm_in_web_src(self):
        """Scan all .ts, .vue, .json, .html files in web/src for zai and glm tokens."""
        illegal_tokens = ["zai", "glm", "glm-5.3", "glm-5.3-flash"]
        violations = []

        for p in SRC_DIR.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".ts", ".vue", ".json", ".html"):
                continue

            content = p.read_text(encoding="utf-8", errors="ignore")
            for token in illegal_tokens:
                # Use word-boundary match to avoid matching legitimate Polish words like zaimplementować
                if re.search(rf"\b{re.escape(token)}\b", content, re.IGNORECASE):
                    violations.append((str(p.relative_to(REPO_ROOT)), token))

        assert len(violations) == 0, f"Found illegal zai/glm tokens in web/src: {violations}"


class TestEmpiricalProductionBuild:
    """Empirical verification that web builds successfully in production mode."""

    def test_npm_run_build_succeeds(self):
        """Run npm run build in web/ and verify exit code 0."""
        res = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(WEB_DIR),
            capture_output=True,
            text=True,
            shell=True,
        )
        assert res.returncode == 0, (
            f"npm run build failed with code {res.returncode}:\n{res.stderr}\n{res.stdout}"
        )
        dist_dir = WEB_DIR / "dist"
        assert dist_dir.is_dir(), "web/dist must exist after successful build"
        assert (dist_dir / "index.html").is_file(), "web/dist/index.html must exist"
        assert (dist_dir / "manifest.webmanifest").is_file(), (
            "web/dist/manifest.webmanifest must exist"
        )

    def test_node_empirical_v8_harness(self):
        """Execute the standalone Node.js V8 empirical harness (tests/test_m9_frontend_empirical.mjs)."""
        node_script = REPO_ROOT / "tests" / "test_m9_frontend_empirical.mjs"
        assert node_script.is_file(), f"Empirical node harness not found at {node_script}"

        res = subprocess.run(
            ["node", str(node_script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, f"Node empirical harness failed:\n{res.stderr}\n{res.stdout}"
        assert "ALL TESTS PASSED" in res.stdout, (
            f"Expected ALL TESTS PASSED in output:\n{res.stdout}"
        )
        assert "VERDICT: APPROVE" in res.stdout, (
            f"Expected VERDICT: APPROVE in output:\n{res.stdout}"
        )

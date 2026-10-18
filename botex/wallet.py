# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Wallet & Credits Service
==============================
Unified provider balance, key usage, and quota tracking service.
Handles credit querying from upstream providers (e.g. OpenRouter auth/key
and credits endpoints), response parsing, short-lived in-memory caching,
secret masking, and human-readable formatting.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Dict, NamedTuple, Optional, Tuple, Type, Union

try:
    from .providers import BaseProvider, CreditsInfo, ProviderRegistry, _extract_openrouter_key_meta
    from .security import mask_secrets
except (ImportError, ValueError):
    from providers import BaseProvider, CreditsInfo, ProviderRegistry, _extract_openrouter_key_meta
    from security import mask_secrets

logger = logging.getLogger(__name__)

DEFAULT_WALLET_CACHE_TTL_S: float = 60.0

# Canonical status strings for CodeQL CWE-312 compliance
_STATUS_MAP: Dict[str, str] = {
    "supported": "supported",
    "unsupported": "unsupported",
    "missing_key": "missing_key",
    "error": "error",
}


def _safe_float(val: Any) -> Optional[float]:
    """Safely convert numeric or string numeric value to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _sanitize_raw_payload(payload: Any) -> Any:
    """Recursively scrub API keys and sensitive tokens from debug payloads."""
    if not isinstance(payload, dict):
        if isinstance(payload, str):
            return mask_secrets(payload)
        if isinstance(payload, list):
            return [_sanitize_raw_payload(item) for item in payload]
        return payload
    cleaned: Dict[str, Any] = {}
    for k, v in payload.items():
        k_str = str(k).lower()
        if any(
            secret_term in k_str
            for secret_term in ("api_key", "token", "auth", "secret", "password", "bearer")
        ):
            cleaned[k] = "[REDACTED]"
        elif k_str == "key" and not isinstance(v, dict):
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k] = _sanitize_raw_payload(v)
        elif isinstance(v, list):
            cleaned[k] = [_sanitize_raw_payload(item) for item in v]
        elif isinstance(v, str):
            cleaned[k] = mask_secrets(v)
        else:
            cleaned[k] = v
    return cleaned


class OpenRouterKeyData(NamedTuple):
    """Parsed OpenRouter /api/v1/auth/key payload."""

    usage: Optional[float] = None
    limit: Optional[float] = None
    limit_remaining: Optional[float] = None
    is_free_tier: bool = False
    label: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class OpenRouterCreditsData(NamedTuple):
    """Parsed OpenRouter /api/v1/credits payload."""

    total_credits: Optional[float] = None
    total_usage: Optional[float] = None
    balance: Optional[float] = None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def parse_openrouter_key_data(
    data: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[float], bool, Optional[str]]:
    """Parse /api/v1/auth/key payload dictionary.

    Accepts either raw payload {'data': {...}} or the inner 'data' dict.
    Returns (usage, limit, limit_remaining, is_free_tier, label).
    """
    if not isinstance(data, dict):
        return OpenRouterKeyData()

    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return OpenRouterKeyData()

    raw_label = inner.get("label")
    label = mask_secrets(str(raw_label)) if raw_label is not None else None

    usage = _safe_float(inner.get("usage"))
    limit = _safe_float(inner.get("limit"))
    is_free_tier = bool(inner.get("is_free_tier", False))

    raw_rem = _safe_float(inner.get("limit_remaining"))
    if raw_rem is not None:
        limit_remaining = raw_rem
    elif limit is not None and usage is not None:
        limit_remaining = max(0.0, limit - usage)
    else:
        limit_remaining = None

    return OpenRouterKeyData(
        usage=usage,
        limit=limit,
        limit_remaining=limit_remaining,
        is_free_tier=is_free_tier,
        label=label,
    )


def parse_openrouter_credits_data(
    data: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Parse /api/v1/credits payload dictionary.

    Accepts either raw payload {'data': {...}} or the inner 'data' dict.
    Returns (total_credits, total_usage, balance).
    """
    if not isinstance(data, dict):
        return OpenRouterCreditsData()

    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return OpenRouterCreditsData()

    total_credits = _safe_float(inner.get("total_credits"))
    total_usage = _safe_float(inner.get("total_usage"))

    balance: Optional[float] = None
    if total_credits is not None and total_usage is not None:
        balance = round(total_credits - total_usage, 6)

    return OpenRouterCreditsData(
        total_credits=total_credits,
        total_usage=total_usage,
        balance=balance,
    )


def parse_openrouter_payloads(
    key_payload: Optional[Dict[str, Any]] = None,
    credits_payload: Optional[Dict[str, Any]] = None,
    provider_name: str = "openrouter",
) -> CreditsInfo:
    """Combine parsed /api/v1/auth/key and /api/v1/credits payloads into CreditsInfo."""
    parsed_key = parse_openrouter_key_data(key_payload or {})
    parsed_credits = parse_openrouter_credits_data(credits_payload or {})

    bal = parsed_credits.balance
    tot_usage = parsed_credits.total_usage
    spend_usd = parsed_key.usage if parsed_key.usage is not None else tot_usage

    inner_key: Dict[str, Any] = {}
    if isinstance(key_payload, dict):
        inner_key = (
            key_payload.get("data") if isinstance(key_payload.get("data"), dict) else key_payload
        )
    resets_at, free_daily = _extract_openrouter_key_meta(inner_key, parsed_key.is_free_tier)

    clean_raw_data = {
        "key": _sanitize_raw_payload(key_payload or {}),
        "credits": _sanitize_raw_payload(credits_payload or {}),
    }

    return CreditsInfo(
        supported=True,
        provider=mask_secrets(str(provider_name)),
        balance_usd=bal,
        usage_usd=tot_usage,
        limit_usd=parsed_key.limit,
        limit_remaining_usd=parsed_key.limit_remaining,
        is_free_tier=parsed_key.is_free_tier,
        label=parsed_key.label,
        raw_data=clean_raw_data,
        spend_usd=spend_usd,
        resets_at=resets_at,
        free_model_daily_requests=free_daily,
    )


def format_credits_summary(info: Optional[CreditsInfo], compact: bool = False) -> str:
    """Format CreditsInfo into a human-readable summary string."""
    if info is None:
        return "No credits information available."

    provider_name = mask_secrets(str(info.provider)) if info.provider else "Unknown"

    # Unsupported provider
    if not info.supported:
        status_lbl = mask_secrets(info.label) if info.label else "No Credits API"
        if compact:
            return f"[{provider_name}] Credits API not supported ({status_lbl})"
        lines = [
            f"Provider:        {provider_name}",
            f"Status:          Credits API not supported ({status_lbl})",
        ]
        if info.error:
            lines.append(f"Error:           {mask_secrets(info.error)}")
        return "\n".join(lines)

    # Missing API key
    if info.label == "Missing API key" or (info.error and "no api key" in info.error.lower()):
        if compact:
            return f"[{provider_name}] API key missing or not configured"
        return (
            f"Provider:        {provider_name}\nStatus:          Missing API key or not configured"
        )

    # Free-tier account
    if info.is_free_tier:
        label_str = mask_secrets(info.label) if info.label else "N/A"
        if compact:
            return f"[{provider_name}] Tier: Free tier | Key: {label_str}"
        lines = [
            f"Provider:        {provider_name}",
            "Status:          Supported",
            f"Key Label:       {label_str}",
            "Tier:            Free tier",
        ]
        if info.usage_usd is not None:
            lines.append(f"Usage:           ${info.usage_usd:.2f} USD")
        return "\n".join(lines)

    # Standard supported paid key
    bal_str = f"${info.balance_usd:.2f} USD" if info.balance_usd is not None else "Unavailable"
    usage_str = f"${info.usage_usd:.2f} USD" if info.usage_usd is not None else "Unavailable"
    limit_str = f"${info.limit_usd:.2f} USD" if info.limit_usd is not None else "Unlimited"
    rem_str = (
        f"${info.limit_remaining_usd:.2f} USD"
        if info.limit_remaining_usd is not None
        else "Unlimited"
    )

    if compact:
        rem_clause = f" (Rem: {rem_str})" if info.limit_usd is not None else ""
        key_clause = f" | Key: {mask_secrets(info.label)}" if info.label else ""
        return (
            f"[{provider_name}] Balance: {bal_str} | "
            f"Usage: {usage_str} | "
            f"Key Limit: {limit_str}{rem_clause}{key_clause}"
        )

    lines = [
        f"Provider:        {provider_name}",
        "Status:          Supported",
    ]
    if info.label:
        lines.append(f"Key Label:       {mask_secrets(info.label)}")
    lines.extend(
        [
            f"Account Balance: {bal_str}",
            f"Total Usage:     {usage_str}",
            f"Key Limit:       {limit_str}",
            f"Key Remaining:   {rem_str}",
            "Tier:            Paid",
        ]
    )
    if info.error:
        lines.append(f"Warning:         {mask_secrets(info.error)}")
    return "\n".join(lines)


class WalletService:
    """Provider-agnostic credit and balance management service with TTL caching."""

    def __init__(
        self,
        cache_ttl_seconds: float = DEFAULT_WALLET_CACHE_TTL_S,
        registry: Optional[Type[ProviderRegistry]] = None,
    ) -> None:
        self.cache_ttl_seconds: float = float(cache_ttl_seconds)
        self.registry: Type[ProviderRegistry] = registry or ProviderRegistry
        self._cache: Dict[str, Tuple[float, CreditsInfo]] = {}

    def _build_cache_key(self, provider_name: str, api_key: Optional[str] = None) -> str:
        """Derive a taint-safe cache key using SHA-256 digested key prefix (CodeQL CWE-312)."""
        prov = mask_secrets((provider_name or "").strip()).lower()
        if api_key:
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
            return f"{prov}:{key_hash}"
        return f"{prov}:default"

    def clear_cache(self) -> None:
        """Clear all cached credit responses."""
        self._cache.clear()

    def invalidate(self, provider_name: str, api_key: Optional[str] = None) -> None:
        """Invalidate cache entry for a specific provider and key."""
        k = self._build_cache_key(provider_name, api_key)
        self._cache.pop(k, None)

    def get_credits(
        self,
        provider: Union[str, BaseProvider] = "openrouter",
        provider_cfg: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        force_refresh: bool = False,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> CreditsInfo:
        """Retrieve credits status for provider with in-memory TTL caching."""
        if isinstance(provider, BaseProvider):
            prov_instance = provider
            prov_name = prov_instance.name
        else:
            prov_name = str(provider).strip()
            try:
                prov_instance = self.registry.get(prov_name, provider_cfg)
            except Exception:
                return CreditsInfo(
                    supported=False,
                    provider=mask_secrets(prov_name),
                    label="Unknown provider",
                    error="Provider not registered",
                )

        resolved_key = (
            api_key
            if api_key is not None
            else prov_instance.resolve_api_key(secrets_cfg=secrets_cfg, provider_cfg=provider_cfg)
        )

        cache_key = self._build_cache_key(prov_name, resolved_key)
        now = time.time()

        if not force_refresh and self.cache_ttl_seconds > 0:
            cached_entry = self._cache.get(cache_key)
            if cached_entry is not None:
                cached_time, cached_info = cached_entry
                if (now - cached_time) < self.cache_ttl_seconds:
                    return cached_info

        try:
            info = prov_instance.get_credits(api_key=resolved_key)
        except Exception as exc:
            masked_msg = mask_secrets(str(exc))
            info = CreditsInfo(
                supported=False,
                provider=mask_secrets(prov_name),
                label="Wallet error",
                error=masked_msg[:256],
            )

        if self.cache_ttl_seconds > 0:
            self._cache[cache_key] = (now, info)

        return info

    async def get_credits_async(
        self,
        provider: Union[str, BaseProvider] = "openrouter",
        provider_cfg: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        force_refresh: bool = False,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> CreditsInfo:
        """Asynchronous credit query delegating to a worker thread."""
        return await asyncio.to_thread(
            self.get_credits,
            provider=provider,
            provider_cfg=provider_cfg,
            api_key=api_key,
            force_refresh=force_refresh,
            secrets_cfg=secrets_cfg,
            timeout=timeout,
        )

    def get_summary(
        self,
        info: Optional[CreditsInfo] = None,
        provider: Union[str, BaseProvider] = "openrouter",
        compact: bool = False,
        provider_cfg: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        force_refresh: bool = False,
    ) -> str:
        """Return formatted human-readable credits summary."""
        if info is None:
            info = self.get_credits(
                provider=provider,
                provider_cfg=provider_cfg,
                api_key=api_key,
                force_refresh=force_refresh,
            )
        return format_credits_summary(info, compact=compact)

    @staticmethod
    def _format_key_status_dict(
        info: CreditsInfo, low_budget_threshold_usd: float = 1.0
    ) -> Dict[str, Any]:
        spend_val = info.spend_usd if info.spend_usd is not None else info.usage_usd
        rem_val = (
            info.limit_remaining_usd if info.limit_remaining_usd is not None else info.balance_usd
        )
        low_warning = bool(rem_val is not None and rem_val < float(low_budget_threshold_usd))
        return {
            "supported": info.supported,
            "provider": info.provider,
            "limit": info.limit_usd,
            "limit_usd": info.limit_usd,
            "limit_remaining": rem_val,
            "remaining_usd": rem_val,
            "spend_usd": spend_val,
            "balance_usd": info.balance_usd,
            "resets_at": info.resets_at,
            "free_model_daily_requests": info.free_model_daily_requests,
            "is_free_tier": info.is_free_tier,
            "low_budget_warning": low_warning,
            "label": info.label,
            "error": info.error,
        }

    def get_key_status(
        self,
        provider: Union[str, BaseProvider] = "openrouter",
        provider_cfg: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        force_refresh: bool = False,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
        provider_name: Optional[Union[str, BaseProvider]] = None,
        low_budget_threshold_usd: float = 1.0,
    ) -> Dict[str, Any]:
        """Return structured API key status and budget telemetry dictionary."""
        eff_provider = provider_name if provider_name is not None else provider
        info = self.get_credits(
            provider=eff_provider,
            provider_cfg=provider_cfg,
            api_key=api_key,
            force_refresh=force_refresh,
            secrets_cfg=secrets_cfg,
            timeout=timeout,
        )
        return self._format_key_status_dict(info, low_budget_threshold_usd=low_budget_threshold_usd)

    async def get_key_status_async(
        self,
        provider: Union[str, BaseProvider] = "openrouter",
        provider_cfg: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        force_refresh: bool = False,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
        provider_name: Optional[Union[str, BaseProvider]] = None,
        low_budget_threshold_usd: float = 1.0,
    ) -> Dict[str, Any]:
        """Asynchronous key status query delegating to get_credits_async."""
        eff_provider = provider_name if provider_name is not None else provider
        info = await self.get_credits_async(
            provider=eff_provider,
            provider_cfg=provider_cfg,
            api_key=api_key,
            force_refresh=force_refresh,
            secrets_cfg=secrets_cfg,
            timeout=timeout,
        )
        return self._format_key_status_dict(info, low_budget_threshold_usd=low_budget_threshold_usd)

    @staticmethod
    def format_credits_summary(info: Optional[CreditsInfo], compact: bool = False) -> str:
        """Static facade delegating to module-level format_credits_summary."""
        return format_credits_summary(info, compact=compact)


default_wallet_service = WalletService()


def get_credits(
    provider_name: Union[str, BaseProvider] = "openrouter",
    provider_cfg: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    force_refresh: bool = False,
    secrets_cfg: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> CreditsInfo:
    """Module-level convenience facade querying default_wallet_service."""
    return default_wallet_service.get_credits(
        provider=provider_name,
        provider_cfg=provider_cfg,
        api_key=api_key,
        force_refresh=force_refresh,
        secrets_cfg=secrets_cfg,
        timeout=timeout,
    )


__all__ = [
    "DEFAULT_WALLET_CACHE_TTL_S",
    "CreditsInfo",
    "OpenRouterCreditsData",
    "OpenRouterKeyData",
    "WalletService",
    "default_wallet_service",
    "format_credits_summary",
    "get_credits",
    "parse_openrouter_credits_data",
    "parse_openrouter_key_data",
    "parse_openrouter_payloads",
]

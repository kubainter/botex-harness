# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Z.ai GLM Provider Adapter
===============================
Dedicated provider adapter for the Zhipu AI / Z.ai GLM Coding Plan Lite ($18/mo).
Exposes OpenAI-compatible chat completions at https://api.z.ai/api/coding/paas/v4,
strips OpenRouter-specific extra_body properties, normalizes proprietary
finish reasons (e.g. 'sensitive' -> 'refusal', 'model_context_window_exceeded' -> 'length'),
and parses live account quota limits from https://api.z.ai/api/monitor/usage/quota/limit.
"""

from __future__ import annotations

import copy
import json
import logging
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openai import RateLimitError

try:
    from botex.providers.providers import (
        BaseProvider,
        CreditsInfo,
        NormalizedResponse,
        normalize_response,
    )
    from botex.tools.security import mask_secrets
except (ImportError, ValueError):
    from providers import (  # type: ignore
        BaseProvider,
        CreditsInfo,
        NormalizedResponse,
        normalize_response,
    )
    from security import mask_secrets  # type: ignore

logger = logging.getLogger(__name__)

_OPENROUTER_ONLY_KEYS: Tuple[str, ...] = (
    "transforms",
    "route",
    "provider",
    "data_collection",
    "models",
)

_WINDOW_UNITS_MAP: Dict[int, str] = {
    3: "hours",
    5: "months",
    6: "weeks",
}
_WINDOW_TYPE_MAP: Dict[str, str] = {
    "TOKENS_LIMIT": "tokens",
    "CREDIT_LIMIT": "credits",
    "TIME_LIMIT": "time",
}


def _safe_float(val: Any) -> Optional[float]:
    """Safely convert value to float, handling string numbers or None."""
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any, default: int = 1) -> int:
    """Safely convert value to int."""
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _normalize_reset_time(raw_val: Any) -> Optional[str]:
    """Normalize timestamp or ISO string to UTC ISO-8601 string."""
    if not raw_val:
        return None
    if isinstance(raw_val, (int, float)):
        val = float(raw_val)
        if val <= 0:
            return None
        if val > 1e11:  # Milliseconds epoch
            val = val / 1000.0
        try:
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(raw_val, str):
        val_str = raw_val.strip()
        if not val_str:
            return None
        try:
            num = float(val_str)
            if num <= 0:
                return None
            if num > 1e11:
                num = num / 1000.0
            dt = datetime.fromtimestamp(num, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError, OverflowError):
            return val_str
    return None


def _normalize_quota_window(item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a quota window item across both legacy and modern Z.ai API variants."""
    unit_raw = item.get("unit")
    unit = _WINDOW_UNITS_MAP.get(
        unit_raw, str(unit_raw).lower() if unit_raw is not None else "unknown"
    )
    number = _safe_int(item.get("number"), 1)
    type_raw = str(item.get("type") or item.get("limit_type") or "CREDIT_LIMIT")
    type_norm = _WINDOW_TYPE_MAP.get(type_raw, type_raw.lower())

    limit = _safe_float(item.get("limit"))
    usage = _safe_float(
        item.get("current_usage")
        if item.get("current_usage") is not None
        else (
            item.get("currentUsage") if item.get("currentUsage") is not None else item.get("usage")
        )
    )
    remaining = _safe_float(
        item.get("remaining") if item.get("remaining") is not None else item.get("limit_remaining")
    )
    if remaining is None and limit is not None and usage is not None:
        remaining = max(0.0, limit - usage)

    pct = _safe_float(
        item.get("percentage")
        if item.get("percentage") is not None
        else item.get("percentageValue")
    )
    if pct is None and limit is not None and limit > 0 and usage is not None:
        pct = round((usage / limit) * 100.0, 2)
    elif pct is not None and 0.0 < pct <= 1.0 and limit and limit > 1.0:
        pct = round(pct * 100.0, 2)

    raw_reset = item.get("next_reset_time") or item.get("nextResetTime") or item.get("resets_at")
    resets_at = _normalize_reset_time(raw_reset)

    label = item.get("label")
    if not label:
        if unit_raw == 3 and number == 5:
            label = "5 Hours"
        elif unit_raw == 6 and number == 1:
            label = "Weekly"
        elif unit_raw == 5 and number == 1:
            label = "Monthly"
        else:
            label = f"{number} {unit.capitalize()}"

    return {
        "label": label,
        "unit": unit,
        "number": number,
        "type": type_norm,
        "limit": limit,
        "current_usage": usage,
        "usage": usage,
        "remaining": remaining,
        "percentage": pct,
        "next_reset_time": resets_at,
        "resets_at": resets_at,
    }


class ProviderQuotaExceededError(RuntimeError):
    """Raised when provider credit / quota window (e.g. 5h or weekly) is exhausted."""

    def __init__(
        self, message: str = "Provider quota limit exceeded", status_code: int = 429
    ) -> None:
        super().__init__(mask_secrets(message))
        self.status_code = status_code


class ZaiGLMProvider(BaseProvider):
    """Provider adapter for Z.ai GLM API with live quota parsing and finish_reason mapping."""

    default_base_url: str = "https://api.z.ai/api/coding/paas/v4"
    default_api_key_env: str = "ZAI_API_KEY"
    default_quota_url: str = "https://api.z.ai/api/monitor/usage/quota/limit"

    rate_limit_exceptions: Tuple[type, ...] = (RateLimitError, ProviderQuotaExceededError)

    def __init__(self, name: str = "zai", config: Optional[Dict[str, Any]] = None) -> None:
        import warnings

        warnings.warn(
            "ZaiGLMProvider is deprecated and deactivated to comply with Z.ai ToS.",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(name=name or "zai", config=config)

    @property
    def quota_url(self) -> str:
        return str(self.config.get("quota_url") or self.default_quota_url)

    def get_extra_body(self, allow_non_zdr: bool = False) -> Dict[str, Any]:
        """Return extra_body cleansed of OpenRouter-specific configuration."""
        raw_extra = copy.deepcopy(self.config.get("extra_body") or {})
        for k in _OPENROUTER_ONLY_KEYS:
            raw_extra.pop(k, None)
        return raw_extra

    def prepare_request_payload(
        self,
        model: str,
        messages: list,
        tools: Optional[list] = None,
        max_tokens: int = 4000,
        temperature: float = 0.2,
        allow_non_zdr: bool = False,
        timeout: Optional[float] = None,
        **extra_kwargs: Any,
    ) -> Dict[str, Any]:
        """Prepare OpenAI-compatible request payload sanitized for Z.ai."""
        cleaned_extra = {k: v for k, v in extra_kwargs.items() if k not in _OPENROUTER_ONLY_KEYS}
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "extra_body": self.get_extra_body(allow_non_zdr=allow_non_zdr),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if timeout is not None:
            payload["timeout"] = timeout
        payload.update(cleaned_extra)
        return payload

    def normalize_response(self, response: Any) -> NormalizedResponse:
        """Normalize response and remap Z.ai proprietary finish_reasons."""
        # Handle dict or object responses
        norm = normalize_response(response)

        # Inspect raw finish reason for Z.ai specific values
        raw_fr = ""
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices and isinstance(choices[0], dict):
                raw_fr = str(choices[0].get("finish_reason") or "").lower()
        else:
            choices = getattr(response, "choices", None) or []
            if choices:
                raw_fr = str(getattr(choices[0], "finish_reason", None) or "").lower()

        # Remap Z.ai specific finish reasons if needed
        if raw_fr == "sensitive":
            object.__setattr__(norm, "finish_reason", "refusal")
            object.__setattr__(norm, "raw_finish_reason", "sensitive")
        elif raw_fr == "model_context_window_exceeded":
            object.__setattr__(norm, "finish_reason", "length")
            object.__setattr__(norm, "raw_finish_reason", "model_context_window_exceeded")

        return norm

    def fetch_catalog(
        self,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> List[Dict[str, Any]]:
        """Fetch model list from Z.ai /models endpoint if API key is configured."""
        resolved_key = self.resolve_api_key(explicit_key=api_key)
        if not resolved_key:
            return []
        return super().fetch_catalog(api_key=api_key, timeout=timeout)

    def get_credits(
        self,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
        raise_on_exceeded: bool = False,
    ) -> CreditsInfo:
        """Query live quota status from Z.ai monitor usage endpoint."""
        resolved_key = self.resolve_api_key(explicit_key=api_key)
        if not resolved_key:
            return CreditsInfo(
                supported=True,
                provider=self.name,
                label="Missing API key",
                error="No API key configured",
                units="quota",
            )

        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Accept": "application/json",
        }
        quota_data: Dict[str, Any] = {}
        query_error: Optional[str] = None

        try:
            req = urllib.request.Request(self.quota_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_body = resp.read().decode("utf-8")
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    quota_data = parsed
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                query_error = "Z.ai auth failed: HTTP 401 Unauthorized"
            elif exc.code == 429:
                query_error = "Z.ai rate limit: HTTP 429"
            else:
                query_error = f"Z.ai HTTP Error {exc.code}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            query_error = f"Z.ai network error: {mask_secrets(str(reason))[:128]}"
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            query_error = f"Z.ai invalid response: {mask_secrets(str(exc))[:128]}"
        except Exception as exc:
            query_error = f"Z.ai query failed: {mask_secrets(str(exc))[:128]}"

        if query_error:
            info = CreditsInfo(
                supported=True,
                provider=self.name,
                error=query_error,
                units="quota",
            )
            if raise_on_exceeded and "429" in query_error:
                raise ProviderQuotaExceededError(query_error)
            return info

        # Parse limits array from payload
        inner_data = quota_data.get("data")
        limits_raw = (
            quota_data.get("limits")
            or (inner_data.get("limits") if isinstance(inner_data, dict) else None)
            or []
        )
        if not isinstance(limits_raw, list):
            limits_raw = []

        norm_windows = [
            _normalize_quota_window(item) for item in limits_raw if isinstance(item, dict)
        ]

        window_5h = next(
            (w for w in norm_windows if w.get("unit") == "hours" and w.get("number") == 5),
            None,
        )
        window_weekly = next(
            (w for w in norm_windows if w.get("unit") == "weeks" and w.get("number") == 1),
            None,
        )

        primary_window = window_5h or window_weekly or (norm_windows[0] if norm_windows else {})
        limit_val = primary_window.get("limit")
        usage_val = primary_window.get("current_usage")
        pct_val = primary_window.get("percentage")
        resets_at = primary_window.get("next_reset_time")

        limit_num = float(limit_val) if limit_val is not None else None
        usage_num = float(usage_val) if usage_val is not None else None
        remaining_num = (
            primary_window.get("remaining")
            if primary_window.get("remaining") is not None
            else (
                max(0.0, limit_num - usage_num)
                if (limit_num is not None and usage_num is not None)
                else None
            )
        )

        # Check if quota exceeded in either window
        quota_exceeded = False
        exceeded_msg = ""
        for name, win in (("5h", window_5h), ("weekly", window_weekly)):
            if win:
                w_lim = win.get("limit")
                w_usage = win.get("current_usage")
                if w_lim is not None and w_usage is not None and float(w_lim) > 0:
                    if float(w_usage) >= float(w_lim):
                        quota_exceeded = True
                        exceeded_msg = f"Z.ai {name} quota exceeded: {w_usage}/{w_lim}"
                        break

        info = CreditsInfo(
            supported=True,
            provider=self.name,
            limit_usd=limit_num,
            usage_usd=usage_num,
            limit_remaining_usd=remaining_num,
            spend_usd=usage_num,
            resets_at=str(resets_at) if resets_at else None,
            label=f"GLM Quota {pct_val}%" if pct_val is not None else "GLM Quota",
            error=exceeded_msg if quota_exceeded else None,
            raw_data=quota_data,
            units="quota",
            quota_windows=tuple(norm_windows),
        )

        if quota_exceeded and raise_on_exceeded:
            raise ProviderQuotaExceededError(exceeded_msg)

        return info

    def check_quota(
        self,
        credits: Optional[CreditsInfo] = None,
        api_key: Optional[str] = None,
    ) -> bool:
        """Check live quota, raising ProviderQuotaExceededError if exhausted."""
        cred = credits or self.get_credits(api_key=api_key)
        if cred.error and "exceeded" in cred.error.lower():
            raise ProviderQuotaExceededError(cred.error)
        return True

    def is_retryable(self, exc: BaseException) -> bool:
        """Determine if an exception is a transient or quota error safe to failover."""
        if isinstance(exc, ProviderQuotaExceededError):
            return True
        return super().is_retryable(exc)

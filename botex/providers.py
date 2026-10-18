# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Provider Adapter
======================
Provider-neutral request/response layer. The engine loop consumes
``NormalizedResponse`` instead of SDK objects, so a provider using
different stop-reason names or usage shapes only changes this module —
never the tool loop.

Security boundary: provider-scoped request fields (``extra_body``,
attribution headers) and API-key resolution live here; secrets never enter
normalized responses or error strings.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

from openai import AsyncOpenAI

try:
    from .config import app_headers
    from .security import mask_secrets, sever_taint_numeric
except (ImportError, ValueError):
    from config import app_headers
    from security import mask_secrets, sever_taint_numeric


# Canonical finish kind constants for CodeQL CWE-312 compliance
_FINISH_KIND: Dict[str, str] = {
    "tool_calls": "tool_call",
    "tool_use": "tool_call",
    "stop": "completed",
    "end_turn": "completed",
    "stop_sequence": "completed",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "refusal",
    "refusal": "refusal",
    "error": "provider_error",
}
_VALID_FINISH_KIND_MAP = _FINISH_KIND

# Canonical HTTP error status mappings for CodeQL CWE-312 compliance
_HTTP_ERROR_MAP: Dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized (invalid or expired API key)",
    403: "Forbidden (insufficient permissions)",
    404: "Endpoint Not Found",
    408: "Request Timeout",
    429: "Too Many Requests (rate limit exceeded)",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class NormalizedUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None


@dataclass(frozen=True)
class NormalizedResponse:
    """Provider-neutral view of one model turn.

    ``finish_reason`` is one of: tool_call | completed | length | refusal |
    provider_error | unknown. ``raw_finish_reason`` keeps the provider's
    original string for diagnostics.
    """

    finish_reason: str
    raw_finish_reason: str
    content: str
    tool_calls: Tuple[NormalizedToolCall, ...] = ()
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    generation_id: str = ""
    model: str = ""


@dataclass(frozen=True)
class GenerationStats:
    """Parsed OpenRouter /api/v1/generation?id=... telemetry record."""

    generation_id: str
    model: str = ""
    total_cost: Optional[float] = None
    queue_time_ms: Optional[float] = None
    generation_time_ms: Optional[float] = None
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    provider_name: str = ""


@dataclass(frozen=True)
class CreditsInfo:
    """Unified provider balance and credits status."""

    supported: bool
    provider: str
    balance_usd: Optional[float] = None
    usage_usd: Optional[float] = None
    limit_usd: Optional[float] = None
    limit_remaining_usd: Optional[float] = None
    is_free_tier: bool = False
    label: Optional[str] = None
    error: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)
    spend_usd: Optional[float] = None
    resets_at: Optional[str] = None
    free_model_daily_requests: Optional[int] = None

    def __post_init__(self) -> None:
        if self.provider is not None:
            object.__setattr__(self, "provider", mask_secrets(str(self.provider)))
        if self.label is not None:
            object.__setattr__(self, "label", mask_secrets(str(self.label)))
        if self.error is not None:
            object.__setattr__(self, "error", mask_secrets(str(self.error)))
        if self.resets_at is not None:
            object.__setattr__(self, "resets_at", mask_secrets(str(self.resets_at)))
        if self.raw_data:
            object.__setattr__(self, "raw_data", _sanitize_raw_payload(self.raw_data))


def _safe_float(val: Any) -> Optional[float]:
    """Safely convert value to float, handling string numbers or None."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to int, rejecting booleans and mock objects."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _extract_openrouter_key_meta(
    key_data: Dict[str, Any], is_free_tier: bool
) -> Tuple[Optional[str], Optional[int]]:
    """Extract resets_at and free_model_daily_requests from OpenRouter auth/key data."""
    if not isinstance(key_data, dict) or not key_data:
        return None, None
    raw_reset = (
        key_data.get("resets_at") or key_data.get("limit_reset") or key_data.get("expires_at")
    )
    resets_at = (
        mask_secrets(str(raw_reset))
        if raw_reset is not None and isinstance(raw_reset, (str, int, float))
        else None
    )
    raw_daily = key_data.get("free_model_daily_requests")
    if raw_daily is None and isinstance(key_data.get("rate_limit"), dict):
        raw_daily = key_data["rate_limit"].get("requests")
    free_daily = _safe_int(raw_daily, -1)
    if free_daily < 0:
        free_daily = 50 if is_free_tier else 1000
    return resets_at, free_daily


# OpenRouter meta-router slugs that manage routing internally and must not
# be combined with an explicit extra_body["models"] failover array.
OPENROUTER_META_ROUTERS = frozenset(
    {
        "openrouter/auto",
        "openrouter/auto-beta",
        "openrouter/pareto-code",
        "openrouter/free",
        "openrouter/fusion",
        "openrouter/bodybuilder",
    }
)

_REASONING_SLUG_TOKENS = (
    "r1",
    "o1",
    "o3",
    "o4",
    "qwq",
    "thinking",
    "reason",
    "claude-3.7",
    "claude-3-7",
    "claude-4",
    "sonnet-4",
    "opus-4",
    "gemini-2.5",
    "deepseek-v3.2",
)


def is_reasoning_capable_slug(model: str) -> bool:
    """Heuristic check whether a model slug supports OpenRouter's reasoning parameter."""
    slug = (model or "").strip().lower()
    if not slug:
        return False
    if slug in OPENROUTER_META_ROUTERS:
        return True
    return any(tok in slug for tok in _REASONING_SLUG_TOKENS)


def _sanitize_raw_payload(payload: Any) -> Any:
    """Recursively scrub API keys and sensitive tokens from raw debug payloads."""
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


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access: works for SDK objects and raw dicts alike."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _content_to_text(content: Any) -> str:
    """Flatten provider content into plain text.

    Handles plain strings and Anthropic-style content block lists
    (``[{"type": "text", "text": ...}, ...]``); anything else becomes an
    empty string rather than crashing the loop."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = [
            str(_get(block, "text", ""))
            for block in content
            if _get(block, "type") in (None, "text", "output_text")
        ]
        return "".join(parts)
    return ""


def normalize_response(response: Any) -> NormalizedResponse:
    """Flatten an OpenAI-compatible response into the common model."""
    choices = _get(response, "choices") or []
    choice = choices[0] if choices else None
    msg = _get(choice, "message")
    raw_fr = _get(choice, "finish_reason") or "unknown"

    tool_calls = tuple(
        NormalizedToolCall(
            id=_get(tc, "id") or f"call_{i}",
            name=_get(_get(tc, "function"), "name") or "",
            arguments=_get(_get(tc, "function"), "arguments") or "",
        )
        for i, tc in enumerate(_get(msg, "tool_calls") or [])
    )
    # A response with no choices is a provider protocol violation, not a
    # model turn — mark it so the engine classifies it as provider_error
    # instead of burning retry turns on an empty message.
    if choice is None:
        finish_kind = "provider_error"
        raw_fr = "no_choices"
    else:
        finish_kind = (
            _VALID_FINISH_KIND_MAP.get("tool_calls", "tool_call")
            if tool_calls
            else _VALID_FINISH_KIND_MAP.get(str(raw_fr), "unknown")
        )

    usage = _get(response, "usage")
    prompt_details = _get(usage, "prompt_tokens_details") if usage else None
    comp_details = _get(usage, "completion_tokens_details") if usage else None
    raw_cached = (
        (_get(prompt_details, "cached_tokens", 0) if prompt_details else 0)
        or (_get(usage, "native_tokens_cached", 0) if usage else 0)
        or 0
    )
    raw_cache_write = (
        (_get(prompt_details, "cache_write_tokens", 0) if prompt_details else 0)
        or (_get(usage, "cache_write_tokens", 0) if usage else 0)
        or (_get(usage, "native_tokens_cache_write", 0) if usage else 0)
        or 0
    )
    raw_cost = _get(usage, "cost") if usage else None
    if raw_cost is None and usage is not None:
        raw_cost = _get(usage, "total_cost")

    nusage = NormalizedUsage(
        prompt_tokens=_safe_int(_get(usage, "prompt_tokens", 0) if usage else 0, 0),
        completion_tokens=_safe_int(_get(usage, "completion_tokens", 0) if usage else 0, 0),
        reasoning_tokens=_safe_int(
            _get(comp_details, "reasoning_tokens", 0) if comp_details else 0, 0
        ),
        cached_tokens=_safe_int(raw_cached, 0),
        cache_write_tokens=_safe_int(raw_cache_write, 0),
        cost_usd=_safe_float(raw_cost),
    )
    raw_id = _get(response, "id")
    gen_id = mask_secrets(raw_id) if isinstance(raw_id, str) and raw_id else ""
    raw_model = _get(response, "model")
    resp_model = mask_secrets(raw_model) if isinstance(raw_model, str) and raw_model else ""

    return NormalizedResponse(
        finish_reason=finish_kind,
        raw_finish_reason=str(raw_fr),
        content=_content_to_text(_get(msg, "content")),
        tool_calls=tool_calls,
        usage=nusage,
        generation_id=gen_id,
        model=resp_model,
    )


def sanitize_provider_error(exc: BaseException, max_chars: int = 1024) -> str:
    """Masked, length-bounded provider error text safe for result dicts,
    MCP responses and analytics — never contains secrets or raw payloads."""
    text = mask_secrets(str(exc))
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


class BaseProvider(ABC):
    """Abstract base class for model API providers."""

    default_base_url: str = "https://openrouter.ai/api/v1"
    default_api_key_env: str = "OPENROUTER_API_KEY"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None) -> None:
        self.name: str = (name or "").strip()
        self.config: Dict[str, Any] = copy.deepcopy(config) if config else {}

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or self.default_base_url)

    @property
    def api_key_env(self) -> str:
        return str(self.config.get("api_key_env") or self.default_api_key_env)

    def resolve_api_key(
        self,
        explicit_key: Optional[str] = None,
        *args: Any,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        explicit: Optional[str] = None,
        provider_cfg: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """Resolve API key using explicit > env > secrets precedence.

        Supports explicit_key and legacy explicit argument names.
        Returns empty string "" if no key is found.
        """
        key = explicit_key if explicit_key is not None else explicit
        if key:
            return str(key)

        sec: Optional[Dict[str, Any]] = secrets_cfg
        cfg: Optional[Dict[str, Any]] = provider_cfg
        if len(args) == 2:
            cfg = cfg or args[0]
            sec = sec or args[1]
        elif len(args) == 1:
            arg_dict = args[0]
            if isinstance(arg_dict, dict):
                if sec is None and any(k.endswith("_api_key") for k in arg_dict):
                    sec = arg_dict
                elif cfg is None and ("api_key_env" in arg_dict or "base_url" in arg_dict):
                    cfg = arg_dict
                elif sec is None:
                    sec = arg_dict

        key_env = (cfg.get("api_key_env") if cfg else None) or self.api_key_env
        env_val = os.environ.get(key_env)
        if env_val:
            return str(env_val)

        sec_dict = sec or {}
        clean_name = self.name.strip().lower()
        norm_name = clean_name.replace("-", "_")
        found = (
            sec_dict.get(f"{self.name}_api_key")
            or sec_dict.get(f"{clean_name}_api_key")
            or sec_dict.get(f"{norm_name}_api_key")
            or ""
        )
        return str(found) if found else ""

    def get_default_headers(self) -> Optional[Dict[str, str]]:
        """Return provider-specific HTTP headers."""
        if self.config.get("attribution_headers", False):
            return app_headers()
        return None

    def get_client(
        self,
        api_key: Optional[str] = None,
        client_factory: Any = AsyncOpenAI,
        provider_cfg: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Instantiate an AsyncOpenAI-compatible client with injected factory."""
        merged_cfg = {**self.config, **(provider_cfg or {})}
        base_url = merged_cfg.get("base_url") or self.base_url

        send_headers = merged_cfg.get("attribution_headers")
        if send_headers is not None:
            default_headers = app_headers() if send_headers else None
        else:
            default_headers = self.get_default_headers()

        resolved_key = (
            api_key if api_key is not None else self.resolve_api_key(provider_cfg=merged_cfg)
        )
        return client_factory(
            base_url=base_url,
            api_key=resolved_key or "",
            default_headers=default_headers,
        )

    def build_client(self, *args: Any, **kwargs: Any) -> Any:
        """Alias for get_client preserving polymorphic dispatch for subclasses."""
        return self.get_client(*args, **kwargs)

    def get_extra_body(self, allow_non_zdr: bool = False) -> Dict[str, Any]:
        """Return a deep copy of extra_body payload dictionary."""
        return copy.deepcopy(self.config.get("extra_body") or {})

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
        """Prepare kwargs dictionary for client.chat.completions.create()."""
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
        payload.update(extra_kwargs)
        return payload

    def normalize_response(self, response: Any) -> NormalizedResponse:
        """Normalize provider response into standard NormalizedResponse."""
        return normalize_response(response)

    def sanitize_error(self, exc: BaseException, max_chars: int = 1024) -> str:
        """Sanitize error messages with secret masking and bounded length."""
        return sanitize_provider_error(exc, max_chars=max_chars)

    def get_model_capabilities(self, model: str) -> Dict[str, Any]:
        """Return model capability overrides declared in config."""
        caps = self.config.get("capabilities") or {}
        return caps.get(model) or {}

    def fetch_catalog(
        self,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> List[Dict[str, Any]]:
        """Fetch model list from provider's /models endpoint."""
        url = self.base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resolved_key = self.resolve_api_key(explicit_key=api_key)
        if resolved_key:
            req.add_header("Authorization", f"Bearer {resolved_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("data", [])

    def get_credits(self, api_key: Optional[str] = None) -> CreditsInfo:
        """Query provider credits. Default implementation returns unsupported."""
        return CreditsInfo(supported=False, provider=self.name)

    def get_generation_stats(
        self,
        generation_id: str,
        api_key: Optional[str] = None,
        timeout: float = 2.0,
    ) -> Optional[GenerationStats]:
        """Query single generation telemetry record if supported by the provider."""
        return None


class _GenerationNotIndexedError(Exception):
    """Internal signal raised when OpenRouter /generation returns 404 (not yet indexed)."""


class OpenRouterProvider(BaseProvider):
    """Adapter for OpenRouter API endpoints."""

    default_base_url: str = "https://openrouter.ai/api/v1"
    default_api_key_env: str = "OPENROUTER_API_KEY"

    def __init__(self, name: str = "openrouter", config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(name=name, config=config)
        if "free" in self.name.lower() and "api_key_env" not in self.config:
            self._default_api_key_env = "OPENROUTER_API_FREE_KEY"
        else:
            self._default_api_key_env = "OPENROUTER_API_KEY"

    @property
    def api_key_env(self) -> str:
        return str(self.config.get("api_key_env") or self._default_api_key_env)

    def get_default_headers(self) -> Optional[Dict[str, str]]:
        if self.config.get("attribution_headers", True):
            return app_headers()
        return None

    def get_extra_body(
        self,
        allow_non_zdr: bool = False,
        model: str = "",
        has_tools: bool = False,
        has_response_format: bool = False,
        fallback_models: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        raw_extra = self.config.get("extra_body")
        extra: Dict[str, Any] = copy.deepcopy(raw_extra) if isinstance(raw_extra, dict) else {}
        prov_prefs = extra.get("provider")
        if not isinstance(prov_prefs, dict):
            prov_prefs = {}
            extra["provider"] = prov_prefs
        zdr_enabled = bool(self.config.get("zdr", {}).get("enabled", True))
        if allow_non_zdr or not zdr_enabled:
            prov_prefs["data_collection"] = "allow"
        else:
            prov_prefs["data_collection"] = "deny"
        prov_prefs.setdefault("allow_fallbacks", True)
        if "order" not in prov_prefs and "sort" not in prov_prefs:
            prov_prefs["sort"] = "throughput"
        if has_tools or has_response_format:
            prov_prefs.setdefault("require_parameters", True)
        if "transforms" not in extra:
            extra["transforms"] = []
        if "usage" not in extra:
            extra["usage"] = {"include": True}
        # Only attach reasoning budget when model supports reasoning or when
        # require_parameters is not active (otherwise OpenRouter rejects
        # non-reasoning endpoints with HTTP 404).
        if prov_prefs.get("require_parameters") and model and not is_reasoning_capable_slug(model):
            extra.pop("reasoning", None)
        elif "reasoning" not in extra:
            extra["reasoning"] = {"max_tokens": 4000}

        if fallback_models and model and model.strip().lower() not in OPENROUTER_META_ROUTERS:
            primary_clean = model.strip()
            chain: List[str] = [primary_clean]
            for cand in fallback_models:
                c_clean = (cand or "").strip()
                if (
                    c_clean
                    and c_clean not in chain
                    and c_clean.lower() not in OPENROUTER_META_ROUTERS
                ):
                    chain.append(c_clean)
                if len(chain) >= 3:
                    break
            if len(chain) > 1:
                extra["models"] = chain
        return extra

    def get_credits(self, api_key: Optional[str] = None, timeout: float = 10.0) -> CreditsInfo:
        resolved_key = self.resolve_api_key(explicit_key=api_key)
        if not resolved_key:
            return CreditsInfo(
                supported=True,
                provider=self.name,
                label="Missing API key",
                error="No API key configured",
            )
        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Accept": "application/json",
        }
        key_data: Dict[str, Any] = {}
        key_error: Optional[str] = None
        key_url = f"{self.base_url.rstrip('/')}/auth/key"
        try:
            req_key = urllib.request.Request(key_url, headers=headers)
            with urllib.request.urlopen(req_key, timeout=timeout) as resp:
                raw_body = resp.read().decode("utf-8")
                parsed_json = json.loads(raw_body)
                if isinstance(parsed_json, dict):
                    inner = parsed_json.get("data")
                    key_data = inner if isinstance(inner, dict) else parsed_json
                    if not isinstance(key_data, dict):
                        key_data = {}
        except urllib.error.HTTPError as exc:
            err_desc = _HTTP_ERROR_MAP.get(exc.code, f"HTTP Error {exc.code}")
            key_error = f"auth/key HTTP {exc.code}: {err_desc}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            key_error = f"auth/key network error: {mask_secrets(str(reason))[:128]}"
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            key_error = f"auth/key invalid response: {mask_secrets(str(exc))[:128]}"
        except Exception as exc:
            key_error = f"auth/key query failed: {mask_secrets(str(exc))[:128]}"

        credits_data: Dict[str, Any] = {}
        credits_error: Optional[str] = None
        credits_url = f"{self.base_url.rstrip('/')}/credits"
        try:
            req_cred = urllib.request.Request(credits_url, headers=headers)
            with urllib.request.urlopen(req_cred, timeout=timeout) as resp:
                raw_body = resp.read().decode("utf-8")
                parsed_json = json.loads(raw_body)
                if isinstance(parsed_json, dict):
                    inner = parsed_json.get("data")
                    credits_data = inner if isinstance(inner, dict) else parsed_json
                    if not isinstance(credits_data, dict):
                        credits_data = {}
        except urllib.error.HTTPError as exc:
            err_desc = _HTTP_ERROR_MAP.get(exc.code, f"HTTP Error {exc.code}")
            credits_error = f"credits HTTP {exc.code}: {err_desc}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            credits_error = f"credits network error: {mask_secrets(str(reason))[:128]}"
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            credits_error = f"credits invalid response: {mask_secrets(str(exc))[:128]}"
        except Exception as exc:
            credits_error = f"credits query failed: {mask_secrets(str(exc))[:128]}"

        # If both failed with errors and returned no data
        if (
            not key_data
            and not credits_data
            and (key_error is not None or credits_error is not None)
        ):
            combined_err = " | ".join(filter(None, [key_error, credits_error])) or "Query failed"
            return CreditsInfo(
                supported=False,
                provider=self.name,
                label="Failed to fetch credits",
                error=mask_secrets(combined_err),
            )

        tot_credits = _safe_float(credits_data.get("total_credits"))
        tot_usage = _safe_float(credits_data.get("total_usage"))
        bal = (
            round(tot_credits - tot_usage, 6)
            if (tot_credits is not None and tot_usage is not None)
            else None
        )

        key_usage = _safe_float(key_data.get("usage"))
        key_limit = _safe_float(key_data.get("limit"))
        raw_rem = _safe_float(key_data.get("limit_remaining"))
        if raw_rem is not None:
            key_rem = raw_rem
        elif key_limit is not None and key_usage is not None:
            key_rem = max(0.0, key_limit - key_usage)
        else:
            key_rem = None

        raw_label = key_data.get("label")
        label = mask_secrets(str(raw_label)) if raw_label is not None else None
        is_free_tier = bool(key_data.get("is_free_tier", False))
        resets_at, free_daily = _extract_openrouter_key_meta(key_data, is_free_tier)

        usage_usd = tot_usage if tot_usage is not None else None
        spend_usd = key_usage if key_usage is not None else usage_usd
        secondary_err = key_error or credits_error

        clean_raw_data = {
            "key": _sanitize_raw_payload(key_data),
            "credits": _sanitize_raw_payload(credits_data),
        }

        return CreditsInfo(
            supported=True,
            provider=self.name,
            balance_usd=bal,
            usage_usd=usage_usd,
            limit_usd=key_limit,
            limit_remaining_usd=key_rem,
            is_free_tier=is_free_tier,
            label=label,
            error=secondary_err,
            raw_data=clean_raw_data,
            spend_usd=spend_usd,
            resets_at=resets_at,
            free_model_daily_requests=free_daily,
        )

    def _fetch_generation_stats_raw(
        self,
        generation_id: str,
        api_key: Optional[str] = None,
        timeout: float = 2.0,
    ) -> Optional[GenerationStats]:
        gen_clean = (generation_id or "").strip()
        if not gen_clean:
            return None
        resolved_key = self.resolve_api_key(explicit_key=api_key)
        if not resolved_key:
            return None
        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Accept": "application/json",
        }
        encoded_id = urllib.parse.quote(gen_clean, safe="")
        gen_url = f"{self.base_url.rstrip('/')}/generation?id={encoded_id}"
        try:
            req = urllib.request.Request(gen_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_body = resp.read().decode("utf-8")
                parsed_json = json.loads(raw_body)
                if not isinstance(parsed_json, dict):
                    return None
                inner = parsed_json.get("data")
                data = inner if isinstance(inner, dict) else parsed_json
                if not isinstance(data, dict) or not data:
                    return None
                total_cost = _safe_float(
                    data.get("total_cost")
                    if data.get("total_cost") is not None
                    else data.get("usage")
                )
                queue_ms = _safe_float(
                    data.get("latency")
                    if data.get("latency") is not None
                    else data.get("queue_time")
                )
                gen_ms = _safe_float(data.get("generation_time"))
                t_prompt = _safe_int(
                    data.get("native_tokens_prompt")
                    if data.get("native_tokens_prompt") is not None
                    else data.get("tokens_prompt"),
                    0,
                )
                t_comp = _safe_int(
                    data.get("native_tokens_completion")
                    if data.get("native_tokens_completion") is not None
                    else data.get("tokens_completion"),
                    0,
                )
                t_cached = _safe_int(
                    data.get("native_tokens_cached")
                    if data.get("native_tokens_cached") is not None
                    else data.get("cached_tokens"),
                    0,
                )
                t_cache_write = _safe_int(
                    data.get("native_tokens_cache_write")
                    if data.get("native_tokens_cache_write") is not None
                    else data.get("cache_write_tokens"),
                    0,
                )
                return GenerationStats(
                    generation_id=mask_secrets(str(data.get("id") or gen_clean)),
                    model=mask_secrets(str(data.get("model") or "")),
                    total_cost=total_cost,
                    queue_time_ms=queue_ms,
                    generation_time_ms=gen_ms,
                    tokens_prompt=t_prompt,
                    tokens_completion=t_comp,
                    cached_tokens=t_cached,
                    cache_write_tokens=t_cache_write,
                    provider_name=mask_secrets(str(data.get("provider_name") or "")),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _GenerationNotIndexedError(gen_clean) from exc
            return None
        except Exception:
            return None

    def get_generation_stats(
        self,
        generation_id: str,
        api_key: Optional[str] = None,
        timeout: float = 2.0,
    ) -> Optional[GenerationStats]:
        try:
            return self._fetch_generation_stats_raw(
                generation_id=generation_id, api_key=api_key, timeout=timeout
            )
        except _GenerationNotIndexedError:
            return None


class NvidiaProvider(BaseProvider):
    """Adapter for NVIDIA NIM OpenAI-compatible API."""

    default_base_url: str = "https://integrate.api.nvidia.com/v1"
    default_api_key_env: str = "NVIDIA_API_KEY"

    def __init__(self, name: str = "nvidia", config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(name=name, config=config)

    def get_default_headers(self) -> Optional[Dict[str, str]]:
        if self.config.get("attribution_headers", False):
            return app_headers()
        return None

    def get_extra_body(self, allow_non_zdr: bool = False) -> Dict[str, Any]:
        return copy.deepcopy(self.config.get("extra_body") or {})

    def resolve_api_key(
        self,
        explicit_key: Optional[str] = None,
        *args: Any,
        secrets_cfg: Optional[Dict[str, Any]] = None,
        explicit: Optional[str] = None,
        provider_cfg: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        key = explicit_key if explicit_key is not None else explicit
        if key:
            return str(key)
        key_env = (provider_cfg.get("api_key_env") if provider_cfg else None) or self.config.get(
            "api_key_env"
        )
        if key_env and os.environ.get(key_env):
            return str(os.environ[key_env])
        if os.environ.get("NVIDIA_API_KEY"):
            return str(os.environ["NVIDIA_API_KEY"])
        if os.environ.get("NVAPI_KEY"):
            return str(os.environ["NVAPI_KEY"])
        return super().resolve_api_key(
            explicit_key=explicit_key,
            *args,
            secrets_cfg=secrets_cfg,
            explicit=explicit,
            provider_cfg=provider_cfg,
            **kwargs,
        )

    def get_credits(self, api_key: Optional[str] = None) -> CreditsInfo:
        return CreditsInfo(
            supported=False,
            provider=self.name,
            label="NVIDIA NIM (No Credits API)",
        )


class GenericOpenAIProvider(BaseProvider):
    """Adapter for generic OpenAI-compatible API endpoints."""

    default_base_url: str = "https://openrouter.ai/api/v1"
    default_api_key_env: str = "OPENROUTER_API_KEY"

    def __init__(self, name: str = "generic", config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(name=name, config=config)
        if self.name.lower() in ("openai", "generic_openai") and "api_key_env" not in self.config:
            self._default_api_key_env = "OPENAI_API_KEY"
            self._default_base_url = "https://api.openai.com/v1"
        else:
            self._default_api_key_env = "OPENROUTER_API_KEY"
            self._default_base_url = "https://openrouter.ai/api/v1"

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or self._default_base_url)

    @property
    def api_key_env(self) -> str:
        return str(self.config.get("api_key_env") or self._default_api_key_env)

    def get_default_headers(self) -> Optional[Dict[str, str]]:
        if self.config.get("attribution_headers", False):
            return app_headers()
        return None

    def get_credits(self, api_key: Optional[str] = None) -> CreditsInfo:
        return CreditsInfo(supported=False, provider=self.name)


class ProviderRegistry:
    """Registry managing provider implementations and instantiation."""

    _registry: Dict[str, Type[BaseProvider]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: Type[BaseProvider]) -> None:
        """Register a provider class under a normalized name."""
        if not (isinstance(provider_cls, type) and issubclass(provider_cls, BaseProvider)):
            raise TypeError("Provider class must inherit from BaseProvider")
        cls._registry[name.lower().strip()] = provider_cls

    @classmethod
    def get_provider_class(cls, name: str) -> Type[BaseProvider]:
        """Resolve provider class with exact match, substring heuristic, or fallback."""
        key = name.lower().strip()
        if key in cls._registry:
            return cls._registry[key]
        if "openrouter" in key and "openrouter" in cls._registry:
            return cls._registry["openrouter"]
        if "nvidia" in key and "nvidia" in cls._registry:
            return cls._registry["nvidia"]
        if "openai" in key:
            target = (
                cls._registry.get("openai")
                or cls._registry.get("generic_openai")
                or cls._registry.get("generic")
            )
            if target:
                return target
        return cls._registry.get("generic", GenericOpenAIProvider)

    @classmethod
    def get(
        cls,
        name: str,
        provider_cfg: Optional[Dict[str, Any]] = None,
        strict: bool = False,
    ) -> BaseProvider:
        """Instantiate a provider adapter for the given name and config."""
        key = name.lower().strip()
        if key in cls._registry:
            return cls._registry[key](name=name, config=provider_cfg)

        if "openrouter" in key and "openrouter" in cls._registry:
            return cls._registry["openrouter"](name=name, config=provider_cfg)
        if "nvidia" in key and "nvidia" in cls._registry:
            return cls._registry["nvidia"](name=name, config=provider_cfg)
        if "openai" in key:
            target = (
                cls._registry.get("openai")
                or cls._registry.get("generic_openai")
                or cls._registry.get("generic")
            )
            if target:
                return target(name=name, config=provider_cfg)

        # For unknown providers: if strict or provider_cfg is None, raise ValueError
        if strict or provider_cfg is None:
            raise ValueError(f"Unknown provider: '{name}'")

        fallback_cls = cls._registry.get("generic", GenericOpenAIProvider)
        return fallback_cls(name=name, config=provider_cfg)

    create = get
    get_provider = get

    @classmethod
    def list_providers(cls) -> List[str]:
        """Return sorted list of explicitly registered provider names."""
        return sorted(cls._registry.keys())

    @classmethod
    def reset(cls) -> None:
        """Reset registry to default built-in providers."""
        cls._registry.clear()
        cls._register_defaults()

    @classmethod
    def _register_defaults(cls) -> None:
        cls.register("openrouter", OpenRouterProvider)
        cls.register("openrouter-free", OpenRouterProvider)
        cls.register("nvidia", NvidiaProvider)
        cls.register("generic_openai", GenericOpenAIProvider)
        cls.register("generic", GenericOpenAIProvider)
        cls.register("openai", GenericOpenAIProvider)


# Initialize default registrations
ProviderRegistry._register_defaults()
default_registry = ProviderRegistry


# Backward-compatible module facade functions
def build_async_client(
    provider_name: str,
    provider_cfg: Dict[str, Any],
    api_key: Optional[str] = None,
    client_factory: Any = AsyncOpenAI,
) -> Any:
    """Construct the provider client via ProviderRegistry."""
    provider = ProviderRegistry.get(provider_name, provider_cfg or {})
    return provider.get_client(api_key=api_key, client_factory=client_factory)


def resolve_api_key(
    explicit: Optional[str],
    provider_name: str,
    provider_cfg: Dict[str, Any],
    secrets_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Key resolution order: explicit param -> provider env var -> secrets."""
    provider = ProviderRegistry.get(provider_name, provider_cfg or {})
    return provider.resolve_api_key(explicit_key=explicit, secrets_cfg=secrets_cfg)


async def fetch_openrouter_generations_async(
    generation_ids: List[str],
    provider_name: str = "openrouter",
    provider_cfg: Optional[Dict[str, Any]] = None,
    api_key: str = "",
    timeout: float = 1.5,
    retry_delay_s: float = 0.4,
) -> Dict[str, Any]:
    """Fetch and aggregate OpenRouter /api/v1/generation records in parallel.

    Performs one short retry (``retry_delay_s``) for IDs that return HTTP 404
    due to OpenRouter's asynchronous analytics indexing lag.
    """
    unique_ids: List[str] = []
    for gid in generation_ids or []:
        g_clean = (gid or "").strip() if isinstance(gid, str) else ""
        if g_clean and g_clean not in unique_ids:
            unique_ids.append(g_clean)
    if not unique_ids:
        return {}

    try:
        provider = ProviderRegistry.get(provider_name or "openrouter", provider_cfg or {})
    except Exception as exc:
        msg = f"OpenRouter provider lookup failed: {sanitize_provider_error(exc)}\n"
        sys.stderr.write(sever_taint_numeric(msg))
        return {}

    if not isinstance(provider, OpenRouterProvider):
        return {}

    resolved_key = provider.resolve_api_key(explicit_key=api_key or None)
    if not resolved_key:
        return {}

    async def _query_one(gid: str) -> Tuple[str, Optional[GenerationStats], bool]:
        try:
            stat = await asyncio.to_thread(
                provider._fetch_generation_stats_raw,
                gid,
                resolved_key,
                timeout,
            )
            return gid, stat, False
        except _GenerationNotIndexedError:
            return gid, None, True
        except Exception as exc:
            err_msg = f"OpenRouter generation query error ({mask_secrets(str(gid))}): {sanitize_provider_error(exc)}\n"
            sys.stderr.write(sever_taint_numeric(err_msg))
            return gid, None, False

    first_pass = await asyncio.gather(*(_query_one(gid) for gid in unique_ids))
    records: List[GenerationStats] = []
    retry_ids: List[str] = []
    for gid, stat, needs_retry in first_pass:
        if stat is not None:
            records.append(stat)
        elif needs_retry:
            retry_ids.append(gid)

    if retry_ids and retry_delay_s > 0:
        await asyncio.sleep(retry_delay_s)
        second_pass = await asyncio.gather(*(_query_one(gid) for gid in retry_ids))
        for _gid, stat, _needs_retry in second_pass:
            if stat is not None:
                records.append(stat)

    if not records:
        return {}

    costs = [r.total_cost for r in records if r.total_cost is not None]
    queues = [r.queue_time_ms for r in records if r.queue_time_ms is not None]
    gen_times = [r.generation_time_ms for r in records if r.generation_time_ms is not None]
    sum_cached = sum(r.cached_tokens for r in records)
    sum_cache_write = sum(r.cache_write_tokens for r in records)
    models_used = sorted({r.model for r in records if r.model})

    return {
        "ok": True,
        "records_count": len(records),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "queue_time_ms": round(sum(queues), 2) if queues else None,
        "generation_time_ms": round(sum(gen_times), 2) if gen_times else None,
        "cached_tokens": sum_cached,
        "cache_write_tokens": sum_cache_write,
        "models_used": models_used,
    }

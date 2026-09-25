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
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

try:
    from .config import app_headers
    from .security import mask_secrets
except (ImportError, ValueError):
    from config import app_headers
    from security import mask_secrets


# Provider stop-reason names -> normalized finish kind. Covers the
# OpenAI-compatible dialect plus the common Anthropic-style names so a
# provider-specific API can be plugged in later without loop changes.
_FINISH_KIND = {
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
            "tool_call" if tool_calls else _FINISH_KIND.get(raw_fr, "unknown")
        )

    usage = _get(response, "usage")
    prompt_details = _get(usage, "prompt_tokens_details") if usage else None
    comp_details = _get(usage, "completion_tokens_details") if usage else None
    nusage = NormalizedUsage(
        prompt_tokens=_get(usage, "prompt_tokens", 0) or 0,
        completion_tokens=_get(usage, "completion_tokens", 0) or 0,
        reasoning_tokens=_get(comp_details, "reasoning_tokens", 0) or 0,
        cached_tokens=_get(prompt_details, "cached_tokens", 0) or 0,
    )
    return NormalizedResponse(
        finish_reason=finish_kind,
        raw_finish_reason=raw_fr,
        content=_content_to_text(_get(msg, "content")),
        tool_calls=tool_calls,
        usage=nusage,
    )


def build_async_client(
    provider_name: str,
    provider_cfg: Dict[str, Any],
    api_key: str,
    client_factory=AsyncOpenAI,
):
    """Construct the provider client.

    Attribution headers (X-Title/HTTP-Referer) are an OpenRouter
    convention — they are sent only for OpenRouter, or when a provider
    config explicitly opts in via ``attribution_headers: true``. They are
    never sent to NVIDIA or other endpoints.

    ``client_factory`` is injectable so tests can substitute a fake client
    without touching this module.
    """
    send_headers = provider_cfg.get(
        "attribution_headers", provider_name == "openrouter")
    return client_factory(
        base_url=provider_cfg.get("base_url") or "https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers=app_headers() if send_headers else None,
    )


def resolve_api_key(
    explicit: Optional[str],
    provider_name: str,
    provider_cfg: Dict[str, Any],
    secrets_cfg: Dict[str, Any],
) -> str:
    """Key resolution order: explicit param -> provider env var -> secrets."""
    if explicit:
        return explicit
    key_env = provider_cfg.get("api_key_env") or "OPENROUTER_API_KEY"
    return (
        os.environ.get(key_env)
        or secrets_cfg.get(f"{provider_name}_api_key")
        or ""
    )


def sanitize_provider_error(exc: BaseException, max_chars: int = 1024) -> str:
    """Masked, length-bounded provider error text safe for result dicts,
    MCP responses and analytics — never contains secrets or raw payloads."""
    text = mask_secrets(str(exc))
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text

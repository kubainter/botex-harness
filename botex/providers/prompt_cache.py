# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Prompt-cache policy seam (arena-botex-cache contract v1.0 / plan v1.2)
=====================================================================
Self-contained module: no imports from engine/chat. Three responsibilities:

1. ``resolve_policy`` — decide whether/how prompt caching applies for one
   (model, provider) pair. Order matters: free-tier check FIRST (the
   ``openrouter-free`` substring trap swallows naive ``"openrouter" in name``
   matches), then capability default, then operator override.
2. ``apply_prompt_cache`` — the single payload seam. Returns a TRANSFORMED
   COPY (never mutates the caller's history: checkpoint/fallback safety).
3. ``map_usage`` — per-provider usage-field mapping with strict
   null-vs-zero semantics (missing field = ``None`` "not reported",
   never a fabricated ``0``).

Plus ``should_disable_cache`` — the pure state helper for the reactive
one-way disable (>=3 consecutive misses at a cacheable prefix length).
"""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Model-aware capability tables (local, operator-tunable)
# ---------------------------------------------------------------------------
# Providers whose upstreams honour Anthropic-style ``cache_control``
# breakpoints, mapped to fnmatch globs over the model slug. Matching is by
# EXACT provider name — never substring ("openrouter-free" must not match
# "openrouter"; the free-tier gate runs first anyway).
EPHEMERAL_PROVIDER_GLOBS: Dict[str, Tuple[str, ...]] = {
    "openrouter": ("anthropic/*", "deepseek/*"),
    "anthropic": ("*",),
}

# Providers with server-side implicit caching (Gemini context caching,
# OpenAI automatic prompt caching, NIM KV-reuse). Zero flag injection —
# they reuse the provider-side cache without payload markers.
IMPLICIT_PROVIDERS = frozenset(
    {
        "google",
        "aistudio",
        "gemini",
        "openai",
        "generic_openai",
        "nvidia",
        "nim",
    }
)

# Anthropic hard limit on cache_control breakpoints per request.
DEFAULT_MAX_BREAKPOINTS = 4
# Fallback minimum cacheable prefix when no model rule matches.
DEFAULT_MIN_PREFIX_TOKENS = 1024
# Reactive one-way disable: consecutive misses required before shutdown.
CACHE_MISS_STREAK_DISABLE = 3
# Conservative token estimate: chars/4 with a 1.3 density margin (code/JSON
# tokenize denser than prose). Calibrate against real usage telemetry.
_TOKEN_CHARS_DIVISOR = 4.0
_TOKEN_SAFETY_MARGIN = 1.3

_GEMINI_VERSION_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class CachePolicy:
    """Final per-(model, provider) prompt-cache gating decision."""

    enabled: bool  # free-tier + capability + operator override combined
    mode: str  # "anthropic_ephemeral" | "implicit" | "none"
    min_prefix_tokens: int  # model-aware minimum cacheable prefix
    max_breakpoints: int = DEFAULT_MAX_BREAKPOINTS  # anthropic hard limit


def _min_prefix_for_model(model_id: str) -> int:
    """Model-aware minimum cacheable prefix (verified in provider docs).

    Anthropic: Sonnet/Opus 1024, Haiku 2048. Gemini: 2.5 family 2048,
    3.x+/next-gen 4096. Everything else falls back to 1024.
    """
    slug = (model_id or "").strip().lower()
    m = _GEMINI_VERSION_RE.search(slug)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        if major >= 3:
            return 4096
        if major == 2 and minor >= 5:
            return 2048
    if "haiku" in slug:
        return 2048
    return DEFAULT_MIN_PREFIX_TOKENS


def resolve_policy(
    model_id: str,
    provider_name: str,
    is_free_model: bool,
    operator_override: Optional[bool] = None,
    *,
    ephemeral_globs: Optional[Dict[str, Tuple[str, ...]]] = None,
    implicit_providers: Optional[Sequence[str]] = None,
    min_prefix: Optional[int] = None,
) -> CachePolicy:
    """Resolve the prompt-cache policy for one (model, provider) pair.

    Order (matters — substring trap): ``is_free_model`` FIRST, then the
    capability default (``openrouter`` AND ``anthropic/*``/``deepseek/*``
    family -> ``anthropic_ephemeral``; implicit providers
    gemini/openai/nim -> ``implicit`` with zero flag injection; everything
    else -> ``none``), then ``operator_override`` wins when not ``None``.

    A force-enable on a ``none`` provider promotes to ``implicit`` (never
    injects Anthropic flags onto non-capable upstreams). ``min_prefix``
    overrides the model-aware table for experimentation.
    """
    slug = (model_id or "").strip().lower()
    prov = (provider_name or "").strip().lower()
    globs = ephemeral_globs if ephemeral_globs is not None else EPHEMERAL_PROVIDER_GLOBS
    implicit = (
        frozenset(p.strip().lower() for p in implicit_providers)
        if implicit_providers is not None
        else IMPLICIT_PROVIDERS
    )
    mode = "none"
    enabled = False
    if not is_free_model:
        if any(fnmatch.fnmatchcase(slug, g) for g in globs.get(prov, ())):
            mode = "anthropic_ephemeral"
            enabled = True
        elif prov in implicit:
            mode = "implicit"
            enabled = True
    if operator_override is not None:
        enabled = bool(operator_override)
        if enabled and mode == "none":
            mode = "implicit"  # force-enable never injects anthropic flags
    return CachePolicy(
        enabled=enabled,
        mode=mode,
        min_prefix_tokens=(
            int(min_prefix) if min_prefix is not None else _min_prefix_for_model(slug)
        ),
    )


# ---------------------------------------------------------------------------
# Payload seam
# ---------------------------------------------------------------------------

_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}
_TAIL_ROLES = frozenset({"user", "assistant"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iter_parts(content: Any) -> List[Dict[str, Any]]:
    return [p for p in content if isinstance(p, dict)] if isinstance(content, list) else []


def _message_chars(msg: Dict[str, Any]) -> int:
    """Approximate serialized size of one message in chars."""
    total = 0
    content = msg.get("content")
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for part in _iter_parts(content):
            text = part.get("text")
            if isinstance(text, str):
                total += len(text)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            fn = _get(tc, "function")
            name = _get(fn, "name")
            args = _get(fn, "arguments")
            if isinstance(name, str):
                total += len(name)
            if isinstance(args, str):
                total += len(args)
    return total


def _estimate_prefix_tokens(messages: Sequence[Dict[str, Any]]) -> int:
    """chars/4 x 1.3 — deliberately conservative for the min-prefix gate."""
    chars = sum(_message_chars(m) for m in messages if isinstance(m, dict))
    return int(chars / _TOKEN_CHARS_DIVISOR * _TOKEN_SAFETY_MARGIN)


def _strip_cache_control(messages: List[Dict[str, Any]], stats: Dict[str, Any]) -> None:
    """Remove every ``cache_control`` marker; collapse one-element text-part
    lists back to a plain string (defensive: some OpenAI-compatible upstreams
    400 on parts-lists where a string is expected)."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "cache_control" in msg:
            msg.pop("cache_control", None)
            stats["stripped"] = True
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        removed = False
        for part in _iter_parts(content):
            if "cache_control" in part:
                part.pop("cache_control", None)
                removed = True
        if removed:
            stats["stripped"] = True
        if (
            len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and isinstance(content[0].get("text"), str)
        ):
            msg["content"] = content[0]["text"]
            stats["collapsed"] = True


def _stamp_breakpoint(msg: Dict[str, Any]) -> bool:
    """Attach an ephemeral cache_control to a message's last content block.

    String content is lifted to the canonical parts-list shape. Returns
    False when there is no stampable content (e.g. ``content: None`` on a
    pure tool-call assistant turn).
    """
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL_EPHEMERAL)}
        ]
        return True
    if isinstance(content, list) and content:
        for part in reversed(content):
            if isinstance(part, dict):
                part["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
                return True
    return False


def _find_tail_index(messages: List[Dict[str, Any]], head_idx: Optional[int]) -> Optional[int]:
    """Last user/assistant message with stampable content — the boundary
    stable across context-guard tool folding (tool-role messages get folded;
    user/assistant never do)."""
    for i in range(len(messages) - 1, -1, -1):
        if i == head_idx:
            continue
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") not in _TAIL_ROLES:
            continue
        content = msg.get("content")
        if isinstance(content, str) or (isinstance(content, list) and content):
            return i
    return None


def apply_prompt_cache(
    messages: List[Dict[str, Any]],
    policy: CachePolicy,
    *,
    fold_occurred: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(transformed_copy, stats)`` — NEVER mutates the input.

    - ``enabled=False`` or ``mode='none'``: copy with ``cache_control``
      stripped and one-element text-part lists collapsed to ``str``;
      ``stats['stripped']`` marks the strip path.
    - ``mode='anthropic_ephemeral'``: head-always breakpoint on the system
      message plus a tail breakpoint on the last tool-fold-stable
      user/assistant boundary (skipped when ``fold_occurred`` — a fold
      mutates earlier tool messages, so a fresh tail entry would be a wasted
      1.25x write that the next fold invalidates). Stamping is gated on the
      conservative ``approx_prefix_tokens >= policy.min_prefix_tokens``.
    - ``mode='implicit'``: semantically unchanged copy, ``stats['implicit']``.
    """
    msgs = copy.deepcopy(messages) if isinstance(messages, list) else []
    stats: Dict[str, Any] = {
        "mode": policy.mode,
        "breakpoints": 0,
        "tail_stamped": False,
        "skipped_fold": False,
        "approx_prefix_tokens": _estimate_prefix_tokens(msgs),
        "stripped": False,
        "collapsed": False,
        "implicit": False,
    }
    if policy.enabled and policy.mode == "implicit":
        stats["implicit"] = True
        return msgs, stats
    if not policy.enabled or policy.mode != "anthropic_ephemeral":
        _strip_cache_control(msgs, stats)
        stats["stripped"] = True  # the strip path itself was applied
        return msgs, stats
    # anthropic_ephemeral: normalize first so repeated calls stay idempotent.
    _strip_cache_control(msgs, stats)
    if stats["approx_prefix_tokens"] < policy.min_prefix_tokens:
        return msgs, stats
    head_idx: Optional[int] = None
    for i, msg in enumerate(msgs):
        if isinstance(msg, dict) and msg.get("role") == "system":
            if _stamp_breakpoint(msg):
                head_idx = i
                stats["breakpoints"] += 1
            break
    if fold_occurred:
        stats["skipped_fold"] = True
    elif stats["breakpoints"] < policy.max_breakpoints:
        tail_idx = _find_tail_index(msgs, head_idx)
        if tail_idx is not None and _stamp_breakpoint(msgs[tail_idx]):
            stats["breakpoints"] += 1
            stats["tail_stamped"] = True
    return msgs, stats


# ---------------------------------------------------------------------------
# Usage mapping (fields verified in provider docs 2026-11-07)
# ---------------------------------------------------------------------------
# Dotted paths = nested lookup. Per-provider ordered candidates; the first
# reported field wins. Absent field -> None ("not reported"), never 0-fake.
_READ_KEYS = {
    "openrouter": (
        "cached_tokens",
        "cache_read_input_tokens",
        "prompt_tokens_details.cached_tokens",
        "native_tokens_cached",
    ),
    "gemini": (
        "total_cached_tokens",
        "cached_tokens",
        "prompt_tokens_details.cached_tokens",
    ),
    "nvidia": (
        "prompt_tokens_details.cached_tokens",
        "input_tokens_details.cached_tokens",
        "cached_tokens",
    ),
    "anthropic": ("cache_read_input_tokens", "cached_tokens"),
}
_WRITE_KEYS = {
    "openrouter": (
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens_details.cache_write_tokens",
        "native_tokens_cache_write",
    ),
    "gemini": (),
    "nvidia": (),
    "anthropic": ("cache_creation_input_tokens", "cache_write_tokens"),
}
_COST_KEYS = ("cost", "cost_details.upstream_inference_cost", "total_cost")


def _provider_family(provider_name: str) -> str:
    prov = (provider_name or "").strip().lower()
    if "openrouter" in prov:
        return "openrouter"
    if "nvidia" in prov or "nim" in prov:
        return "nvidia"
    if "google" in prov or "aistudio" in prov or "gemini" in prov:
        return "gemini"
    if "anthropic" in prov or "claude" in prov:
        return "anthropic"
    return "generic"


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for key in dotted.split("."):
        cur = _get(cur, key)
        if cur is None:
            return None
    return cur


def _num(value: Any) -> Optional[float]:
    """Numeric value or None — a real ``0`` passes through, ``bool`` and
    non-numerics are rejected."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _pick(usage: Any, keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        val = _num(_dig(usage, key))
        if val is not None:
            return val
    return None


def map_usage(usage: Optional[Dict[str, Any]], provider_name: str) -> Dict[str, Optional[float]]:
    """Map one provider's raw usage payload to cache telemetry.

    Per-provider shapes (verified in docs): OpenRouter ``cached_tokens`` /
    ``cache_write_tokens`` / ``cost`` (``cost_details.upstream_inference_cost``
    fallback, Anthropic passthrough shapes as fallback); Gemini
    ``total_cached_tokens`` with write ``None`` (implicit — conceptually
    nonexistent); NVIDIA NIM ``prompt_tokens_details.cached_tokens`` (or
    ``input_tokens_details.cached_tokens``) with write unreported; Anthropic
    direct ``cache_read_input_tokens`` / ``cache_creation_input_tokens``.

    Missing field = ``None`` ("not reported"), NEVER a fabricated ``0`` —
    the one-way disable logic depends on distinguishing "reported zero"
    from "not reported".
    """
    out: Dict[str, Optional[float]] = {
        "cache_read": None,
        "cache_write": None,
        "authoritative_cost": None,
    }
    if not usage:
        return out
    family = _provider_family(provider_name)
    read_keys = _READ_KEYS.get(family, ())
    write_keys = _WRITE_KEYS.get(family, ())
    if family == "generic":  # defensive union for unregistered providers
        read_keys = tuple(dict.fromkeys(k for keys in _READ_KEYS.values() for k in keys))
        write_keys = tuple(dict.fromkeys(k for keys in _WRITE_KEYS.values() for k in keys))
    out["cache_read"] = _pick(usage, read_keys)
    out["cache_write"] = _pick(usage, write_keys)
    out["authoritative_cost"] = _pick(usage, _COST_KEYS)
    return out


def should_disable_cache(miss_streak: int, prefix_tokens: int, min_prefix_tokens: int) -> bool:
    """Reactive one-way disable gate (per task, not global).

    Returns True only when ``miss_streak >= 3`` consecutive turns reported
    ``cache_read == 0`` AND ``cache_write in (None, 0)`` while the prompt
    prefix was cacheable (``prefix_tokens >= min_prefix_tokens``).

    Cold-start exemption: turn 1 ALWAYS reads 0 — the cache is empty by
    definition — so callers must not count turn 1 into ``miss_streak``;
    streak counting starts at turn 2 where a hit was possible. The
    ``min_prefix_tokens`` gate additionally excludes turns whose prefix was
    too small to cache at all, so a short task can never trip the disable.
    """
    return int(miss_streak) >= CACHE_MISS_STREAK_DISABLE and int(prefix_tokens) >= int(
        min_prefix_tokens
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
import json
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from .config import app_headers, load_config, resolve_provider
    from .providers import resolve_api_key
    from .security import mask_secrets
except (ImportError, ValueError):
    from config import app_headers, load_config, resolve_provider
    from providers import resolve_api_key
    from security import mask_secrets

_VALID_TASK_TYPES_MAP: Dict[str, str] = {
    "coding": "coding",
    "review": "review",
    "architecture": "architecture",
    "fast": "fast",
    "general": "general",
}

_VALID_TIERS_MAP: Dict[str, str] = {
    "auto": "auto",
    "high": "high",
    "standard": "standard",
    "budget": "budget",
}

_FLAGSHIP_HINTS = (
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-3.7-sonnet",
    "claude-3.5-sonnet",
    "gpt-5",
    "gpt-4o",
    "o3",
    "o4-mini",
    "qwen3-coder",
    "qwen-2.5-coder",
    "deepseek-r1",
    "deepseek-v3",
    "gemini-2.5-pro",
    "gemini-1.5-pro",
    "pareto-code",
    "coder",
)

_LIGHTWEIGHT_HINTS = (
    "nano",
    "lite",
    "mini",
    "flash",
    "haiku",
    "8b",
    "7b",
    "3b",
    "1b",
)


def _classify_model_tier(slug: str, pin: float, pout: float) -> str:
    """Classify a model slug and per-1M pricing into high, standard, or budget tier."""
    s_low = slug.lower()
    total_1m = pin + pout
    if any(k in s_low for k in _LIGHTWEIGHT_HINTS):
        return "budget"
    if any(
        k in s_low
        for k in ("opus", "gpt-5.4", "claude-sonnet-4", "o3", "gemini-2.5-pro", "deepseek-r1")
    ):
        return "high"
    if total_1m >= 6.0:
        return "high"
    if any(k in s_low for k in _FLAGSHIP_HINTS):
        return "standard"
    if total_1m <= 1.0:
        return "budget"
    return "standard"


def get_recommended_models(
    task_type: str = "coding",
    limit: int = 5,
    include_free: bool = False,
    denylist_filter: Optional[Any] = None,
    provider: str = "openrouter",
    profile: Optional[str] = None,
    require_tools: bool = True,
    tier: str = "auto",
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetches model recommendations from OpenRouter's /api/v1/models endpoint using
    authenticated headers (when an API key is configured) and task/tier heuristics.

    ``include_free=True`` admits ``:free`` variants — they may log/train on
    prompts (non-ZDR), so they stay excluded by default.
    """
    norm_task = _VALID_TASK_TYPES_MAP.get(str(task_type or "").strip().lower(), "coding")
    norm_tier = _VALID_TIERS_MAP.get(str(tier or "").strip().lower(), "auto")

    cfg = load_config()
    if denylist_filter is None:
        try:
            from .denylist import DenylistFilter

            denylist_filter = DenylistFilter(cfg)
        except Exception:
            try:
                from denylist import DenylistFilter

                denylist_filter = DenylistFilter(cfg)
            except Exception:
                denylist_filter = None

    pname, pcfg = resolve_provider(provider or "openrouter")
    base_url = str((pcfg or {}).get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{base_url}/models" if not base_url.endswith("/models") else base_url

    headers: Dict[str, str] = {"Accept": "application/json"}
    try:
        headers.update(app_headers())
    except Exception:
        pass

    resolved_key = resolve_api_key(
        api_key, pname or "openrouter", pcfg or {}, cfg.get("secrets", {})
    )
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return [{"error": f"Failed to fetch models from OpenRouter: {mask_secrets(str(e))}"}]

    models = data.get("data", []) if isinstance(data, dict) else []

    scored_models: List[Dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("id") or "").strip()
        if not slug:
            continue

        if denylist_filter is not None and not denylist_filter.is_allowed(
            slug, provider=pname or provider, profile=profile
        ):
            continue

        params = m.get("supported_parameters")
        param_set = (
            {str(p).lower() for p in params} if isinstance(params, (list, tuple, set)) else set()
        )
        if require_tools and params is not None and isinstance(params, (list, tuple, set)):
            if "tools" not in param_set:
                continue

        pricing = m.get("pricing") or {}
        context_length = int(m.get("context_length") or 0)

        try:
            pin = float(pricing.get("prompt", 0)) * 1_000_000
            pout = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            continue

        if pin < 0 or pout < 0:
            continue

        # ZDR (Zero Data Retention) enforcement:
        # OpenRouter's free fallback providers (like Google AI Studio) may collect data.
        # To strictly enforce ZDR, we exclude slugs matching the pre-flight
        # gate's deny patterns (':free', 'openrouter/free') unless the caller
        # opted in via include_free, as well as offline ':batch' endpoints.
        s_low = slug.lower()
        if ":batch" in s_low:
            continue
        if (":free" in s_low or "openrouter/free" in s_low) and not include_free:
            continue

        model_tier = _classify_model_tier(slug, pin, pout)
        if norm_tier != "auto" and model_tier != norm_tier:
            continue

        cost_score = pin + pout

        if norm_task in ("coding", "review", "architecture"):
            if context_length < 32000:
                continue
            is_flagship = any(h in s_low for h in _FLAGSHIP_HINTS)
            is_lightweight = any(lw in s_low for lw in _LIGHTWEIGHT_HINTS)
            if not is_flagship:
                cost_score = cost_score * 1.5 + 100.0
            if norm_task in ("architecture", "review"):
                # Complex reasoning/review favors flagship & reasoning models over lightweight/budget models
                if is_lightweight:
                    cost_score = (cost_score + 50.0) * 2.0
                if "reasoning" in param_set or any(
                    r in s_low for r in ("claude", "gpt-5", "o3", "deepseek-r1", "gemini-2.5-pro")
                ):
                    cost_score *= 0.5
            else:
                # General coding favors full-scale coding/flagship models over lightweight flash/mini/nano variants
                if is_lightweight:
                    cost_score += 20.0
        elif norm_task == "fast":
            if context_length < 16000:
                continue
            if any(lw in s_low for lw in _LIGHTWEIGHT_HINTS):
                cost_score *= 0.6

        capabilities: List[str] = []
        for cap in ("tools", "reasoning", "structured_outputs", "response_format"):
            if cap in param_set:
                capabilities.append(cap)

        scored_models.append(
            {
                "id": slug,
                "name": m.get("name", slug),
                "tier": model_tier,
                "context_length": context_length,
                "capabilities": capabilities,
                "pricing": {"prompt_1M": round(pin, 4), "completion_1M": round(pout, 4)},
                "_score": cost_score,
            }
        )

    # Sort by lowest composite score
    scored_models.sort(key=lambda x: x["_score"])

    # Remove internal sorting score
    for m in scored_models:
        del m["_score"]

    return scored_models[:limit]


# Alias for backward compatibility and MCP tool parity
recommend_models = get_recommended_models

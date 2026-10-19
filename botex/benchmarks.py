# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
import json
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from .config import app_headers, benchmarks_cache_file, load_config, resolve_provider
    from .providers import resolve_api_key
    from .recipes import load_recipe_metadata
    from .security import mask_secrets
except (ImportError, ValueError):
    from config import app_headers, benchmarks_cache_file, load_config, resolve_provider
    from providers import resolve_api_key
    from recipes import load_recipe_metadata
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


def fetch_openrouter_benchmarks(
    cache_ttl_hours: float = 24.0,
    api_key: Optional[str] = None,
    provider_name: str = "openrouter",
) -> Dict[str, Dict[str, float]]:
    """Fetch OpenRouter model benchmarks with 24h atomic disk caching.

    Returns a mapping of normalized model slug -> {"coding_index": float, "tokens_per_second": float}.
    Fallback: Stale cache on network/rate-limit error; empty dict if unavailable.
    """
    cache_path = benchmarks_cache_file()
    now = time.time()

    # 1. Try reading from cache if fresh
    if cache_path.is_file():
        try:
            mtime = cache_path.stat().st_mtime
            if (now - mtime) < (cache_ttl_hours * 3600):
                cached_data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached_data, dict):
                    return cached_data
        except Exception:
            pass

    # 2. Cache is missing or stale -> fetch from API
    cfg = load_config()
    pname, pcfg = resolve_provider(provider_name)
    base_url = str((pcfg or {}).get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    if base_url.endswith("/models"):
        base_url = base_url[:-7]
    url = f"{base_url}/benchmarks"

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
            raw_data = json.loads(resp.read().decode("utf-8"))
            items = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data
            parsed: Dict[str, Dict[str, float]] = {}
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    slug = (
                        str(
                            item.get("model_id")
                            or item.get("id")
                            or item.get("slug")
                            or item.get("model")
                            or ""
                        )
                        .strip()
                        .lower()
                    )
                    if not slug:
                        continue
                    speed = item.get("speed")
                    tps = 0.0
                    if isinstance(speed, dict):
                        tps = float(
                            speed.get("tokens_per_second")
                            or speed.get("throughput")
                            or speed.get("tps")
                            or 0.0
                        )
                    elif isinstance(speed, (int, float)):
                        tps = float(speed)
                    else:
                        tps = float(
                            item.get("tokens_per_second")
                            or item.get("throughput")
                            or item.get("tps")
                            or 0.0
                        )

                    coding = float(
                        item.get("coding_index")
                        or item.get("code")
                        or item.get("overall_index")
                        or item.get("score")
                        or 0.0
                    )
                    parsed[slug] = {
                        "coding_index": coding,
                        "tokens_per_second": tps,
                    }

            # Atomic save to cache
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = cache_path.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            tmp_file.replace(cache_path)
            return parsed
    except Exception:
        # Fallback to stale cache if it exists
        if cache_path.is_file():
            try:
                stale_data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(stale_data, dict):
                    return stale_data
            except Exception:
                pass
        return {}


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
    recipe: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetches model recommendations from OpenRouter's /api/v1/models endpoint using
    authenticated headers, live benchmark weights, and task/recipe/tier heuristics.

    ``include_free=True`` admits ``:free`` variants — they may log/train on
    prompts (non-ZDR), so they stay excluded by default.
    """
    norm_task = _VALID_TASK_TYPES_MAP.get(str(task_type or "").strip().lower(), "coding")
    norm_tier = _VALID_TIERS_MAP.get(str(tier or "").strip().lower(), "auto")

    recipe_meta = load_recipe_metadata(recipe) if recipe else {}
    if norm_tier == "auto" and recipe_meta.get("preferred_tier"):
        norm_tier = _VALID_TIERS_MAP.get(str(recipe_meta["preferred_tier"]).strip().lower(), "auto")

    w_meta = recipe_meta.get("weights", {}) if isinstance(recipe_meta.get("weights"), dict) else {}
    w_coding = float(
        w_meta.get(
            "coding",
            0.6 if norm_task == "coding" else (0.1 if norm_task == "fast" else 0.5),
        )
    )
    w_speed = float(
        w_meta.get(
            "speed",
            0.2 if norm_task == "coding" else (0.7 if norm_task == "fast" else 0.2),
        )
    )
    w_cost = float(
        w_meta.get(
            "cost",
            0.2 if norm_task == "coding" else (0.2 if norm_task == "fast" else 0.3),
        )
    )

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

    benchmarks_data = fetch_openrouter_benchmarks(
        api_key=resolved_key, provider_name=pname or provider
    )

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

        bm_entry = benchmarks_data.get(s_low) or benchmarks_data.get(s_low.split(":")[0])
        bm_info = None
        if bm_entry and (bm_entry.get("coding_index") or bm_entry.get("tokens_per_second")):
            coding_idx = float(bm_entry.get("coding_index") or 50.0)
            tps = float(bm_entry.get("tokens_per_second") or 40.0)
            bm_info = {"coding_index": round(coding_idx, 1), "tokens_per_second": round(tps, 1)}

            norm_coding = (coding_idx / 100.0) if coding_idx > 1.0 else coding_idx
            norm_speed = min(tps / 150.0, 2.0)
            norm_cost = 1.0 / (pin + pout + 0.1)
            composite_score = (
                (w_coding * norm_coding) + (w_speed * norm_speed) + (w_cost * norm_cost)
            )
            sort_score = -composite_score * 100.0
        else:
            sort_score = pin + pout
            if norm_task in ("coding", "review", "architecture"):
                if context_length < 32000:
                    continue
                is_flagship = any(h in s_low for h in _FLAGSHIP_HINTS)
                is_lightweight = any(lw in s_low for lw in _LIGHTWEIGHT_HINTS)
                if not is_flagship:
                    sort_score = sort_score * 1.5 + 100.0
                if norm_task in ("architecture", "review"):
                    if is_lightweight:
                        sort_score = (sort_score + 50.0) * 2.0
                    if "reasoning" in param_set or any(
                        r in s_low
                        for r in ("claude", "gpt-5", "o3", "deepseek-r1", "gemini-2.5-pro")
                    ):
                        sort_score *= 0.5
                else:
                    if is_lightweight:
                        sort_score += 20.0
            elif norm_task == "fast":
                if context_length < 16000:
                    continue
                if any(lw in s_low for lw in _LIGHTWEIGHT_HINTS):
                    sort_score *= 0.6

        capabilities: List[str] = []
        for cap in ("tools", "reasoning", "structured_outputs", "response_format"):
            if cap in param_set:
                capabilities.append(cap)

        entry_dict = {
            "id": slug,
            "name": m.get("name", slug),
            "tier": model_tier,
            "context_length": context_length,
            "capabilities": capabilities,
            "pricing": {"prompt_1M": round(pin, 4), "completion_1M": round(pout, 4)},
            "_score": sort_score,
        }
        if bm_info:
            entry_dict["benchmark"] = bm_info
        scored_models.append(entry_dict)

    # Sort by lowest composite score
    scored_models.sort(key=lambda x: x["_score"])

    # Remove internal sorting score
    for m in scored_models:
        del m["_score"]

    return scored_models[:limit]


# Alias for backward compatibility and MCP tool parity
recommend_models = get_recommended_models

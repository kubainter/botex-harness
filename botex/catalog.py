# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Dynamic Model Catalog & Availability
==========================================
Unified model catalog service providing metadata parsing, pricing normalization,
tool-calling capability inspection, Zero Data Retention (ZDR) policy checks,
and TTL-backed caching with graceful stale fallback.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from .config import PROJECT_ROOT
    from .providers import BaseProvider, ProviderRegistry
    from .security import mask_secrets
    from .wallet import _sanitize_raw_payload
except (ImportError, ValueError):
    from config import PROJECT_ROOT
    from providers import BaseProvider, ProviderRegistry
    from security import mask_secrets
    from wallet import _sanitize_raw_payload

CACHE_FILE_DEFAULT: Path = PROJECT_ROOT / ".model_pricing.json"
DEFAULT_TTL_HOURS: float = 24.0

_ON_UNKNOWN_MAP: Dict[str, str] = {
    "allow": "allow",
    "deny": "deny",
}


@dataclass(frozen=True)
class ModelMetadata:
    """Rich metadata representation for a model in provider catalogs."""

    id: str
    name: str = ""
    context_length: int = 0
    prompt_price_per_m: Optional[float] = None
    completion_price_per_m: Optional[float] = None
    supports_tools: Optional[bool] = None
    is_zdr_compliant: bool = True
    raw_entry: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_free(self) -> bool:
        """Convenience property identifying free-tier models."""
        slug_lower = self.id.lower()
        if ":free" in slug_lower or "openrouter/free" in slug_lower:
            return True
        if self.prompt_price_per_m is not None and self.completion_price_per_m is not None:
            return self.prompt_price_per_m == 0.0 and self.completion_price_per_m == 0.0
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize metadata for local caching and backwards compatibility.

        Includes legacy keys ('input', 'output', 'tools') consumed by botex/pricing.py.
        """
        clean_raw = (
            _sanitize_raw_payload(self.raw_entry)
            if isinstance(self.raw_entry, (dict, list))
            else self.raw_entry
        )
        return {
            "id": self.id,
            "name": mask_secrets(self.name),
            "context_length": self.context_length,
            "prompt_price_per_m": self.prompt_price_per_m,
            "completion_price_per_m": self.completion_price_per_m,
            "supports_tools": self.supports_tools,
            "is_zdr_compliant": self.is_zdr_compliant,
            "raw_entry": clean_raw,
            # Legacy compatibility keys
            "input": self.prompt_price_per_m,
            "output": self.completion_price_per_m,
            "tools": self.supports_tools,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_id: str = "") -> ModelMetadata:
        """Deserialize from dictionary, handling current, raw, and legacy formats."""
        slug = str(data.get("id") or default_id or "")
        name = str(data.get("name") or slug)
        try:
            ctx = int(float(data.get("context_length") or 0))
        except (ValueError, TypeError, OverflowError):
            ctx = 0

        # 1. Resolve prompt price (per 1M tokens)
        prompt_val: Optional[float] = None
        if "prompt_price_per_m" in data:
            raw_p = data.get("prompt_price_per_m")
            if raw_p is not None:
                try:
                    prompt_val = float(raw_p)
                except (ValueError, TypeError):
                    prompt_val = None
        elif "input" in data:
            raw_p = data.get("input")
            if raw_p is not None:
                try:
                    prompt_val = float(raw_p)
                except (ValueError, TypeError):
                    prompt_val = None
        elif "pricing" in data and isinstance(data.get("pricing"), dict):
            pricing_dict = data["pricing"]
            try:
                raw_prompt = float(pricing_dict.get("prompt", -1))
                if raw_prompt >= 0:
                    prompt_val = raw_prompt * 1_000_000.0
            except (ValueError, TypeError, OverflowError):
                prompt_val = None

        # 2. Resolve completion price (per 1M tokens)
        comp_val: Optional[float] = None
        if "completion_price_per_m" in data:
            raw_c = data.get("completion_price_per_m")
            if raw_c is not None:
                try:
                    comp_val = float(raw_c)
                except (ValueError, TypeError):
                    comp_val = None
        elif "output" in data:
            raw_c = data.get("output")
            if raw_c is not None:
                try:
                    comp_val = float(raw_c)
                except (ValueError, TypeError):
                    comp_val = None
        elif "pricing" in data and isinstance(data.get("pricing"), dict):
            pricing_dict = data["pricing"]
            try:
                raw_comp = float(pricing_dict.get("completion", -1))
                if raw_comp >= 0:
                    comp_val = raw_comp * 1_000_000.0
            except (ValueError, TypeError, OverflowError):
                comp_val = None

        # 3. Resolve tool capability
        tools_val: Optional[bool] = None
        if "supports_tools" in data:
            st = data.get("supports_tools")
            tools_val = bool(st) if st is not None else None
        elif "supported_parameters" in data:
            params = data.get("supported_parameters")
            if params is None:
                tools_val = None
            elif isinstance(params, (list, tuple, set)):
                tools_val = "tools" in params
            else:
                tools_val = None
        elif "tools" in data:
            st = data.get("tools")
            tools_val = bool(st) if st is not None else None

        # 4. Resolve ZDR compliance
        if "is_zdr_compliant" in data:
            zdr_comp = bool(data.get("is_zdr_compliant"))
        else:
            slug_lower = slug.lower()
            zdr_comp = not (":free" in slug_lower or "openrouter/free" in slug_lower)

        # 5. Resolve raw entry
        if "raw_entry" in data and isinstance(data.get("raw_entry"), dict):
            raw = dict(data["raw_entry"])
        elif "pricing" in data or "supported_parameters" in data:
            raw = dict(data)
        else:
            raw = {}

        return cls(
            id=slug,
            name=name,
            context_length=ctx,
            prompt_price_per_m=prompt_val,
            completion_price_per_m=comp_val,
            supports_tools=tools_val,
            is_zdr_compliant=zdr_comp,
            raw_entry=raw,
        )


class ModelCatalog:
    """Dynamic model catalog service with TTL caching, offline fallback, and capability inspection."""

    def __init__(
        self,
        provider: Optional[BaseProvider] = None,
        provider_name: str = "openrouter",
        provider_cfg: Optional[Dict[str, Any]] = None,
        cache_file: Optional[Path | str] = None,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        api_key: Optional[str] = None,
    ) -> None:
        if isinstance(provider, str):
            provider_name = provider
            provider = None

        if provider is not None:
            self.provider = provider
        else:
            self.provider = ProviderRegistry.get(provider_name, provider_cfg)

        self.provider_name = self.provider.name
        self.provider_cfg = self.provider.config
        self.api_key = api_key
        self.ttl_hours = float(ttl_hours)
        self.cache_file = Path(cache_file) if cache_file else CACHE_FILE_DEFAULT
        self._models: Dict[str, ModelMetadata] = {}
        self._fetched_at: float = 0.0
        self._loaded: bool = False

    @property
    def cache_key(self) -> str:
        """Cache dictionary key corresponding to the provider's base_url."""
        return mask_secrets(self.provider.base_url.rstrip("/"))

    def get_model(self, model_id: str) -> Optional[ModelMetadata]:
        """Lookup model metadata by slug (case-insensitive and trimmed fallback)."""
        if not self._loaded:
            self.load()
        if model_id in self._models:
            return self._models[model_id]
        norm = model_id.strip().lower()
        for mid, meta in self._models.items():
            if mid.strip().lower() == norm:
                return meta
        return None

    def list_models(self) -> List[ModelMetadata]:
        """Return list of all cached model metadata entries."""
        if not self._loaded:
            self.load()
        return list(self._models.values())

    def check_tool_support(self, model_id: str, on_unknown: str = "allow") -> bool:
        """Check if model supports tool calling with precedence:

        1. Operator capability override in provider config (case-insensitive)
        2. Catalog metadata 'supports_tools'
        3. Configured on_unknown fallback policy ('allow' vs 'deny')
        """
        # 1. Operator override (case-insensitive resolution)
        meta = self.get_model(model_id)
        meta_id = meta.id if meta is not None else None

        cand_keys: List[str] = [model_id, model_id.strip().lower()]
        if meta_id:
            cand_keys.extend([meta_id, meta_id.strip().lower()])

        # Also inspect provider capabilities dict to match lowered keys
        cfg_caps = (self.provider_cfg or {}).get("capabilities")
        if isinstance(cfg_caps, dict):
            cand_norms = {k.strip().lower() for k in cand_keys if k}
            for raw_k in cfg_caps.keys():
                if isinstance(raw_k, str) and raw_k.strip().lower() in cand_norms:
                    cand_keys.append(raw_k)

        seen_keys = set()
        for cand in cand_keys:
            if not cand or cand in seen_keys:
                continue
            seen_keys.add(cand)
            caps = self.provider.get_model_capabilities(cand)
            if isinstance(caps, dict):
                if "supports_tools" in caps:
                    return bool(caps["supports_tools"])
                if "tools" in caps:
                    return bool(caps["tools"])

        # 2. Catalog metadata
        if meta is not None and meta.supports_tools is not None:
            return bool(meta.supports_tools)

        # 3. on_unknown policy (CodeQL CWE-020 static literal mapping)
        norm_policy = _ON_UNKNOWN_MAP.get(str(on_unknown).strip().lower(), "allow")
        return norm_policy != "deny"

    def load(self, force_refresh: bool = False) -> Dict[str, ModelMetadata]:
        """Load catalog from cache if fresh, otherwise fetch from provider API.

        Falls back to stale cache on network failure or offline mode.
        """
        cached_entry = self._read_cache_entry()
        now = time.time()

        if isinstance(cached_entry, dict):
            try:
                self._fetched_at = float(cached_entry.get("fetched_at") or 0.0)
            except (ValueError, TypeError):
                self._fetched_at = 0.0

            if (now - self._fetched_at) < (self.ttl_hours * 3600.0) and not force_refresh:
                raw_catalog = cached_entry.get("catalog") or cached_entry.get("models")
                if isinstance(raw_catalog, dict):
                    self._models = {
                        mid: ModelMetadata.from_dict(
                            d if isinstance(d, dict) else {}, default_id=mid
                        )
                        for mid, d in raw_catalog.items()
                        if isinstance(mid, str)
                    }
                    self._loaded = True
                    return self._models

        # If expired or force_refresh requested, fetch from provider
        try:
            fetched_models = self.fetch_from_provider()
            # If provider returned empty list and we have a valid stale cached catalog,
            # retain stale cache rather than wiping out disk state
            if not fetched_models and isinstance(cached_entry, dict):
                stale_cat = cached_entry.get("catalog") or cached_entry.get("models")
                if isinstance(stale_cat, dict) and stale_cat:
                    sys.stderr.write(
                        "[BoteX] Provider returned empty catalog; retaining stale cache.\n"
                    )
                    self._models = {
                        mid: ModelMetadata.from_dict(
                            d if isinstance(d, dict) else {}, default_id=mid
                        )
                        for mid, d in stale_cat.items()
                        if isinstance(mid, str)
                    }
                    self._loaded = True
                    return self._models

            self._models = {m.id: m for m in fetched_models}
            self._fetched_at = now
            self._loaded = True
            self._write_cache_entry(self._models)
            return self._models
        except Exception as exc:
            masked_err = mask_secrets(str(exc))
            sys.stderr.write(
                f"[BoteX] Catalog fetch failed ({masked_err}); using stale cache if any.\n"
            )

        # Stale cache fallback on network/HTTP exception
        if isinstance(cached_entry, dict):
            raw_catalog = cached_entry.get("catalog") or cached_entry.get("models")
            if isinstance(raw_catalog, dict):
                self._models = {
                    mid: ModelMetadata.from_dict(d if isinstance(d, dict) else {"id": mid})
                    for mid, d in raw_catalog.items()
                    if isinstance(mid, str)
                }
            else:
                self._models = {}
        else:
            self._models = {}
        self._loaded = True
        return self._models

    def fetch_from_provider(self, timeout: float = 10.0) -> List[ModelMetadata]:
        """Fetch model list from provider API and parse into ModelMetadata."""
        resolved_key = self.provider.resolve_api_key(explicit_key=self.api_key)
        raw_list = self.provider.fetch_catalog(api_key=resolved_key, timeout=timeout)

        deny_patterns: Sequence[str] = ((self.provider_cfg or {}).get("zdr") or {}).get(
            "deny_patterns"
        ) or [
            ":free",
            "openrouter/free",
        ]

        results: List[ModelMetadata] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            slug = raw.get("id")
            if not slug or not isinstance(slug, str):
                continue
            meta = self._parse_raw_entry(raw, deny_patterns)
            results.append(meta)
        return results

    def _parse_raw_entry(self, raw: Dict[str, Any], deny_patterns: Sequence[str]) -> ModelMetadata:
        """Parse raw provider JSON dictionary into standardized ModelMetadata."""
        slug = str(raw.get("id") or "")
        name = mask_secrets(str(raw.get("name") or slug))
        clean_raw = _sanitize_raw_payload(raw) if isinstance(raw, (dict, list)) else raw

        caps = self.provider.get_model_capabilities(slug)
        raw_ctx = (
            caps.get("context_length") if "context_length" in caps else raw.get("context_length")
        )
        try:
            ctx = int(float(raw_ctx or 0))
        except (ValueError, TypeError, OverflowError):
            ctx = 0

        # Pricing parsing
        raw_pricing = raw.get("pricing")
        pricing: Dict[str, Any] = raw_pricing if isinstance(raw_pricing, dict) else {}
        prompt_val: Optional[float] = None
        comp_val: Optional[float] = None

        if pricing:
            try:
                raw_prompt = float(pricing.get("prompt", -1))
                if raw_prompt >= 0:
                    prompt_val = raw_prompt * 1_000_000.0
            except (ValueError, TypeError, OverflowError):
                prompt_val = None

            try:
                raw_comp = float(pricing.get("completion", -1))
                if raw_comp >= 0:
                    comp_val = raw_comp * 1_000_000.0
            except (ValueError, TypeError, OverflowError):
                comp_val = None

        # Tool calling capability
        if "supports_tools" in caps:
            supports_tools: Optional[bool] = bool(caps["supports_tools"])
        elif "tools" in caps:
            supports_tools = bool(caps["tools"])
        else:
            supported_params = raw.get("supported_parameters")
            if supported_params is None:
                supports_tools = None
            elif isinstance(supported_params, (list, tuple, set)):
                supports_tools = "tools" in supported_params
            else:
                supports_tools = None

        # ZDR compliance check
        slug_lower = slug.lower()
        is_zdr = not any(p.lower() in slug_lower for p in deny_patterns)

        return ModelMetadata(
            id=slug,
            name=name,
            context_length=ctx,
            prompt_price_per_m=prompt_val,
            completion_price_per_m=comp_val,
            supports_tools=supports_tools,
            is_zdr_compliant=is_zdr,
            raw_entry=clean_raw,
        )

    def _read_cache_entry(self) -> Optional[Dict[str, Any]]:
        """Read cache file and return entry for this provider's base_url."""
        if not self.cache_file.exists():
            return None
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            catalogs = payload.get("catalogs")
            if not isinstance(catalogs, dict):
                catalogs = {}
            # Legacy cache migration
            if not catalogs and isinstance(payload.get("catalog"), dict):
                catalogs = {"https://openrouter.ai/api/v1": payload["catalog"]}
            if not catalogs and isinstance(payload.get("models"), dict):
                catalogs = {"https://openrouter.ai/api/v1": payload["models"]}
            entry = catalogs.get(self.cache_key)
            if isinstance(entry, dict):
                return entry
            return None
        except Exception:
            return None

    def _write_cache_entry(self, models: Dict[str, ModelMetadata]) -> None:
        """Update and persist cache file with new catalog entry."""
        payload: Dict[str, Any] = {}
        if self.cache_file.exists():
            try:
                raw_data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(raw_data, dict):
                    payload = raw_data
            except Exception:
                payload = {}

        catalogs = payload.get("catalogs")
        if not isinstance(catalogs, dict):
            catalogs = {}
        if not catalogs and isinstance(payload.get("catalog"), dict):
            catalogs = {"https://openrouter.ai/api/v1": payload["catalog"]}

        now = time.time()
        catalogs[self.cache_key] = {
            "fetched_at": now,
            "catalog": {mid: meta.to_dict() for mid, meta in models.items()},
        }
        payload["fetched_at"] = now
        payload["catalogs"] = catalogs

        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            sys.stderr.write(f"[BoteX] Failed to write catalog cache: {mask_secrets(str(exc))}\n")


_VALID_PROVIDERS_MAP: Dict[str, str] = {
    "openrouter": "openrouter",
    "openrouter-free": "openrouter-free",
    "nvidia": "nvidia",
}

_KNOWN_DISPLAY_NAMES: Dict[str, str] = {
    "liquid/lfm-2.5-2.6b:free": "Liquid LFM 2.5:free",
    "deepseek/deepseek-r1": "DeepSeek R1",
    "deepseek/deepseek-v3.2": "DeepSeek V3.2",
    "deepseek/deepseek-v4-flash-0731": "DeepSeek V4 Flash",
    "meta-llama/llama-3.1-8b-instruct": "Llama 3.1 8B Instruct",
    "meta/llama-3.3-70b-instruct": "Llama 3.3 70B Instruct",
    "google/gemini-2.5-flash": "Gemini 2.5 Flash",
    "google/gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "google/gemma-4-31b-it:free": "Gemma 4 31B IT:free",
    "qwen/qwen3-coder": "Qwen 3 Coder",
    "qwen/qwen3-coder-flash": "Qwen 3 Coder Flash",
    "qwen/qwen3-coder-480b-a35b-instruct": "Qwen 3 Coder 480B",
    "nvidia/nemotron-3-ultra-550b-a55b:free": "Nemotron 3 Ultra 550B:free",
    "nvidia/nemotron-3.5-lightning:free": "Nemotron 3.5 Lightning:free",
    "cohere/north-mini-code:free": "Cohere North Mini Code:free",
    "poolside/laguna-s-2.1:free": "Poolside Laguna S 2.1:free",
    "openai/gpt-4o-mini": "GPT-4o Mini",
    "anthropic/claude-3.7-sonnet": "Claude 3.7 Sonnet",
    "anthropic/claude-3.5-sonnet": "Claude 3.5 Sonnet",
    "openrouter/auto": "OpenRouter Auto Router",
    "openrouter/free": "OpenRouter Free Router:free",
}

_TOKEN_UPPER_MAP: Dict[str, str] = {
    "lfm": "LFM",
    "gpt": "GPT",
    "llm": "LLM",
    "vl": "VL",
    "it": "IT",
    "r1": "R1",
    "v2": "V2",
    "v3": "V3",
    "v4": "V4",
    "o1": "o1",
    "o3": "o3",
    "o4": "o4",
    "ai": "AI",
    "nim": "NIM",
    "xl": "XL",
    "xs": "XS",
    "moe": "MoE",
    "qwen": "Qwen",
    "qwen2": "Qwen2",
    "qwen3": "Qwen3",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "nvidia": "NVIDIA",
    "meta-llama": "Meta Llama",
}


def _format_readable_name(slug: str, raw_name: str = "") -> str:
    """Return a clean human-readable model name from its slug or raw provider name."""
    clean_slug = slug.strip()
    if clean_slug in _KNOWN_DISPLAY_NAMES:
        return _KNOWN_DISPLAY_NAMES[clean_slug]
    if raw_name and raw_name.strip() and raw_name.strip() != clean_slug:
        return mask_secrets(raw_name.strip())

    is_free_slug = clean_slug.lower().endswith(":free")
    base_slug = clean_slug[:-5] if is_free_slug else clean_slug
    parts = base_slug.split("/", 1)
    model_part = parts[1] if len(parts) > 1 else parts[0]
    vendor_part = parts[0].lstrip("~") if len(parts) > 1 else ""

    tokens = model_part.replace("_", "-").split("-")
    formatted_tokens: List[str] = []
    for tok in tokens:
        if not tok:
            continue
        low = tok.lower()
        if low in _TOKEN_UPPER_MAP:
            formatted_tokens.append(_TOKEN_UPPER_MAP[low])
        elif len(low) >= 2 and low[-1] == "b" and low[:-1].replace(".", "").isdigit():
            formatted_tokens.append(tok[:-1] + "B")
        elif low[0].isdigit():
            formatted_tokens.append(tok)
        else:
            formatted_tokens.append(tok.capitalize())

    readable = " ".join(formatted_tokens)
    if vendor_part:
        v_low = vendor_part.lower()
        v_clean = _TOKEN_UPPER_MAP.get(v_low, vendor_part.capitalize())
        if not readable.lower().startswith(v_clean.lower()):
            readable = f"{v_clean} {readable}"

    if is_free_slug and not readable.lower().endswith(":free"):
        readable = f"{readable}:free"
    return mask_secrets(readable or clean_slug)


def _infer_context_length(slug: str, current_ctx: int) -> int:
    """Return explicit context_length if positive, otherwise infer from model family."""
    if current_ctx and current_ctx > 0:
        return int(current_ctx)
    s = slug.lower()
    if "gemini" in s or "1m" in s:
        return 1048576
    if "claude" in s or "200k" in s:
        return 200000
    if any(
        k in s
        for k in (
            "deepseek",
            "qwen",
            "llama-3.1",
            "llama-3.2",
            "llama-3.3",
            "llama-4",
            "gpt-4o",
            "gpt-4.1",
            "o1",
            "o3",
            "o4",
            "nemotron",
            "mistral-large",
            "codestral",
            "gemma-3",
            "gemma-4",
        )
    ):
        return 131072
    if any(k in s for k in ("yi-", "jamba", "command-r", "mixtral-8x22b")):
        return 65536
    return 32768


# In-memory TTL cache for consolidated models endpoint (<20ms response target)
_CONSOLIDATED_MEM_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "cache_mtime": 0.0,
    "cache_path": "",
    "models": [],
}


def get_consolidated_models(
    providers: Sequence[str] = ("openrouter", "openrouter-free", "nvidia"),
    config: Optional[Dict[str, Any]] = None,
    cache_file: Optional[Path | str] = None,
    ttl_seconds: float = 300.0,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Return consolidated, deduplicated model list across configured providers.

    Uses in-memory TTL caching backed by ModelCatalog's persistent cache to
    respond in <5ms for UI model selectors.
    """
    resolved_cache_file = Path(cache_file) if cache_file else CACHE_FILE_DEFAULT
    cache_path_str = str(resolved_cache_file)
    try:
        mtime = resolved_cache_file.stat().st_mtime if resolved_cache_file.exists() else 0.0
    except OSError:
        mtime = 0.0

    now = time.time()
    norm_providers = tuple(
        _VALID_PROVIDERS_MAP[p.strip().lower()]
        for p in providers
        if isinstance(p, str) and p.strip().lower() in _VALID_PROVIDERS_MAP
    )
    if not norm_providers:
        norm_providers = ("openrouter", "openrouter-free", "nvidia")

    if (
        not force_refresh
        and _CONSOLIDATED_MEM_CACHE["models"]
        and _CONSOLIDATED_MEM_CACHE["cache_path"] == cache_path_str
        and _CONSOLIDATED_MEM_CACHE["cache_mtime"] == mtime
        and (now - float(_CONSOLIDATED_MEM_CACHE["timestamp"])) < ttl_seconds
        and _CONSOLIDATED_MEM_CACHE.get("providers") == norm_providers
    ):
        return list(_CONSOLIDATED_MEM_CACHE["models"])

    if config is None:
        try:
            from .config import load_config

            config = load_config()
        except Exception:
            try:
                from config import load_config

                config = load_config()
            except Exception:
                config = {}

    providers_cfg = (config or {}).get("providers", {}) if isinstance(config, dict) else {}
    entries_by_key: Dict[str, Dict[str, Any]] = {}

    def _add_meta(meta: ModelMetadata, target_provider: str) -> None:
        prov_literal = _VALID_PROVIDERS_MAP.get(target_provider, "openrouter")
        free_flag = bool(meta.is_free)
        # Route OpenRouter free models to openrouter-free provider when enabled
        if prov_literal == "openrouter" and free_flag and "openrouter-free" in norm_providers:
            prov_literal = "openrouter-free"
        elif prov_literal == "openrouter-free" and not free_flag:
            return

        prompt_p = 0.0 if free_flag else round(float(meta.prompt_price_per_m or 0.0), 4)
        comp_p = 0.0 if free_flag else round(float(meta.completion_price_per_m or 0.0), 4)
        zdr_flag = (
            False
            if (free_flag or prov_literal == "openrouter-free")
            else bool(meta.is_zdr_compliant)
        )
        tools_flag = bool(meta.supports_tools) if meta.supports_tools is not None else True

        item = {
            "id": meta.id,
            "name": _format_readable_name(meta.id, meta.name),
            "provider": prov_literal,
            "is_free": free_flag,
            "context_length": _infer_context_length(meta.id, meta.context_length),
            "pricing": {
                "prompt_per_m": prompt_p,
                "completion_per_m": comp_p,
            },
            "supports_tools": tools_flag,
            "is_zdr_compliant": zdr_flag,
        }
        dedup_key = f"{prov_literal}:{meta.id}"
        entries_by_key[dedup_key] = item

    loaded_by_base_url: Dict[str, List[ModelMetadata]] = {}
    for prov_name in norm_providers:
        pcfg = providers_cfg.get(prov_name) if isinstance(providers_cfg, dict) else None
        cat = ModelCatalog(
            provider_name=prov_name,
            provider_cfg=pcfg if isinstance(pcfg, dict) else None,
            cache_file=resolved_cache_file,
        )
        b_key = cat.cache_key
        if not force_refresh and b_key in loaded_by_base_url:
            models_list = loaded_by_base_url[b_key]
        else:
            if force_refresh:
                cat.load(force_refresh=True)
            models_list = cat.list_models()
            loaded_by_base_url[b_key] = models_list

        for m in models_list:
            _add_meta(m, prov_name)

    # Ensure canonical fallback models exist even if cache file is empty in offline/test environments
    _fallback_seeds = [
        ModelMetadata(
            id="liquid/lfm-2.5-2.6b:free",
            name="Liquid LFM 2.5:free",
            context_length=32768,
            prompt_price_per_m=0.0,
            completion_price_per_m=0.0,
            supports_tools=True,
            is_zdr_compliant=False,
        ),
        ModelMetadata(
            id="nvidia/nemotron-3-ultra-550b-a55b:free",
            name="Nemotron 3 Ultra 550B:free",
            context_length=131072,
            prompt_price_per_m=0.0,
            completion_price_per_m=0.0,
            supports_tools=True,
            is_zdr_compliant=False,
        ),
        ModelMetadata(
            id="google/gemma-4-31b-it:free",
            name="Gemma 4 31B IT:free",
            context_length=131072,
            prompt_price_per_m=0.0,
            completion_price_per_m=0.0,
            supports_tools=True,
            is_zdr_compliant=False,
        ),
        ModelMetadata(
            id="deepseek/deepseek-r1",
            name="DeepSeek R1",
            context_length=131072,
            prompt_price_per_m=0.70,
            completion_price_per_m=2.50,
            supports_tools=True,
            is_zdr_compliant=True,
        ),
        ModelMetadata(
            id="meta-llama/llama-3.1-8b-instruct",
            name="Llama 3.1 8B Instruct",
            context_length=131072,
            prompt_price_per_m=0.05,
            completion_price_per_m=0.08,
            supports_tools=True,
            is_zdr_compliant=True,
        ),
        ModelMetadata(
            id="google/gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            context_length=1048576,
            prompt_price_per_m=0.30,
            completion_price_per_m=2.50,
            supports_tools=True,
            is_zdr_compliant=True,
        ),
        ModelMetadata(
            id="qwen/qwen3-coder",
            name="Qwen 3 Coder",
            context_length=131072,
            prompt_price_per_m=0.30,
            completion_price_per_m=1.00,
            supports_tools=True,
            is_zdr_compliant=True,
        ),
        ModelMetadata(
            id="anthropic/claude-3.7-sonnet",
            name="Claude 3.7 Sonnet",
            context_length=200000,
            prompt_price_per_m=3.00,
            completion_price_per_m=15.00,
            supports_tools=True,
            is_zdr_compliant=True,
        ),
    ]
    for seed in _fallback_seeds:
        target_p = "openrouter-free" if seed.is_free else "openrouter"
        if target_p in norm_providers and f"{target_p}:{seed.id}" not in entries_by_key:
            _add_meta(seed, target_p)

    if "nvidia" in norm_providers:
        nv_seed = ModelMetadata(
            id="meta/llama-3.3-70b-instruct",
            name="Llama 3.3 70B Instruct (NIM)",
            context_length=131072,
            prompt_price_per_m=0.0,
            completion_price_per_m=0.0,
            supports_tools=True,
            is_zdr_compliant=True,
        )
        if f"nvidia:{nv_seed.id}" not in entries_by_key:
            entries_by_key[f"nvidia:{nv_seed.id}"] = {
                "id": nv_seed.id,
                "name": nv_seed.name,
                "provider": "nvidia",
                "is_free": False,
                "context_length": 131072,
                "pricing": {"prompt_per_m": 0.0, "completion_per_m": 0.0},
                "supports_tools": True,
                "is_zdr_compliant": True,
            }

    # Sort: curated/known models & free models first, then alphabetical by name
    def _sort_key(entry: Dict[str, Any]) -> Any:
        slug = entry["id"]
        is_curated = 0 if slug in _KNOWN_DISPLAY_NAMES else 1
        is_free_rank = 0 if entry["is_free"] else 1
        return (is_curated, is_free_rank, entry["name"].lower())

    sorted_models = sorted(entries_by_key.values(), key=_sort_key)

    _CONSOLIDATED_MEM_CACHE["timestamp"] = now
    _CONSOLIDATED_MEM_CACHE["cache_mtime"] = mtime
    _CONSOLIDATED_MEM_CACHE["cache_path"] = cache_path_str
    _CONSOLIDATED_MEM_CACHE["providers"] = norm_providers
    _CONSOLIDATED_MEM_CACHE["models"] = sorted_models
    return list(sorted_models)

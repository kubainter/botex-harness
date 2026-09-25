# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Pricing Guardrail
=======================
Pre-flight model cost gate: before the first API call, the resolved model
slug is checked against the provider's published price list
(``GET {base_url}/models`` — public on OpenRouter, no key required).

A task that resolves to an over-cap model is rejected locally with
``PRICE_EXCEEDED`` — zero tokens spent, zero cost.

Prices are normalized to USD per 1M tokens and cached in
``.model_pricing.json`` (gitignored) for ``cache_ttl_hours``.

Router/meta models (``openrouter/auto``, ``openrouter/auto-beta``,
``openrouter/pareto-code``) report dynamic/absent pricing — they fall under
the ``on_unknown`` policy ("allow" by default). A router can still pick an
expensive underlying model internally; the cap cannot see inside it.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from botex.config import PROJECT_ROOT

CACHE_FILE = PROJECT_ROOT / ".model_pricing.json"
DEFAULT_TTL_H = 24.0
_HTTP_TIMEOUT = 10


def _fetch_catalog(base_url: str, api_key: str = "") -> Dict[str, Dict[str, Any]]:
    """Fetch {base_url}/models — prices (USD/1Mtok) + tool-call capability."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    catalog: Dict[str, Dict[str, Any]] = {}
    for m in payload.get("data", []):
        slug = m.get("id")
        pricing = m.get("pricing") or {}
        if not slug:
            continue
        # Capability metadata (OpenRouter publishes supported_parameters;
        # providers that don't expose it leave tools=None -> on_unknown).
        params = m.get("supported_parameters")
        tools: Optional[bool] = None if params is None else ("tools" in params)
        try:
            # OpenRouter reports USD per token (as strings); routers use -1.
            pin = float(pricing.get("prompt", -1)) * 1_000_000
            pout = float(pricing.get("completion", -1)) * 1_000_000
        except (TypeError, ValueError):
            continue
        if pin < 0 or pout < 0:
            catalog[slug] = {"input": None, "output": None, "tools": tools}
        else:
            catalog[slug] = {"input": pin, "output": pout, "tools": tools}
    return catalog


def _read_cache() -> Dict[str, Any]:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_cache(catalogs: Dict[str, Any]) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "catalogs": catalogs}),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_catalog(
    base_url: str,
    api_key: str = "",
    ttl_hours: float = DEFAULT_TTL_H,
    force_refresh: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Return the model catalog for ``base_url``, preferring a fresh cached
    copy. Catalogs are cached per provider base_url — one provider's catalog
    never overwrites another's."""
    key = base_url.rstrip("/")
    cached = _read_cache()
    catalogs = cached.get("catalogs") or {}
    # Legacy single-catalog cache format -> treat as the openrouter entry.
    if not catalogs and isinstance(cached.get("catalog"), dict):
        catalogs = {"https://openrouter.ai/api/v1": cached["catalog"]}
    entry = catalogs.get(key) or {}
    if not force_refresh and entry:
        if time.time() - entry.get("fetched_at", 0) < ttl_hours * 3600:
            return entry.get("catalog", {})
    try:
        catalog = _fetch_catalog(base_url, api_key)
    except Exception as e:
        sys.stderr.write(f"[BoteX] Pricing catalog fetch failed ({e}); using stale cache if any.\n")
        return entry.get("catalog", {})
    catalogs[key] = {"fetched_at": time.time(), "catalog": catalog}
    _write_cache(catalogs)
    return catalog


def model_price(catalog: Dict[str, Dict[str, float]], model: str) -> Optional[Tuple[float, float]]:
    """(input, output) USD/1Mtok for a slug; None when unknown or dynamic."""
    entry = catalog.get(model)
    if not entry or entry.get("input") is None:
        return None
    return (entry["input"], entry["output"])


def model_supports_tools(catalog: Dict[str, Dict[str, Any]], model: str) -> Optional[bool]:
    """Tool-call support for a slug: True/False, or None when the provider
    does not publish capability metadata (e.g. NVIDIA)."""
    entry = catalog.get(model)
    if not entry:
        return None
    return entry.get("tools")


def check_model_tools(
    model: str,
    provider_cfg: Dict[str, Any],
    pricing_cfg: Dict[str, Any],
    api_key: str = "",
) -> Tuple[bool, str]:
    """
    Pre-flight tool-call compatibility gate — a model that the provider's
    catalog marks as lacking tool support can never run the BoteX loop;
    reject it locally instead of paying for a guaranteed API error.

    Unknown capability (no supported_parameters published) follows the
    same ``pricing.on_unknown`` policy as unknown prices.
    """
    if not pricing_cfg.get("enabled", True):
        return True, ""

    # Explicit operator declaration wins over catalog metadata — for
    # providers that publish no capability fields (e.g. NVIDIA), a
    # per-model "supports_tools" flag in providers.<name>.capabilities
    # is the only way to make a model verifiable.
    overrides = (provider_cfg.get("capabilities") or {}).get(model) or {}
    if "supports_tools" in overrides:
        supports: Optional[bool] = bool(overrides["supports_tools"])
    else:
        base_url = provider_cfg.get("base_url", "")
        if not base_url:
            return True, ""
        catalog = load_catalog(
            base_url,
            api_key=api_key,
            ttl_hours=float(pricing_cfg.get("cache_ttl_hours", DEFAULT_TTL_H)),
        )
        supports = model_supports_tools(catalog, model)
    if supports is False:
        return False, (
            f"[COMPAT BLOCK] Model '{model}' does not support tool calling "
            f"per the provider catalog — the BoteX loop requires tools. "
            f"Pick a tool-capable model/profile."
        )
    if supports is None and pricing_cfg.get("on_unknown", "allow") == "deny":
        return False, (
            f"[COMPAT BLOCK] Tool support for '{model}' is unknown and "
            f"pricing.on_unknown=deny forbids unverified models."
        )
    return True, ""


def check_model_price(
    model: str,
    provider_cfg: Dict[str, Any],
    pricing_cfg: Dict[str, Any],
    api_key: str = "",
) -> Tuple[bool, str]:
    """
    Gate a resolved model against configured price caps.

    Returns (allowed, message). Message is a warning for unknown/dynamic
    pricing or the rejection reason when disallowed.
    """
    if not pricing_cfg.get("enabled", True):
        return True, ""

    base_url = provider_cfg.get("base_url", "")
    if not base_url:
        return True, ""

    catalog = load_catalog(
        base_url,
        api_key=api_key,
        ttl_hours=float(pricing_cfg.get("cache_ttl_hours", DEFAULT_TTL_H)),
    )
    price = model_price(catalog, model)

    if price is None:
        on_unknown = pricing_cfg.get("on_unknown", "allow")
        note = (
            f"Model '{model}' has no fixed public price (router/dynamic or "
            f"unknown slug) — the price cap cannot be enforced on it."
        )
        if on_unknown == "deny":
            return False, (
                f"[PRICE BLOCK] {note} pricing.on_unknown=deny forbids it. "
                f"Pick an explicit priced model or relax pricing settings."
            )
        return True, f"[BoteX] {note}"

    pin, pout = price
    max_in = float(pricing_cfg.get("max_input_per_mtok", 0.0) or 0.0)
    max_out = float(pricing_cfg.get("max_output_per_mtok", 0.0) or 0.0)
    breaches = []
    if max_in > 0 and pin > max_in:
        breaches.append(f"input ${pin:.2f}/1M > cap ${max_in:.2f}/1M")
    if max_out > 0 and pout > max_out:
        breaches.append(f"output ${pout:.2f}/1M > cap ${max_out:.2f}/1M")
    if breaches:
        return False, (
            f"[PRICE BLOCK] Model '{model}' exceeds pricing guardrail: "
            + "; ".join(breaches)
            + ". Choose a cheaper profile/model or raise "
            + "'pricing.max_*_per_mtok' in config."
        )
    return True, ""

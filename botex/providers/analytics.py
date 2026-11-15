# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Analytics & Quota Ledger
==============================
Tracks token consumption, estimated USD costs, lines modified, and enforces daily budget limits.
Provides rich CLI reporting and snapshot storage inspection.
"""

import gzip
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from botex.core.contracts import STATUS_FAILURE_KIND
    from botex.server.config import (
        analytics_file as _cfg_analytics_file,
    )
    from botex.server.config import (
        analytics_setting,
    )
    from botex.server.config import (
        resolve_provider as _resolve_provider,
    )
    from botex.server.config import (
        snapshot_dir as _cfg_snapshot_dir,
    )
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from config import (
        analytics_file as _cfg_analytics_file,
    )
    from config import (
        analytics_setting,
    )
    from config import (
        resolve_provider as _resolve_provider,
    )
    from config import (
        snapshot_dir as _cfg_snapshot_dir,
    )
    from contracts import STATUS_FAILURE_KIND
    from security import coerce_text, mask_secrets, sanitize_identifier

ANALYTICS_FILE = _cfg_analytics_file()
SNAPSHOT_DIR = _cfg_snapshot_dir()

# Approximate pricing per 1M tokens for common OpenRouter models (fallback estimates)
MODEL_PRICING_ESTIMATES = {
    "openrouter/auto": {"prompt": 0.20, "completion": 0.80},
    "openrouter/pareto-code": {"prompt": 0.25, "completion": 1.00},
    "qwen/qwen-2.5-coder-32b-instruct": {"prompt": 0.07, "completion": 0.16},
    "deepseek/deepseek-chat": {"prompt": 0.14, "completion": 0.28},
    "anthropic/claude-3.7-sonnet": {"prompt": 3.00, "completion": 15.00},
    "google/gemini-2.5-pro": {"prompt": 1.25, "completion": 5.00},
    "google/gemini-2.5-flash": {"prompt": 0.075, "completion": 0.30},
}

# Canonical cost-provenance enum stored on ledger entries. Raw engine/provider
# labels fold onto the canonical set; unmapped values never persist verbatim.
_COST_SOURCE_MAP: Dict[str, str] = {
    "openrouter_generation_api": "api_generation",
    "openrouter_inline_usage": "api_inline",
    "api_generation": "api_generation",
    "api_inline": "api_inline",
    "api_reported": "api_reported",
    "catalog_estimate": "catalog_estimate",
    "fallback_estimate": "fallback_estimate",
    "free": "free",
}
_ESTIMATED_COST_SOURCES = frozenset({"catalog_estimate", "fallback_estimate"})


_FREE_MODEL_MARKERS = (":free", "openrouter/free")
_FREE_MODEL_PREFIXES = ("openrouter-free/", "ollama/", "nvidia/", "gemini-", "models/gemini-")
_FREE_PROVIDER_NAMES = frozenset(
    {
        "ollama",
        "nvidia",
        "google",
        "gemini",
        "aistudio",
        "openrouter-free",
    }
)


def is_free_model(
    model: str,
    provider_cfg: Optional[Dict[str, Any]] = None,
    provider_name: str = "",
) -> bool:
    """Whether a model run is guaranteed zero-cost.

    Covers free-tier slugs (``:free``, ``openrouter/free``, ``openrouter-free/``),
    zero-cost namespaces (``ollama/``, ``nvidia/``), package/quota-bound
    providers (``nvidia``, ``google`` / ``gemini`` / ``aistudio``, ``ollama``,
    ``openrouter-free``), and providers that cannot bill per request —
    ``local: true``, ``free_tier: true``, or static ``api_key_fallback``.

    Namespace prefixes (``nvidia/``, ``ollama/``, ``gemini-`` ...) only mark a
    model free while no billable provider is resolved: the same slug hosted on
    a paid provider (e.g. ``nvidia/*`` on OpenRouter) is priced per token.
    """
    slug = (model or "").strip().lower()
    if any(marker in slug for marker in _FREE_MODEL_MARKERS):
        return True
    pname = (provider_name or "").strip().lower()
    if not pname or pname in _FREE_PROVIDER_NAMES:
        if slug.startswith(_FREE_MODEL_PREFIXES):
            return True
    if pname in _FREE_PROVIDER_NAMES:
        return True
    cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
    return bool(cfg.get("local") or cfg.get("free_tier") or cfg.get("api_key_fallback"))


# Cached prompt reads are billed at a discount on OpenAI-compatible endpoints
# (~50% on OpenAI upstreams; varies by provider). Free models and providers
# that never price cached reads (NVIDIA NIM, local) yield $0 savings.
CACHE_READ_DISCOUNT = 0.50

# Per-family read discounts for pre-flight estimates (usage docs verified
# 2026-11-07: Anthropic ~0.90 effective read discount; OpenAI-compatible
# ~0.50). The authoritative per-request amount stays usage.cost — these
# constants exist because no discount field arrives pre-dispatch.
_CACHE_READ_DISCOUNT_BY_FAMILY = {
    "anthropic": 0.90,
    "openai": 0.50,
}


def _read_discount_for(model: str) -> float:
    slug = (model or "").lower()
    for family, discount in _CACHE_READ_DISCOUNT_BY_FAMILY.items():
        if family in slug:
            return discount
    return CACHE_READ_DISCOUNT


def _catalog_cache_pricing(model: str) -> Optional[Dict[str, float]]:
    """Resolve per-1M pricing for ``model`` from the on-disk catalog cache.

    Strictly offline: reads ``.model_pricing.json`` directly so callers never
    trigger a ``ModelCatalog`` network refresh. Returns ``None`` when the
    model or its prices are absent.
    """
    slug = (model or "").strip().removeprefix("models/").lower()
    if not slug:
        return None
    try:
        try:
            from botex.providers import catalog as _catalog_mod
        except (ImportError, ValueError):
            import catalog as _catalog_mod  # type: ignore[no-redef]
    except Exception:
        return None
    try:
        cache_path = Path(_catalog_mod.CACHE_FILE_DEFAULT)
        if not cache_path.is_file():
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        entries: List[Any] = []
        catalogs = payload.get("catalogs")
        if isinstance(catalogs, dict):
            entries.extend(v for v in catalogs.values() if isinstance(v, dict))
        # Legacy single-catalog layouts keyed under "catalog"/"models".
        for legacy_key in ("catalog", "models"):
            if isinstance(payload.get(legacy_key), dict):
                entries.append({legacy_key: payload[legacy_key]})
        for entry in entries:
            raw_catalog = entry.get("catalog") or entry.get("models")
            if not isinstance(raw_catalog, dict):
                continue
            for mid, data in raw_catalog.items():
                if not isinstance(mid, str) or mid.strip().removeprefix("models/").lower() != slug:
                    continue
                meta = _catalog_mod.ModelMetadata.from_dict(
                    data if isinstance(data, dict) else {}, default_id=mid
                )
                if meta.prompt_price_per_m is not None and meta.completion_price_per_m is not None:
                    return {
                        "prompt": float(meta.prompt_price_per_m),
                        "completion": float(meta.completion_price_per_m),
                    }
    except Exception:
        return None
    return None


def _lookup_pricing_per_m(model: str, provider_name: str = "") -> tuple[Dict[str, float], bool]:
    """Resolve per-1M pricing: catalog cache first, static estimates last.

    ``is_catalog`` is True only when the ``.model_pricing.json`` cache
    supplied both prices — the lookup stays synchronous and offline-safe.
    """
    del provider_name  # catalog pricing is provider-agnostic for now
    catalog_pricing = _catalog_cache_pricing(model)
    if catalog_pricing is not None:
        return catalog_pricing, True
    pricing = MODEL_PRICING_ESTIMATES.get(model)
    if pricing is not None:
        return pricing, False
    return {"prompt": 0.20, "completion": 0.80}, False


def calculate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider_name: str = "",
) -> float:
    """Estimate cost in USD based on model pricing.

    Full-price fallback: cached prompt reads (billed at ~50% on supporting
    providers) are not discounted here — the authoritative amount comes from
    generation telemetry when available.
    """
    if is_free_model(model, provider_name=provider_name):
        return 0.0
    pricing, _is_catalog = _lookup_pricing_per_m(model, provider_name)
    p_cost = (prompt_tokens / 1_000_000) * pricing["prompt"]
    c_cost = (completion_tokens / 1_000_000) * pricing["completion"]
    return round(p_cost + c_cost, 6)


def _cache_savings_usd(
    model: str,
    cached_tokens: int,
    provider_name: str = "",
    provider_cfg: Optional[Dict[str, Any]] = None,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated USD saved by cached prompt reads for one model.

    Symmetric accounting (R3 Pkt3): subtracts the cache-write premium
    (~1.25x base prompt price on Anthropic-style providers) — without it
    the telemetry reports phantom savings on write-heavy turns.
    """
    if (cached_tokens <= 0 and cache_write_tokens <= 0) or is_free_model(
        model, provider_cfg, provider_name
    ):
        return 0.0
    pricing, _is_catalog = _lookup_pricing_per_m(model, provider_name)
    read_savings = (cached_tokens / 1_000_000) * pricing["prompt"] * _read_discount_for(model)
    write_premium = (cache_write_tokens / 1_000_000) * pricing["prompt"] * 0.25
    return read_savings - write_premium


def _ledger_jsonl() -> Path:
    """Append-only live ledger segment (.agent_analytics.jsonl)."""
    return _cfg_analytics_file().with_suffix(".jsonl")


def _ledger_archive_dir() -> Path:
    """Compressed monthly rollups rotated out of the live segment."""
    return _cfg_analytics_file().parent / "analytics-archive"


def _read_jsonl(path: Path, strict: bool = False) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        if path.suffix == ".gz":
            fh = gzip.open(path, "rt", encoding="utf-8")
        else:
            fh = path.open("r", encoding="utf-8")
        with fh:
            out = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
            return out
    except OSError:
        if strict:
            raise
        return []


def load_analytics() -> List[Dict[str, Any]]:
    """Load all run records: monthly archives + legacy .json + live .jsonl.

    Sources are merged oldest-first and deduplicated on (task_id,
    timestamp) so legacy records migrated into .jsonl are not counted
    twice. Also reads any orphaned .rotating segments left by an
    interrupted rotation.
    """
    records: List[Dict[str, Any]] = []
    archive = _ledger_archive_dir()
    if archive.is_dir():
        for gz in sorted(archive.glob("*.jsonl.gz")):
            records.extend(_read_jsonl(gz))

    target_file = _cfg_analytics_file()
    if target_file.exists():
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                records.extend(r for r in data if isinstance(r, dict))
        except Exception:
            pass
    live = _ledger_jsonl()
    for rot_suffix in (".rotating", ".rotating2"):
        rot_file = live.with_suffix(live.suffix + rot_suffix)
        if rot_file.exists():
            records.extend(_read_jsonl(rot_file))
    records.extend(_read_jsonl(live))

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for r in records:
        tid = r.get("task_id")
        ts = r.get("timestamp")
        # Only dedup rows carrying both identifiers — legacy rows without
        # either field would all collapse onto the (None, None) key.
        if tid and ts:
            key = (tid, ts)
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def save_analytics(records: List[Dict[str, Any]]):
    """Full-replace the live .jsonl segment (test/migration helper; caller must pass merged load_analytics() output to preserve legacy rows)."""
    target_file = _ledger_jsonl()
    target_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_file.with_suffix(target_file.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    os.replace(tmp, target_file)
    legacy = _cfg_analytics_file()
    if legacy.exists() and legacy != target_file:
        try:
            legacy.unlink()
        except OSError:
            pass


def _append_ledger(entry: Dict[str, Any]) -> None:
    """O(1) append of one run record to the live .jsonl segment.

    One-shot migration: a legacy .agent_analytics.json is folded into
    .jsonl on first append and removed, so the read-modify-write of the
    whole history per run is eliminated.
    """
    target = _ledger_jsonl()
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = _cfg_analytics_file()
    if legacy.exists() and legacy != target:
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                with target.open("a", encoding="utf-8") as fh:
                    for r in data:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            legacy.unlink()
        except (OSError, ValueError):
            pass
    line_str = coerce_text(json.dumps(entry, ensure_ascii=False)) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line_str)


def log_run(
    task_id: str,
    model: str,
    duration_s: float,
    steps: int,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    files_touched: List[str],
    lines_added: int,
    lines_removed: int,
    status: str = "DONE",
    summary: str = "",
    cost_usd: Optional[float] = None,
    failure_kind: Optional[str] = None,
    provider: Optional[str] = None,
    cache_write_tokens: int = 0,
    queue_time_ms: Optional[float] = None,
    generation_time_ms: Optional[float] = None,
    origin: Optional[str] = None,
    recipe: Optional[str] = None,
    routed_tier: Optional[str] = None,
    taxonomy: Optional[str] = None,
    applied_skills: Optional[List[str]] = None,
    applied_lessons: Optional[List[str]] = None,
    syntax_rejections: int = 0,
    rollback_count: int = 0,
    shadow_recommended_model: Optional[str] = None,
    cost_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a completed BoteX run into the ledger.

    Privacy: the summary is secret-masked before persisting. Unless
    ``analytics.store_full_text`` is enabled, only a truncated preview is
    stored; the complete text survives solely as ``summary_sha256``.
    Attribution fields (origin/recipe/routed_tier/taxonomy, applied skills
    and lessons, syntax-rejection and rollback counters) are masked and
    persisted only when non-empty, so legacy records keep their shape.
    """
    if is_free_model(model, provider_name=provider or ""):
        cost_usd = 0.0
        resolved_cost_source = "free"
    elif cost_usd is None:
        pricing, from_catalog = _lookup_pricing_per_m(model, provider or "")
        cost_usd = round(
            (prompt_tokens / 1_000_000) * pricing["prompt"]
            + (completion_tokens / 1_000_000) * pricing["completion"],
            6,
        )
        resolved_cost_source = "catalog_estimate" if from_catalog else "fallback_estimate"
    else:
        cost_usd = round(float(cost_usd), 6)
        resolved_cost_source = (
            _COST_SOURCE_MAP.get(str(cost_source or "").strip().lower(), "api_inline")
            if cost_source is not None
            else "api_reported"
        )

    masked_summary = coerce_text(mask_secrets(summary or ""))
    stored_summary = masked_summary
    if not analytics_setting("store_full_text"):
        preview_chars = int(analytics_setting("preview_chars") or 160)
        stored_summary = masked_summary[:preview_chars]
        if len(masked_summary) > preview_chars:
            stored_summary += "…"

    if failure_kind is None:
        failure_kind = STATUS_FAILURE_KIND.get(status)

    entry: Dict[str, Any] = {
        "task_id": task_id,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "provider": provider,
        "failure_kind": failure_kind,
        "duration_s": round(duration_s, 2),
        "steps": steps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": int(cache_write_tokens or 0),
        "cost_usd": cost_usd,
        "cost_source": resolved_cost_source,
        "files_touched": files_touched,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "status": status,
        "summary": stored_summary,
        "summary_sha256": hashlib.sha256(masked_summary.encode("utf-8")).hexdigest(),
    }
    if queue_time_ms is not None:
        entry["queue_time_ms"] = round(float(queue_time_ms), 2)
    if generation_time_ms is not None:
        entry["generation_time_ms"] = round(float(generation_time_ms), 2)

    def _attr_text(val: Optional[str]) -> str:
        # Flatten CR/LF so untrusted multi-line text cannot forge ledger lines.
        flat = re.sub(r"[\r\n]+", " ", str(val or "").strip())
        return coerce_text(mask_secrets(flat))[:64]

    if origin:
        entry["origin"] = _attr_text(origin)
    if recipe:
        entry["recipe"] = _attr_text(recipe)
    if routed_tier:
        entry["routed_tier"] = _attr_text(routed_tier)
    if taxonomy:
        entry["taxonomy"] = _attr_text(taxonomy)
    if applied_skills:
        entry["applied_skills"] = [_attr_text(s) for s in applied_skills][:8]
    if applied_lessons:
        entry["applied_lessons"] = [_attr_text(s) for s in applied_lessons][:8]
    if syntax_rejections and not isinstance(syntax_rejections, bool):
        entry["syntax_rejections"] = int(syntax_rejections)
    if rollback_count and not isinstance(rollback_count, bool):
        entry["rollback_count"] = int(rollback_count)
    if shadow_recommended_model:
        entry["shadow_recommended_model"] = _attr_text(shadow_recommended_model)

    _append_ledger(entry)
    return entry


def _effective_record_cost_usd(
    record: Dict[str, Any],
    prov_cfgs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> float:
    """Return a record's USD cost, clamping free-tier and malformed costs to 0.0."""
    m = str(record.get("model") or "")
    pname = (record.get("provider") or "").strip().lower()
    pcfg = (prov_cfgs or {}).get(pname) if pname and isinstance(prov_cfgs, dict) else None
    if is_free_model(m, pcfg, pname):
        return 0.0
    raw = record.get("cost_usd", 0.0)
    if isinstance(raw, bool) or raw is None:
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) and val >= 0.0 else 0.0


def check_budget_limit(daily_limit_usd: float) -> tuple[bool, float]:
    """
    Check if today's total spending exceeds daily_limit_usd.
    Returns (is_exceeded, today_spend_usd).
    """
    if daily_limit_usd <= 0.0:
        return False, 0.0

    today_str = datetime.now().strftime("%Y-%m-%d")
    records = load_analytics()
    today_spend = sum(
        _effective_record_cost_usd(r)
        for r in records
        if r.get("timestamp", "").startswith(today_str)
    )
    return today_spend >= daily_limit_usd, round(today_spend, 4)


def aggregate_stats(records: List[Dict[str, Any]], days: Optional[int] = None) -> Dict[str, Any]:
    """Pure aggregation over run records — shared by CLI reports and the REST API.

    ``days=None`` aggregates all records; a positive value keeps only runs newer
    than ``now - days``. Returns JSON-serializable totals, per-model breakdown,
    daily time series, self-improvement loop telemetry, and the most recent runs.
    """
    cutoff_naive = datetime.now() - timedelta(days=days) if days and days > 0 else None
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=days) if days and days > 0 else None
    filtered: List[Dict[str, Any]] = [
        r for r in records if _within_days(r.get("timestamp"), cutoff_naive, cutoff_utc)
    ]

    by_model: Dict[str, Dict[str, Any]] = {}
    by_provider: Dict[str, Dict[str, Any]] = {}
    by_taxonomy: Dict[str, Dict[str, Any]] = {}
    by_recipe: Dict[str, Dict[str, Any]] = {}
    by_tier: Dict[str, Dict[str, Any]] = {}
    daily: Dict[str, Dict[str, Any]] = {}
    status_counts: Dict[str, int] = {}
    cost_source_counts: Dict[str, int] = {
        "api_generation": 0,
        "api_inline": 0,
        "api_reported": 0,
        "catalog_estimate": 0,
        "fallback_estimate": 0,
        "free": 0,
        "unknown": 0,
    }
    estimated_cost_usd = 0.0
    total_steps = 0
    syntax_rejections_total = 0
    rollback_total = 0
    durations: List[float] = []
    gen_times: List[float] = []
    queue_times: List[float] = []
    prov_cfgs: Dict[str, Dict[str, Any]] = {}
    lesson_hits: Dict[str, Dict[str, int]] = {}
    skill_hits: Dict[str, Dict[str, int]] = {}
    shadow_hits: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    runs_with_learning = {"applied_lessons": 0, "applied_skills": 0}
    runs_with_shadow_advice = 0

    def _learning_ids(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        seen: set = set()
        for item in value:
            iid = str(item).strip() if item is not None else ""
            if iid and iid not in seen:
                seen.add(iid)
                out.append(iid)
        return out

    for r in filtered:
        st = r.get("status") or "UNKNOWN"
        status_counts[st] = status_counts.get(st, 0) + 1

        m = r.get("model") or "other"
        bm = by_model.setdefault(
            m,
            {
                "model": m,
                "runs": 0,
                "done_runs": 0,
                "cost_usd": 0.0,
                "tokens": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "cache_savings_usd": 0.0,
                "failures": {},
            },
        )
        bm["runs"] += 1
        if st == "DONE":
            bm["done_runs"] += 1
        else:
            fk = r.get("failure_kind") or st or "unknown"
            bm["failures"][fk] = bm["failures"].get(fk, 0) + 1
        pname = (r.get("provider") or "").strip().lower()
        if pname and pname not in prov_cfgs:
            try:
                _pname, pcfg = _resolve_provider(pname)
                prov_cfgs[pname] = pcfg if isinstance(pcfg, dict) else {}
            except Exception:
                prov_cfgs[pname] = {}
        cost = _effective_record_cost_usd(r, prov_cfgs)
        toks = (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0)
        cached_i = r.get("cached_tokens", 0) or 0
        savings_i = _cache_savings_usd(
            m,
            cached_i,
            pname,
            prov_cfgs.get(pname) if pname else None,
            r.get("cache_write_tokens", 0) or 0,
        )
        bm["cost_usd"] += cost
        bm["tokens"] += toks
        bm["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        bm["cached_tokens"] += cached_i
        bm["cache_savings_usd"] += savings_i

        prov_key = pname or "unknown"
        bp = by_provider.setdefault(
            prov_key,
            {
                "provider": prov_key,
                "runs": 0,
                "done_runs": 0,
                "cost_usd": 0.0,
                "tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "cache_savings_usd": 0.0,
                "failures": {},
                "_dur": [],
                "_gen": [],
            },
        )
        bp["runs"] += 1
        if st == "DONE":
            bp["done_runs"] += 1
        else:
            bp["failures"][fk] = bp["failures"].get(fk, 0) + 1
        bp["cost_usd"] += cost
        bp["tokens"] += toks
        bp["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        bp["completion_tokens"] += r.get("completion_tokens", 0) or 0
        bp["cached_tokens"] += cached_i
        bp["cache_savings_usd"] += savings_i

        dur_v = r.get("duration_s")
        gen_v = r.get("generation_time_ms")
        if isinstance(dur_v, (int, float)):
            durations.append(float(dur_v))
            bp["_dur"].append(float(dur_v))
        if isinstance(gen_v, (int, float)):
            gen_times.append(float(gen_v))
            bp["_gen"].append(float(gen_v))

        for dim_map, field in (
            (by_taxonomy, "taxonomy"),
            (by_recipe, "recipe"),
            (by_tier, "routed_tier"),
        ):
            dkey = str(r.get(field) or "").strip().lower() or "unspecified"
            db = dim_map.setdefault(
                dkey,
                {
                    "key": dkey,
                    "runs": 0,
                    "done_runs": 0,
                    "cost_usd": 0.0,
                    "tokens": 0,
                    "_dur": [],
                },
            )
            db["runs"] += 1
            if st == "DONE":
                db["done_runs"] += 1
            db["cost_usd"] += cost
            db["tokens"] += toks
            if isinstance(dur_v, (int, float)):
                db["_dur"].append(float(dur_v))

        cs_key = _COST_SOURCE_MAP.get(str(r.get("cost_source") or "").strip().lower(), "unknown")
        cost_source_counts[cs_key] = cost_source_counts.get(cs_key, 0) + 1
        if cs_key in _ESTIMATED_COST_SOURCES:
            estimated_cost_usd += cost

        steps_v = r.get("steps")
        if isinstance(steps_v, int) and not isinstance(steps_v, bool):
            total_steps += steps_v
        sr_v = r.get("syntax_rejections")
        if isinstance(sr_v, int) and not isinstance(sr_v, bool):
            syntax_rejections_total += sr_v
        rb_v = r.get("rollback_count")
        if isinstance(rb_v, int) and not isinstance(rb_v, bool):
            rollback_total += rb_v
        q_v = r.get("queue_time_ms")
        if isinstance(q_v, (int, float)) and not isinstance(q_v, bool):
            queue_times.append(float(q_v))

        for field, hits in (
            ("applied_lessons", lesson_hits),
            ("applied_skills", skill_hits),
        ):
            ids = _learning_ids(r.get(field))
            if not ids:
                continue
            runs_with_learning[field] += 1
            for iid in ids:
                ent = hits.setdefault(iid, {"runs": 0, "done_runs": 0})
                ent["runs"] += 1
                if st == "DONE":
                    ent["done_runs"] += 1

        shadow_model = str(r.get("shadow_recommended_model") or "").strip()
        if shadow_model:
            runs_with_shadow_advice += 1
            skey = (
                str(r.get("recipe") or "").strip().lower() or "unspecified",
                str(r.get("taxonomy") or "").strip().lower() or "unspecified",
                shadow_model,
            )
            sent = shadow_hits.setdefault(
                skey,
                {"recipe": skey[0], "taxonomy": skey[1], "model": skey[2], "runs": 0},
            )
            sent["runs"] += 1

        day = str(r.get("timestamp", ""))[:10]
        if day:
            d = daily.setdefault(day, {"date": day, "runs": 0, "cost_usd": 0.0, "tokens": 0})
            d["runs"] += 1
            d["cost_usd"] += cost
            d["tokens"] += toks

    models_out: List[Dict[str, Any]] = []
    for bm in sorted(by_model.values(), key=lambda x: x["cost_usd"], reverse=True):
        bm["done_pct"] = round(bm["done_runs"] / bm["runs"] * 100, 1) if bm["runs"] else 0.0
        bm["cache_hit_pct"] = (
            round(min(bm["cached_tokens"] / bm["prompt_tokens"], 1.0) * 100, 1)
            if bm["prompt_tokens"]
            else 0.0
        )
        bm["cost_usd"] = round(bm["cost_usd"], 6)
        bm["top_failure"] = (
            max(bm["failures"].items(), key=lambda x: x[1])[0] if bm["failures"] else None
        )
        bm["cache_savings_usd"] = round(bm["cache_savings_usd"], 6)
        models_out.append(bm)

    providers_out: List[Dict[str, Any]] = []
    for bp in sorted(by_provider.values(), key=lambda x: (x["cost_usd"], x["runs"]), reverse=True):
        bp["done_pct"] = round(bp["done_runs"] / bp["runs"] * 100, 1) if bp["runs"] else 0.0
        bp["cache_hit_pct"] = (
            round(min(bp["cached_tokens"] / bp["prompt_tokens"], 1.0) * 100, 1)
            if bp["prompt_tokens"]
            else 0.0
        )
        bp["cost_usd"] = round(bp["cost_usd"], 6)
        bp["cache_savings_usd"] = round(bp["cache_savings_usd"], 6)
        bp["avg_duration_s"] = round(sum(bp["_dur"]) / len(bp["_dur"]), 2) if bp["_dur"] else None
        bp["avg_generation_ms"] = (
            round(sum(bp["_gen"]) / len(bp["_gen"]), 1) if bp["_gen"] else None
        )
        bp["top_failure"] = (
            max(bp["failures"].items(), key=lambda x: x[1])[0] if bp["failures"] else None
        )
        bp.pop("_dur", None)
        bp.pop("_gen", None)
        providers_out.append(bp)

    def _dim_out(dim_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for db in sorted(dim_map.values(), key=lambda x: (x["cost_usd"], x["runs"]), reverse=True):
            db["done_pct"] = round(db["done_runs"] / db["runs"] * 100, 1) if db["runs"] else 0.0
            db["cost_usd"] = round(db["cost_usd"], 6)
            db["avg_duration_s"] = (
                round(sum(db["_dur"]) / len(db["_dur"]), 2) if db["_dur"] else None
            )
            db.pop("_dur", None)
            out.append(db)
        return out

    prompt_tok = sum(r.get("prompt_tokens", 0) or 0 for r in filtered)
    cached_tok = sum(r.get("cached_tokens", 0) or 0 for r in filtered)
    done = status_counts.get("DONE", 0)

    recent = sorted(filtered, key=lambda r: str(r.get("timestamp", "")), reverse=True)[:12]
    recent_out = [
        {
            "task_id": r.get("task_id"),
            "timestamp": r.get("timestamp"),
            "model": r.get("model"),
            "provider": r.get("provider"),
            "status": r.get("status"),
            "failure_kind": r.get("failure_kind"),
            "cost_usd": _effective_record_cost_usd(r, prov_cfgs),
            "duration_s": r.get("duration_s"),
            "tokens": (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0),
        }
        for r in recent
    ]

    def _top_learning(hits: Dict[str, Dict[str, int]]) -> List[Dict[str, Any]]:
        return [
            {"id": iid, **ent}
            for iid, ent in sorted(hits.items(), key=lambda kv: (-kv[1]["runs"], kv[0]))[:10]
        ]

    learning_summary = {
        "runs_with_applied_lessons": runs_with_learning["applied_lessons"],
        "runs_with_applied_skills": runs_with_learning["applied_skills"],
        "runs_with_shadow_advice": runs_with_shadow_advice,
        "top_applied_lessons": _top_learning(lesson_hits),
        "top_applied_skills": _top_learning(skill_hits),
        "shadow_recommendations": [
            ent
            for _key, ent in sorted(shadow_hits.items(), key=lambda kv: (-kv[1]["runs"], kv[0]))[
                :10
            ]
        ],
    }

    return {
        "runs": len(filtered),
        "done_runs": done,
        "failed_runs": len(filtered) - done,
        "done_pct": round(done / len(filtered) * 100, 1) if filtered else 0.0,
        "cost_usd": round(sum(_effective_record_cost_usd(r, prov_cfgs) for r in filtered), 6),
        "prompt_tokens": prompt_tok,
        "completion_tokens": sum(r.get("completion_tokens", 0) or 0 for r in filtered),
        "cached_tokens": cached_tok,
        "cache_savings_usd": round(sum(bm["cache_savings_usd"] for bm in models_out), 6),
        # prompt_tokens already includes cached reads for OpenAI-schema providers;
        # clamped at 100% for providers that report prompt excluding cached.
        "cache_hit_pct": (round(min(cached_tok / prompt_tok, 1.0) * 100, 1) if prompt_tok else 0.0),
        "lines_added": sum(r.get("lines_added", 0) or 0 for r in filtered),
        "lines_removed": sum(r.get("lines_removed", 0) or 0 for r in filtered),
        "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "avg_generation_ms": round(sum(gen_times) / len(gen_times), 1) if gen_times else None,
        "status_counts": status_counts,
        "by_model": models_out,
        "by_provider": providers_out,
        "by_taxonomy": _dim_out(by_taxonomy),
        "by_recipe": _dim_out(by_recipe),
        "by_tier": _dim_out(by_tier),
        "cost_source_counts": cost_source_counts,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "total_steps": total_steps,
        "avg_steps": round(total_steps / len(filtered), 2) if filtered else 0.0,
        "syntax_rejections": syntax_rejections_total,
        "rollback_count": rollback_total,
        "avg_queue_ms": (round(sum(queue_times) / len(queue_times), 1) if queue_times else None),
        "learning_summary": learning_summary,
        "daily": [daily[k] for k in sorted(daily)],
        "recent": recent_out,
    }


_SHADOW_COST_FLOOR_USD = 0.0001

_SHADOW_TAXONOMY_MAP: Dict[str, str] = {
    "bugfix": "bugfix",
    "refactor": "refactor",
    "test": "test",
    "infra": "infra",
    "analysis": "analysis",
    "feature": "feature",
    "general": "general",
    "security": "security",
    "memory": "memory",
}


def _norm_shadow_recipe(recipe: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "", str(recipe or "").strip().lower())[:64]


def _within_days(
    ts_raw: Any, cutoff_naive: Optional[datetime], cutoff_utc: Optional[datetime]
) -> bool:
    if cutoff_naive is None or cutoff_utc is None:
        return True
    try:
        parsed = datetime.fromisoformat(str(ts_raw or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc) >= cutoff_utc
    return parsed >= cutoff_naive


def recommend_shadow_model(
    records: List[Dict[str, Any]],
    *,
    recipe: str = "",
    taxonomy: str = "general",
    min_samples: int = 10,
    days: Optional[int] = 30,
) -> Optional[Dict[str, Any]]:
    """Compute an advisory-only model recommendation per ``(recipe, taxonomy)``.

    Layer C guarantees:
    - Shadow-only: never mutates or overrides active routing decisions.
    - Respects a positive cost floor (``_SHADOW_COST_FLOOR_USD``) for ``:free``
      or zero-cost providers so cost weighting stays finite and reliability dominates.
    - Zero hard-ban: poor performers remain rankable and are never denylisted.
    """
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("min_samples must be an int >= 1")

    target_recipe = _norm_shadow_recipe(recipe)
    target_tax = _SHADOW_TAXONOMY_MAP.get(str(taxonomy or "").strip().lower(), "general")

    cutoff_naive = None
    cutoff_utc = None
    if (
        days is not None
        and not isinstance(days, bool)
        and isinstance(days, (int, float))
        and days > 0
    ):
        cutoff_naive = datetime.now() - timedelta(days=float(days))
        cutoff_utc = datetime.now(timezone.utc) - timedelta(days=float(days))

    matched: List[Dict[str, Any]] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        if not _within_days(r.get("timestamp"), cutoff_naive, cutoff_utc):
            continue
        r_tax = _SHADOW_TAXONOMY_MAP.get(
            str(r.get("taxonomy") or "general").strip().lower(), "general"
        )
        r_rec = _norm_shadow_recipe(r.get("recipe"))
        if r_tax == target_tax and r_rec == target_recipe:
            if str(r.get("model") or "").strip():
                matched.append(r)

    if len(matched) < min_samples:
        return None

    prov_cfgs: Dict[str, Dict[str, Any]] = {}
    per_model: Dict[str, Dict[str, Any]] = {}
    for r in matched:
        m = str(r.get("model") or "").strip()
        pname = str(r.get("provider") or "").strip().lower()
        if pname and pname not in prov_cfgs:
            try:
                _pn, pcfg = _resolve_provider(pname)
                prov_cfgs[pname] = pcfg if isinstance(pcfg, dict) else {}
            except Exception:
                prov_cfgs[pname] = {}
        cost = _effective_record_cost_usd(r, prov_cfgs)
        st = str(r.get("status") or "").strip().upper()
        rb = r.get("rollback_count")
        sr = r.get("syntax_rejections")
        rb_val = int(rb) if isinstance(rb, int) and not isinstance(rb, bool) and rb > 0 else 0
        sr_val = int(sr) if isinstance(sr, int) and not isinstance(sr, bool) and sr > 0 else 0

        agg = per_model.setdefault(
            m,
            {
                "model": m,
                "runs": 0,
                "done_runs": 0,
                "total_cost_usd": 0.0,
                "rollbacks": 0,
                "syntax_rejections": 0,
                "is_free": is_free_model(m, prov_cfgs.get(pname) if pname else None, pname),
            },
        )
        agg["runs"] += 1
        if st == "DONE":
            agg["done_runs"] += 1
        agg["total_cost_usd"] += max(0.0, float(cost))
        agg["rollbacks"] += rb_val
        agg["syntax_rejections"] += sr_val

    if not per_model:
        return None

    scored: List[Dict[str, Any]] = []
    for agg in per_model.values():
        runs = agg["runs"]
        done_runs = agg["done_runs"]
        smoothed_success = (done_runs + 1.0) / (runs + 2.0)
        avg_cost = agg["total_cost_usd"] / runs
        floored_cost = max(avg_cost, _SHADOW_COST_FLOOR_USD)
        quality_penalty = min(
            0.25,
            0.05 * (agg["rollbacks"] / runs) + 0.01 * (agg["syntax_rejections"] / runs),
        )
        cost_bonus = 0.05 * (_SHADOW_COST_FLOOR_USD / floored_cost)
        score = round(smoothed_success - quality_penalty + cost_bonus, 6)
        scored.append(
            {
                "model": agg["model"],
                "runs": runs,
                "done_runs": done_runs,
                "done_rate": round(done_runs / runs, 4),
                "avg_cost_usd": round(avg_cost, 6),
                "floored_cost_usd": round(floored_cost, 6),
                "is_free": agg["is_free"],
                "score": score,
            }
        )

    scored.sort(key=lambda item: (-item["score"], -item["done_runs"], -item["runs"], item["model"]))
    best = scored[0]
    return {
        "shadow_only": True,
        "actual_override": False,
        "recipe": target_recipe,
        "taxonomy": target_tax,
        "recommended_model": best["model"],
        "score": best["score"],
        "done_rate": best["done_rate"],
        "avg_cost_usd": best["avg_cost_usd"],
        "cost_floor_usd": _SHADOW_COST_FLOOR_USD,
        "is_free": best["is_free"],
        "sample_count": len(matched),
        "model_samples": best["runs"],
        "candidates_considered": len(scored),
        "candidates": scored[:5],
        "hard_banned_models": [],
    }


def get_shadow_routing_advice(
    recipe: str = "",
    taxonomy: str = "general",
    *,
    actual_model: str = "",
    records: Optional[List[Dict[str, Any]]] = None,
    min_samples: int = 10,
    days: Optional[int] = 30,
) -> Optional[Dict[str, Any]]:
    """Evaluate shadow meta-routing recommendation from the ledger without altering routing."""
    source_records = load_analytics() if records is None else records
    rec = recommend_shadow_model(
        source_records,
        recipe=recipe,
        taxonomy=taxonomy,
        min_samples=min_samples,
        days=days,
    )
    if rec is None:
        return None
    clean_actual = coerce_text(
        mask_secrets(re.sub(r"[\r\n]+", " ", str(actual_model or "").strip()))
    )[:64]
    rec["actual_model"] = clean_actual
    rec["diverged"] = bool(clean_actual and rec["recommended_model"] != clean_actual)
    return rec


# ---------------------------------------------------------------------------
# CLI Presentation (Rich or Clean ASCII)
# ---------------------------------------------------------------------------
def print_run_summary(entry: Dict[str, Any]):
    """Pretty print run stats to stderr."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console(stderr=True)
        table = Table(title=f"BoteX Run Summary — Task: {entry['task_id']}", show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Status", entry.get("status", "DONE"))
        table.add_row("Model", entry.get("model", "unknown"))
        table.add_row("Duration", f"{entry.get('duration_s', 0)}s ({entry.get('steps', 0)} steps)")
        table.add_row(
            "Tokens",
            f"Prompt: {entry.get('prompt_tokens', 0):,} | "
            f"Comp: {entry.get('completion_tokens', 0):,} | "
            f"Cached: {entry.get('cached_tokens', 0):,}",
        )
        table.add_row("Cost (USD)", f"${entry.get('cost_usd', 0.0):.5f}")
        table.add_row("Files Modified", ", ".join(entry.get("files_touched", [])) or "(none)")
        table.add_row(
            "Line Balance",
            f"[green]+{entry.get('lines_added', 0)}[/green] / [red]-{entry.get('lines_removed', 0)}[/red]",
        )

        console.print(table)
        if entry.get("summary"):
            console.print(Panel(entry["summary"], title="Summary", style="dim"))
    except ImportError:
        # Fallback to plain ASCII (metrics only, no untrusted summary text)
        safe_tid = sanitize_identifier(str(entry.get("task_id") or "")) or "run"
        safe_status = sanitize_identifier(str(entry.get("status") or "DONE")) or "DONE"
        safe_model = sanitize_identifier(str(entry.get("model") or "").replace("/", "_")) or "model"
        dur_s = float(entry.get("duration_s") or 0.0)
        steps_n = int(entry.get("steps") or 0)
        p_tok = int(entry.get("prompt_tokens") or 0)
        c_tok = int(entry.get("completion_tokens") or 0)
        k_tok = int(entry.get("cached_tokens") or 0)
        cost_v = float(entry.get("cost_usd") or 0.0)
        l_add = int(entry.get("lines_added") or 0)
        l_rem = int(entry.get("lines_removed") or 0)
        f_cnt = len(entry.get("files_touched") or [])
        lines = [
            f"=== BOTEX RUN: {safe_tid} ===",
            f"Status: {safe_status} | Model: {safe_model} | Time: {dur_s:.2f}s | Steps: {steps_n}",
            f"Tokens: prompt={p_tok} comp={c_tok} cached={k_tok}",
            f"Cost: ${cost_v:.5f} | Lines: +{l_add}/-{l_rem} | Files: {f_cnt}",
            "=" * 40,
        ]
        sys.stderr.write("\n".join(lines) + "\n")


def cli_stats(period: str = "week"):
    """CLI stats view: weekly or monthly aggregation."""
    from botex.i18n import t

    records = load_analytics()
    if not records:
        print(t("an.no_data"))
        return

    days = 30 if period == "month" else 7
    stats = aggregate_stats(records, days)

    total_runs = stats["runs"]
    total_cost = stats["cost_usd"]
    total_prompt = stats["prompt_tokens"]
    total_comp = stats["completion_tokens"]
    total_cached = stats["cached_tokens"]
    total_added = stats["lines_added"]
    total_removed = stats["lines_removed"]
    by_model = {m["model"]: m for m in stats["by_model"]}

    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        tbl = Table(title=t("an.title", days=days, period=period.upper()))
        tbl.add_column(t("an.metric"), style="cyan")
        tbl.add_column(t("an.value"), style="green")

        tbl.add_row(t("an.tasks"), str(total_runs))
        tbl.add_row(t("an.total_cost"), f"${total_cost:.4f}")
        tbl.add_row(t("an.prompt_tok"), f"{total_prompt:,}")
        tbl.add_row(t("an.comp_tok"), f"{total_comp:,}")
        tbl.add_row(t("an.cached_tok"), f"{total_cached:,}")
        tbl.add_row(t("an.line_bal"), f"+{total_added:,} / -{total_removed:,}")
        console.print(tbl)

        m_table = Table(title=t("an.model_eff"))
        m_table.add_column("Model", style="bold")
        m_table.add_column(t("an.runs"), justify="right")
        m_table.add_column(t("an.done_rate"), justify="right")
        m_table.add_column(t("an.top_failure"), justify="left")
        m_table.add_column(t("an.tokens"), justify="right")
        m_table.add_column(t("an.cost"), justify="right")

        for m, stats in by_model.items():
            top_fail = stats["top_failure"] or "-"
            m_table.add_row(
                m,
                str(stats["runs"]),
                f"{stats['done_pct']:.1f}%",
                top_fail,
                f"{stats['tokens']:,}",
                f"${stats['cost_usd']:.4f}",
            )
        console.print(m_table)
    except ImportError:
        print(f"=== {t('an.title', days=days, period=period.upper())} ===")
        print(f"{t('an.tasks')}: {total_runs}")
        print(f"{t('an.total_cost')}: ${total_cost:.4f} USD")
        print(
            f"{t('an.tokens')}: prompt={total_prompt:,}, comp={total_comp:,}, cached={total_cached:,}"
        )
        print(f"{t('an.line_bal')}: +{total_added:,} / -{total_removed:,}")
        print("\n" + t("an.models"))
        for m, stats in by_model.items():
            done_pct = (stats["done_runs"] / stats["runs"] * 100) if stats["runs"] > 0 else 0.0
            top_fail = (
                max(stats["failures"].items(), key=lambda x: x[1])[0] if stats["failures"] else "-"
            )
            print(
                f"  * {m}: "
                + t(
                    "an.runs_fmt",
                    runs=stats["runs"],
                    tokens=f"{stats['tokens']:,}",
                    cost=f"{stats['cost_usd']:.4f}",
                )
                + f" | DONE rate: {done_pct:.1f}% | Top failure: {top_fail}"
            )


def cli_history(task_id: Optional[str] = None):
    """View task history or details of a specific task."""
    from botex.i18n import t

    records = load_analytics()
    if not records:
        print(t("an.no_history"))
        return

    if task_id:
        match = [
            r
            for r in records
            if r.get("task_id") == task_id or r.get("task_id", "").startswith(task_id)
        ]
        if not match:
            print(t("an.not_found", id=task_id))
            return
        entry = dict(match[-1])
        entry["cost_usd"] = _effective_record_cost_usd(entry)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return

    # List last 10 tasks
    recent = records[-10:]
    recent.reverse()
    print(t("an.recent"))
    for r in recent:
        ts = r.get("timestamp", "")[:16].replace("T", " ")
        eff_cost = _effective_record_cost_usd(r)
        print(
            f"[{r.get('task_id')}] {ts} | {r.get('model')} | {r.get('status')} | ${eff_cost:.4f} | {r.get('summary', '')[:60]}"
        )

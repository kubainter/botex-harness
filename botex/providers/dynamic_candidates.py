# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Dynamic Catalog-Backed Model Candidates — Pure Policy Module
===================================================================
Offline, opt-in dynamic candidate tails for per-provider model chains
(plan 059). Everything in this module is pure stdlib computation over
already-loaded config, catalog snapshots and local analytics rows: no
network, no ``botex.core.engine`` imports, no config mutation.

Hard invariants enforced here:

* mode defaults to ``off`` and an emergency env override can only
  downgrade (``off``/``shadow``), never force ``append``;
* the static candidate head stays byte-identical — dynamic ids only ever
  append after it, deduped on a normalized key;
* allowlist-first, denylist-always; unknown tool support, unknown price,
  unknown/zero context length and non-chat catalog entries are rejected;
* infra failures (rate limit/timeout/quota/auth/budget/provider errors)
  never count as model-quality failures and never trigger quarantine;
* missing/malformed/stale snapshots fail static.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION: int = 1
DEFAULT_SNAPSHOT_PATH: Path = Path(".botex/dynamic_model_candidates.json")
MAX_SNAPSHOT_BYTES: int = 2 * 1024 * 1024
DEFAULT_MAX_SNAPSHOT_AGE_HOURS: float = 72.0

VALID_DYNAMIC_MODES: Dict[str, str] = {"off": "off", "shadow": "shadow", "append": "append"}
VALID_CANDIDATE_STATES: Dict[str, str] = {
    "observed": "observed",
    "eligible": "eligible",
    "active": "active",
    "quarantined": "quarantined",
}
VALID_TAXONOMIES: Dict[str, str] = {
    "bugfix": "bugfix",
    "refactor": "refactor",
    "test": "test",
    "infra": "infra",
    "analysis": "analysis",
    "feature": "feature",
    "general": "general",
}
VALID_CAPABILITY_MODES: Dict[str, str] = {
    "readonly": "readonly",
    "edit": "edit",
    "destructive": "destructive",
    "full": "full",
}
VALID_CANDIDATE_SOURCES: Dict[str, str] = {"static": "static", "dynamic": "dynamic"}

_VALID_CATALOG_STATES: Dict[str, str] = {
    "fresh": "fresh",
    "stale_or_empty": "stale_or_empty",
    "no_data": "no_data",
    "error": "error",
}

_VALID_SNAPSHOT_STATE_CODES: Dict[str, str] = {
    "ok": "ok",
    "not_loaded": "not_loaded",
    "missing": "missing",
    "unreadable": "unreadable",
    "malformed": "malformed",
    "too_large": "too_large",
    "not_a_mapping": "not_a_mapping",
    "unsupported_schema": "unsupported_schema",
    "missing_generated_at": "missing_generated_at",
    "missing_providers": "missing_providers",
    "malformed_provider_entry": "malformed_provider_entry",
    "stale": "stale",
    "invalid": "invalid",
    "error": "error",
}

DEFAULT_DYNAMIC_CANDIDATES_CONFIG: Dict[str, Any] = {
    "mode": "off",
    "snapshot_file": ".botex/dynamic_model_candidates.json",
    "max_per_provider": 2,
    "max_total_per_task": 2,
    "max_snapshot_age_hours": DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
    "providers": {},
    "task_classes": {
        "analysis": {
            "enabled": True,
            "allow_unproven": True,
            "min_context_length": 32768,
            "allow_paid": False,
        },
        "bugfix": {
            "enabled": True,
            "allow_unproven": False,
            "min_context_length": 65536,
            "allow_paid": False,
        },
        "test": {
            "enabled": True,
            "allow_unproven": False,
            "min_context_length": 65536,
            "allow_paid": False,
        },
        "feature": {"enabled": False},
        "refactor": {"enabled": False},
        "infra": {"enabled": False},
        "general": {"enabled": False},
    },
    "modes": {
        "readonly": {"enabled": True},
        "edit": {"enabled": True},
        "destructive": {"enabled": False},
        "full": {"enabled": False},
    },
    "promotion": {
        "window_days": 30,
        "min_runs": 3,
        "min_done_runs": 2,
        "min_done_rate": 0.6,
        "max_rollback_rate": 0.34,
        "max_syntax_rejections_per_run": 0.5,
        "quarantine_after_consecutive_quality_failures": 2,
        "quarantine_hours": 72,
    },
}

# Catalog entries that can never serve the chat/tool loop. ``:batch`` is a
# suffix marker on OpenRouter-style batch ids; the globs catch
# embedding/rerank/audio/moderation style ids.
_NON_CHAT_SUBSTRINGS: Tuple[str, ...] = (":batch",)
_NON_CHAT_GLOBS: Tuple[str, ...] = (
    "*embed*",
    "*rerank*",
    "*whisper*",
    "*tts*",
    "*moderation*",
)

# Provider-side failure vocabulary mirrored from botex.core.meta_harness:
# infra noise is owned by retry/denylist policy, never by model quality.
_INFRA_FAILURE_KINDS = frozenset(
    {
        "rate_limit",
        "timeout",
        "connection_error",
        "auth_error",
        "budget_exceeded",
        "provider_error",
        "quota_exceeded",
        "config_error",
        "request_timeout",
    }
)
_INFRA_KIND_MARKERS: Tuple[str, ...] = (
    "rate_limit",
    "timeout",
    "timed_out",
    "connection",
    "auth",
    "quota",
    "budget",
    "provider_unavailable",
    "provider_policy_denied",
    "provider_error",
    "config_error",
)

# Explicit model-quality failure vocabulary (editing/contract behavior).
_QUALITY_FAILURE_KINDS = frozenset(
    {
        "no_progress",
        "contract_failed",
        "contract_failure",
        "model_refusal",
        "turns_exhausted",
        "syntax_rejected",
    }
)

_STATE_RANK: Dict[str, int] = {"active": 0, "eligible": 1, "quarantined": 2, "observed": 3}

_MAX_ID_LEN: int = 256
_MAX_GENERATOR_LEN: int = 64


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return default
    return default


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except (ValueError, TypeError):
            return None
        return f if math.isfinite(f) else None
    return None


def _pattern_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().lower())
    return out


def _norm_failure_kind(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def normalize_dynamic_mode(configured_mode: Any, *, emergency_env: str | None = None) -> str:
    """Canonicalize the dynamic-candidate mode.

    ``emergency_env`` is an operator brake: ``off`` forces off and
    ``shadow`` forces shadow regardless of file config. ``append`` or any
    invalid value is ignored — env can never escalate the feature on.
    """
    mode = VALID_DYNAMIC_MODES.get(str(configured_mode or "").strip().lower(), "off")
    env = str(emergency_env or "").strip().lower()
    if env == "off":
        return "off"
    if env == "shadow":
        return "shadow"
    return mode


def normalize_model_key(model_id: str) -> str:
    """Normalize a model id for dedupe/matching: trim, lowercase, one
    leading ``models/`` prefix stripped (Google-style catalog ids)."""
    key = str(model_id or "").strip().lower()
    if key.startswith("models/"):
        key = key[len("models/") :]
    return key


def is_infra_failure_row(status: str | None, failure_kind: str | None) -> bool:
    """Whether an analytics row describes provider-side noise, not model
    quality. ``ABORTED`` is operator-side and excluded from both counts."""
    if str(status or "").strip().upper() == "ABORTED":
        return True
    norm = _norm_failure_kind(failure_kind)
    if not norm:
        return False
    if norm in _INFRA_FAILURE_KINDS:
        return True
    return any(marker in norm for marker in _INFRA_KIND_MARKERS)


def is_quality_failure_row(row: Mapping[str, Any]) -> bool:
    """Whether an analytics row is a model-quality failure: non-infra rows
    with syntax rejections, rollbacks, a non-DONE status, or an explicit
    quality failure kind. Non-mapping input returns False (skipped)."""
    if not isinstance(row, Mapping):
        return False
    if is_infra_failure_row(row.get("status"), row.get("failure_kind")):
        return False
    if (_safe_int(row.get("syntax_rejections"), 0) or 0) > 0:
        return True
    if (_safe_int(row.get("rollback_count"), 0) or 0) > 0:
        return True
    if str(row.get("status") or "").strip().upper() != "DONE":
        return True
    return _norm_failure_kind(row.get("failure_kind")) in _QUALITY_FAILURE_KINDS


def _metadata_get(metadata: Any, key: str) -> Any:
    """Duck-typed metadata read: attribute first, then mapping key."""
    if metadata is None:
        return None
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return getattr(metadata, key, None)


def _capability_override(
    provider_cfg: Mapping[str, Any], model_id: str, norm_key: str
) -> Optional[Mapping[str, Any]]:
    for source_key in ("model_capabilities", "capabilities"):
        caps_map = provider_cfg.get(source_key)
        if not isinstance(caps_map, Mapping):
            continue
        for key in (model_id, model_id.strip(), norm_key):
            entry = caps_map.get(key)
            if isinstance(entry, Mapping):
                return entry
        for raw_key, entry in caps_map.items():
            if (
                isinstance(raw_key, str)
                and normalize_model_key(raw_key) == norm_key
                and isinstance(entry, Mapping)
            ):
                return entry
    return None


def _pricing_override(
    provider_cfg: Mapping[str, Any], model_id: str, norm_key: str
) -> Tuple[Optional[float], Optional[float]]:
    """Operator-declared prices (per 1M tokens): per-model maps under
    ``pricing_overrides``/``pricing``, or a flat pricing dict."""
    for source_key in ("pricing_overrides", "pricing"):
        section = provider_cfg.get(source_key)
        if not isinstance(section, Mapping):
            continue
        entry: Optional[Mapping[str, Any]] = None
        for key in (model_id, model_id.strip(), norm_key):
            candidate = section.get(key)
            if isinstance(candidate, Mapping):
                entry = candidate
                break
        if entry is None:
            for raw_key, candidate in section.items():
                if (
                    isinstance(raw_key, str)
                    and normalize_model_key(raw_key) == norm_key
                    and isinstance(candidate, Mapping)
                ):
                    entry = candidate
                    break
        if entry is None and any(
            k in section
            for k in (
                "prompt",
                "input",
                "prompt_price_per_m",
                "completion",
                "output",
                "completion_price_per_m",
            )
        ):
            entry = section
        if entry is None:
            continue
        prompt = None
        for k in ("prompt_price_per_m", "prompt", "input"):
            prompt = _safe_float(entry.get(k))
            if prompt is not None:
                break
        completion = None
        for k in ("completion_price_per_m", "completion", "output"):
            completion = _safe_float(entry.get(k))
            if completion is not None:
                break
        if prompt is not None or completion is not None:
            return prompt, completion
    return None, None


def _resolve_price(
    provider: str,
    model_id: str,
    norm_key: str,
    metadata: Any,
    provider_cfg: Mapping[str, Any],
) -> Tuple[str, Optional[float], Optional[float]]:
    """Price gate: override > catalog metadata > ``:free`` slug >
    provider free-tier flag. ``unknown`` when no finite non-negative pair."""
    free_slug = ":free" in norm_key or "openrouter/free" in norm_key
    provider_free = (
        provider_cfg.get("free_tier") is True
        or provider_cfg.get("is_free_tier") is True
        or provider.strip().lower().endswith("-free")
    )
    over_p, over_c = _pricing_override(provider_cfg, model_id, norm_key)
    prompt = (
        over_p if over_p is not None else _safe_float(_metadata_get(metadata, "prompt_price_per_m"))
    )
    completion = (
        over_c
        if over_c is not None
        else _safe_float(_metadata_get(metadata, "completion_price_per_m"))
    )
    if free_slug or provider_free:
        return (
            "free",
            prompt if prompt is not None else 0.0,
            completion if completion is not None else 0.0,
        )
    if prompt is not None and completion is not None and prompt >= 0.0 and completion >= 0.0:
        if prompt == 0.0 and completion == 0.0:
            return "free", prompt, completion
        return "paid", prompt, completion
    return "unknown", prompt, completion


def _allowlist_check(
    dynamic_cfg: Mapping[str, Any], provider: str, norm_key: str
) -> Tuple[bool, str]:
    providers = dynamic_cfg.get("providers")
    if not isinstance(providers, Mapping):
        return False, "provider_not_configured"
    entry = providers.get(provider)
    if not isinstance(entry, Mapping):
        lowered = provider.strip().lower()
        for raw_key, candidate in providers.items():
            if (
                isinstance(raw_key, str)
                and raw_key.strip().lower() == lowered
                and isinstance(candidate, Mapping)
            ):
                entry = candidate
                break
    if not isinstance(entry, Mapping):
        return False, "provider_not_configured"
    if entry.get("enabled") is not True:
        return False, "provider_not_enabled"
    allow = _pattern_list(entry.get("allow_models"))
    if not allow:
        return False, "empty_allowlist"
    if not any(fnmatch.fnmatchcase(norm_key, pat) for pat in allow):
        return False, "allowlist_mismatch"
    deny = _pattern_list(entry.get("deny_models"))
    if any(fnmatch.fnmatchcase(norm_key, pat) for pat in deny):
        return False, "provider_deny_models"
    return True, ""


def _call_denylist(denylist_fn: Callable[[str], bool] | None, *model_ids: str) -> bool:
    """Fail-closed denylist probe: an exception or truthy return denies."""
    if denylist_fn is None:
        return False
    for mid in model_ids:
        if not mid:
            continue
        try:
            if denylist_fn(mid):
                return True
        except Exception:
            return True
    return False


def _is_non_chat_entry(norm_key: str) -> bool:
    if any(marker in norm_key for marker in _NON_CHAT_SUBSTRINGS):
        return True
    return any(fnmatch.fnmatchcase(norm_key, pat) for pat in _NON_CHAT_GLOBS)


def _decisions_model_match(provider_cfg: Mapping[str, Any], model_id: str, norm_key: str) -> bool:
    patterns = _pattern_list(provider_cfg.get("decisions_models"))
    raw_lower = str(model_id or "").strip().lower()
    return any(
        fnmatch.fnmatchcase(raw_lower, pat) or fnmatch.fnmatchcase(norm_key, pat)
        for pat in patterns
    )


def evaluate_candidate_gates(
    *,
    provider: str,
    model_id: str,
    metadata: Any,
    dynamic_cfg: Mapping[str, Any],
    provider_cfg: Mapping[str, Any] | None = None,
    denylist_fn: Callable[[str], bool] | None = None,
) -> Dict[str, Any]:
    """Evaluate the hard gates for one catalog entry. Pure function.

    Returns ``{"passed_hard_gates", "gates", "reasons"}`` where ``gates``
    records allowlist/denylist/tool/price/ZDR/context outcomes and
    ``reasons`` is a bounded list of machine-readable rejection codes.
    """
    provider_str = str(provider or "").strip()
    model_str = str(model_id or "").strip()
    norm_key = normalize_model_key(model_str)
    pcfg: Mapping[str, Any] = provider_cfg if isinstance(provider_cfg, Mapping) else {}
    dcfg: Mapping[str, Any] = dynamic_cfg if isinstance(dynamic_cfg, Mapping) else {}

    reasons: List[str] = []
    allowlisted, allow_reason = _allowlist_check(dcfg, provider_str, norm_key)
    if not allowlisted and allow_reason:
        reasons.append(allow_reason)

    denylisted = _call_denylist(denylist_fn, model_str, norm_key)
    if denylisted:
        reasons.append("denylisted")

    if not norm_key:
        reasons.append("empty_model_id")
    elif _is_non_chat_entry(norm_key):
        reasons.append("non_chat_entry")
    elif _decisions_model_match(pcfg, model_str, norm_key):
        reasons.append("decisions_only")

    caps = _capability_override(pcfg, model_str, norm_key)
    if caps is not None and "supports_tools" in caps:
        supports_tools = bool(caps.get("supports_tools"))
        if not supports_tools:
            reasons.append("tools_unsupported")
    else:
        raw_tools = _metadata_get(metadata, "supports_tools")
        supports_tools = bool(raw_tools) if raw_tools is not None else False
        if raw_tools is None:
            reasons.append("tools_unknown")
        elif not supports_tools:
            reasons.append("tools_unsupported")

    price_kind, prompt_price, completion_price = _resolve_price(
        provider_str, model_str, norm_key, metadata, pcfg
    )
    if price_kind == "unknown":
        reasons.append("price_unknown")

    free_slug = ":free" in norm_key or "openrouter/free" in norm_key
    zdr_raw = _metadata_get(metadata, "is_zdr_compliant")
    if caps is not None and "is_zdr_compliant" in caps:
        zdr_raw = caps.get("is_zdr_compliant")
    zdr_supported = not (free_slug or zdr_raw is False)

    context_length = 0
    if caps is not None and "context_length" in caps:
        context_length = _safe_int(caps.get("context_length"), 0) or 0
    else:
        context_length = _safe_int(_metadata_get(metadata, "context_length"), 0) or 0
    if context_length <= 0:
        context_length = 0
        reasons.append("context_unknown")

    passed = (
        bool(norm_key)
        and allowlisted
        and not denylisted
        and supports_tools
        and price_kind != "unknown"
        and context_length > 0
        and "non_chat_entry" not in reasons
        and "decisions_only" not in reasons
        and "empty_model_id" not in reasons
    )

    return {
        "passed_hard_gates": passed,
        "gates": {
            "allowlisted": allowlisted,
            "denylisted": denylisted,
            "supports_tools": supports_tools,
            "price": price_kind,
            "prompt_price": prompt_price,
            "completion_price": completion_price,
            "zdr": "supported" if zdr_supported else "not_supported",
            "context_length": context_length,
        },
        "reasons": reasons,
    }


def _row_timestamp(row: Mapping[str, Any]) -> Optional[float]:
    """Parse ``ts``/``timestamp`` as epoch seconds; ISO strings treated as
    UTC when naive so fixed-clock evidence stays deterministic."""
    for key in ("ts", "timestamp"):
        raw = row.get(key)
        if raw is None:
            continue
        numeric = _safe_float(raw)
        if numeric is not None:
            return numeric
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return None


def _classify_row(row: Mapping[str, Any]) -> str:
    if is_infra_failure_row(row.get("status"), row.get("failure_kind")):
        return "infra"
    if is_quality_failure_row(row):
        return "quality"
    return "done"


def _empty_evidence_counts() -> Dict[str, float]:
    return {
        "runs": 0,
        "done_runs": 0,
        "quality_failures": 0,
        "infra_failures": 0,
        "rollback_count": 0,
        "syntax_rejections": 0,
    }


def _rank_score(evidence: Mapping[str, Any]) -> float:
    """Deterministic 0..1 evidence score: clean-DONE rate credited, quality
    failure signals debited, infra noise only lightly discounted."""
    runs = _safe_int(evidence.get("runs"), 0) or 0
    if runs <= 0:
        return 0.0
    done = _safe_int(evidence.get("done_runs"), 0) or 0
    quality = _safe_int(evidence.get("quality_failures"), 0) or 0
    infra = _safe_int(evidence.get("infra_failures"), 0) or 0
    rollbacks = _safe_int(evidence.get("rollback_count"), 0) or 0
    syntax = _safe_int(evidence.get("syntax_rejections"), 0) or 0
    done_rate = done / runs
    quality_rate = quality / runs
    infra_rate = infra / runs
    rollback_rate = rollbacks / runs
    syntax_rate = syntax / runs
    score = (
        done_rate
        - 0.5 * quality_rate
        - 0.5 * rollback_rate
        - 0.25 * min(syntax_rate, 2.0)
        - 0.1 * infra_rate
    )
    return round(max(0.0, min(1.0, score)), 4)


def _merged_dynamic_cfg(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    base: Dict[str, Any] = copy.deepcopy(DEFAULT_DYNAMIC_CANDIDATES_CONFIG)
    section = cfg.get("dynamic_candidates") if isinstance(cfg, Mapping) else None
    if not isinstance(section, Mapping):
        return base

    def _merge(dst: Dict[str, Any], src: Mapping[str, Any]) -> None:
        for key, value in src.items():
            if isinstance(value, Mapping) and isinstance(dst.get(key), dict):
                _merge(dst[key], value)
            else:
                dst[key] = value

    _merge(base, section)
    return base


def _positive_thresholds_met(evidence: Mapping[str, Any], promo: Mapping[str, Any]) -> bool:
    runs = _safe_int(evidence.get("runs"), 0) or 0
    done = _safe_int(evidence.get("done_runs"), 0) or 0
    rollbacks = _safe_int(evidence.get("rollback_count"), 0) or 0
    syntax = _safe_int(evidence.get("syntax_rejections"), 0) or 0
    min_runs = _safe_int(promo.get("min_runs"), 0) or 0
    min_done = _safe_int(promo.get("min_done_runs"), 0) or 0
    min_done_rate = _safe_float(promo.get("min_done_rate"))
    max_rollback_rate = _safe_float(promo.get("max_rollback_rate"))
    max_syntax_per_run = _safe_float(promo.get("max_syntax_rejections_per_run"))
    if runs < min_runs or done < min_done:
        return False
    if runs <= 0:
        return False
    if min_done_rate is not None and (done / runs) < min_done_rate:
        return False
    if max_rollback_rate is not None and (rollbacks / runs) > max_rollback_rate:
        return False
    if max_syntax_per_run is not None and (syntax / runs) > max_syntax_per_run:
        return False
    return True


def _evidence_block(evidence: Mapping[str, Any], window_days: float) -> Dict[str, Any]:
    return {
        "window_days": window_days,
        "runs": _safe_int(evidence.get("runs"), 0) or 0,
        "done_runs": _safe_int(evidence.get("done_runs"), 0) or 0,
        "quality_failures": _safe_int(evidence.get("quality_failures"), 0) or 0,
        "infra_failures": _safe_int(evidence.get("infra_failures"), 0) or 0,
        "rollback_count": _safe_int(evidence.get("rollback_count"), 0) or 0,
        "syntax_rejections": _safe_int(evidence.get("syntax_rejections"), 0) or 0,
    }


def build_dynamic_candidates_snapshot(
    *,
    cfg: Mapping[str, Any],
    catalogs: Mapping[str, Mapping[str, Any]],
    analytics_rows: Iterable[Mapping[str, Any]],
    catalog_fetched_at_map: Mapping[str, float] | None = None,
    catalog_drift_generated_at: float | None = None,
    existing_snapshot: Mapping[str, Any] | None = None,
    denylist_check: Callable[[str, str], bool] | None = None,
    now_ts: float | None = None,
    generator: str = "night-pulse",
) -> Dict[str, Any]:
    """Build the schema-v1 dynamic-candidate snapshot offline.

    Inputs are cached catalogs (``{provider: {model_id: metadata}}``), the
    full config mapping, and local analytics rows. Evidence is keyed by
    ``(provider, normalize_model_key(model), taxonomy)`` inside the rolling
    ``promotion.window_days`` window ending at ``now_ts``. Never raises on
    malformed rows; per-provider errors are isolated into ``catalog_state``.
    ``denylist_check`` is probed as ``denylist_check(model_id, provider)``.
    """
    now = _safe_float(now_ts)
    if now is None:
        now = time.time()
    cfg_m: Mapping[str, Any] = cfg if isinstance(cfg, Mapping) else {}
    dynamic_cfg = _merged_dynamic_cfg(cfg_m)
    providers_cfg = cfg_m.get("providers") if isinstance(cfg_m.get("providers"), Mapping) else {}
    promo = (
        dynamic_cfg.get("promotion") if isinstance(dynamic_cfg.get("promotion"), Mapping) else {}
    )
    task_classes_cfg = (
        dynamic_cfg.get("task_classes")
        if isinstance(dynamic_cfg.get("task_classes"), Mapping)
        else {}
    )
    window_days = _safe_float(promo.get("window_days"))
    window_days = window_days if window_days is not None and window_days > 0 else 30.0
    window_start = now - window_days * 86400.0
    quarantine_streak = (
        _safe_int(promo.get("quarantine_after_consecutive_quality_failures"), 2) or 2
    )
    quarantine_seconds = (_safe_float(promo.get("quarantine_hours")) or 72.0) * 3600.0

    # --- Evidence aggregation: (provider_lower, model_key) -> buckets ---
    evidence: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rows_iter: Iterable[Any] = analytics_rows if isinstance(analytics_rows, Iterable) else ()
    for row in rows_iter:
        if not isinstance(row, Mapping):
            continue
        provider_key = str(row.get("provider") or "").strip().lower()
        model_key = normalize_model_key(str(row.get("model") or ""))
        if not provider_key or not model_key:
            continue
        ts = _row_timestamp(row)
        if ts is None or ts < window_start or ts > now:
            continue
        taxonomy = VALID_TAXONOMIES.get(str(row.get("taxonomy") or "").strip().lower(), "general")
        kind = _classify_row(row)
        bucket = evidence.setdefault(
            (provider_key, model_key),
            {
                "total": _empty_evidence_counts(),
                "per_tax": {},
                "modes": {},
                "stream": [],
            },
        )
        total = bucket["total"]
        tax_bucket = bucket["per_tax"].setdefault(taxonomy, _empty_evidence_counts())
        for counts in (total, tax_bucket):
            counts["runs"] += 1
            if kind == "done":
                counts["done_runs"] += 1
            elif kind == "quality":
                counts["quality_failures"] += 1
            else:
                counts["infra_failures"] += 1
            counts["rollback_count"] += _safe_int(row.get("rollback_count"), 0) or 0
            counts["syntax_rejections"] += _safe_int(row.get("syntax_rejections"), 0) or 0
        row_mode = VALID_CAPABILITY_MODES.get(str(row.get("mode") or "").strip().lower())
        if row_mode:
            bucket["modes"][row_mode] = bucket["modes"].get(row_mode, 0) + 1
        bucket["stream"].append((ts, kind))

    fetched_map = catalog_fetched_at_map if isinstance(catalog_fetched_at_map, Mapping) else {}
    existing_providers = {}
    if isinstance(existing_snapshot, Mapping) and isinstance(
        existing_snapshot.get("providers"), Mapping
    ):
        existing_providers = existing_snapshot["providers"]

    provider_names = set()
    if isinstance(catalogs, Mapping):
        provider_names.update(str(k) for k in catalogs.keys() if isinstance(k, str))
    provider_names.update(str(k) for k in existing_providers.keys() if isinstance(k, str))
    dyn_providers = dynamic_cfg.get("providers")
    if isinstance(dyn_providers, Mapping):
        provider_names.update(str(k) for k in dyn_providers.keys() if isinstance(k, str))

    providers_out: Dict[str, Any] = {}
    for provider in sorted(provider_names, key=lambda p: p.strip().lower()):
        provider_key = provider.strip().lower()
        fetched_at = _safe_float(fetched_map.get(provider) or fetched_map.get(provider_key))
        try:
            catalog = catalogs.get(provider) if isinstance(catalogs, Mapping) else None
            if not isinstance(catalog, Mapping) or not catalog:
                prev = existing_providers.get(provider) or existing_providers.get(provider_key)
                if isinstance(prev, Mapping):
                    entry = dict(prev)
                    entry["catalog_state"] = _VALID_CATALOG_STATES["stale_or_empty"]
                    entry["catalog_fetched_at"] = fetched_at
                    providers_out[provider] = entry
                else:
                    providers_out[provider] = {
                        "catalog_fetched_at": fetched_at,
                        "catalog_state": _VALID_CATALOG_STATES["no_data"],
                        "candidates": [],
                    }
                continue

            provider_cfg = providers_cfg.get(provider) or providers_cfg.get(provider_key)
            if not isinstance(provider_cfg, Mapping):
                provider_cfg = {}

            def _deny(mid: str, _p: str = provider) -> bool:
                if denylist_check is None:
                    return False
                try:
                    return bool(denylist_check(mid, _p))
                except Exception:
                    return True

            candidates: List[Dict[str, Any]] = []
            for raw_id, metadata in sorted(
                catalog.items(), key=lambda kv: normalize_model_key(str(kv[0]))
            ):
                model_str = str(raw_id or "").strip()
                norm_key = normalize_model_key(model_str)
                if not norm_key:
                    continue
                if len(model_str) > _MAX_ID_LEN:
                    model_str = model_str[:_MAX_ID_LEN]

                gates = evaluate_candidate_gates(
                    provider=provider,
                    model_id=model_str,
                    metadata=metadata,
                    dynamic_cfg=dynamic_cfg,
                    provider_cfg=provider_cfg,
                    denylist_fn=_deny,
                )
                bucket = evidence.get((provider_key, norm_key)) or {
                    "total": _empty_evidence_counts(),
                    "per_tax": {},
                    "modes": {},
                    "stream": [],
                }
                total_ev = bucket["total"]

                streak = 0
                last_quality_ts: Optional[float] = None
                for ts, kind in sorted(bucket["stream"], key=lambda p: p[0], reverse=True):
                    if kind == "infra":
                        continue
                    if kind == "quality":
                        streak += 1
                        if last_quality_ts is None:
                            last_quality_ts = ts
                    else:
                        break
                quarantined = (
                    streak >= quarantine_streak > 0
                    and last_quality_ts is not None
                    and (now - last_quality_ts) <= quarantine_seconds
                )

                active_classes: List[str] = []
                if gates["passed_hard_gates"]:
                    for tax in VALID_TAXONOMIES:
                        policy = task_classes_cfg.get(tax)
                        if not isinstance(policy, Mapping) or policy.get("enabled") is not True:
                            continue
                        min_ctx = _safe_int(policy.get("min_context_length"), 0) or 0
                        if gates["gates"]["context_length"] < min_ctx:
                            continue
                        if (
                            policy.get("allow_paid") is not True
                            and gates["gates"]["price"] == "paid"
                        ):
                            continue
                        if _positive_thresholds_met(bucket["per_tax"].get(tax) or {}, promo):
                            active_classes.append(tax)

                if not gates["passed_hard_gates"]:
                    state = VALID_CANDIDATE_STATES["observed"]
                elif quarantined:
                    state = VALID_CANDIDATE_STATES["quarantined"]
                elif active_classes:
                    state = VALID_CANDIDATE_STATES["active"]
                else:
                    state = VALID_CANDIDATE_STATES["eligible"]

                entry: Dict[str, Any] = {
                    "model": model_str,
                    "state": state,
                    "rank_score": _rank_score(total_ev),
                    "gates": gates["gates"],
                    "evidence": _evidence_block(total_ev, window_days),
                    "task_classes": active_classes,
                    "reasons": gates["reasons"],
                }
                if quarantined and last_quality_ts is not None:
                    until = last_quality_ts + quarantine_seconds
                    entry["quarantined_until"] = until
                    entry["quarantined_until_iso"] = datetime.fromtimestamp(
                        until, timezone.utc
                    ).isoformat()
                per_tax_block = {
                    tax: _evidence_block(ev, window_days)
                    for tax, ev in bucket["per_tax"].items()
                    if tax in VALID_TAXONOMIES
                }
                if per_tax_block:
                    entry["per_task_class"] = per_tax_block
                if bucket["modes"]:
                    entry["capability_modes"] = dict(sorted(bucket["modes"].items()))
                candidates.append(entry)

            candidates.sort(
                key=lambda e: (
                    _STATE_RANK.get(str(e.get("state")), 3),
                    -(e.get("rank_score") or 0.0),
                    normalize_model_key(str(e.get("model") or "")),
                )
            )
            providers_out[provider] = {
                "catalog_fetched_at": fetched_at,
                "catalog_state": _VALID_CATALOG_STATES["fresh"],
                "candidates": candidates,
            }
        except Exception:
            prev = existing_providers.get(provider) or existing_providers.get(provider_key)
            if isinstance(prev, Mapping):
                entry = dict(prev)
                entry["catalog_state"] = _VALID_CATALOG_STATES["stale_or_empty"]
                entry["catalog_fetched_at"] = fetched_at
                providers_out[provider] = entry
            else:
                providers_out[provider] = {
                    "catalog_fetched_at": fetched_at,
                    "catalog_state": _VALID_CATALOG_STATES["error"],
                    "candidates": [],
                }

    gen_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "generated_at_iso": gen_iso,
        "generator": str(generator or "night-pulse")[:_MAX_GENERATOR_LEN],
        "source": {
            "analytics_window_days": window_days,
            "catalog_drift_generated_at": _safe_float(catalog_drift_generated_at),
        },
        "providers": providers_out,
    }


def validate_dynamic_snapshot(
    data: Any,
    *,
    now_ts: float | None = None,
    max_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Validate a snapshot artifact; returns ``(snapshot, "ok")`` or
    ``(None, <reason>)``. Unknown schema versions fail closed."""
    if not isinstance(data, Mapping):
        return None, "not_a_mapping"
    version = _safe_int(data.get("schema_version"))
    if version != SCHEMA_VERSION:
        return None, "unsupported_schema"
    generated_at = _safe_float(data.get("generated_at"))
    if generated_at is None or generated_at <= 0.0:
        return None, "missing_generated_at"
    now = _safe_float(now_ts)
    if now is None:
        now = time.time()
    max_age = _safe_float(max_age_hours)
    if max_age is None or max_age < 0.0:
        max_age = DEFAULT_MAX_SNAPSHOT_AGE_HOURS
    if (now - generated_at) > max_age * 3600.0:
        return None, "stale"
    providers = data.get("providers")
    if not isinstance(providers, Mapping):
        return None, "missing_providers"
    for raw_name, entry in providers.items():
        if not isinstance(raw_name, str) or not isinstance(entry, Mapping):
            return None, "malformed_provider_entry"
        candidates = entry.get("candidates")
        if candidates is not None and not isinstance(candidates, list):
            return None, "malformed_provider_entry"
        for cand in candidates or []:
            if not isinstance(cand, Mapping) or not isinstance(cand.get("model"), str):
                return None, "malformed_provider_entry"
    return dict(data), "ok"


def load_dynamic_snapshot_offline(
    path: Path | str | None = None,
    *,
    now_ts: float | None = None,
    max_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read and validate the snapshot artifact from disk only. Never raises;
    every failure mode returns ``(None, <bounded reason>)``."""
    if path is None:
        try:
            from botex.server.config import PROJECT_ROOT

            resolved = Path(PROJECT_ROOT) / DEFAULT_SNAPSHOT_PATH
        except Exception:
            resolved = Path(DEFAULT_SNAPSHOT_PATH)
    else:
        resolved = Path(path)
    try:
        if not resolved.exists() or not resolved.is_file():
            return None, "missing"
        size = resolved.stat().st_size
        if size > MAX_SNAPSHOT_BYTES:
            return None, "too_large"
        raw = resolved.read_text(encoding="utf-8")
    except OSError:
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, "malformed"
    return validate_dynamic_snapshot(data, now_ts=now_ts, max_age_hours=max_age_hours)


def _result_payload(
    static_list: List[str],
    tail: List[str],
    mode: str,
    snapshot_state: str,
    excluded: Dict[str, List[str]],
) -> Dict[str, Any]:
    return {
        "candidates": static_list + tail if mode == "append" else list(static_list),
        "static_candidates": list(static_list),
        "dynamic_candidates": list(tail),
        "would_append": list(tail),
        "dynamic_mode": mode,
        "snapshot_state": snapshot_state,
        "excluded_reasons": excluded,
    }


def _diag_state(value: Any) -> str:
    return _VALID_SNAPSHOT_STATE_CODES.get(str(value or "").strip().lower(), "invalid")


def resolve_execution_candidates(
    static_candidates: Sequence[str],
    *,
    provider: str,
    profile: str = "default",
    task_class: str = "general",
    capability_mode: str = "edit",
    explicit_model: str = "",
    cfg: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    snapshot_state: str = "not_loaded",
    zdr_required: bool = False,
    denylist_fn: Callable[[str], bool] | None = None,
    remaining_task_cap: int | None = None,
    emergency_env: str | None = None,
) -> Dict[str, Any]:
    """Resolve the final per-provider candidate chain: static head plus an
    optional dynamic tail. Total function — any bad input, snapshot or
    internal failure returns the static chain unchanged with a bounded
    ``snapshot_state`` diagnostic.
    """
    if isinstance(static_candidates, str):
        static_list = [static_candidates]
    elif isinstance(static_candidates, Sequence):
        static_list = list(static_candidates)
    else:
        try:
            static_list = list(static_candidates)  # type: ignore[arg-type]
        except Exception:
            static_list = []
    mode = "off"
    try:
        cfg_m: Mapping[str, Any] = cfg if isinstance(cfg, Mapping) else {}
        dynamic_cfg = _merged_dynamic_cfg(cfg_m)
        mode = normalize_dynamic_mode(dynamic_cfg.get("mode"), emergency_env=emergency_env)

        if str(explicit_model or "").strip():
            return _result_payload(static_list, [], mode, _diag_state(snapshot_state), {})
        if mode == "off":
            return _result_payload(static_list, [], mode, _diag_state(snapshot_state), {})

        if snapshot is None:
            diag = _diag_state(snapshot_state)
            if diag == "ok":
                diag = "missing"
            return _result_payload(static_list, [], mode, diag, {})
        validated, reason = validate_dynamic_snapshot(snapshot)
        if validated is None:
            return _result_payload(static_list, [], mode, reason, {})

        cap_mode = VALID_CAPABILITY_MODES.get(str(capability_mode or "").strip().lower())
        tax = VALID_TAXONOMIES.get(str(task_class or "").strip().lower(), "general")
        modes_cfg = (
            dynamic_cfg.get("modes") if isinstance(dynamic_cfg.get("modes"), Mapping) else {}
        )
        task_cfg = (
            dynamic_cfg.get("task_classes")
            if isinstance(dynamic_cfg.get("task_classes"), Mapping)
            else {}
        )
        mode_policy = modes_cfg.get(cap_mode) if cap_mode else None
        class_policy = task_cfg.get(tax) if isinstance(task_cfg.get(tax), Mapping) else {}

        provider_key = str(provider or "").strip().lower()
        snap_providers = validated.get("providers") or {}
        provider_entry = snap_providers.get(provider) if isinstance(provider, str) else None
        if not isinstance(provider_entry, Mapping):
            for raw_name, entry in snap_providers.items():
                if isinstance(raw_name, str) and raw_name.strip().lower() == provider_key:
                    provider_entry = entry
                    break
        candidates_raw = (
            provider_entry.get("candidates") if isinstance(provider_entry, Mapping) else None
        )
        if not isinstance(candidates_raw, list):
            candidates_raw = []

        providers_cfg = (
            cfg_m.get("providers") if isinstance(cfg_m.get("providers"), Mapping) else {}
        )
        provider_cfg = providers_cfg.get(provider)
        if not isinstance(provider_cfg, Mapping):
            for raw_name, entry in providers_cfg.items():
                if (
                    isinstance(raw_name, str)
                    and raw_name.strip().lower() == provider_key
                    and isinstance(entry, Mapping)
                ):
                    provider_cfg = entry
                    break
        if not isinstance(provider_cfg, Mapping):
            provider_cfg = {}

        static_norms = {normalize_model_key(m) for m in static_list if normalize_model_key(m)}
        excluded: Dict[str, List[str]] = {}
        survivors: List[Tuple[int, float, str, str]] = []
        mode_enabled = isinstance(mode_policy, Mapping) and mode_policy.get("enabled") is True
        class_enabled = class_policy.get("enabled") is True
        unproven_ok = class_policy.get("allow_unproven") is True

        for cand in candidates_raw:
            if not isinstance(cand, Mapping):
                continue
            model_str = str(cand.get("model") or "").strip()
            norm_key = normalize_model_key(model_str)
            if not norm_key:
                continue
            fails: List[str] = []
            state = VALID_CANDIDATE_STATES.get(str(cand.get("state") or "").strip().lower())
            gates = cand.get("gates") if isinstance(cand.get("gates"), Mapping) else {}

            # ``active`` is per task class: for a class the model has not
            # earned it is an unproven probe, selectable only when that class
            # policy opts in via ``allow_unproven``.
            state_rank = _STATE_RANK.get(state or "", 3)
            if not mode_enabled:
                fails.append("capability_mode_disabled")
            if not class_enabled:
                fails.append("task_class_disabled")
            if state == "quarantined":
                fails.append("quarantined")
            elif state == "observed" or state is None:
                fails.append("gates_failed")
            elif state == "active":
                classes = cand.get("task_classes")
                canon_classes = (
                    {
                        VALID_TAXONOMIES.get(str(t).strip().lower())
                        for t in classes
                        if isinstance(t, str)
                    }
                    if isinstance(classes, list)
                    else set()
                )
                if tax not in canon_classes:
                    if unproven_ok:
                        state_rank = _STATE_RANK["eligible"]
                    else:
                        fails.append("task_class_not_active")
            elif state == "eligible":
                if not unproven_ok:
                    fails.append("unproven")

            if norm_key in static_norms:
                fails.append("duplicate_static")

            allowlisted, allow_reason = _allowlist_check(dynamic_cfg, provider_key, norm_key)
            if not allowlisted:
                fails.append(allow_reason or "not_allowlisted")
            if _call_denylist(denylist_fn, model_str, norm_key):
                fails.append("denylisted")
            if _is_non_chat_entry(norm_key):
                fails.append("non_chat_entry")
            if _decisions_model_match(provider_cfg, model_str, norm_key):
                fails.append("decisions_only")

            if gates.get("supports_tools") is not True:
                fails.append("tools_unsupported")
            price_kind = str(gates.get("price") or "unknown")
            if price_kind == "unknown":
                fails.append("price_unknown")
            elif price_kind == "paid" and class_policy.get("allow_paid") is not True:
                fails.append("paid_not_allowed")
            if price_kind == "paid":
                prompt_price = _safe_float(gates.get("prompt_price"))
                completion_price = _safe_float(gates.get("completion_price"))
                max_any = _safe_float(class_policy.get("max_price_per_m"))
                max_prompt = _safe_float(class_policy.get("max_prompt_price_per_m"))
                max_completion = _safe_float(class_policy.get("max_completion_price_per_m"))
                if max_any is not None and (
                    (prompt_price is not None and prompt_price > max_any)
                    or (completion_price is not None and completion_price > max_any)
                ):
                    fails.append("price_cap")
                if (
                    max_prompt is not None
                    and prompt_price is not None
                    and prompt_price > max_prompt
                ):
                    fails.append("price_cap")
                if (
                    max_completion is not None
                    and completion_price is not None
                    and completion_price > max_completion
                ):
                    fails.append("price_cap")
            min_ctx = _safe_int(class_policy.get("min_context_length"), 0) or 0
            if (_safe_int(gates.get("context_length"), 0) or 0) < min_ctx:
                fails.append("context_below_min")
            if zdr_required and gates.get("zdr") != "supported":
                fails.append("zdr_required")

            if fails:
                excluded[norm_key] = fails
                continue
            rank = _safe_float(cand.get("rank_score"))
            survivors.append(
                (state_rank, -(rank if rank is not None else 0.0), norm_key, model_str)
            )

        survivors.sort()
        max_per_provider = _safe_int(dynamic_cfg.get("max_per_provider"), 0) or 0
        task_cap = (
            _safe_int(remaining_task_cap, 0)
            if remaining_task_cap is not None
            else _safe_int(dynamic_cfg.get("max_total_per_task"), 0)
        ) or 0
        cap = max(0, min(max_per_provider, task_cap))
        tail: List[str] = []
        seen_tail = set(static_norms)
        for _rank, _neg_score, norm_key, model_str in survivors:
            if len(tail) >= cap:
                break
            if norm_key in seen_tail:
                continue
            seen_tail.add(norm_key)
            tail.append(model_str)
        return _result_payload(static_list, tail, mode, "ok", excluded)
    except Exception:
        return _result_payload(static_list, [], mode, "error", {})

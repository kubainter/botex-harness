# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Model Denylist & Candidate Filtering Engine
=================================================
Centralized policy engine providing exact, glob, and regex pattern filtering
across global, provider-specific, and profile-specific scopes.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    from .security import coerce_text, mask_secrets
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets

logger = logging.getLogger(__name__)

# Canonical enum maps for CodeQL CWE-312 compliance
_VALID_SCOPES: Dict[str, str] = {
    "global": "global",
    "provider": "provider",
    "profile": "profile",
}

_VALID_PATTERN_TYPES: Dict[str, str] = {
    "exact": "exact",
    "glob": "glob",
    "regex": "regex",
}

_VALID_DECISION_MAP: Dict[str, str] = {
    "allowed": "ALLOWED",
    "denied_global": "DENIED_BY_GLOBAL_POLICY",
    "denied_provider": "DENIED_BY_PROVIDER_POLICY",
    "denied_profile": "DENIED_BY_PROFILE_POLICY",
    "unsupported_tools": "UNSUPPORTED_NO_TOOLS",
    "zdr_violation": "ZDR_VIOLATION",
    "price_exceeded": "PRICE_EXCEEDED",
    "invalid_model_input": "INVALID_MODEL_INPUT",
}

_MAX_REGEX_LEN: int = 256


@dataclass(frozen=True)
class DenialRecord:
    """Immutable audit record detailing why a model was rejected."""

    model: str
    pattern: str
    scope: str  # "global" | "provider" | "profile"
    pattern_type: str  # "exact" | "glob" | "regex"
    provider: str = ""
    profile: Optional[str] = None
    reason: str = ""
    decision_code: str = ""


@dataclass
class _ScopeRuleSet:
    """Pre-compiled rules for a single scope tier."""

    exact_set: Set[str] = field(default_factory=set)
    glob_list: List[str] = field(default_factory=list)
    regex_list: List[Tuple[str, re.Pattern[str]]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.exact_set and not self.glob_list and not self.regex_list


def matches_pattern(model: str, pattern: str) -> bool:
    """Check if model matches a single pattern (exact, glob, or regex)."""
    if not isinstance(model, str) or not isinstance(pattern, str):
        return False
    model_clean = model.strip()
    pattern_clean = pattern.strip()
    if not model_clean or not pattern_clean:
        return False

    if pattern_clean.startswith(("re:", "regex:")):
        re_body = pattern_clean.split(":", 1)[1]
        if not re_body.strip() or len(re_body) > _MAX_REGEX_LEN:
            return False
        try:
            compiled = re.compile(re_body, re.IGNORECASE)
            return bool(compiled.search(model_clean))
        except re.error:
            return False

    if any(ch in pattern_clean for ch in ("*", "?", "[", "]")):
        return fnmatch.fnmatchcase(model_clean.lower(), pattern_clean.lower())

    return model_clean.lower() == pattern_clean.lower()


def _compile_single_pattern(pattern: str) -> Optional[Tuple[str, Any]]:
    """Classify and compile a raw pattern string.

    Returns (pattern_type, compiled_or_lowered_value) or None if invalid.
    """
    if not isinstance(pattern, str):
        return None
    raw = pattern.strip()
    if not raw:
        return None

    if raw.startswith(("re:", "regex:")):
        re_body = raw.split(":", 1)[1]
        if not re_body.strip():
            logger.warning("Empty regex pattern in denylist: '%s'", coerce_text(mask_secrets(raw)))
            return None
        if len(re_body) > _MAX_REGEX_LEN:
            logger.warning(
                "Regex pattern exceeds max length (%d): %s",
                _MAX_REGEX_LEN,
                coerce_text(mask_secrets(raw[:32])),
            )
            return None
        try:
            compiled = re.compile(re_body, re.IGNORECASE)
            return (_VALID_PATTERN_TYPES["regex"], compiled)
        except re.error as err:
            logger.warning(
                "Invalid regex pattern in denylist '%s': %s",
                coerce_text(mask_secrets(raw)),
                coerce_text(mask_secrets(str(err))),
            )
            return None

    if any(ch in raw for ch in ("*", "?", "[", "]")):
        return (_VALID_PATTERN_TYPES["glob"], raw.lower())

    return (_VALID_PATTERN_TYPES["exact"], raw.lower())


def _build_rule_set(patterns: Sequence[str]) -> _ScopeRuleSet:
    rules = _ScopeRuleSet()
    for pat in patterns:
        compiled = _compile_single_pattern(pat)
        if not compiled:
            continue
        ptype, val = compiled
        if ptype == "exact":
            rules.exact_set.add(val)
        elif ptype == "glob":
            rules.glob_list.append(val)
        elif ptype == "regex":
            rules.regex_list.append((pat, val))
    return rules


def _coerce_pattern_list(val: Any) -> Sequence[str]:
    """Safely coerce any config value into a sequence of pattern strings.

    Scalar non-strings (int, float, bool, None) return an empty list.
    Single strings return a 1-element list.
    Iterables (lists, sets, tuples) return a list of elements.
    Mappings and bytes return an empty list.
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (bytes, Mapping)):
        return []
    if isinstance(val, (list, tuple, set)):
        return list(val)
    if isinstance(val, Iterable):
        try:
            return list(val)
        except Exception:
            return []
    return []


class DenylistFilter:
    """Multi-stage model denylist and candidate filtering engine.

    Supports:
    - Exact match (case-insensitive)
    - Glob wildcard (fnmatch)
    - Regex pattern (re:, regex: prefixes)
    - Three-tier hierarchy: global, provider, profile
    - Tool-capability pruning in candidate chains
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        global_denylist: Optional[Sequence[str]] = None,
        provider_denylists: Optional[Dict[str, Sequence[str]]] = None,
        profile_denylists: Optional[Dict[str, Dict[str, Sequence[str]]]] = None,
    ) -> None:
        self._config: Dict[str, Any] = {}
        self._global_rules = _ScopeRuleSet()
        self._provider_rules: Dict[str, _ScopeRuleSet] = {}
        self._profile_rules: Dict[Tuple[str, str], _ScopeRuleSet] = {}
        self._explicit_global = global_denylist
        self._explicit_providers = provider_denylists
        self._explicit_profiles = profile_denylists

        self.reload(config)

    def reload(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Reload configuration and rebuild all pre-compiled rule indices."""
        if config is not None:
            self._config = config if isinstance(config, Mapping) else {}
        else:
            try:
                from .config import load_config

                cfg = load_config()
                self._config = cfg if isinstance(cfg, Mapping) else {}
            except Exception:
                self._config = {}

        # 1. Global denylist
        if self._explicit_global is not None:
            raw_global = self._explicit_global
        else:
            engine_cfg = self._config.get("engine", {}) if isinstance(self._config, Mapping) else {}
            if isinstance(engine_cfg, Mapping):
                raw_global = engine_cfg.get("denylist")
            else:
                raw_global = None
            if raw_global is None and isinstance(self._config, Mapping):
                raw_global = self._config.get("denylist")
        self._global_rules = _build_rule_set(_coerce_pattern_list(raw_global))

        # 2. Provider denylists
        self._provider_rules.clear()
        providers_cfg = (
            self._config.get("providers", {}) if isinstance(self._config, Mapping) else {}
        )
        if self._explicit_providers is not None and isinstance(self._explicit_providers, Mapping):
            for p_name, p_pats in self._explicit_providers.items():
                if isinstance(p_name, str):
                    pats = _coerce_pattern_list(p_pats)
                    self._provider_rules[p_name.strip().lower()] = _build_rule_set(pats)
        elif isinstance(providers_cfg, Mapping):
            for p_name, p_val in providers_cfg.items():
                if isinstance(p_name, str) and isinstance(p_val, Mapping):
                    pats = _coerce_pattern_list(p_val.get("denylist"))
                    if pats:
                        self._provider_rules[p_name.strip().lower()] = _build_rule_set(pats)

        # 3. Profile denylists
        self._profile_rules.clear()
        if self._explicit_profiles is not None and isinstance(self._explicit_profiles, Mapping):
            for p_name, prof_map in self._explicit_profiles.items():
                if isinstance(p_name, str) and isinstance(prof_map, Mapping):
                    for prof_name, prof_pats in prof_map.items():
                        if isinstance(prof_name, str):
                            pats = _coerce_pattern_list(prof_pats)
                            key = (p_name.strip().lower(), prof_name.strip().lower())
                            self._profile_rules[key] = _build_rule_set(pats)
        elif isinstance(providers_cfg, Mapping):
            for p_name, p_val in providers_cfg.items():
                if isinstance(p_name, str) and isinstance(p_val, Mapping):
                    prof_map = p_val.get("profile_denylists") or p_val.get("profile_denylist")
                    if isinstance(prof_map, Mapping):
                        for prof_name, prof_pats in prof_map.items():
                            if isinstance(prof_name, str):
                                pats = _coerce_pattern_list(prof_pats)
                                if pats:
                                    key = (p_name.strip().lower(), prof_name.strip().lower())
                                    self._profile_rules[key] = _build_rule_set(pats)

    def _resolve_default_provider(self) -> str:
        providers_cfg = self._config.get("providers", {})
        if isinstance(providers_cfg, Mapping):
            for name, cfg in providers_cfg.items():
                if isinstance(cfg, Mapping) and cfg.get("default"):
                    return name
            for name in providers_cfg:
                return name
        return "openrouter"

    def _match_rule_set(
        self,
        model_clean: str,
        rules: _ScopeRuleSet,
        scope: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> Optional[DenialRecord]:
        if rules.is_empty():
            return None

        scope_lit = _VALID_SCOPES.get(scope, "global")
        dec_code = _VALID_DECISION_MAP.get(
            f"denied_{scope_lit}", _VALID_DECISION_MAP["denied_global"]
        )

        # 1. Exact match (O(1))
        if model_clean in rules.exact_set:
            kind_lit = _VALID_PATTERN_TYPES["exact"]
            reason = mask_secrets(
                f"[DENYLIST BLOCK] Model '{model_clean}' matches {scope_lit} "
                f"denylist rule '{model_clean}' ({kind_lit})."
            )
            return DenialRecord(
                model=model_clean,
                pattern=model_clean,
                scope=scope_lit,
                pattern_type=kind_lit,
                provider=provider,
                profile=profile,
                reason=reason,
                decision_code=dec_code,
            )

        # 2. Glob match
        for glob_pat in rules.glob_list:
            if fnmatch.fnmatchcase(model_clean, glob_pat):
                kind_lit = _VALID_PATTERN_TYPES["glob"]
                reason = mask_secrets(
                    f"[DENYLIST BLOCK] Model '{model_clean}' matches {scope_lit} "
                    f"denylist pattern '{glob_pat}' ({kind_lit})."
                )
                return DenialRecord(
                    model=model_clean,
                    pattern=glob_pat,
                    scope=scope_lit,
                    pattern_type=kind_lit,
                    provider=provider,
                    profile=profile,
                    reason=reason,
                    decision_code=dec_code,
                )

        # 3. Regex match
        for raw_pat, comp_re in rules.regex_list:
            if comp_re.search(model_clean):
                kind_lit = _VALID_PATTERN_TYPES["regex"]
                reason = mask_secrets(
                    f"[DENYLIST BLOCK] Model '{model_clean}' matches {scope_lit} "
                    f"denylist pattern '{raw_pat}' ({kind_lit})."
                )
                return DenialRecord(
                    model=model_clean,
                    pattern=raw_pat,
                    scope=scope_lit,
                    pattern_type=kind_lit,
                    provider=provider,
                    profile=profile,
                    reason=reason,
                    decision_code=dec_code,
                )

        return None

    def get_denial_record(
        self,
        model: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> Optional[DenialRecord]:
        """Return full DenialRecord if denied, or None if allowed."""
        if not model or not isinstance(model, str):
            return None
        model_clean = model.strip().lower()
        if not model_clean:
            return None

        # Tier 1: Global
        record = self._match_rule_set(model_clean, self._global_rules, "global", provider, profile)
        if record:
            return record

        eff_provider = provider.strip().lower() if provider else ""
        if not eff_provider and profile:
            eff_provider = self._resolve_default_provider().lower()

        # Tier 2: Provider
        if eff_provider:
            p_rules = self._provider_rules.get(eff_provider)
            if p_rules:
                record = self._match_rule_set(
                    model_clean, p_rules, "provider", eff_provider, profile
                )
                if record:
                    return record

        # Tier 3: Profile
        if eff_provider and profile:
            prof_id = (eff_provider, profile.strip().lower())
            prof_rules = self._profile_rules.get(prof_id)
            if prof_rules:
                record = self._match_rule_set(
                    model_clean, prof_rules, "profile", eff_provider, profile
                )
                if record:
                    return record

        return None

    def is_denied(
        self,
        model: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> bool:
        """Return True if model matches any applicable denylist rule."""
        return self.get_denial_record(model, provider, profile) is not None

    def is_allowed(
        self,
        model: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> bool:
        """Return True if model is non-empty and NOT denied by any rule."""
        if not model or not isinstance(model, str) or not model.strip():
            return False
        return not self.is_denied(model, provider, profile)

    def check(
        self,
        model: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Pre-flight check signature: (is_allowed, reason_or_empty)."""
        if not model or not isinstance(model, str) or not model.strip():
            return (False, "Empty or invalid model identifier")
        record = self.get_denial_record(model, provider, profile)
        if record:
            return (False, record.reason)
        return (True, "")

    def check_model(
        self,
        model: str,
        provider: str = "",
        profile: Optional[str] = None,
    ) -> Tuple[bool, str, str]:
        """Detailed check signature returning static decision code:

        (is_allowed, decision_code, reason)
        """
        if not model or not isinstance(model, str) or not model.strip():
            return (
                False,
                _VALID_DECISION_MAP.get("invalid_model_input", "INVALID_MODEL_INPUT"),
                "Empty or invalid model identifier",
            )
        record = self.get_denial_record(model, provider, profile)
        if record:
            return (False, record.decision_code, record.reason)
        return (True, _VALID_DECISION_MAP["allowed"], "")

    def filter_models(
        self,
        models: Sequence[str],
        provider: str = "",
        profile: Optional[str] = None,
    ) -> List[str]:
        """Filter a list of models, returning only allowed models in original order

        with case-insensitive deduplication preserving first occurrence.
        """
        if not models:
            return []
        seen: Set[str] = set()
        result: List[str] = []
        for m in models:
            if not m or not isinstance(m, str) or not m.strip():
                continue
            norm = m.strip().lower()
            if norm in seen:
                continue
            seen.add(norm)
            if self.is_allowed(m, provider, profile):
                result.append(m)
        return result

    def filter_candidates(
        self,
        candidates: Sequence[str],
        provider: str = "",
        profile: Optional[str] = None,
        require_tools: bool = False,
        catalog: Optional[Any] = None,
    ) -> List[str]:
        """Prune model candidates against denylists and optional tool support."""
        if not candidates:
            return []
        if isinstance(candidates, str):
            candidates = [candidates]

        # Step 1: Deduplicate while preserving original order
        seen: Set[str] = set()
        unique: List[str] = []
        for c in candidates:
            if not c or not isinstance(c, str) or not c.strip():
                continue
            norm = c.strip().lower()
            if norm not in seen:
                seen.add(norm)
                unique.append(c)

        # Step 2: Denylist pass
        allowed: List[str] = []
        for c in unique:
            record = self.get_denial_record(c, provider, profile)
            if record:
                logger.debug(
                    "Pruning candidate '%s': %s",
                    coerce_text(mask_secrets(c)),
                    record.reason,
                )
            else:
                allowed.append(c)

        if not require_tools:
            return allowed

        # Step 3: Tool-calling capability pass
        tool_capable: List[str] = []
        eff_provider = provider or self._resolve_default_provider()
        provider_cfg = (
            self._config.get("providers", {}).get(eff_provider, {})
            if isinstance(self._config.get("providers"), Mapping)
            else {}
        )
        on_unknown = self._config.get("catalog", {}).get("on_unknown") or self._config.get(
            "pricing", {}
        ).get("on_unknown", "allow")

        # Build case-insensitive index for provider capability overrides
        raw_caps = provider_cfg.get("capabilities") if isinstance(provider_cfg, Mapping) else {}
        caps_by_lower: Dict[str, Any] = {}
        if isinstance(raw_caps, Mapping):
            for k, v in raw_caps.items():
                if isinstance(k, str) and isinstance(v, Mapping):
                    caps_by_lower[k.strip().lower()] = v

        # Build case-insensitive index for dictionary catalog if applicable
        catalog_by_lower: Dict[str, Any] = {}
        if isinstance(catalog, Mapping):
            for k, v in catalog.items():
                if isinstance(k, str):
                    catalog_by_lower[k.strip().lower()] = v

        for c in allowed:
            c_norm = c.strip().lower()
            overrides = (
                caps_by_lower.get(c_norm)
                or (raw_caps.get(c) if isinstance(raw_caps, Mapping) else None)
                or {}
            )
            supports: Optional[bool]
            if "supports_tools" in overrides:
                supports = bool(overrides["supports_tools"])
            elif catalog is not None:
                if hasattr(catalog, "check_tool_support"):
                    supports = catalog.check_tool_support(c)
                elif hasattr(catalog, "get_model"):
                    meta = catalog.get_model(c)
                    supports = meta.supports_tools if meta else None
                elif isinstance(catalog, Mapping):
                    entry = catalog.get(c) or catalog_by_lower.get(c_norm)
                    supports = entry.get("tools") if isinstance(entry, Mapping) else None
                elif callable(catalog):
                    supports = catalog(c)
                else:
                    supports = None
            else:
                supports = None

            if supports is False:
                logger.debug(
                    "Pruning candidate '%s': tool support unsupported",
                    coerce_text(mask_secrets(c)),
                )
                continue
            if supports is None and str(on_unknown).strip().lower() == "deny":
                logger.debug(
                    "Pruning candidate '%s': tool support unknown and on_unknown=deny",
                    coerce_text(mask_secrets(c)),
                )
                continue

            tool_capable.append(c)

        return tool_capable


_DEFAULT_FILTER: Optional[DenylistFilter] = None


def get_default_denylist_filter(config: Optional[Dict[str, Any]] = None) -> DenylistFilter:
    """Retrieve or initialize the global singleton DenylistFilter."""
    global _DEFAULT_FILTER
    if _DEFAULT_FILTER is None or config is not None:
        _DEFAULT_FILTER = DenylistFilter(config)
    return _DEFAULT_FILTER


def check_model_denylist(
    model: str,
    provider: str = "",
    profile: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Pre-flight check model against default denylist filter."""
    filt = get_default_denylist_filter(config)
    return filt.check(model, provider, profile)


def filter_model_candidates(
    candidates: Sequence[str],
    provider: str = "",
    profile: Optional[str] = None,
    require_tools: bool = False,
    catalog: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Filter candidates against default denylist filter."""
    filt = get_default_denylist_filter(config)
    return filt.filter_candidates(
        candidates,
        provider=provider,
        profile=profile,
        require_tools=require_tools,
        catalog=catalog,
    )

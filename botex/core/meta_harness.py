# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Meta-Harness (Harness² Runtime Supervisor & Experience Store)
===================================================================
Implements Google Harness² dual-level recursion:
1. Task-Level Recursion: In-Memory Propose-Probe evaluation for syntax validation
   and dry-run patch verification before committing disk changes.
2. Harnessing-Level Recursion: Extraction and persistent injection of architectural
   and model lessons (kind="lesson") across tasks via the BoteX Memory Vault.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from botex.memory.memory import read_memory, save_memory, search_memory
    from botex.tools.patch_engine import apply_fuzzy_patch, validate_syntax
    from botex.tools.security import mask_secrets
except (ImportError, ValueError):
    from memory import read_memory, save_memory, search_memory
    from patch_engine import apply_fuzzy_patch, validate_syntax
    from security import mask_secrets


# Canonical taxonomy mapping for CodeQL CWE-020 compliance
_TAXONOMY_MAP: Dict[str, str] = {
    "bugfix": "bugfix",
    "refactor": "refactor",
    "test": "test",
    "infra": "infra",
    "analysis": "analysis",
    "feature": "feature",
    "general": "general",
}


def classify_task_taxonomy(task: str, files: Optional[List[str]] = None) -> str:
    """Classifies task intent into a canonical taxonomy domain for lesson matching."""
    t_low = (task or "").lower()
    words = set(re.findall(r"\b[a-z_0-9-]+\b", t_low))
    files_str = " ".join(files or []).lower()

    if any(
        k in words for k in ("docker", "systemd", "deploy", "vps", "service", "keepalive")
    ) or any(k in files_str for k in ("docker", "systemd", "deploy", ".service", ".sh")):
        return _TAXONOMY_MAP["infra"]

    if any(
        k in words for k in ("fix", "bug", "error", "fail", "broken", "syntax", "issue", "except")
    ):
        return _TAXONOMY_MAP["bugfix"]

    if any(k in words for k in ("test", "tests", "pytest", "unit", "assert", "coverage")):
        return _TAXONOMY_MAP["test"]

    if any(k in words for k in ("refactor", "cleanup", "restructure", "dedup")):
        return _TAXONOMY_MAP["refactor"]

    if any(k in words for k in ("review", "audit", "analyze", "explain", "inspect", "check")):
        return _TAXONOMY_MAP["analysis"]

    if any(k in words for k in ("add", "create", "implement", "feature", "build")):
        return _TAXONOMY_MAP["feature"]

    return _TAXONOMY_MAP["general"]


class InMemoryProposeProbe:
    """Task-level contrastive Propose-Probe evaluator running in RAM.

    Evaluates proposed file patches against in-memory source copies before
    allowing disk mutations, severing syntax errors at zero disk-rollback cost.
    """

    @staticmethod
    def probe_patch(
        original_content: str,
        search_block: str,
        replace_block: str,
        file_path: str | Path,
    ) -> Tuple[bool, str, Optional[str], int]:
        """Probes a patch entirely in RAM.

        Returns:
            (success: bool, new_content: str, error_message: Optional[str], fuzzy_level: int)
        """
        success, patched_content, level = apply_fuzzy_patch(
            original_content, search_block, replace_block
        )
        if not success:
            err = (
                f"Probe failed: search block could not be located in '{file_path}'. "
                "Ensure search block exactly matches current code."
            )
            return False, original_content, err, 0

        is_valid, syntax_err = validate_syntax(patched_content, Path(file_path))
        if not is_valid:
            err = (
                f"Probe failed: syntax error detected in proposed changes ({syntax_err}). "
                "Changes were rejected in RAM without touching disk."
            )
            return False, original_content, err, level

        return True, patched_content, None, level


# Lesson lifecycle confidences: candidates start low, recall only surfaces
# promoted lessons. PR 2 (update_lesson_confidence) and PR 3 (Curator
# candidate -> active promotion) move lessons across this gap.
_CANDIDATE_INITIAL_CONFIDENCE = 0.4
_RECALL_MIN_CONFIDENCE = 0.6

_RESOLUTION_LINE_RE = re.compile(r"^\s*[-*]\s*\*\*Effective Resolution:?\*\*:?\s*(.+)$")

# Provider-side failure vocabulary: infrastructure noise is owned by
# retry.py/denylist.py and must never become an episodic lesson or feed
# negative confidence adjustments.
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

# Engine-side failure_kind vocabulary (contracts.STATUS_FAILURE_KIND /
# classify_api_error) mapped onto the generic infra classes via substring —
# "provider_rate_limited" hits "rate_limit", "budget_or_price_block" hits
# "budget", etc.
_INFRA_KIND_MARKERS = (
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

# "\b429\b" is word-bounded so plain numbers like "1429"/"4291" embedded in
# payloads never false-positive as HTTP 429 rate-limit noise.
_INFRA_TEXT_RE = re.compile(r"\b429\b|timed out|rate limit|connection error")


def is_infra_failure(failure_kind: Optional[str], error_text: str = "") -> bool:
    """Deterministic infrastructure-noise gate for episodic lesson extraction.

    Rate limits, timeouts, connection/quota/auth/budget/config failures are
    provider-side — never lesson material and never confidence penalties.
    """
    norm = str(failure_kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    if norm:
        if norm in _INFRA_FAILURE_KINDS:
            return True
        if any(marker in norm for marker in _INFRA_KIND_MARKERS):
            return True
    text = str(error_text or "").lower()
    return bool(text) and bool(_INFRA_TEXT_RE.search(text))


def _file_dir_prefixes(files: Optional[List[str]], limit: int = 3) -> List[str]:
    """Normalize file paths into POSIX parent-dir prefixes for lesson binding."""
    prefixes: List[str] = []
    for raw in files or []:
        norm = re.sub(r"/{2,}", "/", str(raw or "").replace("\\", "/")).strip().strip("/")
        if not norm or norm.startswith(".."):
            continue
        parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
        if parent and parent not in prefixes:
            prefixes.append(parent)
        if len(prefixes) >= limit:
            break
    return prefixes


def _memory_matches_path_prefix(memory: Dict[str, Any], dir_prefixes: List[str]) -> bool:
    """True when a lesson's ``path:<dir>`` binding tags cover any given dir."""
    if not isinstance(memory, dict):
        return False
    tags = memory.get("tags") or []
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    bound = [str(t).strip().lower()[5:] for t in tags if str(t).strip().lower().startswith("path:")]
    if not bound:
        return False
    for d in dir_prefixes:
        dl = d.lower()
        for p in bound:
            if p and (dl == p or dl.startswith(p + "/") or p.startswith(dl + "/")):
                return True
    return False


def get_relevant_meta_lessons(
    taxonomy: str,
    workspace_dir: Optional[str | Path] = None,
    max_lessons: int = 3,
    lesson_ids_out: Optional[List[str]] = None,
    files: Optional[List[str]] = None,
) -> List[str]:
    """Retrieves concise, deduplicated architectural lessons matching the task taxonomy.

    When `lesson_ids_out` is a list, the memory IDs of the appended lessons are
    pushed onto it so callers can attribute lessons by exact ID. When `files`
    is given, lessons bound to matching ``path:<dir>`` prefixes rank ahead of
    generic taxonomy matches.
    """
    norm_tax = _TAXONOMY_MAP.get(str(taxonomy or "").strip().lower(), "general")
    ws = str(workspace_dir or ".")

    try:
        results = search_memory(
            workspace_dir=ws,
            query=norm_tax,
            kind="lesson",
            scope="all",
            status="active",
            min_confidence=_RECALL_MIN_CONFIDENCE,
        )
    except Exception:
        return []

    memories = results.get("memories", []) if isinstance(results, dict) else []
    if files:
        dir_prefixes = _file_dir_prefixes(files)
        if dir_prefixes:
            memories = sorted(
                memories,
                key=lambda m: 0 if _memory_matches_path_prefix(m, dir_prefixes) else 1,
            )
    lessons: List[str] = []
    seen: set[str] = set()

    for m in memories:
        if not isinstance(m, dict):
            continue
        mem_id = str(m.get("id") or "").strip()
        if not mem_id or mem_id in seen:
            continue
        seen.add(mem_id)

        try:
            full = read_memory(
                workspace_dir=ws,
                memory_id=mem_id,
                scope="all",
            )
            if full.get("ok"):
                content = str(full.get("content") or "").strip()
                if content:
                    # Prefer the explicit Effective Resolution line — the
                    # Context/Error bullet must never be injected as guidance.
                    substantive = ""
                    for line in content.splitlines():
                        m = _RESOLUTION_LINE_RE.match(line)
                        if m:
                            substantive = m.group(1).strip()
                            break
                    if not substantive:
                        # Legacy entries without an explicit resolution line:
                        # fall back to the first non-header/non-tag bullet.
                        for line in content.splitlines():
                            l = line.strip()
                            if not l or l.startswith("<") or l.startswith("#"):
                                continue
                            cand = l.lstrip("- *").strip()
                            if (
                                cand
                                and not cand.lower().startswith(
                                    ("context/error", "effective resolution", "model telemetry")
                                )
                                and not l.lower().startswith(
                                    ("context/error", "effective resolution", "model telemetry")
                                )
                            ):
                                substantive = cand
                                break
                    if substantive and substantive not in lessons:
                        lessons.append(mask_secrets(substantive))
                        if lesson_ids_out is not None and mem_id:
                            lesson_ids_out.append(mem_id)
                        if len(lessons) >= max_lessons:
                            break
        except Exception:
            continue

    return lessons


def extract_meta_lesson(
    task_id: str,
    taxonomy: str,
    error_summary: str,
    recovery_strategy: str,
    workspace_dir: Optional[str | Path] = None,
    failed_model: str = "",
    successful_model: str = "",
    failure_kind: Optional[str] = None,
    files_touched: Optional[List[str]] = None,
) -> Optional[str]:
    """Generates and persists an architectural lesson (kind='lesson') into Memory Vault.

    Called when a task succeeds after an initial mutation failure, rollback, or syntax rejection.
    """
    if is_infra_failure(failure_kind, str(error_summary or "")):
        # Provider-side noise is not a lesson — retry.py/denylist.py own it.
        return None
    clean_tax = _TAXONOMY_MAP.get(str(taxonomy or "").strip().lower(), "general")
    # Flatten CR/LF so untrusted multi-line errors cannot inject synthetic
    # "- **Effective Resolution:**" lines into the persisted lesson body.
    clean_err = mask_secrets(re.sub(r"[\r\n]+", " ", str(error_summary or ""))[:200].strip())
    clean_strat = mask_secrets(re.sub(r"[\r\n]+", " ", str(recovery_strategy or ""))[:300].strip())

    if not clean_err and not clean_strat:
        return None

    seed_str = f"{clean_tax}:{clean_err}:{clean_strat}"
    lesson_hash = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:10]
    lesson_id = f"lesson_{lesson_hash}"

    title = f"Meta-Lesson [{clean_tax}]: {clean_err[:60] or clean_strat[:60]}"
    # Resolution first: recall-side extraction takes the leading bullet of
    # legacy entries, so the actionable fix must precede the error context.
    body = (
        f"### Lesson Learned ({clean_tax})\n"
        f"- **Effective Resolution:** {clean_strat or 'Strategy adaptation'}\n"
        f"- **Context/Error:** {clean_err or 'None reported'}\n"
    )
    if failed_model or successful_model:
        body += f"- **Model Telemetry:** Failed={mask_secrets(failed_model or 'None')}, Resolved={mask_secrets(successful_model or 'None')}\n"

    if not workspace_dir:
        # Never fall back to the process CWD (".") for lesson persistence.
        return None
    ws = str(workspace_dir)
    tags = ["meta-harness", "lesson", clean_tax]
    # Path-prefix binding (not blob SHAs): later tasks touching the same
    # directories rank this lesson ahead of generic taxonomy matches.
    for prefix in _file_dir_prefixes(files_touched):
        tags.append(f"path:{mask_secrets(prefix)}")
    try:
        res = save_memory(
            workspace_dir=ws,
            title=title,
            content=body,
            kind="lesson",
            tags=tags,
            memory_id=lesson_id,
            scope="local",
            status="candidate",
            confidence=_CANDIDATE_INITIAL_CONFIDENCE,
        )
        if isinstance(res, dict) and res.get("ok"):
            return lesson_id
    except Exception:
        pass
    return None


def _pack_lessons_linear(sections: List[str], budget: int) -> List[str]:
    """Byte-budget prefix pack used only when ContextKnapsack itself fails."""
    packed: List[str] = []
    used = 0
    for s in sections:
        size = len(s.encode("utf-8"))
        overhead = 1 if packed else 0
        if used + size + overhead > budget:
            break
        packed.append(s)
        used += size + overhead
    return packed


def pack_lessons_within_budget(
    lessons: List[str],
    max_bytes: int = 800,
    base_dir: Optional[Any] = None,
) -> List[str]:
    """Return the subset of ``lessons`` that fits ``max_bytes`` under ContextKnapsack.

    Order is preserved and overflow entries are dropped — the knapsack's
    ``[context overflow ...]`` scratchpad pointer is never injected as a fake
    lesson.
    """
    if isinstance(max_bytes, bool):
        return []
    try:
        budget = int(max_bytes)
    except (TypeError, ValueError):
        budget = 800
    if budget <= 0:
        return []
    sections = [str(ln or "").strip() for ln in lessons]
    sections = [s for s in sections if s]
    if not sections:
        return []
    try:
        try:
            from botex.core.context_inclusion import ContextKnapsack
        except (ImportError, ValueError):
            from context_inclusion import ContextKnapsack
        result = ContextKnapsack(budget_bytes=budget).pack(
            sections, scratch_id="meta-lessons", base_dir=base_dir
        )
    except Exception:
        return _pack_lessons_linear(sections, budget)
    overflow = result.get("overflow_sections") or []
    if not overflow:
        return sections
    # Identical strings pack greedily in order: of N copies the first
    # (N - overflowed) survived, so occurrence-index mapping is exact.
    over = Counter(overflow)
    total = Counter(sections)
    seen: Counter = Counter()
    packed_out: List[str] = []
    for s in sections:
        seen[s] += 1
        if seen[s] <= max(0, total[s] - over.get(s, 0)):
            packed_out.append(s)
    return packed_out

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Meta-Harness (Harness² Runtime Supervisor & Experience Store)
===================================================================
Implements Google Harness² harnessing-level recursion: extraction and
persistent injection of architectural and model lessons (kind="lesson")
across tasks via the BoteX Memory Vault. Task-level in-RAM patch probing
(fuzzy apply + syntax validation before disk writes) is owned by
botex.tools.patch_engine, not duplicated here.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from botex.memory.memory import read_memory, save_memory, search_memory
    from botex.tools.security import mask_secrets
except (ImportError, ValueError):
    from memory import read_memory, save_memory, search_memory
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


_SCOPE_TAG_CLEAN_RE = re.compile(r"[^a-z0-9._-]+")


def _clean_scope_component(raw: str) -> str:
    """Normalize an edge/workspace identifier into a tag-safe slug component."""
    cleaned = mask_secrets(str(raw or "")).strip().lower()
    return _SCOPE_TAG_CLEAN_RE.sub("-", cleaned).strip("-.")[:64]


def _memory_scope_tag_set(memory: Dict[str, Any]) -> set[str]:
    """Lowercase tag set of a memory row (tags may be list or delimited str)."""
    if not isinstance(memory, dict):
        return set()
    tags = memory.get("tags") or []
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    return {str(t).strip().lower() for t in tags if str(t).strip()}


def get_relevant_meta_lessons(
    taxonomy: str,
    workspace_dir: Optional[str | Path] = None,
    max_lessons: int = 3,
    lesson_ids_out: Optional[List[str]] = None,
    files: Optional[List[str]] = None,
    edge_sub: str = "",
    workspace_id: str = "",
) -> List[str]:
    """Retrieves concise, deduplicated architectural lessons matching the task taxonomy.

    When `lesson_ids_out` is a list, the memory IDs of the appended lessons are
    pushed onto it so callers can attribute lessons by exact ID. When `files`
    is given, lessons bound to matching ``path:<dir>`` prefixes rank ahead of
    generic taxonomy matches. When `edge_sub`/`workspace_id` are non-empty
    (remote/edge task context), only lessons carrying the matching
    ``edge:<sub>``/``ws:<ws>`` binding tags may rank — lessons extracted on a
    different host or workspace never bleed across the edge boundary.
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
    required_scope_tags: List[str] = []
    if str(edge_sub or "").strip():
        required_scope_tags.append(f"edge:{_clean_scope_component(edge_sub)}")
    if str(workspace_id or "").strip():
        required_scope_tags.append(f"ws:{_clean_scope_component(workspace_id)}")
    if required_scope_tags:
        req_tags = set(required_scope_tags)
        memories = [m for m in memories if req_tags <= _memory_scope_tag_set(m)]
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
    edge_sub: str = "",
    workspace_id: str = "",
    origin: str = "botex",
) -> Optional[str]:
    """Generates and persists an architectural lesson (kind='lesson') into Memory Vault.

    Called when a task succeeds after an initial mutation failure, rollback, or syntax rejection.
    When ``edge_sub``/``workspace_id`` are non-empty (remote/edge task
    context) the lesson persists in ``global`` scope under the workspace
    project slug, bound by ``edge:<sub>``/``ws:<ws>`` tags so recall on a
    different host or workspace can never surface it.
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

    remote_ctx = bool(str(edge_sub or "").strip() or str(workspace_id or "").strip())
    clean_sub = _clean_scope_component(edge_sub)
    clean_ws = _clean_scope_component(workspace_id)

    seed_str = f"{clean_tax}:{clean_err}:{clean_strat}"
    if remote_ctx:
        # Edge/workspace identity joins the deterministic seed: the same
        # recovery on two hosts must never collide on lesson_<hash>.
        seed_str = f"{seed_str}:{clean_sub}:{clean_ws}"
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
    if clean_sub:
        tags.append(f"edge:{clean_sub}")
    if clean_ws:
        tags.append(f"ws:{clean_ws}")
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
            origin=str(origin or "botex"),
            scope="global" if remote_ctx else "local",
            project=(clean_ws or None) if remote_ctx else None,
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

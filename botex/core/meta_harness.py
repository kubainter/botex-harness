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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def get_relevant_meta_lessons(
    taxonomy: str,
    workspace_dir: Optional[str | Path] = None,
    max_lessons: int = 3,
    lesson_ids_out: Optional[List[str]] = None,
) -> List[str]:
    """Retrieves concise, deduplicated architectural lessons matching the task taxonomy.

    When `lesson_ids_out` is a list, the memory IDs of the appended lessons are
    pushed onto it so callers can attribute lessons by exact ID.
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
) -> Optional[str]:
    """Generates and persists an architectural lesson (kind='lesson') into Memory Vault.

    Called when a task succeeds after an initial mutation failure, rollback, or syntax rejection.
    """
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
    try:
        res = save_memory(
            workspace_dir=ws,
            title=title,
            content=body,
            kind="lesson",
            tags=["meta-harness", "lesson", clean_tax],
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

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
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    # envfact lessons rank first (deterministic environment rules), then
    # path-bound matches, then generic taxonomy matches; Timsort keeps the
    # original search order within each tier.
    dir_prefixes = _file_dir_prefixes(files) if files else []
    memories = sorted(
        memories,
        key=lambda m: (
            0 if "envfact" in _memory_scope_tag_set(m) else 1,
            0 if (dir_prefixes and _memory_matches_path_prefix(m, dir_prefixes)) else 1,
        ),
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
    extra_tags: Optional[List[str]] = None,
    initial_confidence: Optional[float] = None,
) -> Optional[str]:
    """Generates and persists an architectural lesson (kind='lesson') into Memory Vault.

    Called when a task succeeds after an initial mutation failure, rollback, or syntax rejection.
    When ``edge_sub``/``workspace_id`` are non-empty (remote/edge task
    context) the lesson persists in ``global`` scope under the workspace
    project slug, bound by ``edge:<sub>``/``ws:<ws>`` tags so recall on a
    different host or workspace can never surface it. ``extra_tags`` adds
    sanitized classification tags (reserved provenance tags are dropped);
    ``initial_confidence`` overrides the 0.4 candidate baseline (clamped to
    [0.0, 1.0]) for deterministic fact lessons such as envfacts.
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
    # Reserved provenance tags (human/curator claims) are never accepted
    # from callers — see the server-side origin hardening in server_cloud.
    reserved_tags = {"human", "human-curated", "frozen", "operator", "curator"}
    for raw_tag in extra_tags or []:
        clean_tag = mask_secrets(str(raw_tag or "").strip())
        if not clean_tag or clean_tag.lower() in reserved_tags or clean_tag in tags:
            continue
        tags.append(clean_tag)
    conf = (
        _CANDIDATE_INITIAL_CONFIDENCE
        if initial_confidence is None
        else max(0.0, min(1.0, float(initial_confidence)))
    )
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
            confidence=conf,
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


# ---------------------------------------------------------------------------
# TokenEconomizer primitives: deterministic, zero-LLM spiral
# detection (SpiralTracker) and environment-fact distillation (envfacts).
# ---------------------------------------------------------------------------

_TEST_RUNNER_TOKENS = (
    "pytest",
    "py.test",
    "unittest",
    "ruff",
    "mypy",
    "npm",
    "node",
    "python",
    "py",
)

_EDIT_PATH_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_EDIT_PATH_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_./-]+")

_FAIL_LINE_RE = re.compile(
    r"Traceback|Error|Exception|FAILED|FAIL|denied|not found|AssertionError", re.IGNORECASE
)
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_LINE_NUM_RE = re.compile(r"line \d+", re.IGNORECASE)
_COL_NUM_RE = re.compile(r":\d+")
_LONG_NUM_RE = re.compile(r"\b\d{3,}\b")


def parse_edge_workspace_key(ws_key: str) -> Tuple[str, str]:
    """Split ``edge://<device_id>/<workspace_id>`` into its two components.

    Returns ``("", "")`` for anything that is not a well-formed edge key —
    local paths and bare device segments never partially match.
    """
    key = str(ws_key or "").strip()
    if not key.startswith("edge://"):
        return ("", "")
    rest = key[len("edge://") :].strip()
    if "/" not in rest:
        return ("", "")
    dev, _, ws = rest.partition("/")
    dev = dev.strip()
    ws = ws.strip()
    if not dev or not ws:
        return ("", "")
    return (dev, ws)


def compute_failure_signature(tool_name: str, error_or_stdout: str) -> str:
    """Stable 12-hex signature of a tool/command failure line.

    Volatile noise (hex addresses, line/column numbers, long digit runs) is
    normalized so the same assertion failing on shifted lines hashes
    identically across turns. Empty input yields ``""`` (never a fake sig).
    """
    text = str(error_or_stdout or "")
    line = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and _FAIL_LINE_RE.search(stripped):
            line = stripped
            break
    if not line:
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped:
                line = stripped
                break
    if not line:
        return ""
    norm = _HEX_ADDR_RE.sub("<HEX>", line)
    norm = _LINE_NUM_RE.sub("line <N>", norm)
    norm = _COL_NUM_RE.sub(":<N>", norm)
    norm = _LONG_NUM_RE.sub("<NUM>", norm)
    seed = f"{str(tool_name or '').strip().lower()}:{norm}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


@dataclass
class SpiralTracker:
    """Detects silent edit+test loops from normalized failure signatures.

    Complements the engine's exact-action ``stagnant_turns`` counter: two
    edits that differ by a byte plus the same failing assertion on a shifted
    line still collide on ``compute_failure_signature`` and escalate
    ``repeated_fail_sigs``. The redirect nudge carries ONLY counters and a
    sanitized busiest-file slug — raw command output never reaches the model
    prompt (prompt-injection barrier).
    """

    edit_counts: Dict[str, int] = field(default_factory=dict)
    test_runs: int = 0
    error_outputs: int = 0
    fail_sigs: set[str] = field(default_factory=set)
    repeated_fail_sigs: int = 0
    nudge_sent: bool = False

    @property
    def edit_churn(self) -> int:
        return max(self.edit_counts.values(), default=0)

    @property
    def distinct_edit_files(self) -> int:
        return len(self.edit_counts)

    def _record_fail_sig(self, sig: str) -> None:
        if not sig:
            return
        if sig in self.fail_sigs:
            self.repeated_fail_sigs += 1
        else:
            self.fail_sigs.add(sig)

    def record_edit(self, path: str) -> None:
        norm = str(path or "").replace("\\", "/")
        norm = _EDIT_PATH_DRIVE_RE.sub("", norm).lstrip("/")
        safe_path = _EDIT_PATH_UNSAFE_RE.sub("", norm)[:80]
        if not safe_path:
            safe_path = "workspace"
        self.edit_counts[safe_path] = self.edit_counts.get(safe_path, 0) + 1

    def record_command(self, command: str, ok: bool, output_or_error: str = "") -> None:
        cmd = str(command or "").lower()
        if any(tok in cmd for tok in _TEST_RUNNER_TOKENS):
            self.test_runs += 1
        if ok:
            return
        self.error_outputs += 1
        self._record_fail_sig(compute_failure_signature("run_command", output_or_error))

    def record_mutation_error(self, tool_name: str, path: str, error_text: str) -> None:
        self.error_outputs += 1
        self._record_fail_sig(compute_failure_signature(tool_name, f"{path}:{error_text}"))

    def is_spiraling(self, stagnant_turns: int = 0, failed_mutations_streak: int = 0) -> bool:
        if stagnant_turns >= 2 or failed_mutations_streak >= 2:
            return True
        if self.repeated_fail_sigs >= 2:
            return True
        return self.edit_churn >= 3 and self.repeated_fail_sigs >= 1

    def should_abort(self, stagnant_turns: int = 0, failed_mutations_streak: int = 0) -> bool:
        if stagnant_turns >= 3 or failed_mutations_streak >= 3:
            return True
        if self.nudge_sent and self.repeated_fail_sigs >= 3:
            return True
        return self.repeated_fail_sigs >= 4

    def format_redirect_nudge(self) -> str:
        if self.nudge_sent:
            return ""
        self.nudge_sent = True
        busiest = (
            max(self.edit_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if self.edit_counts
            else "workspace"
        )
        return (
            "Process check (automated): so far you have made "
            f"{sum(self.edit_counts.values())} edit(s) across "
            f"{self.distinct_edit_files} file(s) (most to `{busiest}`), run "
            f"{self.test_runs} verification/command step(s), and hit "
            f"{self.repeated_fail_sigs} repeated failure signature(s) ({self.error_outputs} total errors). "
            "This matches a repetitive failure loop. Do NOT repeat the last action — step back and "
            "try a MATERIALLY DIFFERENT hypothesis: re-read the target file/test expectation, "
            "consider a different root cause, and verify before patching again."
        )


# ---------------------------------------------------------------------------
# EnvFactsDistiller: deterministic environment-fact lessons from
# the command log — command-form repairs and absent-tool facts.
# ---------------------------------------------------------------------------

MAX_ENVFACTS_PER_TASK = 3

_PYTHON_BINARIES = frozenset({"python", "python3", "py"})
_SUBCOMMAND_BINARIES = frozenset({"ruff", "npm", "pip", "git", "cargo", "go"})
_SAFE_CMD_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_./:-]+$")
_BIN_EXT_RE = re.compile(r"\.(exe|cmd|bat)$", re.IGNORECASE)
_BIN_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

_ENV_ERR_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("python module execution is denied", "is denied by exec policy"), "exec_policy_denied"),
    (("not in the exec allowlist",), "not_in_allowlist"),
    (
        ("not found on path", "command not found", "is not recognized as an internal"),
        "command_not_found",
    ),
    (("no module named",), "module_missing"),
)


def _sanitize_cmd_form(command: str) -> tuple[str, str]:
    """Return ``(binary, canonical_form)`` for a shell command line.

    The form collapses a command onto a safe comparison key (e.g.
    ``python -m pytest tests/x.py`` -> ``python -m pytest``) with every token
    restricted to a static allowlist charset.
    """
    raw = str(command or "").strip()
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()
    tokens = [t for t in tokens if t]
    if not tokens:
        return ("", "")
    head = tokens[0]
    binary = _BIN_EXT_RE.sub("", head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).lower()
    binary = _BIN_UNSAFE_RE.sub("", binary)
    if not binary:
        return ("", "")
    safe_tokens = [t for t in tokens[1:] if _SAFE_CMD_TOKEN_RE.match(t)][:3]
    form = binary
    if len(safe_tokens) >= 2 and binary in _PYTHON_BINARIES and safe_tokens[0] == "-m":
        form = f"{binary} -m {safe_tokens[1]}"
    elif safe_tokens and binary in _SUBCOMMAND_BINARIES and not safe_tokens[0].startswith("-"):
        form = f"{binary} {safe_tokens[0]}"
    return (binary, form)


def _classify_cmd_intent(command: str, form: str) -> str:
    blob = f"{str(command or '').lower()} {form.lower()}"
    if any(tok in blob for tok in ("pytest", "py.test", "unittest", "npm test")):
        return "test"
    if any(tok in blob for tok in ("ruff", "mypy", "flake8")):
        return "lint"
    if any(tok in blob for tok in ("python", "python3", "py")):
        return "python"
    if "git" in blob:
        return "git"
    return "general"


def _classify_env_error(text: str) -> str:
    t = str(text or "").lower()
    if not t:
        return ""
    for markers, code in _ENV_ERR_RULES:
        if any(marker in t for marker in markers):
            return code
    return ""


def distill_envfacts_from_commands(cmd_log: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Derive deterministic environment facts from a task's command log.

    Family 1 (``FormRepair``): a command form failed with an environmental
    reason code and a later same-intent command in a different form
    succeeded. Family 2 (``AbsentTool``): a binary failed as
    not-in-allowlist / not-found and never succeeded anywhere in the log.
    Facts deduplicate by recovery strategy and cap at
    ``MAX_ENVFACTS_PER_TASK``.
    """
    parsed: List[Dict[str, Any]] = []
    for entry in cmd_log or []:
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command") or "")
        binary, form = _sanitize_cmd_form(command)
        if not binary:
            continue
        ok = entry.get("ok") is True
        err_text = str(entry.get("error") or entry.get("stderr") or "")
        parsed.append(
            {
                "binary": binary,
                "form": form,
                "intent": _classify_cmd_intent(command, form),
                "reason_code": "" if ok else _classify_env_error(err_text),
                "ok": ok,
            }
        )

    facts: List[Dict[str, str]] = []
    seen_strategies: set[str] = set()

    def _emit(family: str, error_summary: str, recovery_strategy: str) -> None:
        if len(facts) >= MAX_ENVFACTS_PER_TASK or recovery_strategy in seen_strategies:
            return
        seen_strategies.add(recovery_strategy)
        facts.append(
            {
                "family": family,
                "error_summary": error_summary,
                "recovery_strategy": recovery_strategy,
            }
        )

    repaired_forms: set[str] = set()
    for idx, cur in enumerate(parsed):
        if cur["ok"] or not cur["reason_code"]:
            continue
        # Family 1: later same-intent success under a different form repairs it.
        for later in parsed[idx + 1 :]:
            if later["ok"] and later["intent"] == cur["intent"] and later["form"] != cur["form"]:
                _emit(
                    "FormRepair",
                    f"Command form `{cur['form']}` failed ({cur['reason_code']})",
                    f"In this workspace `{cur['form']}` fails ({cur['reason_code']}); "
                    f"use `{later['form']}` instead.",
                )
                repaired_forms.add(cur["form"])
                break
        # Family 2: the binary is environmentally absent and never succeeded.
        if (
            cur["reason_code"] in ("not_in_allowlist", "command_not_found")
            and cur["form"] not in repaired_forms
            and not any(p["ok"] and p["binary"] == cur["binary"] for p in parsed)
        ):
            _emit(
                "AbsentTool",
                f"Tool `{cur['binary']}` unavailable ({cur['reason_code']})",
                f"Tool `{cur['binary']}` is unavailable in this workspace "
                f"({cur['reason_code']}); do not invoke `{cur['binary']}`.",
            )
    return facts


def persist_envfacts_lessons(
    task_id: str,
    taxonomy: str,
    cmd_log: Sequence[Dict[str, Any]],
    workspace_dir: Optional[str | Path],
    successful_model: str = "",
    files_touched: Optional[List[str]] = None,
    edge_sub: str = "",
    workspace_id: str = "",
    origin: str = "botex",
) -> List[str]:
    """Distill envfacts from ``cmd_log`` and persist them as candidate lessons.

    Envfact lessons start at ``confidence=0.5`` (above the generic 0.4
    candidate baseline — the fact is deterministic, not model behavior) with
    ``envfact``/``envfact:<family>`` tags; the Curator fast-tracks their
    promotion once confirmed. Returns the saved lesson IDs.
    """
    facts = distill_envfacts_from_commands(cmd_log)
    if not facts:
        return []
    saved: List[str] = []
    for fact in facts:
        lesson_id = extract_meta_lesson(
            task_id=task_id,
            taxonomy=taxonomy,
            error_summary=fact["error_summary"],
            recovery_strategy=fact["recovery_strategy"],
            workspace_dir=workspace_dir,
            successful_model=successful_model,
            files_touched=files_touched,
            edge_sub=edge_sub,
            workspace_id=workspace_id,
            origin=origin,
            extra_tags=["envfact", f"envfact:{fact['family'].lower()}"],
            initial_confidence=0.5,
        )
        if lesson_id:
            saved.append(lesson_id)
    return saved

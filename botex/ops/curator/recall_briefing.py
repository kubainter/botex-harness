# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Pre-compiled per-project recall briefings — compile side (plan 055).
====================================================================

Nightly, LLM-compiled project digests served later as zero-LLM first-line
recall answers. Design notes (vetted in plan 055, do not relitigate):

* Metadata lives in a machine-readable HTML comment at the top of the note
  body, not in custom frontmatter — ``save_memory`` rejects unknown kwargs.
* Briefings cover shared-brain projects only: workspace-local notes are
  excluded from selection and sources in v1; notes are saved to the global
  scope so every workspace's recall can find them.
* Freshness is structural + age: an unchanged inputs hash within
  ``BRIEFING_MAX_AGE_H`` skips recompilation; content drift changes the
  hash and forces a nightly recompile.
* Inference output is plain markdown (never JSON) — the lenient-parser
  failure class is avoided entirely.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from botex import memory
from botex.ops.curator.prompts import build_recall_briefing_user_prompt
from botex.skills.skills import has_injection_pattern

BRIEFING_TAG = "recall-brief"
BRIEFING_KIND = "context"
BRIEFING_ORIGIN = "curator"
BRIEFING_MAX_AGE_H = 72.0
BRIEFING_MAX_TOKENS = 1200
BRIEFING_MAX_NOTES_PER_PROJECT = 40
BRIEFING_NOTE_CHAR_CAP = 1000

BRIEFING_HEADER_RE = re.compile(
    r"<!--\s*recall-briefing\s+v(\d+)\s+"
    r"project=([a-zA-Z0-9_\-]+)\s+"
    r"generated=([0-9T:+\-.]+(?:Z|z)?)\s+"
    r"inputs=sha256:([0-9a-f]{64})\s+"
    r"sources=([a-zA-Z0-9_\-,\.]*)\s*-->"
)

_WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_\-\./]+)(?:\|([^\[\]\n\r]+))?\]\]")

# Required briefing sections; English (prompt-mandated) plus tolerated PL forms.
_SECTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "decisions": ("decisions", "decision", "decyzje"),
    "conventions": ("conventions", "konwencje"),
    "gotchas": ("gotchas", "gotcha", "pułapki"),
    "open_threads": ("open threads", "open questions", "otwarte wątki", "otwarte pytania"),
}

InferFn = Callable[[str], Awaitable[Optional[str]]]


def parse_briefing_header(content: str) -> Optional[Dict[str, Any]]:
    """Parse the ``<!-- recall-briefing ... -->`` header from a note body, or None."""
    match = BRIEFING_HEADER_RE.search(str(content or ""))
    if not match:
        return None
    sources = [s.strip() for s in (match.group(5) or "").split(",") if s.strip()]
    return {
        "version": int(match.group(1)),
        "project": match.group(2),
        "generated": match.group(3),
        "inputs": f"sha256:{match.group(4)}",
        "sources": sources,
    }


def compute_inputs_hash(notes: Iterable[Dict[str, Any]]) -> str:
    """Order-independent sha256 over the (id, content) pairs of the source notes."""
    pairs = []
    for note in notes or []:
        nid = str((note or {}).get("id") or "")
        content = str((note or {}).get("content") or "")
        pairs.append(f"{nid}:{hashlib.sha256(content.encode('utf-8')).hexdigest()}")
    joined = "\n".join(sorted(pairs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def select_briefing_projects(
    workspace_dir: str,
    min_notes: int = 5,
    max_projects: int = 8,
) -> List[Tuple[str, int]]:
    """Rank shared-brain projects by active-note count (desc), name as tiebreak."""
    counts: Dict[str, int] = {}
    for entry in _search_active_notes(workspace_dir):
        slug = memory._normalize_project_slug(entry.get("project"))
        if not slug or slug == "_unassigned":
            continue
        counts[slug] = counts.get(slug, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(slug, count) for slug, count in ranked if count >= min_notes][:max_projects]


def validate_briefing_body(body: str, source_ids: Iterable[str]) -> str:
    """Sanitize an LLM-drafted briefing: strip foreign citations, require the
    four sections (raises ``ValueError``), enforce the token cap."""
    text = _strip_outer_code_fence(str(body or "").strip())
    text = _strip_foreign_citations(text, source_ids)
    _require_sections(text)
    capped = _enforce_token_cap(text, BRIEFING_MAX_TOKENS)
    _require_sections(capped)
    return capped


async def compile_project_briefing(
    workspace_dir: str,
    project: str,
    notes: List[Dict[str, Any]],
    infer_fn: InferFn,
) -> Optional[str]:
    """Compile one project briefing via ``infer_fn``; None on any failure."""
    prompt_notes = []
    for note in notes or []:
        copy = dict(note)
        copy["content"] = str(note.get("content") or "")[:BRIEFING_NOTE_CHAR_CAP]
        prompt_notes.append(copy)
    user_prompt = build_recall_briefing_user_prompt(project, prompt_notes)
    raw = await infer_fn(user_prompt)
    if not raw or not str(raw).strip():
        return None
    body = str(raw).strip()
    if has_injection_pattern(body):
        return None
    source_ids = {str(note.get("id") or "") for note in notes or []}
    try:
        return validate_briefing_body(body, source_ids)
    except ValueError:
        return None


async def run_briefing_compilation(
    workspace_dir: str,
    infer_fn: InferFn,
    deadline: Optional[float] = None,
    min_notes: int = 5,
    max_projects: int = 8,
) -> Dict[str, Any]:
    """Compile briefings for the top projects; idempotent on unchanged inputs."""
    results: List[Dict[str, Any]] = []
    compiled = 0
    for slug, _note_count in select_briefing_projects(
        workspace_dir, min_notes=min_notes, max_projects=max_projects
    ):
        if deadline is not None and time.time() >= deadline:
            results.append({"project": slug, "action": "deadline"})
            continue
        notes = _gather_project_notes(workspace_dir, slug)
        if not notes:
            results.append({"project": slug, "action": "no_sources"})
            continue
        inputs_hash = compute_inputs_hash(notes)
        existing = _find_existing_briefing(workspace_dir, slug)
        existing_header = (
            parse_briefing_header(str(existing.get("content") or "")) if existing else None
        )
        if (
            existing_header is not None
            and existing_header.get("inputs") == f"sha256:{inputs_hash}"
            and _age_hours(str(existing_header.get("generated") or "")) <= BRIEFING_MAX_AGE_H
        ):
            results.append(
                {"project": slug, "action": "skipped_unchanged", "briefing_id": existing.get("id")}
            )
            continue
        try:
            body = await compile_project_briefing(workspace_dir, slug, notes, infer_fn)
        except Exception as exc:
            results.append({"project": slug, "action": "inference_error", "error": str(exc)[:200]})
            continue
        if body is None:
            results.append({"project": slug, "action": "inference_failed_or_invalid"})
            continue
        save_res = memory.save_memory(
            workspace_dir=workspace_dir,
            memory_id=str(existing.get("id")) if existing and existing.get("id") else None,
            title=f"Recall Briefing: {slug}",
            content=_render_briefing_content(slug, inputs_hash, notes, body),
            kind=BRIEFING_KIND,
            tags=[BRIEFING_TAG, f"project:{slug}"],
            origin=BRIEFING_ORIGIN,
            scope="global",
            project=slug,
            status="active",
        )
        if not save_res.get("ok"):
            results.append({"project": slug, "action": "save_failed"})
            continue
        compiled += 1
        results.append(
            {
                "project": slug,
                "action": "compiled",
                "briefing_id": save_res.get("id"),
                "sources": len(notes),
                "inputs": f"sha256:{inputs_hash}",
            }
        )
    return {"ok": True, "compiled": compiled, "results": results}


def _scope_probe_dirs(workspace_dir: str) -> Dict[str, Optional[Path]]:
    """Physical dir per scope label, resolved once per batch — never per entry."""
    local_dir = Path(workspace_dir).resolve() / ".botex" / "memory"
    try:
        raw_g = memory.get_global_memory_dir()
        global_dir: Optional[Path] = Path(raw_g) if raw_g else None
    except Exception:
        global_dir = None
    try:
        raw_s = memory.get_swarm_memory_dir()
        swarm_dir: Optional[Path] = Path(raw_s) if raw_s else None
    except Exception:
        swarm_dir = None
    return {"local": local_dir, "global": global_dir, "swarm": swarm_dir}


def _resolve_note_file(entry: Dict[str, Any], dirs: Dict[str, Optional[Path]]) -> Optional[Path]:
    """Locate the vault file behind a search entry in its label's own dir.

    ``entry["path"]`` is a display label (``~/.botex/...``, ``swarm memory/...``),
    not a filesystem path, so resolution probes the scan's scope dir by note id.
    Only the label's own dir is probed: same-id twins exist by design (curator
    ``promote_to_global`` copies a lesson with its id intact), and borrowing a
    twin's file would read the wrong frontmatter scope. A miss degrades to the
    dir label downstream — the fallback value the scan already assigned."""
    nid = str(entry.get("id") or "").strip()
    if not nid:
        return None
    try:
        nid = memory._sanitize_id(nid)
    except Exception:
        return None
    cand = dirs.get(str(entry.get("scope") or "local"))
    if cand is None:
        return None
    probe = cand / f"{nid}.md"
    try:
        return probe if probe.is_file() else None
    except OSError:
        return None


def _effective_scope(entry: Dict[str, Any], dirs: Dict[str, Optional[Path]]) -> str:
    """The note's recorded intent: frontmatter ``scope:`` when parseable, else
    the scan's directory label.

    On collapsed-scope deployments (service home == repo dir) every scope
    directory resolves to the same path and dedup labels all entries ``local``
    regardless of writer intent — the frontmatter is the only surviving record.
    The full file is parsed (the indexer parses the same input): truncating the
    frontmatter would silently drop the closing marker on link-heavy notes and
    fall back to the very label this logic exists to correct. Only canonical
    scope values count — a malformed value degrades to the dir label, so a
    ``scope: local`` note can never slip in through a parse edge."""
    label = str(entry.get("scope") or "local")
    note_file = _resolve_note_file(entry, dirs)
    if note_file is not None:
        try:
            raw = note_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        if raw:
            try:
                meta, _body = memory._parse_frontmatter(raw)
            except Exception:
                meta = {}
            fm_scope = memory._VALID_SCOPES_MAP.get(
                unicodedata.normalize("NFKC", str(meta.get("scope") or "")).strip().lower()
            )
            if fm_scope in ("local", "global", "swarm"):
                return fm_scope
    return label


def _search_active_notes(workspace_dir: str) -> List[Dict[str, Any]]:
    """Active global/swarm-scope notes usable as briefing inputs.

    Local-scope notes are excluded in v1; briefing notes and ledger nodes must
    never feed their own (re)compilation."""
    res = memory.search_memory(workspace_dir=workspace_dir, query="", scope="all", status="active")
    if not res.get("ok"):
        return []
    dirs = _scope_probe_dirs(workspace_dir)
    entries: List[Dict[str, Any]] = []
    for entry in res.get("memories") or []:
        tags = {str(t).strip().lower() for t in (entry.get("tags") or [])}
        if BRIEFING_TAG in tags:
            continue
        if memory._is_ledger_node(entry):
            continue
        eff = _effective_scope(entry, dirs)
        if eff == "local":
            continue
        # Propagate the resolved scope: on collapsed deployments the scan label
        # says "local" for every note, and downstream readers of entry["scope"]
        # must see the canonical frontmatter-derived value instead.
        entries.append({**entry, "scope": eff})
    return entries


def _gather_project_notes(
    workspace_dir: str,
    project: str,
    limit: int = BRIEFING_MAX_NOTES_PER_PROJECT,
) -> List[Dict[str, Any]]:
    """Project notes ranked by graph degree first, recency as tiebreak."""
    slug = memory._normalize_project_slug(project)
    pool = [
        entry
        for entry in _search_active_notes(workspace_dir)
        if memory._normalize_project_slug(entry.get("project")) == slug
    ]
    if not pool:
        return []
    degree: Dict[str, int] = {str(e.get("id") or ""): 0 for e in pool}
    for entry in pool:
        nid = str(entry.get("id") or "")
        for link in entry.get("links") or []:
            target = str(link).strip()
            if target and target != nid and target in degree:
                degree[target] += 1
                degree[nid] += 1
    pool.sort(
        key=lambda e: (degree.get(str(e.get("id") or ""), 0), str(e.get("timestamp") or "")),
        reverse=True,
    )
    return pool[:limit]


def _briefing_generated_sort_key(entry: Dict[str, Any], header: Dict[str, Any]) -> tuple:
    try:
        dt = datetime.fromisoformat(str(header.get("generated") or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        generated_ts = dt.timestamp()
    except ValueError:
        generated_ts = float("-inf")
    return (generated_ts, str(entry.get("id") or ""))


def _deprecate_briefing_duplicate(
    workspace_dir: str, entry: Dict[str, Any], winner_id: str
) -> None:
    if not memory.set_memory_status(
        workspace_dir,
        str(entry.get("id") or ""),
        "deprecated",
        superseded_by=winner_id,
        reason="duplicate recall briefing reconciliation",
        scope="global",
    ):
        raise RuntimeError("failed to deprecate duplicate recall briefing")


def _find_existing_briefing(workspace_dir: str, project: str) -> Optional[Dict[str, Any]]:
    """Find the newest parseable global briefing, deprecating older duplicates."""
    slug = memory._normalize_project_slug(project)
    if not slug:
        return None
    res = memory.search_memory(
        workspace_dir=workspace_dir,
        query="",
        tag=BRIEFING_TAG,
        project=slug,
        scope="global",
        status="active",
    )
    parseable = []
    for entry in res.get("memories") or []:
        header = parse_briefing_header(str(entry.get("content") or ""))
        if header and memory._normalize_project_slug(str(header.get("project") or "")) == slug:
            parseable.append((entry, header))
    if not parseable:
        return None
    winner, _winner_header = max(parseable, key=lambda pair: _briefing_generated_sort_key(*pair))
    for duplicate, _header in parseable:
        if str(duplicate.get("id") or "") != str(winner.get("id") or ""):
            _deprecate_briefing_duplicate(workspace_dir, duplicate, str(winner.get("id") or ""))
    return winner


def _render_briefing_content(
    project: str,
    inputs_hash: str,
    notes: List[Dict[str, Any]],
    body: str,
) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    source_ids = ",".join(str(n.get("id") or "") for n in notes if n.get("id"))
    header = (
        f"<!-- recall-briefing v1 project={project} generated={generated} "
        f"inputs=sha256:{inputs_hash} sources={source_ids} -->"
    )
    return f"{header}\n\n{body.strip()}"


def _strip_outer_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).rstrip()
    return text


def _strip_foreign_citations(text: str, source_ids: Iterable[str]) -> str:
    allowed = {str(s) for s in (source_ids or set())}

    def _replace(match: re.Match) -> str:
        target = match.group(1).strip()
        if target in allowed:
            return match.group(0)
        alias = (match.group(2) or "").strip() if match.group(2) else ""
        return alias or target

    return _WIKILINK_RE.sub(_replace, text)


def _require_sections(text: str) -> None:
    headings = set()
    for match in re.finditer(r"^#{1,6}\s*(.+?)\s*:?\s*$", text, re.MULTILINE):
        headings.add(match.group(1).strip().lower())
    missing = [
        name
        for name, aliases in _SECTION_ALIASES.items()
        if not any(alias in headings for alias in aliases)
    ]
    if missing:
        raise ValueError(f"briefing missing required sections: {', '.join(missing)}")


def _enforce_token_cap(text: str, cap: int) -> str:
    """Trim trailing lines inside sections without deleting section headings.

    Policy: trim from the end of the last section first, then walk backwards;
    every markdown heading line is structural and is never removed. If the
    heading-only skeleton still exceeds ``cap``, keep the complete skeleton and
    return it over cap rather than corrupting the required structure.
    """
    if memory._estimate_tokens(text) <= cap:
        return text
    segments: List[List[str]] = []
    current: List[str] = []
    for line in str(text).splitlines():
        if re.match(r"^#{1,6}\s", line) and current:
            segments.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        segments.append(current)

    def _render() -> str:
        return "\n\n".join("\n".join(segment).strip() for segment in segments if segment).strip()

    trimmed = _render()
    while memory._estimate_tokens(trimmed) > cap:
        changed = False
        for segment in reversed(segments):
            if len(segment) > 1:
                segment.pop()
                changed = True
                break
        if not changed:
            break
        trimmed = _render()
    return trimmed


def _age_hours(generated: str) -> float:
    try:
        dt = datetime.fromisoformat(str(generated or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 0:
            return float("inf")
        return delta / 3600.0
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------------------
# Query-time serving side (plan 055 W2): coverage lookup + overlap gate.
# ---------------------------------------------------------------------------


def strip_briefing_header(content: str) -> str:
    """Briefing body without the machine-readable HTML comment header."""
    return BRIEFING_HEADER_RE.sub("", str(content or ""), count=1).strip()


def briefing_overlap_score(query: str, body: str) -> int:
    """Distinct non-stop query tokens (len >= 3) present in the briefing body.

    Mirrors the recall admission-gate vocabulary: stop tokens never count
    towards coverage, and the caller requires score >= _RECALL_MIN_COVERAGE.
    """
    tokens = {
        t
        for t in re.findall(r"[\w\-]{3,}", str(query or "").lower())
        if t not in memory._RECALL_STOP_TOKENS
    }
    if not tokens:
        return 0
    text = str(body or "").lower()
    return sum(1 for token in tokens if token in text)


def list_fresh_briefings(
    workspace_dir: str,
    max_age_h: float = BRIEFING_MAX_AGE_H,
) -> List[Dict[str, Any]]:
    """Active recall briefings inside the age window with structurally valid sources.

    Deterministic tag metadata lookup (never FTS): freshness means generated
    within ``max_age_h`` AND no source note missing/superseded/deprecated.
    Content drift is absorbed by the nightly idempotent recompile instead.
    """
    res = memory.search_memory(
        workspace_dir=workspace_dir,
        query="",
        tag=BRIEFING_TAG,
        scope="global",
        status="active",
    )
    if not res.get("ok"):
        return []
    out: List[Dict[str, Any]] = []
    for entry in res.get("memories") or []:
        content = str(entry.get("content") or "")
        header = parse_briefing_header(content)
        if header is None:
            continue
        entry_project = memory._normalize_project_slug(str(entry.get("project") or ""))
        header_project = memory._normalize_project_slug(str(header.get("project") or ""))
        if not entry_project or entry_project != header_project:
            continue
        if _age_hours(str(header.get("generated") or "")) > float(max_age_h):
            continue
        if not _sources_still_active(workspace_dir, header.get("sources") or []):
            continue
        out.append(
            {
                "id": str(entry.get("id") or ""),
                "project": str(header.get("project") or ""),
                "generated": str(header.get("generated") or ""),
                "sources": list(header.get("sources") or []),
                "content": content,
            }
        )
    return out


def _sources_still_active(workspace_dir: str, source_ids: Iterable[str]) -> bool:
    for sid in source_ids or []:
        try:
            res = memory.read_memory(workspace_dir=workspace_dir, memory_id=str(sid), scope="all")
        except Exception:
            return False
        if not res.get("ok"):
            return False
        meta = res.get("metadata") or {}
        if str(meta.get("status") or "active").strip().lower() != "active":
            return False
    return True


def parse_max_age_h(value: Any, default: float = BRIEFING_MAX_AGE_H) -> float:
    """Defensive numeric parse for operator-configured briefing freshness."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def build_briefing_answer(
    workspace_dir: str,
    query: str,
    nodes: List[Dict[str, Any]],
    project: Optional[str] = None,
    max_age_h: float = BRIEFING_MAX_AGE_H,
) -> Optional[Dict[str, Any]]:
    """Zero-LLM first-line recall answer from a fresh briefing, or None.

    Coverage = deterministic tag lookup + token-overlap admission gate; weak
    overlap returns None so the caller falls through to the LLM cascade.
    Shared by the MCP recall path and the worker bridge.
    """
    norm_project = memory._normalize_project_slug(str(project or ""))
    if not norm_project:
        return None
    try:
        briefings = list_fresh_briefings(workspace_dir, max_age_h=max_age_h)
    except Exception:
        return None
    best: Optional[tuple] = None
    for briefing in briefings:
        if memory._normalize_project_slug(str(briefing.get("project") or "")) != norm_project:
            continue
        body = strip_briefing_header(str(briefing.get("content") or ""))
        if not body:
            continue
        score = briefing_overlap_score(query, body)
        if score < memory._RECALL_MIN_COVERAGE:
            continue
        if best is None or score > best[0]:
            best = (score, briefing, body)
    if best is None:
        return None
    overlap, briefing, body = best
    digest_nodes = [
        n
        for n in nodes or []
        if BRIEFING_TAG not in {str(t).strip().lower() for t in (n.get("tags") or [])}
    ][:3]
    answer = body
    sources = [str(briefing.get("id") or "")]
    if digest_nodes:
        digest = memory.deterministic_recall(query, digest_nodes)
        digest_answer = str(digest.get("answer") or "").strip()
        if digest_answer:
            answer = body + "\n\n---\n\nPowiązane notatki:\n" + digest_answer
        sources.extend(str(n.get("id")) for n in digest_nodes if n.get("id"))
    return {
        "answer": answer,
        "sources": sources,
        "confidence": "HIGH" if overlap >= 4 else "MEDIUM",
        "tier_used": "briefing",
        "briefing_id": str(briefing.get("id") or ""),
        "briefing_generated": str(briefing.get("generated") or ""),
    }

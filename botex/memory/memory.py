# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Workspace Memory Vault
============================
Workspace-scoped, persistent, and portable memory storage for MCP clients.
Supports context notes, architectural decisions, task handoffs, and lessons learned.
Format: Markdown with YAML frontmatter (compatible with ecc.memory.v2 and ecc.memory.v1).
Location: <workspace_dir>/.botex/memory/
Fast Hybrid Search: SQLite WAL + FTS5 Full-Text Index with RRF fusion.
"""

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import uuid
import warnings
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from botex.tools.security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        sanitize_for_storage,
        sanitize_identifier,
        wrap_untrusted_content,
    )
except (ImportError, ValueError):
    from security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        sanitize_for_storage,
        sanitize_identifier,
        wrap_untrusted_content,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "extract_implicit_links",
    "save_memory",
    "search_memory",
    "read_memory",
    "delete_memory",
    "list_memories",
    "purge_ephemeral_memories",
    "get_global_memory_dir",
    "extract_wikilinks",
    "reciprocal_rank_fusion",
    "reflect_memory",
    "gather_recall_context",
    "deterministic_recall",
    "sync_memory_index",
    "close_index_dbs",
    "reset_memory_index_cache",
    "VALID_KINDS",
    "VALID_SCOPES",
    "VALID_STATUSES",
    "set_memory_status",
    "MAX_MEMORY_BYTES",
    "SCHEMA_VERSION",
]

_VALID_KINDS_MAP: dict[str, str] = {
    "context": "context",
    "decision": "decision",
    "handoff": "handoff",
    "lesson": "lesson",
    "belief": "belief",
    "heuristic": "heuristic",
}
VALID_KINDS = frozenset(_VALID_KINDS_MAP.keys())

_VALID_SCOPES_MAP: dict[str, str] = {
    "local": "local",
    "global": "global",
    "_global": "global",
    "swarm": "swarm",
    "all": "all",
    "both": "all",
}
VALID_SCOPES = frozenset(["local", "global", "swarm", "all"])

VALID_STATUSES = frozenset(["active", "superseded", "deprecated"])

# Recall admission gate (arena-recall S.1): OR-matching admits single-token
# noise, so admission needs >=2 distinct matched tokens — a lone match only
# passes when the token is not a common stop-word.
_RECALL_MIN_COVERAGE = 2
_RECALL_STOP_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "how",
        "what",
        "jest",
        "sie",
        "nie",
        "oraz",
        "dla",
        "przy",
        "jaki",
        "jaka",
    }
)


def get_swarm_memory_dir() -> Optional[Path]:
    """Third READ-ONLY search root (arena-recall S.3): paths.swarm_memory_dir.

    Returns None unless configured AND the directory exists — no implicit
    cross-workspace reads. Read-only tools only; write paths never inherit."""
    raw = ""
    try:
        from botex.server.config import swarm_memory_dir as _configured

        raw = (_configured() or "").strip()
    except Exception:
        raw = (os.environ.get("BOTEX_SWARM_MEMORY_DIR") or "").strip()
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_dir():
            return None
        # Guard against misconfiguration pointing at a repo root: indexing
        # AGENTS.md/pyproject.toml neighbors as "notes" poisons the graph
        # (arena latent bug: swarm_memory_dir == workspace root).
        if any(
            (resolved / marker).exists()
            for marker in ("pyproject.toml", "package.json", "AGENTS.md")
        ):
            return None
        return resolved
    except Exception:
        return None


def get_recall_config():
    """Re-export bridge (Agy review blocker #1): Worker B's recall_curator
    resolves recall config via ``botex.memory`` — keep the Worker A owner
    (botex.server.config) reachable through both import paths."""
    try:
        from botex.server.config import get_recall_config as _configured

        return _configured()
    except Exception:
        return None


MEMORY_DIR_NAME = ".botex/memory"
SCHEMA_VERSION = "ecc.memory.v2"
MAX_MEMORY_BYTES = 1_048_576  # 1 MB cap
# Cap for 1-hop spreading-activation neighbours surfaced alongside FTS5 hits.
MAX_SPREAD_ACTIVATION = 25


def _coerce_confidence(value: Any, default: float = 1.0) -> float:
    """Normalize a confidence score to a finite float in [0.0, 1.0].

    Non-numeric, boolean, NaN, or infinite inputs fall back to ``default``;
    finite numbers are clamped into the valid range.
    """
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool) or not math.isfinite(conf):
        return default
    return min(max(conf, 0.0), 1.0)


def _parse_temporal_bound(raw: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    """Parse an ISO-8601 date/datetime bound into an aware UTC ``datetime``.

    Accepts ``YYYY-MM-DD`` (expanded to the start or end of that day),
    naive datetimes (assumed UTC), and offset-aware datetimes (normalized
    to UTC). Returns ``None`` for empty input; raises ``ValueError`` on
    unparseable values.
    """
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s = f"{s}T23:59:59.999999" if end_of_day else f"{s}T00:00:00"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_timestamp_safe(raw: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 timestamp parse returning aware UTC datetime or None."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Regex for extracting Obsidian-style wikilinks [[target]] or [[target|display]]
_WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_\-\./]+)(?:\|([^\[\]\n\r]+))?\]\]")
# Regex for discovering implicit references to canonical memory IDs (e.g. mem_a1b2c3d4, lesson_xyz, handoff_xyz)
_IMPLICIT_REF_RE = re.compile(r"\b((?:mem|lesson|skill|handoff|cr|research)_[a-zA-Z0-9_\-]+)\b")


def _strip_code_spans(text: str) -> str:
    """Linear O(n) stripper for fenced code blocks and inline code spans (zero ReDoS)."""
    if not text:
        return ""
    lines_out: List[str] = []
    in_fence = False
    for line in text.splitlines():
        lstripped = line.lstrip()
        if lstripped.startswith("```") and len(lstripped) < 32 and "`" not in lstripped.lstrip("`"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Strip inline single/multi-backtick spans in a single pass
        out_chars: List[str] = []
        i = 0
        n = len(line)
        while i < n:
            if line[i] == "`":
                j = i
                while j < n and line[j] == "`":
                    j += 1
                delim = line[i:j]
                end_pos = line.find(delim, j)
                if end_pos != -1:
                    i = end_pos + len(delim)
                    continue
                else:
                    out_chars.append(delim)
                    i = j
            else:
                out_chars.append(line[i])
                i += 1
        lines_out.append("".join(out_chars))
    return "\n".join(lines_out)


def extract_wikilinks(text: str) -> List[str]:
    """Extract unique wikilink target IDs from markdown body (ignoring fenced/inline code blocks)."""
    if not text:
        return []
    cleaned = _strip_code_spans(text)
    matches = _WIKILINK_RE.findall(cleaned)
    targets: List[str] = []
    for m in matches:
        tgt = m[0].strip()
        if tgt.lower().endswith(".md"):
            tgt = tgt[:-3].strip()
        if tgt and tgt not in targets:
            targets.append(tgt)
    return targets


def extract_implicit_links(
    text: str,
    self_id: str,
    known_ids: set[str],
    existing_links: Optional[List[str]] = None,
) -> List[str]:
    """Discover bare or inline-code references to existing memory IDs in text."""
    if not text or not known_ids:
        return []
    seen = set(existing_links or [])
    seen.add(self_id)
    discovered: List[str] = []
    for match in _IMPLICIT_REF_RE.findall(text):
        cand = match.strip()
        if cand in known_ids and cand not in seen:
            seen.add(cand)
            discovered.append(cand)
    return discovered


def reciprocal_rank_fusion(
    fts_ranks: Dict[str, int],
    vec_ranks: Dict[str, int],
    k: int = 60,
) -> List[Tuple[str, float]]:
    """Merge ranked lists using Reciprocal Rank Fusion (RRF).

    Score = sum(1.0 / (k + rank)) for each rank list.
    Returns list of (id, score) sorted by score descending.
    """
    scores: Dict[str, float] = {}
    all_keys = set(fts_ranks.keys()) | set(vec_ranks.keys())
    for key in all_keys:
        score = 0.0
        if key in fts_ranks:
            score += 1.0 / (k + fts_ranks[key])
        if key in vec_ranks:
            score += 1.0 / (k + vec_ranks[key])
        scores[key] = score
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def get_global_memory_dir() -> Path:
    """Resolve global memory directory with fallback logic."""
    try:
        from botex.server.config import load_config

        cfg = load_config()
        configured = cfg.get("paths", {}).get("global_memory_dir")
        if configured:
            return Path(os.path.expanduser(str(configured))).resolve()
    except Exception:
        pass

    home = Path.home().resolve()
    primary = home / ".botex" / "memory"
    fallback = home / ".botex" / "global_memory"
    if fallback.exists() and not primary.exists():
        return fallback
    return primary


def _sanitize_id(memory_id: str) -> str:
    """Validate memory_id against path traversal attacks and sever static analysis taint."""
    clean = str(memory_id).strip()
    if clean.endswith(".md"):
        clean = clean[:-3]
    safe = sanitize_identifier(clean)
    if not safe:
        raise SecurityError(
            f"Invalid memory_id '{memory_id}'. Only alphanumeric, '_' and '-' allowed."
        )
    return safe


_PROJECT_ALIASES: Dict[str, str] = {
    "the-witcher-3-wild-hunt-goty": "witcher3",
    "witcher-3": "witcher3",
}


def _get_project_aliases() -> Dict[str, str]:
    """Return canonical project alias map, merging optional ``memory.project_aliases`` config."""
    aliases = dict(_PROJECT_ALIASES)
    try:
        from botex.config import load_config

        cfg = load_config()
        mem_cfg = cfg.get("memory") if isinstance(cfg, dict) else None
        extra = mem_cfg.get("project_aliases") if isinstance(mem_cfg, dict) else None
        if isinstance(extra, dict):
            for k, v in extra.items():
                rk = _raw_project_slug(str(k))
                rv = _raw_project_slug(str(v))
                if rk and rv and rk != "_unassigned" and rv != "_unassigned":
                    aliases[rk] = rv
    except Exception:
        pass
    return aliases


def _raw_project_slug(project: Optional[str]) -> str:
    """Normalize raw project text to a slug without applying alias unification."""
    if project is None:
        return ""
    raw = str(project).strip().lower()
    if not raw:
        return ""
    if raw == "_unassigned":
        return "_unassigned"
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    return sanitize_for_storage(slug[:64])


def _normalize_project_slug(project: Optional[str]) -> str:
    """Normalize a project identifier into a canonical lowercase slug ([a-z0-9_-], max 64 chars)."""
    slug = _raw_project_slug(project)
    if not slug or slug == "_unassigned":
        return slug
    return _get_project_aliases().get(slug, slug)


def _find_existing_memory_file(mem_dir: Path, mid: str) -> Optional[Path]:
    """Resolve an existing memory Markdown file via directory enumeration to decouple taint."""
    if not mem_dir.is_dir():
        return None
    target_name = f"{mid}.md"
    try:
        for entry in mem_dir.iterdir():
            if entry.is_file() and entry.name == target_name:
                return entry
    except Exception:
        pass
    return None


def _serialize_val(val: Any) -> str:
    """Serialize scalar value safely for YAML frontmatter."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).replace("\r", "").replace("\n", " ")
    if any(c in s for c in (":", "#", "[", "]", "{", "}", "'", '"', "\\")) or s.lower() in (
        "true",
        "false",
        "null",
        "none",
    ):
        return json.dumps(s, ensure_ascii=False)
    return s


def _serialize_frontmatter(metadata: Dict[str, Any], content: str) -> str:
    """Format metadata and content as Markdown with YAML frontmatter."""
    lines = ["---"]
    for key, val in metadata.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {_serialize_val(item)}")
        else:
            lines.append(f"{key}: {_serialize_val(val)}")
    lines.append("---")
    lines.append("")
    lines.append(content.rstrip())
    lines.append("")
    return "\n".join(lines)


def _parse_val(raw: str) -> Any:
    """Parse a scalar or inline-list value from frontmatter line."""
    val = raw.strip()
    if not val:
        return ""
    if val.startswith("[") and val.endswith("]"):
        try:
            return json.loads(val)
        except Exception:
            items = [x.strip() for x in val[1:-1].split(",") if x.strip()]
            return [_parse_val(x) for x in items]
    if val.startswith('"') and val.endswith('"'):
        try:
            return json.loads(val)
        except Exception:
            return val[1:-1]
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        if re.match(r"^[-+]?(?:\d+\.\d*|\.\d+|\d+[eE][-+]?\d+)$", val):
            return float(val)
        if re.match(r"^[-+]?\d+$", val):
            return int(val)
    except (ValueError, TypeError):
        pass
    return val


def _parse_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter and body from Markdown text."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    yaml_block = parts[1]
    body = parts[2].lstrip("\r\n")

    metadata: Dict[str, Any] = {}
    current_list_key: Optional[str] = None

    for line in yaml_block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if line.startswith("  - ") or line.startswith("    - ") or stripped.startswith("- "):
            item_raw = stripped[1:].strip()
            item = _parse_val(item_raw)
            if current_list_key and isinstance(metadata.get(current_list_key), list):
                metadata[current_list_key].append(item)
            continue

        if ":" in stripped:
            key, val_raw = stripped.split(":", 1)
            key = key.strip()
            val_raw = val_raw.strip()
            if not val_raw:
                current_list_key = key
                metadata[key] = []
            else:
                current_list_key = None
                metadata[key] = _parse_val(val_raw)

    return metadata, body


def _ensure_workspace_memory_dir(workspace_root: Path) -> Path:
    """Ensure <workspace_root>/.botex/memory exists and is protected with .gitignore."""
    mem_dir = workspace_root / ".botex" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    botex_dir = workspace_root / ".botex"
    gi_path = botex_dir / ".gitignore"
    if not gi_path.exists():
        try:
            gi_path.write_text("# Auto-generated by BoteX Memory Vault\n*\n", encoding="utf-8")
        except Exception:
            pass

    return mem_dir


def _ensure_global_memory_dir(global_dir: Optional[Path] = None) -> Path:
    """Ensure global memory directory exists and is protected with .gitignore."""
    mem_dir = (global_dir or get_global_memory_dir()).resolve()
    mem_dir.mkdir(parents=True, exist_ok=True)

    parent = mem_dir.parent
    gi_path = parent / ".gitignore" if parent.name == ".botex" else mem_dir / ".gitignore"
    if not gi_path.exists():
        try:
            gi_path.write_text("# Auto-generated by BoteX Memory Vault\n*\n", encoding="utf-8")
        except Exception:
            pass

    return mem_dir


def _ensure_memory_dir(workspace_root: Path) -> Path:
    """Ensure <workspace_root>/.botex/memory exists. Retained for backwards compatibility."""
    return _ensure_workspace_memory_dir(workspace_root)


# ============================================================================
# SQLite WAL + FTS5 Full-Text Indexing
# ============================================================================


class _PooledConnection(sqlite3.Connection):
    """Pooled SQLite connection where close() is a no-op to eliminate reopen overhead."""

    def close(self) -> None:
        """No-op to retain connection alive in thread pool."""
        pass

    def force_close(self) -> None:
        """Explicitly dispose the underlying SQLite connection."""
        super().close()


_THREAD_CONNS = threading.local()
_ALL_CONNS: weakref.WeakSet[_PooledConnection] = weakref.WeakSet()
_CONNS_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def _get_index_db(mem_dir: Path) -> sqlite3.Connection:
    """Open or retrieve pooled SQLite index database in WAL mode with DDL memoization."""
    mem_dir.mkdir(parents=True, exist_ok=True)
    db_path = mem_dir / "index.db"
    db_key = str(db_path.resolve())

    if not hasattr(_THREAD_CONNS, "conns"):
        _THREAD_CONNS.conns = {}

    conn = _THREAD_CONNS.conns.get(db_key)
    if conn is not None:
        try:
            conn.execute("SELECT 1;")
            return conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            conn = None

    conn = sqlite3.connect(
        db_key,
        timeout=5.0,
        check_same_thread=False,
        factory=_PooledConnection,
    )
    conn.row_factory = sqlite3.Row
    conn._db_path = db_key

    with _CONNS_LOCK:
        _ALL_CONNS.add(conn)

    if db_key not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if db_key not in _SCHEMA_READY:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories_meta (
                        id TEXT PRIMARY KEY,
                        title TEXT,
                        kind TEXT,
                        tags TEXT,
                        links TEXT,
                        timestamp TEXT,
                        status TEXT,
                        compiled_truth INTEGER DEFAULT 0,
                        confidence REAL DEFAULT 1.0,
                        project TEXT DEFAULT '',
                        mtime REAL
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        id UNINDEXED,
                        title,
                        content,
                        tags,
                        kind,
                        tokenize='unicode61'
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories_edges (
                        source_id TEXT,
                        target_id TEXT,
                        kind TEXT,
                        PRIMARY KEY (source_id, target_id)
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_edges_target ON memories_edges(target_id);
                    """
                )
                # In-place schema migration: older vaults lack confidence or project columns.
                meta_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(memories_meta);").fetchall()
                }
                if "confidence" not in meta_cols:
                    conn.execute(
                        "ALTER TABLE memories_meta ADD COLUMN confidence REAL DEFAULT 1.0;"
                    )
                if "project" not in meta_cols:
                    conn.execute("ALTER TABLE memories_meta ADD COLUMN project TEXT DEFAULT '';")
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memories_project ON memories_meta(project);
                    """
                )
                _SCHEMA_READY.add(db_key)

    _THREAD_CONNS.conns[db_key] = conn
    return conn


def close_index_dbs() -> None:
    """Force close all pooled SQLite connections and clear schema ready cache."""
    with _CONNS_LOCK:
        conns = list(_ALL_CONNS)
        _ALL_CONNS.clear()
    for c in conns:
        try:
            c.force_close()
        except Exception:
            pass
    with _SCHEMA_LOCK:
        _SCHEMA_READY.clear()
    if hasattr(_THREAD_CONNS, "conns"):
        _THREAD_CONNS.conns.clear()


def reset_memory_index_cache() -> None:
    """Close all pooled connections and clear thread-local caches and schema ready state."""
    close_index_dbs()
    if hasattr(_THREAD_CONNS, "conns"):
        _THREAD_CONNS.conns.clear()


# On Windows, wrap shutil.rmtree to automatically release open SQLite file locks
# if a directory containing a pooled database is being deleted (e.g. TemporaryDirectory in tests).
_orig_rmtree = shutil.rmtree


def _safe_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        resolved = str(Path(path).resolve())
        with _CONNS_LOCK:
            conns = list(_ALL_CONNS)
        for c in conns:
            db_p = getattr(c, "_db_path", None)
            if db_p and (db_p == resolved or db_p.startswith(resolved + os.sep)):
                try:
                    c.force_close()
                except Exception:
                    pass
                with _CONNS_LOCK:
                    _ALL_CONNS.discard(c)
                with _SCHEMA_LOCK:
                    _SCHEMA_READY.discard(db_p)
                if hasattr(_THREAD_CONNS, "conns"):
                    _THREAD_CONNS.conns.pop(db_p, None)
    except Exception:
        pass
    return _orig_rmtree(path, *args, **kwargs)


shutil.rmtree = _safe_rmtree


def _sanitize_fts_query(q: str) -> str:
    """Convert raw text to safe FTS5 OR-match prefix tokens.

    Implicit-AND (the previous behavior) zeroed out recall whenever a single
    token was missing from the vault (live-miss: "VPS deployment" vs the
    "VPS Invariants" ADR). OR keeps multi-token documents ranked above
    single-token ones via bm25; precision is enforced downstream by the
    coverage admission gate (see _RECALL_MIN_COVERAGE)."""
    tokens = re.findall(r"[\w]+", q)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens)


def _index_memory_file(
    mem_dir: Path,
    conn: sqlite3.Connection,
    file_p: Path,
    known_ids: Optional[set[str]] = None,
) -> None:
    """Index or update a single memory markdown file in SQLite."""
    if file_p.name.startswith("_"):
        return
    try:
        raw_text = file_p.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw_text)
        mid = file_p.stem
        title = meta.get("title") or mid
        kind = str(meta.get("kind") or "context").lower()
        raw_tags = meta.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.strip("[]").split(",") if t.strip()]
        tags_str = " ".join(str(t) for t in raw_tags)
        body_links = extract_wikilinks(body)
        if "[[" in body or not meta.get("links"):
            raw_links = list(body_links)
        else:
            raw_links = meta.get("links") or []
        if isinstance(raw_links, str):
            raw_links = [l.strip() for l in raw_links.strip("[]").split(",") if l.strip()]
        else:
            raw_links = [str(l).strip() for l in raw_links if str(l).strip()]
        for bl in body_links:
            if bl not in raw_links:
                raw_links.append(bl)

        effective_known_ids = (
            known_ids
            if known_ids is not None
            else {
                f.stem for f in mem_dir.glob("*.md") if f.is_file() and not f.name.startswith("_")
            }
        )
        for il in extract_implicit_links(body, mid, effective_known_ids, raw_links):
            if il not in raw_links:
                raw_links.append(il)

        links_str = " ".join(str(l) for l in raw_links)
        ts = meta.get("timestamp") or ""
        status = str(meta.get("status") or "active").lower()
        compiled = 1 if meta.get("compiled_truth") else 0
        confidence = _coerce_confidence(meta.get("confidence"))
        project = _normalize_project_slug(meta.get("project"))
        if project == "_unassigned":
            project = ""
        mtime = file_p.stat().st_mtime

        with conn:
            conn.execute(
                """
                INSERT INTO memories_meta (id, title, kind, tags, links, timestamp, status, compiled_truth, confidence, project, mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    tags=excluded.tags,
                    links=excluded.links,
                    timestamp=excluded.timestamp,
                    status=excluded.status,
                    compiled_truth=excluded.compiled_truth,
                    confidence=excluded.confidence,
                    project=excluded.project,
                    mtime=excluded.mtime;
                """,
                (
                    mid,
                    title,
                    kind,
                    tags_str,
                    links_str,
                    ts,
                    status,
                    compiled,
                    confidence,
                    project,
                    mtime,
                ),
            )
            conn.execute("DELETE FROM memories_fts WHERE id = ?;", (mid,))
            conn.execute(
                """
                INSERT INTO memories_fts (id, title, content, tags, kind)
                VALUES (?, ?, ?, ?, ?);
                """,
                (mid, title, body, tags_str, kind),
            )
            conn.execute("DELETE FROM memories_edges WHERE source_id = ?;", (mid,))
            for link in raw_links:
                target_id = str(link).strip()
                if target_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO memories_edges (source_id, target_id, kind)
                        VALUES (?, ?, ?);
                        """,
                        (mid, target_id, "wikilink"),
                    )
    except Exception as exc:
        logger.debug("Failed to index memory file: %s", type(exc).__name__)


def sync_memory_index(mem_dir: Path) -> None:
    """Public helper to re-synchronize SQLite index with files on disk."""
    if not mem_dir.exists() or not mem_dir.is_dir():
        return
    try:
        conn = _get_index_db(mem_dir)
        try:
            rows = conn.execute("SELECT id, mtime FROM memories_meta;").fetchall()
            indexed = {row["id"]: float(row["mtime"]) for row in rows}
            md_files = [
                f for f in mem_dir.glob("*.md") if f.is_file() and not f.name.startswith("_")
            ]
            seen_files = {f.stem for f in md_files}
            for file_p in md_files:
                mid = file_p.stem
                st_mtime = file_p.stat().st_mtime
                if mid not in indexed or abs(indexed[mid] - st_mtime) > 0.001:
                    _index_memory_file(mem_dir, conn, file_p, known_ids=seen_files)

            to_delete = set(indexed.keys()) - seen_files
            if to_delete:
                with conn:
                    for del_id in to_delete:
                        conn.execute("DELETE FROM memories_meta WHERE id = ?;", (del_id,))
                        conn.execute("DELETE FROM memories_fts WHERE id = ?;", (del_id,))
                        conn.execute(
                            "DELETE FROM memories_edges WHERE source_id = ? OR target_id = ?;",
                            (del_id, del_id),
                        )
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Failed to synchronize memory index: %s", type(exc).__name__)


# ============================================================================
# Core API Operations
# ============================================================================


def save_memory(
    workspace_dir: str = ".",
    title: str = "",
    content: str = "",
    kind: str = "context",
    tags: Optional[List[str]] = None,
    memory_id: Optional[str] = None,
    origin: str = "botex",
    scope: str = "local",
    namespace: Optional[str] = None,
    status: Optional[str] = "active",
    compiled_truth: Optional[bool] = False,
    links: Optional[List[str]] = None,
    timeline: Optional[List[str]] = None,
    confidence: float = 1.0,
    project: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Save a context, decision, handoff, lesson, belief, or heuristic entry to
    the Memory Vault. Supports local workspace scope and global shared scope.
    Masks secrets and ensures directory isolation.
    ``confidence`` is a Hindsight-style certainty score clamped to [0.0, 1.0].
    """
    try:
        legacy_origin = kwargs.pop("source", None)
        if kwargs:
            raise TypeError(f"Unexpected arguments: {', '.join(sorted(kwargs))}")
        if legacy_origin is not None:
            warnings.warn(
                "The 'source' keyword argument is deprecated; use 'origin' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if origin == "botex":
                origin = legacy_origin

        norm_scope = (
            "global"
            if (
                namespace == "_global"
                or _VALID_SCOPES_MAP.get(str(scope).strip().lower()) == "global"
            )
            else "local"
        )

        norm_kind = _VALID_KINDS_MAP.get(str(kind).strip().lower(), "context")
        clean_tags = [sanitize_for_storage(str(t).strip()) for t in (tags or []) if str(t).strip()]
        masked_title = sanitize_for_storage(title.strip() or "Untitled Memory")
        masked_content = sanitize_for_storage(content.strip())

        if len(masked_content.encode("utf-8")) > MAX_MEMORY_BYTES:
            return {
                "ok": False,
                "error": f"Memory content exceeds maximum allowed size ({MAX_MEMORY_BYTES} bytes).",
            }

        mid = _sanitize_id(memory_id) if memory_id else f"mem_{uuid.uuid4().hex[:8]}"

        if norm_scope == "global":
            mem_dir = _ensure_global_memory_dir()
            rel_path = f"~/.botex/memory/{mid}.md"
        else:
            root_path = Path(workspace_dir).resolve()
            if not root_path.exists():
                return {
                    "ok": False,
                    "error": f"Workspace directory '{workspace_dir}' does not exist.",
                }
            mem_dir = _ensure_workspace_memory_dir(root_path)
            rel_path = f".botex/memory/{mid}.md"

        existing_file = _find_existing_memory_file(mem_dir, mid)
        file_path = existing_file if existing_file is not None else (mem_dir / f"{mid}.md")

        # Preserve existing project on resave when caller does not specify project
        if project is None and existing_file is not None:
            try:
                old_meta, _ = _parse_frontmatter(
                    existing_file.read_text(encoding="utf-8", errors="replace")
                )
                norm_project = _normalize_project_slug(old_meta.get("project"))
            except Exception:
                norm_project = ""
        else:
            norm_project = _normalize_project_slug(project)
        if norm_project == "_unassigned":
            norm_project = ""

        # Extract wikilinks and implicit references to existing notes
        clean_links = [str(l).strip() for l in (links or []) if str(l).strip()]
        for wl in extract_wikilinks(masked_content):
            if wl not in clean_links and wl != mid:
                clean_links.append(wl)
        known_ids = {
            f.stem for f in mem_dir.glob("*.md") if f.is_file() and not f.name.startswith("_")
        }
        for il in extract_implicit_links(masked_content, mid, known_ids, clean_links):
            if il not in clean_links:
                clean_links.append(il)

        norm_status = str(status).strip().lower() if status else "active"
        if norm_status not in VALID_STATUSES:
            norm_status = "active"

        conf_val = _coerce_confidence(confidence)

        metadata = {
            "id": mid,
            "schema": SCHEMA_VERSION,
            "title": masked_title,
            "kind": norm_kind,
            "status": norm_status,
            "compiled_truth": bool(compiled_truth),
            "confidence": conf_val,
            "project": norm_project,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tags": clean_tags,
            "links": clean_links,
            "source": sanitize_for_storage(str(origin)),
            "scope": norm_scope,
            "namespace": "_global" if norm_scope == "global" else "workspace",
        }
        if timeline:
            metadata["timeline"] = [str(item) for item in timeline]

        # Final barrier: the serialized document is sanitized again before the file sink.
        full_text = sanitize_for_storage(_serialize_frontmatter(metadata, masked_content))
        # codeql[py/clear-text-storage-sensitive-data]
        file_path.write_text(
            full_text, encoding="utf-8"
        )  # lgtm[py/clear-text-storage-sensitive-data]

        # Index in SQLite WAL FTS5
        try:
            conn = _get_index_db(mem_dir)
            try:
                _index_memory_file(mem_dir, conn, file_path, known_ids=known_ids | {mid})
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("SQLite index update skipped on save: %s", type(exc).__name__)

        return {
            "ok": True,
            "id": mid,
            "path": rel_path,
            "title": masked_title,
            "kind": norm_kind,
            "tags": clean_tags,
            "links": clean_links,
            "status": norm_status,
            "compiled_truth": bool(compiled_truth),
            "confidence": conf_val,
            "project": norm_project,
            "scope": norm_scope,
        }
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to save memory: {coerce_text(mask_secrets(str(exc)))}",
        }


def search_memory(
    workspace_dir: str = ".",
    query: str = "",
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    scope: str = "local",
    namespace: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    min_confidence: Optional[float] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search memories by keyword query, tag, or kind across local, global, or all scopes.
    Uses SQLite FTS5 index with fallback to disk scanning.
    ``since``/``until`` are ISO-8601 chronological bounds applied to entry
    timestamps; ``min_confidence`` keeps only entries at or above the given
    certainty threshold. When a query produces FTS5 hits, spreading
    activation over the wikilink edge graph also surfaces directly linked
    notes via reciprocal-rank-fusion bonuses.
    """
    try:
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        if namespace == "_global":
            norm_scope = "global"

        try:
            since_dt = _parse_temporal_bound(since, end_of_day=False)
            until_dt = _parse_temporal_bound(until, end_of_day=True)
        except (ValueError, TypeError):
            return {
                "ok": False,
                "error": "Invalid 'since'/'until' bound; expected ISO-8601 date or datetime.",
            }

        min_conf_val: Optional[float] = None
        if min_confidence is not None:
            if isinstance(min_confidence, bool):
                return {
                    "ok": False,
                    "error": "Invalid 'min_confidence'; expected a float in [0.0, 1.0].",
                }
            try:
                min_conf_val = float(min_confidence)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": "Invalid 'min_confidence'; expected a float in [0.0, 1.0].",
                }
            if not math.isfinite(min_conf_val):
                return {
                    "ok": False,
                    "error": "Invalid 'min_confidence'; expected a float in [0.0, 1.0].",
                }
            min_conf_val = min(max(min_conf_val, 0.0), 1.0)

        search_dirs: list[tuple[Path, str]] = []
        search_dirs = _resolve_scope_dirs(workspace_dir, norm_scope)

        q_lower = query.strip().lower() if query else ""
        tag_lower = tag.strip().lower() if tag else ""
        kind_lower = kind.strip().lower() if kind else ""
        norm_project = _normalize_project_slug(project) if project else ""
        target_project: Optional[str] = None
        if norm_project == "_unassigned":
            target_project = ""
        elif norm_project:
            target_project = norm_project

        results = []
        seen_ids = set()

        # Chronological / confidence / project pre-filter resolved through the SQLite index.
        filters_active = (
            since_dt is not None
            or until_dt is not None
            or min_conf_val is not None
            or target_project is not None
        )
        allowed_by_dir: Dict[Path, Optional[set]] = {}
        if filters_active:
            since_str = since_dt.isoformat() if since_dt else None
            until_str = until_dt.isoformat() if until_dt else None
            for mem_dir, _ in search_dirs:
                ids_set: Optional[set] = None
                if mem_dir.is_dir():
                    try:
                        sync_memory_index(mem_dir)
                        conn = _get_index_db(mem_dir)
                        try:
                            rows = conn.execute(
                                """
                                SELECT id FROM memories_meta
                                WHERE (? IS NULL OR (timestamp <> '' AND timestamp >= ?))
                                  AND (? IS NULL OR (timestamp <> '' AND timestamp <= ?))
                                  AND (? IS NULL OR confidence >= ?)
                                  AND (? IS NULL OR project = ?);
                                """,
                                (
                                    since_str,
                                    since_str,
                                    until_str,
                                    until_str,
                                    min_conf_val,
                                    min_conf_val,
                                    target_project,
                                    target_project,
                                ),
                            ).fetchall()
                            ids_set = {r["id"] for r in rows}
                        finally:
                            conn.close()
                    except Exception:
                        ids_set = None
                allowed_by_dir[mem_dir] = ids_set

        # Try FTS search if query is provided
        fts_candidates: Dict[str, int] = {}
        if query:
            for mem_dir, _ in search_dirs:
                if not mem_dir.exists():
                    continue
                try:
                    sync_memory_index(mem_dir)
                    conn = _get_index_db(mem_dir)
                    try:
                        safe_q = _sanitize_fts_query(query)
                        if safe_q:
                            rows = conn.execute(
                                """
                                SELECT id, rank FROM memories_fts
                                WHERE memories_fts MATCH ?
                                ORDER BY rank
                                LIMIT 50;
                                """,
                                (safe_q,),
                            ).fetchall()
                            for rank_idx, row in enumerate(rows):
                                fts_candidates[row["id"]] = rank_idx
                    finally:
                        conn.close()
                except Exception:
                    pass

        # Spreading activation: 1-hop neighbours of FTS hits in the wikilink
        # graph gain activation strength proportional to their connections.
        activation_candidates: Dict[str, int] = {}
        if fts_candidates:
            hit_ids = set(fts_candidates.keys())
            strength: Dict[str, int] = {}
            for mem_dir, _ in search_dirs:
                if not mem_dir.exists():
                    continue
                try:
                    conn = _get_index_db(mem_dir)
                    try:
                        edge_rows = conn.execute(
                            "SELECT source_id, target_id FROM memories_edges;"
                        ).fetchall()
                    finally:
                        conn.close()
                except Exception:
                    continue
                for er in edge_rows:
                    s_id = er["source_id"]
                    t_id = er["target_id"]
                    if s_id in hit_ids:
                        strength[t_id] = strength.get(t_id, 0) + 1
                    if t_id in hit_ids:
                        strength[s_id] = strength.get(s_id, 0) + 1
            ordered = sorted(strength.items(), key=lambda kv: (-kv[1], kv[0]))
            activation_candidates = {
                nid: idx for idx, (nid, _) in enumerate(ordered[:MAX_SPREAD_ACTIVATION])
            }

        for mem_dir, sc in search_dirs:
            if not mem_dir.exists() or not mem_dir.is_dir():
                continue
            for file_p in mem_dir.glob("*.md"):
                if not file_p.is_file():
                    continue
                try:
                    raw_text = file_p.read_text(encoding="utf-8", errors="replace")
                    meta, body = _parse_frontmatter(raw_text)
                    entry_id = file_p.stem
                    # Cross-vault shadowing fix: dedup is per (scope, id) —
                    # a global note may no longer hide a same-stem local one.
                    if (sc, entry_id) in seen_ids:
                        continue
                    entry_title = meta.get("title") or entry_id
                    entry_kind = str(meta.get("kind") or "context").lower()
                    entry_tags = meta.get("tags") or []
                    entry_links = meta.get("links") or []
                    entry_ts = meta.get("timestamp") or ""
                    entry_status = str(meta.get("status") or "active").lower()
                    entry_compiled = bool(meta.get("compiled_truth"))
                    entry_conf = _coerce_confidence(meta.get("confidence"))
                    entry_project = _normalize_project_slug(meta.get("project"))
                    if entry_project == "_unassigned":
                        entry_project = ""

                    # Filter by kind
                    if kind_lower and entry_kind != kind_lower:
                        continue

                    # Filter by tag
                    if tag_lower:
                        tags_lower = [str(t).lower() for t in entry_tags]
                        if tag_lower not in tags_lower:
                            continue

                    # Filter by project
                    if target_project is not None and entry_project != target_project:
                        continue

                    # Chronological / confidence gate (SQL pre-filter with
                    # frontmatter fallback when the index is unavailable)
                    if filters_active:
                        allow_set = allowed_by_dir.get(mem_dir)
                        if allow_set is None:
                            entry_ts_dt = _parse_timestamp_safe(entry_ts)
                            if since_dt and (entry_ts_dt is None or entry_ts_dt < since_dt):
                                continue
                            if until_dt and (entry_ts_dt is None or entry_ts_dt > until_dt):
                                continue
                            if min_conf_val is not None and entry_conf < min_conf_val:
                                continue
                        elif entry_id not in allow_set:
                            continue

                    # Filter by query: FTS (OR-ranked) / graph activation /
                    # coverage-based admission gate. Admission needs >=2
                    # DISTINCT matched tokens; the single-token exemption
                    # applies ONLY to 1-token queries (Agy review blocker #4
                    # — multi-token queries matching a single token stay out).
                    if q_lower and entry_id not in activation_candidates:
                        searchable = (
                            f"{entry_title} {body} {' '.join(str(t) for t in entry_tags)}".lower()
                        )
                        q_tokens = [tok for tok in re.findall(r"[\w]+", q_lower) if tok]
                        if q_tokens:
                            matched_set = {tok for tok in q_tokens if tok in searchable}
                            if len(matched_set) >= _RECALL_MIN_COVERAGE:
                                pass
                            elif (
                                len(q_tokens) == 1
                                and matched_set
                                and not (matched_set <= _RECALL_STOP_TOKENS)
                            ):
                                pass
                            else:
                                continue
                        elif q_lower not in searchable:
                            continue

                    seen_ids.add((sc, entry_id))
                    preview = body[:240] + ("…" if len(body) > 240 else "")
                    if sc == "global":
                        path_str = f"~/.botex/memory/{file_p.name}"
                    elif sc == "swarm":
                        path_str = f"swarm memory/{file_p.name}"
                    else:
                        path_str = f".botex/memory/{file_p.name}"
                    results.append(
                        {
                            "id": entry_id,
                            "title": entry_title,
                            "kind": entry_kind,
                            "tags": entry_tags,
                            "links": entry_links,
                            "status": entry_status,
                            "compiled_truth": entry_compiled,
                            "confidence": entry_conf,
                            "project": entry_project,
                            "activated": entry_id in activation_candidates,
                            "timestamp": entry_ts,
                            "preview": preview,
                            "content": body,
                            "path": path_str,
                            "scope": sc,
                        }
                    )
                except Exception:
                    continue

        # Sort: if query produced FTS/graph candidates, order by fused
        # Reciprocal Rank Fusion score; otherwise newest first
        if fts_candidates or activation_candidates:
            fused_order = {
                mid: pos
                for pos, (mid, _score) in enumerate(
                    reciprocal_rank_fusion(fts_candidates, activation_candidates)
                )
            }
            results.sort(
                key=lambda r: (
                    fused_order.get(r["id"], 9999),
                    r.get("timestamp", ""),
                )
            )
        else:
            results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

        return {"ok": True, "count": len(results), "memories": results}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to search memory: {coerce_text(mask_secrets(str(exc)))}",
        }


def read_memory(
    workspace_dir: str = ".",
    memory_id: str = "",
    scope: str = "local",
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read full memory entry by ID from local or global Memory Vault.
    """
    try:
        mid = _sanitize_id(memory_id)
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        if namespace == "_global":
            norm_scope = "global"

        candidates: list[tuple[Path, str]] = []
        if norm_scope == "global":
            candidates.append((get_global_memory_dir(), "global"))
        elif norm_scope == "all":
            candidates.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
            candidates.append((get_global_memory_dir(), "global"))
        else:
            candidates.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))

        found_path: Optional[Path] = None
        found_scope: str = "local"
        for mem_dir, sc in candidates:
            cand_file = _find_existing_memory_file(mem_dir, mid)
            if cand_file is not None:
                found_path = cand_file
                found_scope = sc
                break

        if not found_path:
            return {"ok": False, "error": f"Memory entry '{mid}' not found."}

        raw_text = found_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw_text)

        return {
            "ok": True,
            "id": mid,
            "metadata": meta,
            "content": wrap_untrusted_content(body, tag="untrusted_memory_content", id=mid),
            "scope": found_scope,
        }
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to read memory: {coerce_text(mask_secrets(str(exc)))}",
        }


def delete_memory(
    workspace_dir: str = ".",
    memory_id: str = "",
    scope: str = "local",
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete a memory entry by ID from local or global Memory Vault and clean up SQLite FTS5/graph indices.
    """
    try:
        mid = _sanitize_id(memory_id)
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        if namespace == "_global":
            norm_scope = "global"

        candidates: list[tuple[Path, str]] = []
        if norm_scope == "global":
            candidates.append((get_global_memory_dir(), "global"))
        elif norm_scope == "all":
            candidates.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
            candidates.append((get_global_memory_dir(), "global"))
        else:
            candidates.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))

        found_path: Optional[Path] = None
        found_dir: Optional[Path] = None
        found_scope: str = "local"
        for mem_dir, sc in candidates:
            cand_file = _find_existing_memory_file(mem_dir, mid)
            if cand_file is not None:
                found_path = cand_file
                found_dir = mem_dir
                found_scope = sc
                break

        if not found_path or not found_dir:
            return {"ok": False, "error": f"Memory entry '{mid}' not found."}

        found_path.unlink()

        try:
            conn = _get_index_db(found_dir)
            try:
                with conn:
                    conn.execute("DELETE FROM memories_meta WHERE id = ?;", (mid,))
                    conn.execute("DELETE FROM memories_fts WHERE id = ?;", (mid,))
                    conn.execute(
                        "DELETE FROM memories_edges WHERE source_id = ? OR target_id = ?;",
                        (mid, mid),
                    )
            finally:
                conn.close()
        except Exception:
            try:
                sync_memory_index(found_dir)
            except Exception:
                pass

        return {
            "ok": True,
            "id": mid,
            "memory_id": mid,
            "scope": found_scope,
            "deleted": True,
        }
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to delete memory: {coerce_text(mask_secrets(str(exc)))}",
        }


def list_memories(
    workspace_dir: str = ".",
    scope: str = "local",
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    namespace: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """List memory entries for the given workspace and scope."""
    return search_memory(
        workspace_dir=workspace_dir,
        query="",
        tag=tag,
        kind=kind,
        scope=scope,
        namespace=namespace,
        project=project,
    )


def list_memory_projects(
    workspace_dir: str = ".",
    scope: str = "all",
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate distinct projects and their note counts across requested scopes with ID deduplication."""
    try:
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "all")
        if namespace == "_global":
            norm_scope = "global"

        search_dirs = _resolve_scope_dirs(workspace_dir, norm_scope)
        seen_ids: set[str] = set()
        counts: Dict[str, int] = {}

        for mem_dir, _ in search_dirs:
            if not mem_dir.exists() or not mem_dir.is_dir():
                continue
            indexed_ok = False
            try:
                sync_memory_index(mem_dir)
                conn = _get_index_db(mem_dir)
                try:
                    rows = conn.execute("SELECT id, project FROM memories_meta;").fetchall()
                    for r in rows:
                        mid = str(r["id"] or "")
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        proj = _normalize_project_slug(r["project"])
                        if proj == "_unassigned":
                            proj = ""
                        counts[proj] = counts.get(proj, 0) + 1
                    indexed_ok = True
                finally:
                    conn.close()
            except Exception:
                indexed_ok = False

            if not indexed_ok:
                for file_p in mem_dir.glob("*.md"):
                    if not file_p.is_file() or file_p.name.startswith("_"):
                        continue
                    try:
                        raw_text = file_p.read_text(encoding="utf-8", errors="replace")
                        meta, _ = _parse_frontmatter(raw_text)
                        mid = file_p.stem
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        proj = _normalize_project_slug(meta.get("project"))
                        if proj == "_unassigned":
                            proj = ""
                        counts[proj] = counts.get(proj, 0) + 1
                    except Exception:
                        continue

        # Sort projects by count descending, with empty/unassigned last
        sorted_items = sorted(
            counts.items(),
            key=lambda kv: (1 if kv[0] == "" else 0, -kv[1], kv[0]),
        )
        projects_list = [{"project": k, "count": v} for k, v in sorted_items]
        return {
            "ok": True,
            "projects": projects_list,
            "total": sum(counts.values()),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to list memory projects: {coerce_text(mask_secrets(str(exc)))}",
        }


def purge_ephemeral_memories(
    workspace_dir: str = ".",
    max_age_days: int = 3,
    scope: str = "local",
    namespace: Optional[str] = None,
) -> int:
    """
    Delete Memory Vault entries tagged ``ephemeral`` that are older than
    ``max_age_days``. Entries without that tag (user notes, decisions,
    handoffs, lessons) are never touched. Returns the number of deleted files.
    """
    deleted = 0
    try:
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        if namespace == "_global":
            norm_scope = "global"

        dirs: list[Path] = []
        if norm_scope in ("local", "all"):
            dirs.append(Path(workspace_dir).resolve() / ".botex" / "memory")
        if norm_scope in ("global", "all"):
            dirs.append(get_global_memory_dir())

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(max_age_days), 0))
        for mem_dir in dirs:
            if not mem_dir.is_dir():
                continue
            for file_p in mem_dir.glob("*.md"):
                try:
                    if not file_p.is_file():
                        continue
                    meta, _ = _parse_frontmatter(
                        file_p.read_text(encoding="utf-8", errors="replace")
                    )
                    tags = [str(t).lower() for t in (meta.get("tags") or [])]
                    if "ephemeral" not in tags:
                        continue
                    ts = datetime.fromisoformat(str(meta.get("timestamp") or ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts <= cutoff:
                        file_p.unlink()
                        deleted += 1
                except Exception:
                    continue
            sync_memory_index(mem_dir)
    except Exception:
        return deleted
    return deleted


# ============================================================================
# Knowledge Graph & PKM Tools (Sprint 3)
# ============================================================================


def _resolve_scope_dirs(workspace_dir: str = ".", scope: str = "local") -> list[tuple[Path, str]]:
    """Helper to resolve memory directories and their scope tags."""
    norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
    search_dirs: list[tuple[Path, str]] = []
    swarm_dir = get_swarm_memory_dir()
    if norm_scope == "global":
        search_dirs.append((get_global_memory_dir(), "global"))
    elif norm_scope == "swarm":
        if swarm_dir:
            search_dirs.append((swarm_dir, "swarm"))
    elif norm_scope == "all":
        search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
        search_dirs.append((get_global_memory_dir(), "global"))
        if swarm_dir:
            search_dirs.append((swarm_dir, "swarm"))
    else:
        search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
    # Same physical dir reachable under two scope labels (e.g. swarm vault
    # == local vault) must not double-scan — dedup on resolved path.
    resolved_seen: set = set()
    deduped: list[tuple[Path, str]] = []
    for d, sc in search_dirs:
        try:
            rp = d.resolve()
        except Exception:
            rp = d
        if rp in resolved_seen:
            continue
        resolved_seen.add(rp)
        deduped.append((d, sc))
    return deduped


def get_backlinks(
    workspace_dir: str = ".",
    memory_id: str = "",
    scope: str = "local",
) -> Dict[str, Any]:
    """
    Retrieves all memories that link TO the given memory_id.
    Queries the SQLite edges index for sub-millisecond retrieval.
    """
    clean_id = sanitize_for_storage(str(memory_id).strip())
    if not clean_id:
        return {"ok": False, "error": "Missing memory_id"}

    try:
        norm_sc = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        search_dirs = _resolve_scope_dirs(workspace_dir, "all" if norm_sc == "local" else norm_sc)
        backlinks: List[Dict[str, Any]] = []
        seen_sources: set[str] = set()

        for mem_dir, sc in search_dirs:
            if not mem_dir.exists():
                continue
            sync_memory_index(mem_dir)
            conn = _get_index_db(mem_dir)
            try:
                rows = conn.execute(
                    """
                    SELECT e.source_id, m.title, m.kind, m.tags, m.status, m.compiled_truth, m.timestamp
                    FROM memories_edges e
                    LEFT JOIN memories_meta m ON e.source_id = m.id
                    WHERE e.target_id = ?
                    ORDER BY m.timestamp DESC;
                    """,
                    (clean_id,),
                ).fetchall()
                for r in rows:
                    sid = r["source_id"]
                    if sid and sid not in seen_sources:
                        seen_sources.add(sid)
                        backlinks.append(
                            {
                                "id": sid,
                                "title": r["title"] or sid,
                                "kind": r["kind"] or "context",
                                "tags": r["tags"].split() if r["tags"] else [],
                                "status": r["status"] or "active",
                                "compiled_truth": bool(r["compiled_truth"]),
                                "timestamp": r["timestamp"] or "",
                                "scope": sc,
                            }
                        )
            finally:
                conn.close()

        return {
            "ok": True,
            "target_id": clean_id,
            "count": len(backlinks),
            "backlinks": backlinks,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Failed to retrieve backlinks: {exc}"}


def patch_memory_section(
    workspace_dir: str = ".",
    memory_id: str = "",
    section: str = "Compiled Truth",
    new_content: str = "",
    scope: str = "local",
    append: bool = False,
) -> Dict[str, Any]:
    """
    Surgically replaces or appends content to a specific Markdown section (e.g. '## Compiled Truth')
    without modifying YAML frontmatter, other sections, or the Timeline audit trail.
    """
    clean_id = sanitize_for_storage(str(memory_id).strip())
    if not clean_id:
        return {"ok": False, "error": "Missing memory_id"}

    norm_sec = section.strip()
    if not norm_sec:
        norm_sec = "Compiled Truth"

    # Invariant: Timeline is an append-only audit trail and cannot be overwritten
    if norm_sec.lower() == "timeline" and not append:
        return {
            "ok": False,
            "error": "Section 'Timeline' is an append-only audit trail and cannot be overwritten",
        }

    norm_sc = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
    search_dirs = _resolve_scope_dirs(workspace_dir, "all" if norm_sc == "local" else norm_sc)
    file_path: Optional[Path] = None
    for mem_dir, _ in search_dirs:
        cand = _find_existing_memory_file(mem_dir, clean_id)
        if cand is not None:
            file_path = cand
            break

    if not file_path or not file_path.exists():
        return {"ok": False, "error": f"Memory entry '{clean_id}' not found"}

    try:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw_text)

        header_pattern = re.compile(
            rf"^(#{{1,6}})\s+{re.escape(norm_sec)}\b.*$", re.MULTILINE | re.IGNORECASE
        )
        match = header_pattern.search(body)
        clean_new_content = sanitize_for_storage(new_content.strip())

        if match:
            header_level = len(match.group(1))
            start_idx = match.end()

            # Find next header boundary respecting code fences (``` or ~~~) and header level <= header_level
            lines = body[start_idx:].splitlines(keepends=True)
            in_fence = False
            fence_marker = ""
            end_offset = len(body[start_idx:])
            current_pos = 0

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    marker = stripped[:3]
                    if not in_fence:
                        in_fence = True
                        fence_marker = marker
                    elif marker == fence_marker:
                        in_fence = False
                elif not in_fence:
                    h_match = re.match(r"^(#{1," + str(header_level) + r"})\s+\S", line)
                    if h_match:
                        end_offset = current_pos
                        break
                current_pos += len(line)

            end_idx = start_idx + end_offset
            if append:
                existing_sec = body[start_idx:end_idx].rstrip()
                updated_sec = f"{existing_sec}\n\n{clean_new_content}\n\n"
            else:
                updated_sec = f"\n\n{clean_new_content}\n\n"
            new_body = body[:start_idx] + updated_sec + body[end_idx:]
        else:
            timeline_match = re.search(
                r"^(#{1,6}\s+Timeline\b.*$)", body, re.MULTILINE | re.IGNORECASE
            )
            section_block = f"\n\n## {norm_sec}\n\n{clean_new_content}\n"
            if timeline_match:
                insert_pos = timeline_match.start()
                new_body = body[:insert_pos].rstrip() + section_block + "\n" + body[insert_pos:]
            else:
                new_body = body.rstrip() + section_block

        if "compiled" in norm_sec.lower():
            meta["compiled_truth"] = True

        meta["links"] = extract_wikilinks(new_body)

        updated_full_text = sanitize_for_storage(_serialize_frontmatter(meta, new_body))
        file_path.write_text(updated_full_text, encoding="utf-8")

        mem_dir = file_path.parent
        try:
            conn = _get_index_db(mem_dir)
            try:
                _index_memory_file(mem_dir, conn, file_path)
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Failed to re-index memory note after patch: %s", type(exc).__name__)

        return {
            "ok": True,
            "id": clean_id,
            "section": norm_sec,
            "path": str(file_path),
            "compiled_truth": bool(meta.get("compiled_truth")),
            "links": meta.get("links", []),
        }
    except Exception as exc:
        return {"ok": False, "error": f"Failed to patch section: {exc}"}


_LEDGER_TAGS = frozenset({"task-receipt", "task_report", "temp-handoff", "ephemeral"})


def _is_ledger_node(node: Dict[str, Any]) -> bool:
    """Return True for ephemeral/operational ledger or deprecated nodes excluded from orphan_count."""
    # Exclude deprecated / superseded notes from orphan counting (terminal state)
    status = str(node.get("status") or "").strip().lower()
    if status in ("deprecated", "superseded"):
        return True
    nid = str(node.get("id") or "").strip().lower()
    if nid == "curator_health_status" or nid.startswith("task-receipt") or nid.startswith("task_report"):
        return True
    tags = {str(t).strip().lower().lstrip("#") for t in (node.get("tags") or [])}
    return bool(tags & _LEDGER_TAGS)


def set_memory_status(
    workspace_dir: str,
    memory_id: str,
    status: str,
    superseded_by: Optional[str] = None,
    reason: str = "",
    scope: str = "all",
) -> bool:
    """Update note status (e.g. deprecated / superseded) without losing provenance.

    Preserves original timestamp, custom frontmatter keys, and adds an audit line
    into ## Timeline containing [[canonical_id]] so an active graph edge is formed.
    """
    clean_id = _sanitize_id(memory_id)
    norm_status = str(status).strip().lower()
    if norm_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(VALID_STATUSES)}")

    search_dirs = _resolve_scope_dirs(workspace_dir, scope)
    target_path: Optional[Path] = None
    for s_dir, _ in search_dirs:
        cand = _find_existing_memory_file(s_dir, clean_id)
        if cand is not None:
            target_path = cand
            break

    if target_path is None or not target_path.exists():
        logger.warning("set_memory_status: Note '%s' not found", clean_id)
        return False

    mem_dir = target_path.parent
    try:
        timeline_msg = f"Status updated to '{norm_status}'."
        if superseded_by:
            clean_super = _sanitize_id(superseded_by.strip("[]"))
            timeline_msg = f"Deprecated in favor of [[{clean_super}]]."
        if reason:
            timeline_msg += f" Reason: {reason.strip()}"

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        audit_line = f"- {today_str}: {timeline_msg}"

        # 1. Append audit line to Timeline section using patch_memory_section
        patch_res = patch_memory_section(
            workspace_dir=workspace_dir,
            memory_id=clean_id,
            section="Timeline",
            new_content=audit_line,
            append=True,
            scope=scope,
        )
        if not patch_res.get("ok"):
            logger.warning("set_memory_status: patch_memory_section failed for %s: %s", clean_id, patch_res.get("error"))
            return False

        # 2. Update status in frontmatter preserving all keys & original timestamp
        updated_raw = target_path.read_text(encoding="utf-8", errors="replace")
        up_meta, up_body = _parse_frontmatter(updated_raw)
        up_meta["status"] = norm_status
        if superseded_by:
            up_meta["superseded_by"] = _sanitize_id(superseded_by.strip("[]"))

        new_doc = sanitize_for_storage(_serialize_frontmatter(up_meta, up_body))
        target_path.write_text(new_doc, encoding="utf-8")

        conn = _get_index_db(mem_dir)
        try:
            _index_memory_file(mem_dir, conn, target_path)
        finally:
            conn.close()

        logger.info("set_memory_status: Note '%s' updated to status='%s'", clean_id, norm_status)
        return True
    except Exception as exc:
        logger.error("set_memory_status: Failed on '%s': %s", clean_id, exc)
        return False


def find_graph_health(
    workspace_dir: str = ".",
    scope: str = "local",
) -> Dict[str, Any]:
    """
    Audits Memory Vault graph integrity:
    1. Broken Links: links pointing to memory_ids that do not exist (sets is_healthy=False).
    2. Orphan Notes: notes with zero incoming and zero outgoing links (topological metric).
    3. Cycles: detects mutual/circular references via iterative DFS (topological metric;
       bidirectional PKM links do not invalidate is_healthy).
    """
    try:
        search_dirs = _resolve_scope_dirs(workspace_dir, scope)
        all_ids: set[str] = set()
        edges: List[Dict[str, str]] = []
        node_metadata: Dict[str, Dict[str, Any]] = {}

        for mem_dir, sc in search_dirs:
            if not mem_dir.exists():
                continue
            sync_memory_index(mem_dir)
            conn = _get_index_db(mem_dir)
            try:
                rows = conn.execute(
                    "SELECT id, title, kind, tags, status, compiled_truth, project, mtime, confidence FROM memories_meta;"
                ).fetchall()
                for r in rows:
                    mid = r["id"]
                    all_ids.add(mid)
                    raw_tags = r["tags"] or ""
                    tags_list = [
                        t.strip().lstrip("#")
                        for t in re.split(r"[\s,]+", str(raw_tags))
                        if t.strip()
                    ]
                    node_metadata[mid] = {
                        "id": mid,
                        "title": r["title"] or mid,
                        "kind": r["kind"] or "context",
                        "status": r["status"] or "active",
                        "compiled_truth": bool(r["compiled_truth"]),
                        "tags": tags_list,
                        "scope": sc,
                        "project": _normalize_project_slug(r["project"]) if r["project"] else "",
                        "mtime": float(r["mtime"] or 0.0),
                        "confidence": float(
                            r["confidence"] if r["confidence"] is not None else 1.0
                        ),
                    }

                edge_rows = conn.execute(
                    "SELECT source_id, target_id, kind FROM memories_edges;"
                ).fetchall()
                for er in edge_rows:
                    edges.append(
                        {
                            "source": er["source_id"],
                            "target": er["target_id"],
                            "kind": er["kind"] or "wikilink",
                        }
                    )
            finally:
                conn.close()

        in_deg: Dict[str, int] = {nid: 0 for nid in all_ids}
        out_deg: Dict[str, int] = {nid: 0 for nid in all_ids}
        broken_links: List[Dict[str, str]] = []

        for e in edges:
            src = e["source"]
            tgt = e["target"]
            if src in out_deg:
                out_deg[src] += 1
            if tgt in all_ids:
                in_deg[tgt] += 1
            else:
                broken_links.append({"source": src, "target": tgt})

        isolated_nodes = [
            node_metadata[nid]
            for nid in all_ids
            if in_deg.get(nid, 0) == 0 and out_deg.get(nid, 0) == 0
        ]
        ledger_nodes = [n for n in isolated_nodes if _is_ledger_node(n)]
        orphans = sorted(
            (n for n in isolated_nodes if not _is_ledger_node(n)),
            key=lambda n: (str(n.get("project") or ""), str(n.get("id") or "")),
        )
        all_nodes_sorted = sorted(
            node_metadata.values(),
            key=lambda n: (str(n.get("project") or ""), str(n.get("id") or "")),
        )

        # Cycle detection via iterative DFS (O(V+E), no recursion limit)
        adj: Dict[str, List[str]] = {nid: [] for nid in all_ids}
        for e in edges:
            if e.get("target") in all_ids and e.get("source") in all_ids:
                adj[e["source"]].append(e["target"])

        cycles: List[List[str]] = []
        # state: 0 = unvisited, 1 = visiting (on current path), 2 = visited
        state: Dict[str, int] = {nid: 0 for nid in all_ids}

        for start_node in all_ids:
            if state[start_node] != 0:
                continue

            # Stack stores [node, neighbor_index]
            stack: List[List[Any]] = [[start_node, 0]]
            path: List[str] = [start_node]
            state[start_node] = 1

            while stack:
                curr, n_idx = stack[-1]
                neighbors = adj.get(curr, [])

                if n_idx < len(neighbors):
                    nxt = neighbors[n_idx]
                    stack[-1][1] = n_idx + 1

                    if state.get(nxt, 0) == 1:
                        if nxt in path:
                            c_start = path.index(nxt)
                            cycle = path[c_start:] + [nxt]
                            if cycle not in cycles:
                                cycles.append(cycle)
                    elif state.get(nxt, 0) == 0:
                        state[nxt] = 1
                        path.append(nxt)
                        stack.append([nxt, 0])
                else:
                    state[curr] = 2
                    stack.pop()
                    path.pop()

        # In PKM / Zettelkasten graphs, bidirectional links (A <-> B) are valid and healthy;
        # only broken references degrade graph integrity.
        is_healthy = len(broken_links) == 0

        return {
            "ok": True,
            "total_nodes": len(all_ids),
            "total_edges": len(edges),
            "broken_links": broken_links,
            "broken_count": len(broken_links),
            "orphans": orphans,
            "orphan_count": len(orphans),
            "ledger_count": len(ledger_nodes),
            "all_nodes": all_nodes_sorted,
            "edges": edges,
            "cycles": cycles,
            "cycle_count": len(cycles),
            "is_healthy": is_healthy,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Graph health check failed: {exc}"}


# ============================================================================
# Reflection & Hindsight Memory Evolution
# ============================================================================

# Canonical reasoning order for reflect_memory synthesis groups.
_REFLECT_KIND_ORDER = ["decision", "belief", "heuristic", "lesson", "handoff", "context"]


def reflect_memory(
    workspace_dir: str = ".",
    query: str = "",
    topic: Optional[str] = None,
    min_confidence: Optional[float] = 0.5,
    scope: str = "all",
    namespace: Optional[str] = None,
    limit: int = 20,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Hindsight-style reflection over the Memory Vault.

    Gathers facts, decisions, beliefs, heuristics, and lessons via
    ``search_memory`` and synthesizes them into an ordered reasoning digest.
    The synthesis is fully deterministic and local (zero LLM calls): entries
    are grouped by kind in canonical reasoning order, then ranked inside each
    group by confidence and recency.

    Args:
        workspace_dir: Workspace root directory.
        query: Free-text query steering which memories to reflect on.
        topic: Optional tag restricting the reflection to a thematic bucket.
        min_confidence: Minimum certainty threshold (default 0.5).
        scope: 'local', 'global', or 'all' (default 'all').
        namespace: Optional namespace alias ('_global' forces global scope).
        limit: Maximum number of entries included in the digest (default 20).
        project: Optional project slug filter.
    """
    try:
        conf_threshold = _coerce_confidence(min_confidence, default=0.5)

        res = search_memory(
            workspace_dir=workspace_dir,
            query=str(query or ""),
            tag=str(topic).strip() if topic else None,
            scope=scope,
            namespace=namespace,
            min_confidence=conf_threshold,
            project=project,
        )
        if not res.get("ok"):
            return res

        cap = max(int(limit), 1) if limit else 20
        mems = (res.get("memories") or [])[:cap]

        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in mems:
            k = str(m.get("kind") or "context").lower()
            groups.setdefault(k, []).append(
                {
                    "id": m.get("id"),
                    "title": m.get("title") or m.get("id"),
                    "kind": k,
                    "confidence": _coerce_confidence(m.get("confidence")),
                    "status": m.get("status") or "active",
                    "timestamp": m.get("timestamp") or "",
                    "scope": m.get("scope") or "local",
                    "activated": bool(m.get("activated")),
                    "preview": (str(m.get("preview") or "").splitlines() or [""])[0][:200],
                }
            )

        ordered_kinds = [k for k in _REFLECT_KIND_ORDER if k in groups] + [
            k for k in sorted(groups) if k not in _REFLECT_KIND_ORDER
        ]
        for k in ordered_kinds:
            groups[k].sort(key=lambda it: (it["confidence"], it["timestamp"]), reverse=True)

        total = sum(len(groups[k]) for k in ordered_kinds)
        conf_sum = sum(it["confidence"] for k in ordered_kinds for it in groups[k])
        avg_conf = conf_sum / total if total else 0.0

        heading = str(topic).strip() or str(query).strip() or "all memories"
        lines = [
            f"## Memory Reflection — {heading}",
            f"_Entries: {total} | confidence >= {conf_threshold:.2f} | "
            f"mean confidence {avg_conf:.2f}_",
        ]
        if not total:
            lines.append("")
            lines.append("No memories matched the reflection criteria.")
        for k in ordered_kinds:
            lines.append("")
            lines.append(f"### {k.capitalize()} ({len(groups[k])})")
            for it in groups[k]:
                date_str = it["timestamp"][:10]
                preview_str = f" — {it['preview']}" if it["preview"] else ""
                lines.append(
                    f"- [conf {it['confidence']:.2f}] **{it['title']}** "
                    f"(`{it['id']}`, {date_str}){preview_str}"
                )
        synthesis = "\n".join(lines)

        return {
            "ok": True,
            "query": str(query or ""),
            "topic": str(topic).strip() if topic else None,
            "min_confidence": conf_threshold,
            "count": total,
            "avg_confidence": round(avg_conf, 4),
            "kinds": ordered_kinds,
            "groups": groups,
            "synthesis": synthesis,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to reflect on memory: {coerce_text(mask_secrets(str(exc)))}",
        }


# ---------------------------------------------------------------------------
# Server-side recall (graph expansion + deterministic fallback)
# ---------------------------------------------------------------------------

RECALL_DEFAULT_TOP_K = 5
RECALL_DEFAULT_TOKEN_BUDGET = 5000
RECALL_MAX_NODE_CHARS = 6000
_RECALL_RELATION_ORDER = ("match", "link", "backlink")


def _estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars per token)."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


def _load_recall_node(workspace_dir: str, memory_id: str, scope: str) -> Optional[Dict[str, Any]]:
    """Load a raw (unwrapped) memory body for recall context, or None."""
    try:
        mid = _sanitize_id(memory_id)
    except Exception:
        return None
    for mem_dir, sc in _resolve_scope_dirs(workspace_dir, scope):
        path = _find_existing_memory_file(mem_dir, mid)
        if path is None:
            continue
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None
        return {
            "id": mid,
            "title": str(meta.get("title") or mid),
            "kind": str(meta.get("kind") or "context"),
            "status": str(meta.get("status") or "active"),
            "scope": sc,
            "content": body,
        }
    return None


def gather_recall_context(
    workspace_dir: str = ".",
    query: str = "",
    topic: Optional[str] = None,
    scope: str = "all",
    top_k: int = RECALL_DEFAULT_TOP_K,
    token_budget: int = RECALL_DEFAULT_TOKEN_BUDGET,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Collect the context for a single-shot recall: top-K search hits plus their
    1-hop graph neighbourhood (outgoing wikilinks and backlinks), trimmed to a
    token budget. Matches are prioritised over links, links over backlinks.
    Superseded/deprecated neighbours are skipped. Zero LLM calls.

    Returns ``{"ok": True, "nodes": [...], "truncated": bool, "token_estimate": int}``
    where each node is ``{id, title, kind, scope, relation, content}``.
    """
    try:
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "all")
        k = top_k if isinstance(top_k, int) and not isinstance(top_k, bool) else 0
        k = min(max(k, 1), 10) if k else RECALL_DEFAULT_TOP_K
        budget = (
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else RECALL_DEFAULT_TOKEN_BUDGET
        )
        budget = min(max(budget, 500), 16000)

        res = search_memory(
            workspace_dir=workspace_dir,
            query=str(query or ""),
            tag=str(topic).strip() if topic else None,
            scope=norm_scope,
            project=project,
        )
        if not res.get("ok"):
            return res

        candidates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        matches = [
            m for m in (res.get("memories") or []) if (m.get("status") or "active") == "active"
        ][:k]
        for m in matches:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            candidates.append(
                {
                    "id": mid,
                    "title": str(m.get("title") or mid),
                    "kind": str(m.get("kind") or "context"),
                    "scope": str(m.get("scope") or "local"),
                    "relation": "match",
                    "content": str(m.get("content") or ""),
                }
            )

        neighbours: List[Dict[str, Any]] = []
        for node in list(candidates):
            for target in extract_wikilinks(node["content"]):
                if target in seen:
                    continue
                loaded = _load_recall_node(workspace_dir, target, norm_scope)
                if loaded is None or loaded["status"] != "active":
                    continue
                seen.add(loaded["id"])
                loaded["relation"] = "link"
                neighbours.append(loaded)
            bl = get_backlinks(workspace_dir=workspace_dir, memory_id=node["id"], scope=norm_scope)
            for b in bl.get("backlinks") or [] if bl.get("ok") else []:
                bid = str(b.get("id") or "")
                if not bid or bid in seen or (b.get("status") or "active") != "active":
                    continue
                loaded = _load_recall_node(workspace_dir, bid, norm_scope)
                if loaded is None:
                    continue
                seen.add(loaded["id"])
                loaded["relation"] = "backlink"
                neighbours.append(loaded)

        neighbours.sort(key=lambda n: _RECALL_RELATION_ORDER.index(n["relation"]))
        nodes: List[Dict[str, Any]] = []
        used = 0
        truncated = False
        for node in candidates + neighbours:
            content = node["content"][:RECALL_MAX_NODE_CHARS]
            cost = _estimate_tokens(content) + 20
            if used + cost > budget:
                remaining_chars = (budget - used - 20) * 4
                truncated = True
                if remaining_chars < 200:
                    break
                content = content[:remaining_chars]
                cost = _estimate_tokens(content) + 20
            nodes.append(
                {
                    "id": node["id"],
                    "title": node["title"],
                    "kind": node["kind"],
                    "scope": node["scope"],
                    "relation": node["relation"],
                    "content": content,
                }
            )
            used += cost
            if truncated:
                break

        return {"ok": True, "nodes": nodes, "truncated": truncated, "token_estimate": used}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to gather recall context: {coerce_text(mask_secrets(str(exc)))}",
        }


def deterministic_recall(query: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    LLM-free fallback for ``recall_memory``: builds a compact digest from the
    first meaningful lines of each gathered node. Never invents facts.
    """
    q = str(query or "").strip()
    if not nodes:
        return {
            "answer": f"Brak udokumentowanych informacji w Memory Vault na temat: {q}.",
            "sources": [],
            "confidence": "NOT_FOUND",
            "tier_used": "deterministic",
        }
    lines: List[str] = []
    for n in nodes:
        body_lines = [
            ln.strip()
            for ln in str(n.get("content") or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        snippet = " ".join(body_lines[:2])[:280]
        lines.append(f"- [{n.get('relation', 'match')}] {n.get('title') or n.get('id')}: {snippet}")
    has_match = any(n.get("relation") == "match" for n in nodes)
    return {
        "answer": "\n".join(lines),
        "sources": [str(n.get("id")) for n in nodes],
        "confidence": "MEDIUM" if has_match else "LOW",
        "tier_used": "deterministic",
    }

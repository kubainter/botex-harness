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
import os
import re
import sqlite3
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        wrap_untrusted_content,
    )
except (ImportError, ValueError):
    from security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        wrap_untrusted_content,
    )

__all__ = [
    "save_memory",
    "search_memory",
    "read_memory",
    "delete_memory",
    "list_memories",
    "purge_ephemeral_memories",
    "get_global_memory_dir",
    "extract_wikilinks",
    "reciprocal_rank_fusion",
    "sync_memory_index",
    "VALID_KINDS",
    "VALID_SCOPES",
    "VALID_STATUSES",
    "MAX_MEMORY_BYTES",
    "SCHEMA_VERSION",
]

_VALID_KINDS_MAP: dict[str, str] = {
    "context": "context",
    "decision": "decision",
    "handoff": "handoff",
    "lesson": "lesson",
}
VALID_KINDS = frozenset(_VALID_KINDS_MAP.keys())

_VALID_SCOPES_MAP: dict[str, str] = {
    "local": "local",
    "global": "global",
    "_global": "global",
    "all": "all",
    "both": "all",
}
VALID_SCOPES = frozenset(["local", "global", "all"])

VALID_STATUSES = frozenset(["active", "superseded", "deprecated"])

MEMORY_DIR_NAME = ".botex/memory"
SCHEMA_VERSION = "ecc.memory.v2"
MAX_MEMORY_BYTES = 1_048_576  # 1 MB cap

# Regex for extracting Obsidian-style wikilinks [[target]] or [[target|display]]
_WIKILINK_RE = re.compile(r"\[\[([a-zA-Z0-9_\-\./]+)(?:\|([^\]]+))?\]\]")


def extract_wikilinks(text: str) -> List[str]:
    """Extract unique wikilink target IDs from markdown body (ignoring fenced/inline code blocks)."""
    if not text:
        return []
    # Strip fenced code blocks and inline code so prompt examples like `[[wikilink]]` never create edges
    cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"`+[^`\n]+`+", "", cleaned)
    matches = _WIKILINK_RE.findall(cleaned)
    targets: List[str] = []
    for m in matches:
        tgt = m[0].strip()
        if tgt.lower().endswith(".md"):
            tgt = tgt[:-3].strip()
        if tgt and tgt not in targets:
            targets.append(tgt)
    return targets


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
        from .config import load_config

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
    if not re.match(r"^[a-zA-Z0-9_\-]+$", clean):
        raise SecurityError(
            f"Invalid memory_id '{memory_id}'. Only alphanumeric, '_' and '-' allowed."
        )
    return coerce_text(clean)


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


def _get_index_db(mem_dir: Path) -> sqlite3.Connection:
    """Open or initialize SQLite index database in WAL mode."""
    db_path = mem_dir / "index.db"
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
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
    return conn


def _sanitize_fts_query(q: str) -> str:
    """Convert raw text to safe FTS5 prefix match tokens."""
    tokens = re.findall(r"[\w]+", q)
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


def _index_memory_file(mem_dir: Path, conn: sqlite3.Connection, file_p: Path) -> None:
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
            raw_links = body_links
        else:
            raw_links = meta.get("links") or []
        if isinstance(raw_links, str):
            raw_links = [l.strip() for l in raw_links.strip("[]").split(",") if l.strip()]
        links_str = " ".join(str(l) for l in raw_links)
        ts = meta.get("timestamp") or ""
        status = str(meta.get("status") or "active").lower()
        compiled = 1 if meta.get("compiled_truth") else 0
        mtime = file_p.stat().st_mtime

        with conn:
            conn.execute(
                """
                INSERT INTO memories_meta (id, title, kind, tags, links, timestamp, status, compiled_truth, mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    tags=excluded.tags,
                    links=excluded.links,
                    timestamp=excluded.timestamp,
                    status=excluded.status,
                    compiled_truth=excluded.compiled_truth,
                    mtime=excluded.mtime;
                """,
                (mid, title, kind, tags_str, links_str, ts, status, compiled, mtime),
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
    except Exception:
        pass


def sync_memory_index(mem_dir: Path) -> None:
    """Public helper to re-synchronize SQLite index with files on disk."""
    if not mem_dir.exists() or not mem_dir.is_dir():
        return
    try:
        conn = _get_index_db(mem_dir)
        try:
            rows = conn.execute("SELECT id, mtime FROM memories_meta;").fetchall()
            indexed = {row["id"]: float(row["mtime"]) for row in rows}
            seen_files = set()
            for file_p in mem_dir.glob("*.md"):
                if not file_p.is_file() or file_p.name.startswith("_"):
                    continue
                mid = file_p.stem
                seen_files.add(mid)
                st_mtime = file_p.stat().st_mtime
                if mid not in indexed or abs(indexed[mid] - st_mtime) > 0.001:
                    _index_memory_file(mem_dir, conn, file_p)

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
    except Exception:
        pass


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
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Save a context, decision, handoff, or lesson entry to the Memory Vault.
    Supports local workspace scope and global shared scope.
    Masks secrets and ensures directory isolation.
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

        file_path = resolve_safe_path(mem_dir, f"{mid}.md")

        # Extract wikilinks if not provided
        clean_links = [str(l).strip() for l in (links or []) if str(l).strip()]
        if not clean_links:
            clean_links = extract_wikilinks(masked_content)

        norm_status = str(status).strip().lower() if status else "active"
        if norm_status not in VALID_STATUSES:
            norm_status = "active"

        metadata = {
            "id": mid,
            "schema": SCHEMA_VERSION,
            "title": masked_title,
            "kind": norm_kind,
            "status": norm_status,
            "compiled_truth": bool(compiled_truth),
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
                _index_memory_file(mem_dir, conn, file_path)
            finally:
                conn.close()
        except Exception:
            pass

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
) -> Dict[str, Any]:
    """
    Search memories by keyword query, tag, or kind across local, global, or all scopes.
    Uses SQLite FTS5 index with fallback to disk scanning.
    """
    try:
        norm_scope = _VALID_SCOPES_MAP.get(str(scope).strip().lower(), "local")
        if namespace == "_global":
            norm_scope = "global"

        search_dirs: list[tuple[Path, str]] = []
        if norm_scope == "global":
            search_dirs.append((get_global_memory_dir(), "global"))
        elif norm_scope == "all":
            search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
            search_dirs.append((get_global_memory_dir(), "global"))
        else:
            search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))

        q_lower = query.strip().lower() if query else ""
        tag_lower = tag.strip().lower() if tag else ""
        kind_lower = kind.strip().lower() if kind else ""

        results = []
        seen_ids = set()

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

        for mem_dir, sc in search_dirs:
            if not mem_dir.exists() or not mem_dir.is_dir():
                continue
            for file_p in mem_dir.glob("*.md"):
                if not file_p.is_file():
                    continue
                try:
                    raw_text = file_p.read_text(encoding="utf-8", errors="replace")
                    meta, body = _parse_frontmatter(raw_text)
                    entry_id = meta.get("id") or file_p.stem
                    if entry_id in seen_ids:
                        continue
                    entry_title = meta.get("title") or entry_id
                    entry_kind = str(meta.get("kind") or "context").lower()
                    entry_tags = meta.get("tags") or []
                    entry_links = meta.get("links") or []
                    entry_ts = meta.get("timestamp") or ""
                    entry_status = str(meta.get("status") or "active").lower()
                    entry_compiled = bool(meta.get("compiled_truth"))

                    # Filter by kind
                    if kind_lower and entry_kind != kind_lower:
                        continue

                    # Filter by tag
                    if tag_lower:
                        tags_lower = [str(t).lower() for t in entry_tags]
                        if tag_lower not in tags_lower:
                            continue

                    # Filter by query (FTS match OR manual substring fallback)
                    if q_lower:
                        if entry_id not in fts_candidates:
                            searchable = f"{entry_title} {body} {' '.join(entry_tags)}".lower()
                            if q_lower not in searchable:
                                continue

                    seen_ids.add(entry_id)
                    preview = body[:200] + ("…" if len(body) > 200 else "")
                    path_str = (
                        f"~/.botex/memory/{file_p.name}"
                        if sc == "global"
                        else f".botex/memory/{file_p.name}"
                    )
                    results.append(
                        {
                            "id": entry_id,
                            "title": entry_title,
                            "kind": entry_kind,
                            "tags": entry_tags,
                            "links": entry_links,
                            "status": entry_status,
                            "compiled_truth": entry_compiled,
                            "timestamp": entry_ts,
                            "preview": preview,
                            "content": body,
                            "path": path_str,
                            "scope": sc,
                        }
                    )
                except Exception:
                    continue

        # Sort: if query was FTS matched, order by FTS rank; otherwise newest first
        if fts_candidates:
            results.sort(
                key=lambda r: (
                    fts_candidates.get(r["id"], 9999),
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
            if not mem_dir.exists():
                continue
            try:
                cand_file = resolve_safe_path(mem_dir, f"{mid}.md")
                if cand_file.exists() and cand_file.is_file():
                    found_path = cand_file
                    found_scope = sc
                    break
            except SecurityError:
                continue

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
            if not mem_dir.exists():
                continue
            try:
                cand_file = resolve_safe_path(mem_dir, f"{mid}.md")
                if cand_file.exists() and cand_file.is_file():
                    found_path = cand_file
                    found_dir = mem_dir
                    found_scope = sc
                    break
            except SecurityError:
                continue

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
) -> Dict[str, Any]:
    """List memory entries for the given workspace and scope."""
    return search_memory(
        workspace_dir=workspace_dir,
        query="",
        tag=tag,
        kind=kind,
        scope=scope,
        namespace=namespace,
    )


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
    if norm_scope == "global":
        search_dirs.append((get_global_memory_dir(), "global"))
    elif norm_scope == "all":
        search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
        search_dirs.append((get_global_memory_dir(), "global"))
    else:
        search_dirs.append((Path(workspace_dir).resolve() / ".botex" / "memory", "local"))
    return search_dirs


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
        search_dirs = _resolve_scope_dirs(workspace_dir, scope)
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

    search_dirs = _resolve_scope_dirs(workspace_dir, scope)
    file_path: Optional[Path] = None
    for mem_dir, _ in search_dirs:
        try:
            cand = resolve_safe_path(mem_dir, f"{clean_id}.md")
            if cand.exists() and cand.is_file():
                file_path = cand
                break
        except SecurityError:
            continue

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
        except Exception:
            pass

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


def find_graph_health(
    workspace_dir: str = ".",
    scope: str = "local",
) -> Dict[str, Any]:
    """
    Audits Memory Vault graph integrity:
    1. Broken Links: links pointing to memory_ids that do not exist.
    2. Orphan Notes: notes with zero incoming and zero outgoing links.
    3. Cycles: detects circular references in graph links via iterative DFS.
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
                    "SELECT id, title, kind, tags, status, compiled_truth FROM memories_meta;"
                ).fetchall()
                for r in rows:
                    mid = r["id"]
                    all_ids.add(mid)
                    node_metadata[mid] = {
                        "id": mid,
                        "title": r["title"] or mid,
                        "kind": r["kind"] or "context",
                        "status": r["status"] or "active",
                        "compiled_truth": bool(r["compiled_truth"]),
                        "scope": sc,
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

        orphans = [
            node_metadata[nid]
            for nid in all_ids
            if in_deg.get(nid, 0) == 0 and out_deg.get(nid, 0) == 0
        ]

        # Cycle detection via iterative DFS (O(V+E), no recursion limit)
        adj: Dict[str, List[str]] = {nid: [] for nid in all_ids}
        for e in edges:
            if e["target"] in all_ids:
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

        is_healthy = len(broken_links) == 0 and len(cycles) == 0

        return {
            "ok": True,
            "total_nodes": len(all_ids),
            "total_edges": len(edges),
            "broken_links": broken_links,
            "broken_count": len(broken_links),
            "orphans": orphans,
            "orphan_count": len(orphans),
            "cycles": cycles,
            "cycle_count": len(cycles),
            "is_healthy": is_healthy,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Graph health check failed: {exc}"}

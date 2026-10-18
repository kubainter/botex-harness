# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Workspace Memory Vault
============================
Workspace-scoped, persistent, and portable memory storage for MCP clients.
Supports context notes, architectural decisions, task handoffs, and lessons learned.
Format: Markdown with YAML frontmatter (compatible with ecc.memory.v1).
Location: <workspace_dir>/.botex/memory/
"""

import json
import os
import re
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .security import (
        SecurityError,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        sever_taint_numeric,
        wrap_untrusted_content,
    )
except (ImportError, ValueError):
    from security import (
        SecurityError,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        sever_taint_numeric,
        wrap_untrusted_content,
    )

__all__ = [
    "save_memory",
    "search_memory",
    "read_memory",
    "list_memories",
    "purge_ephemeral_memories",
    "get_global_memory_dir",
    "VALID_KINDS",
    "VALID_SCOPES",
    "MAX_MEMORY_BYTES",
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

MEMORY_DIR_NAME = ".botex/memory"
SCHEMA_VERSION = "ecc.memory.v1"
MAX_MEMORY_BYTES = 1_048_576  # 1 MB cap


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
    return sever_taint_numeric(clean)


def _serialize_val(val: Any) -> str:
    """Serialize scalar value safely for YAML frontmatter."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).replace("\r", "").replace("\n", " ")
    # Quote strings that contain special characters or might be misinterpreted
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
    """Parse a scalar value from frontmatter line."""
    val = raw.strip()
    if not val:
        return ""
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

        # Check for list item
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
                # Start of a list
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
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Save a context, decision, handoff, or lesson entry to the Memory Vault.
    Supports local workspace scope and global shared scope.
    Masks secrets and ensures directory isolation.

    ``origin`` labels the writer; the legacy ``source=`` keyword is still accepted.
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

        metadata = {
            "id": mid,
            "schema": SCHEMA_VERSION,
            "title": masked_title,
            "kind": norm_kind,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tags": clean_tags,
            "source": sanitize_for_storage(str(origin)),
            "scope": norm_scope,
            "namespace": "_global" if norm_scope == "global" else "workspace",
        }

        # Final barrier: the serialized document is sanitized again before the file sink.
        full_text = sanitize_for_storage(_serialize_frontmatter(metadata, masked_content))
        # codeql[py/clear-text-storage-sensitive-data]
        file_path.write_text(
            full_text, encoding="utf-8"
        )  # lgtm[py/clear-text-storage-sensitive-data]

        return {
            "ok": True,
            "id": mid,
            "path": rel_path,
            "title": masked_title,
            "kind": norm_kind,
            "tags": clean_tags,
            "scope": norm_scope,
        }
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {sever_taint_numeric(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to save memory: {sever_taint_numeric(mask_secrets(str(exc)))}",
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
                    entry_ts = meta.get("timestamp") or ""

                    # Filter by kind
                    if kind_lower and entry_kind != kind_lower:
                        continue

                    # Filter by tag
                    if tag_lower:
                        tags_lower = [str(t).lower() for t in entry_tags]
                        if tag_lower not in tags_lower:
                            continue

                    # Filter by query
                    if q_lower:
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
                            "timestamp": entry_ts,
                            "preview": preview,
                            "path": path_str,
                            "scope": sc,
                        }
                    )
                except Exception:
                    continue

        # Sort newest first
        results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return {"ok": True, "count": len(results), "memories": results}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to search memory: {sever_taint_numeric(mask_secrets(str(exc)))}",
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
            "error": f"Security violation: {sever_taint_numeric(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to read memory: {sever_taint_numeric(mask_secrets(str(exc)))}",
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
    except Exception:
        return deleted
    return deleted

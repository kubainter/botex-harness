# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX File Tools
================
Outline-first I/O tools for the autonomous code agent.
Optimized for maximum token savings and precise targeted editing.
"""
import ast
import re
import shutil
from pathlib import Path
from typing import Dict, Any, List

try:
    from .security import resolve_safe_path, load_gitignore_patterns, is_gitignored, is_safe_dir, mask_secrets, SecurityError
    from .patch_engine import validate_syntax, snapshot_manager
except (ImportError, ValueError):
    from security import resolve_safe_path, load_gitignore_patterns, is_gitignored, is_safe_dir, mask_secrets, SecurityError
    from patch_engine import validate_syntax, snapshot_manager

# ---------------------------------------------------------------------------
# Outline Engine (Token Saver)
# ---------------------------------------------------------------------------
def _get_python_outline(content: str, safe_path: Path) -> List[Dict[str, Any]]:
    outline = []
    try:
        tree = ast.parse(content, filename=safe_path.name)
    except Exception:
        # Fallback to regex if ast parse fails
        return _get_regex_outline(content)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            outline.append({
                "type": "class",
                "name": node.name,
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "methods": [
                    {
                        "type": "method",
                        "name": m.name,
                        "start_line": m.lineno,
                        "end_line": getattr(m, "end_lineno", m.lineno),
                    }
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            outline.append({
                "type": "function",
                "name": node.name,
                "start_line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
            })
    return outline


def _get_regex_outline(content: str) -> List[Dict[str, Any]]:
    outline = []
    lines = content.splitlines()

    class_pat = re.compile(r'^(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_]+)')
    fn_pat = re.compile(
        r'^(?:\s*(?:public|private|protected|static|async|function|def|fn|func)\s+)+([A-Za-z0-9_]+)\s*\('
    )
    arrow_pat = re.compile(r'^(?:\s*(?:const|let|var)\s+)+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>')
    header_pat = re.compile(r'^(#{1,4})\s+(.+)')

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("#"):
            # Check for markdown header
            m_h = header_pat.match(stripped)
            if m_h:
                outline.append({
                    "type": f"header_h{len(m_h.group(1))}",
                    "name": m_h.group(2).strip(),
                    "line": idx,
                })
            continue

        m_cls = class_pat.match(stripped)
        if m_cls:
            outline.append({"type": "class", "name": m_cls.group(1), "line": idx})
            continue

        m_fn = fn_pat.match(stripped)
        if m_fn:
            outline.append({"type": "function", "name": m_fn.group(1), "line": idx})
            continue

        m_arrow = arrow_pat.match(stripped)
        if m_arrow:
            outline.append({"type": "arrow_fn", "name": m_arrow.group(1), "line": idx})
            continue

    return outline


def get_file_outline(path: str, workspace_root: str | Path) -> Dict[str, Any]:
    """
    Returns only class, method, function signatures and line numbers.
    Saves ~90% tokens compared to reading the full file.
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"File '{path}' does not exist."}

    if not safe_path.is_file():
        return {"ok": False, "error": f"'{path}' is a directory."}

    try:
        content = safe_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Failed to read file: {e}"}

    total_lines = len(content.splitlines())
    ext = safe_path.suffix.lower()

    if ext in (".py", ".pyw"):
        outline = _get_python_outline(content, safe_path)
    else:
        outline = _get_regex_outline(content)

    return {
        "ok": True,
        "path": path,
        "total_lines": total_lines,
        "outline": outline,
        "note": "Use read_file_lines(path, start, end) to inspect specific blocks identified in the outline."
    }


# ---------------------------------------------------------------------------
# Targeted Reading
# ---------------------------------------------------------------------------
def read_file_lines(path: str, start: int, end: int, workspace_root: str | Path) -> Dict[str, Any]:
    """
    Read a slice of a file (1-indexed, inclusive).
    Each returned line is prefixed with its line number (e.g. '120: def foo():').
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"File '{path}' does not exist."}

    try:
        content = safe_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Failed to read file: {e}"}

    try:
        start = int(start)
        end = int(end)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"start/end must be integers (got start={start!r}, end={end!r})."}

    if start < 1 or end < 1:
        return {"ok": False, "error": "start and end must be >= 1 (lines are 1-indexed)."}
    if start > end:
        return {"ok": False, "error": f"start ({start}) is greater than end ({end}) — swap the range."}

    lines = content.splitlines()
    total = len(lines)

    start_idx = start
    end_idx = min(total, end)

    if start_idx > total:
        return {
            "ok": False,
            "error": (
                f"start line {start} is beyond file end (total lines: {total}). "
                f"The file ends at line {total} — there is no more content to read."
            )
        }

    selected = lines[start_idx - 1 : end_idx]
    numbered = [f"{start_idx + i}: {line}" for i, line in enumerate(selected)]

    body = mask_secrets("\n".join(numbered))
    return {
        "ok": True,
        "path": path,
        "start": start_idx,
        "end": end_idx,
        "total_file_lines": total,
        "content": body
    }


def read_file(path: str, workspace_root: str | Path) -> Dict[str, Any]:
    """
    Read the entire file. Recommended for small files (<150 lines).
    For larger files, prefer get_file_outline followed by read_file_lines.
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"File '{path}' does not exist."}

    try:
        content = safe_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Failed to read file: {e}"}

    lines = content.splitlines()
    if len(lines) > 250:
        return {
            "ok": False,
            "error": (
                f"File '{path}' has {len(lines)} lines (>250). "
                "Reading full large files wastes quota. "
                "Please call get_file_outline(path) and then read_file_lines(path, start, end)."
            )
        }

    numbered = [f"{i + 1}: {line}" for i, line in enumerate(lines)]
    return {
        "ok": True,
        "path": path,
        "total_lines": len(lines),
        "content": mask_secrets("\n".join(numbered))
    }


# ---------------------------------------------------------------------------
# File Creation & Directory Listing
# ---------------------------------------------------------------------------
def create_file(path: str, content: str, workspace_root: str | Path, task_id: str = "default") -> Dict[str, Any]:
    """
    Create a new file with in-memory pre-write syntax validation.
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if safe_path.is_dir():
        return {"ok": False, "error": f"'{path}' is an existing directory — cannot create a file there."}

    # Validate syntax before writing
    is_valid, syntax_err = validate_syntax(content, safe_path)
    if not is_valid:
        return {
            "ok": False,
            "error": f"File creation rejected: {syntax_err}. File was not written to disk."
        }

    try:
        with snapshot_manager.mutation_lock():
            if safe_path.exists():
                snapshot_manager.snapshot_file(task_id, safe_path, workspace_root)
            else:
                snapshot_manager.record_created_file(task_id, safe_path, workspace_root)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_text(content, encoding="utf-8")
            snapshot_manager.record_expected_state(task_id, safe_path)
            lines_count = len(content.splitlines())
            return {
                "ok": True,
                "path": path,
                "lines_written": lines_count,
                "message": f"File '{path}' created successfully ({lines_count} lines)."
            }
    except Exception as e:
        return {"ok": False, "error": f"Failed to create file: {e}"}


# ---------------------------------------------------------------------------
# Destructive Operations (delete / move — gated by engine authorization)
# ---------------------------------------------------------------------------
def delete_file(path: str, workspace_root: str | Path, task_id: str = "default") -> Dict[str, Any]:
    """
    Delete a file. Snapshots it first so rollback_task() can restore it.
    Only individual files — directories are refused.
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"File '{path}' does not exist."}
    if not safe_path.is_file():
        return {"ok": False, "error": f"'{path}' is a directory — only files can be deleted."}

    try:
        with snapshot_manager.mutation_lock():
            snapshot_manager.snapshot_file(task_id, safe_path, workspace_root)
            safe_path.unlink()
            snapshot_manager.record_expected_state(task_id, safe_path)
            return {
                "ok": True,
                "path": path,
                "message": f"File '{path}' deleted (snapshot retained for rollback)."
            }
    except Exception as e:
        return {"ok": False, "error": f"Failed to delete file: {e}"}


def move_file(src_path: str, dst_path: str, workspace_root: str | Path, task_id: str = "default") -> Dict[str, Any]:
    """
    Move/rename a file within the workspace. Snapshots both endpoints first.
    """
    try:
        safe_src = resolve_safe_path(workspace_root, src_path)
        safe_dst = resolve_safe_path(workspace_root, dst_path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_src.exists() or not safe_src.is_file():
        return {"ok": False, "error": f"Source '{src_path}' does not exist or is not a file."}
    if safe_dst.is_dir():
        return {"ok": False, "error": f"Destination '{dst_path}' is an existing directory."}
    if safe_src == safe_dst:
        return {"ok": False, "error": "Source and destination are the same file."}

    try:
        with snapshot_manager.mutation_lock():
            snapshot_manager.snapshot_file(task_id, safe_src, workspace_root)
            if safe_dst.exists():
                snapshot_manager.snapshot_file(task_id, safe_dst, workspace_root)
            # shutil.move refuses to overwrite on Windows but silently
            # replaces on POSIX — unlink first for consistent behavior.
                safe_dst.unlink()
            else:
                # The move creates dst — record it so rollback removes it;
                # otherwise rollback restores src but orphans the moved file.
                snapshot_manager.record_created_file(task_id, safe_dst, workspace_root)
            safe_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(safe_src), str(safe_dst))
            snapshot_manager.record_expected_state(task_id, safe_src)
            snapshot_manager.record_expected_state(task_id, safe_dst)
            return {
                "ok": True,
                "src": src_path,
                "dst": dst_path,
                "message": f"Moved '{src_path}' -> '{dst_path}' (snapshot retained for rollback)."
            }
    except Exception as e:
        return {"ok": False, "error": f"Failed to move file: {e}"}


def list_dir(path: str, workspace_root: str | Path, max_depth: int = 2) -> Dict[str, Any]:
    """
    List directories and files starting from path, respecting .gitignore and IGNORED_DIRS.
    """
    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"max_depth must be an integer (got {max_depth!r})."}
    if max_depth < 1:
        return {"ok": False, "error": "max_depth must be >= 1."}

    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"Path '{path}' does not exist."}

    root_path = Path(workspace_root).resolve()
    if safe_path.is_dir() and safe_path != root_path and not is_safe_dir(safe_path.name):
        return {"ok": False, "error": f"'{path}' is an ignored directory."}

    gi_patterns = load_gitignore_patterns(root_path)

    entries = []

    def _walk(curr: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for item in sorted(curr.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                name = item.name
                if not is_safe_dir(name):
                    continue
                try:
                    rel = str(item.relative_to(root_path)).replace("\\", "/")
                except Exception:
                    rel = item.name

                if is_gitignored(rel, gi_patterns):
                    continue

                if item.is_dir():
                    entries.append({"path": rel, "type": "dir"})
                    _walk(item, depth + 1)
                else:
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = None
                    entries.append({"path": rel, "type": "file", "size_bytes": size})
        except PermissionError:
            pass

    if safe_path.is_file():
        return {"ok": True, "path": path, "type": "file", "size_bytes": safe_path.stat().st_size}

    _walk(safe_path, 1)
    return {
        "ok": True,
        "base_path": path,
        "total_entries": len(entries),
        "truncated": len(entries) > 200,
        "entries": entries[:200]  # Cap at 200 items to avoid token overflow
    }

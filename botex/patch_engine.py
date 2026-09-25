# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Patch Engine
==================
Multi-tier Fuzzy Patching, In-Memory Syntax Validation, and Snapshot Rollback.
Ensures file modifications are resilient to CRLF/whitespace differences and
never corrupt source code on disk.
"""
import ast
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

try:
    from .security import resolve_safe_path, SecurityError
    from .config import snapshot_dir as _default_snapshot_dir
except (ImportError, ValueError):
    from security import resolve_safe_path, SecurityError
    from config import snapshot_dir as _default_snapshot_dir

# ---------------------------------------------------------------------------
# Snapshot Manager (Rollback & Anti-Bloat)
# ---------------------------------------------------------------------------
class SnapshotManager:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else _default_snapshot_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.active_task_ids: set[str] = set()

    def begin_task(self, task_id: str) -> None:
        self.active_task_ids.add(task_id)

    def end_task(self, task_id: str) -> None:
        self.active_task_ids.discard(task_id)

    def _task_dir(self, task_id: str) -> Path:
        safe_id = "".join(c for c in task_id if c.isalnum() or c in ("-", "_"))[:32]
        return self.base_dir / safe_id

    @staticmethod
    def _entry_name(file_path: Path) -> str:
        """Collision-free registry name for a tracked path.

        Hashing the full path avoids the old flattening collision where
        ``a/b`` and ``a_b`` mapped to the same backup file; the readable
        tail is kept only for debuggability."""
        digest = hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:16]
        tail = "".join(
            c if c.isalnum() or c in ("_", "-", ".") else "_"
            for c in file_path.name
        )[:40]
        return f"{digest}_{tail}"

    def snapshot_file(self, task_id: str, file_path: Path) -> Path:
        """Create a backup of a file before modifying it."""
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        entry = self._entry_name(file_path)
        dest = task_dir / entry
        if not dest.exists() and file_path.exists():
            shutil.copy2(file_path, dest)
            # Store metadata file mapping original path
            meta = task_dir / f"{entry}.meta"
            meta.write_text(str(file_path), encoding="utf-8")
        return dest

    def record_created_file(self, task_id: str, file_path: Path) -> None:
        """Record a newly-created file so rollback can remove it."""
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        marker = task_dir / (self._entry_name(file_path) + ".created")
        marker.write_text(str(file_path), encoding="utf-8")

    def rollback_task(self, task_id: str) -> List[str]:
        """Restore backups and remove files newly created by this task.

        A path recorded as created wins over a later snapshot: a file the
        task created and then deleted/moved must stay gone — restoring its
        post-creation snapshot would resurrect it. Per-entry failures are
        collected instead of aborting the restore; callers must confirm the
        outcome with verify_rollback()."""
        task_dir = self._task_dir(task_id)
        if not task_dir.exists():
            return []
        restored = []
        created_paths = set()
        for marker in task_dir.glob("*.created"):
            try:
                created_path = Path(marker.read_text(encoding="utf-8").strip())
                created_paths.add(created_path)
                if created_path.is_file():
                    created_path.unlink()
                    restored.append(str(created_path))
            except (OSError, ValueError):
                pass
        for meta_file in task_dir.glob("*.meta"):
            try:
                orig_path = Path(meta_file.read_text(encoding="utf-8").strip())
                backup_file = task_dir / meta_file.stem
                if orig_path in created_paths or not backup_file.exists():
                    continue
                orig_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, orig_path)
                restored.append(str(orig_path))
            except (OSError, ValueError):
                pass
        return restored

    def mutated_paths(self, task_id: str) -> List[Path]:
        """Ground-truth mutation registry: every path this task snapshotted
        or recorded as created — the set rollback is responsible for.
        Authoritative over files_touched, which only tracks caller-visible
        tool writes (it misses exec side effects and move destinations)."""
        task_dir = self._task_dir(task_id)
        if not task_dir.exists():
            return []
        paths: List[Path] = []
        for marker in list(task_dir.glob("*.created")) + list(task_dir.glob("*.meta")):
            try:
                paths.append(Path(marker.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                pass
        return paths

    def verify_rollback(self, task_id: str, expect_entries: bool = False) -> List[str]:
        """Return mutated paths whose rollback is NOT confirmed: a created
        file that still exists, or a snapshotted file whose on-disk content
        does not match its backup. An empty list means the workspace was
        verifiably restored.

        ``expect_entries=True`` fails closed when the registry is missing
        entirely — a pruned/lost snapshot dir must not be mistaken for
        "nothing was mutated"."""
        task_dir = self._task_dir(task_id)
        if not task_dir.exists():
            return ["<snapshot registry missing>"] if expect_entries else []
        unconfirmed: List[str] = []
        created_paths = set()
        for marker in task_dir.glob("*.created"):
            try:
                p = Path(marker.read_text(encoding="utf-8").strip())
                created_paths.add(p)
            except (OSError, ValueError):
                continue
            if p.exists():
                unconfirmed.append(str(p))
        for meta in task_dir.glob("*.meta"):
            try:
                orig = Path(meta.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if orig in created_paths:
                continue
            backup = task_dir / meta.stem
            try:
                if (not orig.exists() or not backup.exists()
                        or orig.read_bytes() != backup.read_bytes()):
                    unconfirmed.append(str(orig))
            except OSError:
                unconfirmed.append(str(orig))
        return unconfirmed

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """List all stored snapshots with size and file counts."""
        snapshots = []
        if not self.base_dir.exists():
            return []
        for item in self.base_dir.iterdir():
            if item.is_dir():
                files = list(item.glob("*.meta")) + list(item.glob("*.created"))
                total_bytes = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                mtime = item.stat().st_mtime
                snapshots.append({
                    "task_id": item.name,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                    "files_count": len(files),
                    "size_kb": round(total_bytes / 1024, 2),
                    "age_days": round((time.time() - mtime) / 86400, 1),
                })
        snapshots.sort(key=lambda s: s["created_at"], reverse=True)
        return snapshots

    def clean_old_snapshots(
        self, max_age_days: float = 7.0, max_total_mb: float = 50.0,
        protected_task_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Prune old/large snapshots, never removing protected in-flight tasks."""
        protected_task_ids = protected_task_ids or set()
        if not self.base_dir.exists():
            return {"deleted_dirs": 0, "freed_mb": 0.0}

        deleted_dirs = 0
        freed_bytes = 0
        now = time.time()

        # Step 1: Remove by age
        for item in list(self.base_dir.iterdir()):
            if item.is_dir():
                if item.name in protected_task_ids:
                    continue
                mtime = item.stat().st_mtime
                if (now - mtime) > (max_age_days * 86400):
                    size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    shutil.rmtree(item, ignore_errors=True)
                    deleted_dirs += 1
                    freed_bytes += size

        # Step 2: If still over quota, remove oldest first
        snapshots = self.list_snapshots()
        total_bytes = sum(s["size_kb"] * 1024 for s in snapshots)
        if total_bytes > (max_total_mb * 1024 * 1024):
            # Sort oldest first
            snapshots.sort(key=lambda s: s["created_at"])
            for s in snapshots:
                if s["task_id"] in protected_task_ids:
                    continue
                if total_bytes <= (max_total_mb * 1024 * 1024):
                    break
                s_dir = self.base_dir / s["task_id"]
                if s_dir.exists():
                    size = sum(f.stat().st_size for f in s_dir.rglob("*") if f.is_file())
                    shutil.rmtree(s_dir, ignore_errors=True)
                    deleted_dirs += 1
                    freed_bytes += size
                    total_bytes -= size

        return {
            "deleted_dirs": deleted_dirs,
            "freed_mb": round(freed_bytes / (1024 * 1024), 2),
            "remaining_mb": round(sum(s["size_kb"] for s in self.list_snapshots()) / 1024, 2)
        }


snapshot_manager = SnapshotManager()

# ---------------------------------------------------------------------------
# Pre-write Syntax Validation
# ---------------------------------------------------------------------------
def validate_syntax(content: str, file_path: Path) -> Tuple[bool, Optional[str]]:
    """
    Validate code syntax in memory before touching disk.
    Returns (True, None) if syntax is valid or no linter applies.
    Returns (False, error_message) if syntax is invalid.
    """
    ext = file_path.suffix.lower()

    # 1. Python AST validation
    if ext in (".py", ".pyw"):
        try:
            ast.parse(content, filename=file_path.name)
            return True, None
        except SyntaxError as e:
            return False, f"Python SyntaxError at line {e.lineno}, col {e.offset}: {e.msg}"
        except Exception as e:
            return False, f"Python parse error: {str(e)}"

    # 2. JSON validation
    if ext == ".json":
        try:
            json.loads(content)
            return True, None
        except json.JSONDecodeError as e:
            return False, f"JSON syntax error at line {e.lineno}, col {e.colno}: {e.msg}"

    # 3. PHP syntax check (if php CLI installed)
    if ext in (".php", ".phtml"):
        php_bin = shutil.which("php")
        if php_bin:
            try:
                proc = subprocess.run(
                    [php_bin, "-l"],
                    input=content,
                    text=True,
                    capture_output=True,
                    timeout=5
                )
                if proc.returncode != 0:
                    first_err = proc.stdout.splitlines()[0] if proc.stdout else proc.stderr
                    return False, f"PHP Lint error: {first_err.strip()}"
            except Exception:
                pass  # Do not block if linter fails to execute
        return True, None

    # 4. Node.js check (for JS/TS if node CLI installed)
    if ext in (".js", ".mjs"):
        node_bin = shutil.which("node")
        if node_bin:
            try:
                proc = subprocess.run(
                    [node_bin, "--check"],
                    input=content,
                    text=True,
                    capture_output=True,
                    timeout=5
                )
                if proc.returncode != 0:
                    return False, f"JavaScript SyntaxError: {proc.stderr.strip()}"
            except Exception:
                pass
        return True, None

    # Unknown or markup files (.md, .txt, .html, .css) are assumed safe
    return True, None


# ---------------------------------------------------------------------------
# Multi-tier Fuzzy Matcher
# ---------------------------------------------------------------------------
def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_trailing_spaces(text: str) -> str:
    return "\n".join(line.rstrip() for line in _normalize_newlines(text).splitlines())


def apply_fuzzy_patch(
    original: str,
    search_block: str,
    replace_block: str
) -> Tuple[bool, str, int]:
    """
    Attempts to replace search_block with replace_block in original using 3 levels:
    - Level 1: Exact match
    - Level 2: Newline (\r\n vs \n) and trailing-space normalization
    - Level 3: Leading-whitespace / indentation-tolerant block matching

    Returns: (success: bool, new_content: str, level_used: int)
    """
    if not search_block:
        return False, original, 0

    # Detect original newline style
    crlf_mode = "\r\n" in original

    # --- LEVEL 1: Exact Match ---
    if search_block in original:
        count = original.count(search_block)
        if count == 1:
            new_text = original.replace(search_block, replace_block, 1)
            return True, new_text, 1

    # --- LEVEL 2: Normalized Newlines & Trailing Whitespace ---
    norm_orig = _strip_trailing_spaces(original)
    norm_search = _strip_trailing_spaces(search_block)
    norm_replace = _normalize_newlines(replace_block)

    if norm_search in norm_orig:
        count = norm_orig.count(norm_search)
        if count == 1:
            # Map back onto original lines
            orig_lines = _normalize_newlines(original).splitlines()
            search_lines = norm_search.splitlines()
            replace_lines = norm_replace.splitlines()

            # Find matching line index
            for i in range(len(orig_lines) - len(search_lines) + 1):
                window = [line.rstrip() for line in orig_lines[i : i + len(search_lines)]]
                if window == search_lines:
                    new_lines = orig_lines[:i] + replace_lines + orig_lines[i + len(search_lines):]
                    out = "\n".join(new_lines)
                    if crlf_mode:
                        out = out.replace("\n", "\r\n")
                    return True, out, 2

    # --- LEVEL 3: Indentation-Agnostic Block Matching ---
    search_lines_stripped = [l.strip() for l in norm_search.splitlines() if l.strip()]
    if search_lines_stripped:
        orig_lines = _normalize_newlines(original).splitlines()
        first_target = search_lines_stripped[0]

        for i, line in enumerate(orig_lines):
            if line.strip() == first_target:
                # Potential match: check if subsequent non-empty lines match
                matched = True
                curr_orig_idx = i
                matching_end_idx = i

                for s_line in search_lines_stripped[1:]:
                    curr_orig_idx += 1
                    # Skip empty lines in original
                    while curr_orig_idx < len(orig_lines) and not orig_lines[curr_orig_idx].strip():
                        curr_orig_idx += 1
                    if curr_orig_idx >= len(orig_lines) or orig_lines[curr_orig_idx].strip() != s_line:
                        matched = False
                        break
                    matching_end_idx = curr_orig_idx

                if matched:
                    # Detect indentation from the first matched line
                    lead_indent = line[: len(line) - len(line.lstrip())]
                    replace_lines = norm_replace.splitlines()
                    
                    # Adapt indentation of replacement lines if replacement is unindented
                    adapted_replace = []
                    for r_line in replace_lines:
                        if r_line.strip():
                            # If replacement doesn't have indent, give it the target indent
                            if not r_line.startswith(" ") and not r_line.startswith("\t"):
                                adapted_replace.append(lead_indent + r_line)
                            else:
                                adapted_replace.append(r_line)
                        else:
                            adapted_replace.append("")

                    new_lines = orig_lines[:i] + adapted_replace + orig_lines[matching_end_idx + 1:]
                    out = "\n".join(new_lines)
                    if crlf_mode:
                        out = out.replace("\n", "\r\n")
                    return True, out, 3

    return False, original, 0


# ---------------------------------------------------------------------------
# High-level apply_patch function
# ---------------------------------------------------------------------------
def apply_patch(
    path: str,
    search_block: str,
    replace_block: str,
    workspace_root: str | Path,
    task_id: str = "default"
) -> Dict[str, Any]:
    """
    Safely applies a search-and-replace patch to a file.
    Includes security checks, snapshotting, fuzzy matching, and pre-write syntax validation.
    """
    try:
        safe_path = resolve_safe_path(workspace_root, path)
    except SecurityError as se:
        return {"ok": False, "error": str(se)}

    if not safe_path.exists():
        return {"ok": False, "error": f"File '{path}' does not exist."}

    if not safe_path.is_file():
        return {"ok": False, "error": f"'{path}' is a directory, not a file."}

    try:
        content = safe_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Failed to read file: {e}"}

    # Backup before any modification
    snapshot_manager.snapshot_file(task_id, safe_path)

    # Apply fuzzy patch
    success, new_content, level = apply_fuzzy_patch(content, search_block, replace_block)
    if not success:
        return {
            "ok": False,
            "error": f"Search block could not be located in '{path}'. Make sure the search block matches existing code."
        }

    # Pre-write syntax check on new_content
    is_valid, syntax_err = validate_syntax(new_content, safe_path)
    if not is_valid:
        return {
            "ok": False,
            "error": (
                f"Patch rejected: {syntax_err}. "
                "The file was NOT modified. Please correct the code syntax in your patch."
            )
        }

    # Write to disk
    try:
        safe_path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Failed to write file: {e}"}

    # Line stats
    orig_lines = content.splitlines()
    new_lines = new_content.splitlines()
    lines_added = max(0, len(new_lines) - len(orig_lines))
    lines_removed = max(0, len(orig_lines) - len(new_lines))

    return {
        "ok": True,
        "level_used": level,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "message": f"Successfully patched '{path}' (Fuzzy level: L{level}, +{lines_added}/-{lines_removed} lines)."
    }

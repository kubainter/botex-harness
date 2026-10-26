# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Context-Guard
===================
Offloaded Payload + On-demand Inspection for large tool output.

Large command output is (1) secret-masked, (2) stored in full as a plain
``.botex/scratchpad/<memory_id>.log`` file — keeping ephemeral terminal noise
out of the persistent Memory Vault — and (3) replaced in the live context by
a compact, failure-aware preview that carries a scratchpad reference. The
full log is read on demand via ``read_scratchpad`` (or any file reader).

Slicing is *failure-aware*: failure blocks (pytest FAILURES, tracebacks,
assertion errors, ``FAILED`` summary lines) are prioritised over a rigid
head/tail cut, and long runs of green ``PASSED`` lines are collapsed.
All slicing operates on decoded text, never on raw bytes.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .config import load_config
    from .security import (
        SecurityError,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        sanitize_identifier,
        wrap_untrusted_content,
    )
except (ImportError, ValueError):
    from config import load_config
    from security import (
        SecurityError,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
        sanitize_identifier,
        wrap_untrusted_content,
    )

__all__ = [
    "smart_slice_output",
    "sanitize_and_vault_output",
    "read_scratchpad",
    "compact_verification_summary",
    "guard_settings",
    "DEFAULT_GUARD",
    "SCRATCHPAD_DIR_NAME",
    "MAX_SCRATCHPAD_BYTES",
]

DEFAULT_GUARD: Dict[str, Any] = {
    "enabled": True,
    "threshold_bytes": 2048,
    "max_preview_bytes": 3072,
    "auto_vault_logs": True,
    "vault_retention_days": 3,
}

SCRATCHPAD_DIR_NAME = ".botex/scratchpad"
MAX_SCRATCHPAD_BYTES = 1_048_576  # 1 MB cap, same bound as the Memory Vault

_POINTER_FAIL = (
    "[BoteX Context-Guard: Output exceeded threshold. Key failure tracebacks "
    "preserved below. Full unredacted log ({size_kb} KB) offloaded to "
    "scratchpad '.botex/scratchpad/{memory_id}.log'. Use "
    "read_scratchpad(memory_id='{memory_id}') to inspect.]"
)
_POINTER_PLAIN = (
    "[BoteX Context-Guard: Output exceeded threshold. Head and tail preserved "
    "below. Full unredacted log ({size_kb} KB) offloaded to scratchpad "
    "'.botex/scratchpad/{memory_id}.log'. Use "
    "read_scratchpad(memory_id='{memory_id}') to inspect.]"
)
_POINTER_NOVAULT = (
    "[BoteX Context-Guard: Output exceeded threshold ({size_kb} KB); preview "
    "below is truncated and no Vault copy is available.]"
)

# Failure markers (pytest / python / linters / compilers).
_FAILURES_HEADER_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+\s*$")
_SUMMARY_HEADER_RE = re.compile(r"^=+ short test summary info =+\s*$", re.IGNORECASE)
_MARKER_RE = re.compile(
    r"(^=+ (FAILURES|ERRORS) =+|^FAILED\b|^ERROR\b|Traceback \(most recent call last\)"
    r"|AssertionError|\bError:)"
)
_PASSED_RE = re.compile(r"(\bPASSED\b|^\[PASS\])")


def guard_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve effective context_guard settings (defaults < config < override)."""
    merged = dict(DEFAULT_GUARD)
    try:
        base = load_config().get("context_guard", {})
        if isinstance(base, dict):
            merged.update(base)
    except Exception:
        pass
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def _clip_bytes(text: str, max_bytes: int) -> str:
    """Clip *decoded* text to max_bytes of UTF-8 without splitting a character."""
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _size(lines: List[str]) -> int:
    return sum(len(ln.encode("utf-8")) + 1 for ln in lines)


def _collapse_passed(lines: List[str]) -> List[str]:
    """Collapse runs (>2) of PASSED lines into one marker line."""
    out: List[str] = []
    run: List[str] = []

    def flush() -> None:
        if len(run) > 2:
            out.append(f"[... {len(run)} PASSED lines collapsed ...]")
        else:
            out.extend(run)
        run.clear()

    for ln in lines:
        if _PASSED_RE.search(ln) and not _MARKER_RE.search(ln):
            run.append(ln)
        else:
            flush()
            out.append(ln)
    flush()
    return out


def _fit_edges(lines: List[str], budget: int) -> List[str]:
    """Fit lines into budget bytes keeping the first ~40% and last ~60%."""
    if _size(lines) <= budget:
        return lines
    omit = "[... lines omitted ...]"
    avail = max(budget - len(omit) - 1, 0)
    head_b, tail_b = int(avail * 0.4), avail - int(avail * 0.4)
    head: List[str] = []
    used = 0
    for ln in lines:
        n = len(ln.encode("utf-8")) + 1
        if used + n > head_b:
            break
        head.append(ln)
        used += n
    tail: List[str] = []
    used = 0
    for ln in reversed(lines[len(head) :]):
        n = len(ln.encode("utf-8")) + 1
        if used + n > tail_b:
            break
        tail.append(ln)
        used += n
    tail.reverse()
    return head + [omit] + tail


def _failure_start(lines: List[str]) -> Optional[int]:
    """Index where the failure block begins, or None when no failure found."""
    for i, ln in enumerate(lines):
        if _FAILURES_HEADER_RE.match(ln):
            return i
    for i, ln in enumerate(lines):
        if _SUMMARY_HEADER_RE.match(ln):
            return i
    for i, ln in enumerate(lines):
        if _MARKER_RE.search(ln):
            return max(i - 2, 0)
    return None


def smart_slice_output(
    text: str,
    head_lines: int = 20,
    tail_lines: int = 40,
    max_preview_bytes: int = 3072,
    memory_id: Optional[str] = None,
    original_bytes: Optional[int] = None,
) -> Tuple[str, bool]:
    """Failure-aware slice of ``text``. Returns ``(preview, was_truncated)``.

    Text that already fits ``max_preview_bytes`` is returned unchanged. When a
    failure block is detected it is kept whole (up to the byte budget) in
    preference to a rigid head/tail cut; green PASSED runs are collapsed.
    """
    raw_size = original_bytes if original_bytes is not None else len(text.encode("utf-8"))
    if len(text.encode("utf-8")) <= max_preview_bytes:
        return text, False

    lines = _collapse_passed(text.splitlines())
    fail_idx = _failure_start(lines)
    size_kb = f"{raw_size / 1024:.1f}"
    if memory_id:
        tpl = _POINTER_FAIL if fail_idx is not None else _POINTER_PLAIN
        pointer = tpl.format(size_kb=size_kb, memory_id=memory_id)
    else:
        pointer = _POINTER_NOVAULT.format(size_kb=size_kb)

    budget = max(max_preview_bytes - len(pointer.encode("utf-8")) - 64, 256)
    if fail_idx is not None:
        head = lines[: min(head_lines, fail_idx)]
        head_budget = budget // 5
        if _size(head) > head_budget:
            head = _fit_edges(head, head_budget)
        block = lines[fail_idx:]
        block = _fit_edges(block, budget - _size(head) - 40)
        gap = fail_idx - len(lines[: min(head_lines, fail_idx)])
        parts = head
        if gap > 0:
            parts = parts + [f"[... {gap} lines omitted ...]"]
        body = parts + block
    else:
        head = lines[:head_lines]
        tail = lines[-tail_lines:] if len(lines) > head_lines else []
        omitted = max(len(lines) - len(head) - len(tail), 0)
        mid = [f"[... {omitted} lines omitted ...]"] if omitted else []
        body = _fit_edges(head + mid + tail, budget)

    preview = pointer + "\n" + "\n".join(body)
    return _clip_bytes(preview, max_preview_bytes), True


def _ensure_scratchpad_dir(workspace_root: Path) -> Path:
    """Ensure <workspace_root>/.botex/scratchpad exists and .botex is gitignored."""
    pad_dir = workspace_root / ".botex" / "scratchpad"
    pad_dir.mkdir(parents=True, exist_ok=True)

    gi_path = workspace_root / ".botex" / ".gitignore"
    if not gi_path.exists():
        try:
            gi_path.write_text("# Auto-generated by BoteX\n*\n", encoding="utf-8")
        except Exception:
            pass

    return pad_dir


def _purge_scratchpad(workspace_root: Path, max_age_days: int) -> int:
    """Delete scratchpad ``*.log`` files older than ``max_age_days`` (mtime)."""
    deleted = 0
    try:
        pad_dir = workspace_root / ".botex" / "scratchpad"
        if not pad_dir.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(max_age_days), 0))
        for file_p in pad_dir.glob("*.log"):
            try:
                if not file_p.is_file():
                    continue
                ts = datetime.fromtimestamp(file_p.stat().st_mtime, tz=timezone.utc)
                if ts <= cutoff:
                    file_p.unlink()
                    deleted += 1
            except Exception:
                continue
    except Exception:
        return deleted
    return deleted


def sanitize_and_vault_output(
    output_text: str,
    source_label: str,
    cwd: str,
    task_id: Optional[str] = None,
    guard_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Mask, offload and slice large output. Returns ``(text, memory_id)``.

    Below ``threshold_bytes`` the (masked) text is returned unchanged with no
    offload. Above it, the full masked log is written to the workspace
    scratchpad at ``.botex/scratchpad/<memory_id>.log`` — keeping ephemeral
    terminal logs out of the persistent Memory Vault — and a failure-aware
    preview carrying the scratchpad id is returned.
    """
    cfg = guard_settings(guard_cfg)
    masked = mask_secrets(output_text or "")
    if not cfg.get("enabled", True):
        return masked, None
    full_size = len(masked.encode("utf-8"))
    if full_size <= int(cfg.get("threshold_bytes", 2048)):
        return masked, None

    memory_id: Optional[str] = None
    if cfg.get("auto_vault_logs", True):
        candidate = f"mem_exec_{uuid.uuid4().hex[:8]}"
        # Keep the scratchpad copy within a bounded size (decoded-safe clip).
        stored = _clip_bytes(masked, MAX_SCRATCHPAD_BYTES - 1024)
        try:
            root = Path(cwd).resolve()
            pad_dir = _ensure_scratchpad_dir(root)
            log_path = resolve_safe_path(pad_dir, f"{candidate}.log")
            log_path.write_text(sanitize_for_storage(stored), encoding="utf-8")
            memory_id = candidate
            retention = int(cfg.get("vault_retention_days", 3) or 0)
            if retention > 0:
                _purge_scratchpad(root, retention)
        except Exception:
            memory_id = None

    preview, _ = smart_slice_output(
        masked,
        max_preview_bytes=int(cfg.get("max_preview_bytes", 3072)),
        memory_id=memory_id,
        original_bytes=full_size,
    )
    return preview, memory_id


def read_scratchpad(memory_id: str, cwd: str = ".") -> Dict[str, Any]:
    """Read a full offloaded execution log from ``.botex/scratchpad/``.

    Returns a ``read_memory``-shaped dict: ``ok``, ``id``, ``path`` and
    ``content`` — the latter re-masked and wrapped in
    ``<untrusted_command_output>`` boundary tags for safe model consumption.
    """
    try:
        mid = sanitize_identifier(str(memory_id).strip())
        if not mid:
            return {"ok": False, "error": f"Invalid scratchpad id '{memory_id}'."}
        pad_dir = Path(cwd).resolve() / ".botex" / "scratchpad"
        try:
            log_path = resolve_safe_path(pad_dir, f"{mid}.log")
        except SecurityError as se:
            return {"ok": False, "error": f"Security violation: {se}"}
        if not log_path.is_file():
            return {"ok": False, "error": f"Scratchpad log '{mid}' not found."}
        body = log_path.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "id": mid,
            "path": f"{SCRATCHPAD_DIR_NAME}/{mid}.log",
            "content": wrap_untrusted_content(
                body, tag="untrusted_command_output", id=mid, source="scratchpad"
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read scratchpad: {mask_secrets(str(exc))}"}


def compact_verification_summary(verification: Dict[str, Any], max_bytes: int = 2048) -> str:
    """Compact failure summary of a verify_command result for the model."""
    if verification.get("timed_out"):
        reason = "timed out"
    elif verification.get("error"):
        reason = str(verification["error"])
    else:
        reason = f"exit code {verification.get('exit_code', '?')}"
    stdout = str(verification.get("stdout") or "")
    tail, _ = smart_slice_output(stdout, max_preview_bytes=max_bytes)
    if len(tail.encode("utf-8")) > max_bytes:
        tail = _clip_bytes(tail, max_bytes)
    mem_id = verification.get("vault_memory_id")
    lines = [f"Verification failure reason: {reason}."]
    if tail.strip():
        lines.append("Error output (tail):\n" + tail)
    if mem_id:
        lines.append(
            f"Full log in scratchpad '.botex/scratchpad/{mem_id}.log' "
            f"(read_scratchpad(memory_id='{mem_id}'))."
        )
    return "\n".join(lines)

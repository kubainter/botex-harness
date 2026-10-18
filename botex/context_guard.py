# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Context-Guard
===================
Offloaded Payload + On-demand Inspection for large tool output.

Large command output is (1) secret-masked, (2) stored in full in the Memory
Vault wrapped in ``<untrusted_command_output>`` boundary tags, and (3)
replaced in the live context by a compact, failure-aware preview that carries
a Vault reference. The full log is read on demand through the existing
``read_memory`` tool — no new MCP tool is introduced.

Slicing is *failure-aware*: failure blocks (pytest FAILURES, tracebacks,
assertion errors, ``FAILED`` summary lines) are prioritised over a rigid
head/tail cut, and long runs of green ``PASSED`` lines are collapsed.
All slicing operates on decoded text, never on raw bytes.
"""

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import memory as _memory
    from .config import load_config
    from .security import mask_secrets, wrap_untrusted_content
except (ImportError, ValueError):
    import memory as _memory
    from config import load_config
    from security import mask_secrets, wrap_untrusted_content

__all__ = [
    "smart_slice_output",
    "sanitize_and_vault_output",
    "compact_verification_summary",
    "guard_settings",
    "DEFAULT_GUARD",
]

DEFAULT_GUARD: Dict[str, Any] = {
    "enabled": True,
    "threshold_bytes": 2048,
    "max_preview_bytes": 3072,
    "auto_vault_logs": True,
    "vault_retention_days": 3,
}

_POINTER_FAIL = (
    "[BoteX Context-Guard: Output exceeded threshold. Key failure tracebacks "
    "preserved below. Full unredacted log ({size_kb} KB) offloaded to Memory "
    "Vault: '{memory_id}'. Use read_memory(memory_id='{memory_id}') to inspect.]"
)
_POINTER_PLAIN = (
    "[BoteX Context-Guard: Output exceeded threshold. Head and tail preserved "
    "below. Full unredacted log ({size_kb} KB) offloaded to Memory Vault: "
    "'{memory_id}'. Use read_memory(memory_id='{memory_id}') to inspect.]"
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


def sanitize_and_vault_output(
    output_text: str,
    source_label: str,
    cwd: str,
    task_id: Optional[str] = None,
    guard_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Mask, offload and slice large output. Returns ``(text, memory_id)``.

    Below ``threshold_bytes`` the (masked) text is returned unchanged with no
    Vault entry. Above it, the full masked log is stored in the Memory Vault
    (tags ``ephemeral`` + ``context_guard``) inside ``<untrusted_command_output>``
    tags, and a failure-aware preview carrying the Vault ID is returned.
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
        # Keep the vault copy within the Memory Vault cap (decoded-safe clip).
        stored = _clip_bytes(masked, _memory.MAX_MEMORY_BYTES - 1024)
        wrapped = wrap_untrusted_content(
            stored, tag="untrusted_command_output", source=source_label
        )
        tags = ["ephemeral", "context_guard"]
        if task_id:
            tags.append(f"task_{task_id}")
        saved = _memory.save_memory(
            workspace_dir=str(cwd),
            title="Context-Guard Exec Log",
            content=wrapped,
            kind="context",
            tags=tags,
            memory_id=candidate,
            origin=f"context_guard:{source_label}",
        )
        if saved.get("ok"):
            memory_id = saved["id"]
            retention = int(cfg.get("vault_retention_days", 3) or 0)
            if retention > 0:
                try:
                    _memory.purge_ephemeral_memories(str(cwd), max_age_days=retention)
                except Exception:
                    pass

    preview, _ = smart_slice_output(
        masked,
        max_preview_bytes=int(cfg.get("max_preview_bytes", 3072)),
        memory_id=memory_id,
        original_bytes=full_size,
    )
    return preview, memory_id


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
        lines.append(f"Full log in Memory Vault: '{mem_id}' (read_memory(memory_id='{mem_id}')).")
    return "\n".join(lines)

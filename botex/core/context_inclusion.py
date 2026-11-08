# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Context Inclusion Layer
=======================
Deterministic, 100% local context-inclusion primitives for multi-agent swarms.

This module contains NO network calls and NO model invocations. It transforms
raw agent payloads (Devin, ZCode, BoteX, ...) into a canonical turn model and
compresses noisy observations/traces/diffs so that swarm handoffs fit a strict
byte budget while preserving the signal that matters.

Components:
    * ``OutputNormalizer``      -> normalizes heterogeneous agent output to
                                   ``CanonicalAgentTurn``.
    * ``DeterministicSignalGate`` -> collapse passed tests, pinpoint failure
                                   tracebacks, prune diffs to an AST manifest,
                                   and de-duplicate observations (token Jaccard).
    * ``ContextKnapsack``       -> packs state into a byte budget and spills
                                   overflow to ``.botex/scratchpad/<id>.log``
                                   while injecting a compact pointer.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .state import CanonicalAgentTurn

__all__ = [
    "OutputNormalizer",
    "DeterministicSignalGate",
    "ContextKnapsack",
]


# =============================================================================
# Output Normalizer
# =============================================================================

_CONTENT_KEYS = ("content", "message", "output", "text", "response", "result", "completion")
_TOOL_CALL_KEYS = ("tool_calls", "toolCalls", "actions", "calls")
_STATUS_KEYS = ("status", "state", "finish_reason", "stop_reason")
_MUTATED_KEYS = ("mutated_files", "modified_files", "changed_files", "files")
_MEMORY_KEYS = ("shared_memory_keys", "memory_keys", "memory_notes")

# Per-agent key priority; fallback uses the generic lists above.
_AGENT_CONTENT_PRIORITY: Dict[str, Tuple[str, ...]] = {
    "devin": ("message", "output", "text", "content", "response"),
    "zcode": ("response", "result", "output", "content", "text"),
    "botex": ("content", "message", "output", "text", "response"),
}


def _first_present(payload: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    return str(value)


def _normalize_path(path: Any) -> str:
    """Coerce a path to a POSIX repo-relative string (never a Windows path)."""
    cleaned = _as_str(path).replace("\\", "/").strip()
    # Strip drive letters such as ``C:/...`` -> keep it repo-relative.
    cleaned = re.sub(r"^[A-Za-z]:/", "", cleaned)
    return cleaned


class OutputNormalizer:
    """Normalize heterogeneous agent payloads into ``CanonicalAgentTurn``."""

    def normalize(self, raw_payload: Any, agent_type: str = "") -> CanonicalAgentTurn:
        agent = (agent_type or "").strip().lower()
        if isinstance(raw_payload, dict):
            return self._from_mapping(raw_payload, agent)
        return self._from_text(raw_payload, agent)

    # -- helpers -------------------------------------------------------------
    def _from_text(self, raw_payload: Any, agent: str) -> CanonicalAgentTurn:
        return CanonicalAgentTurn(
            agent_type=agent or "unknown",
            role="assistant",
            content=_as_str(raw_payload),
        )

    def _from_mapping(self, payload: Dict[str, Any], agent: str) -> CanonicalAgentTurn:
        priority = _AGENT_CONTENT_PRIORITY.get(agent, _CONTENT_KEYS)
        content = _as_str(_first_present(payload, priority))

        tool_calls_raw = _first_present(payload, _TOOL_CALL_KEYS) or []
        tool_calls = self._normalize_tool_calls(tool_calls_raw)

        status = _first_present(payload, _STATUS_KEYS)
        status = _as_str(status).strip().lower() or None

        mutated_raw = _first_present(payload, _MUTATED_KEYS) or []
        mutated_files = self._normalize_files(mutated_raw)

        memory_raw = _first_present(payload, _MEMORY_KEYS) or []
        shared_memory_keys = self._normalize_keys(memory_raw)

        role = _as_str(payload.get("role") or "assistant").strip() or "assistant"

        metadata: Dict[str, Any] = {}
        if shared_memory_keys:
            metadata["shared_memory_keys"] = shared_memory_keys
        blocking = payload.get("blocking_reason") or payload.get("blocked_reason")
        if blocking:
            metadata["blocking_reason"] = _as_str(blocking)
        if isinstance(payload.get("metadata"), dict):
            metadata.update(payload["metadata"])

        return CanonicalAgentTurn(
            agent_type=agent or _as_str(payload.get("agent_type") or "unknown"),
            role=role,
            content=content,
            tool_calls=tool_calls,
            status=status,
            mutated_files=mutated_files,
            metadata=metadata,
        )

    @staticmethod
    def _normalize_tool_calls(raw: Any) -> List[Dict[str, Any]]:
        if not raw:
            return []
        calls: List[Dict[str, Any]] = []
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            if isinstance(item, dict):
                calls.append(dict(item))
            else:
                calls.append({"raw": _as_str(item)})
        return calls

    @staticmethod
    def _normalize_files(raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        out: List[str] = []
        for item in raw:
            path = _normalize_path(item)
            if path and path not in out:
                out.append(path)
        return out

    @staticmethod
    def _normalize_keys(raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        out: List[str] = []
        for item in raw:
            key = _as_str(item).strip()
            if key and key not in out:
                out.append(key)
        return out


# =============================================================================
# Deterministic Signal Gate
# =============================================================================

_PASS_LINE_RE = re.compile(r"(?i)\b(PASSED|PASS|OK|SUCCESS)\b")
_FAIL_LINE_RE = re.compile(r"(?i)\b(FAILED|FAIL|ERROR|ERR|FAILURE)\b")
_ERROR_HEADER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning)\b\s*:?")
_FRAME_RE = re.compile(r'^\s*File "[^"]+"')
_HUNK_RE = re.compile(r"^@@[^@]*@@\s*(.*)$")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


class DeterministicSignalGate:
    """Deterministic, model-free compression of noisy agent output."""

    # -- passed-test collapsing ---------------------------------------------
    @staticmethod
    def collapse_passed_tests(text: str) -> str:
        """Collapse consecutive PASS/OK test lines into a single counter."""
        if not text:
            return ""
        out: List[str] = []
        count = 0

        def _flush() -> None:
            nonlocal count
            if count:
                out.append(f"[collapsed: {count} passed test(s)]")
                count = 0

        for line in text.splitlines():
            if _PASS_LINE_RE.search(line) and not _FAIL_LINE_RE.search(line):
                count += 1
            else:
                _flush()
                out.append(line)
        _flush()
        return "\n".join(out)

    # -- failure pinpointing -------------------------------------------------
    @staticmethod
    def pinpoint_failure_trace(text: str) -> str:
        """Keep only the error header line and the last two traceback frames."""
        if not text:
            return ""
        lines = text.splitlines()
        frames = [line.strip() for line in lines if _FRAME_RE.match(line)]
        last_frames = frames[-2:]

        header = ""
        for line in reversed(lines):
            if _ERROR_HEADER_RE.search(line):
                header = line.strip()
                break

        parts: List[str] = list(last_frames)
        if header:
            parts.append(header)
        return "\n".join(parts)

    # -- diff -> AST manifest ------------------------------------------------
    @staticmethod
    def prune_diff_to_ast_manifest(diff_text: str) -> Dict[str, Any]:
        """Extract changed symbols (classes/functions) and added/removed counts."""
        if not diff_text:
            return {"symbols": [], "added": 0, "removed": 0}

        symbols: List[str] = []
        seen: set = set()

        def _add(name: str) -> None:
            if name and name not in seen:
                seen.add(name)
                symbols.append(name)

        added = 0
        removed = 0
        added_python: List[str] = []

        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
                body = line[1:]
                added_python.append(body)
                match = _DEF_RE.match(body)
                if match:
                    _add(match.group(1))
            elif line.startswith("-"):
                removed += 1
                match = _DEF_RE.match(line[1:])
                if match:
                    _add(match.group(1))
            else:
                match = _HUNK_RE.match(line)
                if match:
                    ctx = match.group(1)
                    def_match = _DEF_RE.search(ctx)
                    if def_match:
                        _add(def_match.group(1))

        # AST confirmation for added Python blocks.
        if added_python:
            try:
                tree = ast.parse("\n".join(added_python))
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        _add(node.name)

        return {"symbols": symbols, "added": added, "removed": removed}

    # -- observation de-duplication -----------------------------------------
    @staticmethod
    def _tokens(text: str) -> set:
        return set(_TOKEN_RE.findall((text or "").lower()))

    @classmethod
    def _jaccard(cls, a: str, b: str) -> float:
        ta, tb = cls._tokens(a), cls._tokens(b)
        if not ta and not tb:
            return 1.0
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    @classmethod
    def deduplicate_observations(
        cls, observations: Sequence[str], threshold: float = 0.80
    ) -> List[str]:
        """Drop observations whose token Jaccard similarity exceeds ``threshold``."""
        kept: List[str] = []
        for obs in observations:
            if not obs:
                continue
            if all(cls._jaccard(obs, existing) <= threshold for existing in kept):
                kept.append(obs)
        return kept


# =============================================================================
# Context Knapsack
# =============================================================================


class ContextKnapsack:
    """Pack state sections into a byte budget, spilling overflow to scratchpad."""

    def __init__(
        self,
        budget_bytes: int = 4096,
        scratchpad_dir: str = ".botex/scratchpad",
    ) -> None:
        self.budget_bytes = int(budget_bytes)
        self.scratchpad_dir = scratchpad_dir.replace("\\", "/").strip("/")

    def pack(
        self,
        sections: Sequence[str],
        scratch_id: str = "context",
        base_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Pack ``sections`` into the budget; overflow -> scratchpad pointer."""
        budget = self.budget_bytes
        packed_parts: List[str] = []
        overflow_parts: List[str] = []
        used = 0

        for section in sections:
            text = _as_str(section)  # normalized to string
            size = len(text.encode("utf-8"))
            overhead = 1 if packed_parts else 0
            if used + size + overhead <= budget:
                packed_parts.append(text)
                used += size + overhead
            else:
                overflow_parts.append(text)

        packed = "\n".join(packed_parts)
        pointer: Optional[str] = None

        if overflow_parts:
            from botex.security import mask_secrets

            root = Path(base_dir).resolve() if base_dir is not None else Path(".").resolve()
            scratch_dir = (root / self.scratchpad_dir).resolve()
            scratch_dir.mkdir(parents=True, exist_ok=True)
            # Sanitize scratch_id: prevent path traversal / absolute paths / parent directory traversal.
            safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", _as_str(scratch_id)).strip("._")
            safe_id = re.sub(r"\.{2,}", "_", safe_id)
            log_name = f"{safe_id or 'context'}.log"
            log_path = (scratch_dir / log_name).resolve()
            if not log_path.is_relative_to(scratch_dir):
                log_path = scratch_dir / "context.log"
            payload = mask_secrets("\n".join(overflow_parts))
            try:
                log_path.write_text(payload, encoding="utf-8")
            except OSError:
                # Disk full / permission error: degrade gracefully instead of
                # aborting the whole pack. Emit a pointer without a file target.
                pointer = (
                    f"[context overflow: {len(overflow_parts)} section(s), "
                    f"{len(payload.encode('utf-8'))} B dropped, scratchpad write failed]"
                )
                return {
                    "packed": packed,
                    "overflow_pointer": pointer,
                    "overflow_sections": list(overflow_parts),
                    "budget_bytes": budget,
                    "used_bytes": len(packed.encode("utf-8")),
                }

            overflow_bytes = len(payload.encode("utf-8"))
            rel_path = f"{self.scratchpad_dir}/{log_name}"
            pointer = (
                f"[context overflow: {len(overflow_parts)} section(s), "
                f"{overflow_bytes} B written to {rel_path}]"
            )

            candidate = f"{packed}\n{pointer}" if packed else pointer
            if len(candidate.encode("utf-8")) <= budget:
                packed = candidate
            else:
                packed = pointer

        return {
            "packed": packed,
            "overflow_pointer": pointer,
            "overflow_sections": list(overflow_parts),
            "budget_bytes": budget,
            "used_bytes": len(packed.encode("utf-8")),
        }

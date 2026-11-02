# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Per-task event pipeline: emitter registry, ring buffer, gap-aware tail.

Engine, delegation and FastAPI share one emitter per task_id. Events are
`{seq, ts, kind, data}` dicts; payloads have secrets masked before storage.
`events_since` reports `min_seq`/`dropped` so incremental consumers can
detect gaps instead of silently losing events.
"""

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from .security import mask_secrets
from .tracing import get_current_trace_id


class EventKind:
    PROVIDER_RESOLVED = "provider_resolved"
    ATTEMPT_START = "attempt_start"
    ATTEMPT_END = "attempt_end"
    TOOL_CALL = "tool_call"
    COMPLETION_PARSE = "completion_parse"
    CONTRACT_GATE = "contract_gate"
    RETRY_WAIT = "retry_wait"
    APPROVAL_WAIT = "approval_wait"
    USAGE = "usage"
    NO_PROGRESS = "no_progress"
    DEVIN_UPDATE = "devin_update"
    WAITING_FOR_INPUT = "waiting_for_input"
    TASK_STATE = "task_state"
    ERROR = "error"


ALL_KINDS = {v for k, v in vars(EventKind).items() if not k.startswith("_") and isinstance(v, str)}

_BUFFER_MAX = 1000
_QUEUE_MAX = 500


_MASK_MAX_DEPTH = 6


def _mask_value(val: Any, depth: int, seen: Set[int]) -> Any:
    """Recursively mask strings inside dict/list/tuple payloads (depth-capped, cycle-safe)."""
    if isinstance(val, str):
        return mask_secrets(val)
    if isinstance(val, (dict, list, tuple)):
        if depth >= _MASK_MAX_DEPTH:
            return "[TRUNCATED_NESTING]"
        oid = id(val)
        if oid in seen:
            return "[CIRCULAR]"
        seen.add(oid)
        try:
            if isinstance(val, dict):
                return {k: _mask_value(v, depth + 1, seen) for k, v in val.items()}
            return [_mask_value(v, depth + 1, seen) for v in val]
        finally:
            seen.discard(oid)
    return val


def _mask_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _mask_value(v, 0, set()) for k, v in data.items()}


class TaskEventEmitter:
    """Ring-buffered event log + async-subscriber fan-out for one task."""

    def __init__(self, task_id: str, maxlen: int = _BUFFER_MAX):
        self.task_id = task_id
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._dropped = 0
        self._subs: Set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def emit(self, kind: str, **data) -> None:
        """Append an event and fan out to subscribers. Never raises."""
        try:
            # Stamped at the single emit point so every consumer (SSE,
            # ring buffer, receipts) sees the same correlation id — an
            # honest null when no real span is active (never fabricated).
            data.setdefault("trace_id", get_current_trace_id())
            ev = {"seq": 0, "ts": time.time(), "kind": str(kind), "data": _mask_data(data)}
            with self._lock:
                if len(self._buf) == self._buf.maxlen:
                    self._dropped += 1
                self._seq += 1
                ev["seq"] = self._seq
                self._buf.append(ev)
                subs = list(self._subs)
            for q in subs:
                try:
                    q.put_nowait(ev)
                except Exception:
                    try:
                        loop = getattr(q, "_loop", None)
                        if loop is not None and not loop.is_closed():
                            loop.call_soon_threadsafe(
                                lambda q=q, ev=ev: q.put_nowait(ev) if not q.full() else None
                            )
                    except Exception:
                        pass
        except Exception:
            pass

    def events_since(self, seq: int) -> Tuple[List[Dict[str, Any]], int, int]:
        """Tail after `seq`; (events, min_seq, dropped_total)."""
        with self._lock:
            items = list(self._buf)
            dropped = self._dropped
        min_seq = items[0]["seq"] if items else (self._seq + 1)
        return [e for e in items if e["seq"] > seq], min_seq, dropped

    def tail(self, n: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buf)[-n:]

    def subscribe(self) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        with self._lock:
            self._subs.discard(q)

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


_EMITTERS: Dict[str, TaskEventEmitter] = {}
_REGISTRY_LOCK = threading.Lock()


def get_emitter(task_id: str) -> TaskEventEmitter:
    with _REGISTRY_LOCK:
        em = _EMITTERS.get(task_id)
        if em is None:
            em = TaskEventEmitter(task_id)
            _EMITTERS[task_id] = em
        return em


def emitter_or_none(task_id: str) -> Optional[TaskEventEmitter]:
    with _REGISTRY_LOCK:
        return _EMITTERS.get(task_id)


def drop_emitter(task_id: str) -> None:
    with _REGISTRY_LOCK:
        _EMITTERS.pop(task_id, None)

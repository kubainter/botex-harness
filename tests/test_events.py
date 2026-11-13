# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
import asyncio

from botex.events import (
    ALL_KINDS,
    EventKind,
    TaskEventEmitter,
    drop_emitter,
    emitter_or_none,
    get_emitter,
)


def test_seq_monotonic_and_tail():
    em = TaskEventEmitter("t1")
    for i in range(5):
        em.emit(EventKind.TOOL_CALL, n=i)
    tail = em.tail(10)
    assert [e["seq"] for e in tail] == [1, 2, 3, 4, 5]
    assert tail[-1]["kind"] == EventKind.TOOL_CALL
    assert tail[-1]["data"]["n"] == 4


def test_ring_bound_and_gap_detection():
    em = TaskEventEmitter("t2", maxlen=10)
    for i in range(15):
        em.emit(EventKind.USAGE, i=i)
    events, min_seq, dropped = em.events_since(0)
    assert len(events) == 10
    assert min_seq == 6
    assert dropped == 5
    # Client behind the buffer sees a gap, not silent loss
    events2, min_seq2, dropped2 = em.events_since(3)
    assert min_seq2 == 6 and dropped2 == 5
    assert all(e["seq"] > 3 for e in events2)


def test_events_since_cursor():
    em = TaskEventEmitter("t3")
    for i in range(4):
        em.emit("k", i=i)
    events, _, _ = em.events_since(2)
    assert [e["seq"] for e in events] == [3, 4]
    assert em.events_since(4)[0] == []


def test_subscribe_and_unsubscribe():
    em = TaskEventEmitter("t4")
    q = em.subscribe()
    em.emit(EventKind.TASK_STATE, state="RUNNING")
    ev = q.get_nowait()
    assert ev["kind"] == EventKind.TASK_STATE
    em.unsubscribe(q)
    assert em.subscriber_count == 0


def test_emit_never_raises_on_bad_subscriber():
    em = TaskEventEmitter("t5")

    class BadQ:
        def put_nowait(self, ev):
            raise RuntimeError("boom")

    em._subs.add(BadQ())
    em.emit(EventKind.ERROR, msg="x")  # must not raise
    assert em.last_seq == 1


def test_mask_secrets_in_payload():
    em = TaskEventEmitter("t6")
    em.emit(EventKind.ERROR, token="sk-or-v1-abcdefghij0123456789")
    ev = em.tail(1)[0]
    assert "abcdefghij" not in json_dumps(ev)


def test_registry_lifecycle():
    em = get_emitter("task-xyz")
    assert emitter_or_none("task-xyz") is em
    assert get_emitter("task-xyz") is em  # idempotent
    drop_emitter("task-xyz")
    assert emitter_or_none("task-xyz") is None


def test_queue_overflow_does_not_block_emit():
    em = TaskEventEmitter("t7")
    q = asyncio.Queue(maxsize=1)
    em._subs.add(q)
    for _ in range(5):
        em.emit("k")  # full queue must not raise or block
    assert q.qsize() == 1
    assert em.last_seq == 5


def test_shadow_routing_kind_registered_and_emit():
    """Layer C: SHADOW_ROUTING is a first-class kind and payloads round-trip."""
    assert EventKind.SHADOW_ROUTING == "shadow_routing"
    assert EventKind.SHADOW_ROUTING in ALL_KINDS

    em = TaskEventEmitter("t-shadow")
    em.emit(
        EventKind.SHADOW_ROUTING,
        recipe="planner",
        taxonomy="bugfix",
        recommended_model="vendor/best:free",
        actual_model="paid/live",
        diverged=True,
        sample_count=25,
        shadow_only=True,
    )
    ev = em.tail(1)[0]
    assert ev["kind"] == "shadow_routing"
    assert ev["data"]["recommended_model"] == "vendor/best:free"
    assert ev["data"]["actual_model"] == "paid/live"
    assert ev["data"]["diverged"] is True
    assert ev["data"]["shadow_only"] is True


def json_dumps(obj):
    import json

    return json.dumps(obj)

import asyncio

import pytest

from botex.core import tracing
from botex.ops.curator import recall_curator
from botex.providers import retry as retry_mod


class _Recorder:
    def __init__(self):
        self.spans = []

    def install(self, monkeypatch):
        import contextlib

        rec = self

        @contextlib.contextmanager
        def fake(name, attributes=None):
            span = tracing.NullSpan(name, dict(attributes or {}))
            rec.spans.append(span)
            try:
                yield span
            finally:
                span.end()

        monkeypatch.setattr(tracing, "start_span", fake)


def test_retry_attempt_span_closed_on_failure(monkeypatch):
    rec = _Recorder()
    rec.install(monkeypatch)

    async def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        asyncio.run(retry_mod._traced_attempt(boom, "p", 2))
    assert rec.spans[0].name == "botex.retry.attempt"
    assert rec.spans[0].attributes == {"provider": "p", "attempt": 2}
    assert rec.spans[0].end_time is not None


def test_recall_tier_span_records_status(monkeypatch):
    rec = _Recorder()
    rec.install(monkeypatch)
    attempts = []

    async def fake_impl():
        attempts.append({"tier": "gemini", "status": "ok", "ms": 7})
        return {"answer": "a"}

    result = asyncio.run(recall_curator._traced_tier("gemini", attempts, fake_impl()))
    assert result == {"answer": "a"}
    span = rec.spans[0]
    assert span.name == "botex.recall.tier"
    assert span.attributes["tier_status"] == "ok"
    assert span.attributes["tier_ms"] == 7

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for Plan 051 Harness Limits Telemetry & HarnessInventoryService."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import botex.edge.adapters as adapters_mod
from botex.edge.adapters import (
    HarnessInventoryService,
    HarnessLimitsCollector,
    _default_unknown_limits,
    discover_local_harnesses,
    normalize_limits_payload,
)

_EXPECTED_LIMIT_KEYS = {
    "unit",
    "used",
    "remaining",
    "limit",
    "reset_at",
    "window_s",
    "source",
    "confidence",
    "collected_at",
}


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang_s: float = 0.0,
        pid: int = 99991,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._final_rc = returncode
        self.returncode: int | None = None
        self._hang_s = hang_s
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.waited = False
        self._done = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang_s > 0:
            await asyncio.sleep(self._hang_s)
        self.returncode = self._final_rc
        self._done.set()
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        self.waited = True
        if self.terminated and not self.killed and self._hang_s == 0:
            self.returncode = -15
            self._done.set()
        if self._done.is_set():
            return int(self.returncode or 0)
        await asyncio.sleep(0.2)
        return int(self.returncode or -9)


class _FakeHTTPResponse:
    def __init__(self, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._buf = io.BytesIO(raw)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def test_exports_and_limits_payload_normalization() -> None:
    assert "HarnessLimitsCollector" in adapters_mod.__all__
    assert "HarnessInventoryService" in adapters_mod.__all__

    unk = _default_unknown_limits(source="boot", collected_at="2026-11-24T12:00:00+00:00")
    assert set(unk.keys()) == _EXPECTED_LIMIT_KEYS
    assert unk["unit"] == "unknown"
    assert unk["confidence"] == "unknown"
    assert unk["used"] is None
    assert unk["remaining"] is None
    assert unk["limit"] is None
    assert unk["reset_at"] is None
    assert unk["window_s"] is None
    assert unk["source"] == "boot"

    # Zero scalars preserved; booleans & NaN/Inf rejected; enums mapped statically
    norm_zero = normalize_limits_payload(
        {
            "unit": "ACU",
            "used": 0,
            "remaining": 0.0,
            "limit": 100,
            "window_s": 0,
            "reset_at": "2026-11-25T00:00:00Z",
            "source": "devin_cli",
            "confidence": "EXACT",
            "collected_at": "2026-11-24T12:00:00Z",
        }
    )
    assert set(norm_zero.keys()) == _EXPECTED_LIMIT_KEYS
    assert norm_zero["unit"] == "credits"
    assert norm_zero["used"] == 0.0
    assert norm_zero["remaining"] == 0.0
    assert norm_zero["limit"] == 100.0
    assert norm_zero["window_s"] == 0
    assert norm_zero["confidence"] == "exact"

    norm_bad = normalize_limits_payload(
        {
            "unit": "injected_unit",
            "used": True,
            "remaining": float("nan"),
            "limit": float("inf"),
            "window_s": False,
            "confidence": "injected_conf",
        },
        default_source="fallback_src",
    )
    assert norm_bad["unit"] == "unknown"
    assert norm_bad["confidence"] == "unknown"
    assert norm_bad["used"] is None
    assert norm_bad["remaining"] is None
    assert norm_bad["limit"] is None
    assert norm_bad["window_s"] is None
    assert norm_bad["source"] == "fallback_src"


def test_collector_cli_probes_devin_zcode_and_scrubs_env(monkeypatch) -> None:
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", "btxd_secret_edge_tok")
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "btx_cloud_secret")
    monkeypatch.setenv("BOTEX_CREDENTIALS_DIR", "/tmp/secret_creds")
    monkeypatch.setenv("SWARM_FENCING_SECRET", "swarm_secret_123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai-key-123456")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_123456")
    monkeypatch.setenv("SAFE_OS_MARKER", "keep_me")

    captured_calls: list[tuple[tuple[Any, ...], dict[str, str]]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        env = dict(kwargs.get("env") or {})
        captured_calls.append((args, env))
        prog = str(args[0])
        if "devin" in prog:
            payload = {
                "unit": "credits",
                "used": 12.5,
                "remaining": 87.5,
                "limit": 100.0,
                "reset_at": "2026-12-01T00:00:00Z",
                "window_s": 86400,
                "confidence": "exact",
            }
            return _FakeProcess(stdout=json.dumps(payload).encode("utf-8"))
        if "zcode" in prog:
            payload = {
                "limits": {
                    "tokens_used": 25000,
                    "tokens_limit": 100000,
                    "estimated": True,
                    "window_seconds": 3600,
                }
            }
            return _FakeProcess(stdout=json.dumps(payload).encode("utf-8"))
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    collector = HarnessLimitsCollector()
    res = asyncio.run(collector.collect(["devin", "zcode", "unlisted_tool"]))

    assert set(res.keys()) == {"devin", "zcode", "unlisted_tool"}
    assert res["devin"]["unit"] == "credits"
    assert res["devin"]["used"] == 12.5
    assert res["devin"]["remaining"] == 87.5
    assert res["devin"]["limit"] == 100.0
    assert res["devin"]["window_s"] == 86400
    assert res["devin"]["confidence"] == "exact"

    assert res["zcode"]["unit"] == "tokens"
    assert res["zcode"]["used"] == 25000.0
    assert res["zcode"]["remaining"] == 75000.0
    assert res["zcode"]["limit"] == 100000.0
    assert res["zcode"]["window_s"] == 3600
    assert res["zcode"]["confidence"] == "estimated"

    assert res["unlisted_tool"]["confidence"] == "unknown"
    assert res["unlisted_tool"]["source"] == "unprobed"

    assert len(captured_calls) == 2
    for _args, child_env in captured_calls:
        assert child_env.get("SAFE_OS_MARKER") == "keep_me"
        assert child_env.get("NO_COLOR") == "1"
        for forbidden in (
            "BOTEX_EDGE_TOKEN",
            "BOTEX_ACCESS_TOKEN",
            "BOTEX_CREDENTIALS_DIR",
            "SWARM_FENCING_SECRET",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
        ):
            assert forbidden not in child_env


def test_collector_rejects_non_bare_cli_probe(monkeypatch) -> None:
    spawned: list[Any] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawned.append(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    collector = HarnessLimitsCollector(cli_probes={"devin": ("/tmp/evil_devin", "status")})
    res = asyncio.run(collector.collect(["devin"]))
    assert spawned == []
    assert res["devin"]["confidence"] == "unknown"
    assert res["devin"]["source"] == "invalid_cli_probe"


def test_collector_timeout_bounded_under_1_5s_and_cleans_up_process(monkeypatch) -> None:
    hung_proc = _FakeProcess(hang_s=10.0, pid=88881)

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return hung_proc

    def fake_http(url: str, timeout_s: float) -> dict[str, Any]:
        if url.endswith("/api/ps"):
            return {"models": [{"name": "qwen2.5:3b"}]}
        if url.endswith("/api/version"):
            return {"version": "0.6.2"}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    collector = HarnessLimitsCollector(http_get_json=fake_http)

    # Even when caller passes timeout_s=5.0, collector enforces <= 1.5s budget.
    t0 = time.perf_counter()
    res = asyncio.run(collector.collect(["devin", "ollama"], timeout_s=0.25))
    elapsed = time.perf_counter() - t0

    assert elapsed <= 1.5
    assert res["devin"]["confidence"] == "unknown"
    assert res["devin"]["source"] == "timeout"
    assert hung_proc.terminated is True
    assert hung_proc.killed is True
    assert hung_proc.waited is True

    # Sibling fast probe preserves exact telemetry instead of being discarded.
    assert res["ollama"]["unit"] == "models_loaded"
    assert res["ollama"]["used"] == 1.0
    assert res["ollama"]["confidence"] == "exact"
    assert "0.6.2" in res["ollama"]["source"]


def test_collector_real_subprocess_timeout_leaves_no_orphan() -> None:
    py_name = Path(sys.executable).stem
    if shutil.which(py_name) is None:
        py_name = "python" if shutil.which("python") else "python3"
    assert shutil.which(py_name) is not None

    collector = HarnessLimitsCollector(
        cli_probes={"devin": (py_name, "-c", "import time; time.sleep(30)")}
    )
    t0 = time.perf_counter()
    res = asyncio.run(collector.collect(["devin"], timeout_s=0.3))
    elapsed = time.perf_counter() - t0

    assert elapsed <= 1.5
    assert res["devin"]["confidence"] == "unknown"
    assert res["devin"]["source"] == "timeout"


def test_collector_ollama_loopback_probe_and_non_loopback_rejection(monkeypatch) -> None:
    seen_urls: list[str] = []

    def fake_urlopen(req: Any, timeout: float = 1.0) -> _FakeHTTPResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen_urls.append(url)
        if url.endswith("/api/ps"):
            return _FakeHTTPResponse(
                {"models": [{"name": "qwen2.5:3b"}, {"name": "nomic-embed-text"}]}
            )
        if url.endswith("/api/version"):
            return _FakeHTTPResponse({"version": "0.5.11"})
        raise urllib.error.URLError("unexpected path")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    collector = HarnessLimitsCollector()

    # Valid loopback targets succeed
    ok_res = asyncio.run(collector.collect(["ollama"], ollama_base_url="http://127.0.0.1:11434"))
    assert ok_res["ollama"]["unit"] == "models_loaded"
    assert ok_res["ollama"]["used"] == 2.0
    assert ok_res["ollama"]["confidence"] == "exact"
    assert ok_res["ollama"]["source"] == "ollama/0.5.11"
    assert len(seen_urls) == 2

    # Non-loopback / unsafe URLs are strictly rejected before any HTTP call
    seen_urls.clear()
    bad_urls = (
        "http://192.168.1.25:11434",
        "http://10.0.0.5:11434",
        "http://169.254.169.254:80",
        "http://8.8.8.8:11434",
        "http://ollama.internal:11434",
        "ftp://127.0.0.1:11434",
        "http://user:secret@127.0.0.1:11434",
        "",
    )
    for bad_url in bad_urls:
        rej = asyncio.run(collector.collect(["ollama"], ollama_base_url=bad_url))
        assert rej["ollama"]["confidence"] == "unknown", bad_url
        assert rej["ollama"]["source"] == "non_loopback_rejected", bad_url
    assert seen_urls == []


def test_collector_ollama_absent_degrades_to_unknown(monkeypatch) -> None:
    def failing_urlopen(req: Any, timeout: float = 1.0) -> _FakeHTTPResponse:
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    collector = HarnessLimitsCollector()
    res = asyncio.run(collector.collect(["ollama"]))
    assert res["ollama"]["confidence"] == "unknown"
    assert res["ollama"]["unit"] == "unknown"
    assert res["ollama"]["used"] is None


def test_collector_ollama_redirect_handler_and_timeout_clamp(monkeypatch) -> None:
    import pytest

    req = urllib.request.Request("http://127.0.0.1:11434/api/version")
    with pytest.raises(urllib.error.HTTPError, match="redirects disallowed"):
        adapters_mod._NoRedirectHandler().redirect_request(
            req, None, 302, "Found", {}, "http://198.51.100.10/pwn"
        )

    # IPv6 and localhost loopback endpoints accepted; slow HTTP bounded <= 1.5s
    seen_urls: list[str] = []

    def fake_urlopen(req_obj: Any, timeout: float = 1.0) -> _FakeHTTPResponse:
        url = req_obj.full_url if hasattr(req_obj, "full_url") else str(req_obj)
        seen_urls.append(url)
        if url.endswith("/api/ps"):
            return _FakeHTTPResponse({"models": [{"name": "llama3.2:3b"}]})
        return _FakeHTTPResponse({"version": "0.6.1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    collector = HarnessLimitsCollector()
    res_v6 = asyncio.run(collector.collect(["ollama"], ollama_base_url="http://[::1]:11434"))
    assert res_v6["ollama"]["unit"] == "models_loaded"
    assert res_v6["ollama"]["used"] == 1.0
    assert res_v6["ollama"]["confidence"] == "exact"
    assert res_v6["ollama"]["source"] == "ollama/0.6.1"
    assert any("[::1]:11434" in u for u in seen_urls)

    # Caller passing timeout_s=10.0 is still strictly clamped to <= 1.5s wall-clock
    hung_proc = _FakeProcess(hang_s=5.0, pid=88882)

    async def slow_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return hung_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
    t0 = time.perf_counter()
    clamped = asyncio.run(collector.collect(["devin"], timeout_s=10.0))
    elapsed = time.perf_counter() - t0
    assert elapsed <= 1.65  # 1.5s budget + minimal scheduler jitter
    assert clamped["devin"]["confidence"] == "unknown"
    assert clamped["devin"]["source"] == "timeout"
    assert hung_proc.terminated is True
    assert hung_proc.killed is True


def test_discover_local_harnesses_attaches_limits_and_discovers_ollama(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters_mod,
        "_resolve_cli_availability",
        lambda names: {n: (n == "devin") for n in names},
    )

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        payload = {"unit": "credits", "used": 5, "remaining": 95, "limit": 100}
        return _FakeProcess(stdout=json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    def fake_http(url: str, timeout_s: float) -> dict[str, Any]:
        if url.endswith("/api/ps"):
            return {"models": [{"name": "qwen2.5:3b"}, {"name": "coder:7b"}]}
        return {"version": "0.6.0"}

    collector = HarnessLimitsCollector(http_get_json=fake_http)
    records = asyncio.run(
        discover_local_harnesses(
            allowlisted_clis={"devin": "devin", "zcode": "zcode"},
            limits_collector=collector,
        )
    )

    by_id = {r["harness_id"]: r for r in records}
    assert set(by_id.keys()) == {"devin", "zcode", "ollama"}

    for rec in records:
        assert {"harness_id", "transport", "status", "last_seen", "capabilities", "limits"} <= set(
            rec.keys()
        )
        assert set(rec["limits"].keys()) == _EXPECTED_LIMIT_KEYS

    assert by_id["devin"]["status"] == "online"
    assert by_id["devin"]["limits"]["unit"] == "credits"
    assert by_id["devin"]["limits"]["remaining"] == 95.0
    assert by_id["devin"]["limits"]["confidence"] == "exact"

    assert by_id["zcode"]["status"] == "offline"
    assert by_id["zcode"]["limits"]["confidence"] == "unknown"

    assert by_id["ollama"]["transport"] == "http"
    assert by_id["ollama"]["status"] == "online"
    assert by_id["ollama"]["capabilities"]["models_loaded"] == 2
    assert by_id["ollama"]["capabilities"]["version"] == "0.6.0"
    assert by_id["ollama"]["limits"]["unit"] == "models_loaded"
    assert by_id["ollama"]["limits"]["used"] == 2.0
    assert by_id["ollama"]["limits"]["confidence"] == "exact"


def test_harness_inventory_service_background_loop_ttl_and_nonblocking_snapshot() -> None:
    clock = {"now": 100.0}
    refresh_count = {"n": 0}
    gate = asyncio.Event()

    async def fake_discover() -> list[dict[str, Any]]:
        refresh_count["n"] += 1
        if refresh_count["n"] == 2:
            # Simulate a slow background probe to prove get_cached_snapshot never blocks
            await gate.wait()
        return [
            {
                "harness_id": "devin",
                "transport": "cli",
                "status": "online",
                "last_seen": "2026-11-24T15:00:00+00:00",
                "capabilities": {"available": True},
                "limits": {
                    "unit": "credits",
                    "used": float(refresh_count["n"]),
                    "remaining": 90.0,
                    "limit": 100.0,
                    "reset_at": None,
                    "window_s": 86400,
                    "source": "devin_cli",
                    "confidence": "exact",
                    "collected_at": "2026-11-24T15:00:00+00:00",
                },
            }
        ]

    svc = HarnessInventoryService(
        refresh_interval_s=0.03,
        ttl_s=30.0,
        discover_fn=fake_discover,
        time_fn=lambda: clock["now"],
    )

    # Cold cache returns immediately with empty list
    assert svc.get_cached_snapshot() == []

    async def _run() -> None:
        await svc.start()
        try:
            for _ in range(40):
                if svc.get_cached_snapshot():
                    break
                await asyncio.sleep(0.01)

            snap1 = svc.get_cached_snapshot()
            assert len(snap1) == 1
            assert snap1[0]["limits"]["confidence"] == "exact"
            assert snap1[0]["limits"]["used"] == 1.0

            # Mutating caller copy never mutates internal cache
            snap1[0]["limits"]["confidence"] = "mutated"
            snap1[0]["harness_id"] = "tampered"
            fresh = svc.get_cached_snapshot()
            assert fresh[0]["harness_id"] == "devin"
            assert fresh[0]["limits"]["confidence"] == "exact"

            # Wait until refresh #2 is parked on `gate`, then verify non-blocking read
            for _ in range(40):
                if refresh_count["n"] >= 2:
                    break
                await asyncio.sleep(0.01)
            t0 = time.perf_counter()
            while_parked = svc.get_cached_snapshot()
            assert (time.perf_counter() - t0) < 0.05
            assert while_parked[0]["limits"]["confidence"] == "exact"

            # Advance clock past TTL (30s) while refresh #2 is still blocked -> stale_cache
            clock["now"] = 135.0
            stale = svc.get_cached_snapshot()
            assert stale[0]["limits"]["confidence"] == "unknown"
            assert stale[0]["limits"]["source"] == "stale_cache"

            # Release refresh #2 -> updates timestamp and restores exact confidence
            gate.set()
            for _ in range(40):
                cur = svc.get_cached_snapshot()
                if cur and cur[0]["limits"]["used"] >= 2.0:
                    break
                await asyncio.sleep(0.01)
            recovered = svc.get_cached_snapshot()
            assert recovered[0]["limits"]["confidence"] == "exact"
            assert recovered[0]["limits"]["source"] == "devin_cli"
        finally:
            gate.set()
            await svc.stop()
            # Idempotent stop
            await svc.stop()

    asyncio.run(_run())

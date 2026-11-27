# SPDX-License-Identifier: Apache-2.0

# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)

"""

Plan 059 Wave-2 Integration Tests

==================================

End-to-end wiring coverage for the dynamic model-candidate tail:

``resolve_task_model_candidates`` (config), ``run_botex_task`` (engine),

``_execute_night_pulse_cycle`` + ``check_health`` (server), and the legacy

``botex.dynamic_candidates`` import alias. The engine-level tests exercise

the REAL resolver — never a stubbed one — so off-mode byte-equivalence and

append-tail attribution are verified against production code paths.



Hard guarantee: no network access anywhere in this suite. Beyond the global

socket block, the network-touching seams (``http_pool`` clients,

``urllib.request.urlopen``, ``BaseProvider.fetch_catalog``,

``ModelCatalog.load``) are monkeypatched to raise ``AssertionError`` —

a single accidental call fails the test loudly.

"""

from __future__ import annotations


import asyncio

import copy

import importlib

import json

import time

import urllib.request

from pathlib import Path

from types import SimpleNamespace

from typing import Any, Dict, List

from unittest.mock import patch


import pytest


from botex.core import engine

import botex.providers.dynamic_candidates as dc

from botex.server import config as srv_config

from botex.server.config import resolve_task_model_candidates


P1 = "p1"

P2 = "p2"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Never let the developer's real mode env leak into these tests."""

    monkeypatch.delenv("BOTEX_DYNAMIC_CANDIDATES_MODE", raising=False)

    yield


def _response(content: Any = None, finish: str = "stop") -> SimpleNamespace:

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason=finish,
            )
        ],
        usage=None,
    )


class _FakeClient:
    def __init__(self, responses: List[SimpleNamespace]):

        self.responses = list(responses)

        self.calls: List[Dict[str, Any]] = []

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> SimpleNamespace:

        self.calls.append(kwargs)

        if not self.responses:
            raise AssertionError("FakeClient response queue exhausted")

        return self.responses.pop(0)


_TASK_CLASSES = {
    tax: {
        "enabled": True,
        "allow_unproven": True,
        "min_context_length": 1,
        "allow_paid": True,
    }
    for tax in ("analysis", "bugfix", "test", "feature", "refactor", "infra", "general")
}

_MODES = {m: {"enabled": True} for m in ("readonly", "edit", "destructive", "full")}


def _dyn_section(
    mode: str,
    snap_file: Any,
    allowlists: Dict[str, List[str]],
    *,
    max_total: int = 2,
) -> Dict[str, Any]:

    return {
        "mode": mode,
        "snapshot_file": str(snap_file),
        "max_per_provider": 2,
        "max_total_per_task": max_total,
        "max_snapshot_age_hours": 72,
        "providers": {
            name: {"enabled": True, "allow_models": list(pats), "deny_models": []}
            for name, pats in allowlists.items()
        },
        "task_classes": copy.deepcopy(_TASK_CLASSES),
        "modes": copy.deepcopy(_MODES),
        "promotion": {},
    }


def _gates() -> Dict[str, Any]:

    return {
        "allowlisted": True,
        "denylisted": False,
        "supports_tools": True,
        "price": "free",
        "prompt_price": 0.0,
        "completion_price": 0.0,
        "zdr": "not_supported",
        "context_length": 131072,
    }


def _cand(model: str, *, state: str = "active", rank: float = 0.5) -> Dict[str, Any]:

    return {
        "model": model,
        "state": state,
        "rank_score": rank,
        "gates": _gates(),
        "evidence": {
            "window_days": 30,
            "runs": 3,
            "done_runs": 3,
            "quality_failures": 0,
            "infra_failures": 0,
            "rollback_count": 0,
            "syntax_rejections": 0,
        },
        "task_classes": list(_TASK_CLASSES.keys()),
        "reasons": [],
    }


def _provider_entry(candidates: List[Dict[str, Any]], ts: Any = None) -> Dict[str, Any]:

    return {
        "catalog_fetched_at": ts if ts is not None else time.time(),
        "catalog_state": "fresh",
        "candidates": candidates,
    }


def _write_snapshot(tmp_path: Any, providers_map: Dict[str, Any], generated_at: Any = None) -> Any:

    snap = {
        "schema_version": 1,
        "generated_at": generated_at if generated_at is not None else time.time(),
        "generated_at_iso": "2026-11-27T00:00:00+00:00",
        "generator": "test",
        "source": {"analytics_window_days": 30, "catalog_drift_generated_at": None},
        "providers": providers_map,
    }

    path = tmp_path / "dyn_snap.json"

    path.write_text(json.dumps(snap), encoding="utf-8")

    return path


def _provider_cfg(models: List[str]) -> Dict[str, Any]:
    """Provider entry: models[0] is the default, the rest are fallbacks."""

    return {
        "models": {"default": models[0]},
        "model_fallbacks": {"default": models[1:]},
        "zdr": {"enabled": False},
        "local": True,
        "base_url": "http://127.0.0.1:9/v1",
    }


def _make_cfg(
    providers: Dict[str, Dict[str, Any]],
    dyn_section: Dict[str, Any] | None,
    provider_fallbacks: Dict[str, List[str]] | None = None,
) -> Dict[str, Any]:

    cfg = copy.deepcopy(srv_config.load_config())

    cfg["providers"] = providers

    if dyn_section is not None:
        cfg["dynamic_candidates"] = dyn_section

    else:
        cfg.pop("dynamic_candidates", None)

    cfg["provider_fallbacks"] = provider_fallbacks or {}

    return cfg


def _patch_engine(
    monkeypatch: pytest.MonkeyPatch, cfg: Dict[str, Any], fake: Any
) -> Dict[str, Any]:
    """Point the engine at a controlled config + fake client; capture log_run."""

    captured: Dict[str, Any] = {"log_runs": []}

    monkeypatch.setattr(srv_config, "load_config", lambda *a, **k: cfg)

    monkeypatch.setattr(engine, "load_config", lambda *a, **k: cfg)

    monkeypatch.setattr(engine, "AsyncOpenAI", lambda **_: fake)

    monkeypatch.setattr(engine, "check_model_price", lambda *a, **k: (True, ""))

    monkeypatch.setattr(engine, "check_model_tools", lambda *a, **k: (True, ""))

    monkeypatch.setattr(engine, "check_model_zdr", lambda *a, **k: (True, ""))

    monkeypatch.setattr(engine, "check_budget_limit", lambda *a, **k: (False, 0.0))

    def _log_run(**kw: Any) -> Dict[str, Any]:

        captured["log_runs"].append(kw)

        return {"cost_usd": 0.0}

    monkeypatch.setattr(engine, "log_run", _log_run)

    monkeypatch.setattr(engine, "print_run_summary", lambda *a, **k: None)

    return captured


def _collect() -> Any:

    events: List[Any] = []

    def _cb(kind: str, **data: Any) -> None:

        events.append((kind, data))

    _cb.events = events  # type: ignore[attr-defined]

    return _cb


def _run(tmpdir: str, events: Any, **kwargs: Any) -> Dict[str, Any]:

    kwargs.setdefault("task", "analyze the login module for issues")

    kwargs.setdefault("mode", "readonly")

    kwargs.setdefault("profile", "default")

    kwargs.setdefault("api_key", "x")

    kwargs.setdefault("progress_cb", events)

    Path(tmpdir).mkdir(parents=True, exist_ok=True)

    return asyncio.run(engine.run_botex_task(workspace_dir=tmpdir, **kwargs))


class TestOffModeByteIdentical:
    def test_off_mode_zero_snapshot_reads_and_clean_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a", "static/b"])},
            _dyn_section("off", snap, {P1: ["vendor/*"]}),
        )

        fake = _FakeClient([_response("STATUS: DONE\nreviewed")])

        captured = _patch_engine(monkeypatch, cfg, fake)

        loads = {"n": 0}

        def _counted(*a: Any, **k: Any):

            loads["n"] += 1

            raise AssertionError("off mode must never read the snapshot")

        monkeypatch.setattr(dc, "load_dynamic_snapshot_offline", _counted)

        events = _collect()

        res = _run(str(tmp_path / "ws"), events, provider=P1)

        assert loads["n"] == 0

        assert res["status"] == "DONE"

        assert [c["model"] for c in fake.calls] == ["static/a"]

        assert "dynamic_candidates" not in res

        assert all("candidate_source" not in a for a in res["attempts"])

        assert all(kind != "dynamic_candidates" for kind, _ in events.events)

        assert all(
            lr.get("candidate_source") is None and lr.get("mode") is None
            for lr in captured["log_runs"]
        )

    def test_missing_section_matches_off(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        cfg = _make_cfg({P1: _provider_cfg(["static/a"])}, None)

        monkeypatch.setattr(srv_config, "load_config", lambda *a, **k: cfg)

        res = resolve_task_model_candidates("", "default", P1)

        assert res["candidates"] == ["static/a"]

        assert res["dynamic_mode"] == "off"

        assert res["dynamic_candidates"] == []

        assert res["static_candidates"] == ["static/a"]


class TestAppendMode:
    def test_static_success_burns_no_dynamic_attempts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a", "static/b"])},
            _dyn_section("append", snap, {P1: ["vendor/*"]}),
        )

        fake = _FakeClient([_response("STATUS: DONE\nreviewed")])

        captured = _patch_engine(monkeypatch, cfg, fake)

        events = _collect()

        res = _run(str(tmp_path / "ws"), events, provider=P1)

        assert res["status"] == "DONE"

        assert [c["model"] for c in fake.calls] == ["static/a"]

        assert res["dynamic_candidates"]["dynamic_attempted"] == 0

        assert res["attempts"][0]["candidate_source"] == "static"

        dyn_events = [d for k, d in events.events if k == "dynamic_candidates"]

        assert dyn_events and dyn_events[0]["dynamic_mode"] == "append"

        assert dyn_events[0]["dynamic_count"] == 1

        assert dyn_events[0]["snapshot_state"] == "ok"

        assert captured["log_runs"]

        assert all(lr.get("candidate_source") == "static" for lr in captured["log_runs"])

        assert all(lr.get("mode") == "readonly" for lr in captured["log_runs"])

    def test_all_static_fail_reaches_dynamic_tail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(
            tmp_path,
            {P1: _provider_entry([_cand("vendor/m1"), _cand("vendor/m2", rank=0.4)])},
        )

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a", "static/b"])},
            _dyn_section("append", snap, {P1: ["vendor/*"]}),
        )

        fake = _FakeClient(
            [
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: DONE\nreviewed"),
            ]
        )

        captured = _patch_engine(monkeypatch, cfg, fake)

        events = _collect()

        res = _run(str(tmp_path / "ws"), events, provider=P1)

        assert res["status"] == "DONE"

        assert [c["model"] for c in fake.calls] == ["static/a", "static/b", "vendor/m1"]

        srcs = [a["candidate_source"] for a in res["attempts"]]

        assert srcs == ["static", "static", "dynamic"]

        assert res["dynamic_candidates"]["dynamic_attempted"] == 1

        assert captured["log_runs"][-1]["candidate_source"] == "dynamic"

        assert captured["log_runs"][-1]["mode"] == "readonly"

    def test_shadow_never_appends(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a", "static/b"])},
            _dyn_section("shadow", snap, {P1: ["vendor/*"]}),
        )

        fake = _FakeClient(
            [
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: DONE\nreviewed"),
            ]
        )

        _patch_engine(monkeypatch, cfg, fake)

        events = _collect()

        res = _run(str(tmp_path / "ws"), events, provider=P1)

        assert res["status"] == "DONE"

        assert [c["model"] for c in fake.calls] == ["static/a", "static/b"]

        dyn_events = [d for k, d in events.events if k == "dynamic_candidates"]

        assert dyn_events and dyn_events[0]["would_append_count"] == 1

        assert dyn_events[0]["dynamic_count"] == 1  # would-have-been tail, not appended


class TestExplicitModelBypass:
    def test_explicit_model_never_reads_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a"])},
            _dyn_section("append", snap, {P1: ["vendor/*"]}),
        )

        monkeypatch.setattr(srv_config, "load_config", lambda *a, **k: cfg)

        loads = {"n": 0}

        def _counted(*a: Any, **k: Any):

            loads["n"] += 1

            raise AssertionError("explicit model must never read the snapshot")

        monkeypatch.setattr(dc, "load_dynamic_snapshot_offline", _counted)

        res = resolve_task_model_candidates(
            "explicit/model", "default", P1, task_class="bugfix", capability_mode="readonly"
        )

        assert loads["n"] == 0

        assert res["candidates"] == ["explicit/model"]

        assert res["dynamic_candidates"] == []

        fake = _FakeClient([_response("STATUS: DONE\nreviewed")])

        _patch_engine(monkeypatch, cfg, fake)

        monkeypatch.setattr(dc, "load_dynamic_snapshot_offline", _counted)

        events = _collect()

        out = _run(str(tmp_path / "ws"), events, provider=P1, model="explicit/model")

        assert loads["n"] == 0

        assert [c["model"] for c in fake.calls] == ["explicit/model"]

        assert all(a.get("candidate_source") == "static" for a in out["attempts"])


class TestProviderFallbackIsolation:
    def test_fallback_provider_gets_own_tail_and_total_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(
            tmp_path,
            {
                P1: _provider_entry([_cand("vendor/m1"), _cand("vendor/m2", rank=0.4)]),
                P2: _provider_entry([_cand("p2dyn/x")]),
            },
        )

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a"]), P2: _provider_cfg(["static/c"])},
            _dyn_section("append", snap, {P1: ["vendor/*"], P2: ["p2dyn/*"]}, max_total=1),
            provider_fallbacks={P1: [P2]},
        )

        fake = _FakeClient(
            [
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: DONE\nreviewed"),
            ]
        )

        _patch_engine(monkeypatch, cfg, fake)

        events = _collect()

        res = _run(str(tmp_path / "ws"), events, provider=P1)

        assert res["status"] == "DONE"

        # p1 burns its single dynamic slot on vendor/m1; p2's tail is capped

        # to zero by max_total_per_task — p2dyn/x must never be attempted.

        assert [c["model"] for c in fake.calls] == ["static/a", "vendor/m1", "static/c"]

        assert [a["provider"] for a in res["attempts"]] == [P1, P1, P2]

        assert [a["candidate_source"] for a in res["attempts"]] == [
            "static",
            "dynamic",
            "static",
        ]

        assert res["dynamic_candidates"]["dynamic_attempted"] == 1

        assert res["dynamic_candidates"]["providers"][P2]["dynamic_count"] == 0


class TestFailStatic:
    @pytest.mark.parametrize("kind", ["missing", "malformed", "stale", "schema"])
    def test_bad_snapshot_degrades_to_static(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, kind: str
    ):

        snap_path = tmp_path / "dyn_snap.json"

        if kind == "malformed":
            snap_path.write_text("{not json", encoding="utf-8")

        elif kind == "stale":
            _write_snapshot(
                tmp_path,
                {P1: _provider_entry([_cand("vendor/m1")])},
                generated_at=time.time() - 200 * 3600,
            )

        elif kind == "schema":
            snap_path.write_text(
                json.dumps({"schema_version": 99, "generated_at": time.time()}),
                encoding="utf-8",
            )

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a"])},
            _dyn_section("append", snap_path, {P1: ["vendor/*"]}),
        )

        monkeypatch.setattr(srv_config, "load_config", lambda *a, **k: cfg)

        res = resolve_task_model_candidates("", "default", P1)

        assert res["candidates"] == ["static/a"]

        assert res["dynamic_candidates"] == []

        assert res["snapshot_state"] != "ok"

    def test_task_still_succeeds_with_malformed_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        (tmp_path / "dyn_snap.json").write_text("{broken", encoding="utf-8")

        cfg = _make_cfg(
            {P1: _provider_cfg(["static/a"])},
            _dyn_section("append", tmp_path / "dyn_snap.json", {P1: ["vendor/*"]}),
        )

        fake = _FakeClient([_response("STATUS: DONE\nreviewed")])

        _patch_engine(monkeypatch, cfg, fake)

        res = _run(str(tmp_path / "ws"), _collect(), provider=P1)

        assert res["status"] == "DONE"

        assert res["dynamic_candidates"]["providers"][P1]["snapshot_state"] != "ok"


class TestHotReload:
    def test_mode_flip_without_restart(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        holder: Dict[str, Any] = {
            "cfg": _make_cfg(
                {P1: _provider_cfg(["static/a"])},
                _dyn_section("off", snap, {P1: ["vendor/*"]}),
            )
        }

        monkeypatch.setattr(srv_config, "load_config", lambda *a, **k: holder["cfg"])

        kw: Dict[str, Any] = {"task_class": "bugfix", "capability_mode": "readonly"}

        assert resolve_task_model_candidates("", "default", P1, **kw)["dynamic_mode"] == "off"

        holder["cfg"] = _make_cfg(
            {P1: _provider_cfg(["static/a"])},
            _dyn_section("shadow", snap, {P1: ["vendor/*"]}),
        )

        res = resolve_task_model_candidates("", "default", P1, **kw)

        assert res["dynamic_mode"] == "shadow"

        assert res["candidates"] == ["static/a"]

        assert res["would_append"] == ["vendor/m1"]

        holder["cfg"] = _make_cfg(
            {P1: _provider_cfg(["static/a"])},
            _dyn_section("append", snap, {P1: ["vendor/*"]}),
        )

        res = resolve_task_model_candidates("", "default", P1, **kw)

        assert res["dynamic_mode"] == "append"

        assert res["candidates"] == ["static/a", "vendor/m1"]

        monkeypatch.setenv("BOTEX_DYNAMIC_CANDIDATES_MODE", "off")

        real_load = dc.load_dynamic_snapshot_offline

        loads = {"n": 0}

        def _counted(*a: Any, **k: Any):

            loads["n"] += 1

            raise AssertionError("env-off must not read the snapshot")

        monkeypatch.setattr(dc, "load_dynamic_snapshot_offline", _counted)

        res = resolve_task_model_candidates("", "default", P1, **kw)

        assert res["dynamic_mode"] == "off"

        assert res["candidates"] == ["static/a"]

        assert loads["n"] == 0

        monkeypatch.setattr(dc, "load_dynamic_snapshot_offline", real_load)

        monkeypatch.setenv("BOTEX_DYNAMIC_CANDIDATES_MODE", "shadow")

        res = resolve_task_model_candidates("", "default", P1, **kw)

        assert res["dynamic_mode"] == "shadow"

        assert res["candidates"] == ["static/a"]

        assert res["would_append"] == ["vendor/m1"]


class TestOpenRouterGuard:
    def test_dynamic_never_in_extra_body_models(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        snap = _write_snapshot(tmp_path, {"openrouter": _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {
                "openrouter": {
                    "models": {"default": "static/a"},
                    "model_fallbacks": {"default": ["static/b"]},
                    "zdr": {"enabled": False},
                }
            },
            _dyn_section("append", snap, {"openrouter": ["vendor/*"]}),
        )

        fake = _FakeClient(
            [
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                _response("STATUS: DONE\nreviewed"),
            ]
        )

        _patch_engine(monkeypatch, cfg, fake)

        res = _run(str(tmp_path / "ws"), _collect(), provider="openrouter")

        assert res["status"] == "DONE"

        assert [c["model"] for c in fake.calls] == ["static/a", "static/b", "vendor/m1"]

        static_call, _, dyn_call = fake.calls

        # Static attempts may carry in-payload models; the dynamic attempt

        # must never see the key at all (and its id never appears inside).

        static_models = (static_call.get("extra_body") or {}).get("models") or []

        assert all("vendor/" not in m for m in static_models)

        dyn_extra = dyn_call.get("extra_body") or {}

        assert "vendor/m1" not in json.dumps(dyn_extra)

        assert "models" not in dyn_extra


class TestNoNetwork:
    def test_resolution_never_touches_network(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        def _boom(*a: Any, **k: Any) -> Any:

            raise AssertionError("network call during offline resolution")

        from botex.providers import http_pool

        from botex.providers.catalog import ModelCatalog

        from botex.providers.providers import BaseProvider

        monkeypatch.setattr(http_pool, "get_sync_client", _boom)

        monkeypatch.setattr(http_pool, "get_async_client", _boom)

        monkeypatch.setattr(urllib.request, "urlopen", _boom)

        monkeypatch.setattr(BaseProvider, "fetch_catalog", _boom)

        monkeypatch.setattr(ModelCatalog, "load", _boom)

        for mode in ("append", "shadow"):
            cfg = _make_cfg(
                {P1: _provider_cfg(["static/a"])},
                _dyn_section(mode, snap, {P1: ["vendor/*"]}),
            )

            monkeypatch.setattr(srv_config, "load_config", lambda *a, _c=cfg, **k: _c)

            res = resolve_task_model_candidates("", "default", P1)

            assert res["snapshot_state"] == "ok"

            if mode == "append":
                assert res["candidates"] == ["static/a", "vendor/m1"]

            else:
                assert res["candidates"] == ["static/a"]

                assert res["would_append"] == ["vendor/m1"]


class TestNightPulseAndAlias:
    def test_night_pulse_runs_dynamic_sync(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        import server

        monkeypatch.setattr(srv_config, "get_swarm_store_db_path", lambda: tmp_path / "swarm.db")

        monkeypatch.setenv("BOTEX_SWARM_HOME", str(tmp_path / "swarm_home"))

        (tmp_path / "swarm_home").mkdir(exist_ok=True)

        monkeypatch.delenv("BOTEX_CLOUD_URL", raising=False)

        monkeypatch.setattr(server, "load_config", lambda *a, **k: {"providers": {}})

        drift = {"status": "ok", "generated_at": time.time()}

        monkeypatch.setattr(server, "_run_catalog_drift_sync", lambda *a, **k: drift)

        seen: Dict[str, Any] = {}

        def _spy(cfg_arg: Any, *, drift_report: Any = None) -> Dict[str, Any]:

            seen["drift_report"] = drift_report

            return {"status": "ok", "mode": "shadow", "providers": {}, "counts": {}}

        from botex.ops import maintenance

        monkeypatch.setattr(maintenance, "run_dynamic_candidates_sync", _spy)

        res = server._execute_night_pulse_cycle()

        assert res["dynamic_candidates"]["status"] == "ok"

        assert res["dynamic_candidates"]["mode"] == "shadow"

        assert seen["drift_report"] is drift

    def test_night_pulse_sync_error_is_isolated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ):

        import server

        monkeypatch.setattr(srv_config, "get_swarm_store_db_path", lambda: tmp_path / "swarm.db")

        monkeypatch.setenv("BOTEX_SWARM_HOME", str(tmp_path / "swarm_home"))

        (tmp_path / "swarm_home").mkdir(exist_ok=True)

        monkeypatch.delenv("BOTEX_CLOUD_URL", raising=False)

        monkeypatch.setattr(server, "load_config", lambda *a, **k: {"providers": {}})

        monkeypatch.setattr(server, "_run_catalog_drift_sync", lambda *a, **k: {"status": "ok"})

        def _boom(*a: Any, **k: Any) -> Any:

            raise RuntimeError("sync exploded")

        from botex.ops import maintenance

        monkeypatch.setattr(maintenance, "run_dynamic_candidates_sync", _boom)

        res = server._execute_night_pulse_cycle()

        assert res["dynamic_candidates"]["status"] == "error"

    def test_legacy_import_alias(self):

        mod = importlib.import_module("botex.dynamic_candidates")

        assert mod is dc

    def test_check_health_dynamic_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):

        import server

        snap = _write_snapshot(tmp_path, {P1: _provider_entry([_cand("vendor/m1")])})

        cfg = _make_cfg(
            {P1: {**_provider_cfg(["static/a"]), "default": True}},
            _dyn_section("shadow", snap, {P1: ["vendor/*"]}),
        )

        monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)

        out = asyncio.run(server.check_health())

        assert "dynamic_candidates: mode=shadow" in out

        assert "active=1" in out

        cfg["dynamic_candidates"]["mode"] = "off"

        out = asyncio.run(server.check_health())

        assert "dynamic_candidates" not in out

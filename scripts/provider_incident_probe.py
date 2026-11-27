# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Opt-in operator CLI probing provider surfaces with redacted incident capture.

Plan 060 Step 1 (Worker 060-B). Uses the real BoteX provider adapters via
``botex.providers.providers`` — never hand-rolled HTTP. Every captured record
is redacted through ``botex.providers.incident_probe`` helpers (schema v1) and
contains only structural fields: URL templates, header names, status codes,
bounded masked error messages. Never secrets, prompts, patches or raw bodies.

botex imports are lazy (inside functions) so this module stays importable
offline for tests before ``incident_probe`` lands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_PROVIDERS = "groq,cloudflare"
DEFAULT_INCIDENT_PATH = ".botex/provider_incidents.jsonl"
CHAT_PROMPT = "Reply with OK."
CLOUDFLARE_FALLBACK_MODEL = "@cf/zai-org/glm-4.7-flash"
TRACK_C_CELLS = (
    "readonly_analysis",
    "trivial_one_line_edit",
    "syntax_sensitive_edit",
    "two_file_edit",
)


def _incident_probe():
    from botex.providers import incident_probe

    return incident_probe


def _load_config():
    from botex.server.config import load_config

    return load_config()


def _get_provider(name: str, cfg: dict[str, Any]):
    from botex.providers.providers import ProviderRegistry

    providers = cfg.get("providers", {})
    provider_cfg = providers.get(name)
    if not isinstance(provider_cfg, dict):
        raise SystemExit(f"provider {name!r} not configured in botex.config.json")
    return ProviderRegistry.get(name, provider_cfg), provider_cfg


def _configured_model(provider_cfg: dict[str, Any], name: str) -> str:
    models = provider_cfg.get("models") or {}
    if name == "cloudflare":
        return models.get("fast") or CLOUDFLARE_FALLBACK_MODEL
    return models.get("fast") or models.get("default") or ""


def _fallback_model(provider_cfg: dict[str, Any]) -> str:
    models = provider_cfg.get("models") or {}
    direct = models.get("default") or models.get("coding")
    if direct:
        return direct
    fallbacks = provider_cfg.get("model_fallbacks") or {}
    if isinstance(fallbacks, dict):
        for slot in ("default", "coding", "fast"):
            items = fallbacks.get(slot) or []
            for item in items:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""


def _sink_for(records: list[dict[str, Any]]) -> Callable[[dict[str, Any]], None]:
    def sink(record: dict[str, Any]) -> None:
        records.append(record)

    return sink


class Runner:
    """Collects redacted records; flushes to JSONL or stdout at the end."""

    def __init__(self, output: Path | None, no_write: bool) -> None:
        self.records: list[dict[str, Any]] = []
        self.output = output
        self.no_write = no_write
        self._sink = _sink_for(self.records)

    def emit(self, record: dict[str, Any]) -> None:
        probe = _incident_probe()
        probe.emit_incident_safe(self._sink, probe.build_incident_record(**record))

    def flush(self) -> None:
        if self.no_write:
            for record in self.records:
                print(json.dumps(record, ensure_ascii=False))
            return
        path = self.output or Path(DEFAULT_INCIDENT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"wrote {len(self.records)} incident records -> {path}", file=sys.stderr)


def run_discovery(provider_name: str, runner: Runner, timeout_s: float) -> None:
    """Track A/B discovery cell via the real adapter ``fetch_catalog``."""
    cfg = _load_config()
    provider, _cfg = _get_provider(provider_name, cfg)
    start = time.monotonic()
    exc: BaseException | None = None
    catalog: Any = None
    before_count = len(runner.records)
    try:
        try:
            # Worker 060-A adds the incident_sink kwarg; fall back if absent.
            catalog = provider.fetch_catalog(timeout=timeout_s, incident_sink=self_sink(runner))
        except TypeError:
            catalog = provider.fetch_catalog(timeout=timeout_s)
    except Exception as e:  # noqa: BLE001 - structural capture of any failure
        exc = e
    duration_ms = int((time.monotonic() - start) * 1000)
    emitted_by_adapter = len(runner.records) > before_count
    if exc is None and not emitted_by_adapter:
        # No incident_sink support and success: still record the outcome shape.
        runner.emit(
            {
                "provider": provider_name,
                "operation": "catalog_models_openai",
                "method": "GET",
                "url": str(provider.base_url).rstrip("/") + "/models",
                "headers": {"accept": "application/json", "authorization": "<auth>"},
                "response_status": 200,
                "response_body": {"data_count": len(catalog or [])},
                "duration_ms": duration_ms,
                "exception": None,
            }
        )
    if exc is not None and not emitted_by_adapter:
        runner.emit(
            {
                "provider": provider_name,
                "operation": "catalog_models_openai",
                "method": "GET",
                "url": str(provider.base_url).rstrip("/") + "/models",
                "headers": {"accept": "application/json", "authorization": "<auth>"},
                "response_status": None,
                "response_body": None,
                "duration_ms": duration_ms,
                "exception": exc,
            }
        )


def self_sink(runner: Runner):
    return runner._sink


async def _chat_cell(
    provider_name: str,
    model: str,
    runner: Runner,
    timeout_s: float,
    *,
    stream: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> None:
    cfg = _load_config()
    provider, provider_cfg = _get_provider(provider_name, cfg)
    client = provider.get_client(provider_cfg=provider_cfg)
    extra_body = provider.get_extra_body(model=model) or {}
    start = time.monotonic()
    exc: BaseException | None = None
    status: int | None = None
    body: Any = None
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": CHAT_PROMPT}],
            "max_tokens": 8,
            "timeout": timeout_s,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
        if stream:
            parts: list[str] = []
            stream_obj = await client.chat.completions.create(stream=True, **kwargs)
            async for chunk in stream_obj:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    piece = getattr(delta, "content", None)
                    if piece:
                        parts.append("x" * len(str(piece)))
            body = {"stream_chunks": len(parts), "stream_chars": len("".join(parts))}
            status = 200
        else:
            resp = await client.chat.completions.create(**kwargs)
            status = 200
            body = {"object": getattr(resp, "object", None), "model": getattr(resp, "model", None)}
    except Exception as e:  # noqa: BLE001
        exc = e
        status = getattr(e, "status_code", None)
    duration_ms = int((time.monotonic() - start) * 1000)
    runner.emit(
        {
            "provider": provider_name,
            "operation": "chat_completion",
            "model": model,
            "method": "POST",
            "url": str(provider.base_url).rstrip("/") + "/chat/completions",
            "headers": provider.get_default_headers(),
            "payload": {
                "stream": stream,
                "tools": tools,
                "max_tokens": 8,
                "message_chars": len(CHAT_PROMPT),
            },
            "response_status": status,
            "response_body": body,
            "duration_ms": duration_ms,
            "exception": exc,
        }
    )


TRIVIAL_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo one short string back.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
]


async def _run_chat_matrix_async(
    provider_name: str, model: str, runner: Runner, timeout_s: float
) -> None:
    await _chat_cell(provider_name, model, runner, timeout_s, stream=False, tools=None)
    await asyncio.sleep(_COOLDOWN_S.value)
    await _chat_cell(provider_name, model, runner, timeout_s, stream=False, tools=TRIVIAL_TOOL)
    await asyncio.sleep(_COOLDOWN_S.value)
    await _chat_cell(provider_name, model, runner, timeout_s, stream=True)


def run_chat_matrix(provider_name: str, runner: Runner, timeout_s: float) -> None:
    cfg = _load_config()
    provider_cfg = (cfg.get("providers") or {}).get(provider_name) or {}
    model = _configured_model(provider_cfg, provider_name)
    if not model:
        raise SystemExit(f"no configured model for provider {provider_name!r}")
    asyncio.run(_run_chat_matrix_async(provider_name, model, runner, timeout_s))
    if provider_name == "groq":
        time.sleep(_COOLDOWN_S.value)
        run_discovery(provider_name, runner, timeout_s)


_COOLDOWN_S = argparse.Namespace(value=1.5)


def _track_c_fixtures(workdir: Path) -> dict[str, str]:
    (workdir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (workdir / "broken.py").write_text("def f(:\n    return 1\n", encoding="utf-8")
    (workdir / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workdir / "b.txt").write_text("beta\n", encoding="utf-8")
    return {
        "readonly_analysis": "How many functions does calc.py define? Answer briefly.",
        "trivial_one_line_edit": "In calc.py, append a module-level constant PI = 3.14 on a new last line.",
        "syntax_sensitive_edit": "In broken.py, fix the file so it is syntactically valid Python.",
        "two_file_edit": "Append the digit 1 as a new last line to both a.txt and b.txt.",
    }


_TRACK_C_STRUCTURAL_KEYS = (
    "provider",
    "model",
    "cell",
    "repetition",
    "stream",
    "status",
    "failure_kind",
    "attempts",
    "steps",
    "tool_calls",
    "mutations",
    "syntax_rejections",
    "rollback_count",
    "rollback_reasons",
    "duration_ms",
)


def track_c_structural_metrics(
    provider: str, model: str, cell: str, repetition: int, result: dict[str, Any]
) -> dict[str, Any]:
    """Project a run result onto structural fields only (zero task text)."""
    raw = result if isinstance(result, dict) else {}
    return {
        "provider": provider,
        "model": model,
        "cell": cell,
        "repetition": repetition,
        "stream": False,
        "status": raw.get("status"),
        "failure_kind": raw.get("failure_kind"),
        "attempts": raw.get("attempts"),
        "steps": raw.get("steps"),
        "tool_calls": raw.get("tool_calls"),
        "mutations": raw.get("mutations"),
        "syntax_rejections": raw.get("syntax_rejections"),
        "rollback_count": raw.get("rollback_count"),
        "rollback_reasons": raw.get("rollback_reasons") or [],
        "duration_ms": raw.get("duration_ms"),
    }


def _track_c_scratch_parent() -> Path | None:
    try:
        from botex.server.config import _default_workspace_root, allowed_workspaces

        roots = allowed_workspaces()
        base = Path(roots[0]) if roots else Path(_default_workspace_root())
        parent = base / ".botex"
        parent.mkdir(parents=True, exist_ok=True)
        return parent
    except Exception:  # noqa: BLE001
        return None


async def _run_track_c_async(
    candidates: list[str],
    repetitions: int,
    max_duration_s: int,
) -> list[dict[str, Any]]:
    import inspect

    from botex.core.engine import run_botex_task

    summaries: list[dict[str, Any]] = []
    scratch_parent = _track_c_scratch_parent()
    with tempfile.TemporaryDirectory(
        prefix="botex-trackc-",
        dir=str(scratch_parent) if scratch_parent is not None else None,
    ) as tmp:
        workdir = Path(tmp)
        for model in candidates:
            for cell in TRACK_C_CELLS:
                for rep in range(1, repetitions + 1):
                    prompts = _track_c_fixtures(workdir)
                    cell_mode = "readonly" if cell == "readonly_analysis" else "edit"
                    start = time.monotonic()
                    try:
                        maybe_coro = run_botex_task(
                            task=prompts[cell],
                            workspace_dir=str(workdir),
                            model=model,
                            provider="openrouter-free",
                            mode=cell_mode,
                            max_duration_s=max_duration_s,
                            allow_non_zdr=True,
                        )
                        result = await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro
                    except Exception as e:  # noqa: BLE001
                        result = {"status": "ERROR", "failure_kind": type(e).__name__}
                    metrics = track_c_structural_metrics(
                        "openrouter-free", model, cell, rep, result
                    )
                    metrics["duration_ms"] = int((time.monotonic() - start) * 1000)
                    summaries.append(
                        {k: metrics[k] for k in _TRACK_C_STRUCTURAL_KEYS if k in metrics}
                    )
                    if _COOLDOWN_S.value > 0:
                        await asyncio.sleep(_COOLDOWN_S.value)
    return summaries


def run_track_c(
    runner: Runner,
    repetitions: int,
    fallback_model: str | None,
    max_duration_s: int = 120,
) -> list[dict[str, Any]]:
    cfg = _load_config()
    or_cfg = (cfg.get("providers") or {}).get("openrouter-free") or {}
    lightning = or_cfg.get("models", {}).get("fast") or ""
    candidates: list[str] = []
    if lightning:
        candidates.append(lightning)
    fb = fallback_model or _fallback_model(or_cfg)
    if fb and fb not in candidates:
        candidates.append(fb)

    return asyncio.run(_run_track_c_async(candidates, repetitions, max_duration_s))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in redacted provider incident probe (Plan 060 Step 1)."
    )
    parser.add_argument("--providers", default=DEFAULT_PROVIDERS)
    parser.add_argument("--include-chat", action="store_true")
    parser.add_argument("--track-c", action="store_true")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--fallback-model", default=None)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--output", default=DEFAULT_INCIDENT_PATH)
    parser.add_argument("--cooldown-s", type=float, default=1.5)
    parser.add_argument("--timeout-s", type=float, default=20.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _COOLDOWN_S.value = args.cooldown_s
    runner = Runner(output=Path(args.output), no_write=args.no_write)

    for provider_name in [p.strip() for p in args.providers.split(",") if p.strip()]:
        run_discovery(provider_name, runner, args.timeout_s)
        if args.include_chat:
            time.sleep(args.cooldown_s)
            run_chat_matrix(provider_name, runner, args.timeout_s)
        time.sleep(args.cooldown_s)

    track_c_summary: list[dict[str, Any]] = []
    if args.track_c:
        track_c_summary = run_track_c(runner, args.repetitions, args.fallback_model)

    runner.flush()
    if track_c_summary:
        print(
            json.dumps(
                {"track_c": track_c_summary, "cells": len(track_c_summary)},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

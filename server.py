# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Autonomous Code Worker — Standalone MCP Server
====================================================
Agent-agnostic Model Context Protocol (MCP) server for the BoteX execution
harness. Exposes an autonomous code-editing agent with outline-first I/O,
in-memory syntax validation, fuzzy patching, snapshot rollbacks, and quota
optimization to any MCP-compatible client.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional

from typing_extensions import TypedDict

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure base directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from botex.analytics import cli_history, cli_stats, load_analytics
from botex.benchmarks import get_recommended_models
from botex.chat import (
    default_chat_manager,
    stream_chat_turn,
)
from botex.config import (
    load_config,
    resolve_provider,
    resolve_workspace,
    resolve_workspace_source,
    set_local_config,
)
from botex.context_guard import read_scratchpad as _read_scratchpad
from botex.engine import run_botex_task
from botex.file_tools import get_file_outline as _get_file_outline
from botex.i18n import SUPPORTED, init_language, set_language, t
from botex.memory import (
    delete_memory as _delete_memory,
)
from botex.memory import (
    find_graph_health as _find_graph_health,
)
from botex.memory import (
    get_backlinks as _get_backlinks,
)
from botex.memory import (
    list_memories as _list_memories,
)
from botex.memory import (
    patch_memory_section as _patch_memory_section,
)
from botex.memory import (
    read_memory as _read_memory,
)
from botex.memory import (
    save_memory as _save_memory,
)
from botex.memory import (
    search_memory as _search_memory,
)
from botex.net_tools import fetch_url as _fetch_url
from botex.obsidian import (
    export_graph_index_markdown as _export_graph_index_markdown,
)
from botex.obsidian import (
    init_obsidian_vault as _init_obsidian_vault,
)
from botex.patch_engine import snapshot_manager
from botex.pricing import load_catalog, model_price
from botex.providers import ProviderRegistry
from botex.security import coerce_text, mask_secrets, wrap_untrusted_content, zdr_enforced
from botex.ui import accent, dim, err, mark_status, ok, title, warn

mcp = MCPServer("BoteX-Harness")


class SubagentAttemptResult(TypedDict, total=False):
    """One model-candidate outcome inside the fallback chain."""

    model: str
    task_id: str
    status: str
    failure_kind: str
    duration_s: float
    cost_usd: float
    rolled_back: bool
    rollback_verified: bool
    zdr_enforced: bool


class SubagentTaskResult(TypedDict, total=False):
    """Structured result of a finished BoteX run.

    `status` is one of the engine statuses (DONE, INCOMPLETE, UNSUPPORTED,
    API_ERROR, REQUEST_TIMEOUT, TOKEN_LIMIT, TIME_LIMIT, MAX_TURNS_REACHED,
    STAGNANT_ROLLBACK, VERIFICATION_FAILED, PRICE_EXCEEDED, BUDGET_EXCEEDED,
    CONFIG_ERROR, ERROR); `failure_kind` carries the semantic cause
    (model_refusal, model_tool_misuse, no_progress, capability_missing,
    provider_auth_error, provider_policy_denied, provider_rate_limited,
    provider_error, ...).
    `ok` is true only when the task contract was fulfilled (status == DONE).
    """

    ok: bool
    task_id: str
    status: str
    failure_kind: str
    message: str
    summary: str
    files_touched: list[str]
    exec_ran: bool
    rollback_verified: bool
    steps: int
    duration_s: float
    cost_usd: float
    lines_added: int
    lines_removed: int
    attempts: list[SubagentAttemptResult]
    fallback_from: str
    zdr_enforced: bool
    vault_memory_id: Optional[str]
    applied_skills: list[str]
    artifact_refs: list[str]
    memory_keys: list[str]
    blocking_reason: Optional[str]


def mask_secrets_recursive(val: Any) -> Any:
    """Recursively mask secrets across dictionaries, lists, and strings."""
    if isinstance(val, str):
        return mask_secrets(val)
    elif isinstance(val, dict):
        return {k: mask_secrets_recursive(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [mask_secrets_recursive(item) for item in val]
    return val


def _masked_result(res: dict) -> dict:
    """Copy an engine result with secret patterns recursively redacted."""
    return mask_secrets_recursive(res)


REPORT_OFFLOAD_BYTES = 2048
_REPORT_ABSTRACT_CHARS = 400


def _offload_report(res: dict, workspace: str) -> dict:
    """Context-Guard: when summary exceeds 2 KB, store the full (masked)
    report in the Memory Vault as ``mem_task_<id>_report`` and return a copy
    whose summary/message are a compact abstract + ``vault_memory_id``."""
    summary = res.get("summary") or res.get("message") or ""
    if not isinstance(summary, str) or len(summary.encode("utf-8")) <= REPORT_OFFLOAD_BYTES:
        return res
    if not load_config().get("context_guard", {}).get("enabled", True):
        return res
    safe_id = "".join(
        c if (c.isalnum() or c in "_-") else "_" for c in str(res.get("task_id") or "unknown")
    )
    mem_id = f"mem_task_{safe_id}_report"
    saved = _save_memory(
        workspace_dir=workspace,
        title=f"Task {res.get('task_id')} audit report",
        content=wrap_untrusted_content(
            summary, tag="untrusted_command_output", source="subagent_report"
        ),
        kind="context",
        tags=["ephemeral", "context_guard", "task_report"],
        memory_id=mem_id,
        origin="context_guard:run_subagent",
    )
    if not saved.get("ok"):
        return res  # offload failed — keep the full inline report
    abstract = summary[:_REPORT_ABSTRACT_CHARS].rstrip()
    compact = (
        f"{abstract}…\n[Report truncated; full text in Memory Vault: "
        f"'{mem_id}'. read_memory(memory_id='{mem_id}')]"
    )
    out = dict(res)
    out["summary"] = compact
    if isinstance(out.get("message"), str):
        out["message"] = compact
    out["vault_memory_id"] = mem_id
    return out


def _format_result_text(res: dict) -> str:
    """Human-readable task report (kept as TextContent for legacy clients).
    When the report was offloaded to the Memory Vault this is a compact
    Task Scorecard (< 1 KB) with a read_memory pointer."""
    status_str = "SUCCESS" if res.get("ok") else "FAILURE"
    files = list(res.get("files_touched", []))
    offloaded = res.get("vault_memory_id")
    if offloaded and len(files) > 5:
        files = files[:5] + [f"... (+{len(res.get('files_touched', [])) - 5} more)"]
    files_str = ", ".join(files) or "(none)"
    cost_str = f"${res.get('cost_usd', 0.0):.5f} USD"
    summary_text = res.get("summary") or res.get("message", "")
    lines = [
        f"[{status_str}] BoteX finished task '{res.get('task_id')}' in {res.get('steps', 0)} steps.",
        f"- Status: {res.get('status')}",
    ]
    if res.get("failure_kind"):
        lines.append(f"- Failure kind: {res['failure_kind']}")
    attempts = res.get("attempts") or []
    if len(attempts) > 1:
        chain = " -> ".join(f"{a.get('model')}({a.get('status')})" for a in attempts)
        lines.append(f"- Model chain: {chain}")
    if res.get("rollback_verified") is False:
        lines.append("- WARNING: rollback could not be verified — workspace may be dirty")
    if res.get("zdr_enforced") is False:
        lines.append("- Privacy: non-ZDR — provider may log or train on prompts")
    lines += [
        f"- Duration: {res.get('duration_s', 0)}s | Cost: {cost_str}",
        f"- Modified files: {files_str}",
        f"- Line balance: +{res.get('lines_added', 0)} / -{res.get('lines_removed', 0)}",
    ]
    if res.get("applied_skills"):
        lines.append(f"- Applied skills: {', '.join(res['applied_skills'])}")
    if offloaded:
        lines.append(
            "- Full audit report saved to Memory Vault. "
            f"Run read_memory(memory_id='{offloaded}') for complete diff."
        )
        return "\n".join(lines)
    lines.append(f"\nChange summary:\n{summary_text}")
    return "\n".join(lines)


_TASKS: dict[str, dict] = {}
_TASK_SEMAPHORE = asyncio.Semaphore(
    int(load_config().get("engine", {}).get("max_concurrent_tasks", 3))
)


async def _background_task(task_id: str, kwargs: dict) -> None:
    entry = _TASKS[task_id]
    entry["status"] = "RUNNING"
    try:
        async with _TASK_SEMAPHORE:
            tool_result = await run_subagent(**kwargs)
            # `result` keeps the legacy pretty-text report; `result_data`
            # carries the full structured engine result.
            entry["result"] = (
                mask_secrets(tool_result.content[0].text) if tool_result.content else ""
            )
            entry["result_data"] = mask_secrets_recursive(tool_result.structured_content or {})
            entry["status"] = "DONE"
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        raise
    except Exception as exc:
        entry["status"] = "FAILED"
        entry["result"] = f"Background task failed: {mask_secrets(str(exc))[:500]}"
    finally:
        entry["finished_at"] = time.time()


def _cleanup_tasks() -> None:
    ttl = int(load_config().get("engine", {}).get("task_result_ttl_s", 3600))
    now = time.time()
    for task_id, entry in list(_TASKS.items()):
        fin = entry.get("finished_at") or entry.get("created_at") or 0
        if entry.get("status") in {"DONE", "FAILED", "CANCELLED"} and (now - fin) > ttl:
            _TASKS.pop(task_id, None)


# ---------------------------------------------------------------------------
# Public MCP Tools (Exposed to MCP clients)
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_task(
    task: str,
    files: list[str] = [],
    workspace_dir: str = "",
    model: str = "",
    profile: str = "",
    provider: str = "",
    mode: str = "",
    max_turns: int = 0,
    max_tokens: int = 0,
    max_duration_s: int = 0,
    budget_limit_usd: float = -1.0,
    allow_destructive: bool = False,
    allow_exec: bool = False,
    allow_net: bool = False,
    net_allowed_hosts: list[str] = [],
    net_allowed_urls: list[str] = [],
    api_key: str = "",
    output_path: str = "",
    verify_command: str = "",
    recipe: str = "",
    allow_non_zdr: bool = False,
) -> dict[str, Any]:
    """Start a detached BoteX task and return its task_id immediately.

    Runs the same engine as `run_subagent`, but returns instantly with a
    `task_id` for polling via `get_task_status`. Concurrency is bounded by
    engine.max_concurrent_tasks and the queue by engine.max_queued_tasks —
    a full queue returns status TOO_MANY_QUEUED.

    The capability/limits arguments are identical to `run_subagent` —
    same contracts, same pre-authorization semantics:

    - mode: capability preset — 'readonly' (read tools only; the contract
      is an analysis report), 'edit' (read + write; default), 'destructive'
      (+ delete/move), 'full' (+ run_command). Empty = engine.default_mode.
    - allow_destructive / allow_exec / allow_net only ever WIDEN the
      preset, never narrow it — a readonly task cannot gain file writes.
      allow_exec additionally requires exec.enabled=true in the config.
      None of these is a sandbox: grant them only with the user's consent.
    - output_path makes the contract file_output: DONE requires the file
      to exist, be non-empty, and pass the syntax gate (mutating mode).
    - verify_command is an allowlisted command that must pass before DONE
      is accepted — it runs even when the task made no writes. Requires
      exec authorization.
    - recipe: Optional operational persona / workflow prompt (e.g. 'planner',
      'code-explorer', 'reviewer', 'security-reviewer', 'build-resolver', 'tdd').
    - max_turns / max_tokens / max_duration_s / budget_limit_usd:
      0 (negative for budget) = config value; a non-positive max_turns is
      clamped to 1.
    - net_allowed_hosts / net_allowed_urls narrow or replace the net
      scope per run according to net.policy (see run_subagent).
    - allow_non_zdr: per-run consent to relax Zero Data Retention — needed
      for free (:free) models whose endpoints may log or train on prompts.
      Same semantics as in run_subagent.

    Returns:
        {"task_id": str, "status": "QUEUED"} — poll with get_task_status.
        The finished task's `result_data` carries the full engine result
        (status, failure_kind, ok, files_touched, exec_ran,
        rollback_verified, attempts[], cost_usd, ...).
    """
    _cleanup_tasks()
    max_queued = int(load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(entry.get("status") in {"QUEUED", "RUNNING"} for entry in _TASKS.values())
    if active_count >= max_queued:
        return {
            "ok": False,
            "status": "TOO_MANY_QUEUED",
            "message": f"Task queue limit reached ({max_queued}).",
        }
    task_id = uuid.uuid4().hex[:12]
    kwargs = {
        "task": task,
        "files": files,
        "workspace_dir": resolve_workspace(workspace_dir),
        "model": model,
        "profile": profile,
        "provider": provider,
        "mode": mode,
        "max_turns": max_turns,
        "max_tokens": max_tokens,
        "max_duration_s": max_duration_s,
        "budget_limit_usd": budget_limit_usd,
        "allow_destructive": allow_destructive,
        "allow_exec": allow_exec,
        "allow_net": allow_net,
        "net_allowed_hosts": net_allowed_hosts,
        "net_allowed_urls": net_allowed_urls,
        "api_key": api_key,
        "output_path": output_path,
        "verify_command": verify_command,
        "recipe": recipe,
        "allow_non_zdr": allow_non_zdr,
    }
    task_obj = asyncio.create_task(_background_task(task_id, kwargs))
    _TASKS[task_id] = {
        "status": "QUEUED",
        "created_at": time.time(),
        "task": task_obj,
    }
    return {"task_id": task_id, "status": "QUEUED"}


@mcp.tool()
async def get_task_status(task_id: str) -> dict[str, Any]:
    """Return status and result for a task started with start_task.

    `status` is the task lifecycle (QUEUED | RUNNING | DONE | FAILED |
    CANCELLED) — DONE here means "finished", not "succeeded"; check
    `result_data.status` / `result_data.ok` for the engine outcome.
    `result` is the pretty-text report; `result_data` is the structured
    engine result (status, failure_kind, ok, files_touched, exec_ran,
    rollback_verified, attempts[], cost_usd, ...).
    """
    _cleanup_tasks()
    entry = _TASKS.get(task_id)
    if not entry:
        return {"ok": False, "status": "NOT_FOUND", "task_id": task_id}
    result: dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "status": entry["status"],
    }
    if entry.get("status") in {"DONE", "FAILED", "CANCELLED"}:
        # `result` is the pretty-text report (legacy); `result_data` is the
        # structured engine result (status, failure_kind, attempts, ...).
        result["result"] = entry.get("result", "")
        if entry.get("result_data"):
            result["result_data"] = entry["result_data"]
    return result


@mcp.tool()
async def fetch_url(url: str, max_bytes: int = 0) -> dict[str, Any]:
    """
    Fetch one public http(s) text document for caller-side research.

    This tool is intentionally separate from BoteX's internal read_url: the
    orchestrating agent chooses the URL, so arbitrary public hosts do not have
    to be configured in net.allowed_hosts. SSRF protections, redirect
    revalidation, content-type checks, byte limits, and secret masking still
    apply. In net.policy='allowlist', only configured hosts may be fetched.
    """
    net_cfg = load_config().get("net", {})
    return await _fetch_url(
        url,
        net_cfg=net_cfg,
        max_bytes=max_bytes or None,
    )


@mcp.tool()
async def recommend_models(
    task_type: str = "coding",
    limit: int = 5,
    include_free: bool = False,
    provider: str = "",
    profile: str = "",
    tier: str = "auto",
    recipe: str = "",
) -> str:
    """
    [EN] Fetches the most cost-effective and capable models from OpenRouter based on live benchmarks.
    USE THIS TOOL BEFORE calling `run_subagent` if you are unsure which model ID to use or want to optimize for cost/quality.
    include_free=True also admits ':free' variants (non-ZDR — prompts may be
    logged/trained on); they require allow_non_zdr or the openrouter-free
    provider to actually run.

    [PL] Pobiera rekomendacje modeli z OpenRouter na podstawie benchmarków.
    Użyj tego narzędzia ZANIM wywołasz `run_subagent`, jeśli nie znasz dokładnego ID modelu.
    include_free=True dopuszcza warianty ':free' (non-ZDR — prompty mogą być
    logowane/trenowane); do uruchomienia wymagają allow_non_zdr albo
    providera openrouter-free.
    """
    results = get_recommended_models(
        task_type,
        limit,
        include_free=include_free,
        provider=provider or "openrouter",
        profile=profile or None,
        tier=tier or "auto",
        recipe=recipe or None,
    )
    return json.dumps(results, indent=2)


@mcp.tool()
async def run_subagent(
    task: str,
    files: list[str] = [],
    workspace_dir: str = "",
    model: str = "",
    profile: str = "",
    provider: str = "",
    mode: str = "",
    max_turns: int = 0,
    max_tokens: int = 0,
    max_duration_s: int = 0,
    budget_limit_usd: float = -1.0,
    allow_destructive: bool = False,
    allow_exec: bool = False,
    api_key: str = "",
    output_path: str = "",
    allow_net: bool = False,
    net_allowed_hosts: list[str] = [],
    net_allowed_urls: list[str] = [],
    verify_command: str = "",
    recipe: str = "",
    allow_non_zdr: bool = False,
    approval_policy: str = "",
) -> Annotated[CallToolResult, SubagentTaskResult]:
    """
    Runs BoteX — an autonomous code execution engine (agent-agnostic harness).
    BoteX performs multi-step tasks on local files inside workspace_dir.

    TIP: If the user didn't specify a model, DO NOT guess. Use the `recommend_models` tool first to find the best model for this task!

    Key engine features:
    - Outline-First and Context Pruning: ~90% token savings.
    - Pre-write Syntax Check: in-memory code validation before touching disk.
    - Multi-tier Fuzzy Patching: tolerant of CRLF/LF and indentation drift.
    - Hard Zero-Retention on OpenRouter (provider: data_collection=deny)
      and DLP secret censoring.
    - Automatic Snapshot Rollback on loops or critical failures.

    Args:
        task: Precise description of the coding or refactoring task.
        files: Optional list of primary files affected by the task.
        workspace_dir: Project directory path. Empty = engine.default_workspace
               from the config, else the server's current directory.
        model: Any model from the provider's catalog. Empty = model from config
               (see 'profile' and botex.config.json).
        profile: Model profile from botex.config.json (e.g. 'default', 'coding',
               'auto-beta', 'fast'). When both 'model' and 'profile' are empty,
               the capability mode picks the tier via engine.mode_profiles
               (readonly -> 'fast', destructive/full -> 'coding'). Ignored when
               'model' is given explicitly.
        provider: API provider from the 'providers' section of botex.config.json
               ('openrouter', 'nvidia', 'google', 'openrouter-free', 'ollama').
               Empty = provider marked 'default: true' in the configuration.
        mode: Capability preset: 'readonly' (read only — the contract is an
               analysis report, DONE requires non-empty findings), 'edit'
               (read + edit — default), 'destructive' (+delete/move),
               'full' (+run_command). Empty = engine.default_mode from
               config. Explicit allow_* flags may only WIDEN a preset —
               they never narrow it (readonly can never gain file writes).
               In headless mode this is pre-authorization — grant it
               consciously.
        max_turns: Maximum tool-loop steps (0 = config value).
        max_tokens: Per-turn completion cap (0 = config value; reasoning
               models need ~8000+ since thinking shares this budget).
        max_duration_s: Total wall-clock limit in seconds (0 = config value,
               0 disables only when config is also 0). Checked between turns.
        budget_limit_usd: Daily spend limit in USD (negative = config value,
               0 = no limit).
        allow_destructive: Authorize destructive operations (delete_file/
               move_file) for this task. Headless mode cannot confirm
               mid-run — grant ONLY with the user's consent.
        allow_exec: Authorize run_command for this task (also requires
               exec.enabled=true in config). WARNING: this is NOT a sandbox —
               commands run with operator privileges. Grant only for trusted
               workspaces with the user's consent.
        api_key: Optional provider API key passed per-request (highest
               priority — overrides env/.env/config). Empty = resolved
               internally by the harness.
        output_path: Optional required output file (workspace-relative).
               Sets the file_output contract: DONE is accepted only when
               the file exists on disk, is non-empty, and passes the
               syntax gate; a DONE response carrying the payload as text
               is salvaged to disk. Requires a mutating mode.
        allow_net: Explicitly authorizes the read-only public web tool for
               this run. It is never enabled by a capability mode.
        net_allowed_hosts: Optional per-run host authorization. In
               net.policy='caller' these replace config defaults; in 'public'
               they narrow public access; in 'allowlist' they must stay inside
               net.allowed_hosts.
        net_allowed_urls: Optional per-run URL authorization rules. A URL
               ending in '/' authorizes that subtree; otherwise it authorizes
               the exact URL including its query string.
        verify_command: Optional allowlisted command that must pass before
               DONE is accepted — it runs against the workspace as it
               stands, including when the task made no writes (a passing
               verifier validates a legitimate no-change result). Requires
               exec.enabled=true in config and exec authorization.
        recipe: Optional operational persona / workflow prompt (e.g. 'planner',
               'code-explorer', 'reviewer', 'security-reviewer', 'build-resolver', 'tdd').
        allow_non_zdr: Per-run consent to relax the Zero Data Retention
               gate. Required for free (:free) models whose endpoints may
               log or train on prompts — it also switches the wire
               provider.data_collection flag to 'allow'. Grant only with
               the operator's consent; the result carries
               zdr_enforced=false. The openrouter-free provider preset is
               the persistent config-level alternative.
        approval_policy: Operator approval policy for risky operations: 'none',
               'high_risk' (default), 'mutations', 'all_risky'.

    Returns:
        A pretty-text report in the text content (unchanged format for legacy
        clients) plus the full engine result as structuredContent (status,
        failure_kind, ok, files_touched, exec_ran, rollback_verified,
        attempts[], cost_usd, ...). `ok` is true only when status == DONE —
        i.e. the task contract was verified, not merely claimed by the model.
    """
    res = await run_botex_task(
        task=task,
        files=files,
        workspace_dir=workspace_dir,
        model=model,
        profile=profile,
        provider=provider,
        mode=mode,
        max_turns=max_turns or None,
        max_tokens=max_tokens or None,
        max_duration_s=max_duration_s or None,
        budget_limit_usd=budget_limit_usd if budget_limit_usd >= 0 else None,
        allow_destructive=True if allow_destructive else None,
        allow_exec=True if allow_exec else None,
        api_key=api_key,
        output_path=output_path,
        allow_net=True if allow_net else None,
        net_allowed_hosts=net_allowed_hosts or None,
        net_allowed_urls=net_allowed_urls or None,
        verify_command=verify_command,
        recipe=recipe,
        allow_non_zdr=True if allow_non_zdr else None,
        approval_policy=approval_policy or None,
    )

    res = _masked_result(res)
    res = _offload_report(res, resolve_workspace(workspace_dir))
    return CallToolResult(
        content=[TextContent(type="text", text=_format_result_text(res))],
        structured_content=res,
    )


@mcp.tool()
async def get_outline(path: str, workspace_dir: str = "") -> dict[str, Any]:
    """
    Returns a lightweight file skeleton (classes, methods, functions, headers,
    line numbers) without reading the whole file. Saves ~90% of tokens.
    """
    return _get_file_outline(path, resolve_workspace(workspace_dir))


@mcp.tool()
async def save_memory(
    title: str,
    content: str,
    workspace_dir: str = "",
    kind: str = "context",
    tags: Optional[list[str]] = None,
    memory_id: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Saves a persistent context note, architectural decision, task handoff, or lesson
    into the workspace or global shared Memory Vault.
    Masks secrets and provides directory isolation.

    Args:
        title: Short descriptive title of the memory entry.
        content: Detailed markdown notes, architectural rationale, or handoff context.
        workspace_dir: Workspace root directory. Empty = engine.default_workspace
               from the config, else the server's current directory.
        kind: Entry category ('context' | 'decision' | 'handoff' | 'lesson').
        tags: Optional list of keyword tags for filtering and discovery.
        memory_id: Optional custom identifier (alphanumeric/hyphen/underscore). Auto-generated if omitted.
        scope: Storage scope ('local' for workspace, 'global' for cross-workspace). Default is 'local'.
    """
    return _save_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        title=title,
        content=content,
        kind=kind,
        tags=tags,
        memory_id=memory_id or None,
        scope=scope,
    )


@mcp.tool()
async def search_memory(
    workspace_dir: str = "",
    query: str = "",
    tag: str = "",
    kind: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Searches the Memory Vault by keyword query, tag, or entry kind
    (context, decision, handoff, lesson) across local, global, or all scopes.

    Args:
        workspace_dir: Workspace root directory. Empty = engine.default_workspace
               from the config, else the server's current directory.
        query: Free-text search string matched against title, content, and tags.
        tag: Filter by specific tag.
        kind: Filter by entry kind ('context', 'decision', 'handoff', 'lesson').
        scope: Search scope ('local', 'global', or 'all'). Default is 'local'.
    """
    return _search_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        query=query,
        tag=tag or None,
        kind=kind or None,
        scope=scope,
    )


@mcp.tool()
async def read_memory(
    memory_id: str,
    workspace_dir: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Reads the full content and metadata of a specific memory entry by ID from the Memory Vault.

    Args:
        memory_id: The ID of the memory entry to retrieve (e.g. 'mem_a1b2c3d4').
        workspace_dir: Workspace root directory. Empty = engine.default_workspace
               from the config, else the server's current directory.
        scope: Lookup scope ('local', 'global', or 'all'). Default is 'local'.
    """
    return _read_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        memory_id=memory_id,
        scope=scope,
    )


@mcp.tool()
async def delete_memory(
    memory_id: str,
    workspace_dir: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Deletes a specific memory entry by ID from the workspace or global Memory Vault
    and removes its index/graph links.

    Args:
        memory_id: The ID of the memory entry to delete (e.g. 'mem_a1b2c3d4').
        workspace_dir: Workspace root directory. Empty = engine.default_workspace
               from the config, else the server's current directory.
        scope: Lookup scope ('local', 'global', or 'all'). Default is 'local'.
    """
    return _delete_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        memory_id=memory_id,
        scope=scope,
    )


@mcp.tool()
async def read_scratchpad(
    memory_id: str,
    workspace_dir: str = "",
) -> dict[str, Any]:
    """
    Reads the full execution or terminal log offloaded to the scratchpad (.botex/scratchpad/<memory_id>.log).

    Args:
        memory_id: The ID of the scratchpad entry (e.g. 'mem_exec_a1b2c3d4').
        workspace_dir: Workspace root directory. Empty = engine.default_workspace
               from the config, else the server's current directory.
    """
    return _read_scratchpad(
        memory_id=memory_id,
        cwd=resolve_workspace(workspace_dir),
    )


@mcp.tool()
async def get_backlinks(
    memory_id: str,
    workspace_dir: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Finds all memories that link TO the given memory_id via [[wikilinks]].

    Args:
        memory_id: Target memory ID (e.g. 'mem_a1b2c3d4').
        workspace_dir: Workspace root directory.
        scope: Lookup scope ('local', 'global', or 'all').
    """
    return _get_backlinks(
        workspace_dir=resolve_workspace(workspace_dir),
        memory_id=memory_id,
        scope=scope,
    )


@mcp.tool()
async def patch_memory_section(
    memory_id: str,
    section: str = "Compiled Truth",
    new_content: str = "",
    workspace_dir: str = "",
    scope: str = "local",
    append: bool = False,
) -> dict[str, Any]:
    """
    Surgically updates a Markdown section (e.g. '## Compiled Truth') in a memory note
    without modifying frontmatter, other sections, or the Timeline audit trail.

    Args:
        memory_id: Target memory ID to patch.
        section: Section heading to replace or create (default 'Compiled Truth').
        new_content: New Markdown text for the section.
        workspace_dir: Workspace root directory.
        scope: Lookup scope ('local' or 'global').
        append: Whether to append to existing section content instead of replacing.
    """
    return _patch_memory_section(
        workspace_dir=resolve_workspace(workspace_dir),
        memory_id=memory_id,
        section=section,
        new_content=new_content,
        scope=scope,
        append=append,
    )


@mcp.tool()
async def find_graph_health(
    workspace_dir: str = "",
    scope: str = "local",
) -> dict[str, Any]:
    """
    Audits Memory Vault knowledge graph integrity: reports broken links, orphan notes, and cycles.

    Args:
        workspace_dir: Workspace root directory.
        scope: Lookup scope ('local', 'global', or 'all').
    """
    return _find_graph_health(
        workspace_dir=resolve_workspace(workspace_dir),
        scope=scope,
    )


@mcp.tool()
async def init_obsidian_vault(
    workspace_dir: str = "",
    scope: str = "local",
    override_existing: bool = False,
    export_index: bool = True,
) -> dict[str, Any]:
    """
    Initializes or syncs Obsidian PKM vault configuration (.obsidian/graph.json, app.json)
    with preset color profiles for #decision, #lesson, #handoff, #context.

    Args:
        workspace_dir: Workspace root directory.
        scope: Target scope ('local' or 'global').
        override_existing: Whether to overwrite existing .obsidian config files.
        export_index: Also generate dynamic _GRAPH_INDEX.md note.
    """
    ws = resolve_workspace(workspace_dir)
    res = _init_obsidian_vault(workspace_dir=ws, scope=scope, override_existing=override_existing)
    if export_index and res.get("ok"):
        _export_graph_index_markdown(workspace_dir=ws, scope=scope)
    return res


@mcp.tool()
async def get_stats(period: str = "week") -> str:
    """
    Returns an analytics summary of executed BoteX tasks (USD costs, token
    usage, code-line balance).
    Args:
        period: 'week' (last 7 days) or 'month' (last 30 days).
    """
    days = 30 if period == "month" else 7
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    records = [r for r in load_analytics() if r.get("timestamp", "") >= cutoff]
    if not records:
        return f"No BoteX analytics data recorded in the last {days} days."

    total_runs = len(records)
    total_cost = sum(r.get("cost_usd", 0.0) for r in records)
    total_prompt = sum(r.get("prompt_tokens", 0) for r in records)
    total_comp = sum(r.get("completion_tokens", 0) for r in records)
    total_cached = sum(r.get("cached_tokens", 0) for r in records)
    total_added = sum(r.get("lines_added", 0) for r in records)
    total_removed = sum(r.get("lines_removed", 0) for r in records)

    by_model: dict[str, dict] = {}
    for r in records:
        m = r.get("model", "other")
        if m not in by_model:
            by_model[m] = {"runs": 0, "done": 0, "cost": 0.0, "failures": {}}
        by_model[m]["runs"] += 1
        if r.get("status") == "DONE":
            by_model[m]["done"] += 1
        else:
            fk = r.get("failure_kind") or r.get("status") or "unknown"
            by_model[m]["failures"][fk] = by_model[m]["failures"].get(fk, 0) + 1
        by_model[m]["cost"] += r.get("cost_usd", 0.0)

    model_lines = []
    for m, s in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
        rate = (s["done"] / s["runs"] * 100) if s["runs"] > 0 else 0.0
        top_f = (
            f" (top failure: {max(s['failures'].items(), key=lambda x: x[1])[0]})"
            if s["failures"]
            else ""
        )
        model_lines.append(
            f"  * {m}: {s['runs']} runs | DONE rate: {rate:.1f}%{top_f} | ${s['cost']:.4f} USD"
        )

    models_section = ("\n\nModel Efficiency:\n" + "\n".join(model_lines)) if model_lines else ""

    return (
        f"=== BOTEX ANALYTICS ({period.upper()}) ===\n"
        f"- Total tasks: {total_runs}\n"
        f"- Total cost: ${total_cost:.4f} USD\n"
        f"- Prompt tokens: {total_prompt:,}\n"
        f"- Completion tokens: {total_comp:,}\n"
        f"- Cached tokens: {total_cached:,}\n"
        f"- Line balance: +{total_added:,} / -{total_removed:,}"
        f"{models_section}"
    )


@mcp.tool()
async def check_health() -> str:
    """Checks BoteX environment readiness (provider keys, snapshot dir, disk)."""
    cfg = load_config()
    secrets = cfg.get("secrets", {})
    key_lines = []
    for pname, pcfg in cfg.get("providers", {}).items():
        env_name = pcfg.get("api_key_env") or f"{pname.upper()}_API_KEY"
        marker = " [default]" if pcfg.get("default") else ""
        # Local endpoints (e.g. Ollama / Bielik) need no external API key —
        # a static api_key_fallback or `local: true` flag is sufficient.
        if pcfg.get("local") or pcfg.get("api_key_fallback"):
            key_lines.append(f"[OK] {pname}{marker}: local endpoint active")
            continue
        try:
            prov_adapter = ProviderRegistry.get(pname, pcfg)
            resolved = prov_adapter.resolve_api_key(secrets_cfg=secrets, provider_cfg=pcfg)
            has_key = bool(resolved)
        except Exception:
            has_key = bool(os.environ.get(env_name) or secrets.get(f"{pname}_api_key"))
        key_lines.append(
            f"[{'OK' if has_key else 'MISSING'}] {pname}{marker}: {env_name} "
            f"{'configured' if has_key else '(check .env)'}"
        )
    keys_report = "\n".join(key_lines) or "[MISSING] No providers configured"
    snaps = snapshot_manager.list_snapshots()
    total_mb = sum(s["size_kb"] for s in snaps) / 1024
    def_pname, def_pcfg = resolve_provider()
    zdr_state = "ACTIVE" if zdr_enforced(def_pcfg) else "OFF — provider may log/train on prompts"

    return (
        "=== BOTEX HARNESS HEALTH CHECK ===\n"
        f"{keys_report}\n"
        f"[OK] Snapshot directory: active ({len(snaps)} backups, {total_mb:.2f} MB)\n"
        f"[OK] Path Traversal protection: ACTIVE | "
        f"Zero-Retention ({def_pname}): {zdr_state}\n"
        "[SUCCESS] BoteX engine is fully ready for autonomous work on disk!"
    )


@mcp.tool()
async def clean_snapshots(max_age_days: float = 7.0, max_total_mb: float = 50.0) -> str:
    """Purges old or excess backup snapshots from disk."""
    res = snapshot_manager.clean_old_snapshots(
        max_age_days=max_age_days,
        max_total_mb=max_total_mb,
        protected_task_ids=set(snapshot_manager.active_task_ids),
    )
    return (
        f"[OK] Purged old snapshots: removed {res['deleted_dirs']} folders, "
        f"freed {res['freed_mb']} MB. Remaining: {res['remaining_mb']} MB."
    )


# ---------------------------------------------------------------------------
# CLI Runner
# ---------------------------------------------------------------------------
def _interactive_confirm(
    operation: str,
    path: str = "",
    params: Optional[dict] = None,
    task_id: str = "",
) -> bool:
    """Prompt the terminal operator before a destructive or gated file/command operation."""
    try:
        from botex.approval import TaskAbortedError, default_approval_manager, format_action_preview
    except (ImportError, ValueError):
        from approval import TaskAbortedError, default_approval_manager, format_action_preview

    if params:
        preview = format_action_preview(operation, params)
        print(f"\n{warn('[BoteX Action Confirmation Required]')}\n{preview}")
    else:
        print(warn(f"\n[BoteX] {t('confirm.req')} '{operation}' {t('confirm.on')} '{path}'. "))

    prompt_str = (
        f"{accent('[y]')} {t('confirm.ask')} / {accent('[n]')} reject / "
        f"{accent('[a]')} allow always / {accent('[c]')} abort task: "
    )
    try:
        answer = input(prompt_str).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("a", "always"):
            if task_id:
                default_approval_manager.preapprove_tool(task_id, operation)
            return True
        if answer in ("c", "cancel", "abort"):
            raise TaskAbortedError(f"Task cancelled by operator during '{operation}'")
        return False
    except (EOFError, KeyboardInterrupt):
        raise TaskAbortedError(f"Task interrupted by operator during '{operation}'")


def _print_usage():
    c = accent  # command names
    d = dim  # descriptions
    print(f"""{title("BoteX")} — {t("usage.tagline")}

{title(t("usage.commands"))}
  {c("botex repl [-w DIR] [--profile P]")}   {d(t("usage.repl"))}
  {c('botex run "task" [flags]')}            {d(t("usage.run"))}
  {c("    --allow-non-zdr")}                 {d(t("usage.allow_non_zdr"))}
  {c("botex --stats [--month]")}             {d(t("usage.stats"))}
  {c("botex --history <task_id>")}           {d(t("usage.history"))}
  {c("botex --snapshots")}                   {d(t("usage.snapshots"))}
  {c("botex --clean-snapshots")}             {d(t("usage.clean"))}
  {c("botex health")}                        {d(t("usage.health"))}
  {c("botex serve")}                         {d(t("usage.serve"))}
  {c("botex config")}                        {d(t("usage.config"))}
  {c("botex config provider <name>")}        {d(t("usage.config.provider"))}
  {c("botex config model [prov] <slug>")}    {d(t("usage.config.model"))}
  {c("botex config profile <name>")}         {d(t("usage.config.profile"))}
  {c("botex config mode <preset>")}          {d(t("usage.config.mode"))}
  {c("botex config zdr <on|off> [prov]")}    {d(t("usage.config.zdr"))}
  {c("botex models [--refresh]")}            {d(t("usage.models"))}
  {c("botex wallet [--refresh] [prov]")}     {d("Show provider credit balance and limits")}
  {c("botex chat [prompt] [-w DIR]")}        {d("Read-only conversational engineering assistant")}
  {c("botex memory <cmd> [flags]")}          {d("Workspace & global Memory Vault (save, search, read, list)")}
  {c("botex cloud [--host H] [--port P]")}   {d("FastAPI Cloud Gateway & Remote MCP SSE server")}

{title(t("usage.repl_cmds"))}
  {c("chat, memory, wallet, credits, health, stats [month], history <id>, snapshots, clean-snapshots,")}
  {c("models, config [...], mode <preset>, help, exit")}

{d(t("usage.no_args"))}
{d(t("usage.no_args2"))}""")


def _resolve_workspace_abs(value: str) -> tuple:
    """Resolve the target workspace to an absolute path plus its source label."""
    path, source = resolve_workspace_source(value)
    return str(Path(path).expanduser().resolve()), source


def _warn_if_harness(workspace: str) -> None:
    """Warn when the target workspace is the BoteX repository itself."""
    from botex.config import PROJECT_ROOT

    try:
        same = Path(workspace).resolve() == PROJECT_ROOT.resolve()
    except OSError:
        same = False
    if same:
        print(warn(t("ws.harness_warn")))


def _repl_workspace(args, arg: str) -> None:
    """REPL `workspace [DIR]` / `cd [DIR]`: show or switch the target project."""
    arg = arg.strip().strip("\"'")
    if not arg:
        exists = Path(args.workspace).is_dir()
        line = f"{accent(t('ws.label'))} {args.workspace}"
        print(line if exists else line + " " + warn(t("ws.missing")))
        return
    target = Path(arg).expanduser()
    if not target.is_absolute():
        target = Path(args.workspace) / target
    if not target.is_dir():
        print(err(t("ws.invalid", path=str(target))))
        return
    args.workspace = str(target.resolve())
    print(ok(t("ws.set", path=args.workspace)))
    _warn_if_harness(args.workspace)


def _print_repl_help():
    c = accent
    d = dim
    print(f"""{title(t("repl.title"))}

{title(t("repl.local"))}
  {c("health")}                  {d(t("repl.health"))}
  {c("stats [--month]")}         {d(t("repl.stats"))}
  {c("history <task_id>")}       {d(t("repl.history"))}
  {c("snapshots")}               {d(t("repl.snapshots"))}
  {c("clean-snapshots")}         {d(t("repl.clean"))}
  {c("config")}                  {d(t("repl.config"))}
  {c("config provider|model|profile|mode|workspace|language|price|zdr <value>")}
                      {d(t("repl.config.set"))}
  {c("models [--refresh]")}      {d(t("usage.models"))}
  {c("wallet [--refresh]")}      {d("Show provider credit balance and limits")}
  {c("mode <preset>")}           {d(t("repl.mode"))}
  {c("workspace [DIR]")} / {c("cd [DIR]")}  {d(t("repl.workspace"))}
  {c("botex repl --allow-non-zdr")} {d(t("repl.allow_non_zdr"))}
  {c("help")}                    {d(t("repl.help"))}
  {c("exit")}                    {d(t("repl.exit"))}

{d(t("repl.shell_note"))}""")


def _start_mcp_server():
    # Status goes to stderr — stdout must stay clean for JSON-RPC.
    sys.stderr.write(t("misc.server_start"))
    mcp.run()


def _normalize_cli_style(text: str) -> str:
    """
    Strip redundant CLI-style prefixes typed into the REPL:
    'botex run "x"' -> 'x', 'botex health' -> 'health', 'run "x"' -> 'x'.
    """
    parts = text.split(None, 1)
    had_botex = False
    if parts and parts[0].lower() == "botex" and len(parts) > 1:
        text = parts[1].strip()
        parts = text.split(None, 1)
        had_botex = True
    if parts and parts[0].lower() == "run" and len(parts) > 1:
        rest = parts[1].strip()
        if had_botex or rest[:1] in ("'", '"'):
            text = rest.strip("\"'").strip()
    return text


def _try_local_command(line: str) -> bool:
    """Handle built-in REPL commands locally (no API call). Returns True if consumed."""
    parts = line.split()
    if not parts:
        return True
    cmd = parts[0].lower().lstrip("-")
    if cmd == "health":
        print(mark_status(asyncio.run(check_health())))
    elif cmd == "stats":
        cli_stats("month" if len(parts) > 1 and parts[1] == "month" else "week")
    elif cmd == "history":
        cli_history(parts[1] if len(parts) > 1 else None)
    elif cmd == "snapshots":
        snaps = snapshot_manager.list_snapshots()
        total_mb = sum(s["size_kb"] for s in snaps) / 1024
        print(f"{t('lc.snaps_title')} ({len(snaps)} {t('lc.tasks')}, {total_mb:.2f} MB) ===")
        for s in snaps:
            print(
                f"  {accent('*')} {s['task_id']:<10} | {s['created_at']} | {s['files_count']} {t('lc.files')} | {s['size_kb']} KB ({s['age_days']} {t('lc.days')})"
            )
    elif cmd == "clean-snapshots":
        res = snapshot_manager.clean_old_snapshots(
            protected_task_ids=set(snapshot_manager.active_task_ids)
        )
        print(
            ok(
                f"[OK] {res['deleted_dirs']} {t('lc.cleaned')} {res['freed_mb']} MB. {t('lc.remaining')} {res['remaining_mb']} MB."
            )
        )
    elif cmd == "config":
        _cli_config(parts[1:])
    elif cmd == "models":
        _cli_models(parts[1:])
    elif cmd in ("wallet", "credits"):
        _cli_wallet(parts[1:])
    elif cmd == "chat":
        _cli_chat(parts[1:])
    elif cmd == "memory":
        _cli_memory(parts[1:])
    elif cmd in ("serve", "server"):
        print(warn(f"[BoteX] {t('lc.serve_warn')}"))
    elif cmd == "repl":
        print(dim(t("lc.in_repl")))
    elif parts[0].startswith("-") or parts[0].lower() in ("botex", "python", "py"):
        # Looks like a mistyped CLI command/flag — never bill this to the model.
        print(warn(f"[BoteX] '{parts[0]}' {t('lc.cli_like')}"))
    else:
        return False
    return True


def _pick(label: str, options: list, current: str):
    """Interactive numbered picker. Returns the chosen option or None."""
    print(f"\n{title(label)} ({t('cfg.current')}: {accent(current)}):")
    for i, o in enumerate(options, 1):
        mark = ok(" *") if o == current else ""
        print(f"  {accent(str(i))}. {o}{mark}")
    raw = input(dim(t("cfg.pick_prompt"))).strip()
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    if raw in options:
        return raw
    print(warn(f"{t('cfg.skipped')} '{raw}'"))
    return None


def _interactive_config():
    """Selector-based config picker for interactive terminals."""
    from botex.capabilities import MODES

    cfg = load_config()
    providers = cfg.get("providers", {})
    cur_provider, _ = resolve_provider()
    eng = cfg.get("engine", {})

    p = _pick(t("cfg.provider_label"), list(providers), cur_provider)
    if p and p != cur_provider:
        for pname in providers:
            set_local_config(f"providers.{pname}.default", pname == p)
        cur_provider = p

    pcfg = providers.get(cur_provider, {})
    prof = _pick(
        f"{t('cfg.profile_label')} ({cur_provider})",
        list(pcfg.get("models", {})),
        eng.get("default_profile", "default"),
    )
    if prof:
        set_local_config("engine.default_profile", prof)

    m = _pick(
        t("cfg.mode_label"),
        sorted(MODES),
        eng.get("default_mode", "edit"),
    )
    if m:
        set_local_config("engine.default_mode", m)

    from botex.i18n import get_language

    lang = _pick(t("cfg.lang_label"), list(SUPPORTED), get_language())
    if lang:
        set_local_config("ui.language", lang)
        set_language(lang)

    print(ok(f"\n{t('cfg.saved')}"))
    _cli_config(["show"])


def _cli_config(argv):
    """`botex config` — inspect or persist defaults into botex.config.local.json."""
    from botex.capabilities import MODES

    cfg = load_config()
    providers = cfg.get("providers", {})

    if not argv and sys.stdin.isatty():
        _interactive_config()
        return
    if not argv or argv[0] in ("show", "list"):
        print(title(t("cfg.title")))
        for name, p in providers.items():
            marker = accent(" [default]") if p.get("default") else ""
            zdr_state = "ZDR:on" if zdr_enforced(p) else "ZDR:off"
            print(
                f"{accent('provider ' + name)}{marker}: {dim(p.get('base_url', ''))} {dim('[' + zdr_state + ']')}"
            )
            for prof, mdl in p.get("models", {}).items():
                print(f"    {accent(prof)}: {mdl}")
        eng = cfg.get("engine", {})
        print(f"{accent('default_profile')}: {eng.get('default_profile', 'default')}")
        print(f"{accent('default_mode')}:    {eng.get('default_mode', 'edit')}")
        print(f"{accent('default_workspace')}: {eng.get('default_workspace') or t('ws.unset')}")
        print(f"{accent('language')}:        {cfg.get('ui', {}).get('language', 'auto')}")
        print(dim(t("cfg.persist_note")))
        return

    key, rest = argv[0], argv[1:]
    if key == "provider" and rest:
        name = rest[0]
        if name not in providers:
            print(err(t("cfg.unknown_provider", name=name, avail=", ".join(providers))))
            return
        for pname in providers:
            set_local_config(f"providers.{pname}.default", pname == name)
        print(ok(t("cfg.default_provider", name=name)))
    elif key == "model" and rest:
        # `config model <slug>` (default provider) or `config model <provider> <slug>`
        if len(rest) > 1:
            pname, slug = rest[0], rest[1]
            if pname not in providers:
                print(err(t("cfg.unknown_provider", name=pname, avail=", ".join(providers))))
                return
        else:
            pname, slug = resolve_provider()[0], rest[0]
        set_local_config(f"providers.{pname}.models.default", slug)
        print(ok(f"[OK] {pname}.models.default = {slug}"))
    elif key == "profile" and rest:
        set_local_config("engine.default_profile", rest[0])
        print(ok(t("cfg.default_profile", name=rest[0])))
    elif key == "mode" and rest:
        m = rest[0].lower()
        if m not in MODES:
            print(err(t("cfg.unknown_mode", name=m, avail=", ".join(sorted(MODES)))))
            return
        set_local_config("engine.default_mode", m)
        print(ok(t("cfg.default_mode", name=m)))
    elif key == "workspace" and rest:
        raw = " ".join(rest).strip().strip("\"'")
        if raw.lower() in ("none", "off", "unset", "-"):
            set_local_config("engine.default_workspace", "")
            print(ok(t("cfg.workspace_cleared")))
            return
        p = Path(raw).expanduser()
        if not p.is_dir():
            print(err(t("ws.invalid", path=str(p))))
            return
        set_local_config("engine.default_workspace", str(p.resolve()))
        print(ok(t("cfg.workspace_set", path=str(p.resolve()))))
    elif key in ("language", "lang") and rest:
        lang = rest[0].lower()
        if lang not in SUPPORTED:
            print(err(t("cfg.unknown_lang", name=lang, avail=", ".join(SUPPORTED))))
            return
        set_local_config("ui.language", lang)
        set_language(lang)
        print(ok(t("cfg.default_lang", name=lang)))
    elif key == "zdr" and rest:
        # `config zdr on|off [provider]` — persistent per-provider ZDR toggle
        # in botex.config.local.json. "off" relaxes both the local model gate
        # and the wire data_collection flag (engine derives both from the
        # same zdr_enforced predicate).
        state = rest[0].lower()
        if state not in ("on", "off"):
            print(err(t("cfg.zdr_unknown")))
            return
        pname = rest[1] if len(rest) > 1 else resolve_provider()[0]
        if pname not in providers:
            print(err(t("cfg.unknown_provider", name=pname, avail=", ".join(providers))))
            return
        set_local_config(f"providers.{pname}.zdr.enabled", state == "on")
        print(ok(t("cfg.zdr_set", name=pname, state=state)))
    elif key == "price" and len(rest) >= 2:
        which = rest[0].lower()
        try:
            val = float(rest[1])
        except ValueError:
            print(err(t("cfg.price_unknown")))
            return
        if which in ("input", "in"):
            set_local_config("pricing.max_input_per_mtok", val)
            print(ok(t("cfg.price_set", which="input", val=f"{val:.2f}")))
        elif which in ("output", "out"):
            set_local_config("pricing.max_output_per_mtok", val)
            print(ok(t("cfg.price_set", which="output", val=f"{val:.2f}")))
        else:
            print(err(t("cfg.price_unknown")))
    else:
        print(t("cfg.usage"))


def _cli_models(argv):
    """`botex models` — list configured model profiles with live pricing."""
    cfg = load_config()
    pricing_cfg = cfg.get("pricing", {})
    refresh = "--refresh" in argv
    try:
        from botex.denylist import DenylistFilter

        dfilter = DenylistFilter(cfg)
    except Exception:
        dfilter = None

    print(title(t("models.title")))
    for pname, pcfg in cfg.get("providers", {}).items():
        marker = accent(" [default]") if pcfg.get("default") else ""
        print(f"\n{accent(pname)}{marker} {dim(pcfg.get('base_url', ''))}")
        catalog = load_catalog(
            pcfg.get("base_url", ""),
            ttl_hours=float(pricing_cfg.get("cache_ttl_hours", 24)),
            force_refresh=refresh,
        )
        for prof, slug in pcfg.get("models", {}).items():
            price = model_price(catalog, slug)
            if price:
                pin, pout = price
                price_str = f"in ${pin:.2f} · out ${pout:.2f}"
            else:
                price_str = dim(t("models.dynamic"))
            if dfilter and not dfilter.is_allowed(slug, provider=pname, profile=prof):
                rec = dfilter.get_denial_record(slug, provider=pname, profile=prof)
                scope_tag = rec.scope if rec else "policy"
                price_str += warn(f" [DENIED: {scope_tag}]")
            print(f"  {accent(prof):<12} {slug:<48} {price_str}")
    pin_cap = pricing_cfg.get("max_input_per_mtok", 0)
    pout_cap = pricing_cfg.get("max_output_per_mtok", 0)
    print(dim("\n" + t("models.caps", pin=f"{pin_cap}", pout=f"{pout_cap}")))


def _cli_wallet(argv):
    """`botex wallet` / `botex credits` — display provider credits and balances.

    Supports subcommands `check`, `summary`, flags `--json`, `--refresh`, `--compact`,
    and explicit provider flags `--provider <name>`, `-p <name>`, `--provider=<name>`.
    """
    import dataclasses
    import json

    from botex.wallet import _sanitize_raw_payload, format_credits_summary, get_credits

    refresh = "--refresh" in argv
    json_mode = "--json" in argv
    compact_mode = "--compact" in argv
    target_prov = ""
    subcommand = "summary"

    # 1. Parse flags: extract explicit provider flag and collect non-flag positionals
    i = 0
    clean_positionals = []
    while i < len(argv):
        arg = argv[i]
        if arg in ("--provider", "-p") and i + 1 < len(argv):
            target_prov = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--provider="):
            target_prov = arg.split("=", 1)[1]
            i += 1
            continue
        elif not arg.startswith("-"):
            clean_positionals.append(arg)
        i += 1

    # 2. Inspect positional arguments for subcommands or positional provider
    for a in clean_positionals:
        lower = a.lower()
        if lower in ("check", "summary"):
            subcommand = lower
        elif lower in ("wallet", "credits"):
            # Redundant command alias passed from CLI or test harnesses
            continue
        elif not target_prov:
            target_prov = a

    _ = subcommand

    # 3. Fallback to active configured provider if not explicitly specified
    if not target_prov:
        target_prov, _prov_cfg = resolve_provider()

    info = get_credits(target_prov, force_refresh=refresh)

    # 4. Render output: JSON mode vs human-readable CLI summary
    if json_mode:
        payload = dataclasses.asdict(info) if info is not None else {}
        clean_payload = _sanitize_raw_payload(payload)
        sys.stdout.write(coerce_text(json.dumps(clean_payload, indent=2, default=str)) + "\n")
    else:
        prov_title = coerce_text(title(f"=== BoteX Wallet ({mask_secrets(str(target_prov))}) ==="))
        sys.stdout.write(prov_title + "\n")
        sys.stdout.write(coerce_text(format_credits_summary(info, compact=compact_mode)) + "\n")


def _cli_chat(argv: list[str]) -> None:
    """CLI dispatcher for BoteX Chat Mode."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="botex chat",
        description="Conversational software engineering assistant (strictly read-only).",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Optional initial prompt/question (if omitted, starts interactive chat REPL).",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default="",
        help="Project working directory (default: engine.default_workspace, else cwd)",
    )
    parser.add_argument(
        "--profile", default="fast", help="Model profile from config (default: fast)"
    )
    parser.add_argument("--model", default="", help="Explicit provider model (overrides profile)")
    parser.add_argument(
        "--provider",
        default="",
        help="API provider (openrouter/openrouter-free/nvidia/google/ollama; empty = config default)",
    )
    parser.add_argument("--session-id", default="", help="Resume or label specific chat session")
    parser.add_argument(
        "--allow-non-zdr",
        action="store_true",
        help="Relaxes Zero Data Retention for this session",
    )
    args = parser.parse_args(argv)

    workspace, ws_source = _resolve_workspace_abs(args.workspace)
    session = default_chat_manager.get_or_create_session(
        session_id=args.session_id or None,
        workspace_dir=workspace,
        model=args.model,
        provider=args.provider,
        profile=args.profile,
    )

    async def _run_stream(user_text: str):
        async for event in stream_chat_turn(
            session=session,
            user_input=user_text,
            allow_non_zdr=args.allow_non_zdr,
        ):
            ev_type = event.get("type")
            if ev_type == "content_chunk":
                delta = event.get("delta", "")
                sys.stdout.write(delta)
                sys.stdout.flush()
            elif ev_type == "tool_call_start":
                t_name = event.get("name", "")
                t_args = event.get("arguments", {})
                tool_line = dim(f"\n[Tool: {t_name}({json.dumps(t_args)})]")
                sys.stdout.write(coerce_text(mask_secrets(tool_line)) + "\n")
                sys.stdout.flush()
            elif ev_type == "error":
                err_text = err(f"\nError: {mask_secrets(str(event.get('error', '')))}")
                sys.stdout.write(coerce_text(err_text) + "\n")
                sys.stdout.flush()
        print()

    # Single-turn mode
    if args.prompt.strip():
        asyncio.run(_run_stream(args.prompt.strip()))
        return

    # Interactive mode
    print(title("BoteX Chat") + " — " + dim("Read-only conversational exploration"))
    print(
        f"{accent(t('ws.label'))} {workspace} {dim('(' + ws_source + ')')} | "
        f"{accent('Provider:')} {args.provider or 'default'} | {accent('Profile:')} {args.profile}"
    )
    _warn_if_harness(workspace)

    while True:
        try:
            line = input(accent("chat> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("exit", "quit"):
            break
        if low in ("clear", "reset"):
            session.clear()
            print(dim("Chat session history cleared."))
            continue
        if low in ("help", "?"):
            print(
                dim(
                    "Chat commands: 'clear' to reset history, 'exit' to quit.\n"
                    "Tools: get_file_outline, read_file_lines, read_file, list_dir, search_memory, read_memory.\n"
                    "To make code edits or run commands, switch to Agent Mode ('botex run ...')."
                )
            )
            continue
        try:
            asyncio.run(_run_stream(line))
        except KeyboardInterrupt:
            print(warn("\n" + t("repl.interrupted")))
        except Exception as exc:
            err_line = err(f"\nChat failure: {mask_secrets(str(exc))}")
            sys.stdout.write(coerce_text(err_line) + "\n")
            sys.stdout.flush()


def _cli_memory(argv: list[str]) -> None:
    """CLI dispatcher for BoteX Memory Vault."""
    import argparse

    from botex.wallet import _sanitize_raw_payload

    if not argv:
        print(title("BoteX Memory Vault CLI"))
        print("Usage:")
        print(
            "  botex memory save <title> <content> [--scope local|global] [--kind KIND] [--tags TAGS] [--id ID] [-w DIR]"
        )
        print(
            "  botex memory search [query] [--scope local|global|all] [--tag TAG] [--kind KIND] [-w DIR]"
        )
        print("  botex memory read <id> [--scope local|global|all] [-w DIR]")
        print("  botex memory list [--scope local|global|all] [--tag TAG] [--kind KIND] [-w DIR]")
        return

    sub = argv[0].lower()
    rest = argv[1:]

    parser = argparse.ArgumentParser(prog=f"botex memory {sub}")
    parser.add_argument("--workspace", "-w", default="", help="Workspace directory")
    parser.add_argument("--scope", default="local", help="Memory scope: local, global, or all")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")

    if sub == "save":
        parser.add_argument("title", help="Title of the memory entry")
        parser.add_argument("content", help="Markdown body content")
        parser.add_argument(
            "--kind", default="context", help="Kind (context/decision/handoff/lesson)"
        )
        parser.add_argument("--tags", default="", help="Comma-separated tags")
        parser.add_argument("--id", default="", help="Optional custom memory ID")
        args = parser.parse_args(rest)
        tags_list = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        ws = resolve_workspace(args.workspace)
        res = _save_memory(
            workspace_dir=ws,
            title=args.title,
            content=args.content,
            kind=args.kind,
            tags=tags_list,
            memory_id=args.id or None,
            scope=args.scope,
        )
        if args.json:
            clean_res = _sanitize_raw_payload(res)
            sys.stdout.write(coerce_text(json.dumps(clean_res, indent=2)) + "\n")
        elif res.get("ok"):
            msg = ok(
                f"[OK] Saved memory '{res.get('id')}' in {res.get('scope', 'local')} scope: {res.get('path')}"
            )
            sys.stdout.write(coerce_text(msg) + "\n")
        else:
            sys.stdout.write(coerce_text(err(f"Failed to save memory: {res.get('error')}")) + "\n")

    elif sub in ("search", "find"):
        parser.add_argument("query", nargs="?", default="", help="Search query string")
        parser.add_argument("--tag", default="", help="Filter by tag")
        parser.add_argument("--kind", default="", help="Filter by kind")
        args = parser.parse_args(rest)
        ws = resolve_workspace(args.workspace)
        res = _search_memory(
            workspace_dir=ws,
            query=args.query,
            tag=args.tag or None,
            kind=args.kind or None,
            scope=args.scope,
        )
        if args.json:
            clean_res = _sanitize_raw_payload(res)
            sys.stdout.write(coerce_text(json.dumps(clean_res, indent=2)) + "\n")
        elif res.get("ok"):
            mems = res.get("memories", [])
            header = title(f"=== Memory Search ({len(mems)} entries, scope: {args.scope}) ===")
            sys.stdout.write(coerce_text(header) + "\n")
            for m in mems:
                sc = m.get("scope", "local")
                tags = f" [{', '.join(m.get('tags', []))}]" if m.get("tags") else ""
                line = (
                    f"  {accent('*')} {m.get('id')} ({sc}, {m.get('kind')}){tags}: {m.get('title')}"
                )
                sys.stdout.write(coerce_text(line) + "\n")
                if m.get("preview"):
                    sys.stdout.write(coerce_text(f"    {dim(m.get('preview')[:120])}") + "\n")
        else:
            sys.stdout.write(coerce_text(err(f"Search failed: {res.get('error')}")) + "\n")

    elif sub in ("read", "get", "show"):
        parser.add_argument("memory_id", help="Memory entry ID")
        args = parser.parse_args(rest)
        ws = resolve_workspace(args.workspace)
        res = _read_memory(
            workspace_dir=ws,
            memory_id=args.memory_id,
            scope=args.scope,
        )
        if args.json:
            clean_res = _sanitize_raw_payload(res)
            sys.stdout.write(coerce_text(json.dumps(clean_res, indent=2)) + "\n")
        elif res.get("ok"):
            meta = res.get("metadata", {})
            header = title(f"=== Memory Entry: {res.get('id')} (scope: {res.get('scope')}) ===")
            sys.stdout.write(coerce_text(header) + "\n")
            sys.stdout.write(coerce_text(f"Title: {meta.get('title')}") + "\n")
            sys.stdout.write(
                coerce_text(f"Kind: {meta.get('kind')} | Tags: {meta.get('tags')}") + "\n"
            )
            sys.stdout.write(coerce_text(f"Timestamp: {meta.get('timestamp')}") + "\n")
            sys.stdout.write(coerce_text("\n" + res.get("content", "")) + "\n")
        else:
            sys.stdout.write(coerce_text(err(f"Read failed: {res.get('error')}")) + "\n")

    elif sub == "list":
        parser.add_argument("--tag", default="", help="Filter by tag")
        parser.add_argument("--kind", default="", help="Filter by kind")
        args = parser.parse_args(rest)
        ws = resolve_workspace(args.workspace)
        res = _list_memories(
            workspace_dir=ws,
            scope=args.scope,
            tag=args.tag or None,
            kind=args.kind or None,
        )
        if args.json:
            clean_res = _sanitize_raw_payload(res)
            sys.stdout.write(coerce_text(json.dumps(clean_res, indent=2)) + "\n")
        elif res.get("ok"):
            mems = res.get("memories", [])
            header = title(f"=== Memory Vault ({len(mems)} entries, scope: {args.scope}) ===")
            sys.stdout.write(coerce_text(header) + "\n")
            for m in mems:
                sc = m.get("scope", "local")
                tags = f" [{', '.join(m.get('tags', []))}]" if m.get("tags") else ""
                line = (
                    f"  {accent('*')} {m.get('id')} ({sc}, {m.get('kind')}){tags}: {m.get('title')}"
                )
                sys.stdout.write(coerce_text(line) + "\n")
        else:
            sys.stdout.write(coerce_text(err(f"List failed: {res.get('error')}")) + "\n")
    else:
        sys.stdout.write(
            coerce_text(
                err(f"Unknown memory subcommand: '{sub}'. Use save, search, read, or list.")
            )
            + "\n"
        )


def _cli_cloud(argv: list[str]) -> None:
    """CLI dispatcher to launch BoteX FastAPI Cloud Gateway & Remote MCP SSE server."""
    import argparse

    import uvicorn

    from botex.server_cloud import app

    parser = argparse.ArgumentParser(
        prog="botex cloud",
        description="Launch BoteX FastAPI Cloud Gateway & Remote MCP SSE server.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--log-level", default="info", help="Log level (default: info)")
    args = parser.parse_args(argv)

    print(title(f"BoteX FastAPI Cloud Gateway & Remote MCP SSE on http://{args.host}:{args.port}"))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, workers=1)


def _repl(argv):
    import argparse

    parser = argparse.ArgumentParser(prog="botex repl", description="Interactive BoteX task loop.")
    parser.add_argument(
        "--workspace",
        "-w",
        default="",
        help="Project working directory (default: engine.default_workspace, else cwd)",
    )
    parser.add_argument(
        "--profile", default="default", help="Model profile (default/coding/auto-beta)"
    )
    parser.add_argument(
        "--provider",
        default="",
        help="API provider (openrouter/openrouter-free/nvidia/google/ollama; empty = config default)",
    )
    parser.add_argument(
        "--mode",
        default="",
        help="Capability preset (readonly/edit/destructive/full; empty = config)",
    )
    parser.add_argument(
        "--recipe",
        default="",
        help="Operational persona / recipe workflow prompt (planner, reviewer, ...)",
    )
    parser.add_argument("--model", default="", help="Explicit provider model (overrides profile)")
    parser.add_argument("--max-turns", type=int, default=0, help="Step limit (0 = config value)")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Per-turn completion cap (0 = config; reasoning models need ~8000+)",
    )
    parser.add_argument(
        "--max-duration", type=int, default=0, help="Wall-clock limit in seconds (0 = config)"
    )
    parser.add_argument(
        "--allow-non-zdr",
        action="store_true",
        help="Relaxes Zero Data Retention for this session (needed for :free models; provider may log/train on prompts)",
    )
    args = parser.parse_args(argv)
    args.workspace, ws_source = _resolve_workspace_abs(args.workspace)

    print(title("BoteX REPL") + " — " + t("repl.banner") + " " + dim("(" + t("repl.banner2") + ")"))
    print(
        f"{accent(t('ws.label'))} {args.workspace} {dim('(' + ws_source + ')')} | {accent('Provider:')} "
        f"{args.provider or 'default'} | {accent('Profile:')} {args.profile} "
        f"| {accent('Mode:')} {args.mode or 'default'}"
        + (f" | {accent('Recipe:')} {args.recipe}" if args.recipe else "")
    )
    _warn_if_harness(args.workspace)
    while True:
        try:
            task = input(accent("botex> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        task = _normalize_cli_style(task)
        if task.lower() in ("exit", "quit"):
            break
        if task.lower() in ("help", "?"):
            _print_repl_help()
            continue
        low = task.lower()
        if low in ("workspace", "cd") or low.startswith(("workspace ", "cd ")):
            _repl_workspace(args, task.split(None, 1)[1] if " " in task else "")
            continue
        if low == "mode" or low.startswith("mode "):
            new_mode = low.split(None, 1)[1].strip() if " " in low else ""
            if new_mode:
                args.mode = new_mode
            print(f"{accent(t('repl.mode_set'))} {args.mode or t('repl.mode_default')}")
            continue
        if _try_local_command(task):
            continue
        try:
            res = asyncio.run(
                run_botex_task(
                    task=task,
                    workspace_dir=args.workspace,
                    model=args.model,
                    profile=args.profile,
                    provider=args.provider,
                    mode=args.mode,
                    recipe=args.recipe,
                    max_turns=args.max_turns or None,
                    max_tokens=args.max_tokens or None,
                    max_duration_s=args.max_duration or None,
                    allow_non_zdr=True if args.allow_non_zdr else None,
                    confirm_fn=_interactive_confirm,
                )
            )
        except KeyboardInterrupt:
            print(warn("\n" + t("repl.interrupted")))
            continue
        except Exception as e:
            print(err(f"{t('repl.failure')} {e}"))
            continue
        status_str = ok(t("repl.success")) if res.get("ok") else err(t("repl.failure"))
        print(
            f"{status_str} {res.get('status')} | {t('repl.cost')} ${res.get('cost_usd', 0.0):.5f} USD"
            f" | {accent(t('ws.label'))} {args.workspace}"
        )
        print(res.get("summary") or res.get("message", ""))


def main():
    init_language(load_config().get("ui", {}).get("language", "auto"))
    if len(sys.argv) > 1 and sys.argv[1] == "repl":
        _repl(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "run":
        import argparse

        parser = argparse.ArgumentParser(
            prog="server.py run",
            description="Runs a BoteX task directly from the CLI (no MCP client).",
        )
        parser.add_argument("task", help="Description of the task to perform")
        parser.add_argument(
            "--files", nargs="*", default=[], help="Primary files affected by the task"
        )
        parser.add_argument(
            "--workspace",
            "-w",
            default="",
            help="Project working directory (default: engine.default_workspace, else cwd)",
        )
        parser.add_argument(
            "--model", default="", help="Explicit provider model (overrides profile)"
        )
        parser.add_argument(
            "--profile",
            default="default",
            help="Model profile from botex.config.json (default/coding/auto-beta)",
        )
        parser.add_argument(
            "--provider",
            default="",
            help="API provider (openrouter/openrouter-free/nvidia/google/ollama; empty = config default)",
        )
        parser.add_argument(
            "--mode",
            default="",
            help="Capability preset (readonly/edit/destructive/full; empty = config)",
        )
        parser.add_argument(
            "--recipe",
            default="",
            help="Operational persona / recipe workflow prompt (planner, reviewer, ...)",
        )
        parser.add_argument(
            "--max-turns", type=int, default=0, help="Step limit (0 = config value)"
        )
        parser.add_argument(
            "--max-tokens",
            type=int,
            default=0,
            help="Per-turn completion cap (0 = config; reasoning models need ~8000+)",
        )
        parser.add_argument(
            "--max-duration", type=int, default=0, help="Wall-clock limit in seconds (0 = config)"
        )
        parser.add_argument(
            "--budget",
            type=float,
            default=-1.0,
            help="Daily USD limit (negative = config, 0 = none)",
        )
        parser.add_argument(
            "--api-key", default="", help="Provider API key per-call (overrides env/.env/config)"
        )
        parser.add_argument(
            "--allow-destructive",
            action="store_true",
            help="Authorizes delete_file/move_file without prompting (without it, on an interactive terminal the agent asks per operation)",
        )
        parser.add_argument(
            "--allow-exec",
            action="store_true",
            help="Authorizes run_command without prompting (requires exec.enabled=true in config; THIS IS NOT A SANDBOX)",
        )
        parser.add_argument(
            "--allow-net",
            action="store_true",
            help="Authorizes read_url for configured public hosts",
        )
        parser.add_argument(
            "--net-host",
            action="append",
            default=[],
            help="Authorize a public host for this run (repeatable)",
        )
        parser.add_argument(
            "--net-url",
            action="append",
            default=[],
            help="Authorize an exact public URL or trailing-slash subtree (repeatable)",
        )
        parser.add_argument("--output-path", default="", help="Required output file")
        parser.add_argument(
            "--verify-command", default="", help="Allowlisted command required to pass after writes"
        )
        parser.add_argument(
            "--allow-non-zdr",
            action="store_true",
            help="Relaxes Zero Data Retention for this run (needed for :free models; provider may log/train on prompts)",
        )
        parser.add_argument(
            "--approval-policy",
            choices=["none", "high_risk", "mutations", "all_risky"],
            default="",
            help="Operator approval policy (none, high_risk, mutations, all_risky)",
        )
        args = parser.parse_args(sys.argv[2:])

        # Destructive ops: pre-authorized via flag/config, or gated per-op by an
        # interactive prompt when attached to a real terminal. In non-interactive
        # contexts (pipes, CI) without the flag the tools are not exposed at all.
        confirm_fn = None
        if not args.allow_destructive and sys.stdin.isatty():
            confirm_fn = _interactive_confirm

        args.workspace, _ws_src = _resolve_workspace_abs(args.workspace)
        print(f"{accent(t('ws.label'))} {args.workspace} {dim('(' + _ws_src + ')')}")
        _warn_if_harness(args.workspace)
        res = asyncio.run(
            run_botex_task(
                task=args.task,
                files=args.files,
                workspace_dir=args.workspace,
                model=args.model,
                profile=args.profile,
                provider=args.provider,
                mode=args.mode,
                recipe=args.recipe,
                max_turns=args.max_turns or None,
                max_tokens=args.max_tokens or None,
                max_duration_s=args.max_duration or None,
                budget_limit_usd=args.budget if args.budget >= 0 else None,
                allow_destructive=True if args.allow_destructive else None,
                allow_exec=True if args.allow_exec else None,
                allow_net=True if args.allow_net else None,
                net_allowed_hosts=args.net_host or None,
                net_allowed_urls=args.net_url or None,
                confirm_fn=confirm_fn,
                api_key=args.api_key,
                output_path=args.output_path,
                verify_command=args.verify_command,
                allow_non_zdr=True if args.allow_non_zdr else None,
                approval_policy=args.approval_policy or None,
            )
        )
        status_str = ok(t("repl.success")) if res.get("ok") else err(t("repl.failure"))
        print(
            f"{status_str} {t('run.status')} {res.get('status')} | {t('run.task')} {res.get('task_id')} | "
            f"{t('run.steps')} {res.get('steps')} | {t('repl.cost')} ${res.get('cost_usd', 0.0):.5f} USD"
        )
        print(f"{accent(t('ws.label'))} {args.workspace}")
        print(f"{accent(t('repl.files'))} {', '.join(res.get('files_touched', [])) or '(none)'}")
        print(res.get("summary") or res.get("message", ""))
        sys.exit(0 if res.get("ok") else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--stats":
        period = "month" if "--month" in sys.argv else "week"
        cli_stats(period)
    elif len(sys.argv) > 1 and sys.argv[1] == "--history":
        tid = sys.argv[2] if len(sys.argv) > 2 else None
        cli_history(tid)
    elif len(sys.argv) > 1 and sys.argv[1] == "--snapshots":
        snaps = snapshot_manager.list_snapshots()
        total_mb = sum(s["size_kb"] for s in snaps) / 1024
        print(f"{t('lc.snaps_title')} (total: {len(snaps)} {t('lc.tasks')}, {total_mb:.2f} MB) ===")
        for s in snaps:
            print(
                f"  * Task: {s['task_id']:<10} | {s['created_at']} | {s['files_count']} {t('lc.files')} | {s['size_kb']} KB ({s['age_days']} {t('lc.days')})"
            )
    elif len(sys.argv) > 1 and sys.argv[1] == "--clean-snapshots":
        res = snapshot_manager.clean_old_snapshots(
            max_age_days=7.0,
            max_total_mb=50.0,
            protected_task_ids=set(snapshot_manager.active_task_ids),
        )
        print(
            ok(
                f"[OK] {res['deleted_dirs']} {t('lc.cleaned')} {res['freed_mb']} MB. {t('lc.remaining')} {res['remaining_mb']} MB."
            )
        )
    elif len(sys.argv) > 1 and sys.argv[1] == "health":
        print(mark_status(asyncio.run(check_health())))
    elif len(sys.argv) > 1 and sys.argv[1] == "config":
        _cli_config(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "models":
        _cli_models(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] in ("wallet", "credits"):
        _cli_wallet(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "chat":
        _cli_chat(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "memory":
        _cli_memory(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "cloud":
        _cli_cloud(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] in ("serve", "server", "--serve"):
        # Force MCP stdio server mode even on an interactive terminal
        _start_mcp_server()
    elif len(sys.argv) > 1 and sys.argv[1] in ("help", "--help", "-h", "-?"):
        _print_usage()
    elif len(sys.argv) > 1:
        print(err(t("misc.unknown_cmd", name=sys.argv[1]) + "\n"))
        _print_usage()
        sys.exit(2)
    else:
        # No command: interactive terminal -> REPL help + drop into the REPL
        # (keeps double-clicked windows usable); piped stdin (MCP client)
        # -> stdio server.
        if sys.stdin.isatty():
            _print_repl_help()
            print()
            _repl([])
        else:
            _start_mcp_server()


if __name__ == "__main__":
    main()

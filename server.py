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
import sys
import os
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
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

from botex.engine import run_botex_task
from botex.security import mask_secrets
from botex.file_tools import get_file_outline as _get_file_outline
from botex.analytics import cli_stats, cli_history, load_analytics
from botex.patch_engine import snapshot_manager
from botex.config import load_config, set_local_config, resolve_provider
from botex.ui import title, accent, ok, warn, err, dim, mark_status
from botex.i18n import t, init_language, set_language, SUPPORTED
from botex.pricing import load_catalog, model_price
from botex.benchmarks import get_recommended_models
from botex.net_tools import fetch_url as _fetch_url
from botex.memory import (
    save_memory as _save_memory,
    search_memory as _search_memory,
    read_memory as _read_memory,
)

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


def _masked_result(res: dict) -> dict:
    """Copy an engine result with secret patterns redacted from text fields."""
    masked = dict(res)
    for key in ("summary", "message"):
        if isinstance(masked.get(key), str):
            masked[key] = mask_secrets(masked[key])
    return masked


def _format_result_text(res: dict) -> str:
    """Human-readable task report (kept as TextContent for legacy clients)."""
    status_str = "SUCCESS" if res.get("ok") else "FAILURE"
    files_str = ", ".join(res.get("files_touched", [])) or "(none)"
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
        chain = " -> ".join(
            f"{a.get('model')}({a.get('status')})" for a in attempts)
        lines.append(f"- Model chain: {chain}")
    if res.get("rollback_verified") is False:
        lines.append("- WARNING: rollback could not be verified — workspace may be dirty")
    lines += [
        f"- Duration: {res.get('duration_s', 0)}s | Cost: {cost_str}",
        f"- Modified files: {files_str}",
        f"- Line balance: +{res.get('lines_added', 0)} / -{res.get('lines_removed', 0)}",
        f"\nChange summary:\n{summary_text}"
    ]
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
                tool_result.content[0].text if tool_result.content else ""
            )
            entry["result_data"] = tool_result.structured_content or {}
            entry["status"] = "DONE"
    except asyncio.CancelledError:
        entry["status"] = "CANCELLED"
        raise
    except Exception as exc:
        entry["status"] = "FAILED"
        entry["result"] = (
            f"Background task failed: {mask_secrets(str(exc))[:500]}"
        )
    finally:
        entry["finished_at"] = time.time()


def _cleanup_tasks() -> None:
    ttl = int(load_config().get("engine", {}).get("task_result_ttl_s", 3600))
    now = time.time()
    for task_id, entry in list(_TASKS.items()):
        if (entry.get("status") in {"DONE", "FAILED", "CANCELLED"}
                and now - entry.get("finished_at", now) > ttl):
            _TASKS.pop(task_id, None)


# ---------------------------------------------------------------------------
# Public MCP Tools (Exposed to MCP clients)
# ---------------------------------------------------------------------------

@mcp.tool()
async def start_task(
    task: str,
    files: list[str] = [],
    workspace_dir: str = ".",
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

    Returns:
        {"task_id": str, "status": "QUEUED"} — poll with get_task_status.
        The finished task's `result_data` carries the full engine result
        (status, failure_kind, ok, files_touched, exec_ran,
        rollback_verified, attempts[], cost_usd, ...).
    """
    _cleanup_tasks()
    max_queued = int(load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(
        entry.get("status") in {"QUEUED", "RUNNING"}
        for entry in _TASKS.values()
    )
    if active_count >= max_queued:
        return {
            "ok": False, "status": "TOO_MANY_QUEUED",
            "message": f"Task queue limit reached ({max_queued}).",
        }
    task_id = uuid.uuid4().hex[:12]
    kwargs = {
        "task": task, "files": files, "workspace_dir": workspace_dir,
        "model": model, "profile": profile, "provider": provider,
        "mode": mode, "max_turns": max_turns, "max_tokens": max_tokens,
        "max_duration_s": max_duration_s, "budget_limit_usd": budget_limit_usd,
        "allow_destructive": allow_destructive, "allow_exec": allow_exec,
        "allow_net": allow_net, "net_allowed_hosts": net_allowed_hosts,
        "net_allowed_urls": net_allowed_urls,
        "api_key": api_key, "output_path": output_path,
        "verify_command": verify_command,
        "recipe": recipe,
    }
    task_obj = asyncio.create_task(_background_task(task_id, kwargs))
    _TASKS[task_id] = {
        "status": "QUEUED", "created_at": time.time(), "task": task_obj,
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
        "ok": True, "task_id": task_id, "status": entry["status"],
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
async def recommend_models(task_type: str = "coding", limit: int = 5) -> str:
    """
    [EN] Fetches the most cost-effective and capable models from OpenRouter based on live benchmarks.
    USE THIS TOOL BEFORE calling `run_subagent` if you are unsure which model ID to use or want to optimize for cost/quality.
    
    [PL] Pobiera rekomendacje modeli z OpenRouter na podstawie benchmarków.
    Użyj tego narzędzia ZANIM wywołasz `run_subagent`, jeśli nie znasz dokładnego ID modelu.
    """
    results = get_recommended_models(task_type, limit)
    return json.dumps(results, indent=2)

@mcp.tool()
async def run_subagent(
    task: str,
    files: list[str] = [],
    workspace_dir: str = ".",
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
        workspace_dir: Project directory path (defaults to current directory).
        model: Any model from the provider's catalog. Empty = model from config
               (see 'profile' and botex.config.json).
        profile: Model profile from botex.config.json (e.g. 'default', 'coding',
               'auto-beta', 'fast'). When both 'model' and 'profile' are empty,
               the capability mode picks the tier via engine.mode_profiles
               (readonly -> 'fast', destructive/full -> 'coding'). Ignored when
               'model' is given explicitly.
        provider: API provider from the 'providers' section of botex.config.json
               ('openrouter', 'nvidia'). Empty = provider marked
               'default: true' in the configuration.
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
    )

    res = _masked_result(res)
    return CallToolResult(
        content=[TextContent(type="text", text=_format_result_text(res))],
        structured_content=res,
    )


@mcp.tool()
async def get_outline(path: str, workspace_dir: str = ".") -> dict[str, Any]:
    """
    Returns a lightweight file skeleton (classes, methods, functions, headers,
    line numbers) without reading the whole file. Saves ~90% of tokens.
    """
    return _get_file_outline(path, workspace_dir)


@mcp.tool()
async def save_memory(
    title: str,
    content: str,
    workspace_dir: str = ".",
    kind: str = "context",
    tags: Optional[list[str]] = None,
    memory_id: str = "",
) -> dict[str, Any]:
    """
    Saves a persistent context note, architectural decision, task handoff, or lesson
    into the workspace Memory Vault (<workspace_dir>/.botex/memory/).
    Masks secrets and provides directory isolation.

    Args:
        title: Short descriptive title of the memory entry.
        content: Detailed markdown notes, architectural rationale, or handoff context.
        workspace_dir: Workspace root directory (defaults to current directory).
        kind: Entry category ('context' | 'decision' | 'handoff' | 'lesson').
        tags: Optional list of keyword tags for filtering and discovery.
        memory_id: Optional custom identifier (alphanumeric/hyphen/underscore). Auto-generated if omitted.
    """
    return _save_memory(
        workspace_dir=workspace_dir,
        title=title,
        content=content,
        kind=kind,
        tags=tags,
        memory_id=memory_id or None,
    )


@mcp.tool()
async def search_memory(
    workspace_dir: str = ".",
    query: str = "",
    tag: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """
    Searches the workspace Memory Vault (<workspace_dir>/.botex/memory/)
    by keyword query, tag, or entry kind (context, decision, handoff, lesson).

    Args:
        workspace_dir: Workspace root directory (defaults to current directory).
        query: Free-text search string matched against title, content, and tags.
        tag: Filter by specific tag.
        kind: Filter by entry kind ('context', 'decision', 'handoff', 'lesson').
    """
    return _search_memory(
        workspace_dir=workspace_dir,
        query=query,
        tag=tag or None,
        kind=kind or None,
    )


@mcp.tool()
async def read_memory(
    memory_id: str,
    workspace_dir: str = ".",
) -> dict[str, Any]:
    """
    Reads the full content and metadata of a specific memory entry by ID from the workspace Memory Vault.

    Args:
        memory_id: The ID of the memory entry to retrieve (e.g. 'mem_a1b2c3d4').
        workspace_dir: Workspace root directory (defaults to current directory).
    """
    return _read_memory(
        workspace_dir=workspace_dir,
        memory_id=memory_id,
    )


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
    records = [
        r for r in load_analytics()
        if r.get("timestamp", "") >= cutoff
    ]
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
        top_f = f" (top failure: {max(s['failures'].items(), key=lambda x: x[1])[0]})" if s["failures"] else ""
        model_lines.append(f"  * {m}: {s['runs']} runs | DONE rate: {rate:.1f}%{top_f} | ${s['cost']:.4f} USD")

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
        has_key = bool(os.environ.get(env_name) or secrets.get(f"{pname}_api_key"))
        marker = " [default]" if pcfg.get("default") else ""
        key_lines.append(
            f"[{'OK' if has_key else 'MISSING'}] {pname}{marker}: {env_name} "
            f"{'configured' if has_key else '(check .env)'}"
        )
    keys_report = "\n".join(key_lines) or "[MISSING] No providers configured"
    snaps = snapshot_manager.list_snapshots()
    total_mb = sum(s["size_kb"] for s in snaps) / 1024

    return (
        "=== BOTEX HARNESS HEALTH CHECK ===\n"
        f"{keys_report}\n"
        f"[OK] Snapshot directory: active ({len(snaps)} backups, {total_mb:.2f} MB)\n"
        f"[OK] Path Traversal protection and Zero-Retention: ACTIVE\n"
        "[SUCCESS] BoteX engine is fully ready for autonomous work on disk!"
    )


@mcp.tool()
async def clean_snapshots(max_age_days: float = 7.0, max_total_mb: float = 50.0) -> str:
    """Purges old or excess backup snapshots from disk."""
    res = snapshot_manager.clean_old_snapshots(max_age_days=max_age_days, max_total_mb=max_total_mb)
    return (f"[OK] Purged old snapshots: removed {res['deleted_dirs']} folders, "
            f"freed {res['freed_mb']} MB. Remaining: {res['remaining_mb']} MB.")


# ---------------------------------------------------------------------------
# CLI Runner
# ---------------------------------------------------------------------------
def _interactive_confirm(operation: str, path: str) -> bool:
    """Prompt the terminal operator before a destructive file operation."""
    try:
        answer = input(
            warn(f"[BoteX] {t('confirm.req')} '{operation}' {t('confirm.on')} '{path}'. ")
            + t("confirm.ask") + " "
        ).strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _print_usage():
    c = accent  # command names
    d = dim     # descriptions
    print(f"""{title('BoteX')} — {t('usage.tagline')}

{title(t('usage.commands'))}
  {c('botex repl [-w DIR] [--profile P]')}   {d(t('usage.repl'))}
  {c('botex run "task" [flags]')}            {d(t('usage.run'))}
  {c('botex --stats [--month]')}             {d(t('usage.stats'))}
  {c('botex --history <task_id>')}           {d(t('usage.history'))}
  {c('botex --snapshots')}                   {d(t('usage.snapshots'))}
  {c('botex --clean-snapshots')}             {d(t('usage.clean'))}
  {c('botex health')}                        {d(t('usage.health'))}
  {c('botex serve')}                         {d(t('usage.serve'))}
  {c('botex config')}                        {d(t('usage.config'))}
  {c('botex config provider <name>')}        {d(t('usage.config.provider'))}
  {c('botex config model [prov] <slug>')}    {d(t('usage.config.model'))}
  {c('botex config profile <name>')}         {d(t('usage.config.profile'))}
  {c('botex config mode <preset>')}          {d(t('usage.config.mode'))}
  {c('botex models [--refresh]')}            {d(t('usage.models'))}

{title(t('usage.repl_cmds'))}
  {c('health, stats [month], history <id>, snapshots, clean-snapshots,')}
  {c('models, config [...], mode <preset>, help, exit')}

{d(t('usage.no_args'))}
{d(t('usage.no_args2'))}""")


def _print_repl_help():
    c = accent
    d = dim
    print(f"""{title(t('repl.title'))}

{title(t('repl.local'))}
  {c('health')}                  {d(t('repl.health'))}
  {c('stats [--month]')}         {d(t('repl.stats'))}
  {c('history <task_id>')}       {d(t('repl.history'))}
  {c('snapshots')}               {d(t('repl.snapshots'))}
  {c('clean-snapshots')}         {d(t('repl.clean'))}
  {c('config')}                  {d(t('repl.config'))}
  {c('config provider|model|profile|mode|language|price <value>')}
                      {d(t('repl.config.set'))}
  {c('models [--refresh]')}      {d(t('usage.models'))}
  {c('mode <preset>')}           {d(t('repl.mode'))}
  {c('help')}                    {d(t('repl.help'))}
  {c('exit')}                    {d(t('repl.exit'))}

{d(t('repl.shell_note'))}""")


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
            print(f"  {accent('*')} {s['task_id']:<10} | {s['created_at']} | {s['files_count']} {t('lc.files')} | {s['size_kb']} KB ({s['age_days']} {t('lc.days')})")
    elif cmd == "clean-snapshots":
        res = snapshot_manager.clean_old_snapshots()
        print(ok(f"[OK] {res['deleted_dirs']} {t('lc.cleaned')} {res['freed_mb']} MB. {t('lc.remaining')} {res['remaining_mb']} MB."))
    elif cmd == "config":
        _cli_config(parts[1:])
    elif cmd == "models":
        _cli_models(parts[1:])
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
            print(f"{accent('provider ' + name)}{marker}: {dim(p.get('base_url', ''))}")
            for prof, mdl in p.get("models", {}).items():
                print(f"    {accent(prof)}: {mdl}")
        eng = cfg.get("engine", {})
        print(f"{accent('default_profile')}: {eng.get('default_profile', 'default')}")
        print(f"{accent('default_mode')}:    {eng.get('default_mode', 'edit')}")
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
    elif key in ("language", "lang") and rest:
        lang = rest[0].lower()
        if lang not in SUPPORTED:
            print(err(t("cfg.unknown_lang", name=lang, avail=", ".join(SUPPORTED))))
            return
        set_local_config("ui.language", lang)
        set_language(lang)
        print(ok(t("cfg.default_lang", name=lang)))
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
            print(f"  {accent(prof):<12} {slug:<48} {price_str}")
    pin_cap = pricing_cfg.get("max_input_per_mtok", 0)
    pout_cap = pricing_cfg.get("max_output_per_mtok", 0)
    print(dim("\n" + t("models.caps", pin=f"{pin_cap}", pout=f"{pout_cap}")))


def _repl(argv):
    import argparse
    parser = argparse.ArgumentParser(
        prog="botex repl",
        description="Interactive BoteX task loop."
    )
    parser.add_argument("--workspace", "-w", default=".", help="Project working directory")
    parser.add_argument("--profile", default="default", help="Model profile (default/coding/auto-beta)")
    parser.add_argument("--provider", default="", help="API provider (openrouter/nvidia; empty = config default)")
    parser.add_argument("--mode", default="", help="Capability preset (readonly/edit/destructive/full; empty = config)")
    parser.add_argument("--recipe", default="", help="Operational persona / recipe workflow prompt (planner, reviewer, ...)")
    parser.add_argument("--model", default="", help="Explicit provider model (overrides profile)")
    parser.add_argument("--max-turns", type=int, default=0, help="Step limit (0 = config value)")
    parser.add_argument("--max-tokens", type=int, default=0, help="Per-turn completion cap (0 = config; reasoning models need ~8000+)")
    parser.add_argument("--max-duration", type=int, default=0, help="Wall-clock limit in seconds (0 = config)")
    args = parser.parse_args(argv)

    print(title("BoteX REPL") + " — " + t("repl.banner") + " "
          + dim("(" + t("repl.banner2") + ")"))
    print(f"{accent('Workspace:')} {args.workspace} | {accent('Provider:')} "
          f"{args.provider or 'default'} | {accent('Profile:')} {args.profile} "
          f"| {accent('Mode:')} {args.mode or 'default'}"
          + (f" | {accent('Recipe:')} {args.recipe}" if args.recipe else ""))
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
        if low == "mode" or low.startswith("mode "):
            new_mode = low.split(None, 1)[1].strip() if " " in low else ""
            if new_mode:
                args.mode = new_mode
            print(f"{accent(t('repl.mode_set'))} {args.mode or t('repl.mode_default')}")
            continue
        if _try_local_command(task):
            continue
        try:
            res = asyncio.run(run_botex_task(
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
                confirm_fn=_interactive_confirm
            ))
        except KeyboardInterrupt:
            print(warn("\n" + t("repl.interrupted")))
            continue
        except Exception as e:
            print(err(f"{t('repl.failure')} {e}"))
            continue
        status_str = ok(t("repl.success")) if res.get("ok") else err(t("repl.failure"))
        print(f"{status_str} {res.get('status')} | {t('repl.cost')} ${res.get('cost_usd', 0.0):.5f} USD")
        print(res.get("summary") or res.get("message", ""))


def main():
    init_language(load_config().get("ui", {}).get("language", "auto"))
    if len(sys.argv) > 1 and sys.argv[1] == "repl":
        _repl(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "run":
        import argparse
        parser = argparse.ArgumentParser(
            prog="server.py run",
            description="Runs a BoteX task directly from the CLI (no MCP client)."
        )
        parser.add_argument("task", help="Description of the task to perform")
        parser.add_argument("--files", nargs="*", default=[], help="Primary files affected by the task")
        parser.add_argument("--workspace", "-w", default=".", help="Project working directory")
        parser.add_argument("--model", default="", help="Explicit provider model (overrides profile)")
        parser.add_argument("--profile", default="default", help="Model profile from botex.config.json (default/coding/auto-beta)")
        parser.add_argument("--provider", default="", help="API provider (openrouter/nvidia; empty = config default)")
        parser.add_argument("--mode", default="", help="Capability preset (readonly/edit/destructive/full; empty = config)")
        parser.add_argument("--recipe", default="", help="Operational persona / recipe workflow prompt (planner, reviewer, ...)")
        parser.add_argument("--max-turns", type=int, default=0, help="Step limit (0 = config value)")
        parser.add_argument("--max-tokens", type=int, default=0, help="Per-turn completion cap (0 = config; reasoning models need ~8000+)")
        parser.add_argument("--max-duration", type=int, default=0, help="Wall-clock limit in seconds (0 = config)")
        parser.add_argument("--budget", type=float, default=-1.0, help="Daily USD limit (negative = config, 0 = none)")
        parser.add_argument("--api-key", default="", help="Provider API key per-call (overrides env/.env/config)")
        parser.add_argument(
            "--allow-destructive", action="store_true",
            help="Authorizes delete_file/move_file without prompting (without it, on an interactive terminal the agent asks per operation)"
        )
        parser.add_argument(
            "--allow-exec", action="store_true",
            help="Authorizes run_command without prompting (requires exec.enabled=true in config; THIS IS NOT A SANDBOX)"
        )
        parser.add_argument(
            "--allow-net", action="store_true",
            help="Authorizes read_url for configured public hosts"
        )
        parser.add_argument(
            "--net-host", action="append", default=[],
            help="Authorize a public host for this run (repeatable)"
        )
        parser.add_argument(
            "--net-url", action="append", default=[],
            help="Authorize an exact public URL or trailing-slash subtree (repeatable)"
        )
        parser.add_argument("--output-path", default="", help="Required output file")
        parser.add_argument("--verify-command", default="", help="Allowlisted command required to pass after writes")
        args = parser.parse_args(sys.argv[2:])

        # Destructive ops: pre-authorized via flag/config, or gated per-op by an
        # interactive prompt when attached to a real terminal. In non-interactive
        # contexts (pipes, CI) without the flag the tools are not exposed at all.
        confirm_fn = None
        if not args.allow_destructive and sys.stdin.isatty():
            confirm_fn = _interactive_confirm

        res = asyncio.run(run_botex_task(
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
            verify_command=args.verify_command
        ))
        status_str = ok(t("repl.success")) if res.get("ok") else err(t("repl.failure"))
        print(f"{status_str} {t('run.status')} {res.get('status')} | {t('run.task')} {res.get('task_id')} | "
              f"{t('run.steps')} {res.get('steps')} | {t('repl.cost')} ${res.get('cost_usd', 0.0):.5f} USD")
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
            print(f"  * Task: {s['task_id']:<10} | {s['created_at']} | {s['files_count']} {t('lc.files')} | {s['size_kb']} KB ({s['age_days']} {t('lc.days')})")
    elif len(sys.argv) > 1 and sys.argv[1] == "--clean-snapshots":
        res = snapshot_manager.clean_old_snapshots(max_age_days=7.0, max_total_mb=50.0)
        print(ok(f"[OK] {res['deleted_dirs']} {t('lc.cleaned')} {res['freed_mb']} MB. {t('lc.remaining')} {res['remaining_mb']} MB."))
    elif len(sys.argv) > 1 and sys.argv[1] == "health":
        print(mark_status(asyncio.run(check_health())))
    elif len(sys.argv) > 1 and sys.argv[1] == "config":
        _cli_config(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "models":
        _cli_models(sys.argv[2:])
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

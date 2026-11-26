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
import atexit
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure base directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Fast-path for `server.py mcp-bridge`: avoid importing the full local harness
# (~2-4s on Windows) so stdio `initialize` completes well within client timeouts.
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "mcp-bridge":
    from botex.mcp_bridge import __main__ as _mcp_bridge_main

    _mcp_bridge_main.main(sys.argv[2:])
    sys.exit(0)

from botex.analytics import cli_history, cli_stats, load_analytics
from botex.benchmarks import get_recommended_models
from botex.chat import (
    default_chat_manager,
    stream_chat_turn,
)
from botex.config import (
    WorkspaceOutOfBounds,
    get_mcp_client_origin,
    is_cross_host_windows_path,
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
from botex.net_tools import fetch_url as _fetch_url
from botex.obsidian import (
    export_graph_index_markdown as _export_graph_index_markdown,
)
from botex.obsidian import (
    init_obsidian_vault as _init_obsidian_vault,
)
from botex.patch_engine import snapshot_manager
from botex.pricing import load_catalog, model_price
from botex.security import (
    coerce_text,
    filter_reserved_tags,
    mask_secrets,
    resolve_safe_path,
    sanitize_identifier,
    sanitize_origin_claim,
    wrap_untrusted_content,
    zdr_enforced,
)
from botex.ui import accent, dim, err, mark_status, ok, title, warn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from typing_extensions import TypedDict

from botex.core.blackbox import (
    get_rap_sheet as _get_rap_sheet,
)
from botex.core.blackbox import (
    record_incident as _record_incident,
)
from botex.core.events import drop_emitter
from botex.core.swarm_store import (
    get_active_leases as _get_active_leases,
)
from botex.core.swarm_store import (
    get_bot as _get_bot,
)
from botex.core.swarm_store import (
    get_bot_brief as _get_bot_brief,
)
from botex.core.swarm_store import (
    list_bots as _list_bots,
)
from botex.core.swarm_store import (
    record_bot_domain_turn as _record_bot_domain_turn,
)
from botex.core.swarm_store import (
    upsert_host_lease as _upsert_host_lease,
)
from botex.memory import (
    close_index_dbs,
)
from botex.memory import (
    delete_memory as _delete_memory,
)
from botex.memory import (
    deterministic_recall as _deterministic_recall,
)
from botex.memory import (
    find_graph_health as _find_graph_health,
)
from botex.memory import (
    gather_recall_context as _gather_recall_context,
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
    reflect_memory as _reflect_memory,
)
from botex.memory import (
    save_memory as _save_memory,
)
from botex.memory import (
    search_memory as _search_memory,
)
from botex.providers import ProviderRegistry, resolve_local_llm_base_url, sanitize_provider_error
from botex.server.task_store import (
    load_task_persistent,
    load_tasks_persistent,
    save_task_persistent,
)

try:
    from botex.core.tool_executor import resolve_edge_executor
except ImportError:
    resolve_edge_executor = None

mcp = MCPServer("BoteX-Harness")
logger = logging.getLogger(__name__)
atexit.register(close_index_dbs)


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


# Candidate secret patterns at the end of a string
# These are incomplete tokens that, if continued, could match _SECRET_PATTERNS
_CANDIDATE_PATTERNS = [
    # OpenRouter / OpenAI: sk- or sk-proj- followed by secret chars
    re.compile(r"(?:\b|_)sk-[a-zA-Z0-9\-_]*$"),
    # AWS Access Key ID: AKIA followed by uppercase alnum
    re.compile(r"\bAKIA[0-9A-Z]*$"),
    # Provider prefixes: ghp_, gho_, xoxb-, nvapi-, etc.
    re.compile(
        r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[bapors]-|"
        r"sk_live_|sk_test_|rk_live_|AIza|npm_|pypi-|nvapi-)[A-Za-z0-9_\-]*$"
    ),
    # Bearer tokens: bearer or bearer followed by token chars
    re.compile(r"(?i)\bbearer(?:\s+[a-zA-Z0-9\-_\.]*)?$"),
    # AWS secret access key config line
    re.compile(r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[=:]\s*\S*$"),
    # Generic passwords/tokens in config with optional space/separator
    re.compile(
        r'(?i)\b(password|passwd|token|secret|api[_\-]?key|auth[_\-]?key)\s*(?:[=:]\s*["\']?[^\s"\']*)?$'
    ),
    # PEM key blocks and preamble prefixes
    re.compile(r"-----BEGIN[A-Z0-9 ]*(?:PRIVATE KEY-----)?.*?$", re.DOTALL),
    re.compile(r"-----(?:B(?:E(?:G(?:I(?:N)?)?)?)?)?$", re.IGNORECASE),
    # Redaction tag lookahead to prevent literal tag splitting mid-boundary
    re.compile(r"\[(?:R|RE|RED|REDA|REDAC|REDACT|REDACTE|REDACTED)?$", re.IGNORECASE),
    # Long base64 token that could reach 40+ chars
    re.compile(r"(?<![a-zA-Z0-9/+])[a-zA-Z0-9/+]{20,}$"),
]

# Secret keywords and provider prefixes whose sub-prefixes must be withheld during streaming
_CANDIDATE_PREFIX_WORDS = (
    "github_pat_",
    "glpat-",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "sk_live_",
    "sk_test_",
    "rk_live_",
    "AIza",
    "npm_",
    "pypi-",
    "nvapi-",
    "sk-",
    "AKIA",
    "bearer",
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "api-key",
    "apikey",
    "auth_key",
    "auth-key",
    "authkey",
    "aws_secret_access_key",
    "aws-secret-access-key",
    "awssecretaccesskey",
)

_ALL_SHORT_PREFIXES = sorted(
    {w[:i].lower() for w in _CANDIDATE_PREFIX_WORDS for i in range(1, len(w) + 1)},
    key=lambda s: (-len(s), s),
)

# Short candidate prefixes at word boundaries that could become one of the above
_SHORT_CANDIDATE_PATTERN = re.compile(
    r"(?:\b|_)(?:" + "|".join(re.escape(p) for p in _ALL_SHORT_PREFIXES) + r")$",
    re.IGNORECASE,
)


def _find_candidate_suffix_len(text: str) -> int:
    """Return the length of the trailing candidate secret suffix that should be withheld, or 0."""
    max_len = 0
    for pat in _CANDIDATE_PATTERNS:
        m = pat.search(text)
        if m:
            c_len = len(text) - m.start()
            if c_len > max_len:
                max_len = c_len
    m_short = _SHORT_CANDIDATE_PATTERN.search(text)
    if m_short:
        c_len = len(text) - m_short.start()
        if c_len > max_len:
            max_len = c_len
    return max_len


def _slice_masked_delta(
    masked_combined: str,
    masked_carry: str,
    carry_emitted_len: Optional[int] = None,
    combined: Optional[str] = None,
) -> str:
    """Slice emitted delta from masked combined text accounting for redactions across boundary."""
    if len(masked_combined) == 0:
        return ""
    if carry_emitted_len is None:
        prev_cand_len = _find_candidate_suffix_len(masked_carry) if masked_carry else 0
        carry_emitted_len = max(0, len(masked_carry) - prev_cand_len)
    if carry_emitted_len == 0:
        return masked_combined

    tag = "[REDACTED]"
    tag_len = len(tag)
    idx = 0
    while True:
        pos = masked_combined.find(tag, idx)
        if pos == -1:
            break
        # Boundary condition fix: only prepend tag if this is a newly masked secret,
        # not a literal tag that was already present in unmasked combined text.
        if pos < carry_emitted_len <= pos + tag_len:
            is_literal_in_combined = (
                combined is not None
                and pos + tag_len <= len(combined)
                and combined[pos : pos + tag_len] == tag
            )
            tag_already_emitted = (
                pos + tag_len <= len(masked_carry) and masked_carry[pos : pos + tag_len] == tag
            )
            if not is_literal_in_combined and not tag_already_emitted:
                remainder = masked_combined[pos + tag_len :]
                return tag + remainder
        idx = pos + 1

    if len(masked_combined) <= carry_emitted_len:
        return ""

    return masked_combined[carry_emitted_len:]


def mask_stream_chunk(chunk: Any, carry: str = "", buffer_size: int = 128) -> Tuple[Any, str]:
    """Mask secrets in streaming chunks with candidate secret withholding and carry buffer."""
    if not isinstance(chunk, dict):
        return chunk, carry
    ctype = chunk.get("type")
    if ctype in ("content_chunk", "chunk", "reasoning_chunk") and "delta" in chunk:
        delta = str(chunk["delta"])
        combined = carry + delta
        masked_combined = mask_secrets(combined)
        masked_carry = mask_secrets(carry)

        prev_cand_len = _find_candidate_suffix_len(masked_carry) if masked_carry else 0
        carry_emitted_len = max(0, len(masked_carry) - prev_cand_len)

        curr_cand_len = _find_candidate_suffix_len(masked_combined)
        if curr_cand_len > 0:
            safe_masked_combined = masked_combined[:-curr_cand_len]
        else:
            safe_masked_combined = masked_combined

        emit_delta = _slice_masked_delta(
            safe_masked_combined, masked_carry, carry_emitted_len, combined=combined
        )
        new_carry = combined[-buffer_size:] if buffer_size > 0 else ""
        res = dict(chunk)
        res["delta"] = emit_delta
        if "accumulated" in res:
            res["accumulated"] = mask_secrets(str(res["accumulated"]))
        return res, new_carry

    # Stream completion / flush: if chunk is terminal ('done', 'finish', etc.), flush any pending text
    if ctype in ("done", "finish") and carry:
        prev_cand_len = _find_candidate_suffix_len(carry)
        flushed = ""
        if prev_cand_len > 0:
            masked_carry = mask_secrets(carry)
            flushed = masked_carry[-prev_cand_len:]
        carry = ""
        masked = mask_secrets_recursive(chunk)
        if flushed:
            if "delta" in masked:
                masked["delta"] = flushed + str(masked["delta"])
            else:
                masked["delta"] = flushed
        return masked, carry

    masked = mask_secrets_recursive(chunk)
    return masked, carry


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
    cost_str = f"${float(res.get('cost_usd') or 0.0):.5f} USD"
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


def _task_persist_workspace(entry: dict[str, Any]) -> str:
    ws = str((entry.get("request") or {}).get("workspace_dir") or "")
    return "" if ws.startswith("edge://") else ws


async def _persist_task_entry(task_id: str, entry: dict[str, Any]) -> None:
    await asyncio.to_thread(save_task_persistent, task_id, entry, _task_persist_workspace(entry))


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
        await _persist_task_entry(task_id, entry)


def _cleanup_tasks() -> None:
    ttl = int(load_config().get("engine", {}).get("task_result_ttl_s", 3600))
    now = time.time()
    for task_id, entry in list(_TASKS.items()):
        fin = entry.get("restored_at") or entry.get("finished_at") or entry.get("created_at") or 0
        if entry.get("status") in {"DONE", "FAILED", "CANCELLED"} and (now - fin) > ttl:
            _TASKS.pop(task_id, None)
            # arena v2.1.0 F6: evict the event emitter alongside the task —
            # drop_emitter had zero production callers -> unbounded growth.
            drop_emitter(task_id)


def _resolve_call_origin(origin: str = "") -> str:
    """Resolve per-call provenance '<agent>@<host>', falling back to session headers."""
    return sanitize_origin_claim(origin or "") or get_mcp_client_origin("botex")


# ---------------------------------------------------------------------------
# Tethered Bots visibility — Cloud Soul store + edge fast-cache fallbacks
# ---------------------------------------------------------------------------

# Per-session brief dedup: (session_id, slug) -> last emitted etag. Keeps
# recall/read_memory piggybacks cheap — the same unchanged brief is never
# re-broadcast within one client session.
_SEEN_BRIEF_ETAGS_MAX = 4096
_SEEN_BRIEF_ETAGS_TTL_S = 1800.0
_SEEN_BRIEF_ETAGS: "OrderedDict[tuple[str, str], tuple[str, float]]" = OrderedDict()


def _reset_seen_brief_etags() -> None:
    _SEEN_BRIEF_ETAGS.clear()


def _record_turn_for_projects(projects: Any) -> None:
    """Increment domain turn counters best-effort for matching active bots."""
    items = projects if isinstance(projects, (list, tuple, set)) else [projects]
    for item in items:
        slug_or_ns = str(item or "").strip()
        if slug_or_ns and slug_or_ns != "_unassigned":
            try:
                _record_bot_domain_turn(slug_or_ns)
            except Exception:
                logger.debug("record_bot_domain_turn skipped", exc_info=True)


def _local_bot_briefs(namespace: str = "") -> list[dict[str, Any]]:
    """Edge fast-cache briefs; empty list on any read error (offline-safe)."""
    try:
        from botex.edge.runner import read_local_bot_briefs

        return read_local_bot_briefs(namespace=namespace)
    except Exception:
        return []


def _cloud_bot_brief(slug: str) -> Optional[dict[str, Any]]:
    try:
        return _get_bot_brief(slug)
    except Exception:
        return None


def _swarm_guardians() -> list[dict[str, Any]]:
    """Compact guardian roster from Cloud Soul; edge fast-cache fallback."""
    guardians: list[dict[str, Any]] = []
    try:
        bots = _list_bots(status="active") or []
    except Exception:
        bots = []
    for b in bots:
        slug = str(b.get("slug") or "")
        if not slug:
            continue
        guardians.append(
            {
                "slug": slug,
                "namespace": str(b.get("namespace") or ""),
                "status": str(b.get("status") or "active"),
                "role_desc": str(b.get("role_desc") or "")[:80],
            }
        )
    if not guardians:
        for lb in _local_bot_briefs():
            slug = str(lb.get("slug") or "")
            if not slug:
                continue
            guardians.append(
                {
                    "slug": slug,
                    "namespace": str(lb.get("namespace") or ""),
                    "status": "active",
                    "role_desc": str(lb.get("role_desc") or "")[:80],
                }
            )
    return guardians


def _local_brief_for_slug(slug: str) -> Optional[dict[str, Any]]:
    return next((lb for lb in _local_bot_briefs() if str(lb.get("slug") or "") == slug), None)


def _attach_swarm_visibility_piggyback(
    payload: dict[str, Any], *, namespace: str = "", session_key: str = ""
) -> dict[str, Any]:
    """Attach ``swarm_guardians`` + a session-ETag-deduped ``active_brief`` to a
    successful tool payload (recall_memory / read_memory)."""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return payload
    guardians = _swarm_guardians()
    payload["swarm_guardians"] = guardians
    ns = str(namespace or "").strip()
    if ns:
        slugs = [g["slug"] for g in guardians if g["namespace"] == ns or g["slug"] == ns]
    else:
        slugs = [g["slug"] for g in guardians if g["namespace"] in ("swarm", "global")]
    if not slugs:
        return payload
    slug = slugs[0]
    brief = _cloud_bot_brief(slug) or _local_brief_for_slug(slug)
    if not brief:
        return payload
    etag = str(brief.get("etag") or "")
    session_id = str(session_key or "").strip() or get_mcp_client_origin("botex")
    key = (session_id, slug)
    now_mono = time.monotonic()
    cached = _SEEN_BRIEF_ETAGS.get(key)
    if (
        cached is not None
        and cached[0] == etag
        and (now_mono - cached[1]) < _SEEN_BRIEF_ETAGS_TTL_S
    ):
        _SEEN_BRIEF_ETAGS.move_to_end(key)
        return payload
    _SEEN_BRIEF_ETAGS[key] = (etag, now_mono)
    _SEEN_BRIEF_ETAGS.move_to_end(key)
    while len(_SEEN_BRIEF_ETAGS) > _SEEN_BRIEF_ETAGS_MAX:
        _SEEN_BRIEF_ETAGS.popitem(last=False)
    payload["active_brief"] = {
        "slug": slug,
        "namespace": str(brief.get("namespace") or ""),
        "etag": etag,
        "fencing_epoch": int(brief.get("fencing_epoch") or 0),
        "content": str(brief.get("content") or ""),
    }
    return payload


_INSTRUCTIONS_EXCERPT_CHARS = 4000


def _read_instructions_excerpt(instructions_path: str) -> str:
    """Bounded, secret-masked excerpt of a registered bot's instructions file.

    The path must resolve INSIDE the effective workspace — traversal and
    out-of-workspace absolute paths fail closed to "".
    """
    try:
        target = resolve_safe_path(resolve_workspace(""), str(instructions_path or "").strip())
        if not target.is_file():
            return ""
        raw = target.read_text(encoding="utf-8", errors="replace")
        return mask_secrets(raw[:_INSTRUCTIONS_EXCERPT_CHARS])
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public MCP Tools (Exposed to MCP clients)
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_task(
    task: str,
    files: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    workspace_dir: Annotated[
        str, Field(description="Workspace path or 'edge://<dev>/<ws>'. Empty = default.")
    ] = "",
    model: str = "",
    profile: Annotated[
        str, Field(description="Config profile ('default'|'coding'|'auto-beta'|'fast').")
    ] = "",
    provider: Annotated[
        str,
        Field(
            description="openrouter-free|nvidia|google|ollama|groq|cloudflare|openrouter. REQUIRED unless BOTEX_PROVIDER is set."
        ),
    ] = "",
    mode: Annotated[
        str, Field(description="readonly|edit|destructive|full (empty = config).")
    ] = "",
    max_turns: Annotated[int, Field(description="Max loop steps (0 = config).")] = 0,
    max_tokens: Annotated[
        int, Field(description="Per-turn token cap (0 = config; ~8000+ for reasoning).")
    ] = 0,
    max_duration_s: Annotated[
        int, Field(description="Wall-clock limit in seconds (0 = config).")
    ] = 0,
    budget_limit_usd: Annotated[
        float, Field(description="Daily USD cap (<0 = config, 0 = unlimited).")
    ] = -1.0,
    allow_destructive: Annotated[
        bool, Field(description="Permit delete_file/move_file (needs user consent).")
    ] = False,
    allow_exec: Annotated[
        bool, Field(description="Permit run_command; not a sandbox (needs user consent).")
    ] = False,
    allow_net: Annotated[bool, Field(description="Permit read-only read_url.")] = False,
    net_allowed_hosts: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    net_allowed_urls: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    api_key: str = "",
    output_path: Annotated[
        str, Field(description="Required workspace-relative output file for DONE.")
    ] = "",
    verify_command: Annotated[
        str, Field(description="Allowlisted command that must pass before DONE.")
    ] = "",
    recipe: Annotated[
        str,
        Field(
            description="Workflow persona (planner|code-explorer|reviewer|security-reviewer|build-resolver|tdd)."
        ),
    ] = "",
    allow_non_zdr: Annotated[
        bool, Field(description="Consent to relax Zero Data Retention (needed for :free).")
    ] = False,
    allow_expensive_model: bool = False,
    approval_policy: Annotated[str, Field(description="none|high_risk|mutations|all_risky.")] = "",
    origin: str = "",
) -> dict[str, Any]:
    """Async version of `run_subagent`: queues a task and returns `{task_id, status: "QUEUED"}` for `get_task_status`."""
    _cleanup_tasks()
    effective_prov = (provider or "").strip() or os.environ.get("BOTEX_PROVIDER", "").strip()
    avail_provs = ", ".join((load_config().get("providers") or {}).keys())
    if not effective_prov:
        return {
            "ok": False,
            "status": "CONFIG_ERROR",
            "failure_kind": "security_or_policy_block",
            "message": (
                "Explicit 'provider' is required — delegation without one "
                f"silently routes to paid 'openrouter'. Specify one of: {avail_provs}."
            ),
        }
    _resolved_prov, _prov_cfg = resolve_provider(effective_prov)
    if not _prov_cfg:
        return {
            "ok": False,
            "status": "CONFIG_ERROR",
            "failure_kind": "security_or_policy_block",
            "message": (
                f"Unknown provider '{effective_prov}'. Available providers: {avail_provs}."
            ),
        }
    provider = _resolved_prov
    max_queued = int(load_config().get("engine", {}).get("max_queued_tasks", 32))
    active_count = sum(entry.get("status") in {"QUEUED", "RUNNING"} for entry in _TASKS.values())
    if active_count >= max_queued:
        return {
            "ok": False,
            "status": "TOO_MANY_QUEUED",
            "message": f"Task queue limit reached ({max_queued}).",
        }
    effective_origin = _resolve_call_origin(origin)
    task_id = uuid.uuid4().hex[:12]
    edge_exec = None
    if resolve_edge_executor is not None and workspace_dir and str(workspace_dir).strip():
        edge_exec = resolve_edge_executor(str(workspace_dir).strip())
    kwargs = {
        "task": task,
        "files": files,
        # Logical edge workspace IDs are not local paths — skip local
        # containment resolution; run_subagent routes via the executor.
        "workspace_dir": workspace_dir
        if edge_exec is not None
        else resolve_workspace(workspace_dir),
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
        "allow_expensive_model": allow_expensive_model,
        "approval_policy": approval_policy,
        "origin": effective_origin,
    }
    persisted_request = dict(kwargs)
    persisted_request["api_key"] = ""
    entry = {
        "task_id": task_id,
        "task": mask_secrets(task),
        "request": mask_secrets_recursive(persisted_request),
        "status": "QUEUED",
        "created_at": time.time(),
        "finished_at": None,
        "result": None,
        "result_data": None,
        "origin": effective_origin,
        "async_task": None,
    }
    _TASKS[task_id] = entry
    await _persist_task_entry(task_id, entry)
    entry["async_task"] = asyncio.create_task(_background_task(task_id, kwargs))
    return {"task_id": task_id, "status": "QUEUED", "origin": effective_origin}


@mcp.tool()
async def get_task_status(
    task_id: str,
    timeout_s: Annotated[int, Field(description="Server-side long-poll cap up to 600s.")] = 0,
    collected_by: Annotated[str, Field(description="Optional collector origin override.")] = "",
) -> dict[str, Any]:
    """Return status and `result_data` for a `start_task` run (check `result_data.ok` for verified outcome; delivery marks `collected`)."""
    _cleanup_tasks()
    entry = _TASKS.get(task_id)
    if not entry:
        # Terminal results outlive their in-memory TTL on disk — O(1)
        # restore of this task only, merged on the event loop so worker
        # threads never mutate _TASKS.
        restored = await asyncio.to_thread(load_task_persistent, task_id)
        if restored is not None:
            restored["restored_at"] = time.time()
            entry = _TASKS.setdefault(restored["task_id"], restored)
    if not entry:
        return {"ok": False, "status": "NOT_FOUND", "task_id": task_id}
    deadline = time.monotonic() + max(0, min(int(timeout_s), 600))
    while True:
        if entry.get("status") in {"DONE", "FAILED", "CANCELLED"}:
            result: dict[str, Any] = {
                "ok": True,
                "task_id": task_id,
                "status": entry["status"],
                "collected": True,
            }
            if entry.get("origin"):
                result["origin"] = entry["origin"]
            # `result` is the pretty-text report (legacy); `result_data` is the
            # structured engine result (status, failure_kind, attempts, ...).
            result["result"] = entry.get("result", "")
            if entry.get("result_data"):
                result["result_data"] = entry["result_data"]
            # Mark collected only as the terminal payload is actually being
            # returned — deadline and abort paths must never touch the flag.
            entry["collected"] = True
            entry["collected_at"] = time.time()
            entry["collected_by"] = _resolve_call_origin(collected_by)
            await _persist_task_entry(task_id, entry)
            return result
        if time.monotonic() >= deadline:
            return {
                "ok": True,
                "task_id": task_id,
                "status": entry["status"],
                "deadline_expires": True,
            }
        await asyncio.sleep(1.5)


@mcp.tool()
async def list_tasks(
    uncollected_only: Annotated[
        bool, Field(description="Only terminal tasks not yet collected.")
    ] = False,
    origin: Annotated[str, Field(description="Filter by exact '<agent>@<host>' origin.")] = "",
    limit: Annotated[int, Field(description="Max rows (1..200, newest first).")] = 50,
) -> dict[str, Any]:
    """List background tasks (`{task_id, status, created_at, origin, collected}`) for orphan/startup recovery."""
    _cleanup_tasks()
    max_rows = max(1, min(int(limit), 200))
    restored = await asyncio.to_thread(load_tasks_persistent)
    merged: dict[str, dict[str, Any]] = dict(restored)
    merged.update(_TASKS)
    entries = sorted(merged.items(), key=lambda kv: kv[1].get("created_at") or 0, reverse=True)
    tasks = []
    for tid, entry in entries:
        status = entry.get("status")
        if uncollected_only and (
            status not in {"DONE", "FAILED", "CANCELLED"} or entry.get("collected") is True
        ):
            continue
        if origin and entry.get("origin") != origin:
            continue
        tasks.append(
            {
                "task_id": tid,
                "status": status,
                "created_at": entry.get("created_at"),
                "origin": entry.get("origin", ""),
                "collected": entry.get("collected") is True,
            }
        )
        if len(tasks) >= max_rows:
            break
    return {"ok": True, "tasks": tasks, "count": len(tasks)}


@mcp.tool()
async def fetch_url(
    url: str,
    max_bytes: Annotated[int, Field(description="Max response bytes (0 = default cap).")] = 0,
) -> dict[str, Any]:
    """Fetch one public http(s) text document with SSRF guards and secret masking."""
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
    include_free: Annotated[
        bool,
        Field(description="Include :free non-ZDR models (needs allow_non_zdr or openrouter-free)."),
    ] = False,
    provider: str = "",
    profile: str = "",
    tier: str = "auto",
    recipe: str = "",
) -> str:
    """Rank cost-effective models from benchmarks; call before `run_subagent`/`start_task` if model is unspecified."""
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
    files: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    workspace_dir: Annotated[
        str, Field(description="Workspace path or 'edge://<dev>/<ws>'. Empty = default.")
    ] = "",
    model: str = "",
    profile: Annotated[
        str, Field(description="Config profile ('default'|'coding'|'auto-beta'|'fast').")
    ] = "",
    provider: Annotated[
        str,
        Field(
            description="openrouter-free|nvidia|google|ollama|groq|cloudflare|openrouter. REQUIRED unless BOTEX_PROVIDER is set."
        ),
    ] = "",
    mode: Annotated[
        str, Field(description="readonly|edit|destructive|full ('readonly' is hard read-only).")
    ] = "",
    max_turns: Annotated[int, Field(description="Max loop steps (0 = config).")] = 0,
    max_tokens: Annotated[
        int, Field(description="Per-turn token cap (0 = config; ~8000+ for reasoning).")
    ] = 0,
    max_duration_s: Annotated[
        int, Field(description="Wall-clock limit in seconds (0 = config).")
    ] = 0,
    budget_limit_usd: Annotated[
        float, Field(description="Daily USD cap (<0 = config, 0 = unlimited).")
    ] = -1.0,
    allow_destructive: Annotated[
        bool, Field(description="Permit delete_file/move_file (needs user consent).")
    ] = False,
    allow_exec: Annotated[
        bool, Field(description="Permit run_command; not a sandbox (needs user consent).")
    ] = False,
    api_key: str = "",
    output_path: Annotated[
        str, Field(description="Required workspace-relative output file for DONE.")
    ] = "",
    allow_net: Annotated[bool, Field(description="Permit read-only read_url.")] = False,
    net_allowed_hosts: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    net_allowed_urls: list[str] = [],  # noqa: B006  # FastMCP JSON schema default
    verify_command: Annotated[
        str, Field(description="Allowlisted command that must pass before DONE.")
    ] = "",
    recipe: Annotated[
        str,
        Field(
            description="Workflow persona (planner|code-explorer|reviewer|security-reviewer|build-resolver|tdd)."
        ),
    ] = "",
    allow_non_zdr: Annotated[
        bool, Field(description="Consent to relax Zero Data Retention (needed for :free).")
    ] = False,
    allow_expensive_model: bool = False,
    approval_policy: Annotated[str, Field(description="none|high_risk|mutations|all_risky.")] = "",
    origin: str = "",
) -> Annotated[CallToolResult, SubagentTaskResult]:
    """Run BoteX synchronously inside `workspace_dir`; call `recommend_models` first if model is unspecified (`ok` is true only when `status == DONE`)."""
    effective_prov = (provider or "").strip() or os.environ.get("BOTEX_PROVIDER", "").strip()
    avail_provs = ", ".join((load_config().get("providers") or {}).keys())
    _err_base = {
        "ok": False,
        "status": "CONFIG_ERROR",
        "failure_kind": "security_or_policy_block",
        "files_touched": [],
        "exec_ran": False,
        "steps": 0,
        "attempts": [],
        "duration_s": 0.0,
        "cost_usd": 0.0,
    }
    if not effective_prov:
        err_res = {
            **_err_base,
            "message": (
                "Explicit 'provider' is required — delegation without one "
                f"silently routes to paid 'openrouter'. Specify one of: {avail_provs}."
            ),
        }
        return CallToolResult(
            content=[TextContent(type="text", text=_format_result_text(err_res))],
            structured_content=err_res,
        )
    _resolved_prov, _prov_cfg = resolve_provider(effective_prov)
    if not _prov_cfg:
        err_res = {
            **_err_base,
            "message": (
                f"Unknown provider '{effective_prov}'. Available providers: {avail_provs}."
            ),
        }
        return CallToolResult(
            content=[TextContent(type="text", text=_format_result_text(err_res))],
            structured_content=err_res,
        )
    provider = _resolved_prov
    edge_exec = None
    if resolve_edge_executor is not None and workspace_dir and str(workspace_dir).strip():
        edge_exec = resolve_edge_executor(str(workspace_dir).strip())
    # Edge workspaces are logical IDs — local checkpoints/offloads still need
    # a real dir, so fall back to the default workspace as local scratch.
    local_scratch = resolve_workspace("") if edge_exec is not None else None
    res = await run_botex_task(
        task=task,
        files=files,
        workspace_dir=local_scratch if local_scratch is not None else workspace_dir,
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
        allow_expensive_model=True if allow_expensive_model else None,
        approval_policy=approval_policy or None,
        tool_executor=edge_exec,
    )

    effective_origin = _resolve_call_origin(origin)
    res = _masked_result(res)
    res["origin"] = effective_origin
    res = _offload_report(
        res, local_scratch if local_scratch is not None else resolve_workspace(workspace_dir)
    )
    return CallToolResult(
        content=[TextContent(type="text", text=_format_result_text(res))],
        structured_content=res,
    )


@mcp.tool()
async def get_outline(
    path: str,
    workspace_dir: Annotated[str, Field(description="Workspace root (empty = default).")] = "",
) -> dict[str, Any]:
    """Return file skeleton (classes, functions, line numbers) without reading the full file."""
    return _get_file_outline(path, resolve_workspace(workspace_dir))


@mcp.tool()
async def save_memory(
    title: str,
    content: str,
    workspace_dir: Annotated[
        str, Field(description="Workspace root (empty = default; use '' for global swarm on VPS).")
    ] = "",
    kind: Annotated[
        str, Field(description="context|decision|handoff|lesson|belief|heuristic.")
    ] = "context",
    tags: Optional[list[str]] = None,
    memory_id: str = "",
    scope: Annotated[str, Field(description="local|global.")] = "local",
    confidence: Annotated[float, Field(description="Score in [0.0, 1.0].")] = 1.0,
    project: Annotated[
        Optional[str], Field(description="Project slug (e.g. 'swarm', 'botex').")
    ] = None,
    origin: Annotated[
        str,
        Field(description="'<agent>@<host>'; reserved claims (human/curator/operator) rejected."),
    ] = "",
) -> dict[str, Any]:
    """Save a persistent note into the Memory Vault (secret-masked); never log penal violations here."""
    effective_origin = _resolve_call_origin(origin)
    ws = (
        "."
        if str(scope).strip().lower() == "global" and is_cross_host_windows_path(workspace_dir)
        else resolve_workspace(workspace_dir)
    )
    res = _save_memory(
        workspace_dir=ws,
        title=title,
        content=content,
        kind=kind,
        tags=filter_reserved_tags(tags),
        memory_id=memory_id or None,
        origin=effective_origin,
        scope=scope,
        confidence=confidence,
        project=project,
    )
    norm_proj = str(res.get("project") or project or "").strip() if isinstance(res, dict) else ""
    if (
        isinstance(res, dict)
        and res.get("ok")
        and norm_proj
        and norm_proj != "_unassigned"
        and str(res.get("status") or "active").strip().lower() == "active"
    ):
        await asyncio.to_thread(_record_turn_for_projects, norm_proj)
    return res


@mcp.tool()
async def search_memory(
    workspace_dir: Annotated[str, Field(description="Workspace root (empty = default).")] = "",
    query: str = "",
    tag: str = "",
    kind: Annotated[
        str, Field(description="context|decision|handoff|lesson|belief|heuristic.")
    ] = "",
    scope: Annotated[str, Field(description="local|global|all.")] = "local",
    since: Annotated[str, Field(description="ISO-8601 lower bound.")] = "",
    until: Annotated[str, Field(description="ISO-8601 upper bound.")] = "",
    min_confidence: Optional[float] = None,
    project: str = "",
) -> dict[str, Any]:
    """Search Memory Vault metadata by keyword, tag, kind, or time range (prefer `recall_memory` first)."""
    res = _search_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        query=query,
        tag=tag or None,
        kind=kind or None,
        scope=scope,
        since=since or None,
        until=until or None,
        min_confidence=min_confidence,
        project=project or None,
    )
    if isinstance(res, dict) and isinstance(res.get("memories"), list):
        res = dict(res)
        res["memories"] = [{k: v for k, v in m.items() if k != "content"} for m in res["memories"]]
    return res


@mcp.tool()
async def reflect_memory(
    query: str,
    workspace_dir: Annotated[str, Field(description="Workspace root (empty = default).")] = "",
    topic: Optional[str] = None,
    min_confidence: float = 0.5,
    scope: Annotated[str, Field(description="local|global|all.")] = "all",
    project: str = "",
) -> dict[str, Any]:
    """Deterministically group and rank vault entries by kind, confidence, and recency (no LLM)."""
    return _reflect_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        query=query,
        topic=topic,
        min_confidence=min_confidence,
        scope=scope,
        project=project or None,
    )


@mcp.tool()
async def recall_memory(
    query: str,
    workspace_dir: Annotated[str, Field(description="Workspace root (empty = default).")] = "",
    topic: Optional[str] = None,
    scope: Annotated[str, Field(description="local|global|all.")] = "all",
    project: str = "",
) -> dict[str, Any]:
    """Default first call for memory lookup: returns 1-hop graph synthesis with sources (`read_memory` for raw notes)."""
    return await _recall_memory_impl(
        workspace_dir=resolve_workspace(workspace_dir),
        query=query,
        topic=topic,
        scope=scope,
        project=project or None,
    )


_RECALL_GATE_LOCK = asyncio.Lock()
_RECALL_GATE = {"tokens": 0.0, "ts": 0.0, "initialized": False}
_RECALL_GATE_MAX_WAIT_S = 10.0


async def _recall_rate_gate() -> Optional[dict[str, Any]]:
    """Token-bucket gate enforcing ``rate_limits.recall_rpm`` (arena v2.1.0 F3).

    Concurrent swarm recalls queue instead of cascading into upstream 429s;
    beyond a 10s wait the caller gets a RECALL_RATE_LIMITED envelope.
    ``recall_rpm <= 0`` or missing -> gate disabled.
    """
    raw = (load_config().get("rate_limits", {}) or {}).get("recall_rpm", 0)
    try:
        rpm = float(raw)
    except (TypeError, ValueError):
        rpm = 0.0
    if rpm <= 0:
        return None
    rate = rpm / 60.0
    capacity = max(1.0, rpm)
    deadline = time.monotonic() + _RECALL_GATE_MAX_WAIT_S
    while True:
        async with _RECALL_GATE_LOCK:
            now = time.monotonic()
            if not _RECALL_GATE["initialized"]:
                _RECALL_GATE["tokens"] = capacity
                _RECALL_GATE["ts"] = now
                _RECALL_GATE["initialized"] = True
            _RECALL_GATE["tokens"] = min(
                capacity, _RECALL_GATE["tokens"] + (now - _RECALL_GATE["ts"]) * rate
            )
            _RECALL_GATE["ts"] = now
            if _RECALL_GATE["tokens"] >= 1.0:
                _RECALL_GATE["tokens"] -= 1.0
                return None
            wait = min((1.0 - _RECALL_GATE["tokens"]) / rate, _RECALL_GATE_MAX_WAIT_S)
        # Sleep OUTSIDE the lock — a queued caller must not head-of-line
        # block callers the bucket could admit right now (cross-review).
        await asyncio.sleep(wait)
        if time.monotonic() >= deadline:
            break
    return {
        "ok": False,
        "error": "RECALL_RATE_LIMITED",
        "detail": f"recall_rpm={int(rpm)} exceeded; retry shortly",
        "retry_after_s": round(60.0 / rpm, 3),
    }


def _briefing_recall_config() -> dict[str, Any]:
    try:
        mem = load_config().get("memory") or {}
        recall = mem.get("recall") or {}
        bcfg = recall.get("briefing")
        return dict(bcfg) if isinstance(bcfg, dict) else {}
    except Exception:
        return {}


def _cloud_tiers_exhausted(tier_attempts: list[dict[str, Any]]) -> bool:
    """Every cloud tier attempted and failed — the defer-path signal (plan 055)."""
    try:
        from botex.ops.curator.recall_curator import _cloud_tiers_exhausted as _impl
    except Exception:
        return False
    return _impl(tier_attempts)


def _try_briefing_first_line(
    workspace_dir: str,
    query: str,
    nodes: list[dict[str, Any]],
    project: Optional[str] = None,
    max_age_h: float = 72.0,
) -> Optional[dict[str, Any]]:
    """Zero-LLM first-line recall answer from a fresh pre-compiled briefing, or None.

    Thin wrapper over ``recall_briefing.build_briefing_answer`` (shared with
    the worker bridge); weak coverage returns None so the caller falls through
    to the LLM cascade.
    """
    try:
        from botex.ops.curator.recall_briefing import build_briefing_answer, parse_max_age_h
    except Exception:
        return None
    try:
        return build_briefing_answer(
            workspace_dir, query, nodes, project=project, max_age_h=parse_max_age_h(max_age_h)
        )
    except Exception:
        return None


async def _recall_memory_impl(
    workspace_dir: str,
    query: str,
    topic: Optional[str] = None,
    scope: str = "all",
    project: Optional[str] = None,
) -> dict[str, Any]:
    limited = await _recall_rate_gate()
    if limited is not None:
        return limited
    _recall_t0 = time.monotonic()
    try:
        ctx = _gather_recall_context(
            workspace_dir=workspace_dir,
            query=query,
            topic=topic,
            scope=scope,
            project=project,
        )
    except WorkspaceOutOfBounds:
        # Rejected path is request-tainted: static log text only (CodeQL).
        import logging

        logging.getLogger(__name__).warning(
            "workspace containment: rejected out-of-allowlist workspace_dir"
        )
        return {
            "ok": False,
            "error": "WORKSPACE_OUT_OF_BOUNDS",
            "detail": "workspace_dir is outside cloud.allowed_workspaces",
        }
    if not ctx.get("ok"):
        return ctx
    nodes = ctx.get("nodes") or []
    result: Optional[dict[str, Any]] = None
    tier_attempts: list[dict[str, Any]] = []
    briefing_served = False
    bcfg = _briefing_recall_config()
    if nodes and bcfg.get("enabled", True) and bcfg.get("serve", True):
        result = await asyncio.to_thread(
            _try_briefing_first_line,
            workspace_dir,
            query,
            nodes,
            project,
            bcfg.get("max_age_h", 72),
        )
        briefing_served = result is not None
    if nodes and result is None:
        try:
            from botex.ops.curator import synthesize_recall

            result = await synthesize_recall(query, nodes)
        except Exception as exc:  # cascade unavailable -> deterministic
            import logging

            logging.getLogger(__name__).info(
                "recall_memory LLM cascade unavailable: %s", type(exc).__name__
            )
            result = None
        if isinstance(result, dict):
            raw_attempts = result.get("tier_attempts")
            if isinstance(raw_attempts, list):
                tier_attempts = raw_attempts
            if not result.get("answer"):
                result = None  # cascade reported failure -> deterministic
        else:
            result = None
    defer = False
    if result is not None:
        recall_status = "BRIEFING_SERVED" if briefing_served else "SYNTHESIZED"
    elif nodes:
        defer = _cloud_tiers_exhausted(tier_attempts)
        recall_status = "QUOTA_EXHAUSTED_DEFER" if defer else "FALLBACK_MODELS_FAILED"
    else:
        recall_status = "ZERO_NODES_FOUND"
    if not result:
        result = _deterministic_recall(query, nodes)
        if defer:
            from botex.ops.curator.recall_curator import RECALL_QUOTA_DEFER_HINT

            result["answer"] = str(result.get("answer") or "") + RECALL_QUOTA_DEFER_HINT
    logger.info(
        "recall outcome status=%s coverage=%s tier=%s latency_ms=%d path=mcp",
        recall_status,
        "briefing" if briefing_served else "miss",
        str(result.get("tier_used") or "-"),
        int((time.monotonic() - _recall_t0) * 1000),
    )
    return _attach_swarm_visibility_piggyback(
        {
            "ok": True,
            **result,
            "recall_status": recall_status,
            "tier_attempts": tier_attempts,
            "workspace_searched": workspace_dir,
            "context_nodes": len(nodes),
            "truncated": bool(ctx.get("truncated")),
        },
        namespace=str(project or ""),
    )


@mcp.tool()
async def read_memory(
    memory_id: str,
    workspace_dir: str = "",
    scope: Annotated[str, Field(description="local|global|all.")] = "local",
) -> dict[str, Any]:
    """Read full raw content and metadata of a Memory Vault entry by ID."""
    payload = _read_memory(
        workspace_dir=resolve_workspace(workspace_dir),
        memory_id=memory_id,
        scope=scope,
    )
    return _attach_swarm_visibility_piggyback(
        payload,
        namespace=str((payload.get("metadata") or {}).get("project") or "")
        if isinstance(payload, dict)
        else "",
    )


@mcp.tool()
async def delete_memory(
    memory_id: str,
    workspace_dir: str = "",
    scope: Annotated[str, Field(description="local|global|all.")] = "local",
) -> dict[str, Any]:
    """Delete a Memory Vault entry by ID and clean its graph links."""
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
    """Read full offloaded execution or terminal log from `.botex/scratchpad/<memory_id>.log`."""
    return _read_scratchpad(
        memory_id=memory_id,
        cwd=resolve_workspace(workspace_dir),
    )


@mcp.tool()
async def get_backlinks(
    memory_id: str,
    workspace_dir: str = "",
    scope: Annotated[str, Field(description="local|global|all.")] = "local",
) -> dict[str, Any]:
    """Find all Memory Vault entries linking to `memory_id` via `[[wikilinks]]`."""
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
    scope: Annotated[str, Field(description="local|global.")] = "local",
    append: bool = False,
    origin: str = "",
) -> dict[str, Any]:
    """Update or append a Markdown section in a memory note without altering frontmatter or other sections."""
    effective_origin = _resolve_call_origin(origin)
    ws = resolve_workspace(workspace_dir)
    res = _patch_memory_section(
        workspace_dir=ws,
        memory_id=memory_id,
        section=section,
        new_content=new_content,
        scope=scope,
        append=append,
    )
    res["origin"] = effective_origin
    if res.get("ok"):
        try:
            note = await asyncio.to_thread(
                _read_memory, workspace_dir=ws, memory_id=memory_id, scope=scope
            )
            meta = note.get("metadata") if isinstance(note, dict) and note.get("ok") else None
            if (
                isinstance(meta, dict)
                and meta.get("project")
                and str(meta.get("status") or "active").strip().lower() == "active"
            ):
                await asyncio.to_thread(_record_turn_for_projects, meta.get("project"))
        except Exception:
            pass
    return res


@mcp.tool()
async def find_graph_health(
    workspace_dir: str = "",
    scope: Annotated[str, Field(description="local|global|all.")] = "local",
) -> dict[str, Any]:
    """Audit Memory Vault graph integrity (broken links, orphan notes, cycles)."""
    return _find_graph_health(
        workspace_dir=resolve_workspace(workspace_dir),
        scope=scope,
    )


@mcp.tool()
async def init_obsidian_vault(
    workspace_dir: str = "",
    scope: Annotated[str, Field(description="local|global.")] = "local",
    override_existing: bool = False,
    export_index: bool = True,
) -> dict[str, Any]:
    """Initialize or sync Obsidian vault config and optional `_GRAPH_INDEX.md`."""
    ws = resolve_workspace(workspace_dir)
    res = _init_obsidian_vault(workspace_dir=ws, scope=scope, override_existing=override_existing)
    if export_index and res.get("ok"):
        _export_graph_index_markdown(workspace_dir=ws, scope=scope)
    return res


# ---------------------------------------------------------------------------
# Tethered Bots (ADR-SWARM-02 Phase 1) — static Cloud Soul surface
# ---------------------------------------------------------------------------


@mcp.tool()
async def swarm_list_bots(
    namespace: Annotated[str, Field(description="Optional namespace filter (empty = all).")] = "",
) -> dict[str, Any]:
    """List registered Tethered Bots, active host leases, and brief metadata."""
    ns = str(namespace or "").strip()
    try:
        bots = await asyncio.to_thread(_list_bots, namespace=ns) or []
    except Exception:
        bots = []
    leases_by_bot: dict[str, list] = {}
    try:
        for lease in await asyncio.to_thread(_get_active_leases):
            bslug = str(lease.get("bot_slug") or "")
            if bslug:
                leases_by_bot.setdefault(bslug, []).append(lease)
    except Exception:
        pass
    merged: dict[str, dict[str, Any]] = {}
    for b in bots:
        slug = str(b.get("slug") or "")
        if not slug:
            continue
        brief = await asyncio.to_thread(_cloud_bot_brief, slug) or {}
        merged[slug] = {
            **b,
            "active_leases": list(leases_by_bot.get(slug) or []),
            "etag": str(brief.get("etag") or ""),
            "fencing_epoch": int(brief.get("fencing_epoch") or 0),
            "brief_updated_at": str(brief.get("updated_at") or ""),
            "source": "cloud",
        }
    for lb in await asyncio.to_thread(_local_bot_briefs, ns):
        slug = str(lb.get("slug") or "")
        if not slug or slug in merged:
            continue
        merged[slug] = {
            "slug": slug,
            "namespace": str(lb.get("namespace") or ""),
            "role_desc": str(lb.get("role_desc") or ""),
            "status": "active",
            "active_leases": list(leases_by_bot.get(slug) or []),
            "etag": str(lb.get("etag") or ""),
            "fencing_epoch": int(lb.get("fencing_epoch") or 0),
            "brief_updated_at": str(lb.get("updated_at") or ""),
            "source": "local_cache",
        }
    bots_out = sorted(merged.values(), key=lambda b: str(b.get("slug") or ""))
    return {"ok": True, "bots": bots_out, "count": len(bots_out)}


@mcp.tool()
async def bot_consult(
    bot_id: Annotated[str, Field(description="Registered bot slug.")],
    query: str,
) -> dict[str, Any]:
    """Single-hop (ttl_hops=1) consult of a Tethered Bot: returns its brief and active-only vault recall."""
    slug = sanitize_identifier(str(bot_id or ""))
    if not slug:
        return {"ok": False, "error": "INVALID_SLUG"}
    try:
        bot = await asyncio.to_thread(_get_bot, slug)
    except Exception:
        bot = None
    local_brief = None
    if bot is None:
        local_brief = await asyncio.to_thread(_local_brief_for_slug, slug)
        if local_brief is None:
            return {"ok": False, "error": "BOT_NOT_FOUND"}
    elif str(bot.get("status") or "active").strip().lower() != "active":
        return {
            "ok": False,
            "error": "BOT_INACTIVE",
            "status": str(bot.get("status") or ""),
        }
    ns = str((bot or local_brief or {}).get("namespace") or "")
    instructions = ""
    ipath = str((bot or {}).get("instructions_path") or "").strip()
    if ipath:
        instructions = await asyncio.to_thread(_read_instructions_excerpt, ipath)
    brief = await asyncio.to_thread(_cloud_bot_brief, slug) or local_brief or {}
    active_brief = {
        "slug": slug,
        "namespace": str(brief.get("namespace") or ns),
        "etag": str(brief.get("etag") or ""),
        "fencing_epoch": int(brief.get("fencing_epoch") or 0),
        "content": str(brief.get("content") or ""),
    }
    nodes: list[dict[str, Any]] = []
    try:
        ctx = await asyncio.to_thread(
            _gather_recall_context,
            workspace_dir=resolve_workspace(""),
            query=str(query or ""),
            scope="all",
            project=ns or None,
        )
        if ctx.get("ok"):
            nodes = ctx.get("nodes") or []
    except WorkspaceOutOfBounds:
        nodes = []
    except Exception:
        nodes = []
    # Hard invariant: only active notes reach the consult — candidate,
    # conflict_pending, deprecated and superseded entries never do.
    active_nodes = [
        n for n in nodes if str(n.get("status") or "active").strip().lower() == "active"
    ]
    result: Optional[dict[str, Any]] = None
    bcfg = _briefing_recall_config()
    if active_nodes and bcfg.get("enabled", True) and bcfg.get("serve", True):
        result = await asyncio.to_thread(
            _try_briefing_first_line,
            resolve_workspace(""),
            str(query or ""),
            active_nodes,
            ns or None,
            bcfg.get("max_age_h", 72),
        )
    if active_nodes and result is None:
        try:
            from botex.ops.curator import synthesize_recall

            result = await synthesize_recall(str(query or ""), active_nodes)
        except Exception:
            result = None
        if not isinstance(result, dict) or not result.get("answer"):
            result = None
    if not result:
        result = _deterministic_recall(str(query or ""), active_nodes)
    return {
        "ok": True,
        "bot_id": slug,
        "namespace": ns,
        "ttl_hops": 1,
        "instructions": instructions,
        "active_brief": active_brief,
        **result,
        "context_nodes": len(active_nodes),
    }


@mcp.tool()
async def bot_report_activity(
    bot_id: str,
    scope_path: Annotated[str, Field(description="Lease scope (workspace path or identifier).")],
    summary: str,
    fencing_token: Annotated[
        int, Field(description="Monotonic fencing token; higher wins on a live lease.")
    ] = 0,
) -> dict[str, Any]:
    """Acquire or refresh a fenced activity lease on `scope_path` for `bot_id` (returns `stale_epoch` if out-fenced)."""
    host_id = get_mcp_client_origin("botex")
    try:
        res = await asyncio.to_thread(
            _upsert_host_lease,
            bot_slug=str(bot_id or ""),
            host_id=host_id,
            scope_path=str(scope_path or ""),
            summary=mask_secrets(str(summary or "")),
            fencing_token=fencing_token,
        )
    except Exception as exc:
        return {"ok": False, "error": coerce_text(mask_secrets(str(exc)))}
    if not res.get("ok"):
        return res
    # Best-effort fast-cache refresh — never fatal to the lease write.
    try:
        from botex.edge.runner import write_local_bot_brief

        clean_slug = sanitize_identifier(str(bot_id or ""))
        bot = await asyncio.to_thread(_get_bot, clean_slug) if clean_slug else None
        brief = await asyncio.to_thread(_cloud_bot_brief, clean_slug) if clean_slug else None
        if bot and brief:
            await asyncio.to_thread(
                write_local_bot_brief,
                str(bot.get("slug") or clean_slug),
                namespace=str(bot.get("namespace") or ""),
                role_desc=str(bot.get("role_desc") or ""),
                etag=str(brief.get("etag") or ""),
                fencing_epoch=int(brief.get("fencing_epoch") or 0),
                content=str(brief.get("content") or ""),
                updated_at=str(brief.get("updated_at") or ""),
            )
    except Exception:
        pass
    try:
        clean_slug = sanitize_identifier(str(bot_id or ""))
        await asyncio.to_thread(_record_bot_domain_turn, clean_slug or str(bot_id or ""))
    except Exception:
        pass
    return res


@mcp.tool()
async def get_stats(
    period: Annotated[str, Field(description="week|month.")] = "week",
) -> str:
    """Return USD cost, token usage, and code-line balance summary for executed BoteX tasks."""
    days = 30 if period == "month" else 7
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    records = [r for r in load_analytics() if r.get("timestamp", "") >= cutoff]
    if not records:
        return f"No BoteX analytics data recorded in the last {days} days."

    total_runs = len(records)
    total_cost = sum(float(r.get("cost_usd") or 0.0) for r in records)
    total_prompt = sum(int(r.get("prompt_tokens") or 0) for r in records)
    total_comp = sum(int(r.get("completion_tokens") or 0) for r in records)
    total_cached = sum(int(r.get("cached_tokens") or 0) for r in records)
    total_added = sum(int(r.get("lines_added") or 0) for r in records)
    total_removed = sum(int(r.get("lines_removed") or 0) for r in records)

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
        by_model[m]["cost"] += float(r.get("cost_usd") or 0.0)

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
async def check_health(live: bool = False) -> str:
    """Checks BoteX readiness (keys, snapshots, disk). live=True probes each
    provider's /models in parallel — key presence alone proves nothing."""
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
    if live:

        async def _probe_one(pname: str, pcfg: dict) -> str:
            try:
                eff_cfg = await asyncio.to_thread(resolve_local_llm_base_url, pname, pcfg)
                adapter = ProviderRegistry.get(pname, eff_cfg)
                catalog = await asyncio.to_thread(adapter.fetch_catalog, timeout=6.0)
                return f"[OK] {pname}: reachable ({len(catalog)} models)"
            except Exception as e:
                return f"[FAIL] {pname}: {sanitize_provider_error(e)}"

        live_lines = await asyncio.gather(
            *(_probe_one(pname, pcfg) for pname, pcfg in cfg.get("providers", {}).items())
        )
        keys_report += "\nLive endpoint probe:\n" + "\n".join(live_lines)
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


def _resolve_blackbox_workspace(workspace_dir: str = "") -> str:
    """Preserve logical edge:// URIs verbatim; resolve local workspaces normally."""
    raw = str(workspace_dir or "").strip()
    if raw.startswith("edge://"):
        return raw
    return resolve_workspace(workspace_dir)


@mcp.tool()
async def botex_report_violation(
    offender: Annotated[str, Field(description="Target agent or model ID.")],
    violation_code: Annotated[str, Field(description="SEC_01|SKILL_01|SCOPE_01|HAL_01|LOOP_01.")],
    severity: Annotated[str, Field(description="WARN|REJECT|CRITICAL.")] = "REJECT",
    skill_recipe: str = "",
    evidence_snippet: Annotated[
        str, Field(description="Concise log/diff excerpt (secret-masked).")
    ] = "",
    workspace_dir: Annotated[str, Field(description="Workspace path or 'edge://<dev>/<ws>'.")] = "",
    enforcement_action: Annotated[
        str, Field(description="PENALTY_LOGGED|HANDOFF_REJECTED|ESCALATED.")
    ] = "PENALTY_LOGGED",
    origin: str = "",
) -> dict[str, Any]:
    """Record a verified peer rule violation into the isolated BlackBox penal ledger (never log violations to `save_memory`)."""
    reporter = str(_resolve_call_origin(origin) or "").strip() or "peer-client"
    if reporter.lower().startswith("botex"):
        # Prevent peer MCP callers from spoofing internal engine auto-logs.
        reporter = "peer-client"
    try:
        ws_key = _resolve_blackbox_workspace(workspace_dir)
        return await asyncio.to_thread(
            _record_incident,
            actor_agent=offender,
            reported_by=reporter,
            violation_code=violation_code,
            severity=severity or "REJECT",
            workspace_id=ws_key,
            skill_recipe=skill_recipe,
            evidence_snippet=evidence_snippet,
            enforcement_action=enforcement_action or "PENALTY_LOGGED",
            source="peer",
        )
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "message": str(exc)}


@mcp.tool()
async def botex_get_rap_sheet(
    actor_agent: Annotated[
        str, Field(description="Offender filter (empty = all in workspace).")
    ] = "",
    workspace_dir: Annotated[str, Field(description="Workspace path or 'edge://<dev>/<ws>'.")] = "",
    limit: Annotated[int, Field(description="Max incidents (1..200).")] = 20,
    window_days: Annotated[int, Field(description="Lookback days (1..365).")] = 30,
) -> dict[str, Any]:
    """Query the isolated BlackBox penal ledger for an agent/workspace incident history before review or delegation."""
    try:
        ws_key = _resolve_blackbox_workspace(workspace_dir)
        sheet = await asyncio.to_thread(
            _get_rap_sheet,
            actor_agent=actor_agent,
            workspace_id=ws_key,
            limit=limit,
            window_days=window_days,
        )
        incidents = sheet.get("incidents")
        if isinstance(incidents, list):
            wrapped_incidents = []
            for inc in incidents:
                entry = dict(inc)
                snippet = entry.get("evidence_snippet")
                if snippet:
                    entry["evidence_snippet"] = wrap_untrusted_content(
                        str(snippet),
                        source=f"blackbox:{entry.get('incident_id', 'inc')}",
                    )
                wrapped_incidents.append(entry)
            sheet["incidents"] = wrapped_incidents
        return sheet
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "message": str(exc)}


@mcp.resource("botex://skills/botex", mime_type="text/markdown")
def resource_botex_caller_skill() -> str:
    """Return the canonical caller-facing BoteX SKILL.md instructions."""
    from botex.skills.skills import get_bundled_caller_skill_files

    return get_bundled_caller_skill_files("botex")["SKILL.md"]


@mcp.resource("botex://skills/botex/references/{ref_name}", mime_type="text/markdown")
def resource_botex_caller_skill_reference(ref_name: str) -> str:
    """Return a reference document from the bundled BoteX caller skill."""
    from botex.skills.skills import get_bundled_caller_skill_files

    clean = str(ref_name or "").strip().removesuffix(".md")
    key = f"references/{clean}.md"
    files = get_bundled_caller_skill_files("botex")
    if key not in files:
        raise ValueError(f"Unknown botex skill reference: {ref_name!r}")
    return files[key]


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
    except (EOFError, KeyboardInterrupt) as exc:
        raise TaskAbortedError(f"Task interrupted by operator during '{operation}'") from exc


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
  {c("botex serve|server|--serve")}          {d(t("usage.serve"))}
  {c("botex config [show]")}                 {d(t("usage.config"))}
  {c("botex config provider <name>")}        {d(t("usage.config.provider"))}
  {c("botex config model [prov] <slug>")}    {d(t("usage.config.model"))}
  {c("botex config profile <name>")}         {d(t("usage.config.profile"))}
  {c("botex config mode <preset>")}          {d(t("usage.config.mode"))}
  {c("botex config workspace <dir>")}        {d(t("usage.config.workspace"))}
  {c("botex config language <en|pl>")}       {d(t("usage.config.language"))}
  {c("botex config price <in|out> <usd>")}   {d(t("usage.config.price"))}
  {c("botex config zdr <on|off> [prov]")}    {d(t("usage.config.zdr"))}
  {c("botex models [--refresh]")}            {d(t("usage.models"))}
  {c("botex wallet|credits [--refresh]")}    {d(t("usage.wallet"))}
  {c("botex chat [prompt] [-w DIR] [flags]")} {d(t("usage.chat"))}
  {c("    --provider/--model/--profile/--session-id/--allow-non-zdr")}  {d(t("usage.chat_flags"))}
  {c("botex memory <save|search|read|list>")} {d(t("usage.memory"))}
  {c("botex cloud [--host H] [--port P]")}   {d(t("usage.cloud"))}
  {c("botex edge [--server WSS] [-w DIR]")}    {d("Outbound WSS edge runner for BoteX Cloud")}
  {c("botex bots sync [--url URL]")}        {d("Sync tethered-bot briefs into the local fast-cache")}
  {c("botex night-pulse [--once]")}         {d("Sync host leases, fence stale epochs, and run verify gate")}
  {c("botex setup")}                         {d("Print Cloud MCP + Edge .env setup guide")}
  {c("botex skills <list|export>")}         {d("List or export the bundled BoteX caller skill")}

{title(t("usage.examples"))}
  {c('botex run "fix failing tests" -w .')}   {d(t("usage.ex.run"))}
  {c("botex chat --provider ollama")}         {d(t("usage.ex.chat"))}
  {c('botex memory search "decisions"')}      {d(t("usage.ex.memory"))}
  {c("botex --stats --month")}                {d(t("usage.ex.stats"))}

{title(t("usage.repl_cmds"))}
  {c("chat [prompt], memory <cmd>, wallet|credits, health, stats [month], history <id>,")}
  {c("snapshots, clean-snapshots, models, config [...], mode <preset>, workspace [DIR], help, exit")}

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
  {c('run "task"')}                {d(t("repl.run_task"))}
  {c("chat [prompt] [flags]")}    {d(t("repl.chat"))}
  {c("memory <save|search|read|list>")} {d(t("repl.memory"))}
  {c("health")}                  {d(t("repl.health"))}
  {c("stats [--month]")}         {d(t("repl.stats"))}
  {c("history <task_id>")}       {d(t("repl.history"))}
  {c("snapshots")}               {d(t("repl.snapshots"))}
  {c("clean-snapshots")}         {d(t("repl.clean"))}
  {c("config")}                  {d(t("repl.config"))}
  {c("config provider|model|profile|mode|workspace|language|price|zdr <value>")}
                      {d(t("repl.config.set"))}
  {c("models [--refresh]")}      {d(t("usage.models"))}
  {c("wallet|credits [--refresh]")} {d(t("usage.wallet"))}
  {c("mode <preset>")}           {d(t("repl.mode"))}
  {c("workspace [DIR]")} / {c("cd [DIR]")}  {d(t("repl.workspace"))}
  {c("help")}                    {d(t("repl.help"))}
  {c("exit")}                    {d(t("repl.exit"))}

{title(t("usage.examples"))}
  {c('run "fix failing tests"')}  {d(t("usage.ex.run"))}
  {c('chat "co słychać?"')}       {d(t("usage.ex.chat"))}
  {c('memory search "decisions"')} {d(t("usage.ex.memory"))}
  {c("botex repl --allow-non-zdr")} {d(t("repl.allow_non_zdr"))}

{d(t("repl.shell_note"))}
{d(t("repl.multiline_hint"))}""")


def _start_mcp_server():
    # Status goes to stderr — stdout must stay clean for JSON-RPC.
    sys.stderr.write(t("misc.server_start"))
    try:
        mcp.run()
    finally:
        try:
            close_index_dbs()
        except Exception:
            pass
        try:
            from botex.providers.http_pool import close_http_pool

            asyncio.run(close_http_pool())
        except Exception:
            pass


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


def _dispatch_cli(fn, argv: list) -> None:
    """Run a CLI sub-dispatcher inside the REPL.

    argparse prints its own usage/error and raises SystemExit — that must
    not kill the interactive loop, so it is swallowed here.
    """
    try:
        fn(argv)
    except SystemExit:
        pass


def _try_local_command(line: str, workspace: str = "") -> bool:
    """Handle built-in REPL commands locally (no API call). Returns True if consumed."""
    import shlex

    parts = line.split()
    if not parts:
        return True
    cmd = parts[0].lower().lstrip("-")
    # Rebuild argv for forwarded subcommands so quoted/multi-word arguments
    # survive ('chat "co słychać"', 'memory search "foo bar"'). posix shlex
    # keeps backslashes inside quotes, so "-w \"C:\\dir\"" still works.
    rest_str = line.split(None, 1)[1] if len(parts) > 1 else ""
    try:
        argv = shlex.split(rest_str) if rest_str else []
    except ValueError:
        print(warn(t("lc.unbalanced_quotes")))
        argv = rest_str.split()
    # Forward the REPL workspace to subcommands that accept -w so a
    # mid-session `workspace DIR` switch is honored (explicit -w wins).
    if workspace and cmd in ("chat", "memory") and argv:
        has_ws = any(a in ("-w", "--workspace") or a.startswith("--workspace=") for a in argv)
        if not has_ws:
            if cmd == "chat":
                argv = ["-w", workspace] + argv
            else:
                argv = [argv[0], "-w", workspace] + argv[1:]
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
        _dispatch_cli(_cli_config, argv)
    elif cmd == "models":
        _dispatch_cli(_cli_models, argv)
    elif cmd in ("wallet", "credits"):
        _dispatch_cli(_cli_wallet, argv)
    elif cmd == "chat":
        _dispatch_cli(_cli_chat, argv)
    elif cmd == "memory":
        _dispatch_cli(_cli_memory, argv)
    elif cmd in ("serve", "server"):
        print(warn(f"[BoteX] {t('lc.serve_warn')}"))
    elif cmd == "cloud":
        print(warn(f"[BoteX] {t('lc.cloud_warn')}"))
    elif cmd == "run" and not argv:
        # Bare 'run' would otherwise be billed to the model as a task.
        print(dim(t("repl.run_usage")))
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


_prompt_toolkit_session = None
_prompt_toolkit_broken = False


def _get_prompt_toolkit_session():
    """Lazily build a prompt_toolkit PromptSession for multiline-capable input.

    Returns None when prompt_toolkit is unavailable or stdin is not a TTY —
    callers then fall back to plain input(). With multiline=True, bracketed
    paste inserts newlines literally and Esc+Enter inserts a newline while
    typing, so multi-line prompts no longer submit per line.
    """
    global _prompt_toolkit_session, _prompt_toolkit_broken
    if _prompt_toolkit_session is not None or _prompt_toolkit_broken:
        return _prompt_toolkit_session
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _insert_newline(event):
            event.current_buffer.insert_text("\n")

        _prompt_toolkit_session = PromptSession(multiline=True, key_bindings=kb)
    except Exception:
        _prompt_toolkit_broken = True
    return _prompt_toolkit_session


def _prompt_line(prompt_session, fallback_prompt: str):
    """Read one user input line, multiline-aware when prompt_toolkit is active."""
    global _prompt_toolkit_session, _prompt_toolkit_broken
    if prompt_session is None:
        return input(fallback_prompt)
    from prompt_toolkit.formatted_text import ANSI

    try:
        return prompt_session.prompt(ANSI(fallback_prompt))
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        _prompt_toolkit_broken = True
        _prompt_toolkit_session = None
        return input(fallback_prompt)


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
        help="API provider (openrouter/openrouter-free/nvidia/google/ollama/groq/cloudflare; empty = config default)",
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
                t_name = sanitize_identifier(str(event.get("name", ""))) or "tool"
                tool_line = dim(f"\n[Tool: {t_name}]")
                sys.stdout.write(tool_line + "\n")
                sys.stdout.flush()
            elif ev_type == "error":
                sys.stdout.write(err("\nError: chat turn failed.") + "\n")
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

    prompt_session = _get_prompt_toolkit_session()
    while True:
        try:
            line = _prompt_line(prompt_session, accent("chat> ")).strip()
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
                    "Multiline input: Esc+Enter inserts a newline; pasted text keeps its line breaks.\n"
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
            tags=filter_reserved_tags(tags_list),
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
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--log-level", default="info", help="Log level (default: info)")
    args = parser.parse_args(argv)

    if (
        args.host not in ("127.0.0.1", "::1", "localhost")
        and not os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
    ):
        print(
            f"CRITICAL: refusing to bind to {args.host} without BOTEX_ACCESS_TOKEN set. "
            "Set BOTEX_ACCESS_TOKEN or bind to 127.0.0.1 for local mode.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(title(f"BoteX FastAPI Cloud Gateway & Remote MCP SSE on http://{args.host}:{args.port}"))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, workers=1)


def _load_edge_env_files() -> None:
    """Load BOTEX_EDGE_* defaults from .env files without overriding explicit env vars."""
    if os.environ.get("BOTEX_DISABLE_DOTENV", "").strip() in ("1", "true", "yes"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    candidates: list[Path] = []
    explicit_env = os.environ.get("BOTEX_ENV_FILE", "").strip()
    if explicit_env:
        candidates.append(Path(explicit_env).expanduser())
    candidates.extend(
        [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent / ".env",
        ]
    )
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)


def _cli_skills(argv: list[str]) -> None:
    """CLI dispatcher for listing and exporting bundled caller-facing skills."""
    import argparse

    from botex.skills.skills import (
        CALLER_SKILL_TARGET_PRESETS,
        export_bundled_caller_skill,
        list_bundled_caller_skills,
    )

    parser = argparse.ArgumentParser(
        prog="botex skills",
        description="Inspect or export the bundled caller-facing BoteX skill (SKILL.md + references).",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list", help="List bundled caller skills and target presets")

    exp = sub.add_parser("export", help="Export a bundled caller skill to a local skills directory")
    exp.add_argument(
        "name",
        nargs="?",
        default="botex",
        help="Bundled caller skill name to export (default: botex)",
    )
    exp.add_argument(
        "--target",
        "-t",
        default="ai",
        help=(
            "Destination preset ("
            + ", ".join(sorted(CALLER_SKILL_TARGET_PRESETS))
            + ") or custom directory path (default: ai)"
        ),
    )
    exp.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing skill files in the target directory",
    )

    args = parser.parse_args(argv or ["list"])
    action = args.action or "list"
    if action == "list":
        res = list_bundled_caller_skills()
        print(title("Bundled BoteX Caller Skills"))
        for item in res.get("skills", []):
            ver = item.get("version") or "-"
            print(f"  {accent(item['name'])} (v{ver}) — {item.get('description', '')}")
            print(f"    {dim('files:')} {', '.join(item.get('files', []))}")
        print(f"\n{accent('Target presets:')}")
        for key, path in sorted(CALLER_SKILL_TARGET_PRESETS.items()):
            print(f"  {accent(key):<10} -> {path}")
        return

    res = export_bundled_caller_skill(name=args.name, target=args.target, force=args.force)
    if not res.get("ok"):
        print(err(f"[ERROR] {res.get('error', 'Failed to export caller skill')}"))
        sys.exit(1)
    files_written = res.get("files_written") or []
    print(ok(f"[OK] Exported '{res['name']}' to {res['target_dir']} ({len(files_written)} files):"))
    for fp in files_written:
        print(f"  {dim('-')} {fp}")


def _cli_setup() -> None:
    """Print a host-agnostic setup guide for connecting a workstation to BoteX Cloud."""
    print(f"""{title("BoteX Cloud & Edge Setup Guide")}

1. {accent("Connect your IDE / AI agent to BoteX Cloud MCP (no local server.py needed):")}
   - MCP SSE URL : https://<cloud-host>/mcp/sse
   - HTTP Headers:
       Authorization: Bearer <BOTEX_ACCESS_TOKEN>
       X-Agent-Id:    <agent-name>   (optional, e.g. antigravity, cursor, devin, zcode)
       X-Host:        <device-name>  (optional, for Memory Vault / BlackBox provenance)

2. {accent("Issue an Edge device token (once per device/workspace):")}
   POST https://<cloud-host>/api/edge/tokens
   Header: Authorization: Bearer <BOTEX_ACCESS_TOKEN>
   Body:   {{"device_id": "<device-id>", "workspace_id": "<workspace-id>"}}
   -> Returns one-time token starting with: btxd_...

3. {accent("Store the edge token in the isolated local credential store (recommended):")}
   botex edge enroll --device-id <device-id> --workspace-id <workspace-id>
   (paste the one-time btxd_... token when prompted, or pipe it / use --token-file)
   Credentials live in ~/.botex/credentials/edge/ (override dir with BOTEX_CREDENTIALS_DIR).
   Pass the server separately via --server wss://<cloud-host>/api/edge/ws or BOTEX_EDGE_SERVER.
   CI/services: skip the store and set BOTEX_EDGE_TOKEN (or --token-file) instead.

4. {accent("Start the Edge bridge in your project directory:")}
   botex edge -w .
   (add --allow-exec / --allow-destructive only if Cloud tasks should run commands/deletes)

5. {accent("Target this machine from Cloud MCP tools (start_task / run_subagent):")}
   workspace_dir="edge://<device-id>/<workspace-id>"

6. {accent("Export the BoteX caller skill (SKILL.md + references) into your local agent's skills folder:")}
   botex skills export --target ai
   (presets: ai, claude, gemini, devin, or custom path)
""")


def _cli_mcp_bridge(argv: list[str]) -> None:
    """CLI dispatcher for the MCP client bridge (stdio JSON-RPC <-> Cloud SSE/HTTP)."""
    from botex.mcp_bridge import __main__ as mcp_bridge_main

    try:
        mcp_bridge_main.main(argv)
    except KeyboardInterrupt:
        print(err("MCP Bridge interrupted by user."), file=sys.stderr)
        sys.exit(1)


def _edge_https_base_url(raw_url: str) -> str:
    """Derive an http(s) base URL from a ws(s)/http(s) server URL (path + userinfo dropped)."""
    from urllib.parse import urlsplit

    text = str(raw_url or "").strip()
    if not text:
        return ""
    scheme_map = {"wss": "https", "ws": "http"}
    try:
        parts = urlsplit(text)
        scheme = scheme_map.get(parts.scheme.lower(), parts.scheme.lower())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if scheme not in ("https", "http") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _run_edge_host_daemon(
    server_url: str,
    loopback_port: int,
    *,
    allow_exec: bool = False,
    allow_destructive: bool = False,
) -> None:
    """Run the multi-workspace EdgeHostDaemon until interrupted.

    Serves every ``state='enrolled'`` workspace from the host registry
    (``host_workspaces.json``); the single-workspace flow stays available when
    the registry holds no enrolled entries.
    """
    from botex.edge.runner import EdgeHostDaemon

    daemon = EdgeHostDaemon(
        server_url,
        loopback_port=int(loopback_port),
        allow_exec=bool(allow_exec),
        allow_destructive=bool(allow_destructive),
    )

    async def _run_daemon_main() -> None:
        start_res = await daemon.start()
        if isinstance(start_res, dict) and start_res.get("mode") == "attached":
            print(
                ok(
                    f"Attached to existing BoteX edge daemon "
                    f"(pid={start_res.get('pid')}, port={start_res.get('port')})"
                )
            )
            return
        print(title("BoteX edge host daemon: serving enrolled workspaces from the host registry"))
        await daemon.run_forever()

    try:
        asyncio.run(_run_daemon_main())
    except KeyboardInterrupt:
        try:
            asyncio.run(daemon.stop())
        except Exception:
            pass


def _cli_edge(argv: list[str]) -> None:
    """CLI dispatcher for the thin edge runner (outbound WSS bridge)."""
    import argparse
    import getpass
    import socket

    from botex.edge import EdgeCapabilities, EdgeRunner

    # Capture explicit process-env token before .env loading so stale .env entries
    # cannot shadow per-(device_id, workspace_id) credentials in the isolated store.
    process_env_token = os.environ.get("BOTEX_EDGE_TOKEN", "")
    _load_edge_env_files()

    if argv and argv[0] in ("enroll", "tokens", "workspaces"):
        from botex.edge.credentials import (
            delete_edge_token,
            extract_token_jti,
            list_edge_credentials,
            load_edge_token,
            save_edge_token,
        )
        from botex.edge.workspaces import (
            add_workspace,
            discover_candidate_workspaces,
            list_workspaces,
            remove_workspace,
        )

        sub = argparse.ArgumentParser(prog="botex edge")
        cmd = sub.add_subparsers(dest="command", required=True)
        p_enroll = cmd.add_parser(
            "enroll", help="Store an edge device token in the local credential store"
        )
        p_enroll.add_argument("--device-id", default=os.environ.get("BOTEX_EDGE_DEVICE_ID", ""))
        p_enroll.add_argument(
            "--workspace-id", default=os.environ.get("BOTEX_EDGE_WORKSPACE_ID", "")
        )
        p_enroll.add_argument("--workspace", "-w", default=".")
        p_enroll.add_argument("--token-file", default="")
        p_tokens = cmd.add_parser("tokens", help="Manage stored edge device tokens")
        p_tokens_sub = p_tokens.add_subparsers(dest="tokens_command", required=True)
        p_tokens_sub.add_parser("list", help="List stored edge credentials (no token material)")
        p_remove = p_tokens_sub.add_parser("remove", help="Remove a stored edge credential")
        p_remove.add_argument("--device-id", required=True)
        p_remove.add_argument("--workspace-id", required=True)
        p_upgrade = p_tokens_sub.add_parser(
            "upgrade",
            help="Rotate an edge device token via the Cloud upgrade endpoint (adds delegate_local scope)",
        )
        p_upgrade.add_argument("--device-id", default=os.environ.get("BOTEX_EDGE_DEVICE_ID", ""))
        p_upgrade.add_argument(
            "--workspace-id", default=os.environ.get("BOTEX_EDGE_WORKSPACE_ID", "")
        )
        p_upgrade.add_argument(
            "--cloud-url",
            default="",
            help=(
                "Cloud HTTPS base URL (a wss:// edge URL is converted); "
                "default: derived from BOTEX_EDGE_SERVER"
            ),
        )
        p_upgrade.add_argument("--label", default="", help="Optional token label stored on Cloud")

        p_ws = cmd.add_parser(
            "workspaces",
            help="Manage the multi-workspace host registry used by 'botex edge --edge-daemon'",
        )
        p_ws_sub = p_ws.add_subparsers(dest="workspaces_command", required=True)
        p_ws_add = p_ws_sub.add_parser(
            "add", help="Register a workspace directory (enrolled unless --candidate)"
        )
        p_ws_add.add_argument("path", help="Workspace directory to register")
        p_ws_add.add_argument("--label", default="", help="Human-readable workspace label")
        p_ws_add.add_argument(
            "--workspace-id",
            default="",
            help="Explicit opaque workspace id (ws_...); generated when omitted",
        )
        p_ws_add.add_argument(
            "--candidate",
            action="store_true",
            help="Register as candidate instead of enrolled (stays inert until enrolled)",
        )
        p_ws_add.add_argument(
            "--local-admin",
            action="store_true",
            help="Allow locally-administered skills for this workspace",
        )
        p_ws_list = p_ws_sub.add_parser("list", help="List registered workspaces")
        p_ws_list.add_argument(
            "--include-disabled", action="store_true", help="Include disabled entries"
        )
        p_ws_remove = p_ws_sub.add_parser("remove", help="Remove a registered workspace")
        p_ws_remove.add_argument("--workspace-id", required=True)
        p_ws_discover = p_ws_sub.add_parser(
            "discover", help="Scan roots for project markers and register candidate workspaces"
        )
        p_ws_discover.add_argument(
            "roots", nargs="*", help="Directories to scan (default: current directory)"
        )
        ns = sub.parse_args(argv)

        if ns.command == "enroll":
            device_id = (ns.device_id or "").strip() or socket.gethostname()
            workspace_root = str(Path(ns.workspace).expanduser().resolve())
            workspace_id = (ns.workspace_id or "").strip() or Path(workspace_root).name
            raw_token = ""
            if ns.token_file:
                try:
                    raw_token = Path(ns.token_file).expanduser().read_text(encoding="utf-8").strip()
                except OSError as exc:
                    print(err(f"Cannot read --token-file: {exc.strerror or exc}"), file=sys.stderr)
                    sys.exit(2)
            elif not sys.stdin.isatty():
                raw_token = sys.stdin.read().strip()
            else:
                raw_token = getpass.getpass("Edge device token (btxd_...): ").strip()
            if not raw_token:
                print(
                    err("Empty token: pipe a btxd_ token, pass --token-file, or type one."),
                    file=sys.stderr,
                )
                sys.exit(2)
            try:
                res = save_edge_token(device_id, workspace_id, raw_token)
            except (OSError, ValueError) as exc:
                print(err(f"Failed to store edge token: {exc}"), file=sys.stderr)
                sys.exit(2)
            print(
                ok(
                    f"Stored edge credential for device={res['device_id']} "
                    f"workspace={res['workspace_id']} (jti={res.get('jti', '?')})"
                )
            )
            return

        if ns.command == "tokens" and ns.tokens_command == "upgrade":
            import urllib.error
            import urllib.request

            base_url = _edge_https_base_url(
                str(ns.cloud_url or "").strip() or os.environ.get("BOTEX_EDGE_SERVER", "")
            )
            if not base_url:
                print(
                    err(
                        "Cannot derive the Cloud base URL: pass --cloud-url https://<host> "
                        "or set BOTEX_EDGE_SERVER=wss://<host>/api/edge/ws."
                    ),
                    file=sys.stderr,
                )
                sys.exit(2)
            device_id = (ns.device_id or "").strip() or socket.gethostname()
            workspace_id = (ns.workspace_id or "").strip()
            if not workspace_id:
                print(
                    err("--workspace-id is required for 'botex edge tokens upgrade'."),
                    file=sys.stderr,
                )
                sys.exit(2)
            bearer = os.environ.get("BOTEX_ACCESS_TOKEN", "").strip()
            if not bearer:
                print(
                    err(
                        "Cloud auth token missing: export BOTEX_ACCESS_TOKEN "
                        "(this command reads the environment only, never .env)."
                    ),
                    file=sys.stderr,
                )
                sys.exit(2)
            cur_token = load_edge_token(device_id, workspace_id)
            replace_jti = extract_token_jti(cur_token) or None
            payload = json.dumps(
                {
                    "device_id": device_id,
                    "workspace_id": workspace_id,
                    "replace_jti": replace_jti,
                    "label": str(ns.label or "").strip(),
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/edge/tokens/upgrade",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {bearer}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                print(
                    err(
                        f"Token upgrade rejected by Cloud (HTTP {exc.code}); local token unchanged."
                    ),
                    file=sys.stderr,
                )
                sys.exit(1)
            except urllib.error.URLError as exc:
                print(err(f"Cannot reach the Cloud gateway: {exc.reason}"), file=sys.stderr)
                sys.exit(1)
            new_token = str(body.get("token", "") or "").strip() if isinstance(body, dict) else ""
            if not new_token.startswith("btxd_"):
                print(
                    err("Cloud response contained no valid edge token; local token unchanged."),
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                res = save_edge_token(device_id, workspace_id, new_token)
            except (OSError, ValueError) as exc:
                print(
                    err(f"Token upgraded on Cloud but storing it locally failed: {exc}"),
                    file=sys.stderr,
                )
                sys.exit(2)
            print(
                ok(
                    f"Edge token upgraded for device={device_id} workspace={workspace_id}: "
                    f"jti {replace_jti or '-'} -> {res.get('jti', '?')}"
                )
            )
            return

        if ns.command == "workspaces":
            if ns.workspaces_command == "add":
                root = str(Path(ns.path).expanduser().resolve())
                try:
                    entry = add_workspace(
                        root,
                        label=str(ns.label or "").strip(),
                        workspace_id=str(ns.workspace_id or "").strip(),
                        state="candidate" if ns.candidate else "enrolled",
                        daemon_mode=True,
                        local_admin=bool(ns.local_admin),
                    )
                except (OSError, ValueError) as exc:
                    print(err(f"Cannot add workspace: {exc}"), file=sys.stderr)
                    sys.exit(2)
                print(
                    ok(
                        f"Workspace added: id={entry['workspace_id']} "
                        f"label={entry['label'] or '-'} state={entry['state']}"
                    )
                )
                return

            if ns.workspaces_command == "list":
                items = list_workspaces(include_disabled=bool(ns.include_disabled))
                if not items:
                    print(
                        dim(
                            "No workspaces registered. Use 'botex edge workspaces add <path>' "
                            "or 'botex edge workspaces discover'."
                        )
                    )
                    return
                header = f"{'WORKSPACE_ID':<18} {'LABEL':<24} {'STATE':<10} SOURCE"
                print(title(header))
                for ws in items:
                    print(
                        f"{ws.get('workspace_id', '?'):<18} {(ws.get('label') or '-'):<24} "
                        f"{ws.get('state', '?'):<10} {ws.get('source', '?')}"
                    )
                return

            if ns.workspaces_command == "remove":
                if remove_workspace(str(ns.workspace_id).strip()):
                    print(ok(f"Removed workspace {ns.workspace_id}"))
                else:
                    print(dim(f"No workspace registered with id {ns.workspace_id}"))
                return

            roots = [str(Path(r).expanduser().resolve()) for r in (ns.roots or [])]
            found = discover_candidate_workspaces(roots or [str(Path.cwd())])
            if not found:
                print(dim("No new candidate workspaces discovered."))
                return
            for ws in found:
                print(
                    ok(
                        f"Discovered candidate workspace: id={ws['workspace_id']} "
                        f"label={ws['label']} state={ws['state']}"
                    )
                )
            return

        if ns.command == "tokens" and ns.tokens_command == "list":
            items = list_edge_credentials()
            if not items:
                print(dim("No stored edge credentials. Use 'botex edge enroll' to add one."))
                return
            header = f"{'DEVICE':<28} {'WORKSPACE':<28} {'JTI':<20} UPDATED"
            print(title(header))
            for it in items:
                print(
                    f"{it.get('device_id', '?'):<28} {it.get('workspace_id', '?'):<28} "
                    f"{it.get('jti', '?'):<20} {it.get('updated_at', '?')}"
                )
            return

        try:
            removed = delete_edge_token(ns.device_id, ns.workspace_id)
        except ValueError as exc:
            print(err(f"Invalid credential identifier: {exc}"), file=sys.stderr)
            sys.exit(2)
        if removed:
            print(
                ok(f"Removed edge credential for device={ns.device_id} workspace={ns.workspace_id}")
            )
        else:
            print(
                dim(
                    f"No stored edge credential for device={ns.device_id} workspace={ns.workspace_id}"
                )
            )
        return

    parser = argparse.ArgumentParser(
        prog="botex edge",
        description=(
            "Run the thin edge runner: an outbound WSS bridge that lets BoteX "
            "Cloud execute workspace tools on this device (no inbound port)."
        ),
        epilog=(
            "Use 'botex edge enroll' to store device tokens in the isolated credential store; "
            "run 'botex setup' for full Cloud MCP + Edge instructions."
        ),
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("BOTEX_EDGE_SERVER", ""),
        help="Cloud gateway WSS URL, e.g. wss://<cloud-host>/api/edge/ws (default: BOTEX_EDGE_SERVER)",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=".",
        help="Workspace dir exposed to the cloud (default: .)",
    )
    parser.add_argument(
        "--workspace-id",
        default=os.environ.get("BOTEX_EDGE_WORKSPACE_ID", ""),
        help="Logical workspace ID registered on the gateway (default: BOTEX_EDGE_WORKSPACE_ID or folder name)",
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("BOTEX_EDGE_DEVICE_ID", ""),
        help="Device identifier (default: BOTEX_EDGE_DEVICE_ID or hostname)",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Device bearer token (default: BOTEX_EDGE_TOKEN from env/.env; never logged)",
    )
    parser.add_argument(
        "--token-file",
        default="",
        help="Read the device token from a 0600 file instead of argv/env (avoids process-list leaks)",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Raise the local ceiling to permit run_command (exec policy from edge_caps.json/config)",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Raise the local ceiling to permit delete_file/move_file",
    )
    parser.add_argument(
        "--edge-daemon",
        action="store_true",
        help="Attach to an existing local edge daemon or spawn one with 127.0.0.1 loopback IPC",
    )
    parser.add_argument(
        "--loopback-port",
        type=int,
        default=None,
        help="Optional 127.0.0.1 loopback IPC port (0 = dynamic ephemeral port; disabled unless set or --edge-daemon)",
    )
    args = parser.parse_args(argv)

    server_url = str(args.server or "").strip()
    if not server_url:
        print(
            err(
                "Edge server URL required: pass --server wss://<cloud-host>/api/edge/ws "
                "or set BOTEX_EDGE_SERVER (run 'botex setup' for details)."
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    if getattr(args, "edge_daemon", False):
        from botex.edge.workspaces import list_workspaces

        enrolled = [ws for ws in list_workspaces() if ws.get("state") == "enrolled"]
        if enrolled:
            _run_edge_host_daemon(
                server_url,
                int(args.loopback_port) if args.loopback_port is not None else 0,
                allow_exec=bool(args.allow_exec),
                allow_destructive=bool(args.allow_destructive),
            )
            return
        # No enrolled entries in the host registry: fall through to the legacy
        # single-workspace --edge-daemon flow (attach_or_spawn_edge_daemon + EdgeRunner).

    workspace_root = str(Path(args.workspace).expanduser().resolve())
    device_id = args.device_id or socket.gethostname()
    workspace_id = args.workspace_id or Path(workspace_root).name

    from botex.edge.credentials import resolve_edge_token

    if args.token:
        print(
            dim(
                "Warning: --token exposes credentials in process lists; "
                "prefer 'botex edge enroll' or --token-file."
            ),
            file=sys.stderr,
        )

    try:
        token, _token_source = resolve_edge_token(
            device_id,
            workspace_id,
            cli_token=args.token,
            token_file=args.token_file,
            env_token=process_env_token,
        )
    except (OSError, ValueError) as exc:
        print(err(f"Cannot resolve edge token: {exc}"), file=sys.stderr)
        sys.exit(2)
    if not token and not str(args.token_file or "").strip():
        token = os.environ.get("BOTEX_EDGE_TOKEN", "").strip()
    if not token:
        print(
            err(
                "Edge token required: run 'botex edge enroll --device-id <id> "
                "--workspace-id <id>' to store one interactively, or pass "
                "--token-file / set BOTEX_EDGE_TOKEN for CI (run 'botex setup' for details)."
            ),
            file=sys.stderr,
        )
        sys.exit(2)
    caps = EdgeCapabilities.load(workspace_root)
    if args.allow_exec:
        caps.allow_exec = True
        caps.allowed_tools = caps.allowed_tools | {"run_command"}
    if args.allow_destructive:
        caps.allow_destructive = True
        caps.allowed_tools = caps.allowed_tools | {"delete_file", "move_file"}

    runner = EdgeRunner(
        args.server,
        token=token,
        device_id=device_id,
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        caps=caps,
    )
    daemon_lock = None
    if getattr(args, "edge_daemon", False):
        from botex.edge.runner import attach_or_spawn_edge_daemon

        attach_res = attach_or_spawn_edge_daemon(
            loopback_port=int(args.loopback_port) if args.loopback_port is not None else 0
        )
        if attach_res.get("mode") == "attached":
            print(
                ok(
                    f"Attached to existing BoteX edge daemon "
                    f"(pid={attach_res.get('pid')}, port={attach_res.get('port')})"
                )
            )
            return
        daemon_lock = attach_res.get("lock")

    print(
        title(
            f"BoteX edge → {args.server} | workspace={workspace_root} "
            f"(id={runner.workspace_id}) device={runner.device_id}"
        )
    )

    async def _run_edge_main() -> None:
        want_loopback = getattr(args, "edge_daemon", False) or (
            getattr(args, "loopback_port", None) is not None
        )
        if want_loopback and hasattr(runner, "start_loopback_ipc"):
            lb_port = int(args.loopback_port) if args.loopback_port is not None else 0
            await runner.start_loopback_ipc(port=lb_port)
        try:
            await runner.run_forever()
        finally:
            if hasattr(runner, "stop_loopback_ipc"):
                await runner.stop_loopback_ipc()

    try:
        asyncio.run(_run_edge_main())
    except KeyboardInterrupt:
        runner.stop()
    finally:
        if daemon_lock is not None:
            try:
                daemon_lock.release()
            except Exception:
                pass


class _UrllibSyncResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))


_MAX_SYNC_RESPONSE_BYTES = 1_048_576


class _UrllibSyncClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def get(self, url: str, headers: Optional[dict[str, str]] = None) -> _UrllibSyncResponse:
        import urllib.error
        import urllib.parse
        import urllib.request

        class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                new_req = super().redirect_request(req, fp, code, msg, hdrs, newurl)
                if new_req is None:
                    return None
                old_p = urllib.parse.urlparse(req.full_url)
                new_p = urllib.parse.urlparse(new_req.full_url)
                if (old_p.scheme, old_p.netloc) != (new_p.scheme, new_p.netloc):
                    new_req.remove_header("Authorization")
                return new_req

        opener = urllib.request.build_opener(_SafeRedirectHandler())
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with opener.open(req, timeout=self._timeout) as resp:
                return _UrllibSyncResponse(int(resp.status), resp.read(_MAX_SYNC_RESPONSE_BYTES))
        except urllib.error.HTTPError as exc:
            body = exc.read(_MAX_SYNC_RESPONSE_BYTES) if hasattr(exc, "read") else b""
            return _UrllibSyncResponse(int(exc.code), body)

    def close(self) -> None:
        pass


def sync_bot_briefs(
    base_url: str = "",
    *,
    on_startup: bool = False,
    briefs_dir: Optional[Path] = None,
    client: Optional[Any] = None,
) -> dict[str, Any]:
    """Sync Cloud Soul bot briefs into the edge fast-cache (ETag/304 aware).

    Base URL precedence: ``base_url`` arg > ``BOTEX_CLOUD_URL`` env > config
    ``cloud.url``. An injectable ``client`` (e.g. FastAPI ``TestClient``) makes
    the sync offline-testable. With ``on_startup=True`` network failures still
    return a graceful ``{"ok": False}`` — the caller decides the exit code.
    """
    url = str(base_url or "").strip() or os.environ.get("BOTEX_CLOUD_URL", "").strip()
    if not url:
        url = str((load_config().get("cloud") or {}).get("url") or "").strip()
    try:
        from botex.edge.runner import (
            get_local_bot_briefs_dir,
            read_local_bot_briefs,
            write_local_bot_brief,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "EDGE_HELPERS_UNAVAILABLE",
            "detail": coerce_text(mask_secrets(str(exc))),
        }
    local_etags: dict[str, str] = {}
    try:
        for lb in read_local_bot_briefs(briefs_dir=briefs_dir):
            slug = str(lb.get("slug") or "")
            etag = str(lb.get("etag") or "")
            if slug and etag:
                local_etags[slug] = etag
    except Exception:
        pass
    if not url:
        return {
            "ok": False,
            "error": "NO_CLOUD_URL",
            "on_startup": bool(on_startup),
            "updated": 0,
            "skipped_304": 0,
            "bots": 0,
        }
    from urllib.parse import urlparse

    if urlparse(url).scheme not in ("http", "https"):
        return {
            "ok": False,
            "error": "INVALID_CLOUD_URL",
            "on_startup": bool(on_startup),
            "updated": 0,
            "skipped_304": 0,
            "bots": 0,
        }
    owns_client = client is None
    http = client if client is not None else _UrllibSyncClient(timeout=10.0)
    auth_token = (
        os.environ.get("BOTEX_CLOUD_TOKEN", "").strip()
        or str((load_config().get("cloud") or {}).get("token") or "").strip()
    )
    base_headers: dict[str, str] = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    updated = skipped = removed = 0
    bots: list[dict[str, Any]] = []
    target_dir = get_local_bot_briefs_dir(briefs_dir)
    try:
        root = url.rstrip("/")
        res = http.get(f"{root}/api/bots", headers=base_headers)
        res.raise_for_status()
        data = res.json()
        all_bots = data.get("bots") or []
        for b in all_bots:
            slug = str(b.get("slug") or "")
            if not slug or slug != sanitize_identifier(slug):
                continue
            b_status = str(b.get("status") or "active").strip().lower()
            if b_status != "active":
                stale_file = target_dir / f"{slug}.md"
                if stale_file.is_file():
                    stale_file.unlink(missing_ok=True)
                    removed += 1
                continue
            bots.append(b)
        for b in bots:
            slug = str(b.get("slug") or "")
            headers = dict(base_headers)
            local_etag = local_etags.get(slug)
            if local_etag:
                headers["If-None-Match"] = f'"{local_etag}"'
            r = http.get(f"{root}/api/bots/{slug}/brief", headers=headers)
            if r.status_code == 304:
                skipped += 1
                continue
            if r.status_code != 200:
                continue
            payload = r.json()
            try:
                epoch_val = int(payload.get("fencing_epoch") or 0)
            except (TypeError, ValueError):
                epoch_val = 0
            write_local_bot_brief(
                slug,
                namespace=str(payload.get("namespace") or b.get("namespace") or ""),
                role_desc=str(payload.get("role_desc") or b.get("role_desc") or ""),
                etag=str(payload.get("etag") or ""),
                fencing_epoch=epoch_val,
                content=str(payload.get("content") or ""),
                updated_at=str(payload.get("updated_at") or ""),
                briefs_dir=briefs_dir,
            )
            updated += 1
        return {
            "ok": True,
            "base_url": url,
            "bots": len(bots),
            "updated": updated,
            "skipped_304": skipped,
            "removed": removed,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "SYNC_FAILED",
            "detail": coerce_text(mask_secrets(str(exc))),
            "on_startup": bool(on_startup),
            "updated": updated,
            "skipped_304": skipped,
            "removed": removed,
        }
    finally:
        if owns_client and http is not None:
            try:
                http.close()
            except Exception:
                pass


def _cli_bots(argv: list[str]) -> None:
    """CLI dispatcher for Tethered Bots ops (brief sync into the fast-cache)."""
    import argparse

    parser = argparse.ArgumentParser(prog="botex bots", description="Tethered Bots operations.")
    sub = parser.add_subparsers(dest="cmd")
    sync_p = sub.add_parser("sync", help="Sync bot briefs from Cloud into the local fast-cache")
    sync_p.add_argument(
        "--url",
        default="",
        help="Cloud gateway base URL (default: BOTEX_CLOUD_URL or config cloud.url)",
    )
    sync_p.add_argument(
        "--on-startup",
        action="store_true",
        help="Never fail the process — exit 0 even on network errors",
    )
    args = parser.parse_args(argv)
    if args.cmd != "sync":
        parser.print_help()
        sys.exit(2)
    res = sync_bot_briefs(base_url=str(args.url or ""), on_startup=bool(args.on_startup))
    if res.get("ok"):
        print(
            ok(
                f"bots sync: {res['updated']} updated, "
                f"{res['skipped_304']} unchanged (304), {res['bots']} bots"
            )
        )
        sys.exit(0)
    print(
        err(f"bots sync failed: {res.get('error') or 'unknown'} {res.get('detail') or ''}"),
        file=sys.stderr,
    )
    sys.exit(0 if args.on_startup else 1)


_CATALOG_DRIFT_REPORT_PATH: Optional[Path] = None  # None = default .botex/catalog_drift.json


def _catalog_drift_report_path() -> Path:
    if _CATALOG_DRIFT_REPORT_PATH is not None:
        return Path(_CATALOG_DRIFT_REPORT_PATH)
    try:
        from botex.config import PROJECT_ROOT

        return PROJECT_ROOT / ".botex" / "catalog_drift.json"
    except Exception:
        return Path.cwd() / ".botex" / "catalog_drift.json"


def _load_cached_catalogs(
    provider_names: list[str],
    providers_cfg: dict[str, Any],
    cache_file: Path,
) -> dict[str, dict[str, Any]]:
    """Load per-provider catalogs from the shared cache file only — never network."""
    from botex.providers.catalog import ModelCatalog, ModelMetadata

    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    except Exception:
        raw = {}
    entries = raw.get("catalogs") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        entries = {}

    catalogs: dict[str, dict[str, Any]] = {}
    for name in provider_names:
        models: dict[str, Any] = {}
        try:
            pcfg = providers_cfg.get(name) if isinstance(providers_cfg, dict) else None
            cat = ModelCatalog(
                provider_name=name,
                provider_cfg=pcfg if isinstance(pcfg, dict) else None,
                cache_file=cache_file,
            )
            entry = entries.get(cat.cache_key)
            raw_cat = (
                (entry.get("catalog") or entry.get("models")) if isinstance(entry, dict) else None
            )
            if isinstance(raw_cat, dict):
                models = {
                    mid: ModelMetadata.from_dict(d if isinstance(d, dict) else {}, default_id=mid)
                    for mid, d in raw_cat.items()
                    if isinstance(mid, str)
                }
        except Exception:
            models = {}
        catalogs[name] = models
    return catalogs


def _run_catalog_drift_sync(cfg: dict[str, Any]) -> dict[str, Any]:
    """Night-Pulse live catalog sync + config drift detection. Never raises.

    Refreshes the shared provider-model cache (one GET /models per provider),
    diffs configured model ids against the freshly loaded cached catalogs,
    persists the drift report to .botex/catalog_drift.json, and emits one
    stderr line per drifted model. The static config is never written.
    """
    from botex.providers import catalog as catalog_mod
    from botex.providers.providers import ProviderRegistry

    sync_cfg = cfg.get("catalog_sync") if isinstance(cfg, dict) else None
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}
    if not bool(sync_cfg.get("enabled", True)):
        return {"status": "disabled"}

    raw_providers = sync_cfg.get("providers")
    if not isinstance(raw_providers, (list, tuple)) or not raw_providers:
        raw_providers = ["google", "groq", "nvidia", "cloudflare", "openrouter", "openrouter-free"]
    provider_names = [str(p).strip() for p in raw_providers if str(p).strip()]
    try:
        timeout = float(sync_cfg.get("timeout_s", 10.0) or 10.0)
    except (ValueError, TypeError):
        timeout = 10.0
    try:
        interval_s = float(sync_cfg.get("interval_s", 43200.0) or 0.0)
    except (ValueError, TypeError):
        interval_s = 43200.0

    # Frequency gate: continuous night-pulse loops every minute — a live sync
    # is a per-provider network call and must stay a ~daily maintenance job.
    if interval_s > 0.0:
        try:
            prev_path = _catalog_drift_report_path()
            if prev_path.exists():
                prev = json.loads(prev_path.read_text(encoding="utf-8"))
                prev_ts = float(prev.get("generated_at") or 0.0) if isinstance(prev, dict) else 0.0
                age = time.time() - prev_ts
                if prev_ts > 0.0 and age < interval_s:
                    return {
                        "status": "skipped_fresh",
                        "age_s": round(age, 1),
                        "report_path": str(prev_path),
                        "sync": prev.get("sync") if isinstance(prev, dict) else {},
                        "summary": prev.get("summary") if isinstance(prev, dict) else {},
                    }
        except Exception:
            pass

    cat_cache = (cfg.get("catalog") or {}).get("cache_file") if isinstance(cfg, dict) else None
    if isinstance(cat_cache, str) and cat_cache.strip():
        cache_file = Path(cat_cache.strip())
        if not cache_file.is_absolute():
            try:
                from botex.config import PROJECT_ROOT

                cache_file = PROJECT_ROOT / cache_file
            except Exception:
                cache_file = cache_file.resolve()
    else:
        cache_file = catalog_mod.CACHE_FILE_DEFAULT

    sync_fn = getattr(catalog_mod, "sync_provider_catalogs", None)
    drift_fn = getattr(catalog_mod, "compute_config_drift", None)
    if sync_fn is None or drift_fn is None:
        return {"status": "unavailable", "reason": "catalog sync API missing"}

    sync_results: dict[str, Any] = {}
    try:
        sync_results = sync_fn(
            ProviderRegistry, provider_names, cache_file=cache_file, timeout=timeout
        )
        if not isinstance(sync_results, dict):
            sync_results = {}
    except Exception as exc:
        sys.stderr.write(f"[BoteX] catalog sync failed: {mask_secrets(type(exc).__name__)}\n")

    providers_cfg = cfg.get("providers") if isinstance(cfg, dict) else {}
    if not isinstance(providers_cfg, dict):
        providers_cfg = {}
    try:
        catalogs = _load_cached_catalogs(provider_names, providers_cfg, cache_file)
        drift_entries = drift_fn(providers_cfg, catalogs)
        if not isinstance(drift_entries, list):
            drift_entries = []
    except Exception as exc:
        drift_entries = []
        sys.stderr.write(
            f"[BoteX] catalog drift computation failed: {mask_secrets(type(exc).__name__)}\n"
        )

    drifted = [
        e for e in drift_entries if isinstance(e, dict) and e.get("present_in_catalog") is False
    ]
    for entry in drifted:
        mid = mask_secrets(str(entry.get("model_id") or ""))
        prov = mask_secrets(str(entry.get("provider") or ""))
        slot = mask_secrets(str(entry.get("slot") or ""))
        sys.stderr.write(
            f"[BoteX] DRIFT: configured model '{mid}' (provider '{prov}', slot '{slot}')"
            " absent from live catalog\n"
        )

    report = {
        "generated_at": time.time(),
        "generated_at_iso": datetime.now().isoformat(),
        "sync": sync_results,
        "drift": drift_entries,
        "summary": {
            "drifted": len(drifted),
            "no_data": sum(
                1
                for e in drift_entries
                if isinstance(e, dict) and e.get("present_in_catalog") is None
            ),
            "present": sum(
                1
                for e in drift_entries
                if isinstance(e, dict) and e.get("present_in_catalog") is True
            ),
        },
    }
    report_path: Optional[Path] = None
    try:
        report_path = _catalog_drift_report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = report_path.with_name(f"{report_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(str(tmp_path), str(report_path))
    except Exception as exc:
        report_path = None
        sys.stderr.write(
            f"[BoteX] catalog drift report write failed: {mask_secrets(type(exc).__name__)}\n"
        )

    return {
        "status": "ok",
        "sync": sync_results,
        "drift": drift_entries,
        "summary": report["summary"],
        "report_path": str(report_path) if report_path else "",
    }


def _execute_night_pulse_cycle(
    *,
    base_url: str = "",
    origin_repo: Optional[Path | str] = None,
    worktree_root: Optional[Path | str] = None,
    verify_jobs: Optional[list[dict[str, Any]]] = None,
    fence_jobs: Optional[list[dict[str, Any]]] = None,
    client: Optional[Any] = None,
    briefs_dir: Optional[Path] = None,
    ledger_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    from botex.core.swarm_store import get_active_leases, get_swarm_store_conn
    from botex.edge.host_os_driver import default_swarm_home, fence_local_epoch
    from botex.edge.verify_gate import run_external_verify

    resolved_url = (
        str(base_url or "").strip()
        or os.environ.get("BOTEX_CLOUD_URL", "").strip()
        or str((load_config().get("cloud") or {}).get("url") or "").strip()
    )
    if resolved_url or client is not None:
        sync_res = sync_bot_briefs(
            base_url=resolved_url,
            on_startup=True,
            briefs_dir=briefs_dir,
            client=client,
        )
    else:
        sync_res = {"ok": True, "skipped": True, "reason": "NO_CLOUD_URL"}

    leases = get_active_leases()
    wt_root = (
        Path(worktree_root).expanduser().resolve()
        if worktree_root is not None
        else (default_swarm_home() / "worktrees")
    )
    repo_path = Path(origin_repo).expanduser().resolve() if origin_repo is not None else Path.cwd()

    fenced_results: list[dict[str, Any]] = []
    seen_fences: set[tuple[str, int]] = set()

    for job in fence_jobs or []:
        oid = str(job.get("order_id") or "")
        ep = int(job.get("epoch") or 0)
        j_repo = job.get("origin_repo", repo_path)
        j_wt = job.get("wt_path") or (wt_root / f"{oid}_e{ep}")
        fenced_results.append(
            fence_local_epoch(
                oid,
                ep,
                j_repo,
                j_wt,
                job_guard=job.get("job_guard"),
            )
        )
        seen_fences.add((oid, ep))

    conn = get_swarm_store_conn()
    fenced_rows = conn.execute(
        "SELECT order_id, epoch FROM work_orders WHERE status = 'FENCED';"
    ).fetchall()
    for row in fenced_rows:
        oid = str(row["order_id"] or "")
        ep = int(row["epoch"] or 0)
        candidates = {ep}
        if ep > 0:
            candidates.add(ep - 1)
        if oid:
            prefix = f"{oid}_e"
            try:
                for wt_dir in wt_root.iterdir():
                    if not wt_dir.name.startswith(prefix):
                        continue
                    suffix = wt_dir.name[len(prefix) :]
                    if suffix.isdigit() and int(suffix) <= ep:
                        candidates.add(int(suffix))
            except OSError:
                pass
        for cand_ep in sorted(candidates):
            if (oid, cand_ep) in seen_fences:
                continue
            cand_wt = wt_root / f"{oid}_e{cand_ep}"
            if cand_wt.exists():
                fenced_results.append(fence_local_epoch(oid, cand_ep, repo_path, cand_wt))
                seen_fences.add((oid, cand_ep))

    verified_results: list[dict[str, Any]] = []
    for vjob in verify_jobs or []:
        verified_results.append(
            run_external_verify(
                vjob.get("origin_repo", repo_path),
                vjob["wt_path"],
                vjob["verify_command"],
                order_id=str(vjob.get("order_id") or ""),
                epoch=int(vjob.get("epoch") or 0),
                diff_text=vjob.get("diff_text"),
                changed_files=vjob.get("changed_files"),
                allow_test_mutation=bool(vjob.get("allow_test_mutation", False)),
                ledger_path=vjob.get("ledger_path", ledger_path),
            )
        )

    catalog_drift: dict[str, Any] = {"status": "skipped"}
    try:
        catalog_drift = _run_catalog_drift_sync(load_config())
    except Exception as exc:
        sys.stderr.write(f"[BoteX] catalog drift sync error: {mask_secrets(type(exc).__name__)}\n")
        catalog_drift = {"status": "error", "error": type(exc).__name__}

    return {
        "sync": sync_res,
        "active_leases": len(leases),
        "leases": leases,
        "fenced": fenced_results,
        "verified": verified_results,
        "catalog_sync": catalog_drift.get("sync") or {},
        "catalog_drift": catalog_drift,
    }


def run_night_pulse(
    *,
    base_url: str = "",
    once: bool = True,
    interval_s: float = 60.0,
    max_cycles: Optional[int] = None,
    lock_path: Optional[Path | str] = None,
    origin_repo: Optional[Path | str] = None,
    worktree_root: Optional[Path | str] = None,
    verify_jobs: Optional[list[dict[str, Any]]] = None,
    fence_jobs: Optional[list[dict[str, Any]]] = None,
    client: Optional[Any] = None,
    briefs_dir: Optional[Path] = None,
    ledger_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Run single-instance Night-Pulse sync, epoch fencing, and external verification."""
    import time

    from botex.edge.host_os_driver import SingleInstanceLock, SingleInstanceLockError

    lock = SingleInstanceLock(lock_path)
    try:
        lock.__enter__()
    except SingleInstanceLockError as exc:
        return {
            "ok": False,
            "error": "LOCK_HELD",
            "detail": coerce_text(mask_secrets(str(exc))),
            "cycles": 0,
        }

    cycles = 0
    last_cycle: dict[str, Any] = {}
    try:
        while True:
            cycles += 1
            last_cycle = _execute_night_pulse_cycle(
                base_url=base_url,
                origin_repo=origin_repo,
                worktree_root=worktree_root,
                verify_jobs=verify_jobs,
                fence_jobs=fence_jobs,
                client=client,
                briefs_dir=briefs_dir,
                ledger_path=ledger_path,
            )
            if once or (max_cycles is not None and cycles >= max_cycles):
                break
            time.sleep(max(0.1, float(interval_s)))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()

    return {"ok": True, "cycles": cycles, **last_cycle}


def _cli_night_pulse(argv: list[str]) -> None:
    """CLI handler for ``botex night-pulse [--once]``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="botex night-pulse",
        description="Synchronize host leases, fence stale epochs, and execute pending verify checks.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single Night-Pulse cycle and exit",
    )
    parser.add_argument(
        "--url",
        default="",
        help="Cloud gateway base URL (default: BOTEX_CLOUD_URL or config cloud.url)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Poll interval in seconds when running continuously (default: 60)",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=".",
        help="Origin repository directory (default: cwd)",
    )
    parser.add_argument(
        "--worktree-root",
        default="",
        help="Override epoch worktree root directory",
    )
    parser.add_argument(
        "--lock-path",
        default="",
        help="Override single-instance lock file path",
    )
    args = parser.parse_args(argv)
    res = run_night_pulse(
        base_url=str(args.url or ""),
        once=bool(args.once),
        interval_s=float(args.interval),
        lock_path=args.lock_path or None,
        origin_repo=args.workspace or ".",
        worktree_root=args.worktree_root or None,
    )
    if res.get("ok"):
        print(
            ok(
                f"night-pulse: {res['cycles']} cycle(s), "
                f"{res.get('active_leases', 0)} active lease(s), "
                f"{len(res.get('fenced') or [])} fenced, "
                f"{len(res.get('verified') or [])} verified"
            )
        )
        sys.exit(0)
    print(
        err(f"night-pulse failed: {res.get('error') or 'unknown'} {res.get('detail') or ''}"),
        file=sys.stderr,
    )
    sys.exit(1)


def _cli_maintenance(argv: list[str]) -> None:
    """Run L2 maintenance jobs manually — local parity for the VPS
    ``botex-maintenance.timer`` (Windows dev has no systemd)."""
    import argparse
    import json

    from botex.ops import maintenance

    parser = argparse.ArgumentParser(
        prog="botex maintenance",
        description="Run background maintenance jobs (provider warmup, session sweep, id repair).",
    )
    parser.add_argument(
        "job",
        nargs="?",
        default="all",
        choices=["all", "warm", "sweep", "repair", "rotate", "links"],
        help="Job to run (default: all)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With 'links': apply high-confidence dead-link repairs (default: suggest only)",
    )
    parser.add_argument("--workspace", "-w", default=".", help="Workspace dir (default: cwd)")
    args = parser.parse_args(argv)

    results: dict = {}
    if args.job in ("all", "warm"):
        results["warm_provider_metadata"] = maintenance.warm_provider_metadata(args.workspace)
    if args.job in ("all", "sweep"):
        results["sweep_empty_chat_sessions"] = maintenance.sweep_empty_chat_sessions(args.workspace)
    if args.job in ("all", "repair"):
        results["repair_frontmatter_ids"] = maintenance.repair_frontmatter_ids(args.workspace)
    if args.job in ("all", "rotate"):
        results["rotate_analytics_ledger"] = maintenance.rotate_analytics_ledger(args.workspace)
    if args.job in ("all", "links"):
        results["repair_dead_links"] = maintenance.repair_dead_links(
            args.workspace, apply=args.apply and args.job == "links"
        )
    print(json.dumps(results, indent=2, default=str))


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
        help="API provider (openrouter/openrouter-free/nvidia/google/ollama/groq/cloudflare; empty = config default)",
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
    prompt_session = _get_prompt_toolkit_session()
    while True:
        try:
            task = _prompt_line(prompt_session, accent("botex> ")).strip()
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
        if _try_local_command(task, workspace=args.workspace):
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
            help="API provider (openrouter/openrouter-free/nvidia/google/ollama/groq/cloudflare; empty = config default)",
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
            "--allow-expensive-model",
            action="store_true",
            help="Authorize running a model that exceeds pricing.max_*_per_mtok caps",
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
                allow_expensive_model=True if args.allow_expensive_model else None,
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
    elif len(sys.argv) > 1 and sys.argv[1] == "mcp-bridge":
        _cli_mcp_bridge(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] in ("wallet", "credits"):
        _cli_wallet(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "chat":
        _cli_chat(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "memory":
        _cli_memory(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "cloud":
        _cli_cloud(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "edge":
        _cli_edge(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "bots":
        _cli_bots(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "night-pulse":
        _cli_night_pulse(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "setup":
        _cli_setup()
    elif len(sys.argv) > 1 and sys.argv[1] == "skills":
        _cli_skills(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "maintenance":
        _cli_maintenance(sys.argv[2:])
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

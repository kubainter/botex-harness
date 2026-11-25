# SPDX-License-Identifier: Apache-2.0
"""Read-only Memory Vault bridge for internal BoteX subagent workers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from botex.memory.memory import deterministic_recall, gather_recall_context
from botex.tools.security import mask_secrets, wrap_untrusted_content

_MAX_WORKER_RECALL_CALLS = 3
_MAX_WORKER_MEMORY_PAYLOAD_BYTES = 16_384
_WORKER_SCOPE_MAP: Dict[str, str] = {"local": "local", "global": "global", "all": "all"}
_LOCAL_EDGE_ERR: Dict[str, Any] = {
    "ok": False,
    "error": "LOCAL_VAULT_UNAVAILABLE_ON_EDGE",
    "detail": "Local .botex/memory lives on the edge host; pass scope='global' or scope='all' to read the Cloud Swarm Vault.",
}


def _mask_obj(obj: Any) -> Any:
    """Recursively apply mask_secrets to all str leaves inside dict and list objects."""
    if isinstance(obj, str):
        return mask_secrets(obj)
    if isinstance(obj, dict):
        return {str(k): _mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_obj(item) for item in obj]
    return obj


def _cap_wrapped_text(
    raw_body: str,
    *,
    tag: str,
    entry_id: Optional[str] = None,
    overhead_bytes: int = 0,
) -> tuple[str, bool]:
    """Mask secrets, cap bytes, and wrap with untrusted content boundary.

    Returns (wrapped_body, truncated_flag).
    The closing </{tag}> is ALWAYS preserved; we never double-wrap.
    """
    masked = mask_secrets(raw_body)
    available = max(256, _MAX_WORKER_MEMORY_PAYLOAD_BYTES - overhead_bytes - 240)
    encoded = masked.encode("utf-8")
    truncated = False
    if len(encoded) > available:
        # Trim to available bytes, keeping valid UTF-8
        trimmed = encoded[:available]
        # Decode what we can; drop incomplete trailing byte
        try:
            body = trimmed.decode("utf-8")
        except UnicodeDecodeError:
            body = trimmed.decode("utf-8", errors="ignore")
        truncated = True
        body += "\n...[truncated]"
    else:
        body = masked

    if entry_id is not None:
        wrapped = wrap_untrusted_content(body, tag=tag, id=entry_id)
    else:
        wrapped = wrap_untrusted_content(body, tag=tag)
    return wrapped, truncated


def _strip_untrusted_memory_content(content: str) -> str:
    """Strip outer <untrusted_memory_content ...> ... </untrusted_memory_content> if present."""
    import re

    pattern = re.compile(
        r"^<untrusted_memory_content[^>]*>\s*\n?(.*?)\s*\n?</untrusted_memory_content>\s*$",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.match(content)
    if m:
        return m.group(1)
    return content


def _vault_read_memory(
    workspace_dir: str,
    memory_id: str,
    scope: str,
) -> Dict[str, Any]:
    """Internal vault read (delegates to botex.memory.memory.read_memory logic)."""
    from botex.memory.memory import read_memory as _read_mem

    return _read_mem(
        workspace_dir=workspace_dir,
        memory_id=memory_id,
        scope=scope,
    )


def worker_read_memory(
    memory_id: str,
    *,
    scope: str = "all",
    workspace_dir: Union[str, Path] = ".",
    is_edge_workspace: bool = False,
) -> Dict[str, Any]:
    """Read-only bridge: read a memory entry for subagent workers."""
    norm_scope = _WORKER_SCOPE_MAP.get(str(scope or "").strip().lower(), "all")

    if is_edge_workspace:
        if norm_scope == "local":
            return dict(_LOCAL_EDGE_ERR)
        effective_scope = "global"
    else:
        effective_scope = norm_scope

    res = _vault_read_memory(
        workspace_dir=str(workspace_dir),
        memory_id=str(memory_id or ""),
        scope=effective_scope,
    )

    if not res.get("ok"):
        return {"ok": False, "error": mask_secrets(str(res.get("error") or "Read failed."))}

    raw_content = str(res.get("content") or "")
    # Strip any outer untrusted_memory_content wrapping to avoid double-wrap
    inner_body = _strip_untrusted_memory_content(raw_content)
    meta = _mask_obj(res.get("metadata") or {})
    # Measure JSON overhead of metadata without content overhead
    overhead = len(json.dumps(meta).encode("utf-8"))
    wrapped, was_capped = _cap_wrapped_text(
        inner_body,
        tag="untrusted_memory_content",
        entry_id=str(res.get("id") or memory_id),
        overhead_bytes=overhead,
    )
    return {
        "ok": True,
        "id": res.get("id", memory_id),
        "metadata": meta,
        "content": wrapped,
        "scope": str(res.get("scope") or effective_scope),
        "truncated": was_capped,
    }


async def worker_recall_memory(
    query: str,
    *,
    topic: Optional[str] = None,
    scope: str = "all",
    project: Optional[str] = None,
    workspace_dir: Union[str, Path] = ".",
    is_edge_workspace: bool = False,
    use_llm_synthesis: bool = True,
) -> Dict[str, Any]:
    """Read-only bridge: recall memory via query for subagent workers."""
    if not str(query or "").strip():
        return {"ok": False, "error": "EMPTY_QUERY", "detail": "query must be a non-empty string."}

    norm_scope = _WORKER_SCOPE_MAP.get(str(scope or "").strip().lower(), "all")

    if is_edge_workspace:
        if norm_scope == "local":
            return dict(_LOCAL_EDGE_ERR)
        effective_scope = "global"
    else:
        effective_scope = norm_scope

    ctx = await asyncio.to_thread(
        gather_recall_context,
        workspace_dir=str(workspace_dir),
        query=str(query),
        topic=topic or None,
        scope=effective_scope,
        project=project or None,
    )

    if not ctx.get("ok"):
        return _mask_obj(ctx)

    nodes = ctx.get("nodes") or []

    result: Optional[Dict[str, Any]] = None
    briefing_served = False

    briefing_cfg: Dict[str, Any] = {}
    try:
        from botex.server.config import load_config as _load_cfg

        briefing_cfg = dict(
            (((_load_cfg().get("memory") or {}).get("recall") or {}).get("briefing") or {})
        )
    except Exception:
        briefing_cfg = {}
    if (
        nodes
        and use_llm_synthesis
        and briefing_cfg.get("enabled", True)
        and briefing_cfg.get("serve", True)
    ):
        try:
            from botex.ops.curator.recall_briefing import build_briefing_answer

            result = await asyncio.to_thread(
                build_briefing_answer,
                str(workspace_dir),
                str(query),
                nodes,
                project or None,
                float(briefing_cfg.get("max_age_h", 72) or 72),
            )
            briefing_served = result is not None
        except Exception:
            result = None

    if nodes and use_llm_synthesis and result is None:
        try:
            from botex.ops.curator import synthesize_recall

            result = await synthesize_recall(str(query), nodes)
        except Exception:
            result = None
        if not (isinstance(result, dict) and result.get("answer")):
            result = None

    # Determine recall status
    if result is not None:
        recall_status = "BRIEFING_SERVED" if briefing_served else "SYNTHESIZED"
    elif nodes and not use_llm_synthesis:
        recall_status = "DETERMINISTIC_BUDGET"
    elif nodes:
        recall_status = "FALLBACK_MODELS_FAILED"
    else:
        recall_status = "ZERO_NODES_FOUND"

    if result is None:
        result = deterministic_recall(str(query), nodes)

    # Build metadata dict
    sources = result.get("sources") if isinstance(result, dict) else None
    confidence = result.get("confidence") if isinstance(result, dict) else None
    tier_used = result.get("tier_used") if isinstance(result, dict) else None

    metadata_dict: Dict[str, Any] = {
        "sources": sources,
        "confidence": confidence,
        "tier_used": tier_used,
        "recall_status": recall_status,
        "context_nodes": len(nodes),
    }

    # Measure overhead
    overhead = len(json.dumps(metadata_dict).encode("utf-8"))

    answer_raw = str(result.get("answer") or "")
    wrapped_ans, was_capped = _cap_wrapped_text(
        answer_raw,
        tag="untrusted_memory_recall",
        overhead_bytes=overhead,
    )

    truncated_flag = bool(ctx.get("truncated")) or was_capped

    return {
        "ok": True,
        "answer": wrapped_ans,
        "sources": [str(s) for s in (result.get("sources") or [])],
        "confidence": str(result.get("confidence") or "LOW"),
        "tier_used": str(result.get("tier_used") or "deterministic"),
        "recall_status": recall_status,
        "context_nodes": len(nodes),
        "truncated": truncated_flag,
    }

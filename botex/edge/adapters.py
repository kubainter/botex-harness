# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Downward capability adapters, epoch checkpointing, and the verify terminal hook.

Plan 038 (``ADR-SWARM-02`` Phase 2b): translates canonical ``WorkOrder`` objects
into per-harness dispatch specs for ``botex`` / ``zcode`` / ``devin`` — never
widening ``mode_request`` — while the ``antigravity_ls`` family stays excluded
until a read-status API exists in L0. ``EpochCheckpointManager`` snapshots WIP
every ``min_turns``–``max_turns`` file-mutating turns (or after a green
intermediate test) and refuses to push ``swarm/wip/<order>/e<epoch>`` unless the
remote epoch still matches the local one. ``finalize_verified_work_order`` is
the ``no_self_report_done`` terminal hook: only an external
``verify_gate``/``acceptance_review`` result with ``ok is True`` marks an order
``DONE`` and refreshes the bot brief.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

_LOG = logging.getLogger("botex.edge.adapters")

__all__ = [
    "ANTIGRAVITY_EXCLUSION_REASON",
    "HARNESS_MODE_MAP",
    "RESTRICTIVENESS_RANK",
    "SUPPORTED_HARNESSES",
    "EpochCheckpointManager",
    "build_anti_spiral_reset",
    "check_harness_eligible",
    "finalize_verified_work_order",
    "translate_work_order",
]

# Canonical capability ladder. Mapping is strictly downward-or-equal: a target
# mode whose rank exceeds the requested rank would widen privileges and is
# always rejected before dispatch.
RESTRICTIVENESS_RANK: Dict[str, int] = {
    "readonly": 0,
    "plan": 0,
    "edit": 1,
    "fs_write": 1,
    "destructive": 2,
    "full": 3,
    "yolo": 3,
}

SUPPORTED_HARNESSES = frozenset({"botex", "zcode", "devin"})

ANTIGRAVITY_EXCLUSION_REASON = "missing read-status API in L0"

# Requested canonical mode -> granted canonical mode, per target harness.
# Entries absent here fail closed (BLOCKED) rather than silently degrade.
# ``fs_write`` is deliberately unmapped everywhere: no harness exposes a safe
# file-system-write-only equivalent yet (Plan 038 test invariant).
HARNESS_MODE_MAP: Dict[str, Dict[str, str]] = {
    "botex": {
        "readonly": "readonly",
        "plan": "readonly",
        "edit": "edit",
        "destructive": "destructive",
        "full": "full",
        "yolo": "full",
    },
    "zcode": {
        "readonly": "plan",
        "plan": "plan",
        "edit": "edit",
    },
    "devin": {
        "readonly": "readonly",
        "plan": "plan",
        "edit": "edit",
        "destructive": "destructive",
        "full": "full",
        "yolo": "full",
    },
}

# Preferred outbound transport per harness. ``acp`` = Agent Client Protocol
# (JSON-RPC over stdio); ``mcp_or_cli`` = BoteX MCP/CLI entry points. ACP-capable
# harnesses keep ``stdout`` as the documented fallback channel.
HARNESS_TRANSPORT: Dict[str, str] = {
    "botex": "mcp_or_cli",
    "zcode": "acp",
    "devin": "acp",
}

_MODE_BLOCK_REASON = "unmapped_or_widened_mode"
_MAX_NOTE_CHARS = 1000
_EPOCH_REF_RE = re.compile(r"/e(\d+)\s*$")


# ---------------------------------------------------------------------------
# Small duck-typed helpers (WorkOrder may be a dataclass or a plain dict)
# ---------------------------------------------------------------------------


def _order_field(order: Any, name: str, default: Any = None) -> Any:
    if order is None:
        return default
    if isinstance(order, Mapping):
        return order.get(name, default)
    return getattr(order, name, default)


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _masked(text: Any, limit: int = _MAX_NOTE_CHARS) -> str:
    return coerce_text(mask_secrets(str(text or ""))).strip()[:limit]


def _verify_kind(order: Any) -> str:
    verify = _order_field(order, "verify", None)
    if verify is None:
        return "none"
    if isinstance(verify, str):
        return verify.strip().lower() or "none"
    if isinstance(verify, Mapping):
        return str(verify.get("kind") or "command").strip().lower() or "command"
    kind = getattr(verify, "kind", None)
    if kind is None:
        return "none"
    return str(kind).strip().lower() or "none"


def _verify_field(order: Any, name: str, default: Any = "") -> Any:
    verify = _order_field(order, "verify", None)
    if verify is None:
        return default
    if isinstance(verify, Mapping):
        return verify.get(name, default)
    return getattr(verify, name, default)


def _order_id(order: Any) -> str:
    return str(_order_field(order, "order_id", "") or "").strip()


def _clean_order_id(raw: str) -> Optional[str]:
    clean = sanitize_identifier(str(raw or "").strip())
    if not clean or clean != str(raw or "").strip():
        return None
    return clean


def _normalize_harness_name(name: Any) -> str:
    return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _is_excluded_harness(norm: str) -> bool:
    # The whole antigravity* family is excluded from automated failover until
    # the L0 read-status/cascade-health API exists (Plan 038 invariant).
    return (
        norm == "antigravity"
        or norm.startswith("antigravity_")
        or norm.replace("_", "").startswith("antigravityls")
    )


# ---------------------------------------------------------------------------
# Harness eligibility & downward mode mapping
# ---------------------------------------------------------------------------


def check_harness_eligible(target_harness: str) -> Dict[str, Any]:
    """Return eligibility for ``target_harness``; excluded/unknown -> BLOCKED."""
    norm = _normalize_harness_name(target_harness)
    if _is_excluded_harness(norm):
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": ANTIGRAVITY_EXCLUSION_REASON,
            "harness": norm,
        }
    if norm not in SUPPORTED_HARNESSES:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "unsupported_harness",
            "harness": norm,
        }
    transport = HARNESS_TRANSPORT.get(norm, "")
    out: Dict[str, Any] = {
        "ok": True,
        "status": "eligible",
        "harness": norm,
        "transport": transport,
    }
    if transport == "acp":
        out["transport_fallback"] = "stdout"
    return out


def _mode_block(harness: str, mode_request: str, detail: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCKED",
        "reason": _MODE_BLOCK_REASON,
        "harness": harness,
        "mode_request": mode_request,
        "detail": detail,
    }


def _resolve_mode(
    harness: str, mode_request: Any
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Map ``mode_request`` to a harness mode, strictly downward in rank."""
    req = str(mode_request or "").strip().lower()
    req_rank = RESTRICTIVENESS_RANK.get(req)
    if req_rank is None:
        return None, _mode_block(harness, req, "mode_request_not_in_restrictiveness_rank")
    target = HARNESS_MODE_MAP.get(harness, {}).get(req)
    tgt_rank = RESTRICTIVENESS_RANK.get(target) if target else None
    if target is None:
        return None, _mode_block(harness, req, "no_safe_downward_mapping")
    if tgt_rank is None or tgt_rank > req_rank:
        return None, _mode_block(harness, req, "target_mode_would_widen_restrictiveness")
    return target, None


def translate_work_order(
    order: Any,
    target_harness: str,
    *,
    checkpoint_ref: str = "",
    blind_alley_note: str = "",
) -> Dict[str, Any]:
    """Translate ``order`` into a fresh-epoch dispatch spec for ``target_harness``.

    Never emits ``--resume``: every hop runs clean in a new epoch worktree
    ``e<N+1>`` on ``swarm/wip/<order_id>/e<N+1>``. ``blind_alley_note`` carries
    the concise anti-spiral context (secret-masked) into the next hop.
    """
    elig = check_harness_eligible(target_harness)
    if not elig.get("ok"):
        return dict(elig)
    harness = str(elig["harness"])

    if order is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "missing_order",
            "harness": harness,
        }

    raw_oid = _order_id(order)
    order_id = _clean_order_id(raw_oid)
    if order_id is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "invalid_order_id",
            "harness": harness,
            "order_id": raw_oid,
        }

    base_epoch = max(0, _coerce_int(_order_field(order, "epoch", 0), 0))
    new_epoch = base_epoch + 1
    branch = f"swarm/wip/{order_id}/e{new_epoch}"

    mode_request = _order_field(order, "mode_request", "edit")
    mapped_mode, mode_err = _resolve_mode(harness, mode_request)
    if mode_err is not None:
        return mode_err

    masked_checkpoint = _masked(checkpoint_ref)
    masked_note = _masked(blind_alley_note)
    anti_spiral = bool(masked_checkpoint or masked_note)

    base_ref = str(_order_field(order, "base_ref", "HEAD") or "HEAD").strip() or "HEAD"
    scope_raw = _order_field(order, "scope", ()) or ()
    if isinstance(scope_raw, str):
        scope_list = [scope_raw]
    elif isinstance(scope_raw, (list, tuple)):
        scope_list = [str(s) for s in scope_raw]
    else:
        scope_list = []
    goal = _masked(_order_field(order, "goal", ""))
    verify_kind = _verify_kind(order)
    verify_command = _masked(_verify_field(order, "command", ""), limit=300)

    prompt_lines = [goal or f"Execute WorkOrder {order_id}."]
    if base_ref != "HEAD":
        prompt_lines.append(f"Base ref: {base_ref}")
    if scope_list:
        prompt_lines.append("Scope: " + ", ".join(scope_list))
    prompt_lines.append(f"WorkOrder {order_id} — fresh epoch e{new_epoch} on branch {branch}.")
    if masked_checkpoint:
        prompt_lines.append(f"Anti-spiral checkpoint ref: {masked_checkpoint}")
    if masked_note:
        prompt_lines.append(f"Anti-spiral blind-alley note: {masked_note}")
    if verify_kind != "none" and verify_command:
        prompt_lines.append(f"Required verification: {verify_command}")

    result: Dict[str, Any] = {
        "ok": True,
        "status": "READY",
        "harness": harness,
        "transport": HARNESS_TRANSPORT.get(harness, ""),
        "mode": mapped_mode,
        "mode_request": str(mode_request or "").strip().lower(),
        "order_id": order_id,
        "epoch": new_epoch,
        "base_epoch": base_epoch,
        "branch": branch,
        "worktree": f"{order_id}_e{new_epoch}",
        "base_ref": base_ref,
        "scope": scope_list,
        "goal": goal,
        "verify": {"kind": verify_kind, "command": verify_command},
        "prompt": "\n".join(prompt_lines),
        "resume": False,
        "fresh_run": True,
        "fresh_epoch": True,
        "checkpoint_ref": masked_checkpoint,
        "blind_alley_note": masked_note,
        "anti_spiral_reset": anti_spiral,
    }
    if result["transport"] == "acp":
        result["transport_fallback"] = "stdout"
        result["acp"] = {"protocol": "agent-client-protocol", "framing": "json-rpc/stdio"}
    return result


# ---------------------------------------------------------------------------
# Anti-Spiral Reset
# ---------------------------------------------------------------------------


def _epoch_from_ref(ref: Any) -> Optional[int]:
    match = _EPOCH_REF_RE.search(str(ref or ""))
    return int(match.group(1)) if match else None


def _reset_context(order_id: str, epoch: int, branch: str, ref: str, note: str) -> str:
    parts = [
        f"Anti-Spiral Reset for WorkOrder {order_id}: previous hops spiralled; "
        f"restart clean from the last good checkpoint on epoch e{epoch} ({branch})."
    ]
    if ref:
        parts.append(f"Checkpoint ref: {ref}")
    if note:
        parts.append(f"Blind-alley note (do not repeat): {note}")
    parts.append("Do not resume prior session state; --resume across harnesses is forbidden.")
    return "\n".join(parts)


def build_anti_spiral_reset(
    order: Any,
    *,
    last_checkpoint_ref: str = "",
    blind_alley_note: str = "",
    epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a clean next-hop context from ``last_checkpoint_ref`` + masked note."""
    order_id = ""
    base_epoch: Optional[int] = None
    if order is not None:
        if isinstance(order, str):
            order_id = order.strip()
        else:
            order_id = _order_id(order)
            raw_epoch = _order_field(order, "epoch", None)
            if raw_epoch is not None:
                base_epoch = _coerce_int(raw_epoch, 0)
    clean_oid = _clean_order_id(order_id)
    if clean_oid is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": "invalid_order_id",
            "order_id": order_id,
        }
    if epoch is not None:
        base_epoch = max(0, _coerce_int(epoch, 0))
    if base_epoch is None:
        base_epoch = _epoch_from_ref(last_checkpoint_ref)
    if base_epoch is None:
        base_epoch = 0
    new_epoch = base_epoch + 1
    ref = _masked(last_checkpoint_ref)
    note = _masked(blind_alley_note)
    branch = f"swarm/wip/{clean_oid}/e{new_epoch}"
    return {
        "ok": True,
        "status": "anti_spiral_reset",
        "order_id": clean_oid,
        "epoch": new_epoch,
        "branch": branch,
        "worktree": f"{clean_oid}_e{new_epoch}",
        "checkpoint_ref": ref,
        "blind_alley_note": note,
        "resume": False,
        "fresh_run": True,
        "handoff_context": _reset_context(clean_oid, new_epoch, branch, ref, note),
    }


# ---------------------------------------------------------------------------
# Epoch Checkpoint Manager
# ---------------------------------------------------------------------------


def _run_git(
    repo_dir: Union[str, Path],
    args: list[str],
    *,
    timeout_s: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo_dir), *args]
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["SSH_ASKPASS"] = "true"
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            cmd, 124, stdout="", stderr=coerce_text(mask_secrets(str(exc)))
        )


def _invoke_epoch_fn(
    fn: Callable[..., Any],
    *,
    order_id: str,
    epoch: int,
    remote: str,
    branch: str,
    repo_dir: str,
) -> Any:
    """Call ``fetch_remote_epoch_fn`` tolerantly: kwargs subset or no-arg."""
    kwargs = {
        "order_id": order_id,
        "epoch": epoch,
        "remote": remote,
        "branch": branch,
        "repo_dir": repo_dir,
    }
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    accepted = {
        p.name
        for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    if not accepted:
        return fn()
    return fn(**{k: v for k, v in kwargs.items() if k in accepted})


def _coerce_remote_epoch(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        for key in ("remote_epoch", "epoch", "fencing_epoch"):
            if key in value:
                return _coerce_remote_epoch(value.get(key))
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None


def _push_result_ok(pres: Any) -> tuple[bool, str]:
    if pres is None or pres is True:
        return True, ""
    if isinstance(pres, Mapping):
        if pres.get("ok") is False:
            return False, _masked(pres.get("error") or pres.get("stderr") or "push_failed", 300)
        rc = pres.get("returncode", pres.get("rc"))
        if rc not in (None, 0):
            return False, _masked(pres.get("stderr") or pres.get("error") or "push_failed", 300)
        return True, ""
    rc = getattr(pres, "returncode", None)
    if rc is None or rc == 0:
        return True, ""
    return False, _masked(getattr(pres, "stderr", "") or "push_failed", 300)


class EpochCheckpointManager:
    """Auto-checkpoint WIP with pre-push ``remote_epoch == local_epoch`` fencing.

    ``record_turn`` counts file-mutating turns (and green intermediate tests);
    ``should_checkpoint`` fires once the count reaches the configured interval
    inside ``[min_turns, max_turns]`` or a green test is pending.
    ``checkpoint_and_push`` verifies the remote epoch (via
    ``fetch_remote_epoch_fn`` callback, or ``git ls-remote`` on the swarm
    remote when a repo is known) before pushing
    ``swarm/wip/<order_id>/e<epoch>`` with LF normalization flags. A mismatch
    aborts with ``status="stale_epoch"`` — the push never happens.
    """

    def __init__(
        self,
        order: Any = None,
        *,
        order_id: str = "",
        epoch: Optional[int] = None,
        origin_repo: Optional[Union[str, Path]] = None,
        worktree_root: Optional[Union[str, Path]] = None,
        swarm_remote: str = "",
        min_turns: int = 5,
        max_turns: int = 8,
        interval: Optional[int] = None,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]] = None,
        push_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        resolved_id = str(order_id or "").strip()
        resolved_epoch = epoch
        if order is not None and not isinstance(order, str):
            resolved_id = resolved_id or _order_id(order)
            if resolved_epoch is None:
                resolved_epoch = _order_field(order, "epoch", 0)
        elif isinstance(order, str) and not resolved_id:
            resolved_id = order.strip()
        clean_id = _clean_order_id(resolved_id)
        if clean_id is None:
            raise ValueError(f"EpochCheckpointManager requires a valid order_id: {resolved_id!r}")
        self._order_id = clean_id
        self._epoch = max(0, _coerce_int(resolved_epoch, 0))

        self._repo_dir = Path(origin_repo).expanduser() if origin_repo is not None else None
        self._worktree_root = (
            Path(worktree_root).expanduser() if worktree_root is not None else None
        )
        self._swarm_remote = (
            str(swarm_remote or "").strip()
            or os.environ.get("BOTEX_SWARM_REMOTE", "").strip()
            or "swarm"
        )

        self._min_turns = int(min_turns)
        self._max_turns = int(max_turns)
        if self._min_turns < 1 or self._max_turns < self._min_turns:
            raise ValueError(
                f"invalid turn bounds: min_turns={self._min_turns} max_turns={self._max_turns}"
            )
        self._interval = (
            self._min_turns
            if interval is None
            else min(max(int(interval), self._min_turns), self._max_turns)
        )

        self._fetch_remote_epoch_fn = fetch_remote_epoch_fn
        self._push_fn = push_fn
        self._mutating_turns = 0
        self._green_pending = False
        self._total_turns = 0
        self._last_checkpoint_ref = ""

    # -- introspection ------------------------------------------------------

    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def mutating_turns(self) -> int:
        return self._mutating_turns

    @property
    def total_turns(self) -> int:
        return self._total_turns

    @property
    def last_checkpoint_ref(self) -> str:
        return self._last_checkpoint_ref

    @property
    def branch(self) -> str:
        return f"swarm/wip/{self._order_id}/e{self._epoch}"

    # -- turn accounting ----------------------------------------------------

    def record_turn(
        self,
        *,
        mutated_files: bool = False,
        green_test: bool = False,
    ) -> Dict[str, Any]:
        """Record one executed turn; returns the current checkpoint snapshot."""
        self._total_turns += 1
        if mutated_files:
            self._mutating_turns += 1
        if green_test:
            self._green_pending = True
        return {
            "total_turns": self._total_turns,
            "mutating_turns": self._mutating_turns,
            "green_pending": self._green_pending,
            "should_checkpoint": self.should_checkpoint(),
        }

    def should_checkpoint(self) -> bool:
        """True when the mutating-turn interval elapsed or a green test is pending."""
        return bool(self._green_pending or self._mutating_turns >= self._interval)

    def mark_checkpointed(self, ref: str = "") -> None:
        """Reset turn counters after a successful checkpoint/push."""
        self._mutating_turns = 0
        self._green_pending = False
        if ref:
            self._last_checkpoint_ref = str(ref)

    # -- epoch resolution ---------------------------------------------------

    def _git_remote_epoch(
        self, repo_dir: Path, remote: str, order_id: str, local_epoch: int
    ) -> Optional[int]:
        """Highest ``e<N>`` epoch branch for this order on ``remote`` (None on error).

        No matching remote refs yields ``local_epoch`` — an empty remote cannot
        conflict with the local epoch. Production pushes should still inject
        ``fetch_remote_epoch_fn`` bound to the VPS epoch record.
        """
        res = _run_git(
            repo_dir,
            ["ls-remote", remote, f"refs/heads/swarm/wip/{order_id}/e*"],
            timeout_s=60.0,
        )
        if res.returncode != 0:
            return None
        prefix = f"swarm/wip/{order_id}/e"
        epochs = [
            int(m.group(1))
            for line in res.stdout.splitlines()
            if prefix in line and (m := re.search(r"/e(\d+)\s*$", line.strip()))
        ]
        return max(epochs) if epochs else local_epoch

    def _resolve_remote_epoch(
        self,
        *,
        repo_dir: Optional[Path],
        remote: str,
        branch: str,
        order_id: str,
        epoch: int,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]],
    ) -> tuple[Optional[int], str]:
        fn = (
            fetch_remote_epoch_fn
            if fetch_remote_epoch_fn is not None
            else self._fetch_remote_epoch_fn
        )
        if fn is not None:
            raw = _invoke_epoch_fn(
                fn,
                order_id=order_id,
                epoch=epoch,
                remote=remote,
                branch=branch,
                repo_dir=str(repo_dir or ""),
            )
            epoch = _coerce_remote_epoch(raw)
            return epoch, "callback"
        if repo_dir is not None:
            return self._git_remote_epoch(repo_dir, remote, order_id, epoch), "git_ls_remote"
        return None, "none"

    # -- push ----------------------------------------------------------------

    def checkpoint_and_push(
        self,
        *,
        repo_dir: Optional[Union[str, Path]] = None,
        remote: str = "",
        order_id: str = "",
        epoch: Optional[int] = None,
        branch: str = "",
        commit: bool = True,
        commit_message: str = "",
        dry_run: bool = False,
        fetch_remote_epoch_fn: Optional[Callable[..., Any]] = None,
        push_fn: Optional[Callable[..., Any]] = None,
        timeout_s: float = 120.0,
    ) -> Dict[str, Any]:
        """Commit WIP (optional) and push ``branch`` after the epoch fence check.

        Returns ``status="stale_epoch"`` without pushing whenever the remote
        epoch disagrees with the local one, or ``status="epoch_check_failed"``
        when no epoch source is available — fail-stop either way.
        """
        oid = _clean_order_id(order_id) if str(order_id or "").strip() else self._order_id
        if oid is None:
            return {
                "ok": False,
                "status": "BLOCKED",
                "reason": "invalid_order_id",
                "order_id": str(order_id or ""),
            }
        ep = self._epoch if epoch is None else max(0, _coerce_int(epoch, self._epoch))
        rem = str(remote or "").strip() or self._swarm_remote
        br = str(branch or "").strip() or f"swarm/wip/{oid}/e{ep}"
        repo = Path(repo_dir).expanduser() if repo_dir is not None else self._repo_dir

        base = {
            "order_id": oid,
            "epoch": ep,
            "local_epoch": ep,
            "branch": br,
            "remote": rem,
        }
        try:
            remote_epoch, epoch_source = self._resolve_remote_epoch(
                repo_dir=repo,
                remote=rem,
                branch=br,
                order_id=oid,
                epoch=ep,
                fetch_remote_epoch_fn=fetch_remote_epoch_fn,
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "epoch_check_failed",
                "error": _masked(str(exc), 300),
                "epoch_source": "error",
                **base,
            }
        base["epoch_source"] = epoch_source
        if remote_epoch is None:
            return {
                "ok": False,
                "status": "epoch_check_failed",
                "error": "remote_epoch_unavailable",
                **base,
            }
        remote_epoch = int(remote_epoch)
        if remote_epoch != ep:
            return {
                "ok": False,
                "status": "stale_epoch",
                "error": "EPOCH_MISMATCH",
                "remote_epoch": remote_epoch,
                **base,
            }
        if dry_run:
            return {"ok": True, "status": "dry_run", "remote_epoch": remote_epoch, **base}

        committed = ""
        if commit and repo is not None and repo.exists():
            _run_git(repo, ["add", "-A"], timeout_s=timeout_s)
            status_res = _run_git(repo, ["status", "--porcelain"], timeout_s=timeout_s)
            if status_res.returncode == 0 and status_res.stdout.strip():
                msg = _masked(commit_message or f"swarm checkpoint {oid} e{ep}", 200)
                commit_res = _run_git(
                    repo,
                    [
                        "-c",
                        "user.name=botex-edge",
                        "-c",
                        "user.email=botex-edge@localhost",
                        "commit",
                        "--allow-empty",
                        "-m",
                        msg,
                    ],
                    timeout_s=timeout_s,
                )
                if commit_res.returncode != 0:
                    return {
                        "ok": False,
                        "status": "commit_failed",
                        "error": _masked(commit_res.stderr or commit_res.stdout, 300),
                        **base,
                    }
                committed = (commit_res.stdout.strip().splitlines() or [""])[0]

        pfn = push_fn if push_fn is not None else self._push_fn
        if pfn is not None:
            try:
                pres = pfn(
                    repo_dir=str(repo or ""),
                    remote=rem,
                    branch=br,
                    order_id=oid,
                    epoch=ep,
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": _masked(str(exc), 300),
                    **base,
                }
            push_ok, push_err = _push_result_ok(pres)
            if not push_ok:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": push_err,
                    **base,
                }
        else:
            if repo is None:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": "repo_dir_required",
                    **base,
                }
            push_res = _run_git(
                repo,
                ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "push", rem, br],
                timeout_s=timeout_s,
            )
            if push_res.returncode != 0:
                return {
                    "ok": False,
                    "status": "push_failed",
                    "error": _masked(push_res.stderr or push_res.stdout, 300),
                    **base,
                }

        self.mark_checkpointed(br)
        return {
            "ok": True,
            "status": "pushed",
            "remote_epoch": remote_epoch,
            "commit": committed,
            "pushed_ref": f"{rem}/{br}",
            **base,
        }

    # -- anti-spiral ---------------------------------------------------------

    def build_anti_spiral_reset(
        self,
        order: Any = None,
        *,
        last_checkpoint_ref: str = "",
        blind_alley_note: str = "",
    ) -> Dict[str, Any]:
        """Clean next-hop context from the last checkpoint + masked note."""
        return build_anti_spiral_reset(
            self._order_id if order is None else order,
            last_checkpoint_ref=last_checkpoint_ref or self._last_checkpoint_ref,
            blind_alley_note=blind_alley_note,
            epoch=self._epoch,
        )


# ---------------------------------------------------------------------------
# Terminal verify hook (no_self_report_done)
# ---------------------------------------------------------------------------


def _set_order_status(order: Any, status: str) -> bool:
    try:
        if isinstance(order, dict):
            order["status"] = status
        else:
            order.status = status
        return True
    except Exception:
        return False


_TERMINAL_ORDER_STATUSES = frozenset({"DONE", "FAILED", "BLOCKED", "NEEDS_HUMAN", "FENCED"})


def _transition_order_status(
    order: Any,
    status: str,
    *,
    via_verify_gate: bool = False,
    reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Route ``order`` through the canonical supervisor state machine when possible.

    Returns the transition result dict for real ``WorkOrder`` instances, or
    ``None`` for dict/duck-typed orders and when ``botex.core.work_orders`` is
    not importable yet — adapters must work regardless of import order.
    """
    try:
        from botex.core.work_orders import WorkOrder, WorkOrderSupervisor
    except Exception:
        return None
    if not isinstance(order, WorkOrder):
        return None
    try:
        return WorkOrderSupervisor(audit=False).transition(
            order, status, reason=reason, via_verify_gate=via_verify_gate
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": str(getattr(order, "status", "") or ""),
            "reason": _masked(str(exc), 200),
        }


def _apply_order_status(
    order: Any,
    status: str,
    *,
    via_verify_gate: bool = False,
    reason: str = "",
) -> tuple[bool, str]:
    """Set ``order.status`` — canonical supervisor transition first, direct fallback.

    Returns ``(status_set, via)`` where ``via`` is ``"supervisor"``, ``"direct"``
    or ``"failed"``. A rejected state-machine edge on a non-terminal order falls
    back to a direct set: the external verify evidence already gates ``DONE``.
    """
    tr = _transition_order_status(order, status, via_verify_gate=via_verify_gate, reason=reason)
    if tr is not None:
        if tr.get("ok"):
            return True, "supervisor"
        if _set_order_status(order, status):
            return True, "direct_fallback"
        return False, "failed"
    if _set_order_status(order, status):
        return True, "direct"
    return False, "failed"


def _brief_result(res: Any) -> Dict[str, Any]:
    if res is None:
        return {"ok": True}
    if isinstance(res, Mapping):
        out: Dict[str, Any] = {"ok": bool(res.get("ok", True))}
        for key in ("status", "error", "reason"):
            if res.get(key):
                out[key] = str(res.get(key))
        return out
    return {"ok": True}


def _invoke_refresh(fn: Callable[..., Any], slug: str) -> Dict[str, Any]:
    res = fn(slug)
    if not inspect.isawaitable(res):
        return _brief_result(res)
    try:
        asyncio.get_running_loop()
    except RuntimeError:

        async def _wait(awaitable: Any) -> Any:
            return await awaitable

        return _brief_result(asyncio.run(_wait(res)))
    # Inside a running loop: schedule the coroutine; the DONE transition must
    # not block on Cloud I/O.
    task = asyncio.ensure_future(res)
    return {"ok": True, "scheduled": True, "task": task.get_name()}


def finalize_verified_work_order(
    order: Any,
    verify_result: Optional[Mapping[str, Any]] = None,
    *,
    bot_slug: str = "",
    refresh_brief_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Finalize ``order`` after external verification; ``DONE`` only on ``ok is True``.

    ``verify.kind == "none"`` can never auto-complete — the order lands in
    ``NEEDS_HUMAN`` regardless of the result payload. On success the order is
    marked ``DONE`` and the bot brief refresh is invoked (``refresh_brief_fn``
    or ``botex.ops.curator.brief_curator.refresh_bot_brief`` when ``bot_slug``
    is provided). Brief-refresh failures never revoke ``DONE``.
    """
    if order is None:
        return {"ok": False, "status": "BLOCKED", "reason": "missing_order"}
    order_id = _order_id(order)
    cur_status = str(_order_field(order, "status", "") or "").strip().upper()
    if cur_status in _TERMINAL_ORDER_STATUSES:
        return {
            "ok": cur_status == "DONE",
            "status": cur_status,
            "reason": "order_terminal",
            "order_id": order_id,
        }
    kind = _verify_kind(order)

    if kind == "none":
        status_set, status_via = _apply_order_status(
            order, "NEEDS_HUMAN", reason="verify_kind_none"
        )
        return {
            "ok": False,
            "status": "NEEDS_HUMAN",
            "reason": "verify_kind_none",
            "order_id": order_id,
            "verify_kind": kind,
            "status_set": status_set,
            "status_via": status_via,
        }

    res = verify_result if isinstance(verify_result, Mapping) else {}
    if res.get("ok") is not True:
        return {
            "ok": False,
            "status": str(res.get("status") or res.get("verdict") or "VERIFY_FAILED"),
            "reason": _masked(res.get("reason") or "", 300),
            "order_id": order_id,
            "verify_kind": kind,
        }

    status_set, status_via = _apply_order_status(
        order, "DONE", via_verify_gate=True, reason="external_verify_passed"
    )

    brief: Dict[str, Any] = {"skipped": True}
    target_fn = refresh_brief_fn
    if target_fn is None and str(bot_slug or "").strip():
        try:
            from botex.ops.curator.brief_curator import refresh_bot_brief as target_fn
        except Exception as exc:
            target_fn = None
            brief = {
                "ok": False,
                "error": _masked(f"brief_curator_import_failed: {exc}", 300),
            }
    if target_fn is not None:
        try:
            brief = _invoke_refresh(target_fn, str(bot_slug or "").strip())
        except Exception as exc:
            brief = {"ok": False, "error": _masked(str(exc), 300)}

    return {
        "ok": True,
        "status": "DONE",
        "order_id": order_id,
        "verify_kind": kind,
        "status_set": status_set,
        "status_via": status_via,
        "brief_refresh": brief,
    }

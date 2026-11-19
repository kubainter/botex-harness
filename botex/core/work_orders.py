# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Cross-Harness WorkOrder Supervisor (Plan 038 / ADR-SWARM-02 Phase 2b)
=====================================================================
Canonical ``WorkOrder`` state machine, signed ``HandoffPolicy``,
unit-separated ``CostLedger``, deterministic failure-signature dedup and a
cost/headroom ranker for authorized failover between ``botex`` / ``zcode`` /
``devin`` harnesses.

Invariants (Global Rule 1 / ``no_self_report_done``):
- Missing or unsigned ``handoff_policy`` -> diagnostics only; any failure
  lands on BLOCKED with zero automatic cross-harness failover.
- ``DONE`` is unreachable through :meth:`WorkOrderSupervisor.transition`;
  only :meth:`WorkOrderSupervisor.apply_verify_result` with an external
  ``verify_gate`` result (``{"ok": True}``) closes a WorkOrder, and
  ``verify.kind == "none"`` always lands on NEEDS_HUMAN.
- ``.agent_analytics.jsonl`` is a read-only input for ranking; supervisor
  decisions audit to ``supervisor_ledger.jsonl`` via
  ``botex.edge.verify_gate.append_supervisor_ledger`` only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from botex.tools.security import coerce_text, mask_secrets
except (ImportError, ValueError):  # pragma: no cover - legacy flat import fallback
    from security import coerce_text, mask_secrets

try:
    from botex.core.meta_harness import is_infra_failure
except (ImportError, ValueError):  # pragma: no cover - legacy flat import fallback
    from meta_harness import is_infra_failure

_LOG = logging.getLogger("botex.core.work_orders")

__all__ = [
    "ANTIGRAVITY_EXCLUDED_HARNESSES",
    "ANTIGRAVITY_EXCLUSION_REASON",
    "AttemptRecord",
    "CostLedger",
    "DEFAULT_OLLAMA_SUMMARY_MODEL",
    "HandoffPolicy",
    "TERMINAL_STATUSES",
    "VerifySpec",
    "WorkOrder",
    "WorkOrderStatus",
    "WorkOrderSupervisor",
    "can_transition",
    "compute_failure_signature",
]


class WorkOrderStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    STUCK = "STUCK"
    TRIAGED = "TRIAGED"
    HANDOFF = "HANDOFF"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FENCED = "FENCED"


ALL_STATUSES = frozenset(
    v for k, v in vars(WorkOrderStatus).items() if not k.startswith("_") and isinstance(v, str)
)

# Status normalization resolves through a module-constant map (CodeQL
# CWE-020): never persist a caller-mutated status string.
_STATUS_MAP: Dict[str, str] = {v: v for v in ALL_STATUSES}

TERMINAL_STATUSES = frozenset(
    {
        WorkOrderStatus.DONE,
        WorkOrderStatus.FAILED,
        WorkOrderStatus.BLOCKED,
        WorkOrderStatus.NEEDS_HUMAN,
        WorkOrderStatus.FENCED,
    }
)

_NONTERMINAL_STATUSES = ALL_STATUSES - TERMINAL_STATUSES

_MACHINE_EDGES = frozenset(
    {
        ("QUEUED", "RUNNING"),
        ("QUEUED", "STUCK"),
        ("QUEUED", "BLOCKED"),
        ("QUEUED", "NEEDS_HUMAN"),
        ("RUNNING", "STUCK"),
        ("RUNNING", "VERIFYING"),
        ("RUNNING", "FAILED"),
        ("RUNNING", "BLOCKED"),
        ("RUNNING", "NEEDS_HUMAN"),
        ("STUCK", "TRIAGED"),
        ("STUCK", "BLOCKED"),
        ("STUCK", "NEEDS_HUMAN"),
        ("TRIAGED", "HANDOFF"),
        ("TRIAGED", "RUNNING"),
        ("TRIAGED", "BLOCKED"),
        ("TRIAGED", "NEEDS_HUMAN"),
        ("HANDOFF", "RUNNING"),
        ("HANDOFF", "STUCK"),
        ("HANDOFF", "VERIFYING"),
        ("HANDOFF", "BLOCKED"),
        ("HANDOFF", "NEEDS_HUMAN"),
        ("VERIFYING", "DONE"),
        ("VERIFYING", "STUCK"),
        ("VERIFYING", "FAILED"),
        ("VERIFYING", "BLOCKED"),
        ("VERIFYING", "NEEDS_HUMAN"),
    }
    | {(s, "FENCED") for s in _NONTERMINAL_STATUSES}
)


def _norm_status(value: Any) -> str:
    norm = str(value or "").strip().upper()
    return _STATUS_MAP.get(norm, "")


def can_transition(current: str, target: str) -> bool:
    cur = _norm_status(current)
    tgt = _norm_status(target)
    if not cur or not tgt or cur in TERMINAL_STATUSES:
        return False
    return (cur, tgt) in _MACHINE_EDGES


_VERIFY_KINDS: Dict[str, str] = {
    "command": "command",
    "acceptance_review": "acceptance_review",
    "none": "none",
}


@dataclass
class VerifySpec:
    kind: str = "command"
    command: str = ""
    allow_test_mutation: bool = False
    timeout_s: float = 300.0

    def __post_init__(self) -> None:
        # Unknown kinds degrade to "none": fail-closed means a human reviews
        # rather than a typo silently executing or auto-completing.
        norm = str(self.kind or "").strip().lower()
        self.kind = _VERIFY_KINDS.get(norm, "none")
        self.command = str(self.command or "")
        self.allow_test_mutation = bool(self.allow_test_mutation)
        try:
            self.timeout_s = max(0.0, float(self.timeout_s))
        except (TypeError, ValueError):
            self.timeout_s = 300.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "command": self.command,
            "allow_test_mutation": self.allow_test_mutation,
            "timeout_s": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "VerifySpec":
        d = data if isinstance(data, Mapping) else {}
        return cls(
            kind=str(d.get("kind", "command")),
            command=str(d.get("command", "")),
            allow_test_mutation=bool(d.get("allow_test_mutation", False)),
            timeout_s=d.get("timeout_s", 300.0),
        )


@dataclass
class HandoffPolicy:
    allowed_harnesses: Tuple[str, ...] = ()
    max_hops: int = 2
    signed_by: str = ""
    allow_paid: bool = False
    paid_allowlist: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.allowed_harnesses = tuple(
            str(h).strip().lower() for h in self.allowed_harnesses if str(h or "").strip()
        )
        self.paid_allowlist = tuple(
            str(m).strip().lower() for m in self.paid_allowlist if str(m or "").strip()
        )
        self.signed_by = str(self.signed_by or "")
        self.allow_paid = bool(self.allow_paid)
        try:
            self.max_hops = max(0, int(self.max_hops))
        except (TypeError, ValueError):
            self.max_hops = 2

    def is_authorized(self) -> bool:
        return bool(self.signed_by.strip()) and bool(self.allowed_harnesses)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_harnesses": list(self.allowed_harnesses),
            "max_hops": self.max_hops,
            "signed_by": self.signed_by,
            "allow_paid": self.allow_paid,
            "paid_allowlist": list(self.paid_allowlist),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "HandoffPolicy":
        d = data if isinstance(data, Mapping) else {}
        return cls(
            allowed_harnesses=tuple(d.get("allowed_harnesses") or ()),
            max_hops=d.get("max_hops", 2),
            signed_by=str(d.get("signed_by", "")),
            allow_paid=bool(d.get("allow_paid", False)),
            paid_allowlist=tuple(d.get("paid_allowlist") or ()),
        )


def _pos_num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0.0 else 0.0


@dataclass
class CostLedger:
    """Strict unit separation: ``usd`` (botex), ``acu`` (devin),
    ``quota_units``/``turns`` (zcode/devin). Never cross-accumulate."""

    usd: float = 0.0
    acu: float = 0.0
    quota_units: float = 0.0
    turns: int = 0

    @staticmethod
    def check_provider_allowed(
        provider: str = "",
        model: str = "",
        *,
        allow_paid: bool = False,
        paid_allowlist: Sequence[str] = (),
    ) -> Dict[str, Any]:
        prov = str(provider or "").strip().lower()
        mod = str(model or "").strip().lower()
        if prov == "openrouter" and not mod.endswith(":free"):
            allowlist = {str(m).strip().lower() for m in paid_allowlist if str(m or "").strip()}
            if not (allow_paid and mod in allowlist):
                return {
                    "ok": False,
                    "status": "FORBIDDEN",
                    "reason": "paid_openrouter_forbidden",
                }
        return {"ok": True, "status": "ALLOWED", "reason": ""}

    def record_spend(
        self,
        harness: str,
        *,
        usd: float = 0.0,
        acu: float = 0.0,
        quota_units: float = 0.0,
        turns: int = 0,
        provider: str = "",
        model: str = "",
        allow_paid: bool = False,
        paid_allowlist: Sequence[str] = (),
    ) -> Dict[str, Any]:
        gate = self.check_provider_allowed(
            provider, model, allow_paid=allow_paid, paid_allowlist=paid_allowlist
        )
        if not gate["ok"]:
            gate["harness"] = str(harness or "")
            return gate
        self.usd += _pos_num(usd)
        self.acu += _pos_num(acu)
        self.quota_units += _pos_num(quota_units)
        try:
            t = int(turns)
        except (TypeError, ValueError):
            t = 0
        if t > 0:
            self.turns += t
        return {
            "ok": True,
            "status": "RECORDED",
            "reason": "",
            "harness": str(harness or ""),
            "ledger": self.to_dict(),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "usd": self.usd,
            "acu": self.acu,
            "quota_units": self.quota_units,
            "turns": self.turns,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CostLedger":
        d = data if isinstance(data, Mapping) else {}
        return cls(
            usd=_pos_num(d.get("usd")),
            acu=_pos_num(d.get("acu")),
            quota_units=_pos_num(d.get("quota_units")),
            turns=int(_pos_num(d.get("turns"))),
        )


@dataclass
class AttemptRecord:
    hop: int = 0
    harness: str = ""
    epoch: int = 0
    status: str = ""
    failure_reason: str = ""
    signature: str = ""
    checkpoint_ref: str = ""
    blind_alley_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hop": self.hop,
            "harness": self.harness,
            "epoch": self.epoch,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "signature": self.signature,
            "checkpoint_ref": self.checkpoint_ref,
            "blind_alley_note": self.blind_alley_note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AttemptRecord":
        d = data if isinstance(data, Mapping) else {}
        try:
            hop = int(d.get("hop", 0))
        except (TypeError, ValueError):
            hop = 0
        try:
            epoch = int(d.get("epoch", 0))
        except (TypeError, ValueError):
            epoch = 0
        return cls(
            hop=hop,
            harness=str(d.get("harness", "")),
            epoch=epoch,
            status=str(d.get("status", "")),
            failure_reason=str(d.get("failure_reason", "")),
            signature=str(d.get("signature", "")),
            checkpoint_ref=str(d.get("checkpoint_ref", "")),
            blind_alley_note=str(d.get("blind_alley_note", "")),
        )


@dataclass
class WorkOrder:
    order_id: str = ""
    goal: str = ""
    origin_repo: str = ""
    base_ref: str = "HEAD"
    scope: Tuple[str, ...] = ()
    mode_request: str = "edit"
    verify: VerifySpec = field(default_factory=VerifySpec)
    handoff_policy: Optional[HandoffPolicy] = None
    budget: Optional[CostLedger] = None
    epoch: int = 0
    status: str = WorkOrderStatus.QUEUED
    attempt_ledger: List[AttemptRecord] = field(default_factory=list)
    blocking_reason: str = ""

    def __post_init__(self) -> None:
        self.order_id = str(self.order_id or "")
        self.goal = str(self.goal or "")
        self.origin_repo = str(self.origin_repo or "")
        self.base_ref = str(self.base_ref or "HEAD")
        self.scope = tuple(str(s) for s in self.scope if str(s or ""))
        self.mode_request = str(self.mode_request or "edit")
        if not isinstance(self.verify, VerifySpec):
            self.verify = VerifySpec.from_dict(self.verify)
        if self.handoff_policy is not None and not isinstance(self.handoff_policy, HandoffPolicy):
            self.handoff_policy = HandoffPolicy.from_dict(self.handoff_policy)
        if self.budget is not None and not isinstance(self.budget, CostLedger):
            self.budget = CostLedger.from_dict(self.budget)
        try:
            self.epoch = int(self.epoch)
        except (TypeError, ValueError):
            self.epoch = 0
        resolved = _norm_status(self.status)
        if resolved:
            self.status = resolved
        else:
            blank = not str(self.status or "").strip()
            self.status = WorkOrderStatus.QUEUED if blank else WorkOrderStatus.BLOCKED
            if not blank:
                self.blocking_reason = "unknown_status"
        self.attempt_ledger = [
            r if isinstance(r, AttemptRecord) else AttemptRecord.from_dict(r)
            for r in (self.attempt_ledger or [])
        ]
        self.blocking_reason = str(self.blocking_reason or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "goal": self.goal,
            "origin_repo": self.origin_repo,
            "base_ref": self.base_ref,
            "scope": list(self.scope),
            "mode_request": self.mode_request,
            "verify": self.verify.to_dict() if self.verify else None,
            "handoff_policy": self.handoff_policy.to_dict() if self.handoff_policy else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "epoch": self.epoch,
            "status": self.status,
            "attempt_ledger": [r.to_dict() for r in self.attempt_ledger],
            "blocking_reason": self.blocking_reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WorkOrder":
        d = data if isinstance(data, Mapping) else {}
        hp = d.get("handoff_policy")
        budget = d.get("budget")
        return cls(
            order_id=str(d.get("order_id", "")),
            goal=str(d.get("goal", "")),
            origin_repo=str(d.get("origin_repo", "")),
            base_ref=str(d.get("base_ref", "HEAD")),
            scope=tuple(d.get("scope") or ()),
            mode_request=str(d.get("mode_request", "edit")),
            verify=VerifySpec.from_dict(d.get("verify")),
            handoff_policy=HandoffPolicy.from_dict(hp) if isinstance(hp, Mapping) else None,
            budget=CostLedger.from_dict(budget) if isinstance(budget, Mapping) else None,
            epoch=d.get("epoch", 0),
            status=str(d.get("status", "QUEUED")),
            attempt_ledger=[
                AttemptRecord.from_dict(r)
                for r in (d.get("attempt_ledger") or [])
                if isinstance(r, Mapping)
            ],
            blocking_reason=str(d.get("blocking_reason", "")),
        )


ANTIGRAVITY_EXCLUDED_HARNESSES = frozenset({"antigravity_ls", "antigravity_ls_mcp", "antigravity"})
ANTIGRAVITY_EXCLUSION_REASON = "missing read-status API in L0"
DEFAULT_OLLAMA_SUMMARY_MODEL = "SpeakLeash/bielik-11b-v3.0-instruct:Q4_K_M"
OLLAMA_SUMMARY_TIMEOUT_S = 2.5

# Volatile substrings are scrubbed so identical failures on different
# harnesses collide on one signature while distinct failure classes do not.
_SIG_ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[tT ][0-9:.]+[zZ]?\b")
_SIG_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b")
_SIG_UUID_HEX_RE = re.compile(r"\b[0-9a-f]{8,}(?:-[0-9a-f]{4,})*\b")
_SIG_PATH_RE = re.compile(r"(?:[a-z]:)?(?:[\\/][\w.\-~]+){2,}")
_SIG_DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|minutes|h|hours)\b"
)
_SIG_LONG_NUM_RE = re.compile(r"\b\d{4,}\b")
_SIG_WS_RE = re.compile(r"\s+")
_SIG_KIND_WS_RE = re.compile(r"[\s\-]+")


def _scrub_signature_text(error_text: str) -> str:
    text = mask_secrets(str(error_text or "")).lower()
    for pattern in (
        _SIG_ISO_TS_RE,
        _SIG_CLOCK_RE,
        _SIG_UUID_HEX_RE,
        _SIG_PATH_RE,
        _SIG_DURATION_RE,
        _SIG_LONG_NUM_RE,
    ):
        text = pattern.sub("#", text)
    return _SIG_WS_RE.sub(" ", text).strip()


def compute_failure_signature(failure_kind: str, error_text: str) -> str:
    """Deterministic SHA-256 over normalized ``failure_kind`` + scrubbed error text."""
    kind = _SIG_KIND_WS_RE.sub("_", str(failure_kind or "").strip().lower())
    clean = _scrub_signature_text(error_text)
    return hashlib.sha256(f"{kind}\x00{clean}".encode("utf-8")).hexdigest()


_FAILURE_CLASS_MAP: Dict[str, str] = {
    "rate_limit": "rate_limit",
    "rate_limited": "rate_limit",
    "http_429": "rate_limit",
    "429": "rate_limit",
    "quota": "quota",
    "quota_exceeded": "quota",
    "budget_exceeded": "quota",
    "budget_or_price_block": "quota",
    "price_exceeded": "quota",
    "timeout": "timeout",
    "request_timeout": "timeout",
    "timed_out": "timeout",
    "time_limit": "timeout",
    "auth": "auth",
    "auth_error": "auth",
    "unauthorized": "auth",
    "zdr_violation": "auth",
    "connection_error": "provider_unavailable",
    "provider_unavailable": "provider_unavailable",
    "provider_error": "provider_unavailable",
    "provider_policy_denied": "provider_unavailable",
    "api_error": "provider_unavailable",
    "spiral": "spiral",
    "spiral_warning": "spiral",
    "stagnant_rollback": "spiral",
    "stagnation": "spiral",
    "turns_exhausted": "turns_exhausted",
    "max_turns": "turns_exhausted",
    "max_turns_reached": "turns_exhausted",
    "token_limit": "turns_exhausted",
    "contract_gate": "deterministic",
    "verification_failed": "deterministic",
    "syntax_rejection": "deterministic",
    "unsupported": "deterministic",
    "blocked": "deterministic",
    "done_without_write": "deterministic",
}

_FAILURE_KIND_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("rate_limit", "rate_limit"),
    ("429", "rate_limit"),
    ("quota", "quota"),
    ("budget", "quota"),
    ("price", "quota"),
    ("timeout", "timeout"),
    ("timed_out", "timeout"),
    ("auth", "auth"),
    ("connection", "provider_unavailable"),
    ("provider", "provider_unavailable"),
    ("spiral", "spiral"),
    ("stagnant", "spiral"),
    ("turns", "turns_exhausted"),
    ("token_limit", "turns_exhausted"),
    ("contract", "deterministic"),
    ("verif", "deterministic"),
    ("syntax", "deterministic"),
)

_FAILURE_TEXT_RULES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b429\b|rate.?limit", re.I), "rate_limit"),
    (re.compile(r"quota|budget", re.I), "quota"),
    (re.compile(r"timed?\s*out|timeout|deadline", re.I), "timeout"),
    (re.compile(r"unauthorized|forbidden|api.?key|auth", re.I), "auth"),
    (re.compile(r"connection (?:error|refused|reset)|econn", re.I), "provider_unavailable"),
    (re.compile(r"spiral|stagnant|no.progress", re.I), "spiral"),
    (re.compile(r"max.?turns|token.?limit", re.I), "turns_exhausted"),
)

# The Ollama log summarizer is only consulted for these classes (spec:
# spiral / turns_exhausted) and only as a blind-alley summarizer.
_OLLAMA_SUMMARY_CLASSES = frozenset({"spiral", "turns_exhausted"})


def _classify_failure(failure_kind: str, error_text: str) -> str:
    """Deterministic failure classification; ``"unknown"`` when nothing matches."""
    norm = _SIG_KIND_WS_RE.sub("_", str(failure_kind or "").strip().lower())
    if norm:
        direct = _FAILURE_CLASS_MAP.get(norm)
        if direct:
            return direct
        for marker, klass in _FAILURE_KIND_MARKERS:
            if marker in norm:
                return klass
    text = str(error_text or "")
    if text:
        for pattern, klass in _FAILURE_TEXT_RULES:
            if pattern.search(text):
                return klass
    return "unknown"


def default_analytics_ledger_path() -> Path:
    """Read-only ``.agent_analytics.jsonl`` location (mirrors providers/analytics)."""
    env = os.environ.get("BOTEX_ANALYTICS_FILE", "").strip()
    if env:
        return Path(env).expanduser().with_suffix(".jsonl")
    return Path.cwd() / ".agent_analytics.jsonl"


class WorkOrderSupervisor:
    """Fail-stop WorkOrder state machine + authorized cross-harness failover.

    All external inputs are injectable for hermetic tests: ``analytics_path``
    (read-only JSONL ledger), ``rate_limiter`` (RPM/TPM headroom source),
    ``ollama`` (``False`` disables, mapping overrides ``providers.ollama``),
    and ``ledger_path`` (supervisor_ledger.jsonl audit sink).
    """

    def __init__(
        self,
        *,
        analytics_path: Optional[Path | str] = None,
        rate_limiter: Any = None,
        ollama: Any = None,
        ledger_path: Optional[Path | str] = None,
        audit: bool = True,
    ) -> None:
        self._analytics_path = Path(analytics_path) if analytics_path else None
        self._rate_limiter = rate_limiter
        self._ollama_cfg_src = ollama
        self._ollama_cfg: Optional[Dict[str, Any]] = None
        self._ledger_path = ledger_path
        self._audit_enabled = bool(audit)

    # -- state machine ------------------------------------------------------

    def transition(
        self,
        order: WorkOrder,
        target: str,
        *,
        reason: str = "",
        via_verify_gate: bool = False,
    ) -> Dict[str, Any]:
        resolved = _norm_status(target)
        if not resolved:
            return {"ok": False, "status": order.status, "reason": "unknown_status"}
        if order.status in TERMINAL_STATUSES:
            return {"ok": False, "status": order.status, "reason": "order_terminal"}
        if resolved == WorkOrderStatus.DONE and not via_verify_gate:
            return {
                "ok": False,
                "status": order.status,
                "reason": "done_requires_verify_gate",
            }
        if (order.status, resolved) not in _MACHINE_EDGES:
            return {
                "ok": False,
                "status": order.status,
                "reason": "illegal_transition",
                "from": order.status,
                "to": resolved,
            }
        order.status = resolved
        if resolved == WorkOrderStatus.BLOCKED and reason:
            order.blocking_reason = str(reason)
        return {"ok": True, "status": resolved, "reason": str(reason or "")}

    def begin_attempt(self, order: WorkOrder, harness: str = "") -> Dict[str, Any]:
        return self.transition(
            order, WorkOrderStatus.RUNNING, reason=f"harness:{str(harness or '')}"
        )

    def fence_order(self, order: WorkOrder, *, reason: str = "lease_expired") -> Dict[str, Any]:
        out = self.transition(order, WorkOrderStatus.FENCED, reason=reason)
        if out["ok"]:
            self._audit(order, "fenced", reason=reason)
        return out

    # -- attempt ledger -----------------------------------------------------

    def record_attempt(
        self,
        order: WorkOrder,
        harness: str,
        *,
        status: str = "",
        failure_kind: str = "",
        error_text: str = "",
        signature: str = "",
        checkpoint_ref: str = "",
        blind_alley_note: str = "",
    ) -> AttemptRecord:
        reason_text = " ".join(
            part for part in (str(failure_kind or ""), str(error_text or "")) if part
        )
        sig = signature or (
            compute_failure_signature(failure_kind, error_text)
            if (failure_kind or error_text)
            else ""
        )
        record = AttemptRecord(
            hop=len(order.attempt_ledger),
            harness=str(harness or "").strip().lower(),
            epoch=order.epoch,
            status=str(status or ""),
            failure_reason=coerce_text(mask_secrets(reason_text))[:400],
            signature=sig,
            checkpoint_ref=str(checkpoint_ref or ""),
            blind_alley_note=coerce_text(mask_secrets(str(blind_alley_note or "")))[:800],
        )
        order.attempt_ledger.append(record)
        return record

    # -- failure triage & handoff decision ----------------------------------

    def handle_failure(
        self,
        order: WorkOrder,
        harness: str,
        *,
        failure_kind: str = "",
        error_text: str = "",
        checkpoint_ref: str = "",
        blind_alley_note: str = "",
    ) -> Dict[str, Any]:
        if order.status in TERMINAL_STATUSES:
            return {"ok": False, "status": order.status, "reason": "order_terminal"}

        record = self.record_attempt(
            order,
            harness,
            status="failed",
            failure_kind=failure_kind,
            error_text=error_text,
            checkpoint_ref=checkpoint_ref,
            blind_alley_note=blind_alley_note,
        )
        if order.status in (
            WorkOrderStatus.QUEUED,
            WorkOrderStatus.RUNNING,
            WorkOrderStatus.HANDOFF,
            WorkOrderStatus.VERIFYING,
        ):
            self.transition(order, WorkOrderStatus.STUCK, reason="harness_failure")

        policy = order.handoff_policy
        if policy is None or not policy.is_authorized():
            triage = self._triage(failure_kind, error_text)
            infra = None
            try:
                infra = bool(is_infra_failure(failure_kind, error_text))
            except Exception:  # diagnostics must never break the fail-stop path
                pass
            diagnostics = {
                "signature": record.signature,
                "failure_class": triage["failure_class"],
                "infra": infra,
                "attempted_harnesses": sorted(
                    {r.harness for r in order.attempt_ledger if r.harness}
                ),
                "policy_present": policy is not None,
                "policy_authorized": bool(policy and policy.is_authorized()),
            }
            out = self.transition(order, WorkOrderStatus.BLOCKED, reason="missing_handoff_policy")
            self._audit(order, "blocked", reason="missing_handoff_policy")
            return {
                "ok": False,
                "status": out.get("status", order.status),
                "reason": "missing_handoff_policy",
                "diagnostics": diagnostics,
            }

        sig = record.signature
        if sig:
            sig_harnesses = {
                r.harness for r in order.attempt_ledger if r.signature == sig and r.harness
            }
            if len(sig_harnesses) >= 2:
                out = self.transition(
                    order, WorkOrderStatus.BLOCKED, reason="duplicate_failure_signature"
                )
                self._audit(
                    order,
                    "blocked",
                    reason="duplicate_failure_signature",
                    signature=sig,
                )
                return {
                    "ok": False,
                    "status": out.get("status", order.status),
                    "reason": "duplicate_failure_signature",
                    "signature": sig,
                }

        # ``max_hops`` counts failover hops: the origin attempt is hop 0, so an
        # order may spend at most ``max_hops`` handoffs (attempts <= max_hops).
        if len(order.attempt_ledger) > policy.max_hops:
            out = self.transition(order, WorkOrderStatus.BLOCKED, reason="max_hops_exceeded")
            self._audit(order, "blocked", reason="max_hops_exceeded")
            return {
                "ok": False,
                "status": out.get("status", order.status),
                "reason": "max_hops_exceeded",
            }

        triage = self._triage(failure_kind, error_text)
        if triage["failure_class"] == "unknown":
            out = self.transition(order, WorkOrderStatus.BLOCKED, reason="unclassified_failure")
            self._audit(order, "blocked", reason="unclassified_failure")
            return {
                "ok": False,
                "status": out.get("status", order.status),
                "reason": "unclassified_failure",
                "confidence": triage["confidence"],
            }

        ranking = self.rank_candidate_harnesses(
            order=order, failure_kind=failure_kind, error_text=error_text
        )
        if not ranking["ranked"]:
            out = self.transition(order, WorkOrderStatus.BLOCKED, reason="no_eligible_harness")
            self._audit(order, "blocked", reason="no_eligible_harness")
            return {
                "ok": False,
                "status": out.get("status", order.status),
                "reason": "no_eligible_harness",
                "excluded": ranking["excluded"],
                "confidence": triage["confidence"],
            }

        if order.status == WorkOrderStatus.STUCK:
            self.transition(order, WorkOrderStatus.TRIAGED, reason=triage["failure_class"])
        target = ranking["ranked"][0]
        out = self.transition(order, WorkOrderStatus.HANDOFF, reason=target)
        if not out["ok"]:
            return out
        order.epoch += 1
        self._audit(order, "handoff", target_harness=target, epoch=order.epoch)
        return {
            "ok": True,
            "status": WorkOrderStatus.HANDOFF,
            "target_harness": target,
            "epoch": order.epoch,
            "next_hop": len(order.attempt_ledger),
            "ranked": ranking["ranked"],
            "excluded": ranking["excluded"],
            "triage": triage,
            "confidence": triage["confidence"],
            "blind_alley_note": triage["summary"] or blind_alley_note,
        }

    # -- verification gate ----------------------------------------------------

    def begin_verify(self, order: WorkOrder) -> Dict[str, Any]:
        if order.status in TERMINAL_STATUSES:
            return {"ok": False, "status": order.status, "reason": "order_terminal"}
        if self._verify_kind(order) == "none":
            out = self.transition(
                order, WorkOrderStatus.NEEDS_HUMAN, reason="verify_none_requires_human"
            )
            self._audit(order, "verify_none", verdict="NEEDS_HUMAN")
            return out
        return self.transition(order, WorkOrderStatus.VERIFYING, reason="awaiting_external_verify")

    def apply_verify_result(self, order: WorkOrder, verify_result: Any) -> Dict[str, Any]:
        vr = verify_result if isinstance(verify_result, Mapping) else {}
        if order.status in TERMINAL_STATUSES:
            return {"ok": False, "status": order.status, "reason": "order_terminal"}
        if order.status != WorkOrderStatus.VERIFYING:
            return {"ok": False, "status": order.status, "reason": "not_in_verifying"}
        verdict = str(vr.get("status") or vr.get("verdict") or "").upper()
        if self._verify_kind(order) == "none":
            out = self.transition(
                order, WorkOrderStatus.NEEDS_HUMAN, reason="verify_none_requires_human"
            )
            self._audit(order, "verify_none", verdict=verdict or "NEEDS_HUMAN")
            return out
        if vr.get("ok") is True:
            out = self.transition(
                order,
                WorkOrderStatus.DONE,
                reason="external_verify_passed",
                via_verify_gate=True,
            )
            self._audit(order, "done", verdict=verdict or "DONE")
            return out
        if verdict == "NEEDS_HUMAN":
            out = self.transition(
                order,
                WorkOrderStatus.NEEDS_HUMAN,
                reason=str(vr.get("reason") or "verify_needs_human"),
            )
            self._audit(order, "needs_human", verdict=verdict)
            return out
        out = self.transition(
            order,
            WorkOrderStatus.FAILED,
            reason="verify_failed:" + str(vr.get("reason") or verdict or "unknown"),
        )
        self._audit(order, "failed", verdict=verdict or "FAILED")
        return out

    @staticmethod
    def _verify_kind(order: WorkOrder) -> str:
        kind = getattr(getattr(order, "verify", None), "kind", "command")
        norm = str(kind or "").strip().lower()
        return _VERIFY_KINDS.get(norm, "none")

    # -- spend ledger ---------------------------------------------------------

    def record_spend(self, order: WorkOrder, harness: str, **kwargs: Any) -> Dict[str, Any]:
        ledger = order.budget if isinstance(order.budget, CostLedger) else None
        if ledger is None:
            ledger = CostLedger()
            order.budget = ledger
        policy = order.handoff_policy
        # The signed policy is authoritative: callers cannot widen paid consent.
        kwargs["allow_paid"] = bool(policy and policy.allow_paid)
        kwargs["paid_allowlist"] = (
            tuple(getattr(policy, "paid_allowlist", ()) or ()) if policy else ()
        )
        res = ledger.record_spend(harness, **kwargs)
        if not res.get("ok"):
            self._audit(
                order,
                "spend_forbidden",
                harness=str(harness or ""),
                provider=str(kwargs.get("provider") or ""),
                model=str(kwargs.get("model") or ""),
            )
        return res

    # -- candidate ranking -----------------------------------------------------

    def rank_candidate_harnesses(
        self,
        candidates: Optional[Sequence[str]] = None,
        *,
        order: Optional[WorkOrder] = None,
        harness_providers: Optional[Mapping[str, Sequence[str]]] = None,
        failure_kind: str = "",
        error_text: str = "",
    ) -> Dict[str, Any]:
        policy = order.handoff_policy if order is not None else None
        raw: Sequence[str] = (
            candidates
            if candidates is not None
            else tuple(getattr(policy, "allowed_harnesses", ()) or ())
        )
        attempted = {r.harness for r in getattr(order, "attempt_ledger", [])}
        eligible: List[str] = []
        excluded: List[Dict[str, str]] = []
        for entry in raw:
            name = str(entry or "").strip().lower()
            if not name:
                continue
            if name in ANTIGRAVITY_EXCLUDED_HARNESSES:
                excluded.append({"harness": name, "reason": ANTIGRAVITY_EXCLUSION_REASON})
            elif name in attempted:
                excluded.append({"harness": name, "reason": "already_attempted"})
            else:
                eligible.append(name)

        stats = self._provider_stats()
        provider_map = harness_providers or {}
        detail: List[Dict[str, Any]] = []
        for idx, name in enumerate(eligible):
            provs = [
                str(p).strip().lower()
                for p in provider_map.get(name, (name,))
                if str(p or "").strip()
            ]
            done = total = 0
            cost_sum = 0.0
            cost_n = 0
            for prov in provs:
                st = stats.get(prov)
                if st:
                    done += st["done"]
                    total += st["total"]
                    cost_sum += st["cost_sum"]
                    cost_n += st["n"]
            success_rate = (done / total) if total else 0.5
            headroom = min((self._headroom_s(p) for p in provs), default=0.0)
            detail.append(
                {
                    "harness": name,
                    "providers": provs,
                    "success_rate": round(success_rate, 6),
                    "avg_cost_usd": round(cost_sum / cost_n, 6) if cost_n else 0.0,
                    "headroom_s": round(headroom, 3),
                    "samples": total,
                    "_idx": idx,
                }
            )
        ranked = sorted(
            detail,
            key=lambda d: (
                -d["success_rate"],
                d["headroom_s"],
                d["avg_cost_usd"],
                d["_idx"],
            ),
        )
        for item in ranked:
            item.pop("_idx", None)
        triage = self._triage(failure_kind, error_text)
        return {
            "ok": True,
            "ranked": [d["harness"] for d in ranked],
            "candidates": ranked,
            "excluded": excluded,
            "triage": triage,
            "confidence": triage["confidence"],
            "analytics_path": str(self._analytics_file()),
        }

    # -- internals --------------------------------------------------------------

    def _triage(self, failure_kind: str, error_text: str) -> Dict[str, Any]:
        klass = _classify_failure(failure_kind, error_text)
        triage: Dict[str, Any] = {
            "failure_class": klass,
            "confidence": "high",
            "ollama": "skipped",
            "summary": "",
        }
        if klass in _OLLAMA_SUMMARY_CLASSES:
            summary = self._ollama_summarize(error_text)
            if summary:
                triage["ollama"] = "ok"
                triage["summary"] = summary
            else:
                triage["ollama"] = "offline" if self._ollama_config() else "disabled"
                triage["confidence"] = "low"
        elif klass == "unknown":
            triage["confidence"] = "low"
        return triage

    def _analytics_file(self) -> Path:
        return self._analytics_path or default_analytics_ledger_path()

    def _provider_stats(self) -> Dict[str, Dict[str, Any]]:
        path = self._analytics_file()
        stats: Dict[str, Dict[str, Any]] = {}
        try:
            fh = path.open("r", encoding="utf-8")
        except OSError:
            return stats
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                prov = str(rec.get("provider") or "").strip().lower()
                if not prov:
                    continue
                bucket = stats.setdefault(prov, {"done": 0, "total": 0, "cost_sum": 0.0, "n": 0})
                bucket["total"] += 1
                bucket["n"] += 1
                if str(rec.get("status") or "").upper() == "DONE" and not rec.get("failure_kind"):
                    bucket["done"] += 1
                bucket["cost_sum"] += _pos_num(rec.get("cost_usd"))
        return stats

    def _headroom_s(self, provider: str) -> float:
        limiter = self._rate_limiter
        if limiter is None:
            return 0.0
        try:
            return max(0.0, float(limiter.wait_estimate(provider)))
        except Exception:
            return 0.0

    def _ollama_config(self) -> Dict[str, Any]:
        if self._ollama_cfg is not None:
            return self._ollama_cfg
        src = self._ollama_cfg_src
        if src is None:
            try:
                from botex.server.config import load_config
            except (ImportError, ValueError):  # pragma: no cover
                try:
                    from config import load_config
                except (ImportError, ValueError):
                    load_config = None  # type: ignore[assignment]
            providers: Dict[str, Any] = {}
            if load_config is not None:
                try:
                    providers = (load_config() or {}).get("providers", {})
                except Exception:
                    providers = {}
            src = providers.get("ollama", {}) if isinstance(providers, dict) else {}
        if src is False or (isinstance(src, Mapping) and src.get("enabled") is False):
            self._ollama_cfg = {}
            return self._ollama_cfg
        cfg = dict(src) if isinstance(src, Mapping) else {}
        models = cfg.get("models") if isinstance(cfg.get("models"), Mapping) else {}
        self._ollama_cfg = {
            "base_url": str(cfg.get("base_url") or "http://127.0.0.1:11434/v1"),
            "model": str(cfg.get("model") or models.get("default") or DEFAULT_OLLAMA_SUMMARY_MODEL),
            "timeout_s": _pos_num(cfg.get("timeout_s")) or OLLAMA_SUMMARY_TIMEOUT_S,
        }
        return self._ollama_cfg

    def _ollama_summarize(self, error_text: str) -> Optional[str]:
        cfg = self._ollama_config()
        if not cfg:
            return None
        base = cfg["base_url"].rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = base + "/api/generate"
        clean = coerce_text(mask_secrets(str(error_text or "")))[:4000]
        prompt = (
            "Summarize in <=120 words why this agent run stalled. Reply with a "
            "short blind-alley note for the next harness.\n\n" + clean
        )
        body = json.dumps(
            {
                "model": cfg["model"],
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 160},
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout_s"]) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("response") or "").strip()
        return text[:800] or None

    def _audit(self, order: WorkOrder, action: str, **extra: Any) -> None:
        if not self._audit_enabled:
            return
        try:
            from botex.edge.verify_gate import append_supervisor_ledger
        except (ImportError, ValueError):  # pragma: no cover
            try:
                from verify_gate import append_supervisor_ledger
            except (ImportError, ValueError) as exc:
                _LOG.warning("supervisor audit unavailable: verify_gate import failed: %s", exc)
                return
        record = {
            "type": "supervisor_decision",
            "action": action,
            "order_id": getattr(order, "order_id", ""),
            "epoch": getattr(order, "epoch", 0),
            "status": getattr(order, "status", ""),
            "blocking_reason": getattr(order, "blocking_reason", ""),
            **extra,
        }
        try:
            append_supervisor_ledger(record, ledger_path=self._ledger_path)
        except Exception as exc:  # audit must never break the decision path
            _LOG.warning("supervisor audit write failed: %s", exc)

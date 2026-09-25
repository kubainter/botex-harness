# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Result Contracts
======================
Semantic failure classification shared by the engine loop, the fallback
policy and (later) the MCP result layer. A run's public ``status`` stays
stable for consumers; ``failure_kind`` explains *why* it ended that way and
drives the fallback decision.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from openai import APITimeoutError, APIConnectionError

# Public status -> default failure kind. Sites that can disambiguate further
# (tool-call preflight, prose-refusal strikes, provider exceptions) set
# ``failure_kind`` explicitly on the result dict; everything else falls back
# to this map.
STATUS_FAILURE_KIND: Dict[str, Optional[str]] = {
    "DONE": None,
    "INCOMPLETE": "no_progress",
    "UNSUPPORTED": "capability_missing",
    "API_ERROR": "provider_error",
    "REQUEST_TIMEOUT": "request_timeout",
    "TOKEN_LIMIT": "token_exhausted",
    "TIME_LIMIT": "time_exhausted",
    "MAX_TURNS_REACHED": "turns_exhausted",
    "STAGNANT_ROLLBACK": "no_progress",
    "PRICE_EXCEEDED": "budget_or_price_block",
    "BUDGET_EXCEEDED": "budget_or_price_block",
    "ZDR_VIOLATION": "security_or_policy_block",
    "DONE_WITHOUT_WRITE": "contract_failed",
    "VERIFICATION_FAILED": "contract_failed",
    "CONFIG_ERROR": "config_error",
    "ERROR": "workspace_or_security",
}

# Kinds that justify trying the next model candidate — resource limits,
# provider-side failures, semantic refusals and tool-call misuse (a
# stronger candidate forms valid calls). Caller/config/workspace
# errors can never be fixed by a different model.
FALLBACKABLE_KINDS = frozenset({
    "capability_missing",
    "tool_call_unsupported",
    "model_tool_misuse",
    "model_refusal",
    "no_progress",
    "token_exhausted",
    "request_timeout",
    "time_exhausted",
    "turns_exhausted",
    "provider_policy_denied",
    "provider_unavailable",
    "provider_error",
    "budget_or_price_block",
    "security_or_policy_block",
    "contract_failed",
})

# Statuses that must never fall back regardless of their kind — the kind
# alone cannot distinguish them (BUDGET_EXCEEDED shares budget_or_price_block
# with PRICE_EXCEEDED; VERIFICATION_FAILED shares contract_failed).
NEVER_FALLBACK_STATUSES = frozenset({
    "BUDGET_EXCEEDED",
    "VERIFICATION_FAILED",
    "CONFIG_ERROR",
    "ERROR",
})


def classify_api_error(exc: BaseException) -> str:
    """Map a provider exception to a semantic failure kind.

    401 is an auth problem — retrying another candidate with the same key
    cannot help. 403/404 are policy/availability decisions that another
    model may pass. 429 and 5xx mean provider-side pressure.
    """
    if isinstance(exc, APITimeoutError):
        return "request_timeout"
    status = getattr(exc, "status_code", None)
    if status is None:
        # Some SDKs nest the code on the wrapped HTTP response.
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 401:
        return "provider_auth_error"
    if status in (403, 404):
        return "provider_policy_denied"
    if status == 429 or (isinstance(status, int) and status >= 500):
        return "provider_unavailable"
    if isinstance(exc, APIConnectionError):
        return "provider_unavailable"
    return "provider_error"


def should_fallback(result: Dict[str, Any], has_mutations: bool = False) -> bool:
    """Whether the next model candidate should be attempted.

    A workspace that may have been mutated (file writes or executed
    commands) is never handed to another model until its rollback is
    confirmed — that confirmation lands in the fallback-policy stage.
    """
    if has_mutations:
        return False
    if result.get("status") in NEVER_FALLBACK_STATUSES:
        return False
    return result.get("failure_kind") in FALLBACKABLE_KINDS


# An analysis task must end with a non-empty report. The bar is deliberately
# just "non-empty" — terse but correct findings ("no issues found") are
# valid, and a higher threshold would only reject honest short answers.
ANALYSIS_MIN_SUMMARY_CHARS = 1


@dataclass(frozen=True)
class TaskContract:
    """Acceptance criteria a DONE result must satisfy.

    ``kind`` drives what the harness verifies locally — never what the
    model claims to have done:
    - ``analysis``    — a non-empty final report;
    - ``edit``        — a workspace change, or a confirmed no-change result
                        (the single confirm-nudge in the DONE path);
    - ``file_output`` — ``required_output_path`` exists on disk as a
                        non-empty, syntax-valid file.
    """
    kind: str                      # "analysis" | "edit" | "file_output"
    required_output_path: str = ""
    min_summary_chars: int = 0


def resolve_task_contract(
    output_path: str = "",
    resolved_mode: str = "",
) -> TaskContract:
    """Derive the task contract from caller intent — no extra param needed.

    ``output_path`` always wins (mutating capability is enforced by the
    capability preflight); an explicit readonly mode means analysis;
    everything else is an edit contract.
    """
    if output_path:
        return TaskContract(
            kind="file_output", required_output_path=output_path)
    if resolved_mode == "readonly":
        return TaskContract(
            kind="analysis", min_summary_chars=ANALYSIS_MIN_SUMMARY_CHARS)
    return TaskContract(kind="edit")

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Capability Modes
======================
Orthogonal capability flags + named mode presets. A mode is only a preset —
explicit ``allow_*`` flags may ADD capabilities, never remove them
(``readonly`` stays readonly).

Resolution (fail-closed):
    explicit allow_* flags  >  preset mode  >  engine.default_mode  >  "edit"
"""

from typing import Optional, Set

try:
    from .config import load_config
except (ImportError, ValueError):
    from config import load_config

# All capability domains. Each maps to a set of agent-visible tools.
CAP_READ = "read"
CAP_WRITE = "write"
CAP_DESTRUCTIVE = "destructive"
CAP_EXEC = "exec"
CAP_NET = "net"

from dataclasses import dataclass
from enum import Enum
from typing import List


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ToolPolicy:
    required_scopes: List[str]
    risk: RiskLevel
    approval_required: bool = False


# Tools that only require CAP_READ (implicit)
# The full registry with scopes and risk for OWASP LLM03: Excessive Agency (2026)
TOOL_REGISTRY = {
    "get_file_outline": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "read_file_lines": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "read_file": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "list_dir": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "apply_patch": ToolPolicy(required_scopes=[CAP_WRITE], risk=RiskLevel.MEDIUM),
    "create_file": ToolPolicy(required_scopes=[CAP_WRITE], risk=RiskLevel.MEDIUM),
    "delete_file": ToolPolicy(
        required_scopes=[CAP_DESTRUCTIVE], risk=RiskLevel.HIGH, approval_required=True
    ),
    "move_file": ToolPolicy(
        required_scopes=[CAP_DESTRUCTIVE], risk=RiskLevel.HIGH, approval_required=True
    ),
    "run_command": ToolPolicy(
        required_scopes=[CAP_EXEC], risk=RiskLevel.HIGH, approval_required=True
    ),
    "read_url": ToolPolicy(required_scopes=[CAP_NET], risk=RiskLevel.MEDIUM),
}

# Backwards compatibility mapping for legacy capability lookups
TOOL_CAPABILITY = {
    name: policy.required_scopes[0]
    for name, policy in TOOL_REGISTRY.items()
    if policy.required_scopes
}

# Capabilities that an interactive confirm_fn may grant per-operation.
GATED_BY_CONFIRM = {CAP_DESTRUCTIVE, CAP_EXEC}

MODES = {
    "readonly": {CAP_READ},
    "chat": {CAP_READ},
    "edit": {CAP_READ, CAP_WRITE},
    "destructive": {CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE},
    "full": {CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE, CAP_EXEC},
}

DEFAULT_MODE = "edit"


def resolve_mode_name(mode: str = "") -> str:
    """
    Resolve the effective mode name: explicit ``mode`` > ``engine.default_mode``
    > ``edit``. Unknown modes raise ``ValueError`` (fail-closed).
    """
    cfg_mode = load_config().get("engine", {}).get("default_mode") or DEFAULT_MODE
    chosen = (mode or cfg_mode or DEFAULT_MODE).lower()
    if chosen not in MODES:
        raise ValueError(f"Unknown mode '{chosen}'. Available modes: {', '.join(sorted(MODES))}.")
    return chosen


def resolve_capabilities(
    mode: str = "",
    *,
    allow_destructive: Optional[bool] = None,
    allow_exec: Optional[bool] = None,
    allow_net: Optional[bool] = None,
) -> Set[str]:
    """
    Resolve the capability set for one run.

    ``mode`` names a preset from :data:`MODES`; empty falls back to
    ``engine.default_mode`` in the config, then ``edit``. An unknown mode
    raises ``ValueError`` (fail-closed — never silently downgrade or upgrade).

    Explicit ``allow_*`` flags can only widen the preset, never narrow it.
    ``net`` is deliberately not part of any preset — it is an exfiltration /
    prompt-injection axis and must be opted into on its own.
    """
    chosen = resolve_mode_name(mode)
    caps = set(MODES[chosen])
    if allow_destructive:
        caps.add(CAP_DESTRUCTIVE)
    if allow_exec:
        caps.add(CAP_EXEC)
    if allow_net:
        caps.add(CAP_NET)
    return caps


def tool_allowed(tool_name: str, caps: Set[str], can_confirm: bool = False) -> bool:
    """
    Whether ``tool_name`` may be exposed to the agent under capability set
    ``caps``. Gated capabilities (destructive, exec) may also be granted by an
    interactive confirmation callback (``can_confirm``) — the gate is then
    enforced per-operation at dispatch time.
    """
    policy = TOOL_REGISTRY.get(tool_name)
    if not policy:
        return False

    # We require ALL required scopes to be present in caps
    missing_scopes = set(policy.required_scopes) - caps
    if not missing_scopes:
        return True

    # If not fully granted, check if missing capabilities are purely those
    # that can be interactively granted. If so, and we have can_confirm, allow it.
    if can_confirm and missing_scopes.issubset(GATED_BY_CONFIRM):
        return True

    return False


try:
    from .approval import ApprovalPolicy, TaskAbortedError, default_approval_manager
except (ImportError, ValueError):
    from approval import ApprovalPolicy, TaskAbortedError, default_approval_manager


def check_agency_policy(
    tool_name: str,
    caps: Set[str],
    confirm_fn=None,
    op_path: str = "",
    *,
    params: Optional[dict] = None,
    task_id: str = "",
    approval_policy: Optional[ApprovalPolicy] = None,
) -> str:
    """
    Agency Middleware (OWASP LLM03: Excessive Agency, 2026 edition). Evaluates ToolPolicy before executing the tool.
    Returns an error string if blocked, or an empty string if allowed.
    Synchronous version for unit tests and legacy callers.
    """
    policy = TOOL_REGISTRY.get(tool_name)
    if not policy:
        return f"Unknown tool '{tool_name}'."

    # Check scopes
    missing_scopes = set(policy.required_scopes) - caps
    if missing_scopes and not (confirm_fn and missing_scopes.issubset(GATED_BY_CONFIRM)):
        return f"Missing required scopes for {tool_name}: {', '.join(sorted(missing_scopes))}"

    mgr = (
        confirm_fn
        if hasattr(confirm_fn, "is_action_requiring_approval")
        else default_approval_manager
    )

    # If confirm_fn is a synchronous callable callback (e.g. CLI prompt or lambda in tests)
    if (
        confirm_fn
        and not hasattr(confirm_fn, "request_approval")
        and not hasattr(confirm_fn, "is_action_requiring_approval")
    ):
        if (
            policy.approval_required
            or bool(missing_scopes)
            or (
                approval_policy
                and mgr.is_action_requiring_approval(
                    tool_name, policy=approval_policy, task_id=task_id
                )
            )
        ):
            try:
                confirmed = confirm_fn(tool_name, op_path)
            except Exception as exc:
                import sys

                sys.stderr.write(f"[BoteX] confirm_fn error: {exc}\n")
                confirmed = False
            if not confirmed:
                return f"Operation '{tool_name}' was not confirmed by the operator."
        return ""

    if approval_policy is not None and approval_policy != ApprovalPolicy.NONE:
        requires_approval = (
            bool(missing_scopes)
            or policy.approval_required
            or mgr.is_action_requiring_approval(tool_name, policy=approval_policy, task_id=task_id)
        )
    else:
        requires_approval = bool(missing_scopes)

    # Check session pre-approvals
    if task_id and hasattr(mgr, "is_action_requiring_approval"):
        if not mgr.is_action_requiring_approval(tool_name, policy=approval_policy, task_id=task_id):
            requires_approval = False

    if requires_approval:
        if hasattr(confirm_fn, "is_action_requiring_approval"):
            if not confirm_fn.is_action_requiring_approval(
                tool_name, policy=approval_policy, task_id=task_id
            ):
                return ""
            return f"Operation '{tool_name}' requires async operator confirmation."
        else:
            pol_desc = (
                f" under policy '{getattr(approval_policy, 'value', approval_policy)}'"
                if approval_policy
                else ""
            )
            return f"Operation '{tool_name}' requires operator confirmation{pol_desc}, but none is available."

    return ""


async def async_check_agency_policy(
    tool_name: str,
    caps: Set[str],
    confirm_fn=None,
    op_path: str = "",
    *,
    params: Optional[dict] = None,
    task_id: str = "",
    approval_policy: Optional[ApprovalPolicy] = None,
) -> str:
    """
    Asynchronous Agency Middleware supporting blocking asyncio.Future approvals,
    ApprovalManager, and per-session pre-authorizations.
    """
    policy = TOOL_REGISTRY.get(tool_name)
    if not policy:
        return f"Unknown tool '{tool_name}'."

    # Check scopes
    missing_scopes = set(policy.required_scopes) - caps
    if missing_scopes and not (confirm_fn and missing_scopes.issubset(GATED_BY_CONFIRM)):
        return f"Missing required scopes for {tool_name}: {', '.join(sorted(missing_scopes))}"

    # Check if action requires approval
    mgr = (
        confirm_fn
        if hasattr(confirm_fn, "is_action_requiring_approval")
        else default_approval_manager
    )
    if approval_policy is not None and approval_policy != ApprovalPolicy.NONE:
        requires_approval = (
            bool(missing_scopes)
            or policy.approval_required
            or mgr.is_action_requiring_approval(tool_name, policy=approval_policy, task_id=task_id)
        )
    else:
        requires_approval = bool(missing_scopes)

    # Check session pre-approvals
    if task_id and hasattr(mgr, "is_action_requiring_approval"):
        if not mgr.is_action_requiring_approval(tool_name, policy=approval_policy, task_id=task_id):
            requires_approval = False

    if not requires_approval:
        return ""

    actual_params = params or {}
    if not actual_params and op_path:
        actual_params = {"target": op_path}

    # If no interactive confirm_fn or ApprovalManager is attached (headless mode)
    if confirm_fn is None:
        pol_desc = (
            f" under policy '{getattr(approval_policy, 'value', approval_policy)}'"
            if approval_policy
            else ""
        )
        return f"Operation '{tool_name}' requires operator confirmation{pol_desc}, but none is available."

    # Evaluate approval
    try:
        if hasattr(confirm_fn, "request_approval"):
            approved = await confirm_fn.request_approval(
                task_id=task_id,
                tool_name=tool_name,
                params=actual_params,
                policy=approval_policy,
            )
        else:
            approved = await default_approval_manager.request_approval(
                task_id=task_id,
                tool_name=tool_name,
                params=actual_params,
                policy=approval_policy,
                custom_confirm_fn=confirm_fn,
            )
    except TaskAbortedError as exc:
        return f"Operation '{tool_name}' was aborted by the operator: {exc}"

    if not approved:
        return f"Operation '{tool_name}' was not confirmed by the operator."

    return ""

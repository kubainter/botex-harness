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
# The full registry with scopes and risk for OWASP #3 Excessive Agency
TOOL_REGISTRY = {
    "get_file_outline": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "read_file_lines": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "read_file": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "list_dir": ToolPolicy(required_scopes=[CAP_READ], risk=RiskLevel.LOW),
    "apply_patch": ToolPolicy(required_scopes=[CAP_WRITE], risk=RiskLevel.MEDIUM),
    "create_file": ToolPolicy(required_scopes=[CAP_WRITE], risk=RiskLevel.MEDIUM),
    "delete_file": ToolPolicy(required_scopes=[CAP_DESTRUCTIVE], risk=RiskLevel.HIGH, approval_required=True),
    "move_file": ToolPolicy(required_scopes=[CAP_DESTRUCTIVE], risk=RiskLevel.HIGH, approval_required=True),
    "run_command": ToolPolicy(required_scopes=[CAP_EXEC], risk=RiskLevel.HIGH, approval_required=True),
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
        raise ValueError(
            f"Unknown mode '{chosen}'. Available modes: {', '.join(sorted(MODES))}."
        )
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

def check_agency_policy(tool_name: str, caps: Set[str], confirm_fn=None, op_path: str = "") -> str:
    """
    The Agency Middleware (OWASP #3). Evaluates ToolPolicy before executing the tool.
    Returns an error string if blocked, or an empty string if allowed.
    """
    policy = TOOL_REGISTRY.get(tool_name)
    if not policy:
        return f"Unknown tool '{tool_name}'."

    # Check scopes
    missing_scopes = set(policy.required_scopes) - caps
    # Gated capabilities (like destructive or exec) can be granted dynamically via confirm_fn
    # For capabilities missing from statically-granted caps, if they are gated,
    # we enforce interactive approval below. If a missing capability is not interactive,
    # the operation is blocked.
    if missing_scopes and not (confirm_fn and missing_scopes.issubset(GATED_BY_CONFIRM)):
        return f"Missing required scopes for {tool_name}: {', '.join(missing_scopes)}"

    # Risk and approval middleware
    requires_approval = policy.approval_required or bool(missing_scopes)

    if requires_approval:
        if not confirm_fn:
            return f"Operation '{tool_name}' requires operator confirmation, but none is available."

        # We pass the op_path (e.g. command or path) to confirm_fn
        if not confirm_fn(tool_name, op_path):
            return f"Operation '{tool_name}' was not confirmed by the operator."

    return ""

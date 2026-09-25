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

# Tool name -> required capability. Names absent here default to CAP_READ.
TOOL_CAPABILITY = {
    "apply_patch": CAP_WRITE,
    "create_file": CAP_WRITE,
    "delete_file": CAP_DESTRUCTIVE,
    "move_file": CAP_DESTRUCTIVE,
    "run_command": CAP_EXEC,
    "read_url": CAP_NET,
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
    cap = TOOL_CAPABILITY.get(tool_name, CAP_READ)
    if cap in caps:
        return True
    return can_confirm and cap in GATED_BY_CONFIRM

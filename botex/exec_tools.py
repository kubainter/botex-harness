# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Exec Tools
================
Policy-controlled command execution for the autonomous agent.

THIS IS NOT A SANDBOX. ``run_command`` spawns a real process with the
operator's privileges. The allowlist/denylist narrows the surface but cannot
make arbitrary command execution safe — test runners execute repository code
by design. Enable only for trusted workspaces.

Hard rules:
- argv execution only (``shell=False``) — no shell metacharacter expansion
- binary must match the configured allowlist (basename, extension-insensitive)
- arguments are checked against the configured denylist
- working directory is the workspace root
- child environment is sanitized (secret-named variables stripped)
- hard timeout with process-tree kill; stdout/stderr capped and secret-masked
"""
import asyncio
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    from .security import mask_secrets
except (ImportError, ValueError):
    from security import mask_secrets

# Environment variables whose names look like credentials are not propagated
# into the child process.
_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passwd|password|credential|auth)"
)


def _split_command(command: str) -> List[str]:
    """Split a command string into argv (POSIX-style on POSIX, looser on Win)."""
    tokens = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        # posix=False keeps quotes attached to tokens — strip them
        tokens = [t.strip('"').strip("'") for t in tokens]
    return [t for t in tokens if t]


def _binary_name(token: str) -> str:
    """Normalize a binary token: basename, lowercase, strip .exe/.cmd/.bat."""
    name = Path(token).name.lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


def _deny_hit(tokens: List[str], deny_args: List[str]) -> str:
    """Return the matched deny pattern, or ''. Single-word entries match whole
    tokens; multi-word entries match either as a substring of the joined
    command or as a token subsequence (binary + args in order) — the latter
    catches flag-insertion evasions like 'py -3 -c' vs pattern 'py -c'."""
    binary = _binary_name(tokens[0])
    arg_tokens = [t.lower() for t in tokens[1:]]
    joined = binary + " " + " ".join(arg_tokens)
    for pattern in deny_args:
        p = pattern.lower().strip()
        if not p:
            continue
        if " " not in p:
            if p in arg_tokens:
                return pattern
            continue
        if p in joined:
            return pattern
        parts = p.split()
        if parts[0] == binary:
            # Ordered subsequence match over the argument tokens.
            it = iter(arg_tokens)
            if all(any(tok == want for tok in it) for want in parts[1:]):
                return pattern
    return ""


def command_policy_error(command: str, *, allowlist: List[str], deny_args: List[str]) -> str:
    """Return a policy error for a command, or an empty string if allowed."""
    try:
        tokens = _split_command(command)
    except ValueError as e:
        return f"Cannot parse command: {e}"
    if not tokens:
        return "Empty command."

    binary = _binary_name(tokens[0])
    allowed = {_binary_name(b) for b in allowlist}
    if binary not in allowed:
        return (
            f"Command '{binary}' is not in the exec allowlist "
            f"({', '.join(sorted(allowed)) or 'empty'})."
        )
    hit = _deny_hit(tokens, deny_args)
    if hit:
        return f"Argument '{hit}' is denied by exec policy."
    return ""


def _sanitized_env() -> Dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if not _SECRET_ENV_RE.search(k)
    }


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the process and its children."""
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                lambda: __import__("subprocess").run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def run_command(
    command: str,
    workspace_root: Path,
    *,
    allowlist: List[str],
    deny_args: List[str],
    timeout_s: int = 120,
    max_output_bytes: int = 20000,
) -> Dict[str, Any]:
    """
    Execute ``command`` inside ``workspace_root`` under the configured policy.
    Returns a dict: ok, exit_code, stdout (masked, truncated), timed_out.
    """
    policy_error = command_policy_error(
        command, allowlist=allowlist, deny_args=deny_args
    )
    if policy_error:
        return {"ok": False, "error": policy_error}

    try:
        tokens = _split_command(command)
        proc = await asyncio.create_subprocess_exec(
            *tokens,
            cwd=str(workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_sanitized_env(),
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"Binary '{tokens[0]}' not found on PATH."}
    except Exception as e:
        return {"ok": False, "error": f"Cannot spawn process: {e}"}

    timed_out = False
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_tree(proc)
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            stdout_b = b""

    raw = (stdout_b or b"")[:max_output_bytes]
    text = raw.decode("utf-8", errors="replace")
    if stdout_b and len(stdout_b) > max_output_bytes:
        text += f"\n[... output truncated at {max_output_bytes} bytes]"
    text = mask_secrets(text)

    return {
        "ok": proc.returncode == 0 and not timed_out,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
        "command": tokens[0],
        "stdout": text,
    }

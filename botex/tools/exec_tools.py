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
from pathlib import Path
from typing import Any, Dict, List

try:
    from botex.core.context_guard import sanitize_and_vault_output

    from .security import mask_secrets
except (ImportError, ValueError):
    from context_guard import sanitize_and_vault_output
    from security import mask_secrets

# Environment variables whose names look like credentials are not propagated
# into the child process.
_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|token|secret|passwd|password|credential|auth|_key$|^key_)"
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


def _node_inline_eval(args: List[str]) -> bool:
    """True if node options (before the script name) request inline code evaluation,
    including clustered short flags such as '-pe'."""
    for tok in args:
        if not tok.startswith("-"):
            return False  # first positional = script; the rest belong to the script
        low = tok.lower()
        if low in ("--eval", "--print") or low.startswith(("--eval=", "--print=")):
            return True
        if not low.startswith("--") and any(c in low[1:] for c in ("e", "p")):
            return True
    return False


_PWSH_BINARIES = frozenset({"pwsh", "pwsh-preview", "powershell"})


def _pwsh_param_name(token: str) -> str:
    """Normalize a PowerShell parameter token: strip dashes and ':value'/'=value' tails."""
    return token.lstrip("-").split(":", 1)[0].split("=", 1)[0].lower()


def _pwsh_policy_error(args: List[str]) -> str:
    """PowerShell-family gate: only ``-File <script.ps1>`` execution is permitted.

    Inline command forms (-Command/-EncodedCommand and their unambiguous
    prefix abbreviations like -c/-co/-en/-ec), bare positional commands, and
    -File targets that are not clean .ps1 paths are denied. PowerShell
    resolves any unambiguous parameter prefix, so prefixes must be blocked,
    not just the full spellings.
    """
    saw_file = False
    expect_file_value = False
    for tok in args:
        if expect_file_value:
            expect_file_value = False
            # '..' anywhere, not only as a [\\/]-separated segment: POSIX
            # shlex eats backslashes first ('..\x.ps1' -> '..x.ps1'), so a
            # segment check alone misses traversal attempts off-Windows.
            if tok.lower().endswith(".ps1") and ".." not in tok:
                saw_file = True
                continue
            return "PowerShell -File requires a '.ps1' script path without '..' traversal."
        if tok.startswith("-"):
            name = _pwsh_param_name(tok)
            if name and (
                "command".startswith(name) or "encodedcommand".startswith(name) or name == "ec"
            ):
                return "PowerShell inline command execution is denied by exec policy."
            if name and "file".startswith(name):
                expect_file_value = True
            continue
        if not saw_file:
            return "PowerShell requires '-File <script.ps1>'; bare positional commands are denied."
    if expect_file_value or not saw_file:
        return "PowerShell requires '-File <script.ps1>'."
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
    if binary in {"python", "python3", "py"} and "-m" in tokens[1:]:
        return "Python module execution is denied by exec policy."
    if binary == "node" and _node_inline_eval(tokens[1:]):
        return "Node.js inline evaluation (-e/-p/--eval/--print) is denied by exec policy."
    if binary in _PWSH_BINARIES:
        pwsh_err = _pwsh_policy_error(tokens[1:])
        if pwsh_err:
            return pwsh_err
    hit = _deny_hit(tokens, deny_args)
    if hit:
        return f"Argument '{hit}' is denied by exec policy."
    return ""


def _sanitized_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


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
    policy_error = command_policy_error(command, allowlist=allowlist, deny_args=deny_args)
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

    # Decode the whole stream BEFORE any truncation so a multi-byte UTF-8
    # sequence is never cut in half.
    full_text = (stdout_b or b"").decode("utf-8", errors="replace")
    text, vault_id = sanitize_and_vault_output(full_text, "run_command", str(workspace_root))
    if len(text.encode("utf-8")) > max_output_bytes:
        text = text.encode("utf-8")[:max_output_bytes].decode("utf-8", errors="ignore")
        text += f"\n[... output truncated at {max_output_bytes} bytes]"
    text = mask_secrets(text)

    result: Dict[str, Any] = {
        "ok": proc.returncode == 0 and not timed_out,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
        "command": tokens[0],
        "stdout": text,
    }
    if vault_id:
        result["vault_memory_id"] = vault_id
    return result

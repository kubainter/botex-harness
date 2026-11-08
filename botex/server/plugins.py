# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Creator Mode — Plugin Engine
====================================
Synthesis, validation, sandboxed execution and persistence of task-scoped
helper plugins ("BoteX Plugins"). A plugin is a small Python script with a
strict stdin/stdout JSON contract:

- the runner writes a JSON object to the child's stdin,
- the script must exit with code 0 and print exactly one JSON object on
  stdout (anything else is a contract violation).

Security model (Zero Trust):

- plugins run in a dedicated subprocess via
  ``asyncio.create_subprocess_exec(sys.executable, ...)`` — never in-process;
- Rule 0 (Zero Secret Leakage): the child environment is built from a
  fixed allowlist of innocuous variables only — API keys and access
  tokens from the parent environment are never propagated;
- ``requires_net`` plugins are refused unless network access was granted
  *and* operator-approved for this run;
- execution is hard-capped by a timeout and all output passes through
  ``mask_secrets`` before it reaches the model context.

Lifecycle tiers:

- ``ephemeral`` — script lives under ``.botex/tmp_plugins/<task_id>/`` and
  is removed when the task ends (``task_stack`` cleanup);
- ``reusable`` — after validation the plugin is parked in
  ``.botex/plugins/staging/<name>/`` until an operator promotes it to the
  Local Vault (``.botex/plugins/<name>/``) or the Global Vault
  (``~/.botex/plugins/<name>/``), each indexed by an auto-generated
  ``_PLUGIN_INDEX.md``/``.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

try:
    from botex.tools.security import mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import mask_secrets, sanitize_identifier

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PluginError(Exception):
    """Base class for all BoteX plugin failures."""


class PluginValidationError(PluginError):
    """The plugin definition or its source code violates the contract."""


class PluginExecutionError(PluginError):
    """The plugin subprocess failed (non-zero exit, bad stdout contract)."""


class PluginExecutionTimeout(PluginExecutionError):
    """The plugin subprocess exceeded its execution time budget."""


class PluginPermissionError(PluginError):
    """The plugin requested a capability that was not granted for this run."""


# ---------------------------------------------------------------------------
# Plugin Definition
# ---------------------------------------------------------------------------

_VALID_REUSABILITY = ("ephemeral", "reusable")
_VALID_SCOPES = ("ephemeral", "staging", "local", "global")


def plugin_code_sha256(code: str) -> str:
    """Content hash of a plugin's source — recorded in manifests for audit."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# OpenAI-compatible tool-name surface: letters, digits, underscore, dash.
_PLUGIN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


@dataclass
class BoteXPluginDefinition:
    """Declarative contract of one BoteX plugin (synthesized or vaulted)."""

    name: str
    description: str
    parameters_schema: Dict[str, Any]
    example_usage: Dict[str, Any] = field(default_factory=dict)
    reusability: str = "ephemeral"
    requires_net: bool = False
    scope: str = "ephemeral"
    script_path: str = ""
    sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters_schema": self.parameters_schema,
            "example_usage": self.example_usage,
            "reusability": self.reusability,
            "requires_net": self.requires_net,
            "scope": self.scope,
            "script_path": self.script_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BoteXPluginDefinition":
        if not isinstance(data, dict):
            raise PluginValidationError("Plugin definition must be a JSON object.")
        reusability = str(data.get("reusability") or "ephemeral")
        scope = str(data.get("scope") or "ephemeral")
        if reusability not in _VALID_REUSABILITY:
            reusability = "ephemeral"
        if scope not in _VALID_SCOPES:
            scope = "ephemeral"
        params = data.get("parameters_schema")
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            parameters_schema=params if isinstance(params, dict) else {},
            example_usage=(
                data.get("example_usage") if isinstance(data.get("example_usage"), dict) else {}
            ),
            reusability=reusability,
            requires_net=bool(data.get("requires_net")),
            scope=scope,
            script_path=str(data.get("script_path") or ""),
            sha256=str(data.get("sha256") or ""),
        )

    def to_openai_tool(self) -> Dict[str, Any]:
        """Render the definition as an OpenAI function-calling tool declaration."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


# ---------------------------------------------------------------------------
# Zero-Trust Subprocess Runner
# ---------------------------------------------------------------------------


class ZeroTrustSubprocessRunner:
    """
    Executes plugin scripts in a dedicated child process with a scrubbed
    environment, an I/O JSON contract and a hard wall-clock timeout.
    """

    DEFAULT_TIMEOUT_S = 15.0

    # Rule 0 (Zero Secret Leakage): ONLY these parent variables propagate.
    _SAFE_ENV_KEYS: FrozenSet[str] = frozenset(
        {
            "PATH",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "PYTHONPATH",
        }
    )

    def __init__(self, workspace_dir: Any = "", timeout_s: float = DEFAULT_TIMEOUT_S):
        self.workspace_dir = Path(workspace_dir).resolve() if workspace_dir else Path.cwd()
        self.timeout_s = float(timeout_s) if timeout_s else self.DEFAULT_TIMEOUT_S

    def build_env(self, net_allowed: bool = False) -> Dict[str, str]:
        """Construct the scrubbed child environment — no secrets can leak."""
        env = {k: os.environ[k] for k in self._SAFE_ENV_KEYS if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["BOTEX_WORKSPACE_DIR"] = str(self.workspace_dir)
        env["BOTEX_NET_ALLOWED"] = "1" if net_allowed else "0"
        return env

    async def run(
        self,
        plugin: BoteXPluginDefinition,
        input_data: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        allow_net: bool = False,
        approval_confirmed: bool = False,
    ) -> Dict[str, Any]:
        """
        Spawn the plugin subprocess, pipe ``input_data`` as JSON on stdin and
        return the parsed stdout object. Raises PluginError subclasses on any
        contract or permission violation.
        """
        if plugin.requires_net and not (allow_net and approval_confirmed):
            raise PluginPermissionError(
                f"Plugin '{plugin.name}' declares requires_net=True but network access "
                "was not granted and approved for this run. Refusing to execute."
            )

        script_path = Path(plugin.script_path)
        if not script_path.is_file():
            raise PluginExecutionError(
                f"Plugin '{plugin.name}' script not found: {plugin.script_path}"
            )

        effective_timeout = self.timeout_s if timeout is None else float(timeout)
        env = self.build_env(net_allowed=allow_net and approval_confirmed)
        payload = json.dumps(input_data or {}).encode("utf-8")

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.workspace_dir),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=payload), timeout=effective_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass
            raise PluginExecutionTimeout(
                f"Plugin '{plugin.name}' exceeded the {effective_timeout:.1f}s execution limit."
            )

        stdout_text = mask_secrets((stdout_b or b"").decode("utf-8", errors="replace"))
        stderr_text = mask_secrets((stderr_b or b"").decode("utf-8", errors="replace"))

        if proc.returncode != 0:
            raise PluginExecutionError(
                f"Plugin '{plugin.name}' exited with code {proc.returncode}: "
                f"{stderr_text.strip()[:500] or 'no stderr output'}"
            )

        try:
            parsed = json.loads(stdout_text.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise PluginExecutionError(
                f"Plugin '{plugin.name}' violated the stdout contract — expected a JSON "
                f"object, got unparseable output: {exc}"
            )
        if not isinstance(parsed, dict):
            raise PluginExecutionError(
                f"Plugin '{plugin.name}' violated the stdout contract — expected a JSON "
                f"object, got {type(parsed).__name__}."
            )

        return {
            "ok": True,
            "name": plugin.name,
            "output": parsed,
            "stderr": stderr_text[:2000],
        }


# ---------------------------------------------------------------------------
# Contract Invariant Validator
# ---------------------------------------------------------------------------


class ContractInvariantValidator:
    """
    Gatekeeper for synthesized plugins: validates the declared name and JSON
    Schema, compiles the source in memory, then proves the script end-to-end
    in the Zero-Trust runner with the declared ``example_usage.input``.
    """

    SMOKE_TIMEOUT_S = 5.0

    def __init__(self, runner: Optional[ZeroTrustSubprocessRunner] = None):
        self.runner = runner or ZeroTrustSubprocessRunner()

    @staticmethod
    def validate_name(name: str, *, reserved: FrozenSet[str] = frozenset()) -> str:
        """Normalize and whitelist-check a plugin name (tool name + dir name)."""
        cleaned = (name or "").strip()
        if not cleaned:
            raise PluginValidationError("Plugin name must be a non-empty identifier.")
        if not _PLUGIN_NAME_RE.fullmatch(cleaned):
            raise PluginValidationError(
                f"Invalid plugin name '{cleaned[:64]}' — use letters, digits, '_' or '-' "
                "(max 64 chars, no leading dash)."
            )
        if cleaned in reserved:
            raise PluginValidationError(
                f"Plugin name '{cleaned}' collides with a built-in tool name."
            )
        return cleaned

    @staticmethod
    def validate_parameters_schema(schema: Any) -> Dict[str, Any]:
        """Require a non-empty JSON Schema object declaring an object shape."""
        if not isinstance(schema, dict) or not schema:
            raise PluginValidationError("parameters_schema must be a non-empty JSON Schema object.")
        return schema

    @staticmethod
    def compile_check(code: str) -> None:
        """In-memory syntax gate — a plugin that does not parse never reaches disk."""
        try:
            compile(code, "<plugin>", "exec")
        except SyntaxError as exc:
            raise PluginValidationError(
                f"Plugin source failed to compile: {exc.msg} (line {exc.lineno})."
            ) from exc

    async def smoke_test(
        self,
        plugin: BoteXPluginDefinition,
        *,
        timeout: float = SMOKE_TIMEOUT_S,
        allow_net: bool = False,
        approval_confirmed: bool = False,
    ) -> Dict[str, Any]:
        """
        Prove the plugin end-to-end: run it once in the Zero-Trust subprocess
        with ``example_usage.input`` on stdin; a JSON object on stdout and
        exit code 0 are required by the contract.
        """
        example_input = plugin.example_usage.get("input") or {}
        if not isinstance(example_input, dict):
            raise PluginValidationError(
                "example_usage.input must be a JSON object matching parameters_schema."
            )
        return await self.runner.run(
            plugin,
            input_data=example_input,
            timeout=timeout,
            allow_net=allow_net,
            approval_confirmed=approval_confirmed,
        )


# ---------------------------------------------------------------------------
# Task-Local Plugin Registry
# ---------------------------------------------------------------------------


class TaskLocalPluginRegistry:
    """
    Holds plugins synthesized during a single task. Registries are deliberately
    NOT shared between tasks — concurrent FastAPI tasks can never observe or
    invoke each other's ad-hoc tooling.
    """

    MAX_SYNTHESIS_ATTEMPTS = 2

    def __init__(self, task_id: str, vault: Optional["PluginVaultManager"] = None):
        safe_id = sanitize_identifier(str(task_id)) if task_id else ""
        self.task_id = safe_id or "task"
        self.vault = vault
        self._plugins: Dict[str, BoteXPluginDefinition] = {}
        self._synthesis_attempts = 0

    @property
    def synthesis_attempts(self) -> int:
        return self._synthesis_attempts

    def begin_synthesis_attempt(self) -> Optional[Dict[str, Any]]:
        """
        Consume one synthesis attempt. Returns None while the task is within
        its budget, otherwise a structured error that blocks further
        synthesis and directs the agent back to the standard tools.
        """
        if self._synthesis_attempts >= self.MAX_SYNTHESIS_ATTEMPTS:
            return {
                "ok": False,
                "status": "SYNTHESIS_LIMIT_REACHED",
                "error": (
                    f"Plugin synthesis budget exhausted — at most "
                    f"{self.MAX_SYNTHESIS_ATTEMPTS} attempts (1 generation + 1 fix) "
                    "are allowed per task. Do NOT retry synthesize_plugin; complete "
                    "the task with the standard tools instead."
                ),
            }
        self._synthesis_attempts += 1
        return None

    def register(self, plugin: BoteXPluginDefinition) -> Dict[str, Any]:
        """Register (or replace, on a fix attempt) a validated task plugin."""
        clean_name = ContractInvariantValidator.validate_name(plugin.name)
        plugin.name = clean_name
        self._plugins[clean_name] = plugin
        return {"ok": True, "name": clean_name, "count": len(self._plugins)}

    def get_plugin(self, name: str) -> Optional[BoteXPluginDefinition]:
        return self._plugins.get(name)

    def list_plugins(self) -> List[BoteXPluginDefinition]:
        return list(self._plugins.values())

    def tool_declarations(self) -> List[Dict[str, Any]]:
        return [p.to_openai_tool() for p in self._plugins.values()]

    def remove(self, name: str) -> bool:
        return self._plugins.pop(name, None) is not None


# ---------------------------------------------------------------------------
# Plugin Vault Manager
# ---------------------------------------------------------------------------


class PluginVaultManager:
    """
    Filesystem layout and lifecycle for the three plugin tiers plus the
    staging area, and the auto-generated ``_PLUGIN_INDEX`` documentation.
    """

    EPHEMERAL_ROOT = ".botex/tmp_plugins"
    VAULT_ROOT = ".botex/plugins"
    STAGING_DIRNAME = "staging"
    RUNNER_FILENAME = "plugin_runner.py"
    DEF_FILENAME = "plugin.json"
    INDEX_MD = "_PLUGIN_INDEX.md"
    INDEX_JSON = "_PLUGIN_INDEX.json"

    def __init__(self, workspace_dir: Any, global_dir: Optional[Any] = None):
        self.workspace_dir = Path(workspace_dir).resolve()
        env_dir = (os.environ.get("BOTEX_GLOBAL_PLUGINS_DIR") or "").strip()
        if global_dir is not None:
            base = Path(global_dir)
        elif env_dir:
            base = Path(env_dir)
        else:
            base = Path.home() / ".botex" / "plugins"
        # Both conventions are accepted: a base already ending in "plugins"
        # is the vault root itself, otherwise the vault is <base>/plugins.
        self.global_vault = base if base.name == "plugins" else base / "plugins"

    # -- directory helpers -------------------------------------------------

    def ephemeral_dir(self, task_id: str) -> Path:
        safe = sanitize_identifier(str(task_id)) or "task"
        return self.workspace_dir / self.EPHEMERAL_ROOT / safe

    @property
    def vault_root(self) -> Path:
        return self.workspace_dir / self.VAULT_ROOT

    @property
    def staging_root(self) -> Path:
        return self.vault_root / self.STAGING_DIRNAME

    def _scope_root(self, scope: str) -> Path:
        return self.global_vault if scope == "global" else self.vault_root

    # -- task lifecycle ------------------------------------------------------

    def cleanup_task_plugins(self, task_id: str) -> None:
        """Remove the whole ephemeral plugin dir for a finished task (idempotent)."""
        shutil.rmtree(self.ephemeral_dir(task_id), ignore_errors=True)

    def write_ephemeral_plugin(self, task_id: str, name: str, code: str) -> Path:
        """Materialize a validated script under the task's ephemeral dir."""
        clean = sanitize_identifier(str(name)) or "plugin"
        target_dir = self.ephemeral_dir(task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        script = target_dir / f"{clean}.py"
        script.write_text(code, encoding="utf-8")
        return script

    # -- staging & vault ------------------------------------------------------

    def promote_to_staging(self, plugin: BoteXPluginDefinition, code: str) -> Path:
        """Park a validated reusable plugin in the staging area for review."""
        staged = replace(plugin, scope="staging", sha256=plugin_code_sha256(code))
        target_dir = self.staging_root / staged.name
        target_dir.mkdir(parents=True, exist_ok=True)
        script = target_dir / self.RUNNER_FILENAME
        script.write_text(code, encoding="utf-8")
        staged.script_path = str(script)
        (target_dir / self.DEF_FILENAME).write_text(
            json.dumps(staged.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return target_dir

    def _scan_vault(self, root: Path, scope: str) -> List[BoteXPluginDefinition]:
        """Load plugin manifests from one vault root (staging excluded)."""
        plugins: List[BoteXPluginDefinition] = []
        if not root.is_dir():
            return plugins
        for child in sorted(root.iterdir()):
            if (
                not child.is_dir()
                or child.name.startswith("_")
                or child.name == self.STAGING_DIRNAME
            ):
                continue
            manifest_path = child / self.DEF_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                plugin = BoteXPluginDefinition.from_dict(data)
            except (OSError, json.JSONDecodeError, PluginValidationError):
                continue
            # Re-anchor the runtime path: manifests stay portable across
            # machines, execution always uses the runner inside this dir.
            runner = child / self.RUNNER_FILENAME
            if runner.is_file():
                plugin.script_path = str(runner)
            plugin.scope = scope
            plugins.append(plugin)
        return plugins

    def load_vault_plugins(self, scope: str = "all") -> List[BoteXPluginDefinition]:
        """All active vault plugins (local + global); staged ones stay inactive."""
        plugins: List[BoteXPluginDefinition] = []
        if scope in ("all", "local"):
            plugins.extend(self._scan_vault(self.vault_root, "local"))
        if scope in ("all", "global"):
            plugins.extend(self._scan_vault(self.global_vault, "global"))
        return plugins

    def list_staged_plugins(self) -> List[BoteXPluginDefinition]:
        return self._scan_vault(self.staging_root, "staging")

    def resolve_staged_plugin(self, name: str, action: str) -> Dict[str, Any]:
        """
        Resolve a staged plugin: promote to the Local Vault (``local``),
        promote to the Global Vault (``global``), or ``discard`` it.
        Rebuilds the target vault's index after a promotion.
        """
        clean = sanitize_identifier(str(name))
        if not clean:
            return {"ok": False, "error": f"Invalid plugin name '{name}'."}
        action_norm = (action or "").strip().lower()
        if action_norm not in ("local", "global", "discard"):
            return {
                "ok": False,
                "error": f"Unknown action '{action}' — expected 'local', 'global' or 'discard'.",
            }
        staged_dir = self.staging_root / clean
        if not staged_dir.is_dir():
            return {"ok": False, "error": f"Staged plugin '{clean}' not found."}

        if action_norm == "discard":
            shutil.rmtree(staged_dir, ignore_errors=True)
            return {"ok": True, "name": clean, "action": "discard"}

        dest_root = self._scope_root(action_norm)
        dest = dest_root / clean
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(staged_dir), str(dest))
            # The manifest's stored paths point at the old staging dir —
            # re-anchor them to the promoted location.
            manifest_path = dest / self.DEF_FILENAME
            if manifest_path.is_file():
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                plugin = BoteXPluginDefinition.from_dict(data)
                plugin.scope = action_norm
                runner = dest / self.RUNNER_FILENAME
                if runner.is_file():
                    plugin.script_path = str(runner)
                manifest_path.write_text(
                    json.dumps(plugin.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            index_path = self.rebuild_index(action_norm)
        except (OSError, json.JSONDecodeError, PluginValidationError) as exc:
            return {"ok": False, "error": f"Failed to promote '{clean}': {exc}"}
        return {
            "ok": True,
            "name": clean,
            "action": action_norm,
            "path": str(dest),
            "index": str(index_path),
        }

    # -- index generation ------------------------------------------------------

    def rebuild_index(self, scope: str = "local") -> Path:
        """
        Regenerate ``_PLUGIN_INDEX.md`` and ``_PLUGIN_INDEX.json`` for one
        vault (``local`` or ``global``) from the on-disk manifests.
        """
        scope_norm = scope if scope in ("local", "global") else "local"
        root = self._scope_root(scope_norm)
        root.mkdir(parents=True, exist_ok=True)
        plugins = self._scan_vault(root, scope_norm)

        index_json = {
            "scope": scope_norm,
            "generated_by": "botex.plugins.PluginVaultManager",
            "plugins": [p.to_dict() for p in plugins],
        }
        (root / self.INDEX_JSON).write_text(
            json.dumps(index_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        lines = [
            f"# BoteX Plugin Index — {scope_norm} vault",
            "",
            "| Plugin | Scope | Network | Reusability | Description |",
            "|---|---|---|---|---|",
        ]
        for p in plugins:
            net_policy = "requires_net" if p.requires_net else "offline"
            desc = p.description.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{p.name}` | {scope_norm} | {net_policy} | {p.reusability} | {desc} |")
        if not plugins:
            lines.append("| _(none)_ | — | — | — | No plugins installed yet. |")
        lines.append("")
        for p in plugins:
            net_policy = "requires_net" if p.requires_net else "offline"
            lines.append(f"## `{p.name}`")
            lines.append("")
            lines.append(f"- **Scope:** {scope_norm}")
            lines.append(f"- **Network policy:** {net_policy}")
            lines.append(f"- **Reusability:** {p.reusability}")
            if p.sha256:
                lines.append(f"- **SHA-256:** `{p.sha256}`")
            lines.append("")
            lines.append(p.description or "_(no description)_")
            lines.append("")
            lines.append("**Parameters schema:**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(p.parameters_schema, indent=2, ensure_ascii=False))
            lines.append("```")
            example = p.example_usage or {}
            lines.append("")
            lines.append("**Example usage:**")
            lines.append("")
            lines.append("```json")
            lines.append(
                json.dumps(
                    {
                        "input": example.get("input", {}),
                        "output": example.get("output", {}),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            lines.append("```")
            lines.append("")

        index_md_path = root / self.INDEX_MD
        index_md_path.write_text("\n".join(lines), encoding="utf-8")
        return index_md_path

    def get_index_markdown(self, scope: str = "local") -> str:
        """Return the index doc for one vault, rebuilding it when absent."""
        scope_norm = scope if scope in ("local", "global") else "local"
        index_path = self._scope_root(scope_norm) / self.INDEX_MD
        if not index_path.is_file():
            try:
                index_path = self.rebuild_index(scope_norm)
            except OSError:
                return ""
        try:
            return index_path.read_text(encoding="utf-8")
        except OSError:
            return ""

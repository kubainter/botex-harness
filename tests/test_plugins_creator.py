# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Tests for BoteX Creator Mode — Plugin Engine (Worker B domain)
================================================================
Coverage:
1. Rule 0 (Zero Secret Leakage): the plugin subprocess environment never
   carries provider API keys from the parent process; a net-denied run
   additionally gets dead-end ``*_PROXY`` vars (best-effort egress denial).
2. Hard timeout: a plugin exceeding its budget is killed and raises
   PluginExecutionTimeout.
3. Network permission gate: requires_net=True without an explicit grant +
   approval raises PluginPermissionError.
4. Task-local registry isolation: registries for different task_ids cannot
   see each other's plugins; MAX_SYNTHESIS_ATTEMPTS=2 blocks the 3rd call.
5. Staging & _PLUGIN_INDEX: reusable plugins staged, promoted to local and
   global vaults produce index docs; discard removes them.
6. TwoTierLifoStack: closing the attempt tier never runs task-tier cleanups.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from botex.engine import TwoTierLifoStack
from botex.plugins import (
    BoteXPluginDefinition,
    ContractInvariantValidator,
    PluginExecutionTimeout,
    PluginPermissionError,
    PluginValidationError,
    PluginVaultManager,
    TaskLocalPluginRegistry,
    ZeroTrustSubprocessRunner,
)

# Plugin script contract: read a JSON object on stdin, print one JSON object
# on stdout, exit 0.
ENV_DUMP_SCRIPT = (
    "import sys, json, os\njson.load(sys.stdin)\nprint(json.dumps(dict(os.environ)))\n"
)

SLEEP_SCRIPT = (
    "import sys, json, time\n"
    "json.load(sys.stdin)\n"
    "time.sleep(10)\n"
    "print(json.dumps({'done': True}))\n"
)

ECHO_SCRIPT = (
    "import sys, json\ndata = json.load(sys.stdin)\nprint(json.dumps({'ok': True, 'echo': data}))\n"
)


def _write_script(tmp_path: Path, name: str, code: str) -> Path:
    script = tmp_path / name
    script.write_text(code, encoding="utf-8")
    return script


def _def(name: str, script: Path, **kw) -> BoteXPluginDefinition:
    kw.setdefault("description", f"{name} plugin")
    kw.setdefault("parameters_schema", {"type": "object", "properties": {}})
    return BoteXPluginDefinition(name=name, script_path=str(script), **kw)


# ---------------------------------------------------------------------------
# 1. Rule 0 — Zero Secret Leakage into the child environment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rule0_plugin_env_never_sees_provider_keys(tmp_path, monkeypatch):
    """A plugin dumping os.environ must not observe parent API keys."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-unittest-fake")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-unittest-fake")
    script = _write_script(tmp_path, "envdump.py", ENV_DUMP_SCRIPT)
    plugin = _def("envdump", script)
    runner = ZeroTrustSubprocessRunner(tmp_path)

    res = await runner.run(plugin, input_data={})

    assert res["ok"] is True
    child_env = res["output"]
    assert "OPENROUTER_API_KEY" not in child_env
    assert "NVIDIA_API_KEY" not in child_env
    assert "GEMINI_API_KEY" not in child_env
    assert "BOTEX_ACCESS_TOKEN" not in child_env
    # Positive control: the scrubbed env still carries the harness markers.
    assert child_env.get("BOTEX_NET_ALLOWED") == "0"
    assert child_env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert child_env.get("BOTEX_WORKSPACE_DIR")


# --- Egress denial: dead-end proxy injection (best-effort) ------------------

_DEAD_PROXY_URL = "http://127.0.0.1:9"
_DEAD_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def test_build_env_denied_net_injects_dead_end_proxy(monkeypatch):
    """net_allowed=False -> every proxy var points at the discard endpoint."""
    monkeypatch.setenv("HTTP_PROXY", "http://host-proxy:3128")  # must be overridden
    env = ZeroTrustSubprocessRunner().build_env(net_allowed=False)
    assert env["BOTEX_NET_ALLOWED"] == "0"
    for key in _DEAD_PROXY_KEYS:
        assert env[key] == _DEAD_PROXY_URL
    for key in _NO_PROXY_KEYS:
        assert env[key] == ""


def test_build_env_allowed_net_carries_no_proxy_vars(monkeypatch):
    """net_allowed=True -> no injected proxies and no host proxy leak."""
    for key in _DEAD_PROXY_KEYS + _NO_PROXY_KEYS:
        monkeypatch.setenv(key, "http://host-proxy:3128")
    env = ZeroTrustSubprocessRunner().build_env(net_allowed=True)
    assert env["BOTEX_NET_ALLOWED"] == "1"
    for key in _DEAD_PROXY_KEYS + _NO_PROXY_KEYS:
        assert key not in env


# ---------------------------------------------------------------------------
# 2. Hard execution timeout
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plugin_timeout_kills_subprocess(tmp_path):
    """time.sleep(10) with timeout=0.5 -> killed -> PluginExecutionTimeout."""
    script = _write_script(tmp_path, "sleeper.py", SLEEP_SCRIPT)
    plugin = _def("sleeper", script)
    runner = ZeroTrustSubprocessRunner(tmp_path)

    with pytest.raises(PluginExecutionTimeout):
        await runner.run(plugin, input_data={}, timeout=0.5)


# ---------------------------------------------------------------------------
# 3. Network permission gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_requires_net_blocked_without_authorization(tmp_path):
    """requires_net=True needs BOTH allow_net and approval_confirmed."""
    script = _write_script(tmp_path, "netprobe.py", ECHO_SCRIPT)
    plugin = _def("netprobe", script, requires_net=True)
    runner = ZeroTrustSubprocessRunner(tmp_path)

    with pytest.raises(PluginPermissionError):
        await runner.run(plugin, input_data={}, allow_net=False, approval_confirmed=False)

    with pytest.raises(PluginPermissionError):
        await runner.run(plugin, input_data={}, allow_net=True, approval_confirmed=False)

    with pytest.raises(PluginPermissionError):
        await runner.run(plugin, input_data={}, allow_net=False, approval_confirmed=True)

    res = await runner.run(plugin, input_data={}, allow_net=True, approval_confirmed=True)
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# 4. Task-local registry isolation + synthesis budget
# ---------------------------------------------------------------------------


def test_task_local_registries_do_not_share_plugins():
    reg_a = TaskLocalPluginRegistry("task-aaa")
    reg_b = TaskLocalPluginRegistry("task-bbb")

    reg_a.register(_def("alpha_tool", Path("alpha.py")))

    assert reg_a.get_plugin("alpha_tool") is not None
    assert reg_b.get_plugin("alpha_tool") is None
    assert [p.name for p in reg_b.list_plugins()] == []


def test_synthesis_attempt_cap_blocks_third_call():
    reg = TaskLocalPluginRegistry("task-cap")

    assert reg.begin_synthesis_attempt() is None  # generation
    assert reg.begin_synthesis_attempt() is None  # fix
    blocked = reg.begin_synthesis_attempt()
    assert blocked is not None
    assert blocked["ok"] is False
    assert blocked["status"] == "SYNTHESIS_LIMIT_REACHED"
    assert "standard tools" in blocked["error"]
    # The budget stays exhausted — a 4th call is blocked too.
    assert reg.begin_synthesis_attempt() is not None


# ---------------------------------------------------------------------------
# 5. Staging lifecycle & _PLUGIN_INDEX
# ---------------------------------------------------------------------------

_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "payload"}},
    "required": ["text"],
}
_EXAMPLE = {"input": {"text": "hello"}, "output": {"ok": True, "echo": {"text": "hello"}}}


def test_staging_promotion_local_global_and_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTEX_GLOBAL_PLUGINS_DIR", str(tmp_path / "gvault"))
    vault = PluginVaultManager(tmp_path)

    plugin = _def(
        "echo_tool",
        tmp_path / "unused.py",
        reusability="reusable",
        parameters_schema=_PARAMS_SCHEMA,
        example_usage=_EXAMPLE,
    )
    staged_dir = vault.promote_to_staging(plugin, ECHO_SCRIPT)
    assert (staged_dir / "plugin.json").is_file()
    assert (staged_dir / "plugin_runner.py").is_file()
    assert any(p.name == "echo_tool" for p in vault.list_staged_plugins())

    # --- promote to the LOCAL vault ---
    res = vault.resolve_staged_plugin("echo_tool", "local")
    assert res["ok"] is True
    local_dir = tmp_path / ".botex" / "plugins" / "echo_tool"
    assert (local_dir / "plugin.json").is_file()
    assert (local_dir / "plugin_runner.py").is_file()

    idx_md = tmp_path / ".botex" / "plugins" / "_PLUGIN_INDEX.md"
    idx_json = tmp_path / ".botex" / "plugins" / "_PLUGIN_INDEX.json"
    assert idx_md.is_file() and idx_json.is_file()
    md = idx_md.read_text(encoding="utf-8")
    assert "echo_tool" in md
    assert "echo_tool plugin" in md
    assert "offline" in md  # network policy column
    assert '"text"' in md  # parameters schema rendered
    assert "Example usage" in md
    assert '"hello"' in md  # example_usage JSON rendered
    idx_data = json.loads(idx_json.read_text(encoding="utf-8"))
    assert idx_data["scope"] == "local"
    assert any(p["name"] == "echo_tool" for p in idx_data["plugins"])

    loaded = vault.load_vault_plugins()
    assert any(p.name == "echo_tool" and p.scope == "local" for p in loaded)

    # --- promote a second plugin to the GLOBAL vault ---
    plugin_g = _def(
        "echo_global",
        tmp_path / "unused2.py",
        reusability="reusable",
        parameters_schema=_PARAMS_SCHEMA,
        example_usage=_EXAMPLE,
    )
    vault.promote_to_staging(plugin_g, ECHO_SCRIPT)
    res_g = vault.resolve_staged_plugin("echo_global", "global")
    assert res_g["ok"] is True
    global_dir = Path(str(tmp_path / "gvault" / "plugins" / "echo_global"))
    assert (global_dir / "plugin.json").is_file()
    g_md = global_dir.parent / "_PLUGIN_INDEX.md"
    assert g_md.is_file()
    assert "echo_global" in g_md.read_text(encoding="utf-8")
    assert any(
        p.name == "echo_global" and p.scope == "global"
        for p in vault.load_vault_plugins(scope="global")
    )

    # --- discard removes the staging dir ---
    plugin_d = _def("echo_drop", tmp_path / "unused3.py", reusability="reusable")
    drop_dir = vault.promote_to_staging(plugin_d, ECHO_SCRIPT)
    assert drop_dir.is_dir()
    res_d = vault.resolve_staged_plugin("echo_drop", "discard")
    assert res_d["ok"] is True
    assert not drop_dir.exists()
    assert all(p.name != "echo_drop" for p in vault.list_staged_plugins())


def test_resolve_staged_plugin_unknown_and_bad_action(tmp_path):
    vault = PluginVaultManager(tmp_path)
    assert vault.resolve_staged_plugin("nope", "local")["ok"] is False
    assert vault.resolve_staged_plugin("x", "bogus")["ok"] is False
    assert vault.resolve_staged_plugin("../evil", "local")["ok"] is False


# ---------------------------------------------------------------------------
# 6. TwoTierLifoStack — attempt cleanup never touches the task tier
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_tier_lifo_stack_attempt_close_preserves_task_resources():
    cleaned = []
    stacks = TwoTierLifoStack()
    await stacks.__aenter__()
    stacks.task_stack.callback(lambda: cleaned.append("task"))

    attempt = await stacks.enter_attempt()
    attempt.callback(lambda: cleaned.append("attempt"))
    await stacks.close_attempt_stack()

    assert cleaned == ["attempt"]  # task-tier cleanup NOT run

    await stacks.aclose()
    assert cleaned == ["attempt", "task"]


@pytest.mark.anyio
async def test_two_tier_lifo_stack_enter_attempt_rolls_previous_attempt():
    cleaned = []
    stacks = TwoTierLifoStack()
    await stacks.__aenter__()
    stacks.task_stack.callback(lambda: cleaned.append("task"))

    first = await stacks.enter_attempt()
    first.callback(lambda: cleaned.append("attempt-1"))
    second = await stacks.enter_attempt()  # closing attempt-1 implicitly
    second.callback(lambda: cleaned.append("attempt-2"))

    assert cleaned == ["attempt-1"]
    await stacks.aclose()
    assert cleaned == ["attempt-1", "attempt-2", "task"]


# ---------------------------------------------------------------------------
# Contract validator sanity checks
# ---------------------------------------------------------------------------


def test_validator_rejects_bad_names_and_syntax(tmp_path):
    validator = ContractInvariantValidator(ZeroTrustSubprocessRunner(tmp_path))
    with pytest.raises(PluginValidationError):
        validator.validate_name("")
    with pytest.raises(PluginValidationError):
        validator.validate_name("bad name!")
    with pytest.raises(PluginValidationError):
        validator.validate_name("run_command", reserved=frozenset({"run_command"}))
    with pytest.raises(PluginValidationError):
        validator.compile_check("def broken(:\n")
    validator.compile_check("x = 1\n")


@pytest.mark.anyio
async def test_smoke_test_proves_echo_contract(tmp_path):
    script = _write_script(tmp_path, "echo.py", ECHO_SCRIPT)
    plugin = _def("echo", script, example_usage={"input": {"n": 1}, "output": {}})
    runner = ZeroTrustSubprocessRunner(tmp_path)
    res = await ContractInvariantValidator(runner).smoke_test(plugin)
    assert res["ok"] is True
    assert res["output"]["echo"] == {"n": 1}

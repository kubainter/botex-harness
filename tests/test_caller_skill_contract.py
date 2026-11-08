from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "botex" / "SKILL.md"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _functions(path: Path) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {
        node.name: node
        for node in _module(path).body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _mcp_tools(path: Path) -> set[str]:
    tools = set()
    for node in _module(path).body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "tool"
            for dec in node.decorator_list
        ):
            tools.add(node.name)
    return tools


def test_start_task_and_run_subagent_share_caller_parameters():
    functions = _functions(ROOT / "server.py")
    assert {arg.arg for arg in functions["start_task"].args.args} == {
        arg.arg for arg in functions["run_subagent"].args.args
    }


def test_start_task_declares_and_forwards_approval_policy():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start_task = _functions(ROOT / "server.py")["start_task"]
    params = {arg.arg for arg in start_task.args.args}
    assert "approval_policy" in params
    assert '"approval_policy": approval_policy' in source


def test_caller_tool_catalog_matches_skill():
    source = SKILL.read_text(encoding="utf-8")
    match = re.search(r"^MCP tools:\s*(.+)$", source, re.MULTILINE)
    assert match, "Skill must publish the caller-facing MCP tool catalog."
    documented = set(re.findall(r"`([^`]+)`", match.group(1)))
    assert documented == _mcp_tools(ROOT / "server.py")


def test_skill_documents_all_engine_statuses():
    contracts_file = ROOT / "botex" / "core" / "contracts.py"
    if not contracts_file.exists():
        contracts_file = ROOT / "botex" / "contracts.py"
    contracts = _module(contracts_file)
    status_assignment = next(
        node
        for node in contracts.body
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                any(
                    isinstance(target, ast.Name) and target.id == "STATUS_FAILURE_KIND"
                    for target in node.targets
                )
                if isinstance(node, ast.Assign)
                else isinstance(node.target, ast.Name) and node.target.id == "STATUS_FAILURE_KIND"
            )
        )
    )
    status_value = status_assignment.value
    statuses = {
        key.value
        for key in status_value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    skill = SKILL.read_text(encoding="utf-8")
    assert statuses <= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", skill))


def test_skill_explains_caller_and_worker_layers():
    skill = SKILL.read_text(encoding="utf-8").lower()
    assert "caller-facing" in skill
    assert "internal worker" in skill
    assert "result_data" in skill


def test_hard_readonly_policy_is_present_in_capabilities():
    cap_file = ROOT / "botex" / "core" / "capabilities.py"
    if not cap_file.exists():
        cap_file = ROOT / "botex" / "capabilities.py"
    source = cap_file.read_text(encoding="utf-8")
    assert '{"readonly", "chat"}' in source
    assert "Mode '{chosen}' is hard read-only" in source
    assert '"delete_file": ToolPolicy(' in source
    assert '"run_command": ToolPolicy(' in source
    assert "required_scopes=[CAP_WRITE, CAP_DESTRUCTIVE]" in source
    assert "required_scopes=[CAP_WRITE, CAP_EXEC]" in source
    assert "TOOL_CAPABILITY = {" in source
    assert "CAP_DESTRUCTIVE in policy.required_scopes" in source
    assert "CAP_EXEC in policy.required_scopes" in source

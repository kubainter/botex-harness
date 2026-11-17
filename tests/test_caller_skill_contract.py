from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest


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


def test_bundled_caller_skill_matches_repo_skill_tree():
    from botex.skills.skills import get_bundled_caller_skill_files

    repo_skill_root = ROOT / "skills" / "botex"
    bundled_dir = ROOT / "botex" / "skills" / "bundled" / "botex"
    assert bundled_dir.is_dir()

    expected_files = {
        p.relative_to(repo_skill_root).as_posix(): p.read_text(encoding="utf-8")
        for p in repo_skill_root.rglob("*.md")
        if p.is_file()
    }
    bundled_disk_files = {
        p.relative_to(bundled_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in bundled_dir.rglob("*.md")
        if p.is_file()
    }
    loaded_files = get_bundled_caller_skill_files("botex")

    assert set(expected_files) == {
        "SKILL.md",
        "references/examples.md",
        "references/memory-vault.md",
        "references/parameters.md",
        "references/status-contract.md",
        "references/troubleshooting.md",
    }
    assert bundled_disk_files == expected_files
    assert loaded_files == expected_files
    assert "recall_memory" in loaded_files["SKILL.md"]
    assert "envfact" in loaded_files["SKILL.md"]
    assert "FormRepair" in loaded_files["references/memory-vault.md"]


def test_pyproject_packages_recipes_and_bundled_caller_skill():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_data = pyproject["tool"]["setuptools"]["package-data"]["botex.skills"]
    assert "recipes/*.md" in pkg_data
    assert "bundled/botex/*.md" in pkg_data
    assert "bundled/botex/references/*.md" in pkg_data


def test_caller_skill_excluded_from_worker_skill_vault_and_triage(tmp_path):
    from botex.skills.skills import (
        delete_skill,
        get_skill,
        install_skill,
        list_skills,
        triage_skills_for_task,
    )

    skills_root = ROOT / "skills"
    listed = list_skills(skills_dir=skills_root)
    assert listed["ok"] is True
    assert "botex" not in {s["name"].lower() for s in listed["skills"]}

    fetched = get_skill("botex", skills_dir=skills_root)
    assert fetched["ok"] is False
    assert "caller-only" in fetched["error"].lower()

    matched = triage_skills_for_task(
        task="Delegate a task to botex mcp server using skills",
        taxonomy="general",
        max_skills=10,
        skills_dir=skills_root,
    )
    assert "botex" not in {s["name"].lower() for s in matched}

    inst = install_skill(
        name="botex",
        content="---\nname: botex\ndescription: override attempt\n---\nBody",
        skills_dir=tmp_path,
    )
    assert inst["ok"] is False
    assert "caller-only" in inst["error"].lower()

    deleted = delete_skill("botex", skills_dir=tmp_path)
    assert deleted["ok"] is False
    assert "caller-only" in deleted["error"].lower()


def test_export_bundled_caller_skill_and_force_guard(tmp_path):
    import server as botex_server
    from botex.skills.skills import export_bundled_caller_skill, get_bundled_caller_skill_files

    target_root = tmp_path / "custom_skills"
    first = export_bundled_caller_skill("botex", target=target_root)
    assert first["ok"] is True
    dest_dir = Path(first["target_dir"])
    assert dest_dir == (target_root / "botex").resolve()

    canonical = get_bundled_caller_skill_files("botex")
    for rel, expected in canonical.items():
        assert (dest_dir / rel).read_text(encoding="utf-8") == expected

    # Guard against accidental overwrite without force=True
    skill_md = dest_dir / "SKILL.md"
    skill_md.write_text("# local edit", encoding="utf-8")
    second = export_bundled_caller_skill("botex", target=target_root, force=False)
    assert second["ok"] is False
    assert "force" in second["error"].lower()
    assert skill_md.read_text(encoding="utf-8") == "# local edit"

    # Overwrite succeeds when force=True
    third = export_bundled_caller_skill("botex", target=target_root, force=True)
    assert third["ok"] is True
    assert skill_md.read_text(encoding="utf-8") == canonical["SKILL.md"]

    # Path traversal target must be rejected
    bad = export_bundled_caller_skill("botex", target="../escape")
    assert bad["ok"] is False
    assert "traversal" in bad["error"].lower()

    # CLI dispatcher smoke check
    cli_target = tmp_path / "cli_skills"
    botex_server._cli_skills(["export", "--target", str(cli_target)])
    assert (cli_target / "botex" / "SKILL.md").read_text(encoding="utf-8") == canonical["SKILL.md"]


def test_mcp_resources_expose_bundled_caller_skill():
    import server as botex_server
    from botex.skills.skills import get_bundled_caller_skill_files

    canonical = get_bundled_caller_skill_files("botex")
    assert botex_server.resource_botex_caller_skill() == canonical["SKILL.md"]
    assert (
        botex_server.resource_botex_caller_skill_reference("memory-vault")
        == canonical["references/memory-vault.md"]
    )
    assert (
        botex_server.resource_botex_caller_skill_reference("memory-vault.md")
        == canonical["references/memory-vault.md"]
    )
    with pytest.raises(ValueError):
        botex_server.resource_botex_caller_skill_reference("nonexistent-ref")

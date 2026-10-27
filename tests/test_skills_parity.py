# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Skills & Recipes Parity Test Suite
========================================
Enforces strict synchronization between the public skills catalog (skills/)
and the execution kernel prompts (botex/recipes/) to prevent Split-Brain drift
and ensure cross-platform (Windows local <-> Linux VPS) handoff interoperability.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
RECIPES_DIR = REPO_ROOT / "botex" / "recipes"

# Recipes that MUST have corresponding public skill interfaces for multi-agent workflows
CORE_WORKFLOW_RECIPES: Set[str] = {
    "reviewer",
    "security-reviewer",
    "tdd",
    "build-resolver",
    "planner",
}


def parse_skill_frontmatter(content: str) -> Dict[str, str]:
    """Extract YAML-style key-value pairs from frontmatter without external pyyaml."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    res: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            res[k] = v
    return res


def get_active_skills() -> List[Path]:
    """Return all public skill directories excluding the botex caller wrapper."""
    if not SKILLS_DIR.exists():
        return []
    skill_files = []
    for skill_path in SKILLS_DIR.iterdir():
        if skill_path.is_dir() and skill_path.name != "botex":
            skill_md = skill_path / "SKILL.md"
            if skill_md.is_file():
                skill_files.append(skill_md)
    return skill_files


def test_skills_directory_exists_and_populated():
    """Verify skills directory contains the upgraded engineering skills."""
    assert SKILLS_DIR.exists() and SKILLS_DIR.is_dir()
    skills = get_active_skills()
    skill_names = {s.parent.name for s in skills}
    expected = {
        "code-review",
        "security-audit",
        "tdd-verification",
        "build-resolver",
        "architecture-planner",
    }
    assert expected.issubset(skill_names), f"Missing expected skills: {expected - skill_names}"


def test_skill_frontmatter_and_recipe_binding():
    """Verify each skill has valid metadata and points to an existing BoteX recipe."""
    skills = get_active_skills()
    assert len(skills) >= 5

    available_recipes = {f.stem for f in RECIPES_DIR.glob("*.md") if f.is_file()}

    for skill_md in skills:
        content = skill_md.read_text(encoding="utf-8")
        meta = parse_skill_frontmatter(content)
        skill_id = skill_md.parent.name

        assert "name" in meta, f"Skill '{skill_id}' missing 'name' in frontmatter"
        assert "version" in meta, f"Skill '{skill_id}' missing 'version' in frontmatter"
        assert "botex_recipe" in meta, f"Skill '{skill_id}' missing 'botex_recipe' binding"

        primary_recipe = meta["botex_recipe"]
        assert primary_recipe in available_recipes, (
            f"Skill '{skill_id}' references unknown recipe '{primary_recipe}'. Available: {available_recipes}"
        )

        if "secondary_recipe" in meta:
            sec_recipe = meta["secondary_recipe"]
            assert sec_recipe in available_recipes, (
                f"Skill '{skill_id}' references unknown secondary_recipe '{sec_recipe}'"
            )


def test_core_recipes_covered_by_skills():
    """Ensure every critical BoteX recipe is mapped to a public skill interface."""
    skills = get_active_skills()
    mapped_recipes: Set[str] = set()

    for skill_md in skills:
        meta = parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        if "botex_recipe" in meta:
            mapped_recipes.add(meta["botex_recipe"])
        if "secondary_recipe" in meta:
            mapped_recipes.add(meta["secondary_recipe"])

    missing = CORE_WORKFLOW_RECIPES - mapped_recipes
    assert not missing, f"Core recipes lack public skill representations: {missing}"


def test_portable_relative_paths_in_handoff_contracts():
    """Verify handoff templates enforce repo-relative paths with no hardcoded Windows paths."""
    skills = get_active_skills()

    for skill_md in skills:
        content = skill_md.read_text(encoding="utf-8")
        skill_id = skill_md.parent.name

        # Enforce presence of the portable path policy in section 4/5
        assert "Zasada Ścieżek Przenośnych" in content or "Repo-Relative Paths" in content, (
            f"Skill '{skill_id}' lacks portable relative path directive"
        )

        # Ban hardcoded local Windows machine paths in public skill docs
        assert "C:\\Users\\" not in content, (
            f"Skill '{skill_id}' contains hardcoded Windows drive path 'C:\\Users\\'"
        )

        # Verify structured JSON handoff presence
        assert '"artifact_refs":' in content, (
            f"Skill '{skill_id}' missing 'artifact_refs' in handoff template"
        )
        assert '"status":' in content, f"Skill '{skill_id}' missing 'status' in handoff template"

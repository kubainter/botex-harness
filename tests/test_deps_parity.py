# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Dependencies Parity Test Suite
====================================
Enforce-or-delete guard: requirements.txt is the sole install source for CI
and the VPS (.github/workflows/deploy.yml, release.yml, install.sh), while
pyproject.toml [project.dependencies] is the packaging source of truth. This
test fails when the two lists drift apart — if you intentionally change
dependencies, update BOTH files and this contract.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def _normalize_requirement(req: str) -> tuple[str, str]:
    """Split a requirement string into (name, specifier) for parity comparison."""
    req = req.split("#", 1)[0].strip().replace(" ", "")
    match = re.match(r"[A-Za-z0-9._-]+", req)
    if not match:
        return "", ""
    name = match.group(0).lower()
    spec = req[len(name) :].split(";", 1)[0]
    return name, spec


def _pyproject_core_deps() -> Dict[str, str]:
    raw = PYPROJECT.read_bytes()
    deps = tomllib.loads(raw.decode("utf-8"))["project"]["dependencies"]
    return dict(_normalize_requirement(dep) for dep in deps)


def _requirements_deps() -> Dict[str, str]:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    entries = (_normalize_requirement(line) for line in lines)
    return dict(entry for entry in entries if entry[0])


def test_requirements_in_parity_with_pyproject():
    pyproject_deps = _pyproject_core_deps()
    reqs_deps = _requirements_deps()
    missing_in_reqs = sorted(set(pyproject_deps) - set(reqs_deps))
    missing_in_pyproject = sorted(set(reqs_deps) - set(pyproject_deps))
    drift = [
        name
        for name in set(pyproject_deps) & set(reqs_deps)
        if pyproject_deps[name] != reqs_deps[name]
    ]
    problems = []
    if missing_in_reqs:
        problems.append(f"missing in requirements.txt: {missing_in_reqs}")
    if missing_in_pyproject:
        problems.append(f"missing in pyproject.toml: {missing_in_pyproject}")
    if drift:
        problems.append(
            "specifier drift: "
            + "; ".join(
                f"{name} (pyproject={pyproject_deps[name]!r}, requirements={reqs_deps[name]!r})"
                for name in sorted(drift)
            )
        )
    assert not problems, "Dependency parity violated: " + "; ".join(problems)

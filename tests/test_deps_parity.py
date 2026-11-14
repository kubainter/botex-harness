# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Dependencies Parity Test Suite
====================================
Enforce-or-delete guard: requirements.txt mirrors pyproject.toml
[project.dependencies] as the loose ">=" floor for CI/dev installs, while
requirements.lock (uv pip compile) is the pinned dependency closure deployed
to the VPS (.github/workflows/deploy.yml, deploy/oracle-vps/install.sh).
This test fails when either file drifts from pyproject — if you intentionally
change dependencies, update pyproject.toml AND requirements.txt, then
regenerate requirements.lock (command in the lock header comment).
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Dict

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_LOCK = REPO_ROOT / "requirements.lock"


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


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_pins() -> Dict[str, str]:
    """name -> pinned version for every `==` entry in requirements.lock."""
    pins: Dict[str, str] = {}
    for line in REQUIREMENTS_LOCK.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].split(";", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)==(\S+)$", entry)
        if match:
            pins[_pep503(match.group(1))] = match.group(2)
    return pins


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


def test_lockfile_pins_satisfy_pyproject_floors():
    """requirements.lock must pin every pyproject core dep at a version that
    satisfies its floor — regenerate via `uv pip compile` when deps change."""
    assert REQUIREMENTS_LOCK.is_file(), (
        "requirements.lock missing — regenerate via: "
        "uv pip compile pyproject.toml --universal --python-version 3.13 -o requirements.lock"
    )
    pyproject_deps = _pyproject_core_deps()
    pins = _lock_pins()
    missing = sorted(name for name in pyproject_deps if _pep503(name) not in pins)
    unsatisfied = sorted(
        f"{name} (floor {spec!r}, pinned {pins[_pep503(name)]!r})"
        for name, spec in pyproject_deps.items()
        if _pep503(name) in pins
        and not Requirement(f"{name}{spec}").specifier.contains(
            pins[_pep503(name)], prereleases=True
        )
    )
    problems = []
    if missing:
        problems.append(f"not pinned in requirements.lock: {missing}")
    if unsatisfied:
        problems.append("lock pins violate pyproject floors: " + "; ".join(unsatisfied))
    assert not problems, "Lockfile drift: " + "; ".join(problems)

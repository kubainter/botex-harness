# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Facade contract for the modular `botex` refactoring (Step 1).

Target layout: the flat legacy modules under ``botex/`` move into six domain
subpackages — ``core/``, ``providers/``, ``tools/``, ``memory/``, ``skills/``,
``server/``. ``botex/__init__.py`` must keep every legacy import surface working
by aliasing ``sys.modules`` entries AND setting the same objects as package
attributes, so that all of the following keep resolving to the canonical
``botex.<domain>.<name>`` module object:

* ``import botex.engine``
* ``from botex import engine``
* ``unittest.mock.patch("botex.engine.<attr>")``  (~245 patch targets in tests/)
* ``sys.modules["botex.engine"] is sys.modules["botex.core.engine"]``

Notes for the implementer:

* ``memory``/``providers``/``skills`` legacy names collide with their domain
  package names (``botex.memory`` must still expose ``botex.memory.curator``
  etc.). A uniform ``sys.modules`` alias is still achievable, e.g. by giving
  the aliased module the domain package's ``__path__`` before the swap.
* The swarm plan estimated "39 modules"; the authoritative set is the 38
  importable names discovered below (``botex/recipes/`` is a data directory,
  not a package). ``context_inclusion`` is assigned to ``core`` next to
  ``context_guard`` — the plan's list omitted it.

This test is the acceptance gate: it stays red until the domain move and the
facade land in ``botex/__init__.py``.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from unittest.mock import patch

import pytest

import botex

# legacy module name -> target domain package under botex/
LEGACY_MODULE_DOMAINS = {
    # botex/core — engine loop, state machine, contracts, governance, context pipeline
    "engine": "core",
    "state": "core",
    "contracts": "core",
    "events": "core",
    "capabilities": "core",
    "approval": "core",
    "context_guard": "core",
    "context_inclusion": "core",
    "tracing": "core",
    "meta_harness": "core",
    # botex/providers — adapters, catalog, quota/billing, delegation bridge
    "providers": "providers",
    "catalog": "providers",
    "pricing": "providers",
    "analytics": "providers",
    "wallet": "providers",
    "rate_limiter": "providers",
    "retry": "providers",
    "benchmarks": "providers",
    "denylist": "providers",
    "delegation": "providers",
    # botex/tools — filesystem/exec/net tool surface, patch engine, security
    "file_tools": "tools",
    "exec_tools": "tools",
    "net_tools": "tools",
    "patch_engine": "tools",
    "security": "tools",
    "vision": "tools",
    # botex/memory — vault, curator, embeddings, obsidian graph
    "memory": "memory",
    "curator": "memory",
    "embeddings": "memory",
    "obsidian": "memory",
    # botex/skills — skill vault, recipe loader (data dir travels with it)
    "skills": "skills",
    "recipes": "skills",
    # botex/server — cloud gateway, chat surface, plugins, config, i18n/ui
    "server_cloud": "server",
    "chat": "server",
    "plugins": "server",
    "config": "server",
    "ui": "server",
    "i18n": "server",
}

DOMAIN_PACKAGES = frozenset(LEGACY_MODULE_DOMAINS.values())

# One representative public attribute per domain for patch() resolution checks.
PATCH_TARGETS = [
    ("engine", "prune_tool_history"),
    ("providers", "is_reasoning_capable_slug"),
    ("file_tools", "read_file"),
    ("memory", "extract_wikilinks"),
    ("skills", "list_skills"),
    ("config", "load_config"),
]

_PARAMS = sorted(LEGACY_MODULE_DOMAINS.items())


def test_mapping_covers_every_importable_submodule():
    """Every name importable as ``botex.<name>`` has an explicit domain decision."""
    discovered = {
        m.name for m in pkgutil.iter_modules(botex.__path__) if not m.name.startswith("_")
    }
    unmapped = discovered - set(LEGACY_MODULE_DOMAINS) - DOMAIN_PACKAGES
    assert not unmapped, f"botex submodules missing from LEGACY_MODULE_DOMAINS: {unmapped}"


@pytest.mark.parametrize("name,domain", _PARAMS)
def test_sys_modules_alias_identity(name, domain):
    """``botex.<name>`` and ``botex.<domain>.<name>`` are the same object."""
    canonical = importlib.import_module(f"botex.{domain}.{name}")
    legacy = importlib.import_module(f"botex.{name}")
    assert sys.modules[f"botex.{domain}.{name}"] is canonical
    assert sys.modules[f"botex.{name}"] is canonical
    assert legacy is canonical


@pytest.mark.parametrize("name,domain", _PARAMS)
def test_from_botex_import_legacy_module(name, domain):
    """``from botex import <name>`` yields the canonical domain module."""
    canonical = importlib.import_module(f"botex.{domain}.{name}")
    namespace: dict = {}
    exec(f"from botex import {name}", namespace)
    assert namespace[name] is canonical


@pytest.mark.parametrize("name,attr", PATCH_TARGETS)
def test_patch_via_legacy_path(name, attr):
    """``patch("botex.<name>.<attr>")`` patches the canonical module attribute."""
    canonical = importlib.import_module(f"botex.{LEGACY_MODULE_DOMAINS[name]}.{name}")
    original = getattr(canonical, attr)
    with patch(f"botex.{name}.{attr}") as mocked:
        assert getattr(canonical, attr) is mocked
    assert getattr(canonical, attr) is original

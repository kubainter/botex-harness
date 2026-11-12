# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""BoteX — agent-agnostic autonomous code execution harness for MCP clients.

Exposes modular domain architecture (core, providers, tools, memory, skills, server)
with 100% backward-compatible sys.modules aliasing for legacy import paths.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from typing import Dict

__version__ = "2.1.0"

LEGACY_MODULE_DOMAINS: Dict[str, str] = {
    # botex/core
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
    # botex/providers
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
    "http_pool": "providers",
    # botex/tools
    "file_tools": "tools",
    "exec_tools": "tools",
    "net_tools": "tools",
    "patch_engine": "tools",
    "security": "tools",
    "vision": "tools",
    # botex/memory
    "memory": "memory",
    "curator": "memory",
    "embeddings": "memory",
    "obsidian": "memory",
    # botex/skills
    "skills": "skills",
    "recipes": "skills",
    "skillspector_gate": "skills",
    # botex/server
    "server_cloud": "server",
    "chat": "server",
    "plugins": "server",
    "config": "server",
    "ui": "server",
    "i18n": "server",
}

DOMAIN_PACKAGES = frozenset({"core", "providers", "tools", "memory", "skills", "server", "ops"})
COLLIDING_DOMAINS = frozenset({"providers", "memory", "skills"})


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, canonical_name: str, name: str, domain: str):
        self.canonical_name = canonical_name
        self.name = name
        self.domain = domain

    def create_module(self, spec):
        mod = importlib.import_module(self.canonical_name)
        sys.modules[spec.name] = mod
        pkg = sys.modules.get(__name__)
        if pkg:
            setattr(pkg, self.name, mod)
        return mod

    def exec_module(self, module):
        pass


class _LegacyFinder(importlib.abc.MetaPathFinder):
    def __init__(self, mapping: Dict[str, str]):
        self.mapping = mapping

    def find_spec(self, fullname: str, path, target=None):
        if fullname.startswith("botex."):
            parts = fullname.split(".")
            if len(parts) == 2:
                name = parts[1]
                # Never intercept domain package directory imports
                if name in DOMAIN_PACKAGES:
                    return None
                if name in self.mapping:
                    domain = self.mapping[name]
                    canonical_name = f"botex.{domain}.{name}"
                    return importlib.util.spec_from_loader(
                        fullname, _AliasLoader(canonical_name, name, domain)
                    )
        return None


_finder = _LegacyFinder(LEGACY_MODULE_DOMAINS)
if not any(isinstance(f, _LegacyFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _finder)


def __getattr__(name: str):
    if name in LEGACY_MODULE_DOMAINS:
        domain = LEGACY_MODULE_DOMAINS[name]
        canonical_name = f"botex.{domain}.{name}"
        mod = importlib.import_module(canonical_name)
        if name in COLLIDING_DOMAINS and name == domain:
            dom_pkg = sys.modules.get(f"botex.{domain}")
            if dom_pkg and hasattr(dom_pkg, "__path__"):
                mod.__path__ = dom_pkg.__path__
        sys.modules[f"botex.{name}"] = mod
        globals()[name] = mod
        return mod
    raise AttributeError(f"module 'botex' has no attribute '{name}'")


# Setup colliding aliases eagerly so domain packages and module aliases coexist
for _cname in COLLIDING_DOMAINS:
    try:
        _can_name = f"botex.{_cname}.{_cname}"
        _cmod = importlib.import_module(_can_name)
        _dom = sys.modules.get(f"botex.{_cname}")
        if _dom and hasattr(_dom, "__path__"):
            _cmod.__path__ = _dom.__path__
        sys.modules[f"botex.{_cname}"] = _cmod
        globals()[_cname] = _cmod
    except Exception:
        pass

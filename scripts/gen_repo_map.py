# SPDX-License-Identifier: Apache-2.0
"""Generate and verify docs/REPO_MAP.md from repository Python AST.

Stdlib-only and deterministic (no timestamps, no exact line counts) so CI
`--check` triggers only on structural changes: added/removed modules, public
symbols, cross-module imports, or test coverage links.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "docs" / "REPO_MAP.md"
MAX_SYMBOLS_PER_MODULE = 6
MAX_TESTS_PER_MODULE = 4
TOP_HOT_ZONES = 12


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    server_py = ROOT / "server.py"
    if server_py.exists():
        files.append(server_py)
    botex_dir = ROOT / "botex"
    if botex_dir.is_dir():
        for p in sorted(botex_dir.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def _iter_test_files() -> list[Path]:
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted(
        (p for p in tests_dir.glob("test_*.py") if "__pycache__" not in p.parts),
        key=lambda p: p.relative_to(ROOT).as_posix(),
    )


def _parse_file(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None


def _extract_public_symbols(tree: ast.Module) -> list[str]:
    classes: list[str] = []
    funcs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            funcs.append(node.name)
    combined = classes + funcs
    if len(combined) <= MAX_SYMBOLS_PER_MODULE:
        return combined
    remaining = len(combined) - MAX_SYMBOLS_PER_MODULE
    return [*combined[:MAX_SYMBOLS_PER_MODULE], f"+{remaining} more"]


def _extract_imports(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        imported.add(f"{node.module}.{alias.name}")
    return imported


def _build_alias_map(known_modules: set[str]) -> dict[str, str]:
    alias_to_canonical: dict[str, str] = {}
    for mod in known_modules:
        alias_to_canonical[mod] = mod
        parts = mod.split(".")
        # Legacy v2.1.0 flat alias: botex.<domain>.<leaf> -> botex.<leaf>
        if len(parts) == 3 and parts[0] == "botex":
            flat = f"botex.{parts[2]}"
            alias_to_canonical.setdefault(flat, mod)
            if parts[1] == parts[2]:
                alias_to_canonical.setdefault(f"botex.{parts[1]}", mod)
    return alias_to_canonical


def _resolve_imported_modules(
    raw_imports: set[str], alias_map: dict[str, str], self_mod: str = ""
) -> set[str]:
    resolved: set[str] = set()
    for imp in raw_imports:
        if imp in alias_map:
            target = alias_map[imp]
            if target != self_mod:
                resolved.add(target)
            continue
        # Match parent module if importing a symbol (e.g. botex.core.engine.run)
        parts = imp.split(".")
        for end in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:end])
            if prefix in alias_map:
                target = alias_map[prefix]
                if target != self_mod:
                    resolved.add(target)
                break
    return resolved


def _domain_of(mod: str) -> str:
    if mod == "server":
        return "root"
    parts = mod.split(".")
    if len(parts) >= 2 and parts[0] == "botex":
        return f"botex.{parts[1]}"
    return "root"


def render_repo_map() -> str:
    source_files = _iter_source_files()
    test_files = _iter_test_files()

    mod_to_path: dict[str, Path] = {}
    mod_symbols: dict[str, list[str]] = {}
    mod_raw_imports: dict[str, set[str]] = {}

    for path in source_files:
        mod = _module_name_for(path)
        if not mod:
            continue
        mod_to_path[mod] = path
        tree = _parse_file(path)
        if tree is None:
            mod_symbols[mod] = []
            mod_raw_imports[mod] = set()
            continue
        mod_symbols[mod] = _extract_public_symbols(tree)
        mod_raw_imports[mod] = _extract_imports(tree)

    known_modules = set(mod_to_path.keys())
    alias_map = _build_alias_map(known_modules)

    fan_in: dict[str, set[str]] = defaultdict(set)
    domain_deps: dict[str, set[str]] = defaultdict(set)

    for mod, raw_imps in mod_raw_imports.items():
        targets = _resolve_imported_modules(raw_imps, alias_map, self_mod=mod)
        src_dom = _domain_of(mod)
        for tgt in targets:
            fan_in[tgt].add(mod)
            tgt_dom = _domain_of(tgt)
            if tgt_dom != src_dom:
                domain_deps[src_dom].add(tgt_dom)

    mod_tests: dict[str, list[str]] = defaultdict(list)
    for tpath in test_files:
        tree = _parse_file(tpath)
        if tree is None:
            continue
        raw_imps = _extract_imports(tree)
        targets = _resolve_imported_modules(raw_imps, alias_map)
        tname = tpath.name
        for tgt in targets:
            mod_tests[tgt].append(tname)

    for mod in mod_tests:
        mod_tests[mod] = sorted(set(mod_tests[mod]))

    lines: list[str] = [
        "# BoteX Repository Map",
        "",
        "<!-- Auto-generated by scripts/gen_repo_map.py — do not edit by hand. -->",
        "<!-- Refresh with: python scripts/gen_repo_map.py -->",
        "",
        "## Project Snapshot",
        "",
        "- **What BoteX is:** Agent-agnostic MCP execution harness for autonomous code-editing",
        "  subagents, with a FastAPI Cloud Gateway, outbound WSS Edge Bridge, two-tier Memory Vault",
        "  (local + global/swarm), Skill Vault, and a Vue 3 + Vite PWA frontend (`web/`).",
        "- **Runtime & Stack:** Python `>=3.13,<3.15` (`openai`, `mcp`, `fastapi`, `httpx2`,",
        "  `websockets`, `sse-starlette`), Vue 3 + TypeScript + Pinia + Tailwind in `web/`.",
        f"- **Codebase footprint:** {len(mod_to_path)} Python modules (`server.py` + `botex/`), "
        f"{len(test_files)} test files (`tests/`).",
        "- **Change flow:** `local changes -> PR -> merge to main -> deploy to VPS` (never patch VPS",
        "  directly; `web/src/**` must never expose internal backend paths or config keys).",
        "- **Verification commands:** `ruff check .`, `ruff format --check .`,",
        "  `BOTEX_DISABLE_DOTENV=1 PYTHONPATH=. python -m pytest tests/`.",
        "- **Legacy import aliases:** Canonical `botex.<domain>.<module>` spellings are preferred",
        "  in new code; flat aliases remain grafted in `botex/__init__.py` (see `docs/IMPORT_MAP.md`).",
        "",
        "## Hot Zones (Highest Fan-In)",
        "",
        "Changes here affect the widest surface of the repository — review carefully and run",
        "targeted + integration tests.",
        "",
        "| Module | File | Imported By (modules) | Direct Tests |",
        "|---|---|---:|---:|",
    ]

    hot_ranked = sorted(
        (
            (len(fan_in.get(m, set())), m)
            for m in known_modules
            if m != "botex" and len(fan_in.get(m, set())) > 0
        ),
        key=lambda item: (-item[0], item[1]),
    )[:TOP_HOT_ZONES]

    for count, mod in hot_ranked:
        rel = mod_to_path[mod].relative_to(ROOT).as_posix()
        t_cnt = len(mod_tests.get(mod, []))
        lines.append(f"| `{mod}` | `{rel}` | {count} | {t_cnt} |")

    lines.extend(
        [
            "",
            "## Domain Dependencies",
            "",
            "| Domain | Depends On |",
            "|---|---|",
        ]
    )

    all_domains = sorted({_domain_of(m) for m in known_modules})
    for dom in all_domains:
        deps = sorted(domain_deps.get(dom, set()))
        deps_str = ", ".join(f"`{d}`" for d in deps) if deps else "*(none)*"
        lines.append(f"| `{dom}` | {deps_str} |")

    lines.extend(["", "## Modules & Test Mapping", ""])

    by_domain: dict[str, list[str]] = defaultdict(list)
    for mod in sorted(known_modules):
        by_domain[_domain_of(mod)].append(mod)

    for dom in all_domains:
        lines.append(f"### `{dom}`")
        lines.append("")
        lines.append("| File | Key Public Symbols | Tests |")
        lines.append("|---|---|---|")
        for mod in by_domain[dom]:
            rel = mod_to_path[mod].relative_to(ROOT).as_posix()
            syms = mod_symbols.get(mod, [])
            sym_str = ", ".join(f"`{s}`" for s in syms) if syms else "—"
            tests = mod_tests.get(mod, [])
            if not tests:
                test_str = "—"
            elif len(tests) <= MAX_TESTS_PER_MODULE:
                test_str = ", ".join(f"`{t}`" for t in tests)
            else:
                shown = ", ".join(f"`{t}`" for t in tests[:MAX_TESTS_PER_MODULE])
                test_str = f"{shown}, +{len(tests) - MAX_TESTS_PER_MODULE} more"
            lines.append(f"| `{rel}` | {sym_str} | {test_str} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify docs/REPO_MAP.md.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs/REPO_MAP.md is missing or out of date.",
    )
    args = parser.parse_args(argv)

    rendered = render_repo_map()
    if args.check:
        if not OUTPUT_PATH.exists():
            sys.stderr.write("docs/REPO_MAP.md is missing. Run: python scripts/gen_repo_map.py\n")
            return 1
        existing = OUTPUT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        if existing != rendered:
            sys.stderr.write(
                "docs/REPO_MAP.md is out of date. Run: python scripts/gen_repo_map.py\n"
            )
            return 1
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

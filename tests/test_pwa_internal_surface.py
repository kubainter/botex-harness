# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
PWA Internal Surface Guard Tests

Scans web/src/ for backend internal surface token leakage and asserts
synchronized version parity across the codebase.
"""

from __future__ import annotations

import re
import tomllib
import json
from pathlib import Path

import pytest


# ─── Forbidden internal surface tokens ───
FORBIDDEN_TOKENS = [
    "server_cloud.py",
    "swarm_store.py",
    "botex.config.json",
    "botex/server/",
    "botex/core/",
    "botex/edge/",
    "/opt/botex",
    "C:\\Users\\",
    "C:\\\\Users\\\\",
    "/home/",
    "host_workspaces.json",
    "active_orders.json",
    "edge_outbox.jsonl",
    "_EDGE_SESSIONS",
]

# Regex pattern for raw .py file path references under botex/
PY_PATH_PATTERN = re.compile(r"\bbotex/[a-z_/]+\.py\b", re.IGNORECASE)


def _collect_web_src_files() -> list[Path]:
    """Collect all relevant source files under web/src/."""
    web_src = Path("web/src")
    if not web_src.exists():
        return []
    extensions = {".ts", ".tsx", ".js", ".vue", ".html", ".css"}
    return [f for f in web_src.rglob("*") if f.suffix in extensions and f.is_file()]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


class TestPWAInternalSurface:
    """Guard tests for PWA internal surface leakage."""

    def test_no_forbidden_tokens_in_web_src(self):
        """Assert ZERO occurrences of backend internal surface tokens in web/src/."""
        files = _collect_web_src_files()
        assert files, "No web/src files found to scan"

        violations: list[tuple[Path, str, int, str]] = []

        for file_path in files:
            content = _read_text(file_path)
            if not content:
                continue

            lines = content.splitlines()
            for i, line in enumerate(lines, 1):
                for token in FORBIDDEN_TOKENS:
                    # Case-insensitive check for tokens that are not paths
                    if token.startswith("/") or token.startswith("C:") or "\\" in token:
                        # Path-like tokens: case-sensitive
                        if token in line:
                            violations.append((file_path, token, i, line.strip()[:120]))
                    else:
                        # Identifier-like tokens: case-insensitive
                        if token.lower() in line.lower():
                            violations.append((file_path, token, i, line.strip()[:120]))

                # Check for raw .py file path references
                py_match = PY_PATH_PATTERN.search(line)
                if py_match:
                    violations.append(
                        (file_path, f"raw .py path: {py_match.group()}", i, line.strip()[:120])
                    )

        if violations:
            details = "\n".join(
                f"  {f.relative_to('web/src')}:{ln} → {tok} | {ctx}"
                for f, tok, ln, ctx in violations
            )
            pytest.fail(
                f"Found {len(violations)} internal surface token violation(s) in web/src:\n{details}"
            )

    def test_version_parity_synchronized(self):
        """Assert synchronized version parity == '2.2.0' across all version files."""
        expected = "2.2.0"
        versions: dict[str, str] = {}

        # pyproject.toml
        pyproject = Path("pyproject.toml")
        if pyproject.exists():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            versions["pyproject.toml"] = data.get("project", {}).get("version", "")

        # botex/__init__.py
        init_py = Path("botex/__init__.py")
        if init_py.exists():
            content = init_py.read_text(encoding="utf-8")
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
            versions["botex/__init__.py"] = m.group(1) if m else ""

        # web/package.json
        pkg_json = Path("web/package.json")
        if pkg_json.exists():
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            versions["web/package.json"] = data.get("version", "")

        # web/package-lock.json
        pkg_lock = Path("web/package-lock.json")
        if pkg_lock.exists():
            data = json.loads(pkg_lock.read_text(encoding="utf-8"))
            versions["web/package-lock.json"] = data.get("version", "")

        # web/vite.config.ts
        vite_config = Path("web/vite.config.ts")
        if vite_config.exists():
            content = vite_config.read_text(encoding="utf-8")
            m = re.search(r"version:\s*['\"]([^'\"]+)['\"]", content)
            versions["web/vite.config.ts"] = m.group(1) if m else ""

        # web/src/App.vue (fallback 'v2.2.0')
        app_vue = Path("web/src/App.vue")
        if app_vue.exists():
            content = app_vue.read_text(encoding="utf-8")
            m = re.search(r"['\"]v(\d+\.\d+\.\d+)['\"]", content)
            versions["web/src/App.vue"] = m.group(1) if m else ""

        # web/src/views/SettingsView.vue (fallback '2.2.0')
        settings_vue = Path("web/src/views/SettingsView.vue")
        if settings_vue.exists():
            content = settings_vue.read_text(encoding="utf-8")
            m = re.search(r"healthData\?\.version\s*\|\|\s*['\"]([^'\"]+)['\"]", content)
            versions["web/src/views/SettingsView.vue"] = m.group(1) if m else ""

        # Verify all found versions match expected
        mismatches = {k: v for k, v in versions.items() if v != expected}
        if mismatches:
            details = "\n".join(
                f"  {k}: got '{v}', expected '{expected}'" for k, v in mismatches.items()
            )
            pytest.fail(f"Version parity mismatch (expected {expected}):\n{details}")

        # Ensure we actually checked all expected files
        expected_files = {
            "pyproject.toml",
            "botex/__init__.py",
            "web/package.json",
            "web/package-lock.json",
            "web/vite.config.ts",
            "web/src/App.vue",
            "web/src/views/SettingsView.vue",
        }
        missing = expected_files - set(versions.keys())
        if missing:
            pytest.fail(f"Missing version files: {missing}")

    def test_server_cloud_min_client_version_unchanged(self):
        """Verify server_cloud.py keeps min_client_version == '2.1.0'."""
        server_cloud = Path("botex/server/server_cloud.py")
        assert server_cloud.exists(), "botex/server/server_cloud.py not found"

        content = server_cloud.read_text(encoding="utf-8")
        # Find min_client_version in hello_ack frame
        m = re.search(r'"min_client_version"\s*:\s*["\']([^"\']+)["\']', content)
        assert m, "min_client_version not found in server_cloud.py"
        assert m.group(1) == "2.1.0", (
            f"min_client_version changed to {m.group(1)}, must remain '2.1.0'"
        )


class TestEdgeTestVersions:
    """Verify test files have synchronized server_version."""

    def test_edge_runner_server_version(self):
        """test_edge_runner.py has server_version == '2.2.0'."""
        test_file = Path("tests/test_edge_runner.py")
        assert test_file.exists(), "tests/test_edge_runner.py not found"

        content = test_file.read_text(encoding="utf-8")
        # Find all server_version occurrences
        matches = re.findall(r'"server_version"\s*:\s*"([^"]+)"', content)
        assert matches, "No server_version found in test_edge_runner.py"
        for v in matches:
            assert v == "2.2.0", f"test_edge_runner.py has server_version '{v}', expected '2.2.0'"

    def test_edge_brief_sync_server_version(self):
        """test_edge_brief_sync.py has server_version == '2.2.0'."""
        test_file = Path("tests/test_edge_brief_sync.py")
        assert test_file.exists(), "tests/test_edge_brief_sync.py not found"

        content = test_file.read_text(encoding="utf-8")
        matches = re.findall(r'"server_version"\s*:\s*"([^"]+)"', content)
        assert matches, "No server_version found in test_edge_brief_sync.py"
        for v in matches:
            assert v == "2.2.0", (
                f"test_edge_brief_sync.py has server_version '{v}', expected '2.2.0'"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re

from botex.config import (
    _dotenv_is_disabled,
    normalize_provider_name,
    resolve_model,
    resolve_provider,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPLOY_DIR = REPO_ROOT / "deploy"

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_SECRET_FALLBACK_RE = re.compile(r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\|\|")
_SSH_USER_HOST_RE = re.compile(r"\b(?:ubuntu|opc|root|admin|botex)@[A-Za-z0-9_.-]+")


def test_test_harness_disables_dotenv_loading():
    assert os.environ["BOTEX_DISABLE_DOTENV"] == "1"
    assert _dotenv_is_disabled()


def test_normalize_provider_aliases():
    assert normalize_provider_name("nvmi") == "nvidia"
    assert normalize_provider_name("NIM") == "nvidia"
    assert normalize_provider_name("or-free") == "openrouter-free"
    assert normalize_provider_name("openrouter:free") == "openrouter-free"
    assert normalize_provider_name("gemini") == "google"
    assert normalize_provider_name("bielik") == "ollama"
    prov_name, prov_cfg = resolve_provider("nvmi")
    assert prov_name == "nvidia"
    assert isinstance(prov_cfg, dict) and prov_cfg


def test_resolve_model_provider_binding():
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct", provider="openrouter-free")
        == "meta-llama/llama-3.3-70b-instruct:free"
    )
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct:free", provider="nvmi")
        == "meta/llama-3.3-70b-instruct"
    )
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct", provider="openrouter")
        == "meta-llama/llama-3.3-70b-instruct"
    )


def _find_public_ips(text: str) -> list[str]:
    found: list[str] = []
    for candidate in _IPV4_RE.findall(text):
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.is_global:
            found.append(candidate)
    return found


def test_workflows_have_no_exposed_vps_data_or_secret_fallbacks():
    workflow_files = sorted(list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml")))
    assert workflow_files, "Expected at least one GitHub workflow file"

    for wf in workflow_files:
        content = wf.read_text(encoding="utf-8")
        rel = wf.relative_to(REPO_ROOT).as_posix()
        assert not _SECRET_FALLBACK_RE.search(content), (
            f"{rel} must not define fallback literals for GitHub secrets"
        )
        public_ips = _find_public_ips(content)
        assert not public_ips, f"{rel} contains public IPv4 literal(s): {public_ips}"
        ssh_targets = _SSH_USER_HOST_RE.findall(content)
        assert not ssh_targets, f"{rel} contains hardcoded SSH user@host: {ssh_targets}"

        in_jobs = False
        jobs_without_timeout: list[str] = []
        current_job: str | None = None
        has_timeout = False
        for line in content.splitlines():
            if re.match(r"^jobs:\s*$", line):
                in_jobs = True
                continue
            if in_jobs and line and not line[0].isspace() and not line.startswith("#"):
                in_jobs = False
                if current_job and not has_timeout:
                    jobs_without_timeout.append(current_job)
                current_job = None
            if not in_jobs:
                continue
            m = re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
            if m:
                if current_job and not has_timeout:
                    jobs_without_timeout.append(current_job)
                current_job = m.group(1)
                has_timeout = False
            elif current_job and re.match(r"^    timeout-minutes:\s*\d+", line):
                has_timeout = True
        if current_job and not has_timeout:
            jobs_without_timeout.append(current_job)
        assert not jobs_without_timeout, (
            f"{rel} job(s) missing timeout-minutes: {jobs_without_timeout}"
        )


def test_security_guard_regexes_catch_leaks():
    assert _find_public_ips("VPS_HOST: '203.0.113.10'") == []
    assert _find_public_ips("http://127.0.0.1:8000 and 10.0.0.1 and 999.0.0.1") == []
    assert _find_public_ips("host=8.8.4.4") == ["8.8.4.4"]
    assert _SECRET_FALLBACK_RE.search("${{ secrets.VPS_HOST || '1.2.3.4' }}")
    assert _SSH_USER_HOST_RE.findall("ssh ubuntu@example.org") == ["ubuntu@example.org"]


def test_deploy_configs_have_no_public_ips_or_hardcoded_ssh_targets():
    assert DEPLOY_DIR.is_dir()
    for path in sorted(DEPLOY_DIR.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        public_ips = _find_public_ips(content)
        assert not public_ips, f"{rel} contains public IPv4 literal(s): {public_ips}"
        ssh_targets = _SSH_USER_HOST_RE.findall(content)
        assert not ssh_targets, f"{rel} contains hardcoded SSH user@host: {ssh_targets}"

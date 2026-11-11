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
GUARDED_DIRS = ("deploy", "docs", "botex", "skills", "web/src")
GUARDED_FILES = (
    "README.md",
    "ROADMAP.md",
    "botex.config.json",
    "botex.config.example.json",
    "server.py",
    "glama.json",
)

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_SECRET_FALLBACK_RE = re.compile(r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\|\|")
_SSH_USER_HOST_RE = re.compile(
    r"\b(?:ubuntu|opc|root|admin|botex|deploy|debian|ec2-user)@[A-Za-z0-9_.-]+"
)
_SSH_CMD_TARGET_RE = re.compile(
    r"(?:^|[\s;|&])(?:ssh|scp|rsync|sftp)[ \t]+[^\n]*?[ \t\"'=]([A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+)"
)


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


def _find_ssh_targets(text: str) -> list[str]:
    targets = _SSH_USER_HOST_RE.findall(text)
    for match in _SSH_CMD_TARGET_RE.findall(text):
        if match not in targets:
            targets.append(match)
    return targets


def _find_jobs_missing_timeout(content: str) -> list[str]:
    in_jobs = False
    jobs_without_timeout: list[str] = []
    current_job: str | None = None
    has_timeout = False
    job_indent: int | None = None

    for line in content.splitlines():
        if re.match(r"^jobs:\s*(?:#.*)?$", line):
            in_jobs = True
            continue
        if in_jobs and line and not line[0].isspace() and not line.startswith("#"):
            in_jobs = False
            if current_job and not has_timeout:
                jobs_without_timeout.append(current_job)
            current_job = None
        if not in_jobs or not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.lstrip(" ")
        if (
            job_indent is None
            and indent > 0
            and re.match(r"^[A-Za-z0-9_-]+:\s*(?:#.*)?$", stripped)
        ):
            job_indent = indent

        if job_indent is not None and indent == job_indent:
            m = re.match(r"^([A-Za-z0-9_-]+):\s*(?:#.*)?$", stripped)
            if m:
                if current_job and not has_timeout:
                    jobs_without_timeout.append(current_job)
                current_job = m.group(1)
                has_timeout = False
        elif current_job and job_indent is not None and indent > job_indent:
            if re.match(r"^timeout-minutes:\s*\d+", stripped):
                has_timeout = True

    if current_job and not has_timeout:
        jobs_without_timeout.append(current_job)
    return jobs_without_timeout


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
        ssh_targets = _find_ssh_targets(content)
        assert not ssh_targets, f"{rel} contains hardcoded SSH user@host: {ssh_targets}"

        jobs_without_timeout = _find_jobs_missing_timeout(content)
        assert not jobs_without_timeout, (
            f"{rel} job(s) missing timeout-minutes: {jobs_without_timeout}"
        )

    deploy_wf = (WORKFLOWS_DIR / "deploy.yml").read_text(encoding="utf-8")
    assert "BatchMode=yes" in deploy_wf, (
        "deploy.yml must enable SSH/SCP BatchMode=yes to avoid hangs"
    )


def test_security_guard_regexes_catch_leaks():
    assert _find_public_ips("VPS_HOST: '203.0.113.10'") == []
    assert _find_public_ips("http://127.0.0.1:8000 and 10.0.0.1 and 999.0.0.1") == []
    assert _find_public_ips("host=8.8.4.4") == ["8.8.4.4"]
    assert _SECRET_FALLBACK_RE.search("${{ secrets.VPS_HOST || '1.2.3.4' }}")
    assert _find_ssh_targets("ssh ubuntu@example.org") == ["ubuntu@example.org"]
    assert _find_ssh_targets("ssh -p 2222 customuser@my-vps.internal") == [
        "customuser@my-vps.internal"
    ]
    assert _find_ssh_targets('ssh "${VPS_USER}@${VPS_HOST}"') == []
    assert _find_ssh_targets("ssh <vps-user>@<adres-ip-serwera>") == []
    assert _find_ssh_targets("uses: webfactory/ssh-agent@v0.9.0") == []


def test_deploy_configs_and_docs_have_no_public_ips_or_hardcoded_ssh_targets():
    assert DEPLOY_DIR.is_dir()
    paths: list[Path] = []
    for rel_dir in GUARDED_DIRS:
        target = REPO_ROOT / rel_dir
        if target.is_dir():
            paths.extend(p for p in sorted(target.rglob("*")) if p.is_file())
    for rel_file in GUARDED_FILES:
        target = REPO_ROOT / rel_file
        if target.is_file():
            paths.append(target)

    for path in paths:
        if "__pycache__" in path.parts or path.suffix.lower() in {
            ".pyc",
            ".pyo",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".svg",
            ".ico",
            ".woff2",
            ".db",
        }:
            continue
        content = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert not _SECRET_FALLBACK_RE.search(content), (
            f"{rel} must not contain GitHub secret fallback literals"
        )
        public_ips = _find_public_ips(content)
        assert not public_ips, f"{rel} contains public IPv4 literal(s): {public_ips}"
        ssh_targets = _find_ssh_targets(content)
        assert not ssh_targets, f"{rel} contains hardcoded SSH user@host: {ssh_targets}"

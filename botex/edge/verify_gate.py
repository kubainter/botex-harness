# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""External verification gate, clean worktree env builder, and Test-Taint Guard."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from botex.tools.security import coerce_text, mask_secrets
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets

__all__ = [
    "ALLOWED_VERIFY_ENV",
    "build_clean_verify_env",
    "scan_test_taint",
    "append_supervisor_ledger",
    "run_external_verify",
    "default_supervisor_ledger_path",
]

ALLOWED_VERIFY_ENV: frozenset[str] = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "LANG",
        "LC_ALL",
        "TERM",
    }
)

_ALWAYS_PROTECTED_PREFIXES = (".github/", "skills/")
_ALWAYS_PROTECTED_FILES = frozenset(
    {
        "pyproject.toml",
        "Makefile",
        "tox.ini",
        "pytest.ini",
        "botex.config.json",
        "ruff.toml",
        "requirements.lock",
        "package.json",
    }
)
_TEST_DIR_PREFIXES = ("tests/", "__tests__/")

_ENV_PROBE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PYTEST_CURRENT_TEST", re.compile(r"\bPYTEST_CURRENT_TEST\b")),
    ("SWARM_VERIFY_RUN", re.compile(r"\bSWARM_VERIFY_RUN\b")),
    (
        "OS_ENVIRON_CI",
        re.compile(
            r"""(?:os\.environ(?:\.get\s*\(|[^\n]*\[|\s*\.|\b)|os\.getenv\s*\()[^\n]*['"]CI['"]"""
            r"""|['"]CI['"]\s+(?:not\s+)?in\s+os\.environ"""
        ),
    ),
)

_SKIP_XFAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)\b"),
    re.compile(r"\bpytest\.(?:skip|xfail)\s*\("),
    re.compile(r"@?unittest\.skip(?:If|Unless)?\b"),
)
_DELETED_ASSERT_RE = re.compile(r"^\s*assert\b|\bself\.assert\w*\s*\(")


def default_supervisor_ledger_path() -> Path:
    """Return the supervisor-only JSONL ledger path (never ``.agent_analytics.jsonl``)."""
    env_path = os.environ.get("BOTEX_SUPERVISOR_LEDGER_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    swarm_home = os.environ.get("BOTEX_SWARM_HOME", "").strip()
    if swarm_home:
        return Path(swarm_home).expanduser().resolve() / "supervisor_ledger.jsonl"
    return Path.home().resolve() / ".ai" / ".swarm" / "supervisor_ledger.jsonl"


def build_clean_verify_env(
    origin_repo: Path | str,
    wt_path: Path | str,
    *,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build a secret-free verification env prioritizing ``wt_path`` ahead of ``.pth``."""
    source_env = os.environ if base_env is None else base_env
    clean: dict[str, str] = {}
    for key in ALLOWED_VERIFY_ENV:
        val = source_env.get(key)
        if val is not None and str(val) != "":
            clean[key] = str(val)

    if sys.platform == "win32":
        clean.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC")

    origin_resolved = Path(origin_repo).expanduser().resolve()
    wt_resolved = Path(wt_path).expanduser().resolve()

    venv_bin = origin_resolved / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    if not venv_bin.is_dir():
        venv_bin = Path(sys.executable).resolve().parent

    existing_path = str(source_env.get("PATH") or "")
    clean["PATH"] = f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else str(venv_bin)

    pp_parts = [str(wt_resolved)]
    wt_src = wt_resolved / "src"
    if wt_src.is_dir():
        pp_parts.append(str(wt_src))
    clean["PYTHONPATH"] = os.pathsep.join(pp_parts)

    clean["BOTEX_DISABLE_DOTENV"] = "1"
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    return clean


def _normalize_rel_path(raw: str) -> str:
    norm = str(raw or "").strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    if norm.startswith(("a/", "b/")):
        norm = norm[2:]
    return norm.lstrip("/")


def _matches_always_protected(norm_path: str) -> bool:
    if not norm_path:
        return False
    if any(norm_path.startswith(pref) for pref in _ALWAYS_PROTECTED_PREFIXES):
        return True
    base = norm_path.rsplit("/", 1)[-1]
    if base in _ALWAYS_PROTECTED_FILES or norm_path in _ALWAYS_PROTECTED_FILES:
        return True
    if fnmatch.fnmatch(base, "requirements*.txt") or fnmatch.fnmatch(base, "vitest.config.*"):
        return True
    return False


def _matches_test_protected(norm_path: str) -> bool:
    if not norm_path:
        return False
    if any(norm_path.startswith(pref) or f"/{pref}" in norm_path for pref in _TEST_DIR_PREFIXES):
        return True
    if norm_path.startswith("fixtures/") or "/fixtures/" in norm_path:
        return True
    base = norm_path.rsplit("/", 1)[-1]
    return base == "conftest.py"


def _is_test_file(norm_path: str) -> bool:
    if not norm_path:
        return False
    if _matches_test_protected(norm_path):
        return True
    base = norm_path.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or ".test." in base
        or ".spec." in base
    )


def _extract_diff_paths(diff_text: str) -> set[str]:
    paths: set[str] = set()
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for cand in (parts[2], parts[3]):
                    if cand != "/dev/null":
                        paths.add(_normalize_rel_path(cand))
        elif line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip()
            if raw and raw != "/dev/null":
                paths.add(_normalize_rel_path(raw.split("\t", 1)[0]))
    return {p for p in paths if p}


def scan_test_taint(
    diff_text: str,
    changed_files: Sequence[str] = (),
    allow_test_mutation: bool = False,
) -> dict[str, Any]:
    """Scan touched files and unified diffs for protected edits, env probes, or weakened tests."""
    touched: set[str] = {_normalize_rel_path(p) for p in changed_files if str(p or "").strip()}
    touched.update(_extract_diff_paths(diff_text))

    protected_files: list[str] = []
    reasons: list[str] = []
    for path in sorted(touched):
        if _matches_always_protected(path):
            protected_files.append(path)
            reasons.append(f"protected_config_mutation:{path}")
        elif not allow_test_mutation and _matches_test_protected(path):
            protected_files.append(path)
            reasons.append(f"protected_test_mutation:{path}")

    env_probes: list[dict[str, str]] = []
    weakened_tests: list[dict[str, str]] = []

    current_file = ""
    is_new_file = False
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            is_new_file = False
            parts = line.split()
            current_file = _normalize_rel_path(parts[3]) if len(parts) >= 4 else ""
            continue
        if line.startswith("new file mode "):
            is_new_file = True
            continue
        if line.startswith("--- "):
            src = line[4:].strip().split("\t", 1)[0]
            if src == "/dev/null":
                is_new_file = True
            elif not current_file:
                current_file = _normalize_rel_path(src)
            continue
        if line.startswith("+++ "):
            dst = line[4:].strip().split("\t", 1)[0]
            if dst != "/dev/null":
                current_file = _normalize_rel_path(dst)
            continue

        if line.startswith("+"):
            added = line[1:]
            if not _is_test_file(current_file):
                for probe_name, pat in _ENV_PROBE_PATTERNS:
                    if pat.search(added):
                        env_probes.append(
                            {
                                "file": current_file or "<unknown>",
                                "probe": probe_name,
                                "line": coerce_text(mask_secrets(added.strip()))[:200],
                            }
                        )
                        reasons.append(f"env_probe:{probe_name}:{current_file or '<unknown>'}")
            elif allow_test_mutation and not is_new_file:
                for skip_pat in _SKIP_XFAIL_PATTERNS:
                    if skip_pat.search(added):
                        weakened_tests.append(
                            {
                                "file": current_file,
                                "kind": "added_skip_or_xfail",
                                "line": coerce_text(mask_secrets(added.strip()))[:200],
                            }
                        )
                        reasons.append(f"weakened_test_skip:{current_file}")
                        break
        elif line.startswith("-") and allow_test_mutation and not is_new_file:
            removed = line[1:]
            if _is_test_file(current_file) and _DELETED_ASSERT_RE.search(removed):
                weakened_tests.append(
                    {
                        "file": current_file,
                        "kind": "deleted_assert",
                        "line": coerce_text(mask_secrets(removed.strip()))[:200],
                    }
                )
                reasons.append(f"weakened_test_assert:{current_file}")

    tainted = bool(protected_files or env_probes or weakened_tests)
    return {
        "ok": not tainted,
        "tainted": tainted,
        "verdict": "NEEDS_HUMAN" if tainted else "CLEAN",
        "reasons": reasons,
        "protected_files": protected_files,
        "env_probes": env_probes,
        "weakened_tests": weakened_tests,
    }


def append_supervisor_ledger(
    record: Mapping[str, Any],
    ledger_path: Optional[Path | str] = None,
) -> Path:
    """Append a secret-masked verification entry to ``supervisor_ledger.jsonl``."""
    target = (
        Path(ledger_path).expanduser().resolve()
        if ledger_path is not None
        else default_supervisor_ledger_path()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **dict(record),
    }
    serialized = coerce_text(mask_secrets(json.dumps(entry, ensure_ascii=False, default=str)))
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(serialized + "\n")
    return target


def _split_verify_command(verify_command: str | Sequence[str]) -> list[str]:
    if not isinstance(verify_command, str):
        return [str(arg) for arg in verify_command if str(arg)]
    raw = verify_command.strip()
    if not raw or raw.lower() == "none":
        return []
    if sys.platform == "win32":
        parts = shlex.split(raw, posix=False)
        cleaned: list[str] = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
                cleaned.append(part[1:-1])
            else:
                cleaned.append(part)
        return cleaned
    return shlex.split(raw, posix=True)


def _read_git_diff_and_files(wt_path: Path) -> tuple[str, list[str]]:
    if not (wt_path / ".git").exists():
        return "", []
    diff_res = subprocess.run(
        ["git", "-C", str(wt_path), "diff", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    status_res = subprocess.run(
        ["git", "-C", str(wt_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    files: list[str] = []
    if status_res.returncode == 0:
        for line in status_res.stdout.splitlines():
            if len(line) > 3:
                candidate = line[3:].strip()
                if " -> " in candidate:
                    candidate = candidate.split(" -> ", 1)[1]
                files.append(candidate)
    return (diff_res.stdout if diff_res.returncode == 0 else ""), files


def run_external_verify(
    origin_repo: Path | str,
    wt_path: Path | str,
    verify_command: str | Sequence[str],
    *,
    order_id: str = "",
    epoch: int = 0,
    diff_text: Optional[str] = None,
    changed_files: Optional[Sequence[str]] = None,
    allow_test_mutation: bool = False,
    timeout_s: float = 300.0,
    ledger_path: Optional[Path | str] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Run ``verify_command`` in an isolated clean env after Test-Taint Guard inspection."""
    wt_resolved = Path(wt_path).expanduser().resolve()
    auto_diff, auto_files = (
        ("", [])
        if (diff_text is not None and changed_files is not None)
        else _read_git_diff_and_files(wt_resolved)
    )
    resolved_diff = auto_diff if diff_text is None else diff_text
    resolved_files = auto_files if changed_files is None else list(changed_files)

    taint = scan_test_taint(
        resolved_diff,
        resolved_files,
        allow_test_mutation=allow_test_mutation,
    )
    cmd_args = _split_verify_command(verify_command)

    if taint["tainted"] or not cmd_args:
        reason = "test_taint_detected" if taint["tainted"] else "no_verify_command"
        ledger_file = append_supervisor_ledger(
            {
                "order_id": order_id,
                "epoch": int(epoch),
                "worktree": str(wt_resolved),
                "command": cmd_args,
                "verdict": "NEEDS_HUMAN",
                "reason": reason,
                "taint": taint,
            },
            ledger_path=ledger_path,
        )
        return {
            "ok": False,
            "status": "NEEDS_HUMAN",
            "verdict": "NEEDS_HUMAN",
            "reason": reason,
            "taint": taint,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "ledger_path": str(ledger_file),
        }

    clean_env = build_clean_verify_env(origin_repo, wt_resolved, base_env=base_env)
    started = time.monotonic()
    timed_out = False
    stdout = ""
    stderr = ""
    returncode: Optional[int] = None

    try:
        proc = subprocess.run(
            cmd_args,
            cwd=str(wt_resolved),
            env=clean_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        returncode = proc.returncode
        stdout = coerce_text(mask_secrets(proc.stdout or ""))
        stderr = coerce_text(mask_secrets(proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = coerce_text(
            mask_secrets((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        )
        stderr = coerce_text(
            mask_secrets((exc.stderr or "") if isinstance(exc.stderr, str) else "")
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        verdict = "TIMEOUT"
        passed = False
    elif returncode == 0:
        verdict = "DONE"
        passed = True
    else:
        verdict = "FAILED"
        passed = False

    ledger_file = append_supervisor_ledger(
        {
            "order_id": order_id,
            "epoch": int(epoch),
            "worktree": str(wt_resolved),
            "command": cmd_args,
            "verdict": verdict,
            "returncode": returncode,
            "duration_ms": duration_ms,
            "taint": taint,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        },
        ledger_path=ledger_path,
    )
    return {
        "ok": passed,
        "status": verdict,
        "verdict": verdict,
        "taint": taint,
        "returncode": returncode,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "ledger_path": str(ledger_file),
    }

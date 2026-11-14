# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX SkillSpector Security Pre-Flight Gate
===========================================
Executes isolated, deterministic SAST, Prompt-Injection, and Taint scans on candidate
skill definitions using NVIDIA SkillSpector before physical filesystem persistence.

Guarantees:
- Zero .env Access: Clean environment allowlist strips all API keys and secrets.
- Evasion Resistance: Parses JSON output regardless of exit code (exit 1 on risk_score > 50).
- Resource Bounded: Serialized via asyncio.Semaphore(1) with strict process timeout.
- Fail-Closed Default: missing scanner blocks installs (BLOCKED_NO_SCANNER); the
  skills.audit.allow_unverified config flag restores the legacy UNVERIFIED_NO_SCANNER fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("botex.skills.skillspector_gate")

# Global semaphore to limit concurrent scans on 1 OCPU VPS nodes
_SCAN_SEMAPHORE = asyncio.Semaphore(1)

DEFAULT_TIMEOUT_S = 90.0
DEFAULT_RISK_THRESHOLD = 30


def _build_clean_env(temp_dir: str) -> Dict[str, str]:
    """
    Constructs an explicit environment allowlist devoid of all API keys,
    tokens, or harness secrets (Zero .env & secret leak protection).
    """
    allowed_keys = [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    ]
    env: Dict[str, str] = {}
    for k in allowed_keys:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v

    # Redirect cache and home paths to isolated scratch dir (satisfies systemd ProtectHome)
    env["TEMP"] = temp_dir
    env["TMP"] = temp_dir
    env["HOME"] = temp_dir
    env["USERPROFILE"] = temp_dir
    env["XDG_CACHE_HOME"] = temp_dir
    return env


async def audit_skill_content(
    raw_content: str,
    skill_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Executes a pre-flight security scan on the provided SKILL.md content.

    Returns:
        Dict with keys:
            - ok: bool (True if allowed to install, False if rejected)
            - status: "PASS" | "REJECT" | "NO_SCANNER" | "SCANNER_ERROR"
            - risk_score: int
            - verdict: str
            - findings: list of top findings
            - scanner: "SkillSpector"
            - scanned_at: ISO timestamp
            - scanner_version: str
            - reason: Optional descriptive message
    """
    cfg = config or {}
    audit_cfg = cfg.get("skills", {}).get("audit", {}) if isinstance(cfg, dict) else {}

    if not audit_cfg.get("enabled", True):
        return {
            "ok": True,
            "status": "DISABLED",
            "verdict": "UNVERIFIED_DISABLED",
            "risk_score": 0,
            "findings": [],
            "scanner": "SkillSpector",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "scanner_version": "disabled",
            "reason": "Audit gate disabled by configuration.",
        }

    binary_name = str(
        audit_cfg.get("binary_path") or os.environ.get("SKILLSPECTOR_BIN") or "skillspector"
    ).strip()
    bin_path = shutil.which(binary_name)

    # Check common fallback locations on Windows and Linux
    if not bin_path:
        local_bin_win = Path.home() / ".local" / "bin" / "skillspector.exe"
        opt_bin_linux = Path("/opt/botex/bin/skillspector")
        if local_bin_win.is_file():
            bin_path = str(local_bin_win)
        elif opt_bin_linux.is_file():
            bin_path = str(opt_bin_linux)

    if not bin_path:
        if audit_cfg.get("allow_unverified", False):
            logger.info(
                "SkillSpector binary '%s' not found on system. Falling back to UNVERIFIED_NO_SCANNER.",
                binary_name,
            )
            return {
                "ok": True,
                "status": "NO_SCANNER",
                "verdict": "UNVERIFIED_NO_SCANNER",
                "risk_score": 0,
                "findings": [],
                "scanner": "SkillSpector",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "scanner_version": "none",
                "reason": "SkillSpector binary not installed on host.",
            }
        logger.warning(
            "SkillSpector binary '%s' not found on system. Failing closed "
            "(skills.audit.allow_unverified=false).",
            binary_name,
        )
        return {
            "ok": False,
            "status": "NO_SCANNER",
            "verdict": "BLOCKED_NO_SCANNER",
            "risk_score": 0,
            "findings": [],
            "scanner": "SkillSpector",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "scanner_version": "none",
            "reason": (
                "SkillSpector scanner binary is required to audit remote skill content "
                "before install. Install the scanner binary, or set "
                "skills.audit.allow_unverified=true to accept unaudited skills."
            ),
        }

    threshold = int(audit_cfg.get("risk_threshold", DEFAULT_RISK_THRESHOLD))
    timeout_s = float(audit_cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
    max_findings = int(audit_cfg.get("max_findings", 20))

    async with _SCAN_SEMAPHORE:
        with tempfile.TemporaryDirectory(prefix="botex-audit-") as tmpdir:
            target_file = Path(tmpdir) / "SKILL.md"
            target_file.write_text(raw_content, encoding="utf-8")

            clean_env = _build_clean_env(tmpdir)
            cmd = [bin_path, "scan", tmpdir, "--format", "json", "--no-llm"]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=clean_env,
                    cwd=tmpdir,
                )
            except Exception as ex:
                logger.warning("Failed to spawn SkillSpector subprocess: %s", ex)
                return {
                    "ok": False,
                    "status": "SCANNER_ERROR",
                    "verdict": "SCANNER_ERROR",
                    "risk_score": 100,
                    "findings": [],
                    "scanner": "SkillSpector",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "scanner_version": "unknown",
                    "reason": f"Failed to execute scanner: {ex}",
                }

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                logger.error(
                    "SkillSpector scan timed out after %.1f seconds for skill '%s'",
                    timeout_s,
                    skill_name,
                )
                return {
                    "ok": False,
                    "status": "SCANNER_ERROR",
                    "verdict": "SCANNER_ERROR",
                    "risk_score": 100,
                    "findings": [],
                    "scanner": "SkillSpector",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "scanner_version": "unknown",
                    "reason": f"Skill security scan timed out after {timeout_s}s.",
                }

            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

            parsed_data: Optional[Dict[str, Any]] = None
            if stdout_text:
                try:
                    parsed_data = json.loads(stdout_text)
                except json.JSONDecodeError:
                    # Some versions output logs before JSON; attempt to find first JSON block
                    start = stdout_text.find("{")
                    end = stdout_text.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        try:
                            parsed_data = json.loads(stdout_text[start : end + 1])
                        except Exception:
                            parsed_data = None

            now_iso = datetime.now(timezone.utc).isoformat()

            if not isinstance(parsed_data, dict):
                # Scanner crashed or produced unparseable output -> Fail-Closed
                logger.error(
                    "SkillSpector failed to produce valid JSON output (rc=%d). stderr: %s",
                    proc.returncode,
                    stderr_text[:500],
                )
                return {
                    "ok": False,
                    "status": "SCANNER_ERROR",
                    "verdict": "SCANNER_ERROR",
                    "risk_score": 100,
                    "findings": [],
                    "scanner": "SkillSpector",
                    "scanned_at": now_iso,
                    "scanner_version": "unknown",
                    "reason": f"Scanner failed to return structured report (exit code {proc.returncode}).",
                }

            # Extract metadata and version across schema variants
            metadata_block = (
                parsed_data.get("metadata") if isinstance(parsed_data.get("metadata"), dict) else {}
            )
            scanner_ver = str(
                metadata_block.get("skillspector_version")
                or parsed_data.get("skillspector_version")
                or parsed_data.get("scanner_version")
                or parsed_data.get("version")
                or "2.12.0"
            )

            # Check if scanner reported internal execution failure or incomplete analysis
            analysis_completeness = (
                parsed_data.get("analysis_completeness")
                if isinstance(parsed_data.get("analysis_completeness"), dict)
                else {}
            )
            if (
                parsed_data.get("execution_successful") is False
                or parsed_data.get("status") == "failed"
                or analysis_completeness.get("is_complete") is False
            ):
                err_msg = str(
                    parsed_data.get("error")
                    or parsed_data.get("reason")
                    or "Scan analysis was incomplete or failed internally."
                )
                return {
                    "ok": False,
                    "status": "SCANNER_ERROR",
                    "verdict": "SCANNER_ERROR",
                    "risk_score": 100,
                    "findings": [],
                    "scanner": "SkillSpector",
                    "scanned_at": now_iso,
                    "scanner_version": scanner_ver,
                    "reason": f"SkillSpector reported internal failure: {err_msg}",
                }

            # Parse risk score and findings across SkillSpector schema variants
            risk_assessment = (
                parsed_data.get("risk_assessment")
                if isinstance(parsed_data.get("risk_assessment"), dict)
                else {}
            )
            raw_score = (
                risk_assessment.get("score")
                or parsed_data.get("risk_score")
                or parsed_data.get("score")
                or 0
            )
            try:
                risk_score = int(raw_score)
            except (ValueError, TypeError):
                risk_score = 0

            raw_findings = (
                parsed_data.get("issues")
                or parsed_data.get("findings")
                or parsed_data.get("vulnerabilities")
                or []
            )
            if not isinstance(raw_findings, list):
                raw_findings = []

            max_severity = str(
                risk_assessment.get("max_issue_severity") or parsed_data.get("max_severity") or ""
            ).upper()

            # Check for critical or high severity findings
            has_critical = (max_severity in ("CRITICAL", "HIGH")) or any(
                str(f.get("severity") or "").upper() in ("CRITICAL", "HIGH")
                for f in raw_findings
                if isinstance(f, dict)
            )

            is_rejected = (risk_score > threshold) or has_critical
            scanner_ver = str(
                parsed_data.get("skillspector_version")
                or parsed_data.get("scanner_version")
                or parsed_data.get("version")
                or "2.x"
            )

            # Cap findings for telemetry and API transport
            capped_findings: List[Dict[str, Any]] = []
            for f in raw_findings[:max_findings]:
                if isinstance(f, dict):
                    rule_id = str(
                        f.get("pattern")
                        or f.get("id")
                        or f.get("rule")
                        or f.get("category")
                        or "unknown"
                    )
                    explanation = str(
                        f.get("explanation")
                        or f.get("finding")
                        or f.get("message")
                        or f.get("description")
                        or ""
                    )
                    capped_findings.append(
                        {
                            "rule": rule_id,
                            "severity": str(f.get("severity") or "MEDIUM"),
                            "message": explanation,
                        }
                    )

            if is_rejected:
                reason_msg = f"Risk score {risk_score}/100 exceeds threshold {threshold}"
                if has_critical:
                    reason_msg += " (Critical/High severity findings detected)"
                return {
                    "ok": False,
                    "status": "REJECT",
                    "verdict": "REJECT",
                    "risk_score": risk_score,
                    "findings": capped_findings,
                    "total_findings": len(raw_findings),
                    "scanner": "SkillSpector",
                    "scanned_at": now_iso,
                    "scanner_version": scanner_ver,
                    "reason": reason_msg,
                }

            return {
                "ok": True,
                "status": "PASS",
                "verdict": "PASS",
                "risk_score": risk_score,
                "findings": capped_findings,
                "total_findings": len(raw_findings),
                "scanner": "SkillSpector",
                "scanned_at": now_iso,
                "scanner_version": scanner_ver,
                "reason": "Skill passed security audit.",
            }

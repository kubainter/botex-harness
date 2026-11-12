---
name: security-audit
version: "2.0.0"
description: Use when auditing code for OWASP/CWE vulnerabilities, tracing taint flows, or applying strict-scope input hardening.
taxonomy: [analysis]
metadata:
  author: Swarm Protocol Team
  tags: [security, owasp, taint-analysis, hardening, cwe]
  botex_recipe: security-reviewer
  secondary_recipe: security-taint-hardener
---

# Defensive Security Audit & Taint Analysis Skill

## Purpose

Conduct multi-language application security audits aligned with OWASP Top 10 and CWE standards, trace source-to-sink taint flows with explicit confidence labels, and apply strict-scope input hardening.

## Worker Honesty Contract

- **Strict Scope**: Operate strictly within declared target files and contract boundaries. Report any out-of-scope vulnerabilities in `findings` or `blocking_reason`; never fix or edit out-of-scope files.
- **DECIDE -> BLOCKED**: If a required capability (`exec`, `net`, `destructive`) or target file is missing, return `status: "BLOCKED"` with `blocking_reason` immediately; never bypass or fabricate. For medium-confidence reversible choices, act within scope and record them in `assumptions[]`.
- **Verification Ledger**: Label every result claim `verified` (full taint trace or observed check) or `suspected` / `unverified`. Never claim `SUCCESS` or `secure` without observed evidence from this run.
- **Environment Guard**: Never read, print, or process `.env` or secret files; always mark them as `not inspected`.

## Prerequisites

- `mode="readonly"` for `security-reviewer` audits; `mode="edit"` (strictly scoped to declared target files) for `security-taint-hardener`.
- Declared target files or endpoints in `files` / `Scope`.

## Limitations

- Static taint analysis cannot resolve all runtime reflection, dynamic imports, or external WAF/proxy rules; mark unconfirmed paths as `suspected`.
- `.env` files, private keys, and production secrets are never inspected (`not inspected`).

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)

- **MCP Tool:** `botex.start_task` or `botex.run_subagent`
- **Recipes:**
  - `security-reviewer` (`mode="readonly"`): taint analysis and vulnerability reporting.
  - `security-taint-hardener` (`mode="edit"`): strict-scope input validation and sanitization **only in declared target files**.

```markdown
### GOAL
Audit src/api/export.py for path traversal and command injection; label each finding verified or suspected.

### CONSTRAINTS & CONTEXT
- Mode: readonly (audit) or edit (strict-scope hardening on declared files only)
- Scope: [src/api/export.py]
- Environment Guard: Never read .env files; mark as not inspected.

### REFS
- Files: [src/api/export.py]
- Memory Vault Key: [security/audit-export]
```

## 2. Canonical Engineering Rules

### Filar I: Taint Flow Analysis (Sources -> Sanitizers -> Sinks)
- **Command Injection (CWE-78):** `subprocess.Popen(..., shell=True)`, `os.system()`, `child_process.exec()` -> remediate with `shell=False` / `execFile` + argument arrays + allowlists.
- **Path Traversal (CWE-22):** `open()`, `Path.joinpath()`, `fs.readFile()` -> remediate with `Path.resolve()` + `resolved.is_relative_to(base_dir)`.
- **SQL Injection (CWE-89):** String concatenation in queries -> remediate with parameterized queries.
- **Unsafe Deserialization / Eval (CWE-502 / CWE-95):** `pickle.loads()`, `yaml.load()` without `SafeLoader`, `eval()`, `exec()` -> remediate with JSON + strict DTO/Pydantic validation.

### Filar II: SSRF & Network Boundaries (CWE-918)
- Validate outbound URLs (`httpx`, `fetch`, `requests`) against loopback (`127.0.0.1`, `localhost`), RFC 1918 private ranges, and link-local metadata (`169.254.169.254`).

### Filar III: Secrets & Environment Integrity (CWE-798)
- Zero `.env` access; production code reads secrets only via `os.environ.get()`. Mask `Authorization` headers, cookies, and tokens in logs.

### Filar IV: Access Control & IDOR (CWE-639)
- Verify ownership/authorization on every resource access rather than trusting caller-supplied `user_id` or `resource_id`.

## 3. Vulnerability Report Format

Every finding MUST carry an explicit confidence label:
- **`verified`**: Full observed taint trace from external `Source` through missing/bypassed `Sanitizer` to `Sink` in inspected code.
- **`suspected`**: Probable `Source -> Sink` path where sanitizer effectiveness, upstream validation, or dynamic dispatch could not be fully confirmed.

```markdown
- **Title:** Path Traversal in export_report
- **Confidence:** verified
- **Severity:** HIGH | **CWE:** CWE-22 (OWASP A01:2021)
- **Location:** src/api/export.py:42-51
- **Taint Trace:** `request.args["file"]` -> `os.path.join(BASE_DIR, filename)` -> `open()` (no `is_relative_to` check).
- **Concrete Fix:** Resolve path and assert `resolved.is_relative_to(BASE_DIR.resolve())`.
```

## 4. Structured Handoff & Memory Contract

**Repo-Relative Paths:** All `artifact_refs` MUST be repo-relative POSIX paths (e.g., `src/api/export.py`).

```json
{
  "status": "DONE",
  "summary": "Audited src/api/export.py: 1 verified HIGH (CWE-22), 1 suspected MEDIUM (CWE-918).",
  "vulnerabilities": { "critical": 0, "high": 1, "medium": 1, "low": 0 },
  "findings": [
    {
      "cwe": "CWE-22",
      "severity": "HIGH",
      "confidence": "verified",
      "location": "src/api/export.py:42-51"
    }
  ],
  "assumptions": [
    {
      "claim": "API gateway does not strip '../' segments before forwarding",
      "status": "unverified",
      "evidence": "gateway config not inspected"
    }
  ],
  "artifact_refs": ["src/api/export.py"],
  "memory_keys": ["security/audit-export"],
  "blocking_reason": null
}
```

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `BLOCKED` on `.env` check | Task requested inspecting `.env` for leaked secrets. | Never read `.env`; mark as `not inspected` and check `os.environ.get()` usage in code. |
| Out-of-scope hardening needed | Sanitizer must be added to an undeclared upstream caller. | Report the out-of-scope file in `findings`/`blocking_reason`; harden only declared files. |
| Overstated `verified` findings | Flagged sink without tracing whether an upstream sanitizer exists. | Downgrade to `suspected` until the full `Source -> Sanitizer -> Sink` path is observed. |

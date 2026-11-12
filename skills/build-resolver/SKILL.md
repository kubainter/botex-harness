---
name: build-resolver
version: "2.0.0"
description: Use when diagnosing and fixing compilation, type-checker, or linter failures with minimal diffs and verified re-build execution.
taxonomy: [bugfix]
metadata:
  author: Swarm Protocol Team
  tags: [build, compiler, typecheck, troubleshooting, minimal-diff]
  botex_recipe: build-resolver
---

# Build & Type-Error Resolver Skill

## Purpose

Diagnose and repair compilation, type-checking, and linting failures using minimal, surgical diffs followed by mandatory re-build verification via `run_command`.

## Worker Honesty Contract

- **Strict Scope**: Operate strictly within declared target files and contract boundaries. Report any out-of-scope issues in `findings` or `blocking_reason`; never fix or edit out-of-scope files.
- **DECIDE -> BLOCKED**: If a required capability (`exec`, `net`, `destructive`) or prerequisite is missing, return `status: "BLOCKED"` with `blocking_reason` immediately; never bypass or fabricate. For medium-confidence reversible choices, act within scope and record them in `assumptions[]`.
- **Verification Ledger**: Label every result claim `verified` (with observed command + `exit_code`), `partially_verified`, or `unverified`. Never claim `SUCCESS`, `GREEN`, or `works` without observed evidence from this run.
- **Environment Guard**: Never read, print, or process `.env` or secret files; always mark them as `not inspected`.

## Prerequisites

- Execution capability enabled: `mode="full"` (or `mode="edit"` + `allow_exec=true` + `verify_command`) with `exec.enabled=true` on the BoteX server.
- Reproducible build, type-check, or lint command (e.g., `mypy botex/`, `npm run build`, `cargo check`).

## Limitations

- Cannot verify fixes if `allow_exec` is disabled; in `edit`-only mode without `run_command`, the worker MUST return `BLOCKED` rather than claiming the build is fixed.
- Does not perform architectural refactoring, dependency upgrades, or formatting sweeps outside the failing symbols.

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)

- **MCP Tool:** `botex.start_task` or `botex.run_subagent`
- **Recipe:** `build-resolver`
- **Execution Mode (Critical):** MUST use `mode="full"` OR `mode="edit"` with `allow_exec=true` and `verify_command` set. Do NOT use `edit`-only, because re-build verification requires `run_command`.

```markdown
### GOAL
Diagnose and fix the build/type error for `mypy botex/` with minimal diff and verified re-build.

### CONSTRAINTS & CONTEXT
- Minimal Diff: Only touch lines required to resolve the compiler/type error.
- Scope: [botex/server_cloud.py]

### REFS
- Verify Command: `mypy botex/`
- Memory Vault Key: [build/repair-server-cloud]
```

## 2. Canonical Engineering Rules

### Filar I: Minimal Diff Invariant
- Single objective: restore green build (`exit_code: 0`).
- Zero refactoring, zero style reformatting, zero unrelated cleanup.

### Filar II: Root-Cause Diagnosis
1. Inspect the **first** error in the build output (downstream errors are often cascading).
2. Classify the failure: missing import/dependency, type mismatch / signature drift, language version syntax mismatch, or circular import.

### Filar III: Verification Loop
1. **Reproduce:** Run the failing command via `run_command` and record exact error + exit code.
2. **Surgical Patch:** Apply minimal fix via `apply_patch`.
3. **Re-Build:** Re-run the exact same command; `build_status.status` is `"verified"` ONLY when observed `exit_code == 0`.

## 3. Stack-Specific Repair Patterns

- **Python / Mypy / Pyright:** `Item "None" of "Optional[X]" has no attribute "Y"` -> add guard clause `if val is None: raise ValueError(...)` or narrowing check.
- **TypeScript / ESLint:** `Type 'X | undefined' is not assignable to type 'X'` -> optional chaining `?.`, nullish coalescing `??`, or type guard.
- **Rust / Cargo:** `cannot borrow as mutable more than once` -> narrow borrow scope `{ ... }`, pass `&`, or `.clone()` if appropriate.

## 4. Structured Handoff & Memory Contract

**Repo-Relative Paths:** All `artifact_refs` MUST be repo-relative POSIX paths (e.g., `botex/server_cloud.py`).

```json
{
  "status": "DONE",
  "summary": "Fixed Optional[str] narrowing in botex/server_cloud.py; mypy exited 0.",
  "build_status": {
    "command": "mypy botex/",
    "exit_code": 0,
    "status": "verified"
  },
  "assumptions": [
    {
      "claim": "None value on config_path is an invalid caller state",
      "status": "verified",
      "evidence": "botex/server_cloud.py:42 raises ValueError on empty path"
    }
  ],
  "artifact_refs": ["botex/server_cloud.py"],
  "memory_keys": ["build/repair-server-cloud"],
  "blocking_reason": null
}
```

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `BLOCKED` (missing `run_command`) | Delegated with `mode="edit"` without `allow_exec=true`. | Re-dispatch with `mode="full"` or `mode="edit"` + `allow_exec=true` + `verify_command`. |
| `VERIFICATION_FAILED` | Patch fixed primary error but exposed secondary type error or failed `verify_command` 3 times. | Inspect scratchpad logs, address the next root error, and re-run. |
| Out-of-scope file fails build | Error originates in a file outside declared `Scope`. | Report the out-of-scope file in `blocking_reason` and return `BLOCKED`; do not edit undeclared files. |

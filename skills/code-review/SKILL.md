---
name: code-review
version: "2.0.0"
description: Use when performing read-only code reviews to detect regressions, silent failures, code slop, and contract drift.
taxonomy: [analysis]
metadata:
  author: Swarm Protocol Team
  tags: [code-review, silent-failures, reliability, quality-gate]
  botex_recipe: reviewer
---

# Code Review & Silent Failure Hunting Skill

## Purpose

Perform exhaustive, read-only code reviews across Python, TypeScript/JS, Rust, and Go to catch silent failures, edge-case regressions, contract drift, and AI-generated code slop before merge.

## Worker Honesty Contract

- **Strict Scope**: Operate strictly within declared target files and contract boundaries. Report any out-of-scope issues in `findings` or `blocking_reason`; never fix or edit out-of-scope files.
- **DECIDE -> BLOCKED**: If a required capability or target file is missing/unreadable, return `status: "BLOCKED"` with `blocking_reason` immediately; never bypass or fabricate. For medium-confidence observations, record them in `assumptions[]`.
- **Verification Ledger**: Label every result claim `verified` (with observed file/line evidence or command output), `partially_verified`, or `unverified`. Never claim `SUCCESS` or `clean` without inspecting the declared files.
- **Environment Guard**: Never read, print, or process `.env` or secret files; always mark them as `not inspected`.

## Prerequisites

- Read access to target files in `workspace_dir` using `mode="readonly"`.
- Verifier Firewall inputs: the artifact/diff, the governing contract, and the review checklist.

## Limitations

- Runs in `mode="readonly"`; cannot edit files or execute test suites unless a separate verification step is run.
- Static inspection cannot prove runtime concurrency or external service behavior; label unexecuted runtime paths as `suspected` or `unverified`.

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)

- **MCP Tool:** `botex.start_task` or `botex.run_subagent`
- **Recipe:** `reviewer` (`mode="readonly"`)
- **Verifier Firewall:** Pass ONLY the target artifact/files, the task contract, and the review checklist. NEVER pass the builder agent's reasoning, excuses, or self-reported "all tests pass" claims.

```markdown
### GOAL
Review the modified modules for regressions, silent failures, contract drift, and code slop.

### CONSTRAINTS & CONTEXT
- Mode: readonly (no file mutations)
- Scope: [botex/engine.py, tests/test_engine.py]
- Verifier Firewall: Evaluate only observed code against contract and checklist.

### REFS
- Files: [botex/engine.py]
- Memory Vault Key: [audit/code-review-engine]
```

## 2. Canonical Engineering Rules

**Core Rule:** **`pattern without observation != finding`**. Every reported finding MUST cite the exact observed file, line range, and concrete defect mechanism.

### Filar I: Silent Failure Hunter
- Exception swallowing (`except Exception: pass`, empty `catch (e) {}`), hidden fallback drift (`[]`, `{}`, `None` masking upstream errors), detached async tasks, and unclosed resources.

### Filar II: Logical Correctness & Edge Cases
- Off-by-one bounds, empty collections, extreme values (`0`, `-1`), race conditions, and encoding/newline/timezone mismatches.

### Filar III: Contract Integrity & Backward Compatibility
- Breaking signature or return-type changes, removed optional parameters, and unintended global state mutations.

### Filar IV: Test Hygiene & Mock Hallucinations
- Mock theater (testing mocks instead of real logic) and missing failure-before-fix proof.

### Filar V: Code-Slop Detection (4 Mandatory Patterns)
1. **Debug Residue:** Leftover `print()`, `console.log()`, `dbg!()`, or commented-out debug blocks.
2. **TODO-as-Completion:** `TODO`, `FIXME`, `pass`, or `NotImplementedError` stubs shipped as completed logic.
3. **Dead Abstraction:** Single-use wrappers, unused interfaces/factories, or pointless indirection layers.
4. **Defensive Theater:** Redundant `try/except` or null checks around impossible states that mask real bugs.

## 3. Direct Execution & Severity Classification

1. **Reconnaissance:** Use outlines/search to locate modified symbols.
2. **Wide Read:** Read full function and caller context (never 5-line fragments).
3. **Severity:** `[CRITICAL]` (blocking regression/data loss), `[MAJOR]` (silent failure/resource leak), `[MINOR]` (missing logging/typing drift), `[NITPICK]` (style only).

## 4. Structured Handoff & Memory Contract

**Repo-Relative Paths:** All `artifact_refs` MUST be repo-relative POSIX paths (e.g., `botex/engine.py`).

```json
{
  "status": "DONE",
  "summary": "Inspected 2 files; found 1 MAJOR silent-failure fallback and 1 MINOR debug residue.",
  "findings_count": { "critical": 0, "major": 1, "minor": 1 },
  "findings": [
    {
      "severity": "MAJOR",
      "location": "botex/engine.py:112-118",
      "verification": "verified",
      "detail": "Bare except Exception returns {} and masks JSONDecodeError."
    }
  ],
  "assumptions": [],
  "artifact_refs": ["botex/engine.py"],
  "memory_keys": ["audit/code-review-engine"],
  "blocking_reason": null
}
```

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `CONFIG_ERROR` | `output_path` or `allow_exec=true` passed with `mode="readonly"`. | Omit `output_path` and mutating flags; deliver findings in `summary`. |
| False-positive findings | Flagging patterns from memory without reading full function context. | Enforce `pattern without observation != finding` via wide `read_file_lines`. |
| Biased review approval | Builder's reasoning or self-certification leaked into reviewer prompt. | Apply Verifier Firewall: pass only artifact, contract, and checklist. |

---
name: tdd-verification
version: "2.0.0"
description: Use when implementing features or bug fixes via strict Red-Green-Refactor TDD with verified test execution.
taxonomy: [test]
metadata:
  author: Swarm Protocol Team
  tags: [tdd, testing, verification, red-green-refactor, quality-assurance]
  botex_recipe: tdd
---

# Intent-Driven TDD & Verification Skill

## Purpose

Enforce strict Red-Green-Refactor Test-Driven Development with observed test-runner evidence per phase, zero mock theater, and strict adherence to existing repository test conventions.

## Worker Honesty Contract

- **Strict Scope**: Operate strictly within declared target files and contract boundaries. Report any out-of-scope issues in `findings` or `blocking_reason`; never fix or edit out-of-scope files.
- **DECIDE -> BLOCKED**: If a required capability (`exec`, `net`, `destructive`) or test runner is missing, return `status: "BLOCKED"` with `blocking_reason` immediately; never bypass or fabricate. For medium-confidence reversible choices, act within scope and record them in `assumptions[]`.
- **Verification Ledger**: Label every result claim `verified` (with observed command + `exit_code`), `partially_verified`, or `unverified`. Never claim `SUCCESS`, `GREEN`, or `works` without observed evidence from this run.
- **Environment Guard**: Never read, print, or process `.env` or secret files; always mark them as `not inspected`.

## Prerequisites

- Execution capability enabled: `mode="full"` (or `mode="edit"` + `allow_exec=true` + `verify_command`) with `exec.enabled=true` on the BoteX server.
- Existing test runner configured in the repository (e.g., `pytest`, `vitest`, `cargo test`, `go test`).

## Limitations

- Cannot verify Red, Green, or Refactor phases without `run_command`; in `edit`-only mode without `allow_exec=true`, the worker MUST return `BLOCKED`.
- Cannot validate external third-party APIs unless deterministic local fakes/contracts are provided in scope.

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)

- **MCP Tool:** `botex.start_task` or `botex.run_subagent`
- **Recipe:** `tdd`
- **Execution Mode (Critical):** MUST use `mode="full"` OR `mode="edit"` with `allow_exec=true` (and `verify_command`). Do NOT use `edit`-only, because TDD requires running tests via `run_command`.

```markdown
### GOAL
Implement retry backoff in src/client.py using strict Red-Green-Refactor TDD.

### CONSTRAINTS & CONTEXT
- Mode: edit + allow_exec=true (or full)
- Scope: [src/client.py, tests/test_client.py]
- Match existing pytest conventions; forbid TODO-stubs or debug residue in shipped tests.

### REFS
- Verify Command: `pytest tests/test_client.py -v`
- Memory Vault Key: [tdd/verification-client-retry]
```

## 2. Canonical Engineering Rules (Red-Green-Refactor)

Match the repository's existing test framework, fixture style, and naming conventions. **Forbid `TODO`/`FIXME` stubs, `pass` placeholders, and debug residue (`print`, `console.log`) in shipped tests.**

### Phase 1: RED (Fact-Forcing Failure Proof)
1. Write a test modeling the expected behavior or reproducing the bug.
2. Run the test via `run_command` BEFORE editing production code; confirm non-zero exit code (`FAILED`) for the **expected** reason (`AssertionError`, missing behavior), not syntax/import errors.

### Phase 2: GREEN (Minimal Implementation)
1. Write only the minimal production code needed to pass the new test.
2. Run the test suite via `run_command`; confirm `exit_code: 0` with zero regressions.

### Phase 3: REFACTOR (Hygiene & Edge Cases)
1. Clean up duplication, naming, and typing; add edge-case assertions (empty, `None`, error paths).
2. Re-run the test suite via `run_command`; confirm `exit_code: 0`.

## 3. Anti-Patterns & Zero Test Theater Guard

- **Mocking the Subject:** Never mock the unit under test.
- **Assertion-Free Tests:** Every test must assert concrete output or state changes.
- **Sleep-Based Sync:** Replace `time.sleep()` with deterministic events/conditions.

## 4. Execution Workflow

- **Python:** `pytest tests/test_feature.py -k test_name -v`
- **TypeScript / Node:** `npx vitest run tests/feature.test.ts` or `npm test -- tests/feature.test.ts`
- **Rust:** `cargo test test_name`
- **Go:** `go test -v -run TestName ./...`

## 5. Structured Handoff & Memory Contract

**Repo-Relative Paths:** All `artifact_refs` MUST be repo-relative POSIX paths (e.g., `tests/test_client.py`).

```json
{
  "status": "DONE",
  "summary": "Verified RED failure, implemented backoff in src/client.py, all 12 tests GREEN after refactor.",
  "test_results": {
    "red_verified": true,
    "green_verified": true,
    "refactor_verified": true,
    "command": "pytest tests/test_client.py -v",
    "exit_code": 0,
    "passed": 12,
    "failed": 0,
    "suite_status": "verified"
  },
  "assumptions": [],
  "artifact_refs": ["tests/test_client.py", "src/client.py"],
  "memory_keys": ["tdd/verification-client-retry"],
  "blocking_reason": null
}
```

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `BLOCKED` (missing `run_command`) | Delegated with `mode="edit"` without `allow_exec=true`. | Re-dispatch with `mode="full"` or `mode="edit"` + `allow_exec=true`. |
| Test passes in RED phase | Feature already exists or test asserts trivial/mocked behavior. | Fix test assertions to target real unimplemented behavior before editing source. |
| `VERIFICATION_FAILED` | Existing suite regressed or new test failed after 3 attempts. | Inspect scratchpad output, restore minimal diff, and re-verify. |

---
name: architecture-planner
version: "2.0.0"
description: Use when designing system architecture, decomposing interfaces, or authoring Architecture Decision Records (ADRs) before implementation.
taxonomy: [general]
metadata:
  author: Swarm Protocol Team
  tags: [architecture, planning, adr, system-design, interface-design]
  botex_recipe: planner
  secondary_recipe: architecture-adr
---

# Architecture Planning & ADR Skill

## Purpose

Provide spec-first architectural blueprinting, bounded interface decomposition, and Architecture Decision Record (ADR) authoring before code changes begin, preventing scope creep, interface drift, and unverified blast-radius assumptions.

## Worker Honesty Contract

- **Strict Scope**: Operate strictly within declared target files and contract boundaries. Report any out-of-scope issues in `findings` or `blocking_reason`; never fix or edit out-of-scope files.
- **DECIDE -> BLOCKED**: If a required capability (`exec`, `net`, `destructive`) or prerequisite is missing, return `status: "BLOCKED"` with `blocking_reason` immediately; never bypass or fabricate. For medium-confidence reversible choices, act within scope and record them in `assumptions[]`.
- **Verification Ledger**: Label every result claim `verified` (with observed evidence such as inspected files/imports), `partially_verified`, or `unverified`. Never claim `SUCCESS` or `verified` without observed evidence from this run.
- **Environment Guard**: Never read, print, or process `.env` or secret files; always mark them as `not inspected`.

## Prerequisites

- Target workspace directory accessible in `readonly` (for `planner`) or `edit` (for `architecture-adr` writing ADR markdown).
- Clear feature specification or refactoring goal in the dispatch contract.

## Limitations

- `planner` runs in `mode="readonly"` and cannot modify source files or execute tests.
- Blast-radius claims are limited to statically observable imports and references within the inspected workspace; dynamic/runtime wiring not inspected must be marked `unverified`.

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)

- **MCP Tool:** `botex.start_task` or `botex.run_subagent`
- **Recipes:**
  - `planner` (`mode="readonly"`): repository reconnaissance and phased implementation plan.
  - `architecture-adr` (`mode="edit"` or `"readonly"`): formalized ADR document.
- **Precedence Rule:** `contract > repo > vault > defaults` (explicit task contract overrides repo conventions, which override Memory Vault notes, which override generic defaults).

```markdown
### GOAL
Design the architecture and phased implementation plan for: [feature or refactoring].

### CONSTRAINTS & CONTEXT
- Scope: [target modules and dependencies]
- Precedence: contract > repo > vault > defaults
- Bounded Intent: Evaluate 2-3 materially different decompositions before selecting one.

### REFS
- Files: [src/domain/interfaces.py]
- Memory Vault Key: [architecture/plan-feature-x]
```

## 2. Canonical Engineering Rules

### Filar I: Spec-First (Interfaces Before Code)
- Define method signatures, data models (Pydantic / TypeScript interfaces / structs), and JSON payloads before implementation.
- Lock contracts early so parallel workers do not diverge.

### Filar II: Bounded Intent & Phased Delivery
- **Bounded Intent:** Explore **2–3 materially different decompositions** (e.g., monolith module vs adapter-port vs event-driven) and capture their trade-offs in the ADR `Context` and `Consequences` sections.
- **Phased Delivery:** Split delivery into independently testable phases:
  1. **Phase 0 (Recon & Spikes):** Verify assumptions, inspect existing code and dependencies.
  2. **Phase 1 (Data & Contracts):** Define types, schemas, and interfaces.
  3. **Phase 2 (Core Business Logic & TDD):** Pure logic with unit tests.
  4. **Phase 3 (Integration & Adapters):** Wire API, DB, CLI, or event bus.
  5. **Phase 4 (Verification & Observability):** End-to-end tests, metrics, logging.

### Filar III: Evidence-Backed Blast Radius
- **Observed Imports Only:** Every blast-radius claim MUST cite observed imports or call sites from inspected files (e.g., `src/api/routes.py:14 imports BillingService`). Uninspected callers must be listed as `unverified`.
- **Backward Compatibility & Rollback:** Document breaking API changes, migration paths or feature flags, and a concrete rollback strategy.

## 3. ADR Format

```markdown
# ADR-[NUM]: [Decision Title]

## Status
PROPOSED | ACCEPTED | DEPRECATED | SUPERSEDED

## Context
Problem statement, technical/business constraints, and the 2-3 candidate decompositions evaluated.

## Decision
Selected architecture, interfaces, and rationale.

## Consequences
- Positive: [benefits]
- Negative / Trade-offs: [accepted costs]
- Blast Radius (Observed): [file:line imports/references]
```

## 4. Structured Handoff & Memory Contract

**Repo-Relative Paths:** All `artifact_refs` MUST be repo-relative POSIX paths (e.g., `docs/adr/ADR-004.md`).

```json
{
  "status": "DONE",
  "summary": "Evaluated 3 decompositions; selected port-adapter plan in 4 phases.",
  "plan_overview": {
    "total_phases": 4,
    "breaking_changes": false,
    "risk_level": "MEDIUM"
  },
  "assumptions": [
    {
      "claim": "BillingService is only imported by src/api/routes.py:14",
      "status": "verified",
      "evidence": "grep/read_file on src/api/routes.py"
    },
    {
      "claim": "Production queue supports idempotent retries",
      "status": "unverified",
      "evidence": "infra config not inspected"
    }
  ],
  "artifact_refs": ["docs/adr/ADR-004.md"],
  "memory_keys": ["architecture/plan-billing-v2"],
  "blocking_reason": null
}
```

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `DONE_WITHOUT_WRITE` | `output_path` set for ADR but task ran in `mode="readonly"` or file was empty. | Use `mode="edit"` when writing an ADR file, or omit `output_path` for `readonly` plans. |
| `BLOCKED` | Required module or dependency outside `workspace_dir` cannot be inspected. | Return `status: "BLOCKED"` with `blocking_reason` and list uninspected paths as `unverified`. |
| Interface drift between parallel workers | Contracts omitted concrete types or precedence order was ambiguous. | Define explicit DTO schemas and apply `contract > repo > vault > defaults`. |

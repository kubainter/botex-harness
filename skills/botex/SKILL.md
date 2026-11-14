---
name: botex
version: "2.3.0"
description: Use when delegating bounded code inspection, surgical patching, verification, or Memory Vault operations to the BoteX MCP harness.
taxonomy: [general]
metadata:
  author: Swarm Protocol Team
  tags: [botex, mcp, delegation, sandbox, memory-vault]
---

# BoteX Caller Skill

## Purpose

Define the normative caller-facing contract for AI orchestrators delegating bounded code inspection, surgical edits, command verification, and Memory Vault operations to the BoteX MCP execution harness.

## Prerequisites

- BoteX MCP server running and reachable (`check_health()` returns ready).
- Target `workspace_dir` is reachable by the **BoteX server host**:
  - For **remote BoteX Cloud** (`https://.../mcp/sse`), NEVER pass a local workstation path (`C:\Users\...` or unpushed local repo path) — the server falls back to its own VPS checkout (`/opt/botex`, deployed `main`).
  - To inspect or edit local/unpushed workstation files from BoteX Cloud, an outbound `botex edge` runner must be connected (`docs/EDGE_BRIDGE.md`) and `workspace_dir` MUST be set to `"edge://<device_id>/<workspace_id>"` (or `"<workspace_id>"`). If no edge runner is online, do not delegate local-file work to Cloud.
- Explicit capability flags when required: `exec.enabled=true` on server for `mode="full"` / `allow_exec=true`.

## Limitations

- Headless execution only: callers cannot hold mid-run interactive dialogues with a worker.
- `readonly` and `chat` are hard read-only modes; `chat` is not a programmatic delegation API.
- Protected secret files (`.env*`, `*.pem`, `*.key`, `id_rsa`) are blocked by DLP filters and never read or written.

## 1. Delegation Rules & Verifier Firewall

Delegate to BoteX (`run_subagent` synchronous or `start_task` + `get_task_status` asynchronous) for bounded file inspection, surgical patching, test-verified bug fixes, or Memory Vault operations. Do **not** delegate general knowledge questions, server/daemon administration, or vague unbounded requests.

- **Verifier Firewall:** When delegating a review or verification task (`reviewer`, `security-reviewer`), pass ONLY the target artifact/files, the governing contract, and the review checklist. NEVER pass the builder agent's reasoning, excuses, or self-reported "all tests pass" claims.
- **Fail-Stop Protocol:** If BoteX terminates with `BLOCKED` or `UNSUPPORTED`, do NOT blindly retry the identical prompt; inspect `failure_kind` and fix permissions/constraints or escalate.
- **Verified Ledger:** A task is `verified` ONLY when `ok == true && status == "DONE"` (`result_data` for `start_task`, `structuredContent` for `run_subagent`). Lifecycle `status == "DONE"` alone only means the run finished.
- **Engine Statuses:** `ABORTED`, `API_ERROR`, `BLOCKED`, `BUDGET_EXCEEDED`, `CONFIG_ERROR`, `DONE`, `DONE_WITHOUT_WRITE`, `ERROR`, `FAILED`, `INCOMPLETE`, `MAX_TURNS_REACHED`, `PRICE_EXCEEDED`, `REQUEST_TIMEOUT`, `STAGNANT_ROLLBACK`, `TIME_LIMIT`, `TOKEN_LIMIT`, `UNSUPPORTED`, `VERIFICATION_FAILED`, `ZDR_VIOLATION` (see [status-contract.md](references/status-contract.md) for rollback rules).

## 2. Mode Decision Tree & Security Boundaries

Choose the least-privileged `mode`:
- **`readonly`**: Purely informational/auditing (`list_dir`, `get_file_outline`, `read_file`, `read_file_lines`, plus `read_url` if `allow_net=true`). Never pass `allow_exec`, `allow_destructive`, or `output_path`.
- **`edit`** *(default)*: Create/modify files (`apply_patch`, `create_file`). Add `allow_exec=true` + `verify_command` when tests/builds must run.
- **`destructive`**: File deletions/moves (`delete_file`, `move_file`) with snapshot backup.
- **`full`**: All `destructive` tools + `run_command` for autonomous edit-test-fix loops.

BoteX enforces **Read-Before-Write** on existing files, confines all paths to `workspace_dir`, masks secrets via DLP, and fails closed (`BLOCKED`) in headless mode if an operation lacks pre-authorization (`approval_policy` does not grant capabilities).

## 3. Caller vs Internal Worker Tools

Caller-facing MCP tools (20):
MCP tools: `check_health`, `clean_snapshots`, `delete_memory`, `fetch_url`, `find_graph_health`, `get_backlinks`, `get_outline`, `get_stats`, `get_task_status`, `init_obsidian_vault`, `patch_memory_section`, `read_memory`, `read_scratchpad`, `recall_memory`, `recommend_models`, `reflect_memory`, `run_subagent`, `save_memory`, `search_memory`, `start_task`
- **Internal Worker Tools (Sandbox Only):** `get_file_outline`, `read_file_lines`, `read_file`, `list_dir`, `apply_patch`, `create_file`, `delete_file`, `move_file`, `run_command`, `read_url`, `read_scratchpad`. Callers MUST NOT invoke internal worker tools over MCP.

## 4. Concrete Usage Examples

### Read-Only Audit (`mode="readonly"`)
```json
{
  "task": "Audit src/auth/jwt.py for signature bypasses and algorithm confusion.",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/auth/jwt.py"],
  "mode": "readonly",
  "recipe": "security-reviewer",
  "api_key": ""
}
```

### Edit with Test Verification (`mode="edit"` + `allow_exec=true`)
```json
{
  "task": "Fix decimal rounding in src/billing.py and verify with pytest.",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/billing.py", "tests/test_billing.py"],
  "mode": "edit",
  "verify_command": "pytest tests/test_billing.py",
  "allow_exec": true,
  "api_key": ""
}
```

## 5. Reference Documentation (Progressive Disclosure)

- [Caller Parameter Contract & Capability Presets](references/parameters.md)
- [Canonical JSON Delegation Examples](references/examples.md)
- [Result, Status Taxonomy & Verified Ledger](references/status-contract.md)
- [Two-Tier Memory Vault Workflow](references/memory-vault.md)
- [Extended Troubleshooting & Remedies](references/troubleshooting.md)

## 6. Self-Improvement Loop (Caller-Relevant)

BoteX records every run in a local analytics ledger and feeds that evidence back deterministically:

- **Result attribution:** `result_data` carries `applied_skills` (server-side Skill Vault entries auto-injected by L0 triage), `routed_tier`/`routed_profile` (when `engine.task_size_routing` assigned a tier), `attempts` (fallback audit log), and `syntax_rejections_detail`/`rollback_reason` on quality failures. The ledger also records `taxonomy`, `applied_lessons`, `rollback_count`, and `shadow_recommended_model`.
- **Vault lifecycle:** `kind="lesson"` notes carry `status` (`candidate` -> `active` -> `deprecated`/`superseded`) and a `confidence` score. `search_memory` defaults to `status="active"`; pass `status="all"` to include candidates and deprecated notes. Engine-extracted lessons start as `candidate` and only the Curator promotes them to `active`, so callers do not manage lifecycle directly.
- **Provenance:** on BoteX Cloud, `save_memory`/`POST /api/memory` derive `origin` server-side from sanitized `X-Agent-Id`/`X-Host` headers; reserved claims (`human`, `human-curated`, `operator`, `curator`) are rejected. Never claim a reserved origin.
- **Skill proposals:** the Curator may draft `learned-*` `SKILL.md`/`recipe.md` proposals under `<workspace>/.botex/proposals/` only. The loop never mutates live `skills/` or `botex/skills/recipes/`; promotion happens via a reviewed PR, so do not delegate writes into live skill directories as a promotion path.
- **Shadow routing:** the engine's per-`(recipe, taxonomy)` shadow model advice is advisory telemetry only and never overrides the `model`/`profile` you selected. `recommend_models` remains the caller-facing ranking surface.

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `CONFIG_ERROR` | Mutating flag (`allow_exec`, `allow_destructive`) or `output_path` passed with `mode="readonly"`. | Remove mutating flags/`output_path` for `readonly`, or switch to `edit`/`full`. |
| `BLOCKED` | Headless task attempted `run_command` or `delete_file` without pre-authorization. | Pass `mode="full"` / `allow_exec=true` or `mode="destructive"` / `allow_destructive=true`. |
| `VERIFICATION_FAILED` | `verify_command` exited non-zero 3 times; changes rolled back. | Read `summary` or `read_scratchpad(memory_id)`, fix the root error, and re-dispatch. |
| `TOKEN_LIMIT` | Reasoning model exhausted `max_tokens` before tool call/response. | Set `max_tokens: 8000` or higher, or switch to a non-reasoning coding profile. |

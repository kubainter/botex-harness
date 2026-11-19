---
name: botex
version: "2.5.0"
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

Caller-facing MCP tools (25):
MCP tools: `bot_consult`, `bot_report_activity`, `botex_get_rap_sheet`, `botex_report_violation`, `check_health`, `clean_snapshots`, `delete_memory`, `fetch_url`, `find_graph_health`, `get_backlinks`, `get_outline`, `get_stats`, `get_task_status`, `init_obsidian_vault`, `patch_memory_section`, `read_memory`, `read_scratchpad`, `recall_memory`, `recommend_models`, `reflect_memory`, `run_subagent`, `save_memory`, `search_memory`, `start_task`, `swarm_list_bots`
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
- **Swarm Memory & `envfact` Hygiene (1:1 & Multi-Agent Hosts):** any workstation connected to BoteX Cloud participates in the shared Memory Vault (`scope="global"`, `project="swarm"`, server-derived `origin="<agent>@<host>"`). Always call `recall_memory(query="...", scope="all")` first (One-Shot Recall via 1-hop wikilinks) before falling back to `search_memory` or `read_memory`, and never dump raw files or huge JSON payloads into prompts or notes (Zero Context Bloat). Before delegating on an unfamiliar or Edge workspace, check active `envfact` lessons (`recall_memory(query="envfact <workspace_or_module>", scope="all")`); when a deterministic shell fix (`FormRepair`, e.g., `python -m pytest` over `pytest`) or missing binary (`AbsentTool`) is verified, persist it via `save_memory(..., kind="lesson", tags=["lesson", "envfact"])` for Curator fast-track promotion. Ephemeral `temp-handoff` notes (`scope="local"`, `tags=["temp-handoff"]`) must include `expires: YYYY-MM-DD` and be cleaned up by the caller when done.

### Tethered Bots & Local Fast-Cache (`ADR-SWARM-02` Phase 1)

Local swarm agents (`Antigravity`, `Devin`, `ZCode`) discover Cloud-hosted Tethered Bots at startup without paying Cloud round-trips:

- **Startup Local Fast-Cache (`0 ms`, offline-safe):** check `~/.ai/.swarm/bot_briefs/<slug>.md` (overridable via `BOTEX_SWARM_BRIEFS_DIR`) populated automatically on `/api/edge/ws` `hello_ack -> brief_sync` preload or synced explicitly via `botex bots sync [--url URL] [--on-startup]` (`If-None-Match` -> `304` skip).
- **MCP Discovery & Consultation:** call `swarm_list_bots(namespace="")` to list active Cloud bots, active `host_leases`, and cached local briefs; call `bot_consult(bot_id, query)` for single-hop advice (`ttl_hops=1`, strictly restricted to verified `status == "active"` notes — never `candidate` or `conflict_pending`); call `bot_report_activity(bot_id, scope_path, summary, fencing_token=0)` to update cross-host activity (monotonically fenced: lower `fencing_token` returns `"stale_epoch"`).
- **Recall Piggybacking:** `recall_memory` and `read_memory` automatically attach compact `swarm_guardians` plus `active_brief` once per `(session, etag)` until the brief's `etag` changes.

### BlackBox Penal Registry (`botex.penal.v1`)

Peer cross-review violation tracking, isolated from the Memory Vault:

- **Tools:** `botex_report_violation` (issue a peer cross-review violation ticket) and `botex_get_rap_sheet` (inspect an actor/workspace incident history before delegation or review).
- **Violation codes:** `SEC_01` (security/secret/workspace boundary breach), `SKILL_01` (verification bypass / test theater), `SCOPE_01` (diff/scope explosion), `HAL_01` (hallucinated tool/path), `LOOP_01` (3+ turn failure loop).
- **Isolation invariant:** the BlackBox ledger (`penal_vault.db`) is strictly separated from the Memory Vault (`ecc.memory.v1`) to prevent Context Poisoning — never log violations via `save_memory`.

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `CONFIG_ERROR` | Mutating flag (`allow_exec`, `allow_destructive`) or `output_path` passed with `mode="readonly"`. | Remove mutating flags/`output_path` for `readonly`, or switch to `edit`/`full`. |
| `BLOCKED` | Headless task attempted `run_command` or `delete_file` without pre-authorization. | Pass `mode="full"` / `allow_exec=true` or `mode="destructive"` / `allow_destructive=true`. |
| `VERIFICATION_FAILED` | `verify_command` exited non-zero 3 times; changes rolled back. | Read `summary` or `read_scratchpad(memory_id)`, fix the root error, and re-dispatch. |
| `TOKEN_LIMIT` | Reasoning model exhausted `max_tokens` before tool call/response. | Set `max_tokens: 8000` or higher, or switch to a non-reasoning coding profile. |

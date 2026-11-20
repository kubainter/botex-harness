---
name: botex
version: "2.7.0"
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

Delegate to BoteX (prefer `run_subagent` synchronous by default; `start_task` + `get_task_status` asynchronous only when the fan-out criteria of Global Rule 5.1 hold — and then it REQUIRES a dispatch-ledger entry (Rule 5.2; `list_tasks` is the sweep/recovery surface since Phase 2) plus collection via `get_task_status` before turn end (Rule 5.3)) for bounded file inspection, surgical patching, test-verified bug fixes, or Memory Vault operations. Do **not** delegate general knowledge questions, server/daemon administration, or vague unbounded requests.

- **`provider` is REQUIRED:** every `run_subagent`/`start_task`/`POST /api/tasks` call MUST name an explicit provider (`openrouter`, `nvidia`, `google`, `openrouter-free`, `ollama`). An omitted provider no longer falls back — the request is rejected upfront with `CONFIG_ERROR`/HTTP 400 (empty provider would otherwise silently route to paid `openrouter`). Only `BOTEX_PROVIDER` set server-side exempts the requirement. Prefer `openrouter-free`/`nvidia`/`google` for unpaid work; `openrouter` additionally requires `allow_expensive_model=true` or a `paid_allowlist` entry under `pricing.paid_consent_required`.
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

Caller-facing MCP tools (26):
MCP tools: `bot_consult`, `bot_report_activity`, `botex_get_rap_sheet`, `botex_report_violation`, `check_health`, `clean_snapshots`, `delete_memory`, `fetch_url`, `find_graph_health`, `get_backlinks`, `get_outline`, `get_stats`, `get_task_status`, `init_obsidian_vault`, `list_tasks`, `patch_memory_section`, `read_memory`, `read_scratchpad`, `recall_memory`, `recommend_models`, `reflect_memory`, `run_subagent`, `save_memory`, `search_memory`, `start_task`, `swarm_list_bots`
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
  "provider": "openrouter-free",
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
  "provider": "openrouter-free",
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
- **Swarm Memory & `envfact` Hygiene:** Cloud workstations share the Memory Vault (`scope="global"`, `project="swarm"`, server-derived `origin`). Always call `recall_memory(query="...", scope="all")` first before `search_memory`/`read_memory`, and never dump raw files or large JSON payloads into prompts (Zero Context Bloat). Before delegating on an unfamiliar/Edge workspace, check `recall_memory(query="envfact <workspace_or_module>", scope="all")`; persist verified shell fixes (`FormRepair`, e.g., `python -m pytest`) or missing binaries (`AbsentTool`) as `save_memory(..., kind="lesson", tags=["lesson", "envfact"])`. Ephemeral `temp-handoff` notes (`scope="local"`, `tags=["temp-handoff"]`) must include `expires: YYYY-MM-DD` and be deleted when done.

### Tethered Bots & Local Fast-Cache (`ADR-SWARM-02`)

- **Local Fast-Cache (`~/.ai/.swarm/bot_briefs/<slug>.md`):** Preloaded on `/api/edge/ws` connect or synced via `botex bots sync [--url URL] [--on-startup]`.
- **MCP Discovery & Consultation:** `swarm_list_bots(namespace="")` lists active Cloud bots, `host_leases`, and cached briefs; `bot_consult(bot_id, query)` returns single-hop (`ttl_hops=1`) advice from verified `active` notes only; `bot_report_activity(bot_id, scope_path, summary, fencing_token=0)` updates fenced activity leases (`stale_epoch` if out-fenced).
- **Recall Piggybacking:** `recall_memory` and `read_memory` attach `swarm_guardians` and `active_brief` once per `(session, etag)`.

### BlackBox Penal Registry (`botex.penal.v1`)

Peer cross-review violation tracking, isolated from the Memory Vault:

- **Tools:** `botex_report_violation` (issue a peer cross-review violation ticket) and `botex_get_rap_sheet` (inspect an actor/workspace incident history before delegation or review).
- **Violation codes:** `SEC_01` (security/secret/workspace boundary breach), `SKILL_01` (verification bypass / test theater), `SCOPE_01` (diff/scope explosion), `HAL_01` (hallucinated tool/path), `LOOP_01` (3+ turn failure loop).
- **Isolation invariant:** the BlackBox ledger (`penal_vault.db`) is strictly separated from the Memory Vault (`ecc.memory.v1`) to prevent Context Poisoning — never log violations via `save_memory`.

## Troubleshooting

| Error | Cause | Solution |
|---|---|---|
| `CONFIG_ERROR` | Mutating flag (`allow_exec`, `allow_destructive`) or `output_path` passed with `mode="readonly"`. | Remove mutating flags/`output_path` for `readonly`, or switch to `edit`/`full`. |
| `CONFIG_ERROR` | `provider` omitted from the delegation call. | Pass an explicit `provider` — there is no silent fallback; unscoped calls would route to paid `openrouter`. |
| `BLOCKED` | Headless task attempted `run_command` or `delete_file` without pre-authorization. | Pass `mode="full"` / `allow_exec=true` or `mode="destructive"` / `allow_destructive=true`. |
| `VERIFICATION_FAILED` | `verify_command` exited non-zero 3 times; changes rolled back. | Read `summary` or `read_scratchpad(memory_id)`, fix the root error, and re-dispatch. |
| `TOKEN_LIMIT` | Reasoning model exhausted `max_tokens` before tool call/response. | Set `max_tokens: 8000` or higher, or switch to a non-reasoning coding profile. |

---
name: botex
description: How to delegate autonomous code-editing tasks and connect to the BoteX MCP harness. Covers execution modes (Agent vs Chat), capability scoping (mode presets), approval policies, two-tier memory vaults, and run status contracts. Use whenever connected to a BoteX MCP server (stdio or Remote SSE) or when preparing to call run_subagent.
metadata:
  version: "1.1"
---

# Using BoteX

BoteX is an agent-agnostic execution harness and MCP server. It runs a self-contained tool loop (read -> patch -> validate -> snapshot) over a workspace directory. You delegate a file task to it; it returns a verified contract result with status, files touched, line balance, and USD cost.

MCP tools: `run_subagent`, `start_task`, `get_task_status`, `save_memory`, `search_memory`, `read_memory`, `patch_memory_section`, `get_backlinks`, `find_graph_health`, `init_obsidian_vault`, `read_scratchpad`, `fetch_url`, `get_outline`, `get_stats`, `check_health`, `clean_snapshots`, `recommend_models`.

Transports:
- **Local stdio**: Standard input/output pipe for local agents (Devin, Antigravity, Cursor, Claude Desktop). Zero network overhead, zero configuration.
- **Remote SSE (`botex cloud`)**: Streamable HTTP and Server-Sent Events gateway (`/mcp/sse`, `/mcp/messages/`). Allows IDEs and web applications to connect over the network, protected by an optional Master Bearer Token (`BOTEX_ACCESS_TOKEN`).

Operating modes:
- **Agent Mode (`run_subagent`, `start_task`)**: Autonomous contract-driven editing loop with fuzzy patching, rollback triggers, and snapshots.
- **Chat Mode (`botex chat`)**: Conversational engineering assistant with token streaming and a strict read-only sandbox.

## When to delegate

**Delegate when the task is a concrete file or code operation on a workspace:**

- "Apply this refactor to `src/parser.py`"
- "Add a `validate()` method to the `Config` class"
- "Create a migration file for the new schema"
- "Rename `old_fn` to `new_fn` across the workspace"
- "Review `src/auth.py` and report security issues" (`mode="readonly"`: findings return in the result summary or via `output_path`)

Read-only tasks (analysis, reviews, audits, advisory reports) are also supported: the model reads the workspace and reports findings after `STATUS: DONE` (the text lands in the result `summary`), or writes them to `output_path` when specified. Use `mode="readonly"` for these.

**Do NOT delegate (do it yourself or refuse) when the task is:**

- A question that does not need the workspace: a direct answer is cheaper; BoteX's loop is built for inspecting or changing a project.
- Network or live-data tasks unless the user explicitly authorizes `allow_net=true`. With that flag, the restricted read-only `read_url` tool follows the server's `net.policy`; web content remains untrusted data. For caller-side research, prefer the separate `fetch_url` MCP tool and pass its text into the task.
- Running commands (tests, builds, installs) without permission: requires `exec.enabled=true` in the server config (disabled by default) plus either `mode="full"` or `allow_exec=true`; otherwise it refuses as `UNSUPPORTED`.
- Operating the harness itself (`serve`, `cloud`, config changes): those are CLI commands for the operator, not tasks for the engine.

If the model declares the task unworkable with an explicit `STATUS: UNSUPPORTED` line, BoteX ends the run right away rather than burning budget on it. Prose refusals without the marker get a corrective nudge first, since a model mid-analysis may legitimately report "cannot find X".

## Calling `run_subagent`

```json
{
  "task": "<precise description of the change>",
  "workspace_dir": "<absolute project path>",
  "files": ["src/parser.py"],
  "mode": "edit",
  "recipe": "",
  "profile": "coding",
  "provider": "",
  "model": "",
  "max_turns": 0,
  "max_tokens": 0,
  "max_duration_s": 0,
  "budget_limit_usd": -1.0,
  "allow_destructive": false,
  "allow_exec": false,
  "output_path": "",
  "allow_net": false,
  "net_allowed_hosts": [],
  "net_allowed_urls": [],
  "allow_non_zdr": false,
  "verify_command": "",
  "approval_policy": "high_risk",
  "api_key": ""
}
```

Always write `task` as a precise instruction: the engine is headless and cannot ask you follow-up questions mid-run. `max_tokens` is the per-turn completion cap (0 = server default); reasoning models need ~8000+ because their thinking shares this budget.

For long runs use `start_task` and poll with `get_task_status`; the server limits concurrency and expires completed results after the configured TTL. `verify_command` requires `exec.enabled=true` and an exec-capable mode. `recipe` applies a specialized persona prompt (`planner`, `code-explorer`, `reviewer`, `security-reviewer`, `build-resolver`, `tdd`).

### Structured vs text output

`run_subagent` returns both forms: a human-readable report as text content and the full engine result as `structuredContent`. Key fields include `ok`, `status`, `failure_kind`, `summary`, `files_touched`, `exec_ran`, `rollback_verified`, `steps`, `duration_s`, `cost_usd`, `cost_source` (`openrouter_generation_api`, `openrouter_inline_usage`, or `offline`), `queue_time_ms`, `generation_time_ms`, `cached_tokens`, `cache_write_tokens`, `actual_model`, `lines_added`/`lines_removed`, `attempts[]` (per-candidate model, task ID, status, failure kind, cost, and rollback status), and `fallback_from` (deprecated alias, prefer `attempts[]`).

`get_task_status` exposes the same split: `result` is the pretty text, `result_data` the structured dict. Prefer `structuredContent` and `result_data` over parsing raw text.

### Capability modes (the `mode` parameter)

| Mode | Tools exposed | Use for |
|---|---|---|
| `readonly` | list_dir, get_file_outline, read_file, read_file_lines | analysis, recon |
| `edit` | + apply_patch, create_file | normal code changes (default) |
| `destructive` | + delete_file, move_file | refactors that remove or move files |
| `full` | + delete/move + run_command | tasks needing test runs (see warnings) |

Explicit flags (`allow_destructive`, `allow_exec`, `allow_net`) can only **widen** a preset, never narrow it. `allow_net` exposes only the restricted `read_url` tool and never grants shell commands; its destination scope is governed by `net.policy` plus any per-run host or URL grants.

### Human-in-the-Loop & Approval Policies

BoteX includes an approval system (`botex/approval.py`) to prevent unauthorized high-risk operations:

- `approval_policy`:
  - `none`: No operations require interactive operator confirmation.
  - `high_risk` (default): `delete_file`, `move_file`, and `run_command` require operator approval.
  - `mutations`: All disk writes (`create_file`, `apply_patch`, deletions, commands) require approval.
  - `all_risky`: Mutations plus network fetching require approval.
- **Pre-authorizations**: Passing `--allow-destructive`, `--allow-exec`, or selecting `mode="full"` registers session pre-approvals for those specific tools, allowing headless runs to execute them without mid-run prompts.
- **Headless Fail-Closed**: In headless execution (when no interactive operator or confirmation handler is connected), any operation requiring approval fails closed with a descriptive error rather than bypassing safety gates.

### Pre-authorization rules (important, read before granting)

Headless tasks cannot pause for confirmation unless an async ApprovalManager is attached. What you grant up front applies for the whole run:

- `mode="destructive"` / `allow_destructive=true`: Grant **only** after the user agrees that files may be deleted or moved. Snapshots cover file contents, not side effects elsewhere.
- `mode="full"` / `allow_exec=true`: Grant only for **trusted workspaces**, and only when the task genuinely needs it (such as "run pytest and fix failures"). `run_command` is policy-gated (allowlisted binaries, denied eval forms like `python -c`, `shell=False`, sanitized env, timeout) but it is **not a sandbox**: a test runner executes repository code with the operator's privileges.
- Never pre-authorize elevated modes "just in case": scope the mode to the task at hand.

### Provider, model, cost

- `provider`: `openrouter` (default), `nvidia`, `google` (Google AI Studio / Gemini via its OpenAI-compatible endpoint — key `GEMINI_API_KEY`, fallback `GOOGLE_API_KEY`; no credits/balance API), `ollama` (local), or `openrouter-free` (free `:free` models, ZDR off, no subscription needed).
- `profile`: `default` (`openrouter/auto`), `coding` (`openrouter/pareto-code`), `auto-beta` (`openrouter/auto-beta`), `fast` (`google/gemini-2.5-flash`): resolved per provider (e.g. on `google`: default=`gemini-2.5-flash`, coding=`gemini-2.5-pro`, fast=`gemini-2.5-flash-lite`).
- `model`: explicit slug, overrides profile.
- **Model recommendations (`recommend_models`)**: When unsure which model to pick, call `recommend_models(task_type="coding", limit=5, include_free=False, provider="", profile="", tier="auto")`. It queries `/api/v1/models` with the provider's `Authorization: Bearer <API_KEY>` header (respecting account-level key filters), filters for tool-calling support (`require_tools=True`) and synchronous interactive endpoints (excluding `:batch`), classifies models into `high` / `standard` / `budget` tiers, and ranks them by task aptitude and token cost (`include_free=True` admits `:free` variants).
- **Wallet and key budget monitoring (`botex wallet`, `/api/key/status`)**: Inspect credit balance, key spend (`spend_usd`), remaining budget (`remaining_usd`), reset window (`resets_at`), daily free-model request allowance (`free_model_daily_requests`), and `low_budget_warning` (`remaining_usd < $1.00`) before launching heavy tasks.
- **Leave `model` and `profile` empty to let the mode pick the tier**: `readonly` runs resolve to the cheap `fast` profile, `destructive`/`full` to `coding`, `edit` to the server default (`engine.mode_profiles` in config). Prefer an explicit `model` over router profiles for routine tasks, as routers may select reasoning backends unpredictably.
- `budget_limit_usd`: daily spend cap; the engine refuses when exceeded.
- The server enforces a **pricing guardrail**: models over the configured $/1Mtok cap are rejected before any API call (`PRICE_EXCEEDED`).
- A **compatibility pre-flight** also rejects models the provider catalog marks as lacking tool calling (`UNSUPPORTED`, zero cost). Do not retry with the same model.
- OpenRouter requests enforce Zero Data Retention (`provider.data_collection=deny`), `provider.require_parameters=true` (automatically stripping `reasoning` for non-reasoning slugs so `require_parameters` never triggers HTTP 404), `provider.allow_fallbacks=true`, and `provider.sort="throughput"`. `:free` models fail the pre-flight gate with `ZDR_VIOLATION`: they run only with explicit consent via `allow_non_zdr=true` per run or the `openrouter-free` provider.
- Upon `DONE`, OpenRouter runs query `GET /api/v1/generation?id=...` in parallel (with a short 404 retry) to reconcile exact USD cost (`total_cost`), queue latency (`queue_time_ms`), inference time (`generation_time_ms`), and prompt cache read/write tokens, falling back to inline `usage.cost` or `MODEL_PRICING_ESTIMATES`.

## Reading the result

```
[SUCCESS] BoteX finished task 'a1b2c3d4' in 4 steps.
- Status: DONE
- Duration: 18.2s | Cost: $0.00210 USD
- Modified files: src/parser.py
- Line balance: +12 / -4
```

| Status | Meaning |
|---|---|
| `DONE` | Task contract verified: required output exists on disk or report delivered; see summary and files_touched |
| `UNSUPPORTED` | Explicit `STATUS: UNSUPPORTED` marker from the model, or the resolved model lacks tool-call support (`failure_kind` distinguishes `capability_missing`, `tool_call_unsupported`, or `model_tool_misuse`) |
| `INCOMPLETE` | No useful output after strikes and nudges: `failure_kind` indicates `model_refusal`, `model_tool_misuse`, `no_progress`, or `contract_failed` |
| `TOKEN_LIMIT` | Model exhausted `max_tokens` without visible output (typical for reasoning models): do not retry at the same limit |
| `TIME_LIMIT` | Wall-clock limit (`max_duration_s`) hit between turns: split the task or raise the limit |
| `MAX_TURNS_REACHED` | Step limit hit: `ok` is still `false` even when files were written; partial progress remains in `files_touched` |
| `STAGNANT_ROLLBACK` | Engine detected a no-progress loop and rolled back changes |
| `BUDGET_EXCEEDED` | Daily spend cap reached: surface this to the user |
| `PRICE_EXCEEDED` | Resolved model is over the pricing guardrail cap |
| `ZDR_VIOLATION` | Model matched the ZDR deny list (`:free` or `openrouter/free`): use `allow_non_zdr`, `openrouter-free`, or pick a ZDR model |
| `REQUEST_TIMEOUT` | A single provider request exceeded its wall-clock bound |
| `DONE_WITHOUT_WRITE` | Required output was not written or salvaged |
| `VERIFICATION_FAILED` | `verify_command` failed after 3 attempts: changes were rolled back |
| `CONFIG_ERROR` | Bad provider, mode, net policy, or verify command: fix request parameters |
| `API_ERROR` | Provider call failed; files were rolled back: check `failure_kind` (`provider_auth_error`, `provider_policy_denied`, `provider_rate_limited`, `provider_unavailable`, `provider_error`) |
| `ERROR` | Invalid or missing `workspace_dir` or other pre-run failure |

`ok` is `true` **only** when `status == "DONE"`: it certifies the task contract was fulfilled, not merely that files changed. `ok: false` statuses are signals to change the request or inform the user.

The configured fallback chain (`providers.<name>.model_fallbacks`) operates at two layers:
1. **In-payload wire failover (`extra_body.models`)**: For OpenRouter non-meta-router models, up to 3 candidates validated through all 5 gates (`Denylist`, `Decisions`, `ZDR`, `Price`, `Tools`) are sent in `extra_body["models"]` on initial turns; as soon as a file is modified (`files_touched`) or a command runs (`exec_ran`), `models` is stripped from subsequent turns so a different model never inherits a mutated workspace mid-task.
2. **Engine-level rollback retry**: Across turns, BoteX tries the next candidate model for transient failures (refusals, no-progress, rate limits, provider policy or unavailability, resource limits), but never for auth errors, config errors, budget exhaustion, or failed verification. A workspace touched by file writes is handed to the next candidate only after a verified rollback; a run that executed commands (`exec_ran: true`) never falls back. Inspect `attempts[]` to see what was tried.

Before any candidate fails, the shared retry layer (`providers.<name>.retry`; keys `enabled`, `max_waits`, `max_wait_s`, `base_delay_s`, `jitter_s`) waits out transient *unbilled* rejections — HTTP 429 and connection errors, plus Google 503 "overloaded" — honoring server wait hints (`Retry-After`, `retryDelay`). Timeouts and other 5xx are never retried (a retry could double-bill an executed request), and daily-quota errors skip the wait and fall back immediately. A persistent 429 surfaces as `failure_kind="provider_rate_limited"` (fallbackable). While enabled the SDK's own retries are disabled (`max_retries=0`); setting `"retry": false` on a provider returns control to the SDK.

## Two-Tier Memory Vault & Deterministic Task Receipts

BoteX includes a persistent, two-tier Memory Vault supporting local project vaults and central cross-workspace memory:

- **Local scope (`scope="local"`, default)**: Stored at `<workspace_dir>/.botex/memory/` (automatically gitignored). Contains project-specific context, decisions, and deterministic **Task Receipts** (`kind="handoff"` notes written automatically at 0 LLM tokens upon `DONE` with files touched, line balance, verification status, and live OpenRouter `/api/v1/generation` cost and latency telemetry).
- **Global scope (`scope="global"`)**: Stored centrally at `~/.botex/memory/` (or configured via `paths.global_memory_dir`). Shared across projects and VPS sessions.
- **Consolidated search (`scope="all"`)**: Queries both local and global memory vaults concurrently using outline-first FTS5 retrieval.

MCP tools:
- `save_memory(title, content, workspace_dir="", kind="context", tags=[], memory_id="", scope="local")`: Save context, decisions, handoffs, or lessons with secret redaction.
- `search_memory(workspace_dir="", query="", tag="", kind="", scope="all")`: Fast keyword and tag search across memories.
- `read_memory(memory_id, workspace_dir="", scope="local")`: Retrieve full entry content and metadata.
- `patch_memory_section(memory_id, section="Compiled Truth", new_content="", workspace_dir="", scope="local", append=false)`: Replace or append a single Markdown section without touching frontmatter or other sections — the right tool for updating a note's current state while preserving its history.
- `get_backlinks(memory_id, workspace_dir="", scope="local")`: List notes that link to a memory via `[[wikilinks]]`, for impact analysis before changing a decision.
- `find_graph_health(workspace_dir="", scope="local")`: Audit the wikilink graph — broken links, orphan notes, cycles. Run after large memory refactors.
- `init_obsidian_vault(workspace_dir="", scope="local", override_existing=false, export_index=true)`: Write `.obsidian/` config with per-kind graph colors and refresh `_GRAPH_INDEX.md` so the vault opens as a visual graph.
- `read_scratchpad(memory_id, workspace_dir="")`: Read a task execution log offloaded to `.botex/scratchpad/<memory_id>.log` — where verbose terminal output goes instead of the note body.

Entry kinds:
- `context`: General project architecture notes, environment specifics, or setup details.
- `decision`: Architectural Decision Records (ADRs), trade-off rationales, or schema designs.
- `handoff`: State transfer between tasks or subagents (such as pending changes or remaining work).
- `lesson`: Discovered edge cases, bug root causes, or domain constraints.

## Personas & Recipes Layer (`recipe` parameter)

Specialized personas can be activated via the `recipe` argument in `run_subagent` and `start_task`:

| Recipe | Description | Best Mode |
|---|---|---|
| `planner` | Pre-edit architecture and implementation planning (minimal diff design) | `readonly` / `edit` |
| `code-explorer` | Read-only codebase reconnaissance and architecture mapping | `readonly` |
| `reviewer` | In-depth code review targeting silent failures and edge cases | `readonly` |
| `security-reviewer` | Defensive security audit (OWASP, injection, path traversal, secrets) | `readonly` |
| `build-resolver` | Surgical error resolver for compiler, linter, and test failures | `edit` / `full` |
| `tdd` | Strict Test-Driven Development workflow (Red -> Green -> Refactor) | `edit` / `full` |
| `architecture-adr` | Architecture Decision Record drafting and trade-off analysis | `readonly` / `edit` |
| `security-taint-hardener` | CodeQL-oriented taint-flow hardening and sanitizer placement | `edit` |
| `ui-craftsman` | Frontend/UI implementation with design-system awareness | `edit` |

## Read-Before-Write Gate

To prevent hallucinated edits, BoteX enforces a **Read-Before-Write gate** (`engine.require_read_before_write: true`). Any attempt to `apply_patch` or overwrite an existing file with `create_file` before reading it via `get_file_outline`, `read_file_lines`, or `read_file` is locally rejected with clear corrective instructions. Brand new files do not require a prior read.

## Orchestration Patterns

### 1. Plan -> Edit -> Readonly Review
1. **Plan**: Run `run_subagent(task="Plan changes for X", mode="readonly", recipe="planner")`.
2. **Store**: Save plan to Memory Vault using `save_memory(title="Plan for X", content=..., kind="decision")`.
3. **Edit**: Run `run_subagent(task="Implement plan for X", mode="edit")`.
4. **Review**: Run `run_subagent(task="Review modified files", mode="readonly", recipe="reviewer")`.

### 2. Double Auditor Verification
For high-assurance modifications:
- Run implementation in `edit` mode.
- Spin up two independent read-only reviewer subagents (e.g. `recipe="reviewer"` and `recipe="security-reviewer"`).
- Compare findings before accepting the change set.

### 3. Cross-Provider Verification
Mitigate provider-specific biases and failure modes:
- Worker agent: `provider="openrouter"`, `model="openrouter/pareto-code"` or `coding` profile.
- Auditor agent: `provider="nvidia"`, `model="meta/llama-3.3-70b-instruct"` in `readonly` mode.

### 4. Asynchronous Context Handoffs
When chaining long-running tasks:
- Task A completes and logs a handoff note via `save_memory(..., kind="handoff")`.
- Task B searches recent memories via `search_memory(kind="handoff")` and picks up immediately without context loss.

## Scaling to large workspaces

BoteX is a single-task worker: it does not fan out internally. For broad work ("review the whole repo", "migrate all endpoints"), **split the task at your level**: issue several narrow `run_subagent` calls, each scoped by `files`, and run them concurrently (MCP supports parallel `tools/call` requests). This is faster, cheaper, and keeps each run within `max_turns` and `max_duration_s`.

Read-heavy passes get a larger default step budget via `engine.mode_max_turns` (`readonly` = 30 vs the global 15), and a `turns_exhausted` result retries a different model at most once per call chain. If a broad review still reports `MAX_TURNS_REACHED`, split it by `files` rather than re-running the same wide task.

## Safety properties you can rely on

- Workspace confinement: tools cannot escape `workspace_dir`.
- Blocked files: `.env`, key and credential patterns are never read or written.
- Secret masking: DLP redacts keys and tokens in file reads, streaming chunks, and command output (CWE-312).
- Snapshots: mutating tools snapshot files per task; rollback is automatic on loops and fatal errors.
- No ANSI or formatting escapes in tool responses: safe to relay verbatim.
- Fail-closed agency: approval-required actions require explicit confirmation or pre-authorization.

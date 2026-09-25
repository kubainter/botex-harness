---
name: botex
description: How to delegate autonomous code-editing tasks to the BoteX MCP harness — when to use it, how to scope capabilities (mode presets), when pre-authorization is legitimate, and how to read run statuses and costs. Use whenever you are connected to a BoteX MCP server and considering calling its run_subagent tool.
metadata:
  version: "1.0"
---

# Using BoteX

BoteX is a standalone, agent-agnostic **execution harness**: an MCP server that
runs a self-contained tool loop (read → patch → validate → snapshot) over a
local workspace. You delegate a *file task* to it; it returns a result with
status, files touched, line balance, and USD cost.

MCP tools: `run_subagent`, `start_task`, `get_task_status`, `save_memory`, `search_memory`, `read_memory`, `fetch_url`, `get_outline`, `get_stats`, `check_health`, `clean_snapshots`.

It is **not** a general chat agent, not a shell, and not a sandbox.

## When to delegate

**Delegate when the task is a concrete file/code operation on a workspace:**

- "Apply this refactor to `src/parser.py`"
- "Add a `validate()` method to the `Config` class"
- "Create a migration file for the new schema"
- "Rename `old_fn` to `new_fn` across the workspace"
- "Review `src/auth.py` and report security issues" (`mode="readonly"` —
  findings come back in the result summary or via `output_path`)

Read-only tasks — analysis, reviews, audits, advisory reports — are also
supported: the model reads the workspace and reports findings after
`STATUS: DONE` (the text lands in the result `summary`), or writes them to
`output_path` when you set one. Use `mode="readonly"` for these.

**Do NOT delegate (do it yourself or refuse) when the task is:**

- A question that does not need the workspace — a plain answer is cheaper
  direct; BoteX's loop is built for inspecting or changing a project
- Network/live-data tasks unless the user explicitly authorizes `allow_net=true`.
  With that flag, the restricted read-only `read_url` tool follows the server's
  `net.policy`; web content remains untrusted data. For caller-side research,
  prefer the separate `fetch_url` MCP tool and pass its text into the task.
- Running commands (tests, builds, installs) — requires `exec.enabled=true`
  in the server config (disabled by default) *plus* either `mode="full"`
  or `allow_exec=true`; otherwise it will refuse as `UNSUPPORTED`
- Operating the harness itself (`serve`, config changes) — those are CLI
  commands for the operator, not tasks for the engine

If the model declares the task unworkable with an explicit
`STATUS: UNSUPPORTED` line, BoteX ends the run right away rather than
burning budget on it. Prose refusals without the marker get a corrective
nudge first — a model mid-analysis may legitimately say "cannot find X".

## Calling `run_subagent`

```json
{
  "task": "<precise description of the change>",
  "workspace_dir": ".",
  "files": ["src/parser.py"],
  "mode": "edit",
  "recipe": "",
  "max_turns": 0,
  "max_tokens": 0,
  "budget_limit_usd": -1.0,
  "output_path": "",
  "allow_net": false,
  "net_allowed_hosts": [],
  "net_allowed_urls": [],
  "verify_command": ""
}
```

Always write `task` as a precise instruction — the engine is headless and
cannot ask you follow-up questions mid-run. `max_tokens` is the per-turn
completion cap (0 = server default); reasoning models need ~8000+ because
their thinking shares this budget.

For long runs use `start_task` and poll with `get_task_status`; the server
limits concurrency and expires completed results after the configured TTL.
`verify_command` requires `exec.enabled=true` and an exec-capable mode.
`recipe` applies a specialized persona prompt (`planner`, `code-explorer`,
`reviewer`, `security-reviewer`, `build-resolver`, `tdd`).

### Structured vs text output

`run_subagent` returns both forms: a human-readable report as text
content (unchanged format) and the full engine result as
`structuredContent` — fields include `ok`, `status`, `failure_kind`,
`summary`, `files_touched`, `exec_ran`, `rollback_verified`, `steps`,
`duration_s`, `cost_usd`, `lines_added`/`lines_removed`, `attempts[]`
(per-candidate model/task_id/status/failure_kind/cost, plus `rolled_back`
and `rollback_verified` when a failed attempt was reverted) and
`fallback_from` (deprecated alias — read `attempts[]`).
`get_task_status` exposes the same split: `result` is the pretty text,
`result_data` the structured dict. Prefer `structuredContent`/
`result_data` over parsing the text.

### Network policy (`net.policy`)

`allow_net=true` exposes `read_url`, but the server-side policy decides which
URLs the internal model may request:

- `caller` (default): `net_allowed_hosts` and/or `net_allowed_urls` supplied
  with the call authorize destinations for that run; when omitted, configured
  defaults apply.
- `allowlist`: only `net.allowed_hosts`/`net.allowed_urls` are available;
  per-run entries may narrow but cannot widen the configured scope.
- `public`: any public `http(s)` host is allowed unless per-run entries narrow
  it. Use only for non-sensitive workspaces — a model could encode workspace
  data in a URL it chooses.
- `off`: no internal network tool.

`net_allowed_hosts` entries authorize the named host and its subdomains.
`net_allowed_urls` rules are stronger than host rules: a URL ending in `/`
authorizes that subtree; otherwise it authorizes the exact URL including its
query string. `fetch_url` is separate from `read_url` and is meant for the
calling agent: under `caller`/`public` it can fetch the explicit URL the caller
chooses; under `allowlist` it stays inside the configured allowlist. All modes
keep public-IP checks, DNS/IP pinning, default-port enforcement, redirect
revalidation, text-only content, request/byte limits, and DLP masking.

### Capability modes (the `mode` parameter)

| Mode | Tools exposed | Use for |
|---|---|---|
| `readonly` | list_dir, get_file_outline, read_file, read_file_lines | analysis, recon |
| `edit` | + apply_patch, create_file | normal code changes (default) |
| `destructive` | + delete_file, move_file | refactors that remove/move files |
| `full` | + delete/move + run_command | tasks needing test runs (see warnings) |

Explicit flags (`allow_destructive`, `allow_exec`, `allow_net`) can only
**widen** a preset, never narrow it. `allow_net` exposes only the restricted
`read_url` tool and never grants shell/network commands; its destination scope
is governed by `net.policy` plus any per-run host/URL grants.

### Pre-authorization rules (important — read before granting)

Headless mode cannot pause to confirm. Whatever you grant up front applies
for the whole run:

- `mode="destructive"` / `allow_destructive=true` — grant **only** after the
  user has agreed that files may be deleted or moved. Snapshots cover file
  contents, not side effects elsewhere.
- `mode="full"` / `allow_exec=true` — grant only for **trusted workspaces**,
  and only when the task genuinely needs it (e.g. "run pytest and fix
  failures"). `run_command` is policy-gated (allowlisted binaries, denied
  eval forms like `python -c`, `shell=False`, sanitized env, timeout) but it
  is **NOT a sandbox**: a test runner executes repository code with the
  operator's privileges.
- Never pre-authorize elevated modes "just in case" — scope the mode to the
  task at hand.

### Provider, model, cost

- `provider`: `openrouter` (default) or `nvidia`, per server config
- `profile`: `default` / `coding` / `auto-beta` / `fast` — resolved per provider
- `model`: explicit slug, overrides profile
- **Leave `model` and `profile` empty to let the mode pick the tier**:
  `readonly` runs resolve to the cheap `fast` profile, `destructive`/`full`
  to `coding`, `edit` to the server default (`engine.mode_profiles` in
  config). Prefer an explicit `model` over router profiles for routine
  tasks — routers may select reasoning backends unpredictably.
- `budget_limit_usd`: daily cap; the engine refuses when exceeded
- The server may enforce a **pricing guardrail** — models over the
  configured $/1Mtok cap are rejected before any API call
  (`PRICE_EXCEEDED`). Pick a cheaper profile/model if you hit it.
- A **compatibility pre-flight** also rejects models the provider catalog
  marks as lacking tool calling (`UNSUPPORTED`, zero cost) — do not retry
  with the same model.
- OpenRouter requests enforce `provider.data_collection=deny` (ZDR).
  NVIDIA applies its own data policy.

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
| `DONE` | Task contract verified — required output exists on disk / report delivered; see summary and files_touched |
| `UNSUPPORTED` | Explicit `STATUS: UNSUPPORTED` marker from the model, or the resolved model lacks tool-call support (`failure_kind` distinguishes `capability_missing` vs `tool_call_unsupported` vs `model_tool_misuse` — the model formed invalid tool calls and gave up; retry with a more capable model) — relay the reason, do not retry with the same model |
| `INCOMPLETE` | No useful output after strikes/nudges — `failure_kind` says why: `model_refusal` (prose refusal without the marker), `model_tool_misuse`, `no_progress`, or `contract_failed`; retry with a clearer task |
| `TOKEN_LIMIT` | Model exhausted `max_tokens` without visible output (typical for reasoning models) — do not retry at the same limit; note router profiles (`openrouter/auto`, `auto-beta`) may route to reasoning backends unpredictably — prefer an explicit model for routine tasks |
| `TIME_LIMIT` | Wall-clock limit (`max_duration_s`) hit between turns — split the task or raise the limit |
| `MAX_TURNS_REACHED` | Step limit hit — `ok` is still `false` even when files were written; partial progress remains in `files_touched` |
| `STAGNANT_ROLLBACK` | Engine detected a no-progress loop and rolled back changes |
| `BUDGET_EXCEEDED` | Daily spend cap reached — surface this to the user |
| `PRICE_EXCEEDED` | Resolved model is over the pricing guardrail cap |
| `REQUEST_TIMEOUT` | A single provider request exceeded its wall-clock bound |
| `DONE_WITHOUT_WRITE` | Required output was not written or salvaged |
| `VERIFICATION_FAILED` | `verify_command` failed twice; changes were rolled back |
| `CONFIG_ERROR` | Bad provider/mode, net policy, or verify command — fix the request parameters |
| `API_ERROR` | Provider call failed; files were rolled back — check `failure_kind` (`provider_auth_error` = bad key, `provider_policy_denied`, `provider_unavailable`) |
| `ERROR` | Invalid/missing `workspace_dir` or other pre-run failure |

`ok` is `true` **only** when `status == "DONE"` — it certifies the task
contract was fulfilled, not merely that files changed. `ok: false`
statuses are signals to change the request or inform the user.

The configured fallback chain (`providers.<name>.model_fallbacks`) may
try the next model automatically for failures another model could fix
(refusals, no-progress, provider policy/unavailability, resource limits)
— but never for auth errors, config errors, budget exhaustion, or failed
verification. A workspace touched by file writes is handed to the next
candidate only after a *verified* rollback; a run that executed commands
(`exec_ran: true`) never falls back. Inspect `attempts[]` to see what
was tried.

## Workspace Memory Vault

BoteX includes a persistent, per-workspace Memory Vault stored at `<workspace_dir>/.botex/memory/`
(automatically protected with `.gitignore`).

MCP tools:
- `save_memory(title, content, workspace_dir=".", kind="context", tags=[])`: Save context, decisions, handoffs, or lessons with secret redaction.
- `search_memory(workspace_dir=".", query="", tag="", kind="")`: Fast keyword/tag search across workspace memories.
- `read_memory(memory_id, workspace_dir=".")`: Retrieve full entry content and metadata.

Entry kinds:
- `context`: General project architecture notes, environment specifics, or setup details.
- `decision`: Architectural Decision Records (ADRs), trade-off rationales, or schema designs.
- `handoff`: State transfer between tasks or subagents (e.g. pending changes, remaining work).
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

## Read-Before-Write Gate

To prevent hallucinated edits (the R1 blind write issue), BoteX enforces a **Read-Before-Write gate**
(`engine.require_read_before_write: true`). Any attempt to `apply_patch` or overwrite an existing file
with `create_file` before reading it via `get_file_outline`, `read_file_lines`, or `read_file` is
locally rejected with clear corrective instructions. Brand new files do not require a prior read.

## Orchestration Patterns

### 1. Plan → Edit → Readonly Review
1. **Plan**: Run `run_subagent(task="Plan changes for X", mode="readonly", recipe="planner")`.
2. **Store**: Save plan to Memory Vault using `save_memory(title="Plan for X", content=..., kind="decision")`.
3. **Edit**: Run `run_subagent(task="Implement plan for X", mode="edit")`.
4. **Review**: Run `run_subagent(task="Review modified files", mode="readonly", recipe="reviewer")`.

### 2. Santa Method (Double Auditor Verification)
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

BoteX is a single-task worker — it does not fan out internally. For broad
work ("review the whole repo", "migrate all endpoints"), **split the task at
your level**: issue several narrow `run_subagent` calls, each scoped by
`files`, and run them concurrently (MCP supports parallel `tools/call`
requests). This is faster, cheaper, and keeps each run within `max_turns`
and `max_duration_s`. One call per well-defined change set — not one call
per repository.

## Safety properties you can rely on

- Workspace confinement: tools cannot escape `workspace_dir`
- Blocked files: `.env`, key/credential patterns are never read or written
- Secret masking: DLP redacts keys/tokens in file reads and command output
- Snapshots: mutating tools snapshot files per task; rollback is automatic
  on loops and fatal errors
- No ANSI/formatting escapes in tool responses — safe to relay verbatim

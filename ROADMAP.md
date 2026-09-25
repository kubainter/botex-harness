<!--
SPDX-License-Identifier: Apache-2.0
Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
-->
# BoteX Roadmap

Deferred topics triaged by **usefulness × feasibility** — collected here so
design rationale isn't lost. Each entry notes *why it waits* (or why it's
next), not just *what*.

## Near term — high value, feasible

Ordered by payoff for actual observed failures.

### 1. Async task pattern (`start_task` / `get_task_status`) — implemented
`start_task` now detaches execution and returns a task ID; `get_task_status`
polls the bounded in-process registry. The engine loop itself was unchanged;
the MCP server owns concurrency (max 3 by default) and result TTL cleanup.

Every `run_subagent` call blocks until the run ends. MCP supports concurrent
`tools/call`, but a single call still occupies the client for the whole
duration — and some clients cap tool calls at 60–120s (a Devin client
tolerated 543s, others may not). Slow reasoning models make this worse: an
uncapped R1 turn was observed thinking for 400s+ inside one call, and a
client-side timeout discards an already-paid-for result.

A start/poll pattern (`start_task` → task_id → `get_task_status`) lets
clients detach, poll, and run several chunks in parallel. The implementation
uses an in-process task registry + `asyncio`, with a concurrency cap and TTL
cleanup. The engine loop already returns a structured result dict.
Mitigations in place today: `max_duration_s` (900s), `request_timeout_s`
(300s per single call).

### 2. Per-tier fallback chains — implemented
Today `tier -> model_slug`; AGY's design review suggested
`tier -> [slug, slug, ...]` with failover on pre-flight rejection or a
tools-unsupported error. Easier than when it was deferred: the engine now
emits machine-readable failure statuses that are natural fallback triggers
(`TOKEN_LIMIT`, `REQUEST_TIMEOUT`, `DONE_WITHOUT_WRITE`, `UNSUPPORTED`,
`PRICE_EXCEEDED`). Observed motivation: `deepseek-r1` burns budget on
thinking where `deepseek-v3`/Claude/Gemini complete — a fallback would make
that a config choice instead of a manual call.

Fallback is limited to one configured next model per attempt and only when
no file was touched. A task that partially writes never hands that workspace
to another model; it returns the failure for explicit operator handling.

### 3. Dynamic Snapshot Pruning — implemented
`.snapshots` grows unbounded; `clean_snapshots` is manual. Auto-prune on
successful run end (keep last N task snapshots / max age / max MB — the
knobs already exist in `clean_snapshots`). The configured
age/size quotas are best-effort; pruning skips protected in-flight `task_id`s
and pruning errors never fail the task.

### 4. Edit/destructive E2E + snapshot rollback verification
Unit tests cover the pieces (including rollback of newly created files), but
all real-model E2E so far ran `readonly`. Before wider use: run
`edit`/`destructive` against a real model and confirm `rollback_task` restores
files after `STAGNANT_ROLLBACK`, verification failure, and fatal errors —
snapshots are the only safety net where git is unavailable.

### 5. `cancel_task` MCP tool
`start_task` creates an `asyncio.Task` and `_background_task` already handles
`CancelledError`, but no tool exposes cancellation — a consumer watching a
runaway loop can only kill the server. Add `cancel_task(task_id)` calling
`task_obj.cancel()`; the `CANCELLED` status path already exists.

### 6. Mid-run progress in `get_task_status`
Consumers see only `QUEUED`/`RUNNING` until completion — no turn count, no
last action, no token burn so far. Write per-turn state into
`_TASKS[task_id]` from the engine loop (current turn, elapsed, last tool
call, cumulative tokens) and expose it in the status response. Cheaper than
MCP `notifications/progress` and works for every client that can poll.

### 7. `search_files` agent tool
The loop has no grep: finding a symbol's usages costs `list_dir` + multiple
`read_file` calls — burned turns and tokens, worst on `readonly` analysis
tasks where search is the primary operation. Add a bounded
`search_files(pattern, path)` tool (regex, result cap, respects
`resolve_safe_path` and .gitignore) next to outline-first reading.

### 8. Consumer-facing `undo_task` + `get_task_diff`
Snapshots exist and internal rollback is verified, but after `DONE` a
consumer cannot revert or inspect what changed — only `files_touched` and
line counts. `undo_task(task_id)` wraps `rollback_task` + `verify_rollback`
(surviving snapshots, within TTL); `get_task_diff(task_id)` renders
pre/post diffs from the snapshot store. Together they make delegation
trustable: every run is inspectable and reversible.

## Later — needs design or an external dependency

### Plan / dry-run mode (`mode="plan"`)
Agent performs full recon and returns proposed changes (patch list / plan)
without writing; the consumer shows the user, then dispatches `mode="edit"`
to execute. This is the standard approve-then-apply workflow in mature
harnesses; the pieces exist (readonly caps, contract checks) but the engine
needs a "proposal" contract kind that collects intended edits instead of
applying them.

### Steering / `resume_task`
Tasks are fire-and-forget: after `MAX_TURNS_REACHED` there is no
"continue" or "do it differently". `resume_task(task_id, instruction)` with
a retained `messages` history would fix long-run workflows, but requires
persisting conversation state between calls (with secret masking) —
overlaps the cross-task context question. A lighter alternative is
`session_id` chaining that carries only summaries/diffs (masked) between
tasks instead of the full message history.

### Multi-command verification
`verify_command` accepts one command; real projects want
`pytest && ruff && mypy`. Extend to `verify_commands: list[str]` with a
per-command report, each gated by the same exec policy.

### Cost from the provider catalog, not the static table
`analytics.MODEL_PRICING_ESTIMATES` drifts from real prices; `log_run`
could price runs from `pricing.load_catalog` (cached per provider) with the
static table as fallback.

### Symlink-safe writes (TOCTOU)
`resolve_safe_path` validates before use, but a race remains between path
resolution and the write — a hostile workspace could swap a symlink in that
window. File-descriptor-based validation or `O_NOFOLLOW`-style opens would
close it; only matters for untrusted workspaces.

### Full `.gitignore` semantics
The matcher approximates gitignore (anchored patterns, negations) but lacks
`**` globs and directory-only rules; sufficient for `list_dir`, not for any
future ignore-driven write filtering. See also the nested-`.gitignore` gap.

### Decisions API transport (`call_decisions`)
`typesafe/jev-*` and other decisions-only models are already rejected
locally before any API call (`UNSUPPORTED`/`capability_missing`) — the
guard exists, the *feature* does not. Adding real support is mechanically
simple but a separate API contract, so it is a deliberate next step, not an
edit.

Why deferred: current refusal detection and contract checks are
deterministic and free, and a new endpoint means a new transport plus a new
failure point. Revisit when `.agent_analytics.json` shows
`model_refusal`/`no_progress` as a dominant failure driver, or when a
caller wants cheap typed judging.

Implementation plan, when resumed:

1. **Transport** — `botex/decisions.py`: plain `httpx` POST to
   `{base_url}/../alpha/decisions` (OpenRouter path is
   `POST /api/alpha/decisions`), not the `AsyncOpenAI` path. Provider-scoped:
   reuse key resolution and `mask_secrets`; no `extra_body` ZDR injection
   unless the endpoint accepts it (verify per provider).
2. **Caller surface** — MCP tool `ask_decision(question, options, model?)`
   plus internal engine hook for typed checks (e.g. refusal classification,
   contract judging). `model` must match `decisions_models` — the guard
   inverts: this tool *requires* a decisions-only slug.
3. **Config** — extend `providers.<name>.decisions_models` with an optional
   `decisions_url` override (default derived from `base_url`) so the
   endpoint shape is not hardcoded to OpenRouter.
4. **Safety/cost** — same rules as the chat path: `request_timeout_s`,
   `max_error_chars` truncation, secret masking on state/questions,
   `pricing` gate if the endpoint reports per-call cost, analytics record
   with a `decision` task kind.
5. **Tests** — `test_botex.py`: mock transport (accepted option set,
   malformed response, timeout, secret masking on input), guard inversion
   (chat slug rejected, decisions slug accepted), cost/record fields.
   `test_live_e2e.py`: one opt-in scenario only.
6. **Docs** — README tools list + parameter table, TUTORIAL §6, and the
   `decisions_models` docstring in `config.py` (it currently describes only
   the rejection path).

### Smart Context Scaling (task-size-aware model routing)
`recommend_models` exists and picks cost-effective models by task type, but
does not estimate task size. Next step: derive expected context from the
workspace outline (cheap) and route tiny tasks to small-context cheap
models, huge repos to 1M+ context models. `engine.mode_profiles` already
routes `readonly` -> `fast`; this generalizes the idea to size.

### `net` tool (`read_url` / `fetch_url` / `search_web`) — implemented
`read_url` is a restricted, read-only tool. It requires explicit
`allow_net=true` and follows `net.policy`: `allowlist` keeps destinations in
the configured scope, `caller` accepts per-run `net_allowed_hosts` /
`net_allowed_urls` grants, `public` permits public hosts for non-sensitive
workspaces, and `off` disables the tool. All modes keep private/loopback/
reserved DNS blocking, IP-pinned connections, default-port enforcement,
redirect revalidation, request/size limits, text-only responses, and secret
masking. `fetch_url` is a separate caller-side MCP tool for research on URLs
chosen by the orchestrator; it is not exposed to BoteX's internal model loop.
`search_web` remains deferred until there is a concrete need.

### Capability probing / prefix heuristics
For providers without `/models` capability metadata, options are a
one-token dummy tool-call probe at provider init, or a static model-prefix
ruleset (LiteLLM-style). Deferred: the probe costs a call, and prefix rules
guess at unverified formats — the explicit `providers.<name>.capabilities`
declaration covers the need today.

### NVIDIA pricing adapter
`integrate.api.nvidia.com` `/models` publishes no price fields, so the
pricing guardrail treats every NVIDIA model as "unknown" (`on_unknown`
policy). If NVIDIA introduces paid tiers with a real price format, add an
adapter for *their actual* schema — do not invent one speculatively.

### Nested `.gitignore` support in `list_dir`
Only the workspace-root `.gitignore` is loaded today
(`load_gitignore_patterns` reads `workspace_root/.gitignore`); subdirectory
`.gitignore` files are ignored. Requires per-directory pattern loading —
low value, no reports of misses.

### Richer outlines
`_get_python_outline` scans top-level nodes only (nested functions and
nested classes are omitted). Deliberate for skeleton views — revisit if
consumers report missing structure.

### `mask_secrets` precision
The base64-shaped pattern (`[a-zA-Z0-9/+]{40,}`) can redact legitimate long
string literals in read output. Tightening needs a better secret heuristic
without losing DLP coverage.

### Progressive-disclosure task skills
Pattern from agent-skills ecosystems: lazily loaded "recipe" files for
common BoteX tasks (e.g. `migrate-to-X`, `add-tests`) that callers could
fetch before delegating. Marketing/onboarding layer, not engine code —
`skills/botex/SKILL.md` already covers consumers.

### Real sandboxing for `run_command`
`run_command` is policy-gated (allowlist, deny-args, `shell=False`,
timeout) but explicitly **not** a sandbox — commands run with operator
privileges. True isolation (job objects / containers / restricted tokens)
is a much larger project; the docs must keep saying "not a sandbox" until
then.

### PyPI / packaging release
`pyproject.toml` exists (`botex-harness`, console script `botex`). Remains:
publish the package and document the `botex serve` MCP config for Claude
Code / Cursor / Windsurf / Devin clients.

## ECC import candidates (affaan-m/ECC, MIT)

ECC is a content layer for harnesses (agents/skills/hooks/rules as markdown +
JS), not an execution engine — most of its "wisdom" BoteX already implements
deterministically (verification, rollback, cost caps, fallback chains). What
remains worth importing is content and a few patterns. Anything imported must
be reviewed first — uneven quality, Claude-Code-specific tool names, and the
project itself warns about malicious third-party mirrors.

### Task-recipe prompt library (ECC `agents/` personas)
ECC's `agents/` are vetted subagent personas (planner, code-reviewer,
security-reviewer, per-language build-error-resolvers) usable as
system-prompt templates for `run_subagent`. Extends the deferred
"progressive-disclosure task skills" idea: ship a recipes layer mapping task
types to prompts. MIT-licensed, but prompts reference Claude Code tool names
and need porting to the BoteX toolset.

### Fresh-context review pass (caller-side orchestration)
ECC's plan → implement → review-from-fresh-context loop maps to a caller
recipe: after an edit task returns `DONE`, dispatch a second `run_subagent`
in `readonly` mode as reviewer. No engine changes — document the pattern in
`skills/botex/SKILL.md` alongside the existing decomposition guidance.

### Memory vault format (`ecc.memory.v1`)
If cross-task memory is ever added, adopt ECC's portable markdown format
(project scope under `.ecc/memory/`, explicit "unreviewed context, not
executable policy" trust boundary) instead of inventing one. Recall into
prompts is an injection surface — bodies would need secret masking and must
never be treated as instructions. ECC's own vault writes have open
native-Windows defects, so this means adopting the *format*, not their CLI.

### Configurable pre/post-write hooks
Generalize the hardcoded pre-write syntax validation into user-defined hooks
in `botex.config.json` (e.g. run `ruff` after every `*.py` write). Hooks run
outside the model context — deterministic enforcement like ECC's hook layer,
but scoped through the existing exec policy (allowlist, `shell=False`,
timeout) rather than arbitrary shell.

### AgentShield vetting pipeline
`ecc-agentshield` (npm) scans agent files/prompts/MCP configs for injection,
secrets, and permission issues. Use it on any ECC content before import and
periodically on `skills/` and config. Tooling dependency, not engine code.

### Eval metrics for `benchmarks.py`
ECC's eval-harness material (grader types, pass@k) is inspiration for richer
benchmark reporting — e.g. per-model success rates across repeated tasks to
inform `model_fallbacks` ordering from `.agent_analytics.json` data.

## Covered by existing mechanics (retired ideas)

### Self-Healing tests / auto-correction loop — already how the loop works
When `exec` is enabled, `run_command` results (failing pytest, linter
output) return to the model as tool output and it self-corrects within the
same `max_turns` budget — no separate correction loop needed. The remaining
sliver ("force verification before DONE") is now covered by the optional
`verify_command` parameter. It runs through the existing exec allowlist,
allows two attempts, and rolls back on final failure.

### Streaming early-abort on runaway reasoning — superseded
Idea: watch the reasoning stream and cut the response once thinking exceeds
a threshold. Superseded by the provider-side `reasoning.max_tokens` cap in
`extra_body`, which bounds thinking server-side regardless of streaming.

## Probably never (conscious non-goals)

### Internal fan-out / multi-worker orchestration
Splitting one task across parallel workers inside BoteX would mean merged
snapshots, write conflicts, and multiplied cost. Task decomposition
belongs to the *caller* — issue several narrow `run_subagent` calls
concurrently (documented in `skills/botex/SKILL.md`). BoteX stays a
single-workspace worker.

### `AGENTS.md` contributor guide
Decided against publishing modification guidelines for the engine itself;
`skills/botex/SKILL.md` covers consumers, and the config/docs cover the
rest.

## Done recently (for reference)

- Result contracts: `TaskContract` kinds (`analysis`/`edit`/`file_output`),
  disk-validated `DONE`, `failure_kind` taxonomy, `model_tool_misuse`,
  malformed tool-arg validation with signature echo
- Fallback as policy: `should_fallback` on `failure_kind`, rollback
  confirmation via the snapshot mutation registry, `attempts[]`,
  `max_task_duration_s` chain budget, `move_file` `.created` registration
- Provider-neutral layer: `botex/providers.py` (response normalization,
  per-provider headers/`extra_body`, classified provider errors) — OpenRouter
  attribution/ZDR no longer leaks to NVIDIA
- Structured MCP outputs: `run_subagent` returns `CallToolResult` with
  `structuredContent`; `start_task`/`get_task_status` propagate result dicts
- Decisions-model guard: `typesafe/jev-*` rejected locally
  (`UNSUPPORTED`/`capability_missing`) before any API call
- False-completion hardening: DONE write-guard nudge, `output_path`
  contract with payload salvage, `DONE_WITHOUT_WRITE` status
- Model reliability: ordered per-profile fallback chains, limited to failures
  before the first successful write
- Controlled web access: explicit `allow_net`, `read_url`, `net.policy`
  (`allowlist`/`caller`/`public`/`off`), per-run host/URL grants, caller-side
  `fetch_url`, SSRF checks with pinned IPs, redirect/size/request limits,
  text-only responses, DLP masking
- Async MCP execution: `start_task`/`get_task_status`, concurrency cap, TTL
  cleanup
- Optional `verify_command` gate using the existing exec policy; failed
  verification rolls back changes
- Rollback now removes files newly created by the failed task
- Snapshot auto-pruning on successful runs with age/size quotas and protected
  in-flight task IDs
- Reasoning controls: `reasoning_max_tokens` auto-bump on detected
  reasoning tokens, provider-side `reasoning.max_tokens` thinking cap,
  thinking-aware `TOKEN_LIMIT` (no retry when thinking ate the budget)
- `request_timeout_s` per-request bound + `REQUEST_TIMEOUT` status
- Truncated tool-call arguments detected (`finish_reason=length` mid-call)
  and reported to the model as a resend-with-smaller-payload error
- Deadline nudge near `max_turns` to force pending writes before the limit
- `recommend_models` MCP tool (live OpenRouter cost/quality ranking)
- Capability modes (`readonly`/`edit`/`destructive`/`full`), `run_command` policy gate
- Pricing guardrail (`PRICE_EXCEEDED` before any API call), model catalog cache
- `max_tokens` (default 8000) + `max_duration_s` (default 900s) limits, `TIME_LIMIT` status
- Termination hardening: `UNSUPPORTED`, `INCOMPLETE`, `TOKEN_LIMIT`, `STAGNANT_ROLLBACK`
- i18n EN/PL for human-facing CLI; agent-facing MCP stays English
- file_tools hardening (dir guards, snapshot-in-try, range validation, truncation flag)

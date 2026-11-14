# BoteX Technical Tutorial

BoteX is an MCP-native execution harness for autonomous code-editing subagents and conversational engineering assistance.
It runs as a standalone stdio MCP server, a remote Server-Sent Events (SSE) cloud gateway, or directly from the command line. Any MCP client can delegate bounded file tasks to it.

Entry points: `server.py` (stdio MCP & CLI dispatcher) and `botex/server/server_cloud.py` (FastAPI Cloud Gateway).

## 1. Architecture

Since v2.1.0 the package is organized into domain subpackages; legacy flat imports (`botex.engine`, `botex.memory`, ...) still resolve through the alias graft in `botex/__init__.py` (see `docs/IMPORT_MAP.md`).

```
./ (repo root)
├── server.py              MCP server + CLI dispatcher
├── botex/
│   ├── core/                engine internals
│   │   ├── engine.py          tool loop, context pruning, termination statuses, provider fallbacks
│   │   ├── state.py           in-flight task state + contrastive ring buffers across fallbacks
│   │   ├── contracts.py       task contract verification & status taxonomy
│   │   ├── events.py          engine event emitter taxonomy (EventKind)
│   │   ├── capabilities.py    capability presets & agency middleware
│   │   ├── approval.py        Human-in-the-Loop approval manager & policies
│   │   ├── context_guard.py   context compaction & verification output pruning
│   │   ├── context_inclusion.py deterministic in-process context relay for swarm agents
│   │   ├── meta_harness.py    task taxonomy, episodic lesson extraction/recall, infra-failure gate
│   │   └── tracing.py         OTel-style span helpers (engine, retry, recall_curator, server_cloud)
│   ├── providers/           provider-facing services
│   │   ├── providers.py       provider registry & adapters (OpenRouter, NVIDIA, Google AI Studio, etc.)
│   │   ├── analytics.py       cost/token ledger, run attribution, shadow meta-routing
│   │   ├── benchmarks.py      live model benchmarks, recommend_models & task-tier estimation
│   │   ├── catalog.py         dynamic model catalog with TTL caching & ZDR checks
│   │   ├── denylist.py        hierarchical model denylist (glob & regex)
│   │   ├── pricing.py         pre-flight price and capability guardrails
│   │   ├── rate_limiter.py    asynchronous token-bucket rate limiter & cooldowns
│   │   ├── retry.py           shared transient-failure backoff for provider calls (429/conn/overload)
│   │   ├── wallet.py          credit balance, usage, and limit checker
│   │   ├── http_pool.py       pooled httpx clients
│   │   ├── prompt_cache.py    prompt-cache policy gating
│   │   └── adapters/zai.py    Z.ai GLM Coding Plan adapter
│   ├── tools/               worker tool implementations
│   │   ├── patch_engine.py    fuzzy patching, in-memory syntax check, snapshots
│   │   ├── file_tools.py      outline-first file I/O tools
│   │   ├── exec_tools.py      run_command policy implementation
│   │   ├── net_tools.py       SSRF-safe network fetching (read_url, fetch_url)
│   │   ├── security.py        path traversal guard, blocked files, secret masking
│   │   └── vision.py          vision input triage
│   ├── memory/              Memory Vault stack
│   │   ├── memory.py          two-tier Memory Vault, wikilink graph, lifecycle & confidence
│   │   ├── curator.py         facade re-exporting the botex.ops.curator service API
│   │   ├── embeddings.py      local embedding helpers for memory/graph features
│   │   └── obsidian.py        Obsidian PKM vault export & graph index generation
│   ├── ops/                 autonomous operations
│   │   ├── curator/           Knowledge Graph Curator daemon (CuratorService):
│   │   │                      lesson promotion, PR-only skill crystallization, recall cascade, Telegram alerts
│   │   ├── maintenance.py     maintenance jobs (WAL checkpoints, index sync)
│   │   └── migrate_projects.py vault migration tooling
│   ├── skills/              Skill Vault stack
│   │   ├── skills.py          server-side Skill Vault, safe installer, L0 triage & proposal drafts
│   │   ├── skillspector_gate.py SkillSpector pre-flight audit gate
│   │   ├── recipes.py         recipe loader & system prompt injection
│   │   └── recipes/           bundled markdown persona prompts
│   └── server/              interfaces
│       ├── server_cloud.py    FastAPI Cloud Gateway, Remote MCP SSE/HTTP & origin hardening
│       ├── chat.py            conversational chat loop & read-only tool sandbox
│       ├── config.py          layered config loader (defaults < local < env)
│       ├── plugins.py         ZeroTrust subprocess plugin runner
│       ├── i18n.py            CLI localization (EN/PL); MCP responses are always EN
│       └── ui.py              ANSI helpers with plain-text fallback
├── web/                   Vue 3 + Vite + Pinia + Tailwind PWA frontend
├── tests/
│   ├── test_botex.py          core unit and integration suite
│   ├── test_stage_4_6.py      OpenRouter live telemetry, routing & JSON schema tests
│   ├── test_stage_4_7.py      recipes metadata, benchmarks cache, cloud recipe endpoints
│   ├── test_stage_4_8.py      skill vault, L0 triage & memory deletion tests
│   ├── test_approval.py       HITL approval and policy tests
│   ├── test_chat.py           streaming chat and sandbox tests
│   ├── test_memory_twotier.py two-tier memory vault scoping tests
│   ├── test_memory_v2.py      memory vault v2 schema tests
│   ├── test_server_cloud.py   FastAPI, SSE, and WebSocket tests
│   ├── test_providers.py      provider registry and adapter tests
│   ├── test_retry.py          shared retry/backoff layer tests
│   ├── test_rate_limiter.py   token-bucket rate limiter and cooldown tests
│   ├── test_state_checkpoint.py in-flight state carryover across fallbacks
│   ├── test_curator.py        knowledge-graph curator and Telegram dispatch tests
│   ├── test_events.py         task event emitter taxonomy tests
│   ├── test_circuit_breaker.py provider circuit breaker tests
│   ├── test_tracing.py        tracing span and secret-sanitization tests
│   ├── test_task_size_routing.py task-tier estimation tests
│   ├── test_structured_contracts_v1.py canonical handoff schema tests
│   ├── test_caller_skill_contract.py SKILL.md <-> server.py contract parity
│   ├── test_skills_parity.py  skills <-> recipes parity invariant
│   ├── test_config_safety.py  dotenv-isolation guard tests
│   ├── test_catalog.py        dynamic catalog and ZDR tests
│   ├── test_denylist.py       hierarchical denylist tests
│   ├── test_wallet.py         wallet and credit query tests
│   ├── test_context_guard.py  context pruning and compaction tests
│   ├── test_analytics.py      cost/token ledger and digest tests
│   ├── test_embeddings.py     local embedding helper tests
│   ├── test_meta_harness.py   task taxonomy and meta-lesson tests
│   ├── test_obsidian_graph.py Obsidian vault export and graph tests
│   ├── test_obsidian_edge_cases.py obsidian edge-case tests
│   ├── test_nvidia_streaming.py NVIDIA streaming adapter tests
│   ├── test_credits_stress.py wallet credits stress tests
│   ├── test_challenger_m4_3.py challenger suite M4.3
│   ├── test_challenger_m5_empiric.py challenger suite M5 empiric
│   ├── test_integration.py    cross-module integration tests
│   └── test_live_e2e.py       live end-to-end integration suite (opt-in)
├── docs/
│   ├── TUTORIAL.md        this file
│   ├── IMPORT_MAP.md      canonical vs legacy import paths (v2.1.0 modularization)
│   └── TELEGRAM_NOTIFICATIONS.md  curator alert setup guide
├── deploy/oracle-vps/     systemd units & one-click installer for Oracle A1.Flex ARM64
├── skills/botex/SKILL.md  agent-facing usage contract
├── bin/                   zero-install launchers (`botex.cmd`, `botex.sh`, `botex-edge.*`)
├── pyproject.toml         package configuration & scripts
├── requirements.txt       runtime dependencies
├── ROADMAP.md             deferred features with rationale
└── botex.config.json      committed defaults
```

Runtime artifacts: `.snapshots/`, `.agent_analytics.jsonl`, `analytics-archive/`, `.model_pricing.json`, `.botex/proposals/`; all are gitignored.

## 2. Core mechanisms

### A. Outline-first file access
Agents never read whole large files at once. They call `get_file_outline` to inspect top-level symbols and line ranges, then `read_file_lines(path, start, end)` for the exact slice they need.

### B. Context pruning
After a patch is accepted, earlier full-file reads in the conversation history are replaced with a short placeholder. This prevents the message history from growing quadratically during multi-step edits.

### C. Pre-write syntax check
Before disk is touched, modified content is linted in memory:

* `ast.parse` for `.py`
* `json.loads` for `.json`
* `php -l` for PHP if installed

On failure the file is not written and the agent receives the syntax error.

### D. Fuzzy patching
Three matching levels for `apply_patch`:

1. Exact 1:1 match.
2. Normalize CRLF/LF and strip trailing whitespace.
3. Indentation-agnostic block match.

### E. Snapshot rollback
Every mutating tool snapshots the target file to `.snapshots/<task_id>/` first. If the engine detects a no-progress loop or hits a fatal error, it restores the original files. The CLI provides `--snapshots` and `--clean-snapshots` for maintenance.

### F. Security boundaries

* `resolve_safe_path()` blocks path traversal outside `workspace_dir` and validates boundaries across both local and global memory vaults.
* Read/write of `.env*`, `*.pem`, `*.key`, `id_rsa`, and similar credential patterns is blocked.
* Secret-like strings are recursively masked in file reads, command output, streaming chunks, and analytics (CWE-312).
* OpenRouter requests include `provider: {"data_collection": "deny"}` while ZDR is enforced. Both the local model gate and this wire flag derive from one predicate (`zdr_enforced`). Relaxed runs print a stderr warning and report `zdr_enforced: false`.
* `reasoning.max_tokens` (in `extra_body`) caps hidden thinking so reasoning models cannot consume the entire `max_tokens` budget before emitting output.

## 3. Capability modes & Agency Middleware

`run_subagent` accepts a `mode` parameter. Presets are fail-closed; an unknown mode returns `CONFIG_ERROR`.

| Mode | Tools exposed |
|---|---|
| `readonly` | list_dir, get_file_outline, read_file, read_file_lines |
| `edit` (default) | readonly + apply_patch, create_file |
| `destructive` | edit + delete_file, move_file |
| `full` | destructive + run_command |

Explicit flags (`allow_destructive`, `allow_exec`, `allow_net`) can widen ordinary presets, but `readonly` and `chat` remain hard read-only modes: no local writes, deletions, moves, or command execution (flags cannot widen readonly to write/exec; `allow_net` adds only read-only network access via `read_url`). `output_path` requires a mutating mode (`edit`, `destructive`, `full`); `readonly` reports findings in `summary`. `run_command` additionally requires `exec.enabled: true` in the config; without it `CAP_EXEC` is removed regardless of mode/flags.

`net` is deliberately **not** part of any preset: it is an exfiltration and prompt-injection axis, so it requires a separate `allow_net=true` opt-in. It exposes the restricted, read-only `read_url` tool, scoped by `net.policy` (`caller` / `allowlist` / `public` / `off`) plus per-run `net_allowed_hosts` / `net_allowed_urls` grants.

## 4. Human-in-the-Loop (HITL) & Approval System

BoteX provides a policy-driven approval manager (`botex/core/approval.py`). Both `run_subagent` and `start_task` accept the `approval_policy` parameter:

- **Approval Policies**:
  - `none`: Disables interactive approval checks.
  - `high_risk` (default): `delete_file`, `move_file`, and `run_command` require confirmation.
  - `mutations`: All disk writes and execution require confirmation.
  - `all_risky`: Mutations, execution, and external network reads require confirmation.
- **Fail-Closed in Headless Mode**: When running headlessly (no operator or confirmation handler connected), tools requiring approval fail closed unless covered by an explicit session pre-approval.
- **Interactive Prompts**: Terminal runs prompt the operator with `(y)es`, `(n)o`, `(a)lways allow for this task`, or `(c)ancel task`.
- **Task Abort & Rollback**: Cancelling an action immediately halts execution and verifies that any partial workspace changes are safely rolled back.

## 5. Chat Mode & Conversational Sandbox

BoteX includes a conversational mode (`botex/server/chat.py`) as a separate CLI/REST interface, not the normal task delegation API:

- **Command**: `botex chat` launches an interactive REPL with real-time token streaming.
- **Strict Read-Only Sandbox**: Only non-mutating inspection tools (`get_file_outline`, `read_file_lines`, `read_file`, `list_dir`, `search_memory`, `read_memory`) are exposed. File mutations and shell commands are blocked.
- **Context Management**: Automatically prunes large tool outputs and strips dangling tool calls when context windows compress.

## 6. Two-Tier Memory Vault

Persistent Memory Vault (`botex/memory/memory.py`) supports three distinct scopes:

1. **Local Scope (`scope="local"`)**: Stored at `<workspace_dir>/.botex/memory/`. Holds project-specific architecture notes, decisions, and deterministic task receipts (`_save_task_receipt` written automatically at 0 LLM tokens upon `DONE`, enriched with live OpenRouter `/api/v1/generation` telemetry when available).
2. **Global Scope (`scope="global"`)**: Stored centrally at `~/.botex/memory/` (or `paths.global_memory_dir`). Shared across projects and VPS sessions.
3. **Swarm Scope (`scope="swarm"`)**: Third read-only root at `paths.swarm_memory_dir` (empty by default). Makes one shared vault reachable from any workspace; write tools never inherit this root.
4. **Consolidated Search (`scope="all"`)**: Searches local, global, and (when configured) swarm entries concurrently. Matching is per-token OR with a coverage admission gate (≥2 distinct tokens; single-token queries admit on non-stop-word matches), so partial keyword overlap no longer returns zero hits.

Notes link to each other through `[[wikilinks]]`, forming a navigable knowledge graph; verbose execution logs are offloaded to `.botex/scratchpad/<memory_id>.log` instead of the note body.

Standard Memory Vault flow: one-shot synthesized query via `recall_memory` (preferred for agentic swarms to cut token bloat by up to 98.8%), or classical multi-turn `search_memory` -> `read_memory` -> `patch_memory_section` or `save_memory` -> optional `find_graph_health`.

`recall_memory` runs a hedged curator cascade configured in `memory.recall` (Schema v2): local Qwen (`qwen2.5:3b` by default, 7b opt-in) synthesizes first; NVIDIA hedges in only after `hedge_s` of silence, then Gemini and an OpenRouter `:free` model follow sequentially. A prompt-size guard skips the local tier when gathered context exceeds `num_ctx_guard_tokens`. Every response carries `recall_status` (`SYNTHESIZED` / `ZERO_NODES_FOUND` / `FALLBACK_MODELS_FAILED`), `tier_attempts` with per-tier timings, and `workspace_searched` — check these before assuming the vault lacks an answer.

Notes carry a lifecycle `status` (`candidate` -> `active` -> `deprecated`/`superseded`) and a `confidence` score. Engine-extracted `kind="lesson"` notes start as `candidate` at 0.4; `search_memory` filters on `status` and defaults to `"active"` (pass `"all"` to include deprecated/superseded). Promotion to `active` is the Curator's evidence-gated job (see §11), not a write-tool parameter.

MCP tools: `recall_memory` (one-shot graph recall with hedged curator cascade), `save_memory` (supports `confidence`, `belief`, `heuristic`, `lesson`), `read_memory`, `search_memory` (temporal `since`/`until`, `min_confidence`, `status`, `project`, spreading activation via RRF), `reflect_memory` (topic clustering & synthesis), `patch_memory_section` (surgical section update), `delete_memory`, `get_backlinks`, `find_graph_health` (broken links / orphans / cycles), `init_obsidian_vault` (Obsidian config + `_GRAPH_INDEX.md`), `read_scratchpad`.

## 7. FastAPI Cloud Gateway & Remote MCP

BoteX can operate as a central HTTP/SSE daemon (`botex/server/server_cloud.py`):

- **Command**: `botex cloud --host 0.0.0.0 --port 8000`
- **Zero Friction Local / Strict Cloud**:
  - Unauthenticated local mode when `BOTEX_ACCESS_TOKEN` is unset or empty.
  - Constant-time `hmac.compare_digest` Bearer token authentication when `BOTEX_ACCESS_TOKEN` is configured.
  - Public `GET /api/health` endpoint for Docker and proxy health checks.
- **Remote MCP (StreamableHTTP & SSE)**: Exposes StreamableHTTP at `/mcp/http` and legacy SSE at `GET /mcp/sse` + `POST /mcp/messages/` with keepalive comments, allowing remote IDEs and cloud orchestrators to connect over WAN.
- **REST & WebSocket API**:
  - `/api/wallet` & `/api/key/status`: Live credit balance, API key budget limits (`limit_usd`, `spend_usd`, `remaining_usd`, `resets_at`, `free_model_daily_requests`), and `low_budget_warning` (`remaining_usd < $1.00`).
  - `/api/analytics` & `POST /api/analytics/digest`: Aggregated cost/token/cache statistics (`period=week|month|all`) and an optional local-LLM digest loop saved into the Memory Vault.
  - `/api/tasks` (+`/{id}`, `/{id}/cancel`): Background task delegation, status polling, and cancellation. Task entries persist the original request for retry/clone.
  - `/api/approvals` (+`/{id}/decide`): Real-time decision queue for Human-in-the-Loop operators.
  - `/api/memory` (+`/graph`, `/backlinks`): REST interface for the two-tier memory vault and its wikilink graph. `POST /api/memory` ignores the client-supplied `origin` field: provenance is derived server-side from sanitized `X-Agent-Id`/`X-Host` headers (NFKC-folded, `[a-zA-Z0-9_.-]` allowlist, 32 chars per part), and claims of reserved agents (`human`, `human-curated`, `operator`, `curator` in `_RESERVED_ORIGIN_AGENTS`) are rejected.
  - `/api/models` & `/api/models/recommended`: Dynamic model catalog plus benchmark-driven recommendations.
  - `/api/recipes`: Bundled persona prompts available to task runs.
  - `/api/chat` (+`/abort`, `/sessions`, `/sessions/{id}` incl. `DELETE`, `/sessions/{id}/save_to_memory`): SSE streaming, session persistence, and abort.
  - `/api/chat/ws`: WebSocket with 15s `{"type":"ping"}` heartbeat.

## 8. Cost, routing, and capability guardrails

* **Pricing guardrail**: before the first API call the resolved model is checked against the provider's `/models` catalog (cached in `.model_pricing.json`). Models above `pricing.max_*_per_mtok` are rejected locally with `PRICE_EXCEEDED`.
* **Capability guardrail**: models marked by the catalog as lacking tool calling are rejected with `UNSUPPORTED`.
* **5-gate in-payload failovers (`extra_body.models`)**: for OpenRouter, fallback models are validated through all five safety gates (`Denylist`, `Decisions`, `ZDR`, `Price`, `Tools`) and passed in `extra_body["models"]` (capped at 3 models, disabled for meta-routers like `openrouter/auto` and `openrouter/pareto-code`, and stripped after the first file write or command execution).
* **Provider routing (`extra_body.provider`)**: sets `data_collection: "deny"` (when ZDR is active), `require_parameters: true` (when tools or `response_format` are used, automatically stripping `reasoning` for non-reasoning slugs), `allow_fallbacks: true`, and `sort: "throughput"`.
* **Live cost reconciliation**: upon `DONE`, OpenRouter runs query `GET /api/v1/generation?id=...` in parallel (with a short 404 retry) to record official billing (`total_cost`), queue latency (`queue_time_ms`), generation time (`generation_time_ms`), and cache read/write tokens, falling back to inline `usage.cost` or `MODEL_PRICING_ESTIMATES`.
* **Strict JSON Schema completion**: supports `TASK_COMPLETION_JSON_SCHEMA` (`strict: true`, `additionalProperties: false`) via `response_format` and `parse_structured_completion`, alongside `additionalProperties: false` on all tool parameter schemas.
* **Budget**: `budget_limit_usd` is checked at the start of each task and mid-loop. Exceeding it returns `BUDGET_EXCEEDED`.
* **Limits**: `max_turns` (steps), `max_tokens` (completion cap), `max_duration_s` (wall-clock limit).

## 9. Termination statuses

`ok` is `true` **only** when `status == "DONE"`: it certifies that the task contract was verified (required output exists on disk or report delivered), not merely that the model claimed success. For background tasks (`start_task` / `get_task_status`), the outer `status` is lifecycle (`QUEUED`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`); lifecycle `DONE` means finished, not success. Success strictly requires `result_data.ok == true` and `result_data.status == "DONE"`. Workspace note: `files` are primary target hints (not a security allowlist); `workspace_dir` is the hard boundary (located on the BoteX host machine for remote SSE). Keep API keys empty in examples; do not pass secrets in tasks or memories.

| Status | Meaning |
|---|---|
| `DONE` | Task contract verified |
| `BLOCKED` | Task halted by security/policy gate or approval failure in headless execution |
| `FAILED` | Task failed contract criteria or internal error |
| `UNSUPPORTED` | Explicit `STATUS: UNSUPPORTED` marker, or model lacks tool calls |
| `INCOMPLETE` | No useful output after strikes and nudges |
| `TOKEN_LIMIT` | `max_tokens` exhausted without visible output; common with reasoning models |
| `TIME_LIMIT` | `max_duration_s` exceeded between turns |
| `MAX_TURNS_REACHED` | Step limit reached; partial progress stays in `files_touched` |
| `STAGNANT_ROLLBACK` | No-progress loop detected; changes rolled back |
| `DONE_WITHOUT_WRITE` | Required `output_path` never written or salvaged |
| `VERIFICATION_FAILED` | `verify_command` failed after 3 attempts; changes rolled back |
| `REQUEST_TIMEOUT` | A single provider request exceeded `request_timeout_s` |
| `BUDGET_EXCEEDED` | Daily spend cap exceeded |
| `PRICE_EXCEEDED` | Model over the configured price cap; rejected locally |
| `ZDR_VIOLATION` | Model matched the ZDR deny list; requires allow_non_zdr or openrouter-free |
| `CONFIG_ERROR` | Invalid provider, mode, policy, or parameters |
| `ABORTED` | Task explicitly aborted mid-run by operator or API request |
| `API_ERROR` | Provider call failed; rollback attempted if changes existed |
| `ERROR` | Pre-run failure such as missing `workspace_dir` |

## 10. Rate limiting and state carryover

Free-tier providers enforce strict request and token limits per minute. BoteX handles this with two layers:

1. **`ProviderRateLimiter` (`botex/providers/rate_limiter.py`)**: An asynchronous token bucket tracks both RPM and TPM per provider, using limits from `botex.config.json` (`rate_limits`). Requests that exceed the bucket wait until tokens refill. When an endpoint returns HTTP 429 or a `retry-after` header, the limiter activates a monotonic cooldown window.
2. **`InFlightTaskState` (`botex/core/state.py`)**: When an attempt fails before completing the contract (due to rate limits, timeouts, or lack of progress), the engine snapshots accumulated plan steps, file inspection notes, and findings. Modified files are rolled back to clean snapshots, and saved findings are passed directly into the user prompt of the next model candidate or fallback provider (`provider_fallbacks`). The state also carries two bounded contrastive buffers (`syntax_rejections`, last 5 `{file, error}` pairs, and `rollback_reasons`, last 5) so the next candidate does not repeat edits already rejected or rolled back (see §11).

## 11. Self-Improvement Loop

BoteX feeds run telemetry back through deterministic checks. Learning stays local and evidence-gated: promotion into live behavior requires verified confirmation counts or a human-reviewed PR.

### Telemetry & attribution

`analytics.log_run` (`botex/providers/analytics.py`) appends each run to `.agent_analytics.jsonl` (legacy `.agent_analytics.json` is folded in once; monthly rollups rotate to `analytics-archive/`). Non-empty attribution fields (`origin`, `recipe`, `routed_tier`, `taxonomy`, `applied_skills`, `applied_lessons`, `syntax_rejections`, `rollback_count`, `shadow_recommended_model`) are CR/LF-flattened, `mask_secrets`-masked, capped at 64 chars, and persisted alongside the standard cost/token/duration record. `taxonomy` comes from `classify_task_taxonomy` (`botex/core/meta_harness.py`): `bugfix`, `refactor`, `test`, `infra`, `analysis`, `feature`, `general`.

### Contrastive episodic learning

* `extract_meta_lesson` persists a `kind="lesson"` note as `status="candidate"`, `confidence=0.4`, tagged `path:<dir>` for touched directories, when a task recovers after a model failure. The `is_infra_failure` gate keeps provider noise (rate limits, timeouts, quota/auth/budget/config errors, HTTP 429 text) out of the vault.
* `get_relevant_meta_lessons` surfaces only `active` lessons at `confidence >= 0.6`; `path:<dir>`-bound lessons rank ahead of generic taxonomy matches. `pack_lessons_within_budget` caps the injected block at 800 bytes via `ContextKnapsack`; overflow lessons are dropped.
* `update_lesson_confidence` (`botex/memory/memory.py`) nudges injected lesson ids deterministically: `+0.1` on `DONE` with zero rollbacks, `-0.15` on quality failures (rollbacks, `VERIFICATION_FAILED`, `STAGNANT_ROLLBACK`, `INCOMPLETE`), and never on infra failures or `ABORTED`. Confidence clamps to [0, 1]; crossing below `0.2` marks the note `deprecated`.

### Curator lesson promotion (step 6d)

`CuratorService.promote_candidate_lessons` (`botex/ops/curator/curator_service.py`) flips `candidate` -> `active` only when `confidence >= 0.8` **and** at least 2 distinct `DONE`, zero-rollback ledger runs cite the lesson in `applied_lessons`. `origin="human"`, `human-curated`, and `frozen` notes are never mutated. The opt-in `promote_to_global=True` copies verified lessons into `scope="global"`, `project="swarm"` behind a secret-leak guard.

### Layer B: PR-only skill crystallization (step 6e)

`CuratorService.crystallize_skill_proposals` clusters recurring `active` lessons (`min_cluster_size >= 5` sharing a `path:`/domain tag, or untagged lessons with >= 2 co-occurring significant tokens) and drafts `learned-<taxonomy>-<topic>` `SKILL.md` + `recipe.md` via `write_skill_proposal_draft` (`botex/skills/skills.py`). Drafts land solely under `<workspace>/.botex/proposals/skills/` and `.botex/proposals/recipes/` after size (128 KB), secret (`mask_secrets`), prompt-injection (`has_injection_pattern`), and SkillSpector (`audit_skill_content`) gates; frontmatter is stamped `proposal_only: true`. Live `skills/` and `botex/skills/recipes/` are never touched; promotion requires a reviewed PR.

### Layer C: shadow meta-routing

`recommend_shadow_model` / `get_shadow_routing_advice` (`botex/providers/analytics.py`) score models per `(recipe, taxonomy)` bucket from the ledger once >= 10 matching samples exist (30-day lookback): smoothed success, a rollback/rejection quality penalty capped at 0.25, and a small cost bonus with `_SHADOW_COST_FLOOR_USD` ($0.0001) flooring `:free` costs. The engine evaluates advice off-loop (`asyncio.to_thread`), emits a `shadow_routing` event (`EventKind.SHADOW_ROUTING`), and logs `shadow_recommended_model`. Guarantees: `shadow_only`/`actual_override: false`, `hard_banned_models` empty, active routing never overridden.

## 12. CLI usage

```bash
# interactive REPL (in-session: `workspace` or `cd [DIR]` switches target project)
botex repl -w ./my-project --profile coding

# conversational engineering chat
botex chat -w ./my-project --model deepseek/deepseek-chat

# cloud gateway / remote MCP server
botex cloud --host 0.0.0.0 --port 8000

# wallet & credit monitoring
botex wallet
botex wallet --refresh openrouter

# one-shot tasks
botex run "Add a --verbose flag to cli.py" -w ./project --profile coding
botex run "Fix failing test" --files tests/test_app.py --max-turns 25 --budget 0.50
botex run "Remove the legacy module" -w ./project --mode destructive --approval-policy high_risk
botex run "Quick code review" -w ./project --mode readonly --max-duration 300 --max-tokens 4000

# free models (non-ZDR opt-in)
botex run "task" --provider openrouter-free
botex run "task" --model qwen/qwen3.8-27b:free --allow-non-zdr
botex config provider openrouter-free
botex config zdr off

# default project directory
botex config workspace ./my-project
botex config workspace none

# memory vault
botex memory save "note title" "content..." --kind decision --tags arch
botex memory search "query" --scope all

# knowledge graph curator
python -m botex.ops.curator.curator_service --force --notify

# maintenance
botex --stats
botex --stats --month
botex --history <task_id>
botex --snapshots
botex --clean-snapshots
botex health
botex config
botex models [--refresh]
botex serve
```

## 13. Test suite

```bash
python -m pytest tests/
```

Runs the complete hermetic test suite (1344 tests across 67 files). All socket access is hermetically mocked.
A secondary, opt-in suite `tests/test_live_e2e.py` runs against live provider APIs when `OPENROUTER_API_TESTS_KEY` is provided.

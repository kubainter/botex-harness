# BoteX Technical Tutorial

BoteX is an MCP-native execution harness for autonomous code-editing subagents and conversational engineering assistance.
It runs as a standalone stdio MCP server, a remote Server-Sent Events (SSE) cloud gateway, or directly from the command line. Any MCP client can delegate bounded file tasks to it.

Entry points: `server.py` (stdio MCP & CLI dispatcher) and `botex/server_cloud.py` (FastAPI Cloud Gateway).

## 1. Architecture

```
./ (repo root)
├── server.py              MCP server + CLI dispatcher
├── botex/                 engine package
│   ├── engine.py          tool loop, context pruning, termination statuses
│   ├── contracts.py       task contract verification & status taxonomy
│   ├── config.py          layered config loader (defaults < local < env)
│   ├── capabilities.py    capability presets & agency middleware
│   ├── approval.py        Human-in-the-Loop approval manager & policies
│   ├── chat.py            conversational chat loop & read-only tool sandbox
│   ├── server_cloud.py    FastAPI Cloud Gateway & Remote MCP SSE server
│   ├── exec_tools.py      run_command policy implementation
│   ├── file_tools.py      outline-first file I/O tools
│   ├── patch_engine.py    fuzzy patching, in-memory syntax check, snapshots
│   ├── pricing.py         pre-flight price and capability guardrails
│   ├── security.py        path traversal guard, blocked files, secret masking
│   ├── analytics.py       cost ledger, budget checks, CLI reports
│   ├── providers.py       provider registry & adapters (OpenRouter, NVIDIA, etc.)
│   ├── catalog.py         dynamic model catalog with TTL caching & ZDR checks
│   ├── denylist.py        hierarchical model denylist (glob & regex)
│   ├── wallet.py          credit balance, usage, and limit checker
│   ├── benchmarks.py      live model benchmarks & recommend_models
│   ├── net_tools.py       SSRF-safe network fetching (read_url, fetch_url)
│   ├── memory.py          two-tier Memory Vault (local & global scopes)
│   ├── recipes.py         recipe loader & system prompt injection
│   ├── i18n.py            CLI localization (EN/PL); MCP responses are always EN
│   ├── ui.py              ANSI helpers with plain-text fallback
│   └── recipes/           bundled markdown persona prompts
├── web/                   Vue 3 + Vite + Pinia + Tailwind PWA frontend
├── tests/
│   ├── test_botex.py          core unit and integration suite
│   ├── test_stage_4_6.py      OpenRouter live telemetry, routing & JSON schema tests
│   ├── test_approval.py       HITL approval and policy tests
│   ├── test_chat.py           streaming chat and sandbox tests
│   ├── test_memory_twotier.py two-tier memory vault scoping tests
│   ├── test_server_cloud.py   FastAPI, SSE, and WebSocket tests
│   ├── test_providers.py      provider registry and adapter tests
│   ├── test_catalog.py        dynamic catalog and ZDR tests
│   ├── test_denylist.py       hierarchical denylist tests
│   ├── test_wallet.py         wallet and credit query tests
│   ├── test_context_guard.py  context pruning and compaction tests
│   └── test_live_e2e.py       live end-to-end integration suite
├── docs/
│   └── TUTORIAL.md        this file
├── skills/botex/SKILL.md  agent-facing usage contract
├── botex.cmd / botex.sh   zero-install execution launchers
├── pyproject.toml         package configuration & scripts
├── requirements.txt       runtime dependencies
├── ROADMAP.md             deferred features with rationale
└── botex.config.json      committed defaults
```

Runtime artifacts: `.snapshots/`, `.agent_analytics.json`, `.model_pricing.json`; all are gitignored.

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

Explicit flags (`allow_destructive`, `allow_exec`, `allow_net`) can only widen a preset, never narrow it. `run_command` additionally requires `exec.enabled: true` in the config; without it `CAP_EXEC` is removed regardless of mode/flags.

`net` is deliberately **not** part of any preset: it is an exfiltration and prompt-injection axis, so it requires a separate `allow_net=true` opt-in. It exposes the restricted, read-only `read_url` tool, scoped by `net.policy` (`caller` / `allowlist` / `public` / `off`) plus per-run `net_allowed_hosts` / `net_allowed_urls` grants.

## 4. Human-in-the-Loop (HITL) & Approval System

BoteX provides a policy-driven approval manager (`botex/approval.py`):

- **Approval Policies**:
  - `none`: Disables interactive approval checks.
  - `high_risk` (default): `delete_file`, `move_file`, and `run_command` require confirmation.
  - `mutations`: All disk writes and execution require confirmation.
  - `all_risky`: Mutations, execution, and external network reads require confirmation.
- **Fail-Closed in Headless Mode**: When running headlessly (no operator or confirmation handler connected), tools requiring approval fail closed unless covered by an explicit session pre-approval.
- **Interactive Prompts**: Terminal runs prompt the operator with `(y)es`, `(n)o`, `(a)lways allow for this task`, or `(c)ancel task`.
- **Task Abort & Rollback**: Cancelling an action immediately halts execution and verifies that any partial workspace changes are safely rolled back.

## 5. Chat Mode & Conversational Sandbox

BoteX includes a conversational mode (`botex/chat.py`):

- **Command**: `botex chat` launches an interactive REPL with real-time token streaming.
- **Strict Read-Only Sandbox**: Only non-mutating inspection tools (`get_file_outline`, `read_file_lines`, `read_file`, `list_dir`, `search_memory`, `read_memory`) are exposed. File mutations and shell commands are blocked.
- **Context Management**: Automatically prunes large tool outputs and strips dangling tool calls when context windows compress.

## 6. Two-Tier Memory Vault

Persistent Memory Vault (`botex/memory.py`) supports two distinct scopes:

1. **Local Scope (`scope="local"`)**: Stored at `<workspace_dir>/.botex/memory/`. Holds project-specific architecture notes, decisions, and deterministic task receipts (`_save_task_receipt` written automatically at 0 LLM tokens upon `DONE`, enriched with live OpenRouter `/api/v1/generation` telemetry when available).
2. **Global Scope (`scope="global"`)**: Stored centrally at `~/.botex/memory/` (or `paths.global_memory_dir`). Shared across projects and VPS sessions.
3. **Consolidated Search (`scope="all"`)**: Searches across both local and global memory entries concurrently using outline-first FTS5 retrieval.

MCP tools: `save_memory`, `read_memory`, `search_memory`.

## 7. FastAPI Cloud Gateway & Remote MCP

BoteX can operate as a central HTTP/SSE daemon (`botex/server_cloud.py`):

- **Command**: `botex cloud --host 0.0.0.0 --port 8000`
- **Zero Friction Local / Strict Cloud**:
  - Unauthenticated local mode when `BOTEX_ACCESS_TOKEN` is unset or empty.
  - Constant-time `hmac.compare_digest` Bearer token authentication when `BOTEX_ACCESS_TOKEN` is configured.
  - Public `GET /api/health` endpoint for Docker and proxy health checks.
- **Remote MCP via SSE**: Exposes `GET /mcp/sse` and `POST /mcp/messages/` with keepalive comments, allowing remote IDEs (Cursor, Windsurf, Claude Desktop, Antigravity) to connect over WAN.
- **REST & WebSocket API**:
  - `/api/wallet` & `/api/key/status`: Live credit balance, API key budget limits (`limit_usd`, `spend_usd`, `remaining_usd`, `resets_at`, `free_model_daily_requests`), and `low_budget_warning` (`remaining_usd < $1.00`).
  - `/api/tasks`: Background task delegation, status polling, and cancellation.
  - `/api/approvals`: Real-time decision queue for Human-in-the-Loop operators.
  - `/api/memory`: REST interface for two-tier memory vault.
  - `/api/chat` (SSE streaming) & `/api/chat/ws` (WebSocket with 15s ping heartbeat).

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

`ok` is `true` **only** when `status == "DONE"`: it certifies that the task contract was verified (required output exists on disk or report delivered), not merely that the model claimed success.

| Status | Meaning |
|---|---|
| `DONE` | Task contract verified |
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
| `API_ERROR` | Provider call failed; rollback attempted if changes existed |
| `ERROR` | Pre-run failure such as missing `workspace_dir` |

## 10. CLI usage

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

## 11. Test suite

```bash
python -m pytest tests/
```

Runs the complete hermetic test suite (377 tests across 14 test modules). All socket access is hermetically mocked.
A secondary, opt-in suite `tests/test_live_e2e.py` runs against live provider APIs when `OPENROUTER_API_TESTS_KEY` is provided.

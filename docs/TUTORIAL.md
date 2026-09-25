# BoteX Technical Tutorial

BoteX is an MCP-native execution harness for autonomous code-editing subagents.
It runs as a standalone stdio MCP server and can also be driven directly from
the command line. Any MCP client can delegate a bounded file task to it.

Entry point: `server.py`.

## 1. Architecture

```
./ (repo root)
├── server.py              MCP server + CLI dispatcher
├── botex/                 engine package
│   ├── engine.py          tool loop, context pruning, termination statuses
│   ├── config.py          layered config loader (defaults < local < env)
│   ├── capabilities.py    capability presets (readonly/edit/destructive/full)
│   ├── exec_tools.py      run_command policy implementation
│   ├── file_tools.py      outline-first file I/O tools
│   ├── patch_engine.py    fuzzy patching, in-memory syntax check, snapshots
│   ├── pricing.py         pre-flight price and capability guardrails
│   ├── security.py        path traversal guard, blocked files, secret masking
│   ├── analytics.py       cost ledger, budget checks, CLI reports
│   ├── i18n.py            CLI localization (EN/PL); MCP responses are always EN
│   └── ui.py              ANSI helpers with plain-text fallback
├── tests/test_botex.py    unit and integration suite
├── docs/TUTORIAL.md       this file
├── skills/botex/SKILL.md  agent-facing usage contract
├── ROADMAP.md             deferred features with rationale
└── botex.config.json      committed defaults
```

Runtime artifacts: `.snapshots/`, `.agent_analytics.json`, `.model_pricing.json`;
all are gitignored.

## 2. Core mechanisms

### A. Outline-first file access
Agents never read whole large files at once. They call `get_file_outline`
to see top-level symbols and line ranges, then `read_file_lines(path, start, end)`
for the exact slice they need.

### B. Context pruning
After a patch is accepted, earlier full-file reads in the conversation history
are replaced with a short placeholder. This prevents the message history from
growing quadratically during multi-step edits.

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
Every mutating tool snapshots the target file to `.snapshots/<task_id>/` first.
If the engine detects a no-progress loop or hits a fatal error, it restores the
original files. The CLI provides `--snapshots` and `--clean-snapshots` for
maintenance.

### F. Security boundaries

* `resolve_safe_path()` blocks path traversal outside `workspace_dir`.
* Read/write of `.env*`, `*.pem`, `*.key`, `id_rsa`, and similar patterns is blocked.
* Secret-like strings are masked in file reads, command output, and analytics.
* OpenRouter requests always include `provider: {"data_collection": "deny"}`.
* `reasoning.max_tokens` (in `extra_body`) caps hidden thinking so reasoning
  models cannot consume the entire `max_tokens` budget before emitting output.

## 3. Capability modes

`run_subagent` accepts a `mode` parameter. Presets are fail-closed; an unknown
mode returns `CONFIG_ERROR`.

| Mode | Tools exposed |
|---|---|
| `readonly` | list_dir, get_file_outline, read_file, read_file_lines |
| `edit` (default) | readonly + apply_patch, create_file |
| `destructive` | edit + delete_file, move_file |
| `full` | destructive + run_command |

Explicit flags (`allow_destructive`, `allow_exec`, `allow_net`) can only widen a
preset, never narrow it. `run_command` additionally requires
`exec.enabled: true` in the config; without it `CAP_EXEC` is removed regardless
of mode/flags.

`net` is deliberately **not** part of any preset — it is an exfiltration and
prompt-injection axis, so it requires a separate `allow_net=true` opt-in. It
exposes the restricted, read-only `read_url` tool, scoped by `net.policy`
(`caller` / `allowlist` / `public` / `off`) plus per-run `net_allowed_hosts` /
`net_allowed_urls` grants. All modes keep SSRF/IP-pinning, redirect, port, size,
and text-only guards.

## 4. Cost and capability guardrails

* **Pricing guardrail** — before the first API call the resolved model is checked
  against the provider's `/models` catalog (cached per provider in
  `.model_pricing.json`). Models above `pricing.max_*_per_mtok` are rejected
  locally with `PRICE_EXCEEDED`.
* **Capability guardrail** — models marked by the catalog as not supporting
  tool calls are rejected with `UNSUPPORTED`. For providers that publish no
  capability metadata, set `providers.<name>.capabilities.<slug> =
  {"supports_tools": true}`; this declaration overrides the catalog.
* **Budget** — `budget_limit_usd` is checked at the start of each task.
  Exceeding it returns `BUDGET_EXCEEDED`.
* **Limits** — `max_turns` (steps), `max_tokens` (per-turn completion ceiling),
  `max_duration_s` (wall-clock limit, checked between turns).

Router profiles such as `openrouter/auto` and `openrouter/auto-beta` can route
to reasoning backends unpredictably and may burn the whole `max_tokens` budget
on reasoning. Prefer an explicit `model` or a non-reasoning profile for
repeatable tasks.

## 5. Termination statuses

`ok` is `true` **only** when `status == "DONE"` — it certifies that the task
contract was verified (required output exists on disk / report delivered), not
that the model claimed success or that bytes changed.

| Status | Meaning |
|---|---|
| `DONE` | Task contract verified |
| `UNSUPPORTED` | Explicit `STATUS: UNSUPPORTED` marker, or the model lacks tool calls |
| `INCOMPLETE` | No useful output after strikes/nudges |
| `TOKEN_LIMIT` | `max_tokens` exhausted without visible output; common with reasoning models |
| `TIME_LIMIT` | `max_duration_s` exceeded between turns |
| `MAX_TURNS_REACHED` | Step limit reached; partial progress stays in `files_touched` |
| `STAGNANT_ROLLBACK` | No-progress loop detected; changes rolled back |
| `DONE_WITHOUT_WRITE` | Required `output_path` never written or salvaged |
| `VERIFICATION_FAILED` | `verify_command` failed twice; changes rolled back |
| `REQUEST_TIMEOUT` | A single provider request exceeded `request_timeout_s` |
| `BUDGET_EXCEEDED` | Daily spend cap exceeded |
| `PRICE_EXCEEDED` | Model over the configured price cap; rejected locally |
| `CONFIG_ERROR` | Invalid provider, mode, policy, or parameters |
| `API_ERROR` | Provider call failed; rollback attempted if changes existed |
| `ERROR` | Pre-run failure such as missing `workspace_dir` |

Each failure also carries a machine-readable `failure_kind` (e.g.
`model_refusal`, `no_progress`, `provider_auth_error`, `contract_failed`) that
drives the `model_fallbacks` retry policy.

## 6. Using BoteX through MCP

### Available MCP tools

* `run_subagent(task, ...)` — run a bounded editing task; blocks until done
* `start_task(task, ...)` — detached execution; returns a `task_id`
* `get_task_status(task_id)` — poll a detached task (`result` text + `result_data` dict)
* `save_memory` / `search_memory` / `read_memory` — per-workspace Memory Vault
* `fetch_url(url)` — caller-side URL fetch (not exposed to the internal loop)
* `get_outline(path, workspace_dir)` — symbol skeleton of a file
* `get_stats(period)` — cost/token report from analytics
* `check_health()` — provider, key, model, and budget readiness
* `clean_snapshots(max_age_days, max_total_mb)` — prune old snapshots
* `recommend_models(task_type, limit)` — live OpenRouter cost/quality ranking

`run_subagent` is a single blocking call; for long runs prefer `start_task` +
`get_task_status`. Clients can also issue multiple independent `tools/call`
requests in parallel to fan out work. For large repositories, prefer several
narrow tasks with explicit `files` over one broad "review everything" request.

### `run_subagent` parameters

```json
{
  "task": "Add email validation to auth/validators.py and add tests",
  "files": ["auth/validators.py"],
  "workspace_dir": "./your-project",
  "mode": "edit",
  "recipe": "",
  "profile": "coding",
  "provider": "",
  "model": "",
  "max_turns": 0,
  "max_tokens": 0,
  "max_duration_s": 0,
  "budget_limit_usd": -1,
  "allow_destructive": false,
  "allow_exec": false,
  "allow_net": false,
  "net_allowed_hosts": [],
  "net_allowed_urls": [],
  "output_path": "",
  "verify_command": "",
  "api_key": ""
}
```

Model resolution priority: `model` > `profile` > `mode_profiles[mode]` >
`default_profile`. Zeros and empty strings fall back to configured defaults.

## 7. Configuration

Resolution order (lowest to highest priority):

1. `botex.config.json` — committed defaults
2. `botex.config.local.json` — gitignored local overrides
3. `BOTEX_*` environment variables

Key sections:

* `providers.<name>` — `base_url`, `api_key_env`, `extra_body`, `capabilities`,
  `models` profiles, and per-profile `model_fallbacks` retry chains.
* `engine.default_profile`, `engine.mode_profiles`, `engine.max_turns`,
  `engine.max_tokens`, `engine.reasoning_max_tokens`, `engine.request_timeout_s`,
  `engine.max_duration_s`, `engine.temperature`, `engine.budget_limit_usd`,
  `engine.allow_destructive`, `engine.default_mode`,
  `engine.max_concurrent_tasks`, `engine.task_result_ttl_s`.
* `net.policy`, `net.allowed_hosts`, `net.allowed_urls`, `net.timeout_s`,
  `net.max_bytes`, `net.max_requests_per_task`, `net.max_redirects` —
  `read_url`/`fetch_url` destination policy.
* `exec.enabled`, `exec.allowlist`, `exec.deny_args`, `exec.timeout_s`,
  `exec.max_output_bytes` — `run_command` master switch and policy.
* `pricing.enabled`, `pricing.max_input_per_mtok`, `pricing.max_output_per_mtok`,
  `pricing.on_unknown`, `pricing.cache_ttl_hours`.
* `paths.snapshot_dir`, `paths.analytics_file`, `paths.env_file`,
  `paths.snapshot_auto_prune` (+ age/size quotas).
* `app.referer` — your attribution URL; the name "BoteX" is fixed.
* `analytics.store_full_text`, `analytics.preview_chars`.
* `ui.language` — `en` or `pl`; affects CLI only.

## 8. CLI usage

```bash
# interactive REPL
botex repl -w ./my-project --profile coding

# one-shot tasks
botex run "Add a --verbose flag to cli.py" -w ./project --profile coding
botex run "Fix failing test" --files tests/test_app.py --max-turns 25 --budget 0.50
botex run "Remove the legacy module" -w ./project --mode destructive
botex run "Quick code review" -w ./project --mode readonly --max-duration 300 --max-tokens 4000

# maintenance
botex --stats
botex --stats --month
botex --history <task_id>
botex --snapshots
botex --clean-snapshots
botex health
botex config
botex models
botex serve
```

`botex` with no arguments starts the MCP server when stdin is a pipe; on an
interactive terminal it enters the REPL. Use `botex serve` to force server mode.

## 9. Test suite

```bash
python tests/test_botex.py
```

Expected output — one `[PASS]` line per suite (security, syntax validation,
fuzzy patching & rollback, file tools, destructive ops, capability modes, exec
policy, pricing, network guards, fetch policy, fallback & verify, async task
registry, write-guard, provider adapter, analytics, MCP structured output, ZDR,
review regression, tool-misuse guards, read-before-write, memory vault, recipes,
MCP memory & stats), ending with:

```
[SUCCESS] ALL BOTEX ENGINE TESTS PASSED!
```

A second, opt-in suite `tests/test_live_e2e.py` runs against the real OpenRouter
API; it requires `OPENROUTER_API_TESTS_KEY` and skips itself otherwise.

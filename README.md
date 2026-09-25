# BoteX — Autonomous Code Execution Harness

BoteX is an agent-agnostic, MCP-native execution harness for autonomous
code-editing subagents. Any MCP-compatible client (IDE agents, desktop
assistants, orchestrators) can delegate multi-step coding tasks to it.

Key features:

- **Outline-First I/O** — agents read symbol outlines before requesting specific line ranges
- **Read-Before-Write Gate** — prevents hallucinated edits by requiring models to inspect files before patching
- **Context Pruning** — stale file reads are compacted after successful patches
- **Pre-write syntax validation** — code is linted in memory before touching disk
- **Multi-tier fuzzy patching** — tolerant to CRLF/LF and indentation differences
- **Workspace Memory Vault** — portable, persistent markdown/YAML context storage per workspace
- **Personas & Recipes** — specialized workflow modes (planner, reviewer, security-reviewer, build-resolver, tdd)
- **Snapshot rollback** — automatic restore on stagnation or critical failure
- **Security layer** — path traversal guard, `.env`/key blocking, secret masking (DLP)
- **Zero Data Retention** — `provider.data_collection=deny` on every OpenRouter call
- **Cost ledger** — per-task analytics, budget limits, rich CLI reports with DONE rates

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.10+ and an `OPENROUTER_API_KEY`. The harness resolves the
key **internally** — no need to pass it from the MCP client. Lookup order:

1. `OPENROUTER_API_KEY` environment variable
2. `.env` in the process working directory
3. `paths.env_file` in the config / `BOTEX_ENV_FILE` env var (custom dotenv path)
4. `.env` in the project root (next to `server.py`)
5. `secrets.openrouter_api_key` in **`botex.config.local.json`** (gitignored)

> Never put the key in `botex.config.json` — that file is committed to the
> repository. The local config is gitignored and additionally blocked from
> the subagent's file tools.

So for a self-contained setup just drop a `.env` next to `server.py`:

```
OPENROUTER_API_KEY=sk-or-...
```

## Running as an MCP server

The `botex` launcher is context-aware: spawned by an MCP client (piped
stdin) it serves the stdio MCP protocol; run on an interactive terminal with
no arguments it prints help and drops into the REPL. `botex serve` forces
server mode manually.
Three ways to get it:

- **`botex.cmd` / `botex.sh`** — zero-install shims shipped in the repo
  (they auto-detect `py`/`python3`/`python`)
- **`pip install .`** — installs a real `botex` command via `pyproject.toml`
- **`python server.py`** — direct interpreter invocation

Register the launcher as a stdio MCP server in any client:

```json
{
  "mcpServers": {
    "botex": {
      "command": "C:/path/to/harness/botex.cmd"
    }
  }
}
```

The server exposes task tools `run_subagent`, `start_task`,
`get_task_status`, Memory Vault tools `save_memory`, `search_memory`, `read_memory`,
caller-side `fetch_url`, and utility tools `get_outline`, `get_stats`,
`check_health`, `clean_snapshots`, `recommend_models`.

### Delegating a task

```json
{
  "task": "Add email validation to auth/validators.py and cover it with tests",
  "files": ["auth/validators.py"],
  "workspace_dir": "./your-project",
  "profile": "coding"
}
```

`run_subagent` parameters:

| param | default | meaning |
|---|---|---|
| `task` | — | task description |
| `files` | `[]` | primary target files (hint) |
| `workspace_dir` | `.` | project root the agent is confined to |
| `model` | `""` | explicit model; empty = resolve from provider config/profile |
| `profile` | `"default"` | model profile from the provider's `models` (`default`, `coding`, `auto-beta`) |
| `provider` | `""` | provider from `providers` config (`openrouter`, `nvidia`); empty = provider flagged `default: true` |
| `mode` | `""` | capability preset: `readonly`, `edit`, `destructive`, `full`; empty = `engine.default_mode` |
| `recipe` | `""` | operational persona / workflow prompt (`planner`, `code-explorer`, `reviewer`, `security-reviewer`, `build-resolver`, `tdd`) |
| `max_turns` | `0` | tool-loop step cap; `0` = configured default |
| `max_tokens` | `0` | per-turn completion cap; `0` = configured default |
| `max_duration_s` | `0` | wall-clock limit in seconds; `0` = configured default |
| `budget_limit_usd` | `-1` | daily spend cap; negative = configured default, `0` = unlimited |
| `allow_destructive` | `false` | expose `delete_file`/`move_file` to the agent — grant only with user consent |
| `allow_exec` | `false` | expose `run_command` (also needs `exec.enabled: true`) — trusted workspaces only |
| `allow_net` | `false` | expose `read_url` under the configured `net.policy` |
| `net_allowed_hosts` | `[]` | per-run public host authorization (`caller` replaces defaults, `public` narrows, `allowlist` may only narrow) |
| `net_allowed_urls` | `[]` | per-run exact URLs or trailing-slash URL subtrees |
| `api_key` | `""` | per-request provider key; overrides env/.env/config resolution |
| `output_path` | `""` | required output file; DONE is rejected until it is written |
| `verify_command` | `""` | allowlisted post-write check; requires exec authorization |

### Result contract

`run_subagent` returns the legacy pretty-text report as its text content
**plus** the full engine result as `structuredContent` (a published
`outputSchema` describes it; text-only clients see no change). Detached
tasks expose the same split via `get_task_status`: `result` holds the
pretty text, `result_data` the structured dict.

Key fields: `ok`, `status`, `failure_kind`, `task_id`, `summary`,
`files_touched`, `exec_ran`, `rollback_verified`, `steps`, `duration_s`,
`cost_usd`, `lines_added`/`lines_removed`, `attempts[]`, `fallback_from`
(deprecated alias for the last failed candidate — `attempts[]` carries
the chain).

**`ok` is true only when `status == "DONE"`** — it means the task
contract was verified, not that the model claimed success or that bytes
changed. A partially-written run under a limit returns `ok: false` with
the progress still visible in `files_touched`.

The contract derives from the request: `output_path` → the file must
exist on disk, non-empty and syntax-valid; `mode="readonly"` → a
non-empty analysis report; otherwise → an edit task where a no-change
DONE is accepted after one confirmation nudge. Model claims and
`files_touched` alone never produce `DONE`.

| status | meaning | typical `failure_kind` |
|---|---|---|
| `DONE` | contract verified | — |
| `INCOMPLETE` | no useful output after strikes/nudges | `model_refusal`, `no_progress`, `model_tool_misuse`, `contract_failed` |
| `UNSUPPORTED` | explicit `STATUS: UNSUPPORTED` marker, or the model lacks tool calls | `capability_missing`, `tool_call_unsupported`, `model_tool_misuse` |
| `API_ERROR` | provider call failed | `provider_auth_error` (401), `provider_policy_denied` (403/404), `provider_unavailable` (429/5xx/conn), `provider_error` |
| `REQUEST_TIMEOUT` | a single provider request exceeded its bound | `request_timeout` |
| `TOKEN_LIMIT` | `max_tokens` exhausted with no visible output | `token_exhausted` |
| `TIME_LIMIT` | wall-clock limit hit between turns | `time_exhausted` |
| `MAX_TURNS_REACHED` | step limit hit | `turns_exhausted` |
| `STAGNANT_ROLLBACK` | no-progress loop; changes rolled back | `no_progress` |
| `DONE_WITHOUT_WRITE` | required output never written/salvaged | `contract_failed` |
| `VERIFICATION_FAILED` | `verify_command` failed twice; rolled back | `contract_failed` |
| `PRICE_EXCEEDED` | model over the pricing cap (pre-flight) | `budget_or_price_block` |
| `BUDGET_EXCEEDED` | daily spend cap reached | `budget_or_price_block` |
| `CONFIG_ERROR` | bad provider/mode/policy/verify config | `config_error` |
| `ERROR` | workspace/security pre-run failure | `workspace_or_security` |

### Model fallback (`model_fallbacks`)

Each provider profile may list backup slugs under
`providers.<name>.model_fallbacks.<profile>`. When a candidate fails with
a fallbackable `failure_kind` (model refusals, no-progress, provider
policy/unavailability, resource limits), the next candidate is tried —
`attempts[]` records each candidate's model/status/failure_kind/cost.
Failures a different model cannot fix (`provider_auth_error`,
`config_error`, `BUDGET_EXCEEDED`, `VERIFICATION_FAILED`, workspace
errors) never fall back.

A mutated workspace is **never** handed to the next candidate dirty:
file mutations fall back only after `rollback_task` is *verified*
complete (snapshot mutation registry, normalized paths — `move_file`
restores both endpoints; `run_command` executions are unverifiable and
always block fallback). `engine.max_task_duration_s` caps the whole
candidate chain (`0` = per-attempt `max_duration_s` only).

## Capability modes

Modes are named presets over orthogonal capability flags
(`read`, `write`, `destructive`, `exec`, `net`). Explicit `allow_*` flags
may only **widen** a preset, never narrow it — `readonly` stays readonly.
Unknown modes fail closed with an error.

| mode | capabilities |
|---|---|
| `readonly` | read tools only |
| `edit` (default) | read + `apply_patch`/`create_file` |
| `destructive` | edit + `delete_file`/`move_file` |
| `full` | destructive + `run_command` (exec) |

`net` is intentionally **not** part of any preset — it is an
exfiltration/prompt-injection axis and must be opted into separately via
`allow_net`. `net.policy` controls the destination scope: `caller` accepts
per-run hosts/URL rules, `allowlist` is limited to configured entries,
`public` allows any public host for non-sensitive workspaces, and `off`
disables network access. All modes keep SSRF/IP-pinning, redirect, port,
size, and text-only guards. Resolution: explicit flags > `mode` param >
`engine.default_mode` config > `edit`. In headless MCP the mode is set at
task start — there is no mid-task prompting; grant `destructive`/`full`
only after user consent. CLI: `botex run --mode full`, or `mode <preset>`
inside `botex repl`.

## Destructive operations

`delete_file` and `move_file` exist but are **never exposed to the agent by
default** (`engine.allow_destructive: false` / mode below `destructive`).
Authorization model:

- **Headless / MCP** — there is no way to ask mid-task, so the caller must
  pre-authorize: `run_subagent(..., allow_destructive=true)` or
  `mode="destructive"`/`"full"` (per task) or
  `engine.allow_destructive: true` / `BOTEX_ALLOW_DESTRUCTIVE=1` (global).
- **Interactive CLI** — without the flag, an interactive terminal gets a
  per-operation `[y/N]` prompt; `--allow-destructive` skips prompting.
  In non-interactive contexts (pipes, CI) the tools stay hidden.

Every delete/move snapshots the file first — `rollback_task` can restore it.

## Command execution (`run_command`)

> **Warning — this is NOT a sandbox.** `run_command` spawns a real process
> with the operator's privileges. The policy below narrows the surface but
> cannot make arbitrary execution safe: test runners execute repository
> code by design. Enable it only for trusted workspaces.

`run_command` lets the agent verify its own changes (`pytest`, `ruff`,
`git status/diff`, …). It is gated by **three** layers — all required:

1. `exec.enabled: true` in the config (master switch, default **off**;
   also `BOTEX_EXEC_ENABLED=1`),
2. the `exec` capability — `mode="full"` or `allow_exec=true` per request
   (headless pre-authorization) — or an interactive `[y/N]` prompt in CLI,
3. the command policy: `exec.allowlist` (binaries only),
   `exec.deny_args` (eval flags, git mutations, network tools),
   `exec.timeout_s`, `exec.max_output_bytes`.

Guarantees: `shell=False` argv execution (no metacharacter expansion),
workspace as cwd, secret-named env vars stripped from the child process,
process-tree kill on timeout, stdout/stderr truncated and secret-masked.

Note: command side effects are **not** covered by snapshot rollback —
files changed by a command are unknown to the harness.

## Configuration

Resolution order (lowest → highest priority):

1. `botex.config.json` — committed defaults
2. `botex.config.local.json` — gitignored machine-local overrides
3. `BOTEX_*` environment variables

```json
{
  "providers": {
    "openrouter": {
      "default": true,
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY",
      "extra_body": {"provider": {"data_collection": "deny"}, "reasoning": {"max_tokens": 3000}},
      "models": { "default": "openrouter/auto", "auto-beta": "openrouter/auto-beta", "coding": "openrouter/pareto-code", "fast": "deepseek/deepseek-v4-flash-0731" }
    },
    "nvidia": {
      "default": false,
      "base_url": "https://integrate.api.nvidia.com/v1",
      "api_key_env": "NVIDIA_API_KEY",
      "extra_body": {},
      "models": { "default": "meta/llama-3.3-70b-instruct", "coding": "qwen/qwen3-coder-480b-a35b-instruct" }
    }
  },
  "engine":  { "max_turns": 15, "max_tokens": 8000, "reasoning_max_tokens": 16000, "request_timeout_s": 300, "max_duration_s": 900, "max_task_duration_s": 0, "temperature": 0.1, "budget_limit_usd": 0.5, "default_mode": "edit", "require_read_before_write": true },
  "net":     { "enabled": true, "policy": "caller", "allowed_hosts": ["docs.openrouter.ai", "openrouter.ai"], "allowed_urls": [] },
  "exec":    { "enabled": false, "allowlist": ["pytest", "python", "ruff", "git", "..."], "deny_args": ["python -c", "git push", "..."], "timeout_s": 120 },
  "paths":   { "snapshot_dir": ".snapshots", "analytics_file": ".agent_analytics.json", "env_file": "" },
  "app":     { "referer": "" },
  "analytics": { "store_full_text": false, "preview_chars": 160 },
  "ui":      { "language": "auto" }
}
```

This is an abridged view — `botex.config.json` holds the complete defaults
(fallback chains, full `exec` allow/deny lists, pricing caps, snapshot quotas).

Two separate reasoning limits exist: `providers.<name>.extra_body.reasoning.max_tokens`
is sent to the provider and caps hidden thinking server-side, while
`engine.reasoning_max_tokens` is BoteX's local per-turn `max_tokens` raise,
applied once a response reports reasoning tokens. `ui.language` accepts
`en`, `pl`, or `auto` (OS locale; override with `BOTEX_LANG`).

The product name (`BoteX`, sent as `X-Title`) is part of the harness
identity and is **not** configurable; `app.referer` is your own
attribution URL for OpenRouter.

**Providers.** Each provider is an OpenAI-compatible endpoint with its
own `base_url`, `api_key_env`, `extra_body`, and model `profiles`. The
provider flagged `"default": true` is used when a request does not name
one; `run_subagent(provider="nvidia", profile="coding")` selects that
provider's coding profile. API keys resolve per provider:
`api_key` param > `<PROVIDER>_API_KEY` env > `.env`/`env_file` >
`secrets.<provider>_api_key` in `botex.config.local.json`.
Provider fields are strictly scoped: a provider's `extra_body` goes only
to its own endpoint — OpenRouter's `data_collection: deny` (ZDR) is
centrally enforced there and cannot be weakened per call — and
attribution headers (`X-Title`/`HTTP-Referer`) are sent only to
OpenRouter unless another provider sets `"attribution_headers": true`.
Provider exception text is secret-masked and truncated to
`engine.max_error_chars` before it reaches results, MCP responses, or
analytics. Note: `data_collection: deny` is OpenRouter-specific — other
providers apply their own data policies. `reasoning.max_tokens` caps
hidden "thinking" on reasoning models so they cannot exhaust the whole
`max_tokens` budget before emitting output (`TOKEN_LIMIT`) — raise it in
`botex.config.local.json` for harder tasks (deep-merge keeps ZDR).

**Model tiers.** Each provider maps profile names (`default`, `coding`,
`fast`) to its own model slugs — the tier names are portable, the slugs are
provider-specific. When a call names neither `model` nor `profile`, the
capability mode picks the tier via `engine.mode_profiles`
(`readonly`→`fast`, `destructive`/`full`→`coding`, `edit`→default). Before
any API call, the provider's `/models` catalog also gates the resolved
model: over-cap price → `PRICE_EXCEEDED`; no tool-call support →
`UNSUPPORTED` (providers without capability metadata fall under
`pricing.on_unknown`). Providers that publish no capability fields can
declare them explicitly: `providers.<name>.capabilities.<slug>
= {"supports_tools": true}` — the declaration wins over catalog metadata.

**Persistent defaults.** `botex config` shows the effective config;
`botex config provider <name>` / `config model [provider] <slug>` /
`config profile <name>` / `config mode <preset>` write your defaults to
`botex.config.local.json` (gitignored) — no need to repeat `--profile` /
`--provider` / `--mode` flags on every call.

Environment overrides: `BOTEX_DEFAULT_MODEL`, `BOTEX_CODING_MODEL`,
`BOTEX_AUTO_BETA_MODEL` (per-profile `BOTEX_<PROFILE>_MODEL`),
`BOTEX_MAX_TURNS`, `BOTEX_MAX_TOKENS`, `BOTEX_TEMPERATURE`,
`BOTEX_BUDGET_USD`, `BOTEX_DEFAULT_MODE`, `BOTEX_DEFAULT_PROFILE`,
`BOTEX_EXEC_ENABLED`, `BOTEX_SNAPSHOT_DIR`, `BOTEX_ANALYTICS_FILE`,
`BOTEX_ENV_FILE`, `BOTEX_REFERER`, `BOTEX_ANALYTICS_FULL_TEXT`.

**Pricing guardrail.** Before the first API call, the resolved model is
checked against the provider's published price list
(`GET {base_url}/models`, cached in gitignored `.model_pricing.json`).
Over-cap models are rejected locally with `PRICE_EXCEEDED` — zero cost:

```json
"pricing": {
  "enabled": true,
  "max_input_per_mtok": 5.0,
  "max_output_per_mtok": 5.0,
  "on_unknown": "allow",
  "cache_ttl_hours": 24
}
```

`0` disables a given cap. Router/meta models (`openrouter/auto`,
`openrouter/auto-beta`, `openrouter/pareto-code`) report dynamic pricing —
`on_unknown` decides whether they pass (`"deny"` forces explicit priced
models). Providers that do not publish prices (NVIDIA's `/models` returns
a catalog without pricing fields) fall under `on_unknown` as well —
their models are currently free-tier anyway. `botex models` lists configured profiles with live
prices; `botex config price input|output <usd>` persists caps. Env
overrides: `BOTEX_PRICE_MAX_INPUT`, `BOTEX_PRICE_MAX_OUTPUT`.

**Analytics privacy.** Ledger entries never persist secrets: run
summaries are secret-masked before being written to
`.agent_analytics.json`, and by default only a truncated preview is
kept — the full text survives solely as a `summary_sha256` hash.
Set `analytics.store_full_text` (or `BOTEX_ANALYTICS_FULL_TEXT=1`) only
if you accept storing complete masked summaries on disk.

> **Note on ZDR accounts:** if your OpenRouter key enforces Zero Data
> Retention at the account level, routing only reaches ZDR-compliant
> endpoints. `openrouter/auto` resolves automatically; if a chosen model
> returns 404 `zdr-violation`, pick another one via `model`/`profile`.

## CLI

### Interactive mode

```bash
botex repl -w ./my-project --profile coding
# botex> refactor the parser module
# botex> exit
```

The REPL prompts for destructive-op approval (`[y/N]`) when the agent
requests `delete_file`/`move_file`.

### Run a task headlessly (no MCP client)

```bash
botex run "Add a --verbose flag to cli.py" -w ./my-project --profile coding
botex run "Fix the failing test" --files tests/test_app.py --max-turns 25
botex run "Remove the legacy module" -w ./proj --allow-destructive
```

Flags: `--files`, `--workspace/-w`, `--model`, `--profile`, `--provider`,
`--mode`, `--max-turns`, `--max-tokens`, `--max-duration`, `--budget`,
`--api-key`, `--allow-destructive`, `--allow-exec`, `--allow-net`,
`--net-host` (repeatable), `--net-url` (repeatable), `--output-path`,
`--verify-command`.
Exit code is `0` when the result `ok` field is true — that is, only on
`DONE`. A run that hits `MAX_TURNS_REACHED`/`TIME_LIMIT` after writing
files exits non-zero; the partial progress is still on disk and listed
in `files_touched`.

### Analytics & maintenance

```bash
botex --stats              # weekly report (tasks, tokens, cost per model)
botex --stats --month      # monthly report
botex --history <task_id>  # details of a specific run
botex --snapshots          # snapshot disk usage
botex --clean-snapshots    # prune old snapshots
botex health               # environment readiness check
```

(`python server.py ...` works identically — the launcher only resolves the
interpreter for you.)

## Repository layout

```
server.py            MCP server entry point (+ CLI dispatcher, main())
botex.cmd, botex.sh  zero-install launchers (auto-detect interpreter)
pyproject.toml       pip-installable; exposes the `botex` command
botex/
  engine.py          autonomous tool loop, context pruning, ZDR
  config.py          layered configuration loader
  file_tools.py      outline-first file I/O tools
  patch_engine.py    fuzzy patching, syntax validation, snapshots
  security.py        path traversal guard, secret masking, .gitignore awareness
  capabilities.py    capability modes (readonly/edit/destructive/full)
  exec_tools.py      policy-controlled run_command (allowlist, timeout, masking)
  analytics.py       cost/token ledger, budget checks, CLI reports
  pricing.py         pre-flight model price guardrail (provider catalog)
  providers.py       provider-neutral adapter: request scoping, response
                     normalization, key resolution, error sanitization
  contracts.py       result contracts: status/failure_kind taxonomy,
                     fallback policy, TaskContract resolution
  i18n.py            CLI localization (en/pl; MCP responses stay English)
  ui.py              ANSI styling helpers (CLI only, graceful fallback)
tests/test_botex.py  unit & integration suite (python tests/test_botex.py)
tests/test_live_e2e.py opt-in live E2E suite against OpenRouter
                     (requires OPENROUTER_API_TESTS_KEY; skips otherwise)
docs/TUTORIAL.md     technical deep dive
ROADMAP.md           deferred features & design rationale
skills/botex/        SKILL.md — consumer-facing usage guide for agents
botex.config.json    default configuration
LICENSE, NOTICE      Apache-2.0 license + attribution/trademark notices
```

Runtime artifacts (`.snapshots/`, `.agent_analytics.json`,
`.model_pricing.json`, `__pycache__/`) are gitignored.

**For agents consuming BoteX:** `skills/botex/SKILL.md` is a
progressive-disclosure usage guide — when to delegate, how to scope
`mode`, pre-authorization rules, and status semantics. Copy it into your
agent's skills directory (e.g. `.claude/skills/botex/`, `.agents/skills/`)
so the calling agent knows the contract.

## License & Trademark

BoteX is released under the **Apache License 2.0** — see `LICENSE`.
Derivative works must retain the attribution notices in `NOTICE`.

The **BoteX name and logo are trademarks of Jakub Grzesiak**
(https://jg-webtech.pl). The license covers the code, not the brand:

- You may fork, modify, and redistribute the code under Apache-2.0.
- You may NOT distribute forks or derivatives under the name "BoteX",
  nor use the name/logo in a way that suggests affiliation with or
  endorsement by the BoteX project.
- Reasonable, customary use to describe origin ("based on BoteX",
  "fork of BoteX") is permitted and required by the NOTICE retention
  clause.
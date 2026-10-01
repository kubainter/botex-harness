# BoteX — Execution Harness for AI Coding Agents

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)

Giving an LLM direct, unrestricted access to your repository usually ends the same way: it hallucinates line numbers, corrupts indentation, writes broken syntax, eats your token budget reading thousands of irrelevant lines, or gets stuck in an infinite fix-break loop.

**BoteX** is a guardrail harness designed specifically for autonomous code-editing agents. It sits between an MCP client (such as Devin, Antigravity, Claude Desktop, or Cursor) and your codebase, enforcing deterministic safeguards before any code ever touches your disk.

---

## Why BoteX?

Instead of trusting the model to behave, BoteX wraps every action in safety gates:

* **Outline-first inspection**: Agents inspect symbol skeletons (`get_outline`) before asking for specific line ranges. This prevents dumping massive files into context and slashes token waste.
* **Read-before-write gate**: Models cannot patch a file they haven't inspected in the current session. No blind edits, no guessing.
* **Pre-write syntax validation**: Code modifications are validated in memory (e.g. `ast.parse` for Python) *before* saving. If the patch breaks syntax, it gets rejected on the spot.
* **Multi-tier fuzzy patching**: Patches are whitespace- and newline-resilient (CRLF/LF agnostic with indentation detection), so minor formatting mismatches don't break execution.
* **Automatic snapshot rollback**: Every touched file is snapshotted before modification. If an agent loops, stagnates, or fails verification, the entire workspace reverts cleanly.
* **Hard budget & pricing caps**: Pre-flight checks verify model pricing against live provider catalogs. If a task exceeds its budget or a model is overpriced, BoteX aborts before spending a cent.
* **Strict privacy & Zero Data Retention (ZDR)**: Enforces `provider.data_collection: deny` on OpenRouter calls, blocks `.env`/secrets, masks API keys in logs, and prevents path traversal attacks.

---

## Quickstart

### 1. Installation

Requires Python 3.10+:

```bash
pip install -r requirements.txt
```

*(Optional: install globally via `pip install .` to get the `botex` CLI command).*

### 2. Add your API key

BoteX handles provider keys internally, so calling MCP clients don't need to pass credentials on every request. Create a `.env` file next to `server.py`:

```env
OPENROUTER_API_KEY=sk-or-v1-...
```

*Key lookup order: `OPENROUTER_API_KEY` env var → local `.env` → custom `paths.env_file` → `botex.config.local.json` (gitignored).*

### 3. Connect to your MCP client

You can register BoteX using the included zero-install launchers (`botex.cmd` on Windows, `botex.sh` on Linux/macOS) or directly with Python.

Add this snippet to your client's MCP configuration (e.g. `claude_desktop_config.json` or Antigravity's `mcp_config.json`):

```json
{
  "mcpServers": {
    "botex": {
      "command": "C:/path/to/harness/botex.cmd"
    }
  }
}
```

---

## How It Works

Every delegated task goes through an explicit contract-driven lifecycle:

```
Delegated Task
     │
     ▼
[Pre-flight check] ──► Validate budget, model capabilities & price cap
     │
     ▼
[Tool Loop]        ──► Symbol outline ─► Target read ─► In-memory patch ─► Syntax check
     │
     ├─► Success   ──► Verify contract (file exists, syntax valid, verify_command passed) ─► DONE
     │
     └─► Failure   ──► Rollback snapshots ─► Try next candidate (model_fallbacks) or abort
```

A task returns `ok: true` **only when its completion is verified on disk**, not just because an LLM said "I'm done".

---

## MCP Tools Reference

BoteX exposes a focused set of MCP tools:

### Task Execution
* **`run_subagent(task, ...)`**: Runs a full coding task synchronously and blocks until finished. Returns both human-readable text and detailed structured metadata.
* **`start_task(task, ...)`**: Launches a task asynchronously in the background and returns a `task_id`.
* **`get_task_status(task_id)`**: Polls progress, logs, and results for a background task.

### Workspace Memory Vault
* **`save_memory(key, content, category)`**: Saves persistent notes, decisions, or architectural context across agent sessions.
* **`read_memory(key)`** / **`search_memory(query)`**: Retrieves saved workspace memory.

### Diagnostics & Utility
* **`get_outline(path, workspace_dir)`**: Returns a high-level symbol outline of a file without reading all lines.
* **`get_stats(period)`**: Generates token usage and cost reports from the local analytics ledger.
* **`check_health()`**: Checks readiness of providers, models, keys, and budget limits.
* **`clean_snapshots(max_age_days, max_total_mb)`**: Prunes old workspace backup snapshots.
* **`recommend_models(task_type, limit)`**: Queries live OpenRouter pricing/quality rankings for coding tasks.
* **`fetch_url(url)`**: Safe, caller-side URL fetching with strict host and size filters.

---

## Task Delegation & Parameters

Example payload for `run_subagent`:

```json
{
  "task": "Add email validation to auth/validators.py and cover it with unit tests",
  "files": ["auth/validators.py"],
  "workspace_dir": "./my-project",
  "profile": "coding"
}
```

### Key Parameters

| Parameter | Default | Description |
|---|---|---|
| `task` | *(required)* | Clear description of what needs to be implemented or fixed. |
| `files` | `[]` | List of primary target files to nudge the agent toward. |
| `workspace_dir` | `.` | Root directory the agent is restricted to. |
| `profile` | `"default"` | Model profile configured in `botex.config.json` (`default`, `coding`, `fast`). |
| `mode` | `"edit"` | Capability preset: `readonly`, `edit`, `destructive`, `full`. |
| `recipe` | `""` | Specialized workflow persona (`planner`, `code-explorer`, `reviewer`, `security-reviewer`, `build-resolver`, `tdd`). |
| `max_turns` | `0` | Max tool-loop iterations (`0` uses configured default, usually 15). |
| `budget_limit_usd`| `-1` | Hard spend limit for this task (`-1` uses global config, `0` = unlimited). |
| `allow_destructive`| `false` | Explicit opt-in for `delete_file` and `move_file`. |
| `allow_exec` | `false` | Explicit opt-in for `run_command` (requires `exec.enabled: true` in config). |
| `allow_net` | `false` | Explicit opt-in for web access via `read_url`. |
| `output_path` | `""` | Enforce that this exact file must be written and non-empty for `DONE` status. |
| `verify_command` | `""` | Verification command (e.g. `pytest tests/test_auth.py`) that must pass before completion. |

---

## Safety & Capability Modes

Permissions follow the principle of least privilege. Modes are presets over core capabilities:

| Mode | Allowed Operations | Typical Use Case |
|---|---|---|
| `readonly` | Outline inspection, ranged file reads, memory search. | Architecture reviews, exploratory passes. |
| `edit` *(default)* | Read tools + `apply_patch` + `create_file`. | Standard feature work and bug fixing. |
| `destructive` | Edit tools + `delete_file` + `move_file`. | Refactoring, renaming, file reorganizations. |
| `full` | Destructive tools + gated `run_command` execution. | Full autonomous loop (edit → test → fix). |

> [!NOTE]
> Network access (`allow_net`) is **never** enabled by default in any preset. It must be explicitly enabled per request or configured via `net.allowed_hosts`.

### Command Execution (`run_command`) Safety

BoteX is **not a full OS sandbox**. When `allow_exec` is granted, processes run under the operator's account. However, BoteX guards execution with multiple layers:
1. **Disabled by default**: Requires `exec.enabled: true` in configuration.
2. **Binary allowlist**: Only pre-approved executables (`pytest`, `python`, `ruff`, `git`, etc.) can be called.
3. **Deny rules**: Blocks dangerous arguments (`python -c`, `git push`, `rm`, shell piping, network downloads).
4. **Isolated environment**: Executes directly (`shell=False`), workspace set as cwd, secret environment variables stripped, and execution timeouts strictly enforced.

---

## Model Fallbacks (`model_fallbacks`)

If a model refuses a task, burns its token limit without output, or hits provider rate limits, BoteX automatically falls back to alternative models defined in `botex.config.json`:

```json
"model_fallbacks": {
  "coding": ["qwen/qwen3-coder", "deepseek/deepseek-v3.2", "google/gemini-2.5-flash"]
}
```

Before handing off the workspace to the next candidate model, **BoteX verifies that any partial changes from the failed attempt were completely rolled back**. A candidate never inherits a dirty or half-broken workspace.

---

## Configuration

Settings are resolved hierarchically (lowest to highest priority):
1. `botex.config.json` — committed defaults (provider profiles, engine limits, security lists).
2. `botex.config.local.json` — machine-local overrides (**gitignored**; place custom keys or limits here).
3. `BOTEX_*` environment variables.

Example `botex.config.local.json`:

```json
{
  "engine": {
    "budget_limit_usd": 1.0,
    "max_turns": 20
  },
  "pricing": {
    "max_input_per_mtok": 3.0,
    "max_output_per_mtok": 6.0
  }
}
```

---

## Standalone CLI & REPL

BoteX can be run directly from your terminal without any MCP client:

### Interactive REPL

```bash
botex repl -w ./my-project --profile coding
```
*Prompts for `[y/N]` confirmation whenever the agent wants to delete or move files.*

### Headless One-Shot Tasks

```bash
# Run a specific task
botex run "Add --verbose flag to cli.py" -w ./my-project --profile coding

# Fix a bug with a tight turn budget
botex run "Fix the failing auth test" --files tests/test_auth.py --max-turns 15

# Refactoring with destructive permissions
botex run "Clean up legacy modules" -w ./my-project --allow-destructive
```

### Maintenance & Observability

```bash
botex health               # Verify API keys, provider connectivity, and model pricing
botex --stats              # View weekly token consumption and spend per model
botex --stats --month      # Monthly cost report
botex --history <task_id>  # Inspect exact prompts, tool calls, and diffs for a run
botex --snapshots          # Check disk usage of backup snapshots
botex --clean-snapshots    # Prune old snapshot backups
```

---

## Project Structure

```
server.py              MCP server entry point & CLI dispatcher
botex.cmd / botex.sh   Zero-install launchers (auto-detects python environment)
pyproject.toml         Package configuration (exposes `botex` CLI command)
botex/
  engine.py            Autonomous execution loop, context management & rollback triggers
  file_tools.py        Safe outline-first reading, file creation, moving & deletion
  patch_engine.py      Fuzzy patch matching, syntax validation & snapshot management
  security.py          Path traversal defenses, secret masking & gitignore filtering
  exec_tools.py        Policy-controlled command execution & argument filtering
  capabilities.py      Permission presets (readonly, edit, destructive, full)
  analytics.py         Cost/token ledger, budgeting & summary reporting
  pricing.py           Live model price guardrail & catalog checks
  providers.py         Provider adapters (OpenRouter, NVIDIA, etc.) & request normalizer
  contracts.py         Task completion verification & status taxonomy
  recipes/             Specialized agent personas (planner, reviewer, tdd, etc.)
tests/
  test_botex.py        Comprehensive test suite (24 unit & integration test groups)
  test_live_e2e.py     Live end-to-end integration suite against OpenRouter
docs/
  TUTORIAL.md          Architecture deep dive and technical walkthrough
```

---

## Testing

Run the full local test suite (no API key required):

```bash
python -m pytest tests/test_botex.py
```

Expected result:
```
============================= 24 passed in 7.90s ==============================
```

---

## License & Trademark

BoteX is licensed under the **Apache License 2.0** — see [LICENSE](LICENSE) for details. Derivative works must retain attribution notices as specified in [NOTICE](NOTICE).

**The BoteX name and logo are trademarks of Jakub Grzesiak** ([jg-webtech.pl](https://jg-webtech.pl)). You are free to fork and modify the code under Apache-2.0, but forks or derived distributions should not use the name "BoteX" or suggest official endorsement without prior written consent.
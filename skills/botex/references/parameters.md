# BoteX Caller Parameter Contract

`run_subagent` and `start_task` share the exact same 22 caller-facing parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task` | `string` | *(required)* | Precise, self-contained description of the work to perform. |
| `workspace_dir` | `string` | `""` | Hard boundary directory for file operations. Empty falls back to `engine.default_workspace` or server CWD. |
| `files` | `array[string]` | `[]` | Primary target file hints. These prime model attention; they do NOT restrict access. |
| `mode` | `string` | `"edit"` | Capability preset: `"readonly"`, `"edit"`, `"destructive"`, `"full"`. |
| `recipe` | `string` | `""` | Persona prompt injection: `"planner"`, `"code-explorer"`, `"reviewer"`, `"security-reviewer"`, `"build-resolver"`, `"tdd"`, `"architecture-adr"`, `"security-taint-hardener"`, `"ui-craftsman"`. |
| `profile` | `string` | `""` | Provider profile from config (`"default"`, `"coding"`, `"auto-beta"`, `"fast"`, `"review"`). |
| `provider` | `string` | `""` | Provider name (`"openrouter"`, `"nvidia"`, `"google"`, `"ollama"`, `"openrouter-free"`). |
| `model` | `string` | `""` | Explicit model slug; overrides `profile`. |
| `max_turns` | `integer` | `0` | Maximum tool-loop iterations (`0` uses configured default, e.g. 45 for edit, 30 for readonly). |
| `max_tokens` | `integer` | `0` | Per-turn completion token cap (`0` uses config default; reasoning models require 8000+). |
| `max_duration_s` | `number` | `0` | Wall-clock limit in seconds (`0` uses config default). |
| `budget_limit_usd` | `number` | `-1.0` | Maximum spend in USD for this task (`-1.0` uses global limit, `0.0` disables limit). |
| `allow_destructive` | `boolean` | `false` | Explicit opt-in for `delete_file` and `move_file`. |
| `allow_exec` | `boolean` | `false` | Explicit opt-in for `run_command` (requires `exec.enabled: true` in server config). |
| `allow_net` | `boolean` | `false` | Explicit opt-in for `read_url` external network reading. |
| `net_allowed_hosts` | `array[string]` | `[]` | Permitted hostnames for `read_url`. |
| `net_allowed_urls` | `array[string]` | `[]` | Permitted exact URLs or URL prefixes (with trailing `/`). |
| `api_key` | `string` | `""` | Per-request provider key override. MUST be left empty unless dynamically overriding. |
| `output_path` | `string` | `""` | Enforces non-empty file creation at this relative path. Requires mutating mode. |
| `verify_command` | `string` | `""` | Verification command (e.g. `pytest tests/test_foo.py`). Requires execution capability and `exec.enabled=true`. |
| `allow_non_zdr` | `boolean` | `false` | Required to permit `:free` or data-retaining models. |
| `approval_policy` | `string` | `"high_risk"` | HITL policy: `"none"`, `"high_risk"`, `"mutations"`, `"all_risky"`. |

## Capability Presets & Hard Read-Only Invariant

| Mode | Allowed Worker Tools | Use Case | Mutating? |
|---|---|---|---|
| `readonly` | `list_dir`, `get_file_outline`, `read_file`, `read_file_lines` (`+ read_url` if `allow_net=true`) | Code audits, architecture analysis, bug triage. | NO |
| `edit` *(default)* | `readonly` tools + `apply_patch`, `create_file` | Code editing, bug fixes, feature implementation. | YES |
| `destructive` | `edit` tools + `delete_file`, `move_file` | Refactorings involving deletions or moves. | YES |
| `full` | `destructive` tools + `run_command` | Autonomous edit-test-fix loops, test suites. | YES |

- `readonly` and `chat` are hard read-only modes: passing `allow_destructive=true`, `allow_exec=true`, or `output_path` raises `CONFIG_ERROR`.
- `output_path` enforces a non-empty file write in mutating modes (`edit`, `destructive`, `full`); missing/empty output terminates as `DONE_WITHOUT_WRITE`.

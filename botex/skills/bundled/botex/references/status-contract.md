# BoteX Result & Status Handling Contract

## Verification Ledger Invariant

Lifecycle `status == "DONE"` from `get_task_status` indicates ONLY that the run finished, not that the task succeeded.

A task is **`verified`** ONLY when BOTH conditions hold:
- Asynchronous (`start_task` / `get_task_status`): `result_data.ok == true && result_data.status == "DONE"`
- Synchronous (`run_subagent`): `structuredContent.ok == true && structuredContent.status == "DONE"`

Any other combination (`ok == false`, `BLOCKED`, `FAILED`, `VERIFICATION_FAILED`, `MAX_TURNS_REACHED`, etc.) MUST NOT be reported as `verified` or `SUCCESS`.

## Lifecycle States (`get_task_status`)

- `QUEUED`: Pending in execution queue.
- `RUNNING`: Executing in engine tool loop.
- `DONE`: Run terminated (inspect `result_data.ok` and `result_data.status`).
- `FAILED` / `CANCELLED` / `NOT_FOUND` / `TOO_MANY_QUEUED`: Queue or system failure.

## Engine Status Taxonomy

| Engine Status | Meaning / Condition | `ok` | Rollback Behavior |
|---|---|---|---|
| `DONE` | Contract verified: syntax valid, `verify_command` passed (or findings reported for `readonly`). | `true` | Preserved on disk |
| `BLOCKED` | Halted by security/policy gate or missing approval in headless mode. | `false` | Automatic rollback |
| `FAILED` | Failed contract criteria or unrecoverable logic failure. | `false` | Automatic rollback |
| `INCOMPLETE` | No progress after nudges or task refused. | `false` | Automatic rollback |
| `UNSUPPORTED` | Model issued `STATUS: UNSUPPORTED` or lacks tool-calling. | `false` | No changes made |
| `API_ERROR` | Provider API failure (auth, 5xx, network). | `false` | Automatic rollback |
| `REQUEST_TIMEOUT` | Provider call exceeded `request_timeout_s`. | `false` | Automatic rollback |
| `TOKEN_LIMIT` | Exhausted `max_tokens` (raise to 8000+ for reasoning models). | `false` | Automatic rollback |
| `TIME_LIMIT` | Exceeded `max_duration_s`. | `false` | Automatic rollback |
| `MAX_TURNS_REACHED` | Exhausted `max_turns`; partial edits may remain. | `false` | Partial preserved |
| `STAGNANT_ROLLBACK` | Repetitive no-progress loop detected. | `false` | Automatic rollback |
| `PRICE_EXCEEDED` | Model pricing exceeds `pricing.max_*_per_mtok`. | `false` | No changes made |
| `BUDGET_EXCEEDED` | Daily spend budget exceeded. | `false` | Automatic rollback |
| `ZDR_VIOLATION` | Non-ZDR/`:free` model used without `allow_non_zdr=true`. | `false` | No changes made |
| `DONE_WITHOUT_WRITE` | `output_path` set in mutating mode but file missing/empty. | `false` | Automatic rollback |
| `VERIFICATION_FAILED` | `verify_command` failed 3 consecutive attempts. | `false` | Automatic rollback |
| `CONFIG_ERROR` | Invalid parameter combination (e.g., `allow_exec` in `readonly`). | `false` | No changes made |
| `ABORTED` | Explicitly aborted by operator or API request. | `false` | Automatic rollback |
| `ERROR` | Invalid `workspace_dir` or pre-run security boundary failure. | `false` | No changes made |

## Structured Result Schema (`result_data` / `structuredContent`)

- `ok` (`boolean`): Master success certification.
- `status` (`string`): Engine termination status from above.
- `failure_kind` (`string | null`): Semantic failure classification (e.g., `contract_failed`, `token_exhausted`).
- `summary` (`string`): High-level description of changes or read-only findings.
- `files_touched` (`array[string]`): Relative paths created, modified, moved, or deleted.
- `lines_added` / `lines_removed` (`integer`): Net line delta.
- `exec_ran` (`boolean`): Whether `run_command` executed.
- `rollback_verified` (`boolean`): Confirms snapshot restoration on failure.
- `duration_s` (`number`) / `cost_usd` (`number`): Execution telemetry.
- `attempts` (`array[object]`): Model fallback audit log.
- `applied_skills` (`array[string]`): Server-side Skill Vault entries auto-injected by L0 triage.
- `routed_tier` / `routed_profile` (`string`, optional): Present when `engine.task_size_routing` assigned a tier profile.
- `syntax_rejections_detail` (`array[object]`, optional) / `rollback_reason` (`string`, optional): Contrastive failure detail on quality failures.
- `in_flight_state` (`object`): Serialized `InFlightTaskState` checkpoint (plan steps, findings, bounded `syntax_rejections`/`rollback_reasons` buffers).

The analytics ledger (`.agent_analytics.jsonl`) records additional attribution per run: `origin`, `recipe`, `taxonomy`, `routed_tier`, `applied_skills`, `applied_lessons`, `syntax_rejections`, `rollback_count`, `shadow_recommended_model` (advisory Layer C pick; routing is never overridden).

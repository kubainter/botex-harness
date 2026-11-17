# BoteX Troubleshooting & Normative Remedies

| Symptom / Status | Root Cause | Normative Caller Action |
|---|---|---|
| `CONFIG_ERROR` | Invalid parameter combination (e.g., unknown mode, `allow_destructive`/`allow_exec` or `output_path` in `readonly`). | Fix caller arguments. Never pass mutating flags or `output_path` to `readonly`. |
| `DONE_WITHOUT_WRITE` | `output_path` was set in a mutating mode but target file was not created or was empty. | Ensure task explicitly directs writing `output_path` in `edit`, `destructive`, or `full` mode. |
| `TOKEN_LIMIT` | Model exhausted `max_tokens` before emitting response (common with reasoning models). | Set `max_tokens` to at least `8000` or switch to a non-reasoning coding model. |
| `VERIFICATION_FAILED` | `verify_command` exited non-zero 3 consecutive times; partial changes rolled back. | Inspect test failure logs in `summary` or `read_scratchpad`; refine prompt with exact error details. |
| `ZDR_VIOLATION` | Attempted `:free` or data-retaining model while ZDR is enforced. | Pass `allow_non_zdr=true` or use provider `openrouter-free` when permitted. |
| `PRICE_EXCEEDED` | Model pricing exceeds configured max rate per million tokens. | Call `recommend_models` to select an affordable model or use standard profiles (`coding`, `fast`). |
| `BLOCKED` | High-risk operation attempted in headless mode without pre-authorization. | Grant explicit capability (`destructive`, `full`, or matching `allow_*` flag); `approval_policy` does not grant capabilities. |
| `STAGNANT_ROLLBACK` | Agent repeated identical actions without progress; workspace restored. | Provide clearer target files in `files` or decompose task into smaller steps. |
| `UNSUPPORTED` | Model lacks tool-calling capabilities or explicitly refused task. | Select a tool-capable model from `recommend_models`; do not retry identical prompt on same model. |

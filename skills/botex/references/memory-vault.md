# BoteX Two-Tier Memory Vault Flow

## Scopes

- `local` *(default)*: `<workspace_dir>/.botex/memory/` (gitignored). Use for workspace-private notes or ephemeral `temp-handoff` entries (`tags=["temp-handoff"]`, `expires: YYYY-MM-DD` — caller is responsible for cleanup via `delete_memory` when the task ends).
- `global`: `~/.botex/memory/` on the target BoteX server. In Cloud-First multi-host setups (including 1:1 `uv tool` + `botex edge` workstations connected to BoteX Cloud), shared knowledge lives on BoteX Cloud under `scope="global"` + `project="swarm"` (with server-derived `origin="<agent>@<host>"`). Never dump raw files or large JSON logs into prompts or memory entries ("derive, do not dump" — Zero Context Bloat).
- `swarm`: Optional read-only third root configured via `paths.swarm_memory_dir`.
- `all`: Queries `local`, `global`, and `swarm` concurrently.

## Standard Memory Workflow

1. **One-Shot Recall (Preferred):** `recall_memory(query="...", scope="all")`: expands hits via 1-hop wikilinks/backlinks and synthesizes facts with cited sources (up to 98% token reduction). Always check `recall_memory` (including `envfact` constraints for the target repo/Edge workspace) before falling back to `search_memory`.
2. **Diagnostic Search (Fallback):** `search_memory(query="...", scope="all")`: raw FTS5 candidate discovery or chronological filtering (`since`/`until`).
3. **Full Inspection:** `read_memory(memory_id="...", scope="local")` — read complete markdown, frontmatter, and `[[wikilinks]]`.
4. **Write / Update:**
   - Existing note: `patch_memory_section(memory_id, section="Compiled Truth", new_content=...)`
   - New concept: `save_memory(title="...", content="...", kind="decision"|"lesson", tags=[...])`
   - Verified runtime constraints (`FormRepair` / `AbsentTool`): tag as `tags=["lesson", "envfact"]` so future runs on the same host/repo reuse the working command form immediately.
5. **Graph Health & Maintenance:** `find_graph_health(scope="local")`, `get_backlinks(memory_id)`, `delete_memory(memory_id)`, `init_obsidian_vault()`, `read_scratchpad(memory_id)`.

## Memory Entry Kinds

- `context`: Architecture notes, environment setup, stack specs.
- `decision`: Architectural Decision Records (ADRs) and trade-offs.
- `handoff`: State transfer between tasks/subagents.
- `lesson`: Discovered edge cases, bug root causes, operational constraints.
- `belief`: Provisional observations (`confidence < 1.0`).
- `heuristic`: Actionable rules of thumb distilled from lessons.

## Vault Lifecycle & Provenance

- Every note carries `status` (`candidate` -> `active` -> `deprecated`/`superseded`) and `confidence` (0.0-1.0). `search_memory` defaults to `status="active"`; pass `status="all"` for a full scan, or an explicit `status` to target one state.
- Engine-extracted `lesson` notes start as `candidate` (confidence 0.4) and are promoted to `active` only by the Curator after verified evidence (`confidence >= 0.8` plus >= 2 distinct clean-run confirmations). Deterministic `envfact` lessons (`FormRepair` / `AbsentTool`, extracted from `run_command` traces or scoped to `edge:<device_id>/<workspace_id>` on Edge runs) are fast-tracked by the Curator to `status="active"`, `confidence=0.85` after 1 verified clean run (`DONE`, 0 rollbacks). Human-authored notes (`origin="human"`, `human-curated`, `frozen` tags) are never mutated by the loop.
- On BoteX Cloud, `origin` is derived server-side from sanitized `X-Agent-Id`/`X-Host` headers; client-supplied `origin` fields are ignored and reserved claims (`human`, `human-curated`, `operator`, `curator`) are rejected.
- Curator skill drafts land under `<workspace>/.botex/proposals/` only and require a reviewed PR to reach live `skills/`/`botex/skills/recipes/`; they are not part of the vault.

## Task Receipts

Upon every `DONE` status, BoteX writes a deterministic **Task Receipt** (`kind="handoff"`) to `<workspace_dir>/.botex/memory/` at 0 LLM token cost, recording touched files, line deltas, verification status, and reconciled telemetry.

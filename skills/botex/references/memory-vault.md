# BoteX Two-Tier Memory Vault Flow

## Scopes

- `local` *(default)*: `<workspace_dir>/.botex/memory/` (gitignored). Use for workspace-private notes or ephemeral `temp-handoff` entries (`tags=["temp-handoff"]`, `expires: YYYY-MM-DD`).
- `global`: `~/.botex/memory/` on the target BoteX server. In Cloud-First multi-host swarms, shared knowledge lives on BoteX Cloud under `scope="global"` + `project="swarm"` (with `origin="<agent>@<host>"`).
- `swarm`: Optional read-only third root configured via `paths.swarm_memory_dir`.
- `all`: Queries `local`, `global`, and `swarm` concurrently.

## Standard Memory Workflow

1. **One-Shot Recall (Preferred):** `recall_memory(query="...", scope="all")` — expands hits via 1-hop wikilinks/backlinks and synthesizes facts with cited sources (up to 98% token reduction).
2. **Diagnostic Search (Fallback):** `search_memory(query="...", scope="all")` — raw FTS5 candidate discovery or chronological filtering.
3. **Full Inspection:** `read_memory(memory_id="...", scope="local")` — read complete markdown, frontmatter, and `[[wikilinks]]`.
4. **Write / Update:**
   - Existing note: `patch_memory_section(memory_id, section="Compiled Truth", new_content=...)`
   - New concept: `save_memory(title="...", content="...", kind="decision"|"lesson")`
5. **Graph Health & Maintenance:** `find_graph_health(scope="local")`, `get_backlinks(memory_id)`, `delete_memory(memory_id)`, `init_obsidian_vault()`, `read_scratchpad(memory_id)`.

## Memory Entry Kinds

- `context`: Architecture notes, environment setup, stack specs.
- `decision`: Architectural Decision Records (ADRs) and trade-offs.
- `handoff`: State transfer between tasks/subagents.
- `lesson`: Discovered edge cases, bug root causes, operational constraints.
- `belief`: Provisional observations (`confidence < 1.0`).
- `heuristic`: Actionable rules of thumb distilled from lessons.

## Task Receipts

Upon every `DONE` status, BoteX writes a deterministic **Task Receipt** (`kind="handoff"`) to `<workspace_dir>/.botex/memory/` at 0 LLM token cost, recording touched files, line deltas, verification status, and reconciled telemetry.

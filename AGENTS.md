---
id: AGENTS
---

# BoteX - Operational Instructions for AI Agents

This document serves as a guide and rulebook for AI agents working on the **BoteX** repository.

## 1. Architecture Overview

BoteX is an execution harness for autonomous code-editing agents, functioning as an MCP server. Since v2.1.0 the package is organized into domain subpackages; legacy flat imports (`botex.engine`, `botex.memory`, ...) still resolve through the alias graft in `botex/__init__.py` (see `docs/IMPORT_MAP.md`). The main directories and modules are:

*   **Root files:**
    *   `server.py` - Main MCP server entry point and CLI dispatcher.
    *   `botex.config.json` - Committed default configuration.
    *   `botex.cmd` / `botex.sh` - Zero-install execution launchers.
    *   `pyproject.toml` / `requirements.txt` - Project packaging and dependencies.
    *   `ROADMAP.md` - Technical roadmap, deferred features, and architecture rationale.
*   **`botex/core/`** - Engine internals.
    *   `engine.py` - Autonomous execution loop, 5-gate in-payload failovers, in-flight state checkpointing, provider fallbacks, context management, deterministic task receipts, rollback triggers, attribution telemetry wiring, and `SHADOW_ROUTING` advisory emission.
    *   `state.py` - In-flight execution checkpoints (`InFlightTaskState`), plan/finding carryover and contrastive ring buffers (`syntax_rejections`, `rollback_reasons`, last 5 each) across model/provider fallbacks.
    *   `rate_limiter.py` - see `botex/providers/rate_limiter.py`.
    *   `contracts.py` - Task completion contract verification, strict JSON Schema (`TASK_COMPLETION_JSON_SCHEMA`), and status taxonomy.
    *   `events.py` - Engine and lifecycle event taxonomy (`EventKind`, incl. `SHADOW_ROUTING`).
    *   `capabilities.py` - Permission presets (modes: `readonly`, `edit`, `destructive`, `full`) and agency middleware.
    *   `approval.py` - Human-in-the-Loop approval manager, async pending actions, and risk policies.
    *   `context_guard.py` - Context compaction and verification output pruning.
    *   `context_inclusion.py` - Deterministic swarm context relay (`OutputNormalizer`, `DeterministicSignalGate`, `ContextKnapsack`).
    *   `meta_harness.py` - Task taxonomy classification, episodic meta-lesson extraction/recall (`extract_meta_lesson`, `get_relevant_meta_lessons`), `is_infra_failure` noise gate, `path:<dir>` lesson binding, and `pack_lessons_within_budget` (800 B cap).
    *   `tracing.py` - OTel-style span helpers.
*   **`botex/providers/`** - Provider-facing services.
    *   `providers.py` - Provider registry, adapters (`OpenRouterProvider`, `NvidiaProvider`, `GoogleAIStudioProvider`, `GenericOpenAIProvider`), `/api/v1/generation` telemetry reconciliation, and request normalizer.
    *   `analytics.py` - Cost/token ledger (`.agent_analytics.jsonl` + `analytics-archive/` rollups), run-attribution fields, advisory shadow meta-routing (`recommend_shadow_model`, `get_shadow_routing_advice`, `_SHADOW_COST_FLOOR_USD`), budget tracking, and CLI reports.
    *   `benchmarks.py` - Authenticated live model recommendations (`recommend_models`), tier-aware rankings, and task-size estimation (`estimate_task_tier`, `resolve_tier_profile`).
    *   `catalog.py` - Dynamic model catalog service with TTL caching and ZDR checks.
    *   `denylist.py` - Hierarchical model denylist filter (exact, glob, regex).
    *   `pricing.py` - Model pricing guardrail, live catalog checks, and cost caps.
    *   `rate_limiter.py` - Token-bucket rate limiting (RPM/TPM) and cooldown tracking for free and cloud providers.
    *   `retry.py` - Shared transient-failure backoff layer for provider calls (429, connection errors, provider-blessed overloads); when active the SDK client is built with `max_retries=0`.
    *   `wallet.py` - Credit balance, key budget status (`get_key_status`), and low-budget warnings.
    *   `delegation.py` - Delegation bridge to external specialized agents (e.g. Devin).
    *   `http_pool.py` - Pooled httpx clients; `prompt_cache.py` - prompt-cache policy gating; `adapters/zai.py` - Z.ai GLM adapter.
*   **`botex/tools/`** - Worker tool implementations.
    *   `patch_engine.py` - Fuzzy patch mechanism, in-memory syntax validation, and snapshot management.
    *   `file_tools.py` - Safe file reading (outline-first), creation, moving, and deletion.
    *   `security.py` - Protections against Path Traversal attacks, secret masking, and `.gitignore` filtering.
    *   `exec_tools.py` - Command execution with policy control and argument filtering.
    *   `net_tools.py` - SSRF-safe network fetching tools (`read_url`, `fetch_url`).
    *   `vision.py` - Vision input triage.
*   **`botex/memory/`** - Memory Vault stack.
    *   `memory.py` - Persistent two-tier Memory Vault (local `.botex/memory/` and global `~/.botex/memory/`) with wikilink graph, lifecycle statuses (`candidate`/`active`/`deprecated`/`superseded`), confidence scoring, `update_lesson_confidence` feedback, 1-hop recall context gathering, and scratchpad offload.
    *   `curator.py` - Facade re-exporting the `botex.ops.curator` service API.
    *   `embeddings.py` - Local embedding helpers supporting memory/graph features.
    *   `obsidian.py` - Obsidian PKM vault export, per-kind graph colors, and `_GRAPH_INDEX.md` generation.
*   **`botex/ops/`** - Autonomous operations.
    *   `curator/` - `CuratorService` nightly vault synthesis: graph health, batch drain, append-only linking, compiled truth, lesson promotion (step 6d, `promote_candidate_lessons`), PR-only skill crystallization (step 6e, `crystallize_skill_proposals` into `.botex/proposals/`), `recall_curator.py` multi-tier LLM recall cascade, Telegram dispatch.
    *   `maintenance.py` / `migrate_projects.py` - Maintenance and vault migration jobs.
*   **`botex/skills/`** - Skill Vault stack.
    *   `skills.py` - Server-side Skill Vault (`skills/<name>/SKILL.md` catalog, safe install/update with injection guards), L0 task triage with `applied_skills` telemetry, and `write_skill_proposal_draft` for PR-only `.botex/proposals/` drafts.
    *   `skillspector_gate.py` - SkillSpector pre-flight audit (`audit_skill_content`; threshold via `skills.audit.risk_threshold`).
    *   `recipes.py` - Recipe loader and system-prompt injection.
    *   `recipes/` - Bundled persona prompts (`planner`, `reviewer`, `tdd`, `build-resolver`, `security-reviewer`, `security-taint-hardener`, `code-explorer`, `architecture-adr`, `ui-craftsman`).
*   **`botex/server/`** - Interfaces.
    *   `server_cloud.py` - FastAPI Cloud Gateway, Remote MCP SSE/HTTP server, rate-limited task queue, REST/WebSocket API (`/api/wallet`, `/api/key/status`, `/api/analytics`, `/api/tasks`, `/api/approvals`, `/api/memory`, `/api/models`, `/api/recipes`, `/api/skills`, `/api/chat`), and memory-origin hardening (`_RESERVED_ORIGIN_AGENTS`, NFKC-folded `X-Agent-Id`/`X-Host` sanitization).
    *   `chat.py` - Conversational chat sessions, SSE/WebSocket token streaming, and read-only sandbox.
    *   `config.py` - Layered configuration loader (defaults < local < env) with `mtime` hot-reload, workspace containment, and `MCP_CLIENT_ORIGIN` provenance context.
    *   `plugins.py` - ZeroTrust subprocess plugin runner; `i18n.py` - CLI localization (EN/PL); `ui.py` - ANSI styling helpers.
*   **`web/`** - Vue 3 + Vite + Pinia + Tailwind CSS PWA frontend for the Cloud Gateway.
*   **`tests/`** - Test suite (67 files, 1344 collected tests: `test_botex.py`, `test_analytics.py`, `test_approval.py`, `test_caller_skill_contract.py`, `test_catalog.py`, `test_challenger_m4_3.py`, `test_challenger_m5_empiric.py`, `test_challenger_m7_concurrency.py`, `test_challenger_m7_empirical.py`, `test_challenger_m8_empiric.py`, `test_challenger_m8_empirical.py`, `test_challenger_m9_empirical.py`, `test_chat.py`, `test_chat_error_observability.py`, `test_circuit_breaker.py`, `test_config_keys_enforced.py`, `test_config_safety.py`, `test_containment_recall_gate.py`, `test_context_guard.py`, `test_context_inclusion.py`, `test_credits_stress.py`, `test_curator.py`, `test_curator_and_migration_f4.py`, `test_curator_f7.py`, `test_curator_tasks.py`, `test_delegation.py`, `test_denylist.py`, `test_deps_parity.py`, `test_e2e_contracts.py`, `test_embeddings.py`, `test_events.py`, `test_http_pool.py`, `test_idle_backend_layers.py`, `test_idle_layers_l1.py`, `test_integration.py`, `test_memory_hindsight_features.py`, `test_memory_recall.py`, `test_memory_twotier.py`, `test_memory_v2.py`, `test_meta_harness.py`, `test_module_aliases.py`, `test_nvidia_streaming.py`, `test_obsidian_edge_cases.py`, `test_obsidian_graph.py`, `test_ops_package.py`, `test_plugins_creator.py`, `test_prompt_cache.py`, `test_providers.py`, `test_rate_limiter.py`, `test_recall_curator.py`, `test_recall_observability.py`, `test_retry.py`, `test_security_hardening.py`, `test_server_cloud.py`, `test_server_f4_provenance.py`, `test_skills_parity.py`, `test_skills_security.py`, `test_stage_4_6.py`, `test_stage_4_7.py`, `test_stage_4_8.py`, `test_state_checkpoint.py`, `test_structured_contracts_v1.py`, `test_task_size_routing.py`, `test_tracing.py`, `test_tracing_wiring.py`, `test_vision_pipeline.py`, `test_wallet.py`, `test_zai_provider.py`, and live integration tests in `test_live_e2e.py`).
*   **`docs/`** - Documentation (`TUTORIAL.md`, `IMPORT_MAP.md`, `TELEGRAM_NOTIFICATIONS.md`).
*   **`skills/`** - Skill definitions: caller-side contract (`skills/botex/SKILL.md`) plus server-side packs (`architecture-planner`, `build-resolver`, `code-review`, `security-audit`, `tdd-verification`) managed by `botex/skills/skills.py`.

### 1.1. Self-Improvement Loop (Layers A-C)

BoteX records its own runs and feeds the evidence back deterministically. Learning stays local and evidence-gated: promotion into live behavior requires verified confirmation counts or a human-reviewed PR.

*   **Layer A (telemetry & attribution):** every run lands in `.agent_analytics.jsonl` with masked attribution fields (`origin`, `recipe`, `routed_tier`, `taxonomy`, `applied_skills`, `applied_lessons`, `syntax_rejections`, `rollback_count`, `shadow_recommended_model`), persisted only when non-empty.
*   **Episodic learning:** a task that recovers after a model failure persists a `kind="lesson"` note as `status="candidate"`, `confidence=0.4` (`extract_meta_lesson`), tagged `path:<dir>` for touched directories. `is_infra_failure` keeps provider-side noise (rate limits, timeouts, quota/auth/budget/config errors, HTTP 429) out of the vault. Recall (`get_relevant_meta_lessons`) surfaces only `active` lessons at `confidence >= 0.6`, path-bound lessons first, packed to <= 800 B. `update_lesson_confidence` nudges `+0.1` on `DONE` zero-rollback runs and `-0.15` on quality failures (never on infra failures or `ABORTED`); below `0.2` the note becomes `deprecated`.
*   **Curator promotion (step 6d):** `promote_candidate_lessons` flips `candidate` -> `active` only at `confidence >= 0.8` with >= 2 distinct `DONE` zero-rollback ledger confirmations (`applied_lessons` citations). `human`, `human-curated`, and `frozen` notes are never mutated. `promote_to_global=True` (code-level opt-in) copies verified lessons into `scope="global"`, `project="swarm"` behind a secret-leak guard.
*   **Layer B (PR-only skill crystallization, step 6e):** `crystallize_skill_proposals` clusters recurring `active` lessons (`min_cluster_size >= 5`, `path:`/domain tags or >= 2 co-occurring tokens) and drafts `learned-<tax>-<topic>` `SKILL.md` + `recipe.md` into `.botex/proposals/` only, after size, secret, prompt-injection, and SkillSpector gates. It never mutates `skills/` or `botex/skills/recipes/`; promotion requires a reviewed PR. Agents must not hand-edit proposal drafts into live skill directories.
*   **Layer C (shadow meta-routing):** `get_shadow_routing_advice` logs an advisory per-`(recipe, taxonomy)` model pick once >= 10 samples exist (`EventKind.SHADOW_ROUTING`, `_SHADOW_COST_FLOOR_USD` floor for `:free`, `hard_banned_models` empty, `actual_override: false`). It never overrides active routing.
*   **Provenance hardening:** `POST /api/memory` ignores the client-supplied `origin`; provenance derives from sanitized `X-Agent-Id`/`X-Host` headers, and reserved claims (`human`, `human-curated`, `operator`, `curator`) are rejected server-side.

## 2. Tools and Commands

*   **Testing (Pytest):** The test harness forcibly disables dotenv loading, so tests cannot consume provider keys from `.env`. Prefer targeted tests during iteration; run the full suite only when the change warrants it:
    ```bash
    BOTEX_DISABLE_DOTENV=1 PYTHONPATH=. python -m pytest tests/
    ```
    On PowerShell:
    ```powershell
    $env:BOTEX_DISABLE_DOTENV='1'
    $env:PYTHONPATH='.'
    python -m pytest tests/
    ```
*   **Linting and Formatting (Ruff):** The project uses Ruff. Always run both check and format validation before committing changes:
    ```bash
    ruff check .
    ruff format --check .
    ```
    To automatically fix lint issues and reformat:
    ```bash
    ruff check --fix .
    ruff format .
    ```
*   **Installation:** The application is defined in `pyproject.toml`. To install in development mode, you can use:
    ```bash
    pip install -e .
    ```

## 3. Rules and Constraints

### 3.1. Directories and Files Blocked from Editing
Under no circumstances should you modify the following locations:
*   `build/`, `dist/`, `*.egg-info/` - build process artifacts.
*   `.snapshots/` - directory for backup copies created during operation (modified only by the BoteX system itself).
*   `.env`, `botex.config.local.json` - configuration files containing sensitive data and developer environment secrets.
*   `.agent_analytics.json`, `.agent_analytics.jsonl`, `analytics-archive/`, `.model_pricing.json` - files storing state and history.
*   `.botex/proposals/` - Curator-generated skill draft workspace; promotion happens only via a reviewed PR, never by editing drafts into `skills/`.

### 3.2. Repository Conventions
*   **Commits:** Try to keep commit messages concise, factual, and in English. The title should not exceed 50 characters, and if a longer description is needed, add it after a blank line.
*   **Pull Requests:** Avoid exposing details of potential vulnerabilities (CRITICAL) in public PR descriptions, in accordance with the Zero Data Retention / leak-free rule. Remember that security-related fixes should be shorter than 50 lines of code.
*   **Comment & Docstring Hygiene (Zero-Bloat):** When writing logic, do NOT generate bloatware commentary — no per-line narration restating what the code already says, no boilerplate docstrings on trivial helpers. Comments and docstrings are reserved for non-obvious *why* (security boundaries, invariants, deliberate trade-offs); self-explanatory code ships uncommented. Do not strip existing docstrings/comments while editing adjacent code.

### 3.3. Deployment Flow & Internal-Surface Hygiene (HARD RULES)
*   **Single change path:** All BoteX logic changes flow exclusively through `local changes → PR → merge to main → deploy to VPS`. Never patch files or data directly on the VPS; never bypass the PR gate with a "quick" direct fix — including memory-vault data repairs, which must ship as code (e.g. maintenance jobs) through the normal pipeline.
*   **No internal surface in client code:** Files under `web/src/**` ship to browsers unauthenticated users can reach. They must NEVER embed internal file paths, module/class names, route names, or config keys of the backend (e.g. `botex/server/server_cloud.py`, `botex.config.json`). UI copy, starter prompts, placeholders and docs links stay generic; anything needing internals belongs behind authenticated API responses.

## 4. Autonomy Criteria

1.  **Act conservatively and autonomously:** In case of minor uncertainty when solving engineering problems, choose the most logical, conservative solution that aligns with existing patterns. However, **if you are not 100% sure about the solution (acting as a safety fuse), you must halt work and ask the user** how they want to proceed to ensure it is fixed properly.
2.  **Safe commands only:** If you must execute scripts, restrict yourself to the authorized allowlist (e.g., `pytest`, `python`, `ruff`, `git`).
3.  **Verify before committing:** Remember to run tests after making changes and check the syntax of the modifications introduced.
4.  **Pre-Flight Skill & Tool Security Audit (HARD RULE):** Before installing, cloning, or registering any third-party skill, plugin, or MCP tool into workspace or global directories (even upon explicit user request), agents MUST run a pre-flight security audit via Toolport (`toolport_call_tool(name: "skillspector__scan_skill", ...)`). If the scanner detects Prompt Injection, Data Exfiltration, or returns `risk_score > 30`, immediately halt (`BLOCKED`) and present findings to the user. If Toolport/SkillSpector is unavailable, degrade gracefully to a thorough manual inspection following Verified Agent Skills rules; silent unverified installation is strictly forbidden. When authoring or revising custom skills, execute `.\scripts\eval-skill.ps1` for Tier 1 Quality Check and Tier 2 Semantic Overlap verification.

## 5. CodeQL Static Analysis & CI/CD Invariants

### 5.1. Taint Tracking & Data-Flow Rules (CWE-312 / CWE-020 / CWE-078)
*   **Prioritize flow hardening over inline suppressions:** Do not rely on inline comment suppressions (`# lgtm[...]` is obsolete and ignored; `# codeql[...]` behavior depends on specific SARIF emission flags in CI). Always fix the taint flow and harden sanitization at the root rather than muting alerts.
*   **`re.sub` and string operations are TaintSteps, NOT sanitizers:** The CodeQL semantic model treats `re.sub`, `str.replace`, and f-strings as taint-propagating summary steps. Calling masking functions iteratively multiplies graph edges instead of severing them.
*   **Use Module Constant Mappings for Enums:** When normalizing dynamic parameters (e.g., `kind`, `mode`, `status`), never return the mutated user string (`kind.lower()`). Look up and return a statically declared module string literal (`_VALID_KINDS_MAP.get(norm, "default")`).
*   **Taint Decoupling & Sanitization Architecture:** When persisting text derived from untrusted inputs/keys, prefer strict schema validation and allowlist mappings. Where unstructured text must be stored verbatim, do not treat codepoint or identity round-trips as taint barriers: they leave the taint flow intact. Rely on `mask_secrets` and keep secret values out of the stored text.

### 5.2. CI/CD & Runtime Invariants
*   **Repository-wide Linting & Formatting:** Always run `ruff check .` and `ruff format --check .` across the entire repository root (`.`), not on scoped subsets of files, matching Section 2 conventions.
*   **Falsy & Scalar Edge Cases:** Avoid naive `if not value:` checks on polymorphic values, as they conflate legitimate values (`0`, `False`) with missing data (`None`). Use explicit guards (`if value is None:`) and account for `bool` being a subclass of `int` in Python.
*   **I/O & Buffer Deadlock Pitfalls:** Avoid sequential single-threaded `os.write()` followed by `os.read()` on `os.pipe()` for payloads exceeding the operating system pipe buffer (>64 KB). Use `tempfile.TemporaryFile`, `io.BytesIO`, or thread/event loops for large in-process data transfers.

---

## 6. Context Management & Knowledge Search (Toolport & context-mode)

This repository is fully indexed in the local FTS5 knowledge base of the `context-mode` plugin.
External engine bridges and sandboxes (context-mode, ue4ss-bridge, tw3se-bridge, skillspector) are served via **Toolport MCP Gateway** in **Lazy Discovery** mode, while autonomous harnesses (`botex`, `agy`) are connected directly:
- Search tools on-demand via `toolport_search_tools(query: "...")` (e.g. `query: "ctx"` or `query: "scan"`).
- Execute tools via `toolport_call_tool(name: "<server>__<tool>", arguments: {...})` (e.g. `context_mode__ctx_execute` or `skillspector__scan_skill`).

### 6.1. Knowledge Search (`context_mode__ctx_search`)
- Instead of repeatedly walking directories or executing multiple grep commands across modules, query `context_mode__ctx_search` first via Toolport.
- Designated source:
  - `source: "ai-harness"` — complete architecture, engine modules, security barriers, recipes, memory vault, and tests.
- Batch questions into a single `queries` array: `toolport_call_tool(name: "context_mode__ctx_search", arguments: {"queries": ["..."], "source": "ai-harness"})`.

### 6.2. In-Sandbox File Inspection (`context_mode__ctx_execute_file`)
- **NEVER dump entire files into the conversation** (neither via `view_file` over 100 lines nor shell commands).
- For summarizing, parsing AST, inspecting test matrices, or filtering configurations, use `context_mode__ctx_execute_file` (or `context_mode__ctx_execute` with Python/JS) and return only the derived answer.
- Reserve native `view_file` strictly for small, exact byte-ranges immediately preceding code modifications.

### 6.3. Heavy Command Output Sandboxing (`context_mode__ctx_execute` / `context_mode__ctx_batch_execute`)
- When running full pytest suites, CodeQL traces, or coverage matrices that produce extensive logs, execute or filter them through `context-mode` via Toolport to prevent raw byte flooding into the context window.

### 6.4. Memory Vault & Zero-Bloat Knowledge Recall (`recall_memory`)
- When querying project context, architectural decisions, or prior lessons from BoteX Memory Vault, ALWAYS use `recall_memory(query="...")` first via direct MCP.
- `recall_memory` performs automatic 1-hop graph expansion (wikilinks + backlinks) and server-side curator synthesis (hedged 4-tier cascade: local qwen2.5:3b → NVIDIA → Gemini → OR-free), returning concise facts and cited sources in 1 turn without token-stuffing agent history.
- **Vault Locality (Cloud-First):** Shared swarm knowledge lives in **BoteX Cloud** (`scope="global"`, `project="swarm"`). Physical vault location follows the called server endpoint (`botex` cloud vs `botex-local`), not `scope` alone. Never pass local Windows `workspace_dir` paths (`C:\Users\...`) to a remote Linux VPS for swarm queries — use `scope="global"`, `project="swarm"`, `workspace_dir=""` (remote `save_memory` also auto-populates `origin` from `X-Agent-Id`/`X-Host` headers or the `origin` parameter).
- Local notes (`scope="local"`) are reserved for workspace-private context or ephemeral `temp-handoff` entries (`tags=["temp-handoff"]`, `expires: YYYY-MM-DD`), which are swept automatically by the Curator.
- Swarm knowledge is also reachable via `scope="swarm"` (optional third read-only root, configured in `paths.swarm_memory_dir`); `scope="all"` includes it when configured.
- Use `read_memory(memory_id="...")` only when exact raw note contents must be inspected for surgical editing. Avoid multi-turn `search_memory` hunting loops. Every `recall_memory` response carries `recall_status`, `tier_attempts`, `workspace_searched` — inspect them before assuming a miss.
- **Vault lifecycle:** `kind="lesson"` notes carry a lifecycle (`candidate` -> `active` -> `deprecated`/`superseded`) plus a `confidence` score. Engine-extracted lessons start as `candidate` (0.4) and only the Curator promotes them to `active`; callers should leave lifecycle management to the loop and must never spoof `origin` claims (`human`, `human-curated`, `operator`, `curator` are reserved and server-rejected).




---

## 7. Multi-Agent Interoperability & Portable Path Invariant (VPS / Cloud Alignment)

### 7.1. Portable Repo-Relative Paths in Handoffs
*   **Zero Absolute Paths:** All agent-to-agent and agent-to-human communications containing `artifact_refs` or file pointers MUST use repository-relative paths with POSIX forward slashes (`/`), e.g., `botex/core/engine.py`, `src/services/auth.ts`, `docs/ADR-001.md`.
*   **Cross-Environment Interoperability:** Never emit absolute Windows drive paths (`C:\Users\...`) or Linux home paths (`/home/ubuntu/...`) in JSON handoff contracts. This ensures seamless interoperability between local developer workstations and remote Linux VPS instances (e.g. Oracle Linux ARM).

### 7.2. Skills <-> Recipes Parity Invariant
*   **Dual-Mode Parity:** BoteX engineering rules live symmetrically in two layers:
    1.  Public contract layer: `skills/<skill_name>/SKILL.md` (read by external orchestrators like Antigravity, Devin, Codex).
    2.  Kernel execution layer: `botex/skills/recipes/<recipe_name>.md` (injected directly into BoteX model prompts).
*   **Mandatory Synchronization:** Whenever engineering checklists, linting requirements, or runtime targets are updated, both layers must be updated in tandem. Parity is enforced by automated tests in `tests/test_skills_parity.py`.
*   **Dependencies Parity:** `requirements.txt` (the sole install source for CI and the VPS deploy) and `pyproject.toml` `[project.dependencies]` must list identical core dependencies with identical specifiers. Drift is enforced by `tests/test_deps_parity.py`.

### 7.3. Cloud Brain vs. Local Workspace Delegation (Edge Bridge)
*   **NEVER delegate local/unpushed file work to BoteX Cloud using a local host path (`C:\Users\...`):** On the remote Linux VPS, `resolve_workspace()` treats cross-host Windows paths as invalid for the server filesystem and falls back to the server's own checkout (`/opt/botex`, i.e., deployed `main`), silently missing unpushed branch edits or local files.
*   **Use `edge://<device_id>/<workspace_id>` when targeting a workstation:** Local file inspection, patching, and verification from BoteX Cloud require an active outbound `botex edge` runner (`docs/EDGE_BRIDGE.md`) connected to `/api/edge/ws`. Pass `workspace_dir="edge://<device_id>/<workspace_id>"` (or the registered `<workspace_id>`), and verify an active session via `GET /api/edge/devices` first if unsure.
*   **Fail-Stop when no edge runner is connected:** If `botex edge` is offline and the target files are unpushed or local-only, do **not** fall back to BoteX Cloud against `/opt/botex`; ask the user to start `botex edge` (`botex-edge`) or handle the local files locally.

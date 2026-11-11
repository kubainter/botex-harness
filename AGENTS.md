# BoteX - Operational Instructions for AI Agents

This document serves as a guide and rulebook for AI agents working on the **BoteX** repository.

## 1. Architecture Overview

BoteX is an execution harness for autonomous code-editing agents, functioning as an MCP server. The main directories and modules are:

*   **Root files:**
    *   `server.py` - Main MCP server entry point and CLI dispatcher.
    *   `botex.config.json` - Committed default configuration.
    *   `botex.cmd` / `botex.sh` - Zero-install execution launchers.
    *   `pyproject.toml` / `requirements.txt` - Project packaging and dependencies.
    *   `ROADMAP.md` - Technical roadmap, deferred features, and architecture rationale.
*   **`botex/`** - Main application package.
    *   `engine.py` - Autonomous execution loop, 5-gate in-payload failovers, in-flight state checkpointing, provider fallbacks, context management, deterministic task receipts, and rollback triggers.
    *   `state.py` - In-flight execution checkpoints (`InFlightTaskState`), plan/finding carryover across model/provider fallbacks.
    *   `rate_limiter.py` - Token-bucket rate limiting (RPM/TPM) and cooldown tracking for free and cloud providers.
    *   `curator.py` / `ops/curator/` - Autonomous Memory Vault synthesis, telemetry digests, and multi-tier LLM recall synthesis (`recall_curator.py`).
    *   `delegation.py` - Delegation bridge to external specialized agents (e.g. Devin).
    *   `events.py` - Engine and lifecycle event taxonomy (`EventKind`).
    *   `contracts.py` - Task completion contract verification, strict JSON Schema (`TASK_COMPLETION_JSON_SCHEMA`), and status taxonomy.
    *   `patch_engine.py` - Fuzzy patch mechanism, in-memory syntax validation, and snapshot management.
    *   `file_tools.py` - Safe file reading (outline-first), creation, moving, and deletion.
    *   `security.py` - Protections against Path Traversal attacks, secret masking, and `.gitignore` filtering.
    *   `exec_tools.py` - Command execution with policy control and argument filtering.
    *   `capabilities.py` - Permission presets (modes: `readonly`, `edit`, `destructive`, `full`) and agency middleware.
    *   `approval.py` - Human-in-the-Loop approval manager, async pending actions, and risk policies.
    *   `chat.py` - Conversational chat sessions, SSE/WebSocket token streaming, and read-only sandbox.
    *   `server_cloud.py` - FastAPI Cloud Gateway, Remote MCP SSE server, rate-limited task queue, and REST/WebSocket API (`/api/wallet`, `/api/key/status`, `/api/analytics`, `/api/tasks`, `/api/approvals`, `/api/memory`, `/api/models`, `/api/recipes`, `/api/skills`, `/api/chat`).
    *   `pricing.py` - Model pricing guardrail, live catalog checks, and cost caps.
    *   `analytics.py` - Cost/token ledger, generation latency/cache telemetry, budget tracking, and CLI reports.
    *   `providers.py` - Provider registry, adapters (`OpenRouterProvider`, `NvidiaProvider`, `GoogleAIStudioProvider`, `GenericOpenAIProvider`), `/api/v1/generation` telemetry reconciliation, and request normalizer.
    *   `retry.py` - Shared transient-failure backoff layer for provider calls (429, connection errors, provider-blessed overloads); when active the SDK client is built with `max_retries=0`.
    *   `catalog.py` - Dynamic model catalog service with TTL caching and ZDR checks.
    *   `denylist.py` - Hierarchical model denylist filter (exact, glob, regex).
    *   `wallet.py` - Credit balance, key budget status (`get_key_status`), and low-budget warnings.
    *   `benchmarks.py` - Authenticated live model recommendations (`recommend_models`) and tier-aware (`high` / `standard` / `budget`) rankings.
    *   `context_guard.py` - Context compaction and verification output pruning.
    *   `net_tools.py` - SSRF-safe network fetching tools (`read_url`, `fetch_url`).
    *   `memory.py` - Persistent two-tier Memory Vault (local `.botex/memory/` and global `~/.botex/memory/`) with wikilink graph, 1-hop recall context gathering, and scratchpad offload.
    *   `embeddings.py` - Local embedding helpers supporting memory/graph features.
    *   `obsidian.py` - Obsidian PKM vault export, per-kind graph colors, and `_GRAPH_INDEX.md` generation.
    *   `meta_harness.py` - Task taxonomy classification, patch probing, and meta-lesson extraction/recommendation (self-improvement layer).
    *   `recipes.py` - Recipe loader and system-prompt injection.
    *   `skills.py` - Server-side Skill Vault (`skills/<name>/SKILL.md` catalog, safe install/update with injection guards) and L0 task triage with `applied_skills` telemetry.
    *   `config.py` - Layered configuration loader (defaults < local < env) with `mtime` hot-reload.
    *   `i18n.py` - CLI localization (EN/PL).
    *   `ui.py` - ANSI styling helpers with plain-text fallback.
    *   `recipes/` - Bundled persona prompts (`planner`, `reviewer`, `tdd`, `build-resolver`, `security-reviewer`, `security-taint-hardener`, `code-explorer`, `architecture-adr`, `ui-craftsman`).
*   **`web/`** - Vue 3 + Vite + Pinia + Tailwind CSS PWA frontend for the Cloud Gateway.
*   **`tests/`** - Test suite (40 files, 650 collected tests: 	est_context_inclusion.py, 	est_memory_hindsight_features.py, `test_botex.py`, `test_rate_limiter.py`, `test_state_checkpoint.py`, `test_curator.py`, `test_delegation.py`, `test_events.py`, `test_stage_4_6.py`, `test_stage_4_7.py`, `test_stage_4_8.py`, `test_retry.py`, `test_skills_parity.py`, `test_server_cloud.py`, `test_approval.py`, `test_chat.py`, `test_wallet.py`, `test_catalog.py`, `test_denylist.py`, `test_providers.py`, `test_memory_twotier.py`, `test_memory_v2.py`, `test_context_guard.py`, `test_analytics.py`, `test_embeddings.py`, `test_meta_harness.py`, `test_obsidian_graph.py`, `test_obsidian_edge_cases.py`, `test_nvidia_streaming.py`, `test_credits_stress.py`, `test_challenger_m4_3.py`, `test_challenger_m5_empiric.py`, `test_integration.py`, `test_circuit_breaker.py`, `test_tracing.py`, `test_task_size_routing.py`, `test_structured_contracts_v1.py`, `test_caller_skill_contract.py`, `test_config_safety.py`, and live integration tests in `test_live_e2e.py`).
*   **`docs/`** - Documentation (`TUTORIAL.md`).
*   **`skills/`** - Skill definitions: caller-side contract (`skills/botex/SKILL.md`) plus server-side packs (`architecture-planner`, `build-resolver`, `code-review`, `security-audit`, `tdd-verification`) managed by `botex/skills.py`.

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
*   `.agent_analytics.json`, `.model_pricing.json` - files storing state and history.

### 3.2. Repository Conventions
*   **Commits:** Try to keep commit messages concise, factual, and in English. The title should not exceed 50 characters, and if a longer description is needed, add it after a blank line.
*   **Pull Requests:** Avoid exposing details of potential vulnerabilities (CRITICAL) in public PR descriptions, in accordance with the Zero Data Retention / leak-free rule. Remember that security-related fixes should be shorter than 50 lines of code.
*   **Comment & Docstring Hygiene (Zero-Bloat):** When writing logic, do NOT generate bloatware commentary — no per-line narration restating what the code already says, no boilerplate docstrings on trivial helpers. Comments and docstrings are reserved for non-obvious *why* (security boundaries, invariants, deliberate trade-offs); self-explanatory code ships uncommented. Do not strip existing docstrings/comments while editing adjacent code.

### 3.3. Deployment Flow & Internal-Surface Hygiene (HARD RULES)
*   **Single change path:** All BoteX logic changes flow exclusively through `local changes → PR → merge to main → deploy to VPS`. Never patch files or data directly on the VPS; never bypass the PR gate with a "quick" direct fix — including memory-vault data repairs, which must ship as code (e.g. maintenance jobs) through the normal pipeline.
*   **No internal surface in client code:** Files under `web/src/**` ship to browsers unauthenticated users can reach. They must NEVER embed internal file paths, module/class names, route names, or config keys of the backend (e.g. `botex/server_cloud.py`, `botex.config.json`). UI copy, starter prompts, placeholders and docs links stay generic; anything needing internals belongs behind authenticated API responses.

## 4. Autonomy Criteria

1.  **Act conservatively and autonomously:** In case of minor uncertainty when solving engineering problems, choose the most logical, conservative solution that aligns with existing patterns. However, **if you are not 100% sure about the solution (acting as a safety fuse), you must halt work and ask the user** how they want to proceed to ensure it is fixed properly.
2.  **Safe commands only:** If you must execute scripts, restrict yourself to the authorized allowlist (e.g., `pytest`, `python`, `ruff`, `git`).
3.  **Verify before committing:** Remember to run tests after making changes and check the syntax of the modifications introduced.

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
External engine bridges and sandboxes (context-mode, ue4ss-bridge, tw3se-bridge) are served via **Toolport MCP Gateway** in **Lazy Discovery** mode, while autonomous harnesses (`botex`, `agy`) are connected directly:
- Search tools on-demand via `toolport_search_tools(query: "...")` (e.g. `query: "ctx"`).
- Execute tools via `toolport_call_tool(name: "<server>__<tool>", arguments: {...})` (e.g. `context_mode__ctx_execute`).

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
- **ALWAYS pass `workspace_dir="C:\Users\kubai\.ai\harness"`** when querying swarm knowledge — the swarm vault lives in the harness repo, and the default workspace resolves elsewhere (verified trap: `recall_status=ZERO_NODES_FOUND` usually means wrong vault, not a dead model). Every response now carries `recall_status`, `tier_attempts`, `workspace_searched` — read them before assuming a miss.
- Swarm knowledge is also reachable via `scope="swarm"` (third read-only root, configured in `paths.swarm_memory_dir`); `scope="all"` includes it when configured.
- Use `read_memory(memory_id="...")` only when exact raw note contents must be inspected for surgical editing. Avoid multi-turn `search_memory` hunting loops; if you must use `search_memory`, pass the same `workspace_dir`.




---

## 7. Multi-Agent Interoperability & Portable Path Invariant (VPS / Cloud Alignment)

### 7.1. Portable Repo-Relative Paths in Handoffs
*   **Zero Absolute Paths:** All agent-to-agent and agent-to-human communications containing `artifact_refs` or file pointers MUST use repository-relative paths with POSIX forward slashes (`/`), e.g., `botex/engine.py`, `src/services/auth.ts`, `docs/ADR-001.md`.
*   **Cross-Environment Interoperability:** Never emit absolute Windows drive paths (`C:\Users\...`) or Linux home paths (`/home/ubuntu/...`) in JSON handoff contracts. This ensures seamless interoperability between local developer workstations and remote Linux VPS instances (e.g. Oracle Linux ARM).

### 7.2. Skills <-> Recipes Parity Invariant
*   **Dual-Mode Parity:** BoteX engineering rules live symmetrically in two layers:
    1.  Public contract layer: `skills/<skill_name>/SKILL.md` (read by external orchestrators like Antigravity, Devin, Codex).
    2.  Kernel execution layer: `botex/recipes/<recipe_name>.md` (injected directly into BoteX model prompts).
*   **Mandatory Synchronization:** Whenever engineering checklists, linting requirements, or runtime targets are updated, both layers must be updated in tandem. Parity is enforced by automated tests in `tests/test_skills_parity.py`.
*   **Dependencies Parity:** `requirements.txt` (the sole install source for CI and the VPS deploy) and `pyproject.toml` `[project.dependencies]` (packaging source of truth) must list identical core dependencies with identical specifiers. Drift is enforced by `tests/test_deps_parity.py`.

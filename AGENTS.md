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
    *   `engine.py` - Autonomous execution loop, context management, and rollback triggers.
    *   `contracts.py` - Task completion contract verification and status taxonomy.
    *   `patch_engine.py` - Fuzzy patch mechanism, in-memory syntax validation, and snapshot management.
    *   `file_tools.py` - Safe file reading (outline-first), creation, moving, and deletion.
    *   `security.py` - Protections against Path Traversal attacks, secret masking, and `.gitignore` filtering.
    *   `exec_tools.py` - Command execution with policy control and argument filtering.
    *   `capabilities.py` - Permission presets (modes: `readonly`, `edit`, `destructive`, `full`).
    *   `pricing.py` - Model pricing guardrail, live catalog checks, and cost caps.
    *   `analytics.py` - Cost/token ledger, budget tracking, and CLI reports.
    *   `providers.py` - Provider adapters (OpenRouter, NVIDIA, etc.) and request normalizer.
    *   `benchmarks.py` - Live model recommendations (`recommend_models`) and benchmark rankings.
    *   `net_tools.py` - SSRF-safe network fetching tools (`read_url`, `fetch_url`).
    *   `memory.py` - Persistent workspace Memory Vault (`.botex/memory/`).
    *   `recipes.py` - Recipe loader and system-prompt injection.
    *   `config.py` - Layered configuration loader (defaults < local < env).
    *   `i18n.py` - CLI localization (EN/PL).
    *   `ui.py` - ANSI styling helpers with plain-text fallback.
    *   `recipes/` - Bundled persona prompts (`planner`, `reviewer`, `tdd`, `build-resolver`, `security-reviewer`, `code-explorer`).
*   **`tests/`** - Test suite (unit tests in `test_botex.py` and live integration tests in `test_live_e2e.py`).
*   **`docs/`** - Documentation (`TUTORIAL.md`).
*   **`skills/`** - Caller-side skill definitions (`skills/botex/SKILL.md`).

## 2. Tools and Commands

*   **Testing (Pytest):** To run the full test suite, use the following command with the environment variable set:
    ```bash
    PYTHONPATH=. python -m pytest tests/test_botex.py
    ```
*   **Linting and Formatting (Ruff):** The project uses Ruff. To check the code:
    ```bash
    ruff check .
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

## 4. Autonomy Criteria

1.  **Act conservatively and autonomously:** In case of minor uncertainty when solving engineering problems, choose the most logical, conservative solution that aligns with existing patterns. However, **if you are not 100% sure about the solution (acting as a safety fuse), you must halt work and ask the user** how they want to proceed to ensure it is fixed properly.
2.  **Safe commands only:** If you must execute scripts, restrict yourself to the authorized allowlist (e.g., `pytest`, `python`, `ruff`, `git`).
3.  **Verify before committing:** Remember to run tests after making changes and check the syntax of the modifications introduced.

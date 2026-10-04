# BoteX - Operational Instructions for AI Agents

This document serves as a guide and rulebook for AI agents working on the **BoteX** repository.

## 1. Architecture Overview

BoteX is an execution harness for autonomous code-editing agents, functioning as an MCP server. The main directories and modules are:

*   **`botex/`** - Main application package.
    *   `server.py` - Main MCP server entry point and CLI dispatcher. Located in the root directory.
    *   `engine.py` - Autonomous execution loop, context management, and rollback triggers.
    *   `patch_engine.py` - Fuzzy patch mechanism, in-memory syntax validation, and snapshot management.
    *   `file_tools.py` - Safe file reading (outline-first), creation, moving, and deletion.
    *   `security.py` - Protections against Path Traversal attacks, secret masking, and `.gitignore` filtering.
    *   `exec_tools.py` - Command execution with policy control and argument filtering.
    *   `capabilities.py` - Permission presets (modes: `readonly`, `edit`, `destructive`, `full`).
    *   `pricing.py` & `analytics.py` - Budget protection, cost ledger, and model pricing checks.
    *   `providers.py` - Provider adapters (OpenRouter, etc.) and request normalizer.
*   **`tests/`** - Test suite (unit and end-to-end integration tests).
*   **`docs/`** - Documentation, including a detailed architecture deep dive (`TUTORIAL.md`).
*   **`recipes/`** - (in `botex/` package) Specialized agent personas (planner, reviewer, etc.).

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

1.  **Act conservatively and autonomously:** In case of uncertainty when solving engineering problems, choose the most logical, conservative solution that aligns with existing patterns, **instead of halting work and asking the user**.
2.  **Safe commands only:** If you must execute scripts, restrict yourself to the authorized allowlist (e.g., `pytest`, `python`, `ruff`, `git`).
3.  **Verify before committing:** Remember to run tests after making changes and check the syntax of the modifications introduced.

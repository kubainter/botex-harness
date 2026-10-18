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
    PYTHONPATH=. python -m pytest tests/
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

## 4. Autonomy Criteria

1.  **Act conservatively and autonomously:** In case of minor uncertainty when solving engineering problems, choose the most logical, conservative solution that aligns with existing patterns. However, **if you are not 100% sure about the solution (acting as a safety fuse), you must halt work and ask the user** how they want to proceed to ensure it is fixed properly.
2.  **Safe commands only:** If you must execute scripts, restrict yourself to the authorized allowlist (e.g., `pytest`, `python`, `ruff`, `git`).
3.  **Verify before committing:** Remember to run tests after making changes and check the syntax of the modifications introduced.

## 5. CodeQL Static Analysis & CI/CD Invariants

### 5.1. Taint Tracking & Data-Flow Rules (CWE-312 / CWE-020 / CWE-078)
*   **Prioritize flow hardening over inline suppressions:** Do not rely on inline comment suppressions (`# lgtm[...]` is obsolete and ignored; `# codeql[...]` behavior depends on specific SARIF emission flags in CI). Always fix the taint flow and harden sanitization at the root rather than muting alerts.
*   **`re.sub` and string operations are TaintSteps, NOT sanitizers:** The CodeQL semantic model treats `re.sub`, `str.replace`, and f-strings as taint-propagating summary steps. Calling masking functions iteratively multiplies graph edges instead of severing them.
*   **Use Module Constant Mappings for Enums:** When normalizing dynamic parameters (e.g., `kind`, `mode`, `status`), never return the mutated user string (`kind.lower()`). Look up and return a statically declared module string literal (`_VALID_KINDS_MAP.get(norm, "default")`).
*   **Taint Decoupling & Sanitization Architecture:** When persisting text derived from untrusted inputs/keys, prefer strict schema validation and allowlist mappings. Where unstructured text must be stored verbatim without external encryption/binary formats, document low-level decoupling barriers (such as codepoint decomposition) as context-specific workarounds and verify they preserve data fidelity across multi-byte UTF-8 encodings.

### 5.2. CI/CD & Runtime Invariants
*   **Repository-wide Linting & Formatting:** Always run `ruff check .` and `ruff format --check .` across the entire repository root (`.`), not on scoped subsets of files, matching Section 2 conventions.
*   **Falsy & Scalar Edge Cases:** Avoid naive `if not value:` checks on polymorphic values, as they conflate legitimate values (`0`, `False`) with missing data (`None`). Use explicit guards (`if value is None:`) and account for `bool` being a subclass of `int` in Python.
*   **I/O & Buffer Deadlock Pitfalls:** Avoid sequential single-threaded `os.write()` followed by `os.read()` on `os.pipe()` for payloads exceeding the operating system pipe buffer (>64 KB). Use `tempfile.TemporaryFile`, `io.BytesIO`, or thread/event loops for large in-process data transfers.


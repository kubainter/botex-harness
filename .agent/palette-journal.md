## 2026-03-30 - Force Color Support for CLI Micro-UX

**Learning:** CLI styling helpers in `botex/ui.py` previously checked `sys.stdout.isatty() and not os.environ.get("NO_COLOR")`. For accessibility and non-interactive environments (e.g. piped viewers like `less -R`, log aggregators, or CI runners with explicit color support), respecting `FORCE_COLOR` and `CLICOLOR_FORCE` standard CLI environment variables enables forced ANSI color rendering.
**Action:** When working on CLI presentation/color logic in `botex/ui.py`, check `FORCE_COLOR` and `CLICOLOR_FORCE` alongside `NO_COLOR` and `sys.stdout.isatty()`, resetting `ui._enabled = None` when mocking environment variables in tests.

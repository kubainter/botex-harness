## 2026-03-29 - CLI Status Token Formatting

**Learning:** CLI terminal output uses `botex.ui.mark_status` to apply ANSI color codes to plain status strings. Missing status tokens (such as `[FAILED]`, `[WARNING]`, `[WARN]`, `[INTERRUPTED]`, `[PRZERWANO]`, `[ACTIVE]`) reduce status contrast and visual feedback in terminal output.
**Action:** Always maintain comprehensive token coverage in `mark_status` when introducing new CLI or REPL status strings.

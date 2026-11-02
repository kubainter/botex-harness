## 2026-10-31 - Missing aria-label/title attributes on key mode switcher and modal buttons

**Learning:** Important interactive buttons like Mode Switchers (`ChatWindow.vue`) and generic Modal closes (`Modal.vue`) lacked proper visual tooltips (title) which help screen-reader users via fallback and sighted users via tooltips.
**Action:** Always ensure that icon-only or ambiguous text buttons are properly labelled with `title` and/or `aria-label` throughout Vue components in this repository.

## 2026-03-30 - Accessible ARIA Labels in Chat Drawer Components

**Learning:** Vue components in `web/src/components/chat/` using Lucide icons (e.g. `ChatHistoryDrawer.vue`) often relied solely on `title` attributes for icon buttons. Explicit `aria-label` attributes (and `:aria-label` bound to i18n placeholders) are required to ensure assistive technologies reliably announce icon button functions and search input fields.
**Action:** Always complement `title` attributes with matching `aria-label` attributes on icon-only buttons and search inputs across Vue frontend components.

## 2026-03-30 - Force Color Support for CLI Micro-UX

**Learning:** CLI styling helpers in `botex/ui.py` previously checked `sys.stdout.isatty() and not os.environ.get("NO_COLOR")`. For accessibility and non-interactive environments (e.g. piped viewers like `less -R`, log aggregators, or CI runners with explicit color support), respecting `FORCE_COLOR` and `CLICOLOR_FORCE` standard CLI environment variables enables forced ANSI color rendering.
**Action:** When working on CLI presentation/color logic in `botex/ui.py`, check `FORCE_COLOR` and `CLICOLOR_FORCE` alongside `NO_COLOR` and `sys.stdout.isatty()`, resetting `ui._enabled = None` when mocking environment variables in tests.

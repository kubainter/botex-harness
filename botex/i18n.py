# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX i18n — lightweight two-language CLI localization (en/pl).

Scope: human-facing CLI strings only (REPL, help, config, status lines).
Strings returned by MCP tools/engine stay in English — they are an API
surface for agents, not a UI for operators.

Resolution order: explicit set_language() > config ui.language /
BOTEX_LANG env > auto-detect from OS locale > "en" fallback.
"""
import os
import locale

_LANG = "en"
SUPPORTED = ("en", "pl")

M = {
    "en": {
        # usage
        "usage.tagline": "agent-agnostic code execution harness.",
        "usage.commands": "Commands:",
        "usage.repl": "Interactive mode — type tasks, exit quits",
        "usage.run": "One-shot headless task",
        "usage.stats": "Task/token/cost report",
        "usage.history": "Details of a specific task",
        "usage.snapshots": "Disk usage by snapshots",
        "usage.clean": "Purge old snapshots",
        "usage.health": "Environment check",
        "usage.serve": "Force MCP server mode (stdio)",
        "usage.config": "Interactive defaults picker",
        "usage.config.provider": "Set the default provider",
        "usage.config.model": "Set a provider's default model",
        "usage.config.profile": "Set the default profile (e.g. coding)",
        "usage.config.mode": "Set the default capability preset",
        "usage.models": "Model profiles and live pricing",
        "usage.repl_cmds": "Local REPL commands (no API calls):",
        "usage.no_args": "No arguments: on a terminal shows REPL help and drops into the",
        "usage.no_args2": "REPL; launched by an MCP client (pipe) it starts the server.",
        # repl help
        "repl.title": "BoteX REPL — type a task or a local command:",
        "repl.local": "Local commands (no API calls):",
        "repl.health": "Environment check",
        "repl.stats": "Task/token/cost report",
        "repl.history": "Details of a specific task",
        "repl.snapshots": "Disk usage by snapshots",
        "repl.clean": "Purge old snapshots",
        "repl.config": "Interactive defaults picker",
        "repl.config.set": "Persist a default (botex.config.local.json)",
        "repl.mode": "Change capabilities for this session (readonly/edit/destructive/full)",
        "repl.help": "This help",
        "repl.exit": "Quit",
        "repl.shell_note": "Shell commands (serve, run --flags, ...) run outside the REPL, from a terminal.",
        "repl.banner": "BoteX REPL — coding tasks on workspace files",
        "repl.banner2": "Type 'exit' to quit, 'help' for local commands.",
        # repl runtime
        "repl.mode_set": "Mode:",
        "repl.mode_default": "default (from config)",
        "repl.interrupted": "[INTERRUPTED] Task cancelled — back to prompt ('exit' quits the REPL).",
        "repl.success": "[SUCCESS]",
        "repl.failure": "[FAILURE]",
        "repl.cost": "cost:",
        "repl.files": "Files:",
        # local commands
        "lc.serve_warn": "'serve' starts the MCP server — it cannot run inside the REPL. Exit ('exit') and run 'botex serve' from a terminal.",
        "lc.in_repl": "[BoteX] Already in the REPL — type tasks directly.",
        "lc.cli_like": "looks like a shell command, not a task. In the REPL type tasks directly; commands: 'help'.",
        "lc.snaps_title": "=== BOTEX SNAPSHOTS",
        "lc.tasks": "tasks",
        "lc.files": "files",
        "lc.days": "days",
        "lc.cleaned": "folders removed, freed",
        "lc.remaining": "Remaining:",
        # confirm
        "confirm.req": "Agent requests",
        "confirm.on": "on",
        "confirm.ask": "Approve? [y/N]",
        # config
        "cfg.current": "current",
        "cfg.pick_prompt": "  number/name, Enter = no change: ",
        "cfg.skipped": "  [skipped] unknown option",
        "cfg.saved": "[OK] Saved to botex.config.local.json.",
        "cfg.title": "=== BOTEX CONFIG (effective) ===",
        "cfg.persist_note": "(persistent writes: botex.config.local.json)",
        "cfg.unknown_provider": "[ERROR] Unknown provider '{name}'. Available: {avail}",
        "cfg.default_provider": "[OK] Default provider: {name}",
        "cfg.default_profile": "[OK] Default profile: {name}",
        "cfg.unknown_mode": "[ERROR] Unknown mode '{name}'. Available: {avail}",
        "cfg.default_mode": "[OK] Default mode: {name}",
        "cfg.default_lang": "[OK] CLI language: {name}",
        "cfg.unknown_lang": "[ERROR] Unknown language '{name}'. Available: {avail}",
        "cfg.usage": "Usage: botex config [show] | provider <name> | model [provider] <slug> | profile <name> | mode <preset> | language <en|pl> | price <input|output> <usd>",
        "cfg.price_set": "[OK] Price cap {which}: ${val}/1M tokens",
        "cfg.price_unknown": "[ERROR] Use 'price input <usd>' or 'price output <usd>' (0 disables that cap).",
        "models.title": "=== BOTEX MODELS & PRICING (USD per 1M tokens) ===",
        "models.dynamic": "dynamic/unknown price",
        "models.caps": "Active caps: input ≤ ${pin}/1M, output ≤ ${pout}/1M (pricing.*)",
        "cfg.provider_label": "Provider",
        "cfg.profile_label": "Model profile",
        "cfg.mode_label": "Capability preset (mode)",
        "cfg.lang_label": "CLI language",
        # misc
        "misc.unknown_cmd": "[BoteX] Unknown command: '{name}'",
        "misc.server_start": "[BoteX] MCP server 'BoteX-Harness' listening on stdio. Ctrl+C to exit.\n",
        "run.status": "Status:",
        "run.task": "Task:",
        "run.steps": "Steps:",
        # analytics
        "an.no_data": "No analytics data recorded (.agent_analytics.json).",
        "an.title": "BoteX Analytics — last {days} days ({period})",
        "an.metric": "Metric",
        "an.value": "Value",
        "an.tasks": "Tasks",
        "an.total_cost": "Total cost (USD)",
        "an.prompt_tok": "Prompt tokens",
        "an.comp_tok": "Completion tokens",
        "an.cached_tok": "Cached tokens",
        "an.line_bal": "Line balance",
        "an.model_eff": "Model efficiency and cost",
        "an.runs": "Runs",
        "an.done_rate": "DONE Rate",
        "an.top_failure": "Top Failure",
        "an.tokens": "Tokens",
        "an.cost": "Cost USD",
        "an.models": "Models:",
        "an.runs_fmt": "{runs} tasks, {tokens} tokens, ${cost}",
        "an.no_history": "No task history.",
        "an.not_found": "No task found with ID '{id}'.",
        "an.recent": "=== RECENT BOTEX TASKS ===",
    },
    "pl": {
        "usage.tagline": "agent-agnostic code execution harness.",
        "usage.commands": "Komendy:",
        "usage.repl": "Tryb interaktywny — wpisuj zadania, exit kończy",
        "usage.run": "Jednorazowe zadanie headless",
        "usage.stats": "Raport zadań/tokenów/kosztów",
        "usage.history": "Szczegóły konkretnego zadania",
        "usage.snapshots": "Zajętość dysku przez snapshoty",
        "usage.clean": "Czyszczenie starych snapshotów",
        "usage.health": "Sprawdzenie środowiska",
        "usage.serve": "Wymuszenie trybu serwera MCP (stdio)",
        "usage.config": "Interaktywny selektor defaultów",
        "usage.config.provider": "Ustaw domyślnego providera",
        "usage.config.model": "Ustaw domyślny model providera",
        "usage.config.profile": "Ustaw domyślny profil (np. coding)",
        "usage.config.mode": "Ustaw domyślny preset uprawnień",
        "usage.models": "Profile modeli i cennik na żywo",
        "usage.repl_cmds": "Lokalne komendy REPL (bez wywołań API):",
        "usage.no_args": "Bez argumentów: na terminalu pokazuje pomoc REPL i wchodzi w REPL;",
        "usage.no_args2": "uruchomiony przez klienta MCP (pipe) automatycznie startuje serwer.",
        "repl.title": "BoteX REPL — wpisz zadanie wprost albo lokalną komendę:",
        "repl.local": "Lokalne komendy (bez wywołań API):",
        "repl.health": "Sprawdzenie środowiska",
        "repl.stats": "Raport zadań/tokenów/kosztów",
        "repl.history": "Szczegóły konkretnego zadania",
        "repl.snapshots": "Zajętość dysku przez snapshoty",
        "repl.clean": "Czyszczenie starych snapshotów",
        "repl.config": "Interaktywny selektor defaultów",
        "repl.config.set": "Trwałe ustawienie defaultu (botex.config.local.json)",
        "repl.mode": "Zmiana uprawnień na czas sesji (readonly/edit/destructive/full)",
        "repl.help": "Ta pomoc",
        "repl.exit": "Wyjście",
        "repl.shell_note": "Komendy powłoki (serve, run --flagi, ...) odpalaj poza REPL-em, z terminala.",
        "repl.banner": "BoteX REPL — zadania kodowe na plikach workspace",
        "repl.banner2": "'exit' kończy, 'help' pokazuje lokalne komendy.",
        "repl.mode_set": "Mode:",
        "repl.mode_default": "default (z configu)",
        "repl.interrupted": "[PRZERWANO] Zadanie anulowane — powrót do promptu ('exit' kończy REPL).",
        "repl.success": "[SUKCES]",
        "repl.failure": "[BŁĄD]",
        "repl.cost": "koszt:",
        "repl.files": "Pliki:",
        "lc.serve_warn": "'serve' startuje serwer MCP — nie można go uruchomić wewnątrz REPL. Wyjdź ('exit') i odpal 'botex serve'.",
        "lc.in_repl": "[BoteX] Już jesteś w REPL — wpisuj zadania bezpośrednio.",
        "lc.cli_like": "wygląda na komendę CLI, nie zadanie. W REPL wpisuj zadania wprost; komendy: 'help'.",
        "lc.snaps_title": "=== BOTEX SNAPSHOTS",
        "lc.tasks": "zadań",
        "lc.files": "plików",
        "lc.days": "dni",
        "lc.cleaned": "usunięto folderów, zwolniono",
        "lc.remaining": "Pozostało:",
        "confirm.req": "Agent żąda",
        "confirm.on": "na",
        "confirm.ask": "Zatwierdzić? [y/N]",
        "cfg.current": "obecnie",
        "cfg.pick_prompt": "  numer/nazwa, Enter = bez zmian: ",
        "cfg.skipped": "  [pominięto] nieznana opcja",
        "cfg.saved": "[OK] Zapisano w botex.config.local.json.",
        "cfg.title": "=== BOTEX CONFIG (efektywne) ===",
        "cfg.persist_note": "(zapisy trwale: botex.config.local.json)",
        "cfg.unknown_provider": "[BŁĄD] Nieznany provider '{name}'. Dostępne: {avail}",
        "cfg.default_provider": "[OK] Domyślny provider: {name}",
        "cfg.default_profile": "[OK] Domyślny profil: {name}",
        "cfg.unknown_mode": "[BŁĄD] Nieznany mode '{name}'. Dostępne: {avail}",
        "cfg.default_mode": "[OK] Domyślny mode: {name}",
        "cfg.default_lang": "[OK] Język CLI: {name}",
        "cfg.unknown_lang": "[BŁĄD] Nieznany język '{name}'. Dostępne: {avail}",
        "cfg.usage": "Użycie: botex config [show] | provider <nazwa> | model [provider] <slug> | profile <nazwa> | mode <preset> | language <en|pl> | price <input|output> <usd>",
        "cfg.price_set": "[OK] Limit ceny {which}: ${val}/1M tokenów",
        "cfg.price_unknown": "[BŁĄD] Użyj 'price input <usd>' albo 'price output <usd>' (0 wyłącza limit).",
        "models.title": "=== BOTEX MODELE I CENY (USD per 1M tokenów) ===",
        "models.dynamic": "cena dynamiczna/nieznana",
        "models.caps": "Aktywne limity: input ≤ ${pin}/1M, output ≤ ${pout}/1M (pricing.*)",
        "cfg.provider_label": "Provider",
        "cfg.profile_label": "Profil modelu",
        "cfg.mode_label": "Preset uprawnień (mode)",
        "cfg.lang_label": "Język CLI",
        "misc.unknown_cmd": "[BoteX] Nieznana komenda: '{name}'",
        "misc.server_start": "[BoteX] Serwer MCP 'BoteX-Harness' nasłuchuje na stdio. Ctrl+C kończy.\n",
        "run.status": "Status:",
        "run.task": "Task:",
        "run.steps": "Kroki:",
        "an.no_data": "Brak zapisanych danych analitycznych (.agent_analytics.json).",
        "an.title": "BoteX Analytics — ostatnie {days} dni ({period})",
        "an.metric": "Metryka",
        "an.value": "Wartość",
        "an.tasks": "Liczba zadań",
        "an.total_cost": "Łączny koszt (USD)",
        "an.prompt_tok": "Tokeny prompt",
        "an.comp_tok": "Tokeny completion",
        "an.cached_tok": "Tokeny z cache",
        "an.line_bal": "Bilans linii kodu",
        "an.model_eff": "Efektywność i koszt modelu",
        "an.runs": "Zadania",
        "an.done_rate": "Wskaźnik DONE",
        "an.top_failure": "Główny błąd",
        "an.tokens": "Tokeny",
        "an.cost": "Koszt USD",
        "an.models": "Modele:",
        "an.runs_fmt": "{runs} zadań, {tokens} tokenów, ${cost}",
        "an.no_history": "Brak historii zadań.",
        "an.not_found": "Nie znaleziono zadania o ID '{id}'.",
        "an.recent": "=== OSTATNIE ZADANIA BOTEX ===",
    },
}


def detect_language() -> str:
    """Best-effort OS locale detection; 'pl*' -> 'pl', everything else -> 'en'."""
    try:
        loc = (locale.getlocale()[0] or "") or os.environ.get("LANG", "")
    except Exception:
        loc = os.environ.get("LANG", "")
    return "pl" if loc.lower().startswith("pl") else "en"


def init_language(setting: str = "auto") -> str:
    """Resolve 'auto'/'en'/'pl' (or BOTEX_LANG env) and activate it."""
    global _LANG
    lang = os.environ.get("BOTEX_LANG", "").strip().lower() or setting
    if lang in ("auto", ""):
        lang = detect_language()
    _LANG = lang if lang in SUPPORTED else "en"
    return _LANG


def set_language(lang: str) -> bool:
    """Switch the active language at runtime. Returns False for unsupported."""
    global _LANG
    if lang in SUPPORTED:
        _LANG = lang
        return True
    return False


def get_language() -> str:
    return _LANG


def t(key: str, **kwargs) -> str:
    """Translate a message key; falls back to English, then to the key itself."""
    text = M.get(_LANG, M["en"]).get(key, M["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text

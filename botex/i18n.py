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

import locale
import os

_LANG = "en"
SUPPORTED = ("en", "pl")

M = {
    "en": {
        # usage
        "usage.tagline": "agent-agnostic code execution harness.",
        "usage.commands": "Commands:",
        "usage.repl": "Interactive shell: type a task, 'exit' quits",
        "usage.run": "Run one headless task",
        "usage.stats": "Task, token, and cost report",
        "usage.history": "Inspect a specific task run",
        "usage.snapshots": "Snapshot disk usage",
        "usage.clean": "Delete old snapshots",
        "usage.health": "Check environment and API keys",
        "usage.serve": "Start stdio MCP server",
        "usage.config": "Interactive defaults picker",
        "usage.config.provider": "Set default provider",
        "usage.config.model": "Set default model for a provider",
        "usage.config.profile": "Set default profile (e.g. coding)",
        "usage.config.mode": "Set default permission mode",
        "usage.config.zdr": "Toggle ZDR policy for a provider",
        "usage.models": "List model profiles and live pricing",
        "usage.allow_non_zdr": "Allow non-ZDR and :free models for this run",
        "usage.repl_cmds": "Local REPL commands (no API calls):",
        "usage.no_args": "No arguments: opens the interactive REPL in a terminal,",
        "usage.no_args2": "or starts the stdio MCP server when piped.",
        # repl help
        "repl.title": "BoteX REPL: type a task or a local command:",
        "repl.local": "Local commands (no API calls):",
        "repl.health": "Check environment and API keys",
        "repl.stats": "Task, token, and cost report",
        "repl.history": "Inspect a specific task run",
        "repl.snapshots": "Snapshot disk usage",
        "repl.clean": "Delete old snapshots",
        "repl.config": "Interactive defaults picker",
        "repl.config.set": "Save a setting to botex.config.local.json",
        "repl.mode": "Change session permissions (readonly/edit/destructive/full)",
        "repl.help": "Show this help",
        "repl.exit": "Quit",
        "repl.allow_non_zdr": "Startup flag: allows non-ZDR models for the session",
        "repl.shell_note": "Run CLI subcommands (serve, run --flags) directly from your shell, outside the REPL.",
        "repl.banner": "BoteX REPL: workspace code runner",
        "repl.banner2": "Type 'exit' to quit, 'help' for local commands.",
        # repl runtime
        "repl.mode_set": "Mode:",
        "repl.mode_default": "default (from config)",
        "repl.workspace": "Show or change the active workspace directory",
        "ws.label": "Workspace:",
        "ws.unset": "(unset - uses current directory)",
        "ws.set": "[OK] Workspace: {path}",
        "ws.invalid": "[ERROR] Directory does not exist: {path}",
        "ws.missing": "[WARN] directory does not exist",
        "ws.harness_warn": "[WARN] Workspace points to the BoteX repo itself. Tasks will modify harness source files. Pass -w <DIR>, cd <DIR>, or run `botex config workspace <DIR>`.",
        "cfg.workspace_set": "[OK] Default workspace: {path}",
        "cfg.workspace_cleared": "[OK] Default workspace cleared (uses current directory)",
        "repl.interrupted": "[INTERRUPTED] Task cancelled. Back at prompt ('exit' quits).",
        "repl.success": "[SUCCESS]",
        "repl.failure": "[FAILURE]",
        "repl.cost": "cost:",
        "repl.files": "Files:",
        # local commands
        "lc.serve_warn": "'serve' starts the stdio MCP server and cannot run inside the REPL. Type 'exit' and run 'botex serve' from your terminal.",
        "lc.in_repl": "[BoteX] Already inside the REPL. Type a task directly.",
        "lc.cli_like": "looks like a CLI flag, not a task. Type the task description directly, or 'help' for commands.",
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
        "cfg.pick_prompt": "  number/name, Enter = keep current: ",
        "cfg.skipped": "  [skipped] unknown option",
        "cfg.saved": "[OK] Saved to botex.config.local.json.",
        "cfg.title": "=== BOTEX CONFIG (effective) ===",
        "cfg.persist_note": "(writes saved to botex.config.local.json)",
        "cfg.unknown_provider": "[ERROR] Unknown provider '{name}'. Available: {avail}",
        "cfg.default_provider": "[OK] Default provider: {name}",
        "cfg.default_profile": "[OK] Default profile: {name}",
        "cfg.unknown_mode": "[ERROR] Unknown mode '{name}'. Available: {avail}",
        "cfg.default_mode": "[OK] Default mode: {name}",
        "cfg.default_lang": "[OK] CLI language: {name}",
        "cfg.unknown_lang": "[ERROR] Unknown language '{name}'. Available: {avail}",
        "cfg.zdr_set": "[OK] {name}: zdr.enabled = {state}",
        "cfg.zdr_unknown": "[ERROR] Use 'zdr on|off [provider]'. 'off' allows non-ZDR and :free models.",
        "cfg.usage": "Usage: botex config [show] | provider <name> | model [provider] <slug> | profile <name> | mode <preset> | workspace <path> | language <en|pl> | price <input|output> <usd> | zdr <on|off> [provider]",
        "cfg.price_set": "[OK] Price cap {which}: ${val}/1M tokens",
        "cfg.price_unknown": "[ERROR] Use 'price input <usd>' or 'price output <usd>' (0 disables the cap).",
        "models.title": "=== BOTEX MODELS & PRICING (USD per 1M tokens) ===",
        "models.dynamic": "dynamic/unknown price",
        "models.caps": "Active caps: input <= ${pin}/1M, output <= ${pout}/1M (pricing.*)",
        "cfg.provider_label": "Provider",
        "cfg.profile_label": "Model profile",
        "cfg.mode_label": "Permission preset (mode)",
        "cfg.lang_label": "CLI language",
        # misc
        "misc.unknown_cmd": "[BoteX] Unknown command: '{name}'",
        "misc.server_start": "[BoteX] MCP server 'BoteX-Harness' listening on stdio. Press Ctrl+C to stop.\n",
        "run.status": "Status:",
        "run.task": "Task:",
        "run.steps": "Steps:",
        # analytics
        "an.no_data": "No analytics data recorded (.agent_analytics.json).",
        "an.title": "BoteX Analytics: last {days} days ({period})",
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
        "usage.tagline": "narzędzie uruchomieniowe dla agentów kodujących.",
        "usage.commands": "Polecenia:",
        "usage.repl": "Tryb interaktywny: wpisz zadanie, 'exit' kończy pracę",
        "usage.run": "Jednorazowe zadanie w tle (headless)",
        "usage.stats": " Zestawienie zadań, tokenów i kosztów",
        "usage.history": "Szczegóły wybranego zadania",
        "usage.snapshots": "Miejsce zajmowane przez kopie zapasowe (snapshoty)",
        "usage.clean": "Usuwanie starych kopii zapasowych",
        "usage.health": "Sprawdzenie środowiska i kluczy API",
        "usage.serve": "Uruchomienie serwera MCP (stdio)",
        "usage.config": "Interaktywna zmiana ustawień domyślnych",
        "usage.config.provider": "Ustaw domyślnego dostawcę",
        "usage.config.model": "Ustaw domyślny model dostawcy",
        "usage.config.profile": "Ustaw domyślny profil (np. coding)",
        "usage.config.mode": "Ustaw domyślny tryb uprawnień",
        "usage.config.zdr": "Włącz lub wyłącz wymóg ZDR dla dostawcy",
        "usage.models": "Profile modeli i aktualny cennik",
        "usage.allow_non_zdr": "Zezwól na modele bez ZDR (:free) w tym uruchomieniu",
        "usage.repl_cmds": "Lokalne polecenia REPL (bez zapytań do API):",
        "usage.no_args": "Bez argumentów: w terminalu otwiera tryb interaktywny REPL,",
        "usage.no_args2": "a wywołany przez klienta MCP (pipe) uruchamia serwer.",
        "repl.title": "BoteX REPL: wpisz zadanie albo lokalne polecenie:",
        "repl.local": "Lokalne polecenia (bez zapytań do API):",
        "repl.health": "Sprawdzenie środowiska i kluczy API",
        "repl.stats": "Zestawienie zadań, tokenów i kosztów",
        "repl.history": "Szczegóły wybranego zadania",
        "repl.snapshots": "Miejsce zajmowane przez kopie zapasowe",
        "repl.clean": "Usuwanie starych kopii zapasowych",
        "repl.config": "Interaktywna zmiana ustawień domyślnych",
        "repl.config.set": "Zapisz ustawienie w botex.config.local.json",
        "repl.mode": "Zmień uprawnienia w bieżącej sesji (readonly/edit/destructive/full)",
        "repl.help": "Pokaż tę pomoc",
        "repl.exit": "Zakończ",
        "repl.allow_non_zdr": "Flaga startowa: dopuszcza modele bez ZDR w całej sesji",
        "repl.shell_note": "Polecenia powłoki (serve, run --flagi) uruchamiaj bezpośrednio w terminalu, poza REPL.",
        "repl.banner": "BoteX REPL: praca na plikach projektu",
        "repl.banner2": "Wpisz 'exit', aby wyjść, lub 'help', aby wyświetlić listę poleceń.",
        "repl.mode_set": "Tryb:",
        "repl.mode_default": "domyślny (z konfiguracji)",
        "repl.workspace": "Pokaż lub zmień katalog roboczy projektu",
        "ws.label": "Katalog roboczy:",
        "ws.unset": "(nie ustawiono - bieżący katalog)",
        "ws.set": "[OK] Katalog roboczy: {path}",
        "ws.invalid": "[BŁĄD] Katalog nie istnieje: {path}",
        "ws.missing": "[UWAGA] katalog nie istnieje",
        "ws.harness_warn": "[UWAGA] Katalogiem roboczym jest repozytorium BoteX. Zadania zmodyfikują kod samego narzędzia. Użyj -w <KATALOG>, `cd <KATALOG>` lub `botex config workspace <KATALOG>`.",
        "cfg.workspace_set": "[OK] Domyślny katalog roboczy: {path}",
        "cfg.workspace_cleared": "[OK] Wyczyszczono domyślny katalog roboczy (używany bieżący katalog)",
        "repl.interrupted": "[PRZERWANO] Anulowano zadanie. Powrót do wiersza poleceń ('exit' kończy pracę).",
        "repl.success": "[SUKCES]",
        "repl.failure": "[BŁĄD]",
        "repl.cost": "koszt:",
        "repl.files": "Pliki:",
        "lc.serve_warn": "Polecenie 'serve' uruchamia serwer MCP i nie działa wewnątrz REPL. Wpisz 'exit' i uruchom 'botex serve' w terminalu.",
        "lc.in_repl": "[BoteX] Jesteś już w trybie REPL. Wpisz treść zadania.",
        "lc.cli_like": "wygląda na polecenie terminala, a nie zadanie. Wpisz od razu polecenie dla modelu lub 'help'.",
        "lc.snaps_title": "=== KOPIE ZAPASOWE BOTEX (SNAPSHOTS)",
        "lc.tasks": "zadań",
        "lc.files": "plików",
        "lc.days": "dni",
        "lc.cleaned": "usuniętych katalogów, zwolniono",
        "lc.remaining": "Pozostało:",
        "confirm.req": "Akceptacja operatora:",
        "confirm.on": "w",
        "confirm.ask": "Zatwierdzić operację? [y/N]",
        "cfg.current": "obecnie",
        "cfg.pick_prompt": "  numer/nazwa, Enter = bez zmian: ",
        "cfg.skipped": "  [pominięto] nieznana opcja",
        "cfg.saved": "[OK] Zapisano w botex.config.local.json.",
        "cfg.title": "=== KONFIGURACJA BOTEX (aktywna) ===",
        "cfg.persist_note": "(trwały zapis: botex.config.local.json)",
        "cfg.unknown_provider": "[BŁĄD] Nieznany dostawca '{name}'. Dostępni: {avail}",
        "cfg.default_provider": "[OK] Domyślny dostawca: {name}",
        "cfg.default_profile": "[OK] Domyślny profil: {name}",
        "cfg.unknown_mode": "[BŁĄD] Nieznany tryb '{name}'. Dostępne: {avail}",
        "cfg.default_mode": "[OK] Domyślny tryb: {name}",
        "cfg.default_lang": "[OK] Język CLI: {name}",
        "cfg.unknown_lang": "[BŁĄD] Nieznany język '{name}'. Dostępne: {avail}",
        "cfg.zdr_set": "[OK] {name}: zdr.enabled = {state}",
        "cfg.zdr_unknown": "[BŁĄD] Użyj 'zdr on|off [dostawca]'. Wartość 'off' dopuszcza modele bez ZDR oraz :free.",
        "cfg.usage": "Użycie: botex config [show] | provider <nazwa> | model [dostawca] <slug> | profile <nazwa> | mode <tryb> | workspace <ścieżka> | language <en|pl> | price <input|output> <usd> | zdr <on|off> [dostawca]",
        "cfg.price_set": "[OK] Limit kosztu {which}: ${val}/1M tokenów",
        "cfg.price_unknown": "[BŁĄD] Użyj 'price input <usd>' albo 'price output <usd>' (0 wyłącza limit).",
        "models.title": "=== MODELE I CENNIK BOTEX (USD za 1M tokenów) ===",
        "models.dynamic": "stawka dynamiczna/nieznana",
        "models.caps": "Aktywne limity: wejście <= ${pin}/1M, wyjście <= ${pout}/1M (pricing.*)",
        "cfg.provider_label": "Dostawca",
        "cfg.profile_label": "Profil modelu",
        "cfg.mode_label": "Tryb uprawnień (mode)",
        "cfg.lang_label": "Język CLI",
        "misc.unknown_cmd": "[BoteX] Nieznane polecenie: '{name}'",
        "misc.server_start": "[BoteX] Serwer MCP 'BoteX-Harness' nasłuchuje na stdio. Ctrl+C kończy pracę.\n",
        "run.status": "Status:",
        "run.task": "Zadanie:",
        "run.steps": "Kroki:",
        "an.no_data": "Brak danych o zużyciu (.agent_analytics.json).",
        "an.title": "Statystyki BoteX: ostatnie {days} dni ({period})",
        "an.metric": "Metryka",
        "an.value": "Wartość",
        "an.tasks": "Liczba zadań",
        "an.total_cost": "Łączny koszt (USD)",
        "an.prompt_tok": "Tokeny wejściowe (prompt)",
        "an.comp_tok": "Tokeny wyjściowe (completion)",
        "an.cached_tok": "Tokeny z pamięci podręcznej",
        "an.line_bal": "Bilans linii kodu",
        "an.model_eff": "Skuteczność i koszt modeli",
        "an.runs": "Uruchomienia",
        "an.done_rate": "Skuteczność (DONE)",
        "an.top_failure": "Najczęstszy błąd",
        "an.tokens": "Tokeny",
        "an.cost": "Koszt USD",
        "an.models": "Modele:",
        "an.runs_fmt": "{runs} zadań, {tokens} tokenów, ${cost}",
        "an.no_history": "Brak zapisanych zadań w historii.",
        "an.not_found": "Nie znaleziono zadania o identyfikatorze '{id}'.",
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

# BoteX - Instrukcja Operacyjna dla Agentów AI

Niniejszy dokument stanowi przewodnik i zbiór reguł dla agentów AI pracujących nad repozytorium **BoteX**.

## 1. Przegląd architektury

BoteX to uprząż egzekucyjna (execution harness) dla agentów edytujących kod, działająca jako serwer MCP. Główne katalogi i moduły to:

*   **`botex/`** - Główny pakiet aplikacji.
    *   `server.py` - Główny punkt wejścia serwera MCP oraz dyspozytor CLI. W katalogu głównym.
    *   `engine.py` - Autonomiczna pętla wykonawcza, zarządzanie kontekstem i wyzwalanie wycofań (rollbacks).
    *   `patch_engine.py` - Mechanizm fuzzy patchowania, weryfikacja składni w pamięci oraz zarządzanie snapshotami.
    *   `file_tools.py` - Bezpieczne czytanie plików (outline-first), tworzenie, przenoszenie i usuwanie.
    *   `security.py` - Zabezpieczenia przed atakami Path Traversal, maskowanie sekretów, filtrowanie `.gitignore`.
    *   `exec_tools.py` - Wykonywanie poleceń z kontrolą polityk i filtrowaniem argumentów.
    *   `capabilities.py` - Ustawienia uprawnień (tryby: `readonly`, `edit`, `destructive`, `full`).
    *   `pricing.py` & `analytics.py` - Ochrona budżetu, księga kosztów, sprawdzanie cen modeli.
    *   `providers.py` - Adaptery dostawców (OpenRouter, itp.) i normalizator żądań.
*   **`tests/`** - Zestaw testów (unit i integracyjnych end-to-end).
*   **`docs/`** - Dokumentacja, w tym szczegółowe omówienie architektury (`TUTORIAL.md`).
*   **`recipes/`** - (w pakiecie `botex/`) Wyspecjalizowane persony agentów (planner, reviewer itp.).

## 2. Narzędzia i komendy

*   **Testy (Pytest):** Aby uruchomić pełen zestaw testów, użyj poniższej komendy z ustawioną zmienną środowiskową:
    ```bash
    PYTHONPATH=. python -m pytest tests/test_botex.py
    ```
*   **Linting i formatowanie (Ruff):** Projekt korzysta z narzędzia Ruff. Aby sprawdzić kod:
    ```bash
    ruff check .
    ```
*   **Instalacja:** Aplikacja definiowana jest w `pyproject.toml`. Do instalacji w trybie deweloperskim można użyć np.:
    ```bash
    pip install -e .
    ```

## 3. Zasady i ograniczenia

### 3.1. Katalogi i pliki zablokowane do edycji
Pod żadnym pozorem nie należy modyfikować następujących lokalizacji:
*   `build/`, `dist/`, `*.egg-info/` - artefakty procesu budowania.
*   `.snapshots/` - katalog kopii zapasowych tworzonych w trakcie działania (modyfikowany tylko przez sam system BoteX).
*   `.env`, `botex.config.local.json` - pliki konfiguracyjne zawierające wrażliwe dane i sekrety środowiska dewelopera.
*   `.agent_analytics.json`, `.model_pricing.json` - pliki przechowujące stan i historię.

### 3.2. Konwencje repozytorium
*   **Commity:** Staraj się, by opisy commitów były zwięzłe, rzeczowe i po angielsku. Tytuł nie powinien przekraczać 50 znaków, a jeśli potrzebny jest dłuższy opis, dodaj go po pustej linii.
*   **Pull Requesty:** Unikaj eksponowania szczegółów potencjalnych luk w zabezpieczeniach (CRITICAL) w publicznych opisach PR, zgodnie z regułą Zero Data Retention / bezwyciekową. Pamiętaj, aby poprawki związane z bezpieczeństwem były krótsze niż 50 linii kodu.

## 4. Kryteria autonomii

1.  **Działaj zachowawczo i autonomicznie:** W przypadku braku pewności podczas rozwiązywania problemów inżynieryjnych wybieraj najbardziej logiczne, zachowawcze i zgodne z istniejącymi wzorcami rozwiązanie, **zamiast wstrzymywać pracę i pytać użytkownika**.
2.  **Tylko bezpieczne polecenia:** Jeśli musisz wykonać skrypty, ogranicz się do autoryzowanej białej listy (m.in. `pytest`, `python`, `ruff`, `git`).
3.  **Weryfikuj przed zatwierdzeniem:** Pamiętaj o uruchomieniu testów po dokonaniu zmian i sprawdzaniu składni wprowadzanych modyfikacji.

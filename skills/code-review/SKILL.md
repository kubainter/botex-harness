---
name: code-review
version: 2.0.0
description: Exhaustive code review and silent failure hunting for multi-agent workflows. Detects regressions, swallowed exceptions, unhandled edge cases, and contract drift across Python, TypeScript/JS, Rust, and Go.
botex_recipe: reviewer
tags: [code-review, silent-failures, reliability, quality-gate]
---

# Code Review & Silent Failure Hunting Skill

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)
Gdy agent zewnętrzny (Antigravity, Devin, Codex) zleca przegląd kodu do BoteXa:
- **MCP Tool:** `botex.start_task` lub `botex.run_subagent`
- **Recipe:** `reviewer`
- **Tryb:** `readonly` (BoteX analizuje kod bez modyfikacji)
- **Format zlecenia:**
```markdown
### GOAL
Przeprowadź rygorystyczny przegląd kodu pod kątem regresji, cichych błędów i spójności kontraktów.

### CONSTRAINTS & CONTEXT
- Zakres: [ścieżki plików lub katalogów do analizy]
- Focus: ciche błędy, połykanie wyjątków, wycieki zasobów, backward compatibility.

### REFS
- Files: [file:///...]
- Memory Vault Key: [opcjonalny klucz pamięci]
```

---

## 2. Canonical Engineering Rules (Wspólny Rdzeń Kryteriów)
Zarówno BoteX wewnątrz swojej receptury `reviewer.md`, jak i agenci zewnętrzny audytujący kod, muszą bezwzględnie weryfikować poniższe 4 filary:

### Filar I: Polowanie na Ciche Błędy (Silent Failure Hunter)
- **Połykanie wyjątków (Exception Swallowing):** Wykrywaj `except Exception: pass`, puste bloki `catch (e) {}` oraz catch-all bez logowania z pełnym stack-trace.
- **Ukryty Fallback Drift:** Zwracanie domyślnych pustych wartości (`[]`, `{}`, `None`, `false`) w blokach błędu, które maskują krytyczne awarie upstreamu.
- **Wiszące operacje asynchroniczne:** Brakujące `await`, nieobsłużone odrzucenia `Promise.catch()`, błędy w detached goroutines/threads.
- **Niezwalniane zasoby:** Uchwyty plików, sesje HTTP, połączenia DB lub blokady locków bez bloków `finally` / context managerów (`with`, `using`).

### Filar II: Poprawność Logiczna i Przypadki Brzegowe
- **Błędy granic (Off-by-one):** Błędy indeksowania, puste kolekcje, operacje na skrajnych wartościach (`0`, `-1`, `MAX_INT`).
- **Wielowątkowość i współbieżność:** Niezsynchronizowany dostęp do zmiennych mutowalnych, wyścigi (race conditions), brak atomowości w operacjach read-modify-write.
- **Kodowanie i formaty:** Błędy konwersji znaków (UTF-8 vs CP1250/Windows), obsługa znaków nowej linii (CRLF/LF), formaty stref czasowych UTC.

### Filar III: Integralność Kontraktów i Wsteczna Kompatybilność
- **Zmiany w API:** Zmiana typów zwracanych, usunięcie opcjonalnych argumentów, naruszenie sygnatur funkcji eksportowanych.
- **Efekty uboczne (Side Effects):** Modyfikacja globalnego stanu środowiska, nadpisywanie zmiennych systemowych bez przywrócenia ich stanu.

### Filar IV: Higiena Testów i Mock Hallucinations
- **Mock Theater:** Testy, które weryfikują jedynie wywołanie zmockowanej metody, a nie realną logikę biznesową.
- **Fact-Forcing Verification:** Każdy test sprawdzający poprawkę błędu musi być w stanie udowodnić istnienie błędu przed poprawką.

---

## 3. Direct Execution / Independent Audit Workflow
Gdy agent wykonuje review samodzielnie (lub weryfikuje raport BoteXa):
1. **Reconnaissance:** Użyj narzędzi outlines/FTS do identyfikacji zmodyfikowanych modułów.
2. **Wide Read:** Zawsze czytaj pełny kontekst funkcji i modułu w szerokim zakresie linii (unikaj fragmentarycznego czytania po 5 linii).
3. **Severity Classification:**
   - `[CRITICAL]`: Regresja blokująca, utrata danych, załamanie kontraktu publicznego.
   - `[MAJOR]`: Cichy błąd, wyciek zasobów, nieobsłużony wyjątek w ścieżce głównej.
   - `[MINOR]`: Niespójność typowania, brak logowania błędu, dług technologiczny.
   - `[NITPICK]`: Stylistyczne uwagi nie wpływające na działanie kodu.

---

## 4. Structured Handoff & Memory Contract (Portable Remote/VPS Format)
> **Zasada Ścieżek Przenośnych (Repo-Relative Paths):** Wszelkie wpisy w `artifact_refs` MUSZĄ być ścieżkami względnymi względem roota repozytorium z separatorami Unix (`/`), np. `botex/engine.py` lub `docs/reviews/review-01.md`. Bezwzględny zakaz stosowania ścieżek bezwzględnych (`C:\...`, `/home/...`), aby zachować 100% interoperacyjności między maszyną lokalną (Windows) a zdalnym VPS (Oracle Linux ARM).

Odpowiedź z audytu musi kończyć się ustrukturyzowanym blokiem:
```json
{
  "status": "DONE" | "FAILED" | "BLOCKED",
  "summary": "1-2 zdaniowe syntetyczne podsumowanie audytu",
  "findings_count": {
    "critical": 0,
    "major": 0,
    "minor": 0
  },
  "artifact_refs": [
    "sciezka/wzgledna/do/pliku.py"
  ],
  "memory_keys": [
    "audit/code-review-<feature-id>"
  ],
  "blocking_reason": null
}
```
Po zakończeniu audytu kluczowe wnioski architektoniczne należy zsyntetyzować do BoteX Memory Vault (`save_memory`).

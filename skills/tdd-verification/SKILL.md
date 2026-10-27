---
name: tdd-verification
version: 2.0.0
description: Intent-driven Test-Driven Development (TDD) and verification loop. Enforces strict Red-Green-Refactor cycles, genuine red-phase failure proof, anti-mock-hallucination guards, and regression testing across Python, TS/JS, and systems languages.
botex_recipe: tdd
tags: [tdd, testing, verification, red-green-refactor, quality-assurance]
---

# Intent-Driven TDD & Verification Skill

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)
Gdy agent zewnętrzny (Antigravity, Devin, Codex) zleca implementację w oparciu o TDD do BoteXa:
- **MCP Tool:** `botex.start_task` lub `botex.run_subagent`
- **Recipe:** `tdd`
- **Tryb:** `edit` (BoteX tworzy testy, weryfikuje ich błąd, implementuje kod i doprowadza suite do stanu zielonego)
- **Format zlecenia:**
```markdown
### GOAL
Zaimplementuj [funkcjonalność/poprawkę buga] w rygorystycznym cyklu TDD (Red-Green-Refactor).

### CONSTRAINTS & CONTEXT
- Zakres: [pliki testowe oraz pliki źródłowe do modyfikacji]
- Wymóg TDD: Test musi najpierw autentycznie paść (faza czerwona), a następnie przejść minimalną zmianą w kodzie.
- Zero Test Theater: Zakaz pisania testów, które mockują testowaną logikę biznesową.

### REFS
- Files: [file:///...]
- Memory Vault Key: [opcjonalny klucz pamięci]
```

---

## 2. Canonical Engineering Rules (Rygorystyczny Cykl TDD)

Każda zmiana kodu musi przejść przez 3 żelazne fazy:

### Faza 1: RED (Dowód Awarii / Fact-Forcing Test)
1. **Zdefiniuj intencję i kontrakt:** Napisz test jednostkowy/integracyjny, który precyzyjnie modeluje oczekiwane zachowanie lub odtwarza zgłoszony błąd (*regression test*).
2. **Uruchom test przed zmianą w kodzie:** Test **musi** zakończyć się niepowodzeniem (*FAILED*).
3. **Weryfikacja przyczyny awarii:** Upewnij się, że test pada dokładnie z oczekiwanego powodu (np. `AssertionError: expected X but got Y`, `NotImplementedError`, brak metody), a nie z powodu błędu składniowego lub złego importu w samym teście.
4. *Zasada krytyczna:* Jeśli test przechodzi na zielono przed dotknięciem kodu produkcyjnego – test jest wadliwy lub funkcjonalność już istnieje.

### Faza 2: GREEN (Minimalna Implementacja)
1. **Napisz minimalny kod produkcyjny:** Wprowadź wyłącznie taką ilość kodu, która jest niezbędna do przejścia nowego testu.
2. **Zakaz przedwczesnej optymalizacji:** Nie buduj nadmiarowych abstrakcji na tym etapie.
3. **Uruchom pełny test suite:** Upewnij się, że nowy test jest zielony (`PASSED`) ORAZ że żadne istniejące testy w repozytorium nie uległy regresji.

### Faza 3: REFACTOR (Higiena i Odporność)
1. **Oczyszczenie kodu:** Uprość kod, wyeliminuj duplikacje, popraw nazewnictwo i typowanie.
2. **Weryfikacja regresji:** Po każdym kroku refaktoryzacji uruchom testy ponownie – status musi pozostać w 100% zielony.
3. **Test Robustness Check:** Zadbaj o testy przypadków brzegowych (wartości puste, null/None, błędy sieciowe, timeouty).

---

## 3. Anti-Patterns & Zero Test Theater Guard
Zarówno BoteX, jak i audytujący agent mają zakaz akceptowania poniższych praktyk:
- **Mocking the Subject:** Mockowanie modułu lub funkcji, którą test ma weryfikować (testuje makietę zamiast kodu).
- **Assertion-Free Tests:** Testy wykonujące operację bez asercji stanu końcowego (`assert result == expected`).
- **Sleep-Based Sync:** Używanie `time.sleep()` zamiast deterministycznych mechanizmów oczekiwania (`wait_for`, `event.wait()`, retry loops).
- **Happy-Path Only:** Pomijanie testów ścieżek błędów i wyjątków.

---

## 4. Execution Workflow (Polecenia Testowe według Ekosystemu)
- **Python:** `pytest path/to/test.py -k test_name -v`
- **TypeScript / Node:** `npm test -- path/to/test.ts` lub `npx vitest run path/to/test.ts`
- **Rust:** `cargo test test_name`
- **Go:** `go test -v -run TestName ./...`

---

## 5. Structured Handoff & Memory Contract (Portable Remote/VPS Format)
> **Zasada Ścieżek Przenośnych (Repo-Relative Paths):** Wszelkie wpisy w `artifact_refs` MUSZĄ być ścieżkami względnymi względem roota repozytorium z separatorami Unix (`/`), np. `tests/test_feature.py`, `src/feature.py`. Bezwzględny zakaz stosowania ścieżek bezwzględnych (`C:\...`, `/home/...`).

Zakończenie zadania TDD musi zwrócić ustrukturyzowany blok:
```json
{
  "status": "DONE" | "FAILED" | "BLOCKED",
  "summary": "Syntetyczne podsumowanie cyklu TDD: potwierdzona faza czerwona, minimalna implementacja, status suity testowej",
  "test_results": {
    "red_phase_verified": true,
    "passed": 12,
    "failed": 0,
    "suite_status": "GREEN"
  },
  "artifact_refs": [
    "tests/test_feature.py",
    "src/feature.py"
  ],
  "memory_keys": [
    "tdd/verification-<feature-name>"
  ],
  "blocking_reason": null
}
```
Zapisz wynik i wnioski w BoteX Memory Vault.

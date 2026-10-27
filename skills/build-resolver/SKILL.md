---
name: build-resolver
version: 2.0.0
description: Surgical build and type-error diagnostic and repair skill. Resolves compilation failures, missing dependencies, and linter/type-checker errors with minimal code diffs and zero scope drift.
botex_recipe: build-resolver
tags: [build, compiler, typecheck, troubleshooting, minimal-diff]
---

# Build & Type-Error Resolver Skill

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)
Gdy agent zewnętrzny (Antigravity, Devin, Codex) zleca naprawę błędu buildu do BoteXa:
- **MCP Tool:** `botex.start_task` lub `botex.run_subagent`
- **Recipe:** `build-resolver`
- **Tryb:** `edit` (BoteX lokalizuje błąd w logach kompilacji, aplikuje chirurgiczny patch i weryfikuje ponownym buildem)
- **Format zlecenia:**
```markdown
### GOAL
Zdiagnozuj i napraw błąd kompilacji / typowania / lintingu: [krótki opis błędu lub komenda która pada].

### CONSTRAINTS & CONTEXT
- Zasada Minimal Diff: Wprowadź wyłącznie minimalne zmiany konieczne do zielonego buildu.
- Zero Refactoring: Zakaz przepisywania architektury, zmian formatowania czy sprzątania kodu pobocznego.
- Scope: [pliki zgłaszające błędy kompilatora/lintera]

### REFS
- Build Logs / Command: [np. `pytest`, `mypy botex/`, `npm run build`, `cargo check`]
- Memory Vault Key: [opcjonalny klucz pamięci]
```

---

## 2. Canonical Engineering Rules (Zasady Chirurgicznej Naprawy)

### Filar I: Zasada Minimalnego Diffu (Minimal Diff Invariant)
- **Cel pojedynczy:** Przywrócenie stanu zielonego buildu (`exit code 0`).
- **Zakaz dryfu:** Naprawa błędu buildu to NIE jest moment na refaktoryzację, zmianę bibliotek, modernizację architektury czy formatowanie kodu.
- **Odwracalność:** Każdy patch musi być zwięzły, czytelny i łatwy do zrewidowania w Code Review.

### Filar II: Diagnoza Przyczyny Pierwotnej (Root-Cause Diagnosis)
1. **Lokalizacja błędu:** Zbadaj pierwszy błąd w logu kompilacji (błędy kaskadowe poniżej często wynikają z pierwszego).
2. **Kategorie awarii:**
   - *Missing Import / Dependency:* Brakujący moduł, brakująca biblioteka w `requirements.txt` / `package.json` / `Cargo.toml`.
   - *Type Mismatch / Signature Drift:* Zmiana sygnatury w upstreamie, niezgodność typów `Optional[T]` vs `T`, niekompatybilne generyki.
   - *Syntax & Syntax Compatibility:* Błędy składniowe, użycie funkcji z nowszej wersji języka niż wspierana w środowisku (np. Python 3.10 match-case w 3.9).
   - *Circular Dependency:* Zapętlenie importów w czasie ładowania modułów.

### Filar III: Pętla Weryfikacji (Verification Loop)
1. **Reprodukcja:** Uruchom komendę wywołującą błąd (`run_command`), rejestrując dokładny kod błędu i linię.
2. **Surgical Patch:** Zastosuj punktową poprawkę (`apply_patch`).
3. **Re-Test:** Uruchom tę samą komendę buildu. Upewnij się, że błąd ustąpił i nie pojawiły się nowe błędy w powiązanych modułach.

---

## 3. Typowe Błędy i Wzorce Naprawcze według Stosu
- **Python / Mypy / Pyright:**
  - *Błąd:* `Item "None" of "Optional[X]" has no attribute "Y"`
  - *Fix:* Dodanie asercji lub guard clause: `if val is None: return ...` lub `assert val is not None`.
- **TypeScript / ESLint:**
  - *Błąd:* `Type 'X | undefined' is not assignable to type 'X'`
  - *Fix:* Optional chaining `?.`, nullish coalescing `??`, lub narrowing przez `typeof`/`if`.
- **Rust / Cargo:**
  - *Błąd:* `cannot borrow as mutable more than once at a time` / `value moved here`
  - *Fix:* Klonowanie jeśli dozwolone (`.clone()`), refaktoryzacja zakresu borrowa `{ ... }`, lub przekazywanie referencji `&`.

---

## 4. Structured Handoff & Memory Contract (Portable Remote/VPS Format)
> **Zasada Ścieżek Przenośnych (Repo-Relative Paths):** Wszelkie wpisy w `artifact_refs` MUSZĄ być ścieżkami względnymi względem roota repozytorium z separatorami Unix (`/`), np. `botex/server_cloud.py`. Bezwzględny zakaz stosowania ścieżek bezwzględnych (`C:\...`, `/home/...`).

Zakończenie zadania naprawy buildu musi zwrócić ustrukturyzowany blok:
```json
{
  "status": "DONE" | "FAILED" | "BLOCKED",
  "summary": "Syntetyczne podsumowanie: zdiagnozowany błąd pierwotny, nałożony patch, wynik re-kompilacji",
  "build_status": {
    "command": "mypy botex/",
    "exit_code": 0,
    "status": "SUCCESS"
  },
  "artifact_refs": [
    "botex/server_cloud.py"
  ],
  "memory_keys": [
    "build/repair-<module-name>"
  ],
  "blocking_reason": null
}
```
Zapisz wpis diagnostyczny w BoteX Memory Vault.

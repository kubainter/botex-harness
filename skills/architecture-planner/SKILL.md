---
name: architecture-planner
version: 2.0.0
description: Spec-first architectural planning, interface decomposition, and Architecture Decision Record (ADR) authoring. Pre-implementation blueprinting that eliminates scope creep, interface conflicts, and architectural regressions.
botex_recipe: planner
secondary_recipe: architecture-adr
tags: [architecture, planning, adr, system-design, interface-design]
---

# Architecture Planning & ADR Skill

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)
Gdy agent zewnętrzny (Antigravity, Devin, Codex) zleca zaprojektowanie architektury nowego modułu lub planu refaktoryzacji do BoteXa:
- **MCP Tool:** `botex.start_task` lub `botex.run_subagent`
- **Recipe:** 
  - `planner` (w trybie readonly do analizy repozytorium i opracowania sekwencyjnego planu wdrożenia)
  - `architecture-adr` (w trybie edit/readonly do sformułowania sformalizowanego rekordu decyzji architektonicznej ADR)
- **Format zlecenia:**
```markdown
### GOAL
Opracuj plan wdrożenia i specyfikację architektury dla: [opis nowej funkcjonalności lub refaktoryzacji].

### CONSTRAINTS & CONTEXT
- Zakres: [moduły objęte zmianą i ich zależności]
- Cel: Identyfikacja interfejsów, analiza ryzyk regresji, podział na małe, atomowe etapy wdrożeniowe.

### REFS
- Files: [file:///...]
- Memory Vault Key: [opcjonalny klucz pamięci]
```

---

## 2. Canonical Engineering Rules (Zasady Projektowania Architektury)

### Filar I: Zasada Spec-First (Najpierw Interfejsy, Potem Kod)
- **Kontrakty i DTO:** Przed rozpoczęciem implementacji zdefiniuj sygnatury metod, modele danych (Pydantic / TypeScript Interfaces / Structs) oraz formaty wymiany JSON.
- **Odporność na rozjazd:** Ustalenie jednoznacznych kontraktów zapobiega konfliktom, gdy nad różnymi częściami systemu pracują równolegle różni agenci.

### Filar II: Dekompozycja na Atomowe Etapy (Phased Delivery)
Każdy plan musi dzielić zadanie na fazy, które można niezależnie przetestować i zcommitować:
1. **Faza 0 (Recon & Spikes):** Weryfikacja założeń, sprawdzenie istniejącego kodu i zależności.
2. **Faza 1 (Data & Contracts):** Definicja struktur, typów, tabel DB lub schematów zdarzeń.
3. **Faza 2 (Core Business Logic & TDD):** Implementacja czystej logiki biznesowej z testami jednostkowymi.
4. **Faza 3 (Integration & Adapters):** Spięcie z API, bazą danych, interfejsem CLI lub systemem zdarzeń.
5. **Faza 4 (Verification & Observability):** Testy end-to-end, metryki, logowanie, dokumentacja.

### Filar III: Ocena Ryzyk i Analiza Wpływu (Impact & Blast Radius)
- **Blast Radius Analysis:** Jakie moduły w repozytorium importują modyfikowane komponenty?
- **Backward Compatibility:** Czy zmiana łamie istniejące API? Czy wymagana jest ścieżka migracyjna lub flaga feature flag?
- **Rollback Strategy:** W jaki sposób wycofać zmianę w razie nieprzewidzianej awarii w produkcji?

---

## 3. Standard Rekordu Decyzji Architektonicznej (ADR Format)
Gdy podejmowana jest istotna decyzja technologiczna, plan musi zawierać lub wygenerować plik ADR w formacie:
```markdown
# ADR-[NUMER]: [Tytuł Decyzji]

## Status
[PROPOSED | ACCEPTED | DEPRECATED | SUPERSEDED]

## Context
Jaki problem rozwiązujemy? Jakie były ograniczenia techniczne i biznesowe?

## Decision
Co zdecydowaliśmy się zrobić i dlaczego? Jaka technologia lub wzorzec projektowy został wybrany?

## Consequences
- Pozytywne: Jakie korzyści uzyskujemy?
- Negatywne / Trade-offs: Jakie koszty lub ograniczenia akceptujemy?
- Wpływ na inne komponenty: Co wymaga dostosowania?
```

---

## 4. Structured Handoff & Memory Contract (Portable Remote/VPS Format)
> **Zasada Ścieżek Przenośnych (Repo-Relative Paths):** Wszelkie wpisy w `artifact_refs` MUSZĄ być ścieżkami względnymi względem roota repozytorium z separatorami Unix (`/`), np. `docs/adr/ADR-004.md` lub `src/domain/interfaces.ts`. Bezwzględny zakaz stosowania ścieżek bezwzględnych (`C:\...`, `/home/...`).

Zakończenie zadania planowania musi zwrócić ustrukturyzowany blok:
```json
{
  "status": "DONE" | "FAILED" | "BLOCKED",
  "summary": "Syntetyczne podsumowanie planu architektonicznego, liczba zidentyfikowanych etapów i ocena ryzyka",
  "plan_overview": {
    "total_phases": 4,
    "breaking_changes": false,
    "risk_level": "LOW" | "MEDIUM" | "HIGH"
  },
  "artifact_refs": [
    "docs/adr/ADR-004.md"
  ],
  "memory_keys": [
    "architecture/plan-<feature-name>"
  ],
  "blocking_reason": null
}
```
Zapisz plan i kluczowe ustalenia w BoteX Memory Vault (`save_memory`).

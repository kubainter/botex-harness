---
name: security-audit
version: 2.0.0
description: Comprehensive application security audit, multi-language taint analysis, and vulnerability hardening. Aligned with OWASP Top 10, CWE standards, and defensive engineering patterns.
botex_recipe: security-reviewer
secondary_recipe: security-taint-hardener
tags: [security, owasp, taint-analysis, hardening, cwe]
---

# Defensive Security Audit & Taint Analysis Skill

## 1. Multi-Agent Delegation Contract (Delegacja do BoteXa)
Gdy agent zewnętrzny (Antigravity, Devin, Codex) zleca audyt bezpieczeństwa do BoteXa:
- **MCP Tool:** `botex.start_task` lub `botex.run_subagent`
- **Recipe:** 
  - `security-reviewer` (w trybie readonly do analizy i raportowania podatności)
  - `security-taint-hardener` (w trybie edit do implementacji filtrów i walidacji wejść)
- **Format zlecenia:**
```markdown
### GOAL
Przeprowadź audyt bezpieczeństwa i analizę przepływu danych (Taint Analysis) w wyznaczonych modułach.

### CONSTRAINTS & CONTEXT
- Zakres: [moduły sieciowe, endpointy API, parsery, operacje filesystem/subprocess]
- Zasady: Zero tolerancji dla twardych sekretów, SQLi, SSRF i Command Injection.
- Ochrona .env: Ścisły zakaz odczytu plików .env i sekretów produkcyjnych.

### REFS
- Files: [file:///...]
- Memory Vault Key: [opcjonalny klucz pamięci]
```

---

## 2. Canonical Engineering Rules (Wspólny Rdzeń Bezpieczeństwa)
Każdy audyt musi przejść przez weryfikację wektorów ataku według klasyfikacji CWE / OWASP Top 10:

### Filar I: Analiza Przepływu Danych (Taint Flow: Sources -> Sanitizers -> Sinks)
Zidentyfikuj zewnętrzne źródła danych (*Sources*), sprawdź obecność rygorystycznej walidacji (*Sanitizers*) i zbadaj miejsca wykonania (*Sinks*):
- **Command Injection (CWE-78):**
  - *Sinks:* `subprocess.Popen(..., shell=True)`, `os.system()`, `child_process.exec()`, `exec.Command("sh", "-c")`.
  - *Remediation:* Zamiana na tablicę argumentów (`shell=False`, `execFile`), walidacja allowlistą znaków alfanumerycznych.
- **Path Traversal & Insecure Filesystem (CWE-22):**
  - *Sinks:* `open()`, `Path.joinpath()`, `fs.readFile()` z parametrem od użytkownika.
  - *Remediation:* Normalizacja przez `Path.resolve()`, asercja `resolved_path.is_relative_to(base_dir)`.
- **SQL / Query Injection (CWE-89):**
  - *Sinks:* Konkatenacja stringów w zapytaniach SQL / NoSQL / ORM raw queries.
  - *Remediation:* Bezwzględne użycie zapytań parametryzowanych (*prepared statements*).
- **Unsafe Deserialization & Code Evaluation (CWE-502 / CWE-95):**
  - *Sinks:* `pickle.loads()`, `yaml.load()` bez `SafeLoader`, `eval()`, `exec()`, `vm.runInContext()`.
  - *Remediation:* Zastąpienie bezpiecznym parserem JSON lub zdekodowaniem do ściśle typowanego DTO / Pydantic.

### Filar II: SSRF i Ochrona Granic Sieciowych (CWE-918)
- **Unrestricted Outbound HTTP:**
  - Weryfikacja docelowych adresów URL przed wykonaniem żądania `httpx`, `fetch`, `requests`.
  - Blokowanie pętli zwrotnych (`127.0.0.1`, `localhost`), prywatnych podsieci RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) oraz adresów link-local (`169.254.169.254` dla AWS metadata).

### Filar III: Zarządzanie Sekretami i Integralność Środowiska (CWE-798)
- **Zero .env Access Protocol:** Bezwzględny zakaz odczytu, wyświetlania lub commitowania plików `.env`, kluczy prywatnych `.pem`, tokenów JWT.
- **Weryfikacja wyłącznie systemowa:** Odczyt zmiennych wyłącznie przez `os.environ.get()` w kodzie produkcyjnym.
- **Log Masking:** Wszelkie nagłówki `Authorization`, ciasteczka sesyjne i payloady logowań muszą być maskowane w logach i telemetrii.

### Filar IV: Kontrola Dostępu i IDOR (CWE-639)
- Weryfikacja czy operacje na zasobach sprawdzają tożsamość i uprawnienia bieżącego użytkownika (Ownership Verification), a nie polegają wyłącznie na przekazanym identyfikatorze `user_id` / `resource_id`.

---

## 3. Direct Execution / Vulnerability Report Format
Każda wykryta podatność musi zostać opisana według jednolitego schematu:
- **Title:** Zwięzły opis luki (np. `Path Traversal w module export_report`)
- **Severity:** `CRITICAL` | `HIGH` | `MEDIUM` | `LOW`
- **CWE / OWASP:** np. `CWE-22`, `OWASP A01:2021`
- **Vulnerable Code Location:** `file:///sciezka/plik.ext#L10-L25`
- **Taint Trace:** Opis wejścia użytkownika i drogi do niebezpiecznego wykonania.
- **Concrete Fix:** Gotowy snippet kodu remediacyjnego.

---

## 4. Structured Handoff & Memory Contract (Portable Remote/VPS Format)
> **Zasada Ścieżek Przenośnych (Repo-Relative Paths):** Wszelkie wpisy w `artifact_refs` MUSZĄ być ścieżkami względnymi względem roota repozytorium z separatorami Unix (`/`), np. `botex/security.py` lub `docs/security/audit-report.md`. Bezwzględny zakaz stosowania ścieżek bezwzględnych (`C:\...`, `/home/...`).

Zakończenie zadania audytu lub hardeningu musi zwrócić ustrukturyzowany blok:
```json
{
  "status": "DONE" | "FAILED" | "BLOCKED",
  "summary": "Syntetyczne podsumowanie audytu bezpieczeństwa i liczby zidentyfikowanych ryzyk",
  "vulnerabilities": {
    "critical": 0,
    "high": 0,
    "medium": 0,
    "low": 0
  },
  "artifact_refs": [
    "sciezka/wzgledna/do/pliku.py"
  ],
  "memory_keys": [
    "security/audit-<target-module>"
  ],
  "blocking_reason": null
}
```
Raport bezpieczeństwa (bez wrażliwych sekretów) rejestrowany jest w BoteX Memory Vault.

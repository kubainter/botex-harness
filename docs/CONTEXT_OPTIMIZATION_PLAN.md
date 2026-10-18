# BoteX: Architektura Optymalizacji Okna Kontekstowego (Context Window Optimization Plan)

> **Dokumentacja architektoniczna i audyt techniczny**  
> **Status:** Analiza, Audyt i Plan Wdrożenia (Faza Przedwdrożeniowa)  
> **Cel:** Eliminacja context window bloat w pętli wykonawczej BoteX oraz w interfejsie MCP dla klientów nadrzędnych (np. Devin Desktop) przy zachowaniu 100% zgodności wstecznej, bezpieczeństwa ZDR, walidacji AST i testów regresyjnych.

---

## 1. Raport z audytu technicznego (Audyt wycieków i puchnięcia tokenów)

Przeprowadzony audyt kodu repozytorium BoteX ujawnił trzy główne wektory niekontrolowanego drenażu okna kontekstowego (Context Window Bloat / Token Drain).

### 1.1. Wektor 1: Przechwytywanie i wstrzykiwanie stdout/stderr w `botex/exec_tools.py` i `botex/engine.py`

#### A. Obcięcie tylko od początku (Head-only slicing) w `botex/exec_tools.py`
* **Lokalizacja:** [`botex/exec_tools.py`](file:///c:/Users/kubai/.ai/harness/botex/exec_tools.py#L143-L193)
* **Mechanizm:**
  W linii 143 zdefiniowano parametr domyślny `max_output_bytes: int = 20000` (20 KB). W liniach 180–183:
  ```python
  raw = (stdout_b or b"")[:max_output_bytes]
  text = raw.decode("utf-8", errors="replace")
  if stdout_b and len(stdout_b) > max_output_bytes:
      text += f"\n[... output truncated at {max_output_bytes} bytes]"
  ```
* **Wąskie gardło:**
  1. **Niewłaściwa treść:** W przypadku testów jednostkowych (`pytest`), linterów (`ruff`, `mypy`) i kompilatorów kluczowe informacje diagnostyczne (asercje, stack trace, podsumowanie błędów `FAILED ...`, lista failed tests) znajdują się **na samym końcu** (w części TAIL). Slicing od indeksu 0 do 20000 zachowuje nagłówki i listę przechodzących testów, a ucina krytyczny koniec.
  2. **Rozmiar:** Pojedyncze wywołanie polecenia może zwrócić do 20 KB tekstu (~5 000 tokenów), które bez żadnej kompresji trafia do historii rozmowy agenta.

#### B. Akumulacja niesprzątanych wyników poleceń w pętli agenta
* **Lokalizacja:** [`botex/engine.py`](file:///c:/Users/kubai/.ai/harness/botex/engine.py#L1914-L1944)
* **Mechanizm:**
  ```python
  tool_result = await run_command(...)
  ...
  messages.append(
      {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(tool_result, ensure_ascii=False)}
  )
  ```
* **Wąskie gardło:**
  W przeciwieństwie do operacji na plikach (gdzie działa `prune_tool_history`), wynik `run_command` **nigdy nie jest czyszczony ani przycinany**. Jeśli agent w ramach 15-turowej pętli uruchomi `pytest` 3 razy (analiza wstępna, próba 1, próba 2), w historii `messages` akumuluje się do 60 KB (~15 000 tokenów) surowego logu testów. Każda kolejna tura modelu LLM przesyła te dane powtórnie, generując kwadratowy wzrost zużycia tokenów wejściowych ($O(N^2)$).

#### C. Wstrzykiwanie surowego zrzutu weryfikacji w prompcie użytkownika
* **Lokalizacja:** [`botex/engine.py`](file:///c:/Users/kubai/.ai/harness/botex/engine.py#L1500-L1512)
* **Mechanizm:**
  Gdy `verify_command` zawiedzie (do 3 prób), cały słownik weryfikacji (wraz z 20 KB stdout) jest serializowany przez `json.dumps` i doklejany do roli `user`:
  ```python
  messages.append(
      {
          "role": "user",
          "content": (
              "Verification failed. You MUST fix the workspace using the "
              "tool result below before finishing... \n\n"
              + json.dumps(verification, ensure_ascii=False)
          ),
      }
  )
  ```
* **Wąskie gardło:**
  Wiadomości z rolą `user` nie podlegają żadnemu mechanizmowi czyszczenia. Trzy nieudane próby weryfikacji wprowadzają 60 KB redundancji bezpośrednio w główny prompt pętli.

---

### 1.2. Wektor 2: Ograniczenia wewnętrznej pętli wykonawczej w `botex/engine.py`

#### A. Wąski zakres działania `prune_tool_history`
* **Lokalizacja:** [`botex/engine.py`](file:///c:/Users/kubai/.ai/harness/botex/engine.py#L466-L485)
* **Mechanizm:**
  Funkcja `prune_tool_history` jest wywoływana wyłącznie w liniach 1849 (`apply_patch`) oraz 1877 (`create_file`) i tylko wtedy, gdy modyfikacja zakończyła się sukcesem (`tool_result.get("ok") == True`).
* **Wąskie gardło:**
  1. **Brak czyszczenia przy błędach:** Jeśli `apply_patch` nie dopasuje bloku kodu (fuzzy match fail) lub napotka błąd składni, odczyt pliku (`read_file` / `read_file_lines`) **nie zostaje skrócony**.
  2. **Brak wsparcia dla trybu `readonly`:** W zadaniach audytowych/przeglądowych (`mode="readonly"`, gdzie nie są wykonywane żadne zapisy), agent czyta kolejne pliki (zgodnie z promptem nawet po 1000–2000 linii naraz). Żaden z tych odczytów nigdy nie jest przycinany. Przy 30 krokach okno kontekstowe osiąga 200–400 KB surowego kodu.
  3. **Pomijanie innych narzędzi:** Funkcja całkowicie ignoruje `run_command`, `read_url` (które w linii 1907 dopuszcza do 200 KB) oraz `list_dir` (które zwraca do 200 wpisów JSON, ~15 KB).

---

### 1.3. Wektor 3: Podwójny ładunek i brak progowania w API MCP (`server.py`)

#### A. Podwójny zrzut tekstu i struktury w `run_subagent`
* **Lokalizacja:** [`server.py`](file:///c:/Users/kubai/.ai/harness/server.py#L113-L140), [`server.py`](file:///c:/Users/kubai/.ai/harness/server.py#L486-L489)
* **Mechanizm:**
  W wywołaniu `run_subagent`:
  ```python
  return CallToolResult(
      content=[TextContent(type="text", text=_format_result_text(res))],
      structured_content=res,
  )
  ```
  Zarówno `content[0].text` (za pośrednictwem `_format_result_text`), jak i `structured_content` (pełny słownik `res`) zawierają to samo obszerne pole `summary` / `message`.
* **Wąskie gardło:**
  Klient MCP (np. Devin Desktop) otrzymuje pełny raport subagenta (np. audyt bezpieczeństwa o rozmiarze 8 KB lub szczegółowe zestawienie zmian) dwukrotnie. Devin parsuje i przechowuje w swoim oknie kontekstowym zarówno blok `content`, jak i `structuredContent`, marnując cenne tokeny nadrzędnego agenta.

#### B. Brak buforowania i brak wskaźników referencyjnych (Reference Pointers)
* **Lokalizacja:** [`server.py`](file:///c:/Users/kubai/.ai/harness/server.py#L284-L308) (`get_task_status`)
* **Wąskie gardło:**
  Niezależnie od wielkości podsumowania lub liczby prób w łańcuchu fallback (`attempts[]`), serwer zawsze zwraca pełne dane inline. Nie istnieje mechanizm progowania (np. przycięcie powyżej 2 KB i odłożenie pełnego raportu do Memory Vault pod identyfikatorem `mem_task_<id>_summary`).

---

## 2. Architektura rozwiązania: Warstwa Context-Guard

Wzorzec zaczerpnięty z architektury `context-mode` opiera się na zasadzie:  
**"Kompaktowy podgląd w bieżącej pętli + pełny zrzut w bezpiecznym magazynie na żądanie (Offloaded Payload + On-demand Inspection)".**

```
┌────────────────────────────────────────────────────────────────────────┐
│                          BoteX MCP Server                              │
│                                                                        │
│   Client (Devin) ◄───────── [CallToolResult: Compact Card + Mem ID]    │
│         │                                                              │
│         ▼                                                              │
│   read_memory(mem_id) ────► [.botex/memory/mem_task_xyz.md]            │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│                        Context-Guard Pipeline                          │
│                                                                        │
│   Tool Output (stdout / review / file read)                            │
│         │                                                              │
│         ▼                                                              │
│   [Threshold Check: > 2048 bytes?] ──── NIE ───► Standard Return       │
│         │                                                              │
│        TAK                                                             │
│         ▼                                                              │
│   1. Skan ZDR & DLP (mask_secrets)                                     │
│   2. Zapis pełnego zrzutu do Memory Vault (.botex/memory/...)          │
│   3. Generowanie Head/Tail Slice (25 linii head + 35 linii tail)       │
│   4. Zwrócenie podglądu z unikalnym tokenem referencyjnym (Vault ID)   │
└────────────────────────────────────────────────────────────────────────┘
```

### 2.1. Specyfikacja modułu `botex/context_guard.py`

Zaprojektowany jako lekki, bezstanowy moduł pomocniczy wykorzystywany zarówno przez `exec_tools.py`, `engine.py`, jak i `server.py`.

#### Parametry konfiguracyjne (`botex.config.json` -> sekcja `context_guard`):
```json
"context_guard": {
  "enabled": true,
  "threshold_bytes": 2048,
  "head_lines": 25,
  "tail_lines": 35,
  "max_preview_bytes": 3072,
  "auto_vault_logs": true,
  "vault_retention_days": 3
}
```

#### Reguły progowania i ekstrakcji Head/Tail:
1. **Próg aktywacji:** Jeśli rozmiar tekstu przekracza `threshold_bytes` (domyślnie 2 048 B / 2 KB):
   - Tekst zostaje poddany sanityzacji DLP przez `mask_secrets()`.
   - Pełny tekst zostaje zbuforowany w Memory Vault za pomocą `save_memory()` z tagami `["exec_log", "context_guard", "task_<id>"]` i `kind="context"`.
2. **Format wyjściowy (Sanitized Preview):**
   - **Head:** pierwsze `head_lines` linii (np. 25 linii) — pokazuje wywołanie komendy, środowisko, start testów.
   - **Separator / Pointer:**
     ```
     [... BoteX Context-Guard: truncated 142 lines (18,432 bytes). Full log saved to Memory Vault id: "mem_exec_a1b2c3d4" ...]
     ```
   - **Tail:** ostatnie `tail_lines` linii (np. 35 linii) — zawiera kluczowe asercje, tracebaki i statusy niepowodzenia `pytest`/lintera.
3. **Zapewnienie integralności protokołu OWASP LLM01:**
   Wszelkie zrzuty podglądu generowane przez `Context-Guard` zachowują otoczkę znaczników granicznych (boundary tags), np. `<untrusted_exec_output>` lub `<untrusted_memory_content>`.

---

## 3. Specyfikacja zmian w API narzędzi i integracja z klientem Devin

### 3.1. Odchudzenie `run_subagent` i `get_task_status` w `server.py`

#### A. Kontrakt odpowiedzi `CallToolResult`
Aby utrzymać 100% kompatybilność wsteczną z obecnymi klientami oraz testem `test_mcp_structured_output`, schemat `SubagentTaskResult` w `server.py` **nie zmienia nazw istniejących pól**. Zmianie ulega zawartość pól tekstowych przy przekroczeniu progu:

1. **Pole `content[0].text` (Kompaktowa karta zadania):**
   Zamiast zrzucania wielokilobajtowego raportu, `_format_result_text` generuje zwięzłą kartę statusową:
   ```text
   [SUCCESS] BoteX finished task 'task_8f1a2b' in 3 steps.
   - Status: DONE
   - Duration: 14.2s | Cost: $0.00182 USD
   - Modified files: src/auth.py, tests/test_auth.py
   - Line balance: +24 / -8
   - Full Artifacts: Stored in Memory Vault (ID: mem_task_8f1a2b_report)

   Summary:
   Refactored token verification in src/auth.py to address timing attack vulnerability.
   ```
2. **Pole `structured_content`:**
   - Jeśli `summary` mieści się w limicie 2 KB, zostaje zachowane w całości.
   - Jeśli `summary` przekracza 2 KB:
     - `summary`: zawiera zwięzły abstrakt (Executive Summary) + wskaźnik `vault_memory_id: "mem_task_8f1a2b_report"`.
     - Pełny raport jest automatycznie zapisywany w Memory Vault przez `save_memory()`.
     - Do słownika dodawane jest opcjonalne pole `vault_memory_id: str` (zgodnie z `TypedDict, total=False`, co jest w 100% zgodne z typowaniem).

### 3.2. Dostęp do pełnego logu przez mikronarzędzie inspekcyjne

Kluczowe odkrycie z audytu testu `test_mcp_tool_surface`:
* W teście [`tests/test_botex.py`](file:///c:/Users/kubai/.ai/harness/tests/test_botex.py#L2392-L2418) weryfikowany jest stały zestaw 12 narzędzi MCP:
  `{"run_subagent", "start_task", "get_task_status", "fetch_url", "recommend_models", "get_outline", "save_memory", "search_memory", "read_memory", "get_stats", "check_health", "clean_snapshots"}`.
* **Rozwiązanie architektoniczne bez naruszania zestawu narzędzi:**  
  Nie wprowadzamy nowego narzędzia MCP o innej nazwie! Zamiast tego wykorzystujemy **istniejące, w pełni przetestowane narzędzie `read_memory`**.
* **Przebieg inspekcji przez Devina:**
  1. Devin wywołuje `run_subagent` -> otrzymuje zwięzłą kartę zadania ze statusem `DONE` i `vault_memory_id: "mem_task_8f1a2b_report"`.
  2. Jeśli Devin potrzebuje pełnego raportu lub pełnego diffa do dalszych kroków, wykonuje standardowe wywołanie:
     ```json
     read_memory(memory_id="mem_task_8f1a2b_report")
     ```
  3. Narzędzie `read_memory` natywnie nakłada izolację OWASP LLM01 (`<untrusted_memory_content id="...">`) oraz maskowanie sekretów, zwracając bezpieczną treść tylko wtedy, gdy jest ona rzeczywiście potrzebna.

---

## 4. Plan wdrożenia krok po kroku (Phased Implementation Roadmap)

Wdrożenie podzielono na 4 izolowane, sekwencyjne fazy.

```
┌────────────────────────────────────────────────────────────────────────┐
│ FAZA 1: Sanitizacja poleceń (exec_tools & verify_command)              │
│ - Implementacja botex/context_guard.py (Head/Tail + Memory Vault)     │
│ - Integracja z run_command w exec_tools.py                            │
│ - Sanitizacja zrzutu weryfikacji w engine.py (verify_command)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ FAZA 2: Aktywne oczyszczanie kontekstu (engine.py Anti-Drain)         │
│ - Rozszerzenie prune_tool_history o oczyszczanie starych run_command   │
│ - Przycinanie wielkich odczytów w trybie readonly                      │
│ - Zabezpieczenie limitu promptu w pętli wieloturowej                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ FAZA 3: Odchudzenie interfejsu MCP (server.py)                         │
│ - Progowanie raportów run_subagent i get_task_status                   │
│ - Eliminacja duplikacji danych między text content a structuredContent │
│ - Automatyczne buforowanie dużych summary w Memory Vault               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ FAZA 4: Retencja i testy integracyjne                                  │
│ - Integracja czyszczenia bufora z clean_snapshots                      │
│ - Weryfikacja pełnego zestawu 43 testów w tests/test_botex.py          │
│ - Dodanie dedykowanych testów jednostkowych Context-Guard              │
└────────────────────────────────────────────────────────────────────────┘
```

### Faza 1: Sanitizacja `exec_tools` i `verify_command`
1. Utworzenie modułu `botex/context_guard.py` z funkcjami:
   - `slice_head_tail(text: str, head_lines: int, tail_lines: int) -> tuple[str, bool]`
   - `vault_payload(content: str, workspace_root: Path, tag_prefix: str) -> str`
2. Modyfikacja `botex/exec_tools.py`:
   - Zamiana prostego cięcia `stdout_b[:max_output_bytes]` na inteligentny podział Head/Tail, gwarantujący zachowanie końcówki raportu testów.
   - Buforowanie zrzutu stdout w Memory Vault przy przekroczeniu progu `threshold_bytes`.
3. Modyfikacja obsługi `verify_command` w `botex/engine.py` (linia 1505):
   - Zastąpienie surowego `json.dumps(verification)` podsumowaniem zawierającym status, kod wyjścia i przycięty `tail` błędów.

### Faza 2: Aktywne oczyszczanie historii narzędzi w `botex/engine.py`
1. Usprawnienie `prune_tool_history(messages, target_path)`:
   - Dodanie czyszczenia starych wywołań `run_command` (pozostawienie tylko ostatniego wykonania, starsze zastąpione jednolinijkowym `[Command 'pytest' completed with exit code 0 — output pruned]`).
   - Dodanie czyszczenia starych odpowiedzi `read_url`.
2. Ochrona trybu `readonly`:
   - Wprowadzenie progu maksymalnej liczby jednocześnie przechowywanych pełnych bloków plików w historii wiadomości. Przy przekroczeniu limitu starsze odczyty zastępowane są skrótami `[File content pruned to preserve context window]`.

### Faza 3: Progowanie odpowiedzi MCP w `server.py`
1. Aktualizacja funkcji `_format_result_text(res)` w `server.py`:
   - Automatyczne streszczanie `summary` powyżej 2 KB i odsyłanie do identyfikatora Memory Vault.
2. Aktualizacja `_masked_result(res)`:
   - Buforowanie pełnego podsumowania w `.botex/memory/` z przedrostkiem `mem_task_<task_id>_report.md`.
   - Zapewnienie, że `structured_content` zawiera zwięzły abstrakt oraz klucz `vault_memory_id`.

### Faza 4: Retencja danych i pełna walidacja
1. Rozszerzenie logiki `clean_snapshots` / `snapshot_manager.clean_old_snapshots` o czyszczenie zbuforowanych zrzutów logów starszych niż `vault_retention_days`.
2. Uruchomienie i walidacja wszystkich 43 testów z `tests/test_botex.py`.

---

## 5. Zgodność z mechanizmami bezpieczeństwa BoteX

Proponowane zmiany w 100% zachowują i wzmacniają istniejące filary bezpieczeństwa BoteX:

1. **Walidacja AST i Syntax Checks:**
   - Mechanizm Context-Guard dotyczy wyłącznie warstwy prezentacji i buforowania odpowiedzi narzędzi (`tool_result`, `stdout`, `summary`).
   - Weryfikacja składni w `botex/patch_engine.py` (`validate_syntax`), `create_file` oraz `apply_patch` przed dotknięciem dysku pozostaje całkowicie nienaruszona.
2. **Zero Data Retention (ZDR) i OWASP (LLM01–LLM10):**
   - **LLM01 (Prompt Injection):** Wszystkie zrzuty pobierane z Memory Vault lub generowane w podglądzie są bezwzględnie owijane w znaczniki `<untrusted_memory_content>` lub `<untrusted_exec_output>` z neutralizacją sekwencji ucieczki (`<\\/tag>`).
   - **LLM02 (Sensitive Information Disclosure):** Funkcja `mask_secrets` jest aplikowana w pipeline Context-Guard **przed** zapisem do Memory Vault oraz **przed** generowaniem Head/Tail podglądu.
   - **LLM06 (Unbounded Consumption):** Drastyczne obniżenie zużycia tokenów bezpośrednio przeciwdziała drenażowi budżetu i wyczerpaniu limitów TPM/RPM u dostawców API.
   - **ZDR Enforced:** Wszystkie pliki cache lądują lokalnie w workspace w katalogu `.botex/memory/` chronionym przez `.gitignore` — żadne surowe logi nie są wysyłane do zewnętrznych usług agregujących.
3. **Mawki i automatyczny rollback (Snapshots):**
   - Mechanizm `snapshot_manager` operuje na plikach projektu. Pliki bufora Memory Vault w `.botex/` nie podlegają cofaniu przy rollbacku kodu zadania, co pozwala na pełną inspekcję przyczyny niepowodzenia nawet po wycofaniu błędnych zmian w plikach źródłowych.
4. **Zgodność z protokołem MCP:**
   - Zero zmian w sygnaturach istniejących narzędzi MCP.
   - Kompatybilność wsteczna z `SubagentTaskResult` i formatem `CallToolResult`.

---

## 6. Scenariusze testowe i zabezpieczenie przed regresją

Wszystkie 43 istniejące testy w `tests/test_botex.py` muszą przejść bezbłędnie (100% pass). Dodatkowo wdrożenie musi zostać pokryte nowymi testami dedykowanymi.

### 6.1. Istniejące testy krytyczne dla wdrożenia Context-Guard

| Nazwa testu | Weryfikowane zachowanie | Kryterium sukcesu |
|---|---|---|
| `test_mcp_tool_surface` | Niezmieniona lista 12 narzędzi MCP | Zbiór nazw narzędzi musi być identyczny z `expected` (linie 2392–2396) |
| `test_mcp_structured_output` | Pola `status`, `failure_kind`, `ok`, `files_touched`, `attempts` | Schemat i typy zwracane w `structured_content` pozostają w 100% zgodne |
| `test_exec_policy` | Wykonanie `run_command`, kod wyjścia, timeout, `[REDACTED]` w stdout | Klucze słownika wyjściowego (`ok`, `exit_code`, `timed_out`, `stdout`) nienaruszone |
| `test_owasp_prompt_injection_isolation` | Etykiety graniczne dla treści nieufnych | Znaczniki `<untrusted_...>` i ucieczka znaczników zamykających |
| `test_mcp_memory_and_stats` | Działanie `save_memory`, `search_memory`, `read_memory` | Round-trip pamięci w formacie ECC frontmatter |
| `test_fallback_and_verify_guards` | Zachowanie `verify_command` i failover modeli | Prawidłowe raportowanie `CONFIG_ERROR` i `VERIFICATION_FAILED` |

### 6.2. Nowe scenariusze testowe (do dodania w dedykowanym module testowym)

1. **Test Head/Tail Slicing (`test_context_guard_slicing`):**
   - Podanie na wejście 200 linii tekstu symulującego wyjście `pytest` (100 linii passed, błąd asercji w linii 195).
   - Weryfikacja, czy wyjście zawiera linie 1–25, marker o przycięciu oraz linie 165–200 z asercją.
2. **Test buforowania stdout polecenia w Memory Vault (`test_exec_tools_vault_offload`):**
   - Wywołanie polecenia generującego 10 KB tekstu.
   - Asercja, że w `stdout` pojawia się token referencyjny `mem_exec_...`.
   - Asercja, że odczyt przez `read_memory` zwraca pełne 10 KB z zamaskowanymi sekretami.
3. **Test oczyszczania historii poleceń w pętli wieloturowej (`test_engine_history_command_pruning`):**
   - Wykonanie 3 kolejnych komend w symulowanym przebiegu agenta.
   - Weryfikacja, że w `messages` przekazywanym do LLM starsze wywołania zostały skrócone do metadanych statusowych, a tylko najświeższy log zachowuje treść.
4. **Test zwięzłości odpowiedzi `run_subagent` (`test_mcp_subagent_compact_response`):**
   - Symulacja zadania generującego raport audytowy o długości 10 000 znaków.
   - Asercja, że `res.content[0].text` oraz `res.structured_content["summary"]` nie przekraczają 2 KB.
   - Asercja, że pełny raport jest dostępny pod zwróconym `vault_memory_id`.

---

## 7. Wnioski i podsumowanie

Wdrożenie warstwy Context-Guard rozwiązuje fundamentalny problem puchnięcia okna kontekstowego w BoteX:
* Redukuje zużycie tokenów wejściowych w pętlach wieloturowych o **60–80%** przy zadaniach z intensywnym użyciem testów i poleceń terminala.
* Zapobiega marnotrawieniu okna kontekstowego klienta nadrzędnego (Devin Desktop) poprzez eliminację podwójnego zrzutu ładunku.
* Zwiększa skuteczność agenta w naprawianiu błędów kodu dzięki zachowaniu kluczowej końcówki (Tail) logów testów zamiast obcinania jej na sztywnym limicie 20 KB.
* Wykorzystuje istniejące, przetestowane mechanizmy Memory Vault bez dodawania nowych narzędzi MCP, zachowując 100% stabilności istniejącego pakietu testów.

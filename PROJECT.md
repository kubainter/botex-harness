# Project: BoteX Harness - Provider Architecture & Dynamic Catalog Refactor

## Architecture
- **Provider Subsystem (`botex/providers.py` or `botex/providers/`)**:
  - `BaseProvider`: Abstract base class defining provider interface (`resolve_api_key`, `get_client`, `normalize_response`, `sanitize_error`, `fetch_catalog`, `get_credits`).
  - Specialized adapters:
    - `OpenRouterProvider`: Handles OpenRouter attribution headers, ZDR wire flags, reasoning tokens, credits endpoints (`/api/v1/auth/key` and `/api/v1/credits`).
    - `NvidiaProvider`: Handles NVIDIA NIM OpenAI-compatible endpoint with explicit capabilities.
    - `GenericOpenAIProvider`: Handles custom OpenAI-compatible endpoints.
  - `ProviderRegistry`: Singleton / factory registry mapping provider names to provider classes and instances.
  - Backward Compatibility Layer: Retains top-level module functions `build_async_client(provider_name, provider_cfg, api_key, client_factory=AsyncOpenAI)`, `normalize_response(response)`, `resolve_api_key(explicit, provider_name, provider_cfg, secrets_cfg)`, `sanitize_provider_error(exc, max_chars=1024)`.
  - Immutable Data Models: `NormalizedResponse`, `NormalizedToolCall`, `NormalizedUsage`.

- **Catalog & Filtering Subsystem (`botex/catalog.py`, `botex/pricing.py`, `botex/benchmarks.py`)**:
  - `ModelCatalog`: Unified catalog fetching service with TTL caching (`.model_pricing.json` / cache file), offline fallback, and rich metadata extraction (`context_length`, `pricing`, `supported_parameters`, `supports_tools`, `is_zdr_compliant`).
  - `DenylistFilter`: Multi-stage model filtering engine supporting exact matches, glob (`fnmatch`), and regex (`re:`) patterns across global (`engine.denylist`), provider (`providers.<p>.denylist`), and profile (`providers.<p>.profile_denylists.<profile>`) configurations.
  - Candidate Pruning: Integration into `resolve_model_candidates` in `botex/config.py` and `get_recommended_models` in `botex/benchmarks.py`.

- **Wallet & Credits Subsystem (`botex/wallet.py`)**:
  - `CreditsInfo`: Dataclass containing balance, usage, limit, limit_remaining, is_free_tier, supported status.
  - `WalletService`: Provider-agnostic balance querying service with CWE-312 secret masking.

- **Security & Quality Guarantees**:
  - CodeQL CWE-312 taint-safety: Static dictionary mappings for enums/status strings, secret masking with `mask_secrets()`.
  - Zero unmocked network calls in unit tests.
  - Ruff formatting and lint clean across entire repository (`ruff check .` and `ruff format --check .`).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| 1 | BaseProvider Interface | Abstract base class with client creation, response normalization, error sanitization, catalog fetching, and credit querying | M1 | R1 |
| 2 | OpenRouterProvider Adapter | Concrete adapter with attribution headers, ZDR parameters, and credits API integration | M1 | R1 |
| 3 | NvidiaProvider Adapter | Concrete adapter for NVIDIA NIM API with capability overrides | M1 | R1 |
| 4 | GenericOpenAIProvider Adapter | Generic OpenAI-compatible adapter for custom endpoints | M1 | R1 |
| 5 | ProviderRegistry | Registry pattern for registering and instantiating provider adapters | M1 | R1 |
| 6 | Backward-Compatible Facade | Module-level functions in `botex/providers.py` delegating to Registry while maintaining exact legacy signatures | M1 | R1, R4 |
| 7 | Dynamic Model Catalog | Service fetching model metadata from provider APIs with TTL caching and stale offline fallback | M2 | R2 |
| 8 | Model Metadata Extraction | Extraction of context length, token pricing (USD/1M), supported parameters, tool-calling support, and ZDR compliance | M2 | R2 |
| 9 | Multi-Stage Denylist Engine | Filter supporting exact, glob, and regex pattern matching across global, provider, and profile denylists | M2 | R2 |
| 10 | Benchmark & Candidate Pruning | Filtering candidates in `resolve_model_candidates` and `get_recommended_models` against tool support and denylists | M2 | R2 |
| 11 | Unified Credits & Balance Model | `CreditsInfo` dataclass representing account and key-level usage, limit, remaining balance, and free-tier flags | M3 | R3 |
| 12 | Wallet & Credits Service | Service querying `/api/v1/auth/key` and `/api/v1/credits` with graceful fallback for unsupported providers | M3 | R3 |
| 13 | CWE-312 Secret Masking | Rigorous secret masking in errors, logs, credits data, and catalog responses | M3 | R3, R4 |
| 14 | Configuration Schema Extensions | `botex.config.json` support for catalog caching, denylist patterns, and wallet configuration | M4 | R2, R4 |
| 15 | Engine & CLI/MCP Integration | Seamless execution in `botex/engine.py` and exposure in CLI commands / MCP tools | M4 | R4 |
| 16 | Comprehensive Mocked Unit Tests | Offline unit test suite covering providers, catalog, denylist, and wallet | M5 | Acceptance Criteria |
| 17 | Zero-Regression & Lint Verification | 100% pass on all existing tests (`test_botex.py`, `test_context_guard.py`), ruff check & format clean | M5 | Acceptance Criteria |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Provider Registry & BaseProvider | Implement `BaseProvider`, concrete adapters (`OpenRouter`, `Nvidia`, `GenericOpenAI`), `ProviderRegistry`, and backward-compatible module facade in `botex/providers.py` with unit tests in `tests/test_providers.py` | none | DONE |
| M2 | Dynamic Model Catalog & Denylist Engine | Implement `ModelCatalog` service with TTL caching, rich metadata parsing, and `DenylistFilter` (exact, glob, regex; global, provider, profile), integrate with `benchmarks.py` and `pricing.py` with unit tests in `tests/test_catalog.py` and `tests/test_denylist.py` | M1 | DONE |
| M3 | Wallet & Credits Service | Implement `CreditsInfo` and `WalletService` with OpenRouter `/api/v1/auth/key` and `/api/v1/credits` integration, graceful fallback, and secret masking with unit tests in `tests/test_wallet.py` | M1 | DONE |
| M4 | Engine & CLI/Config Integration | Wire dynamic denylist into `resolve_model_candidates` in `botex/config.py`, verify `botex/engine.py` and `server.py`, extend `botex.config.json` schema cleanly | M1, M2, M3 | DONE |
| M5 | Comprehensive Test & Quality Verification | Full regression test run (`pytest tests/`), new unit tests validation, `ruff check .`, `ruff format --check .`, and CodeQL CWE-312 taint-safety verification | M4 | DONE |
| M6 | OpenRouter Live Intelligence, Routing & Structured Outputs | Live `/api/v1/generation` telemetry reconciliation, `/api/key/status` budget warnings, 5-gate in-payload `extra_body.models` failovers, strict JSON Schema completion (`TASK_COMPLETION_JSON_SCHEMA`), and authenticated tier-aware `recommend_models` | M5 | DONE |

## Interface Contracts

### `botex/providers.py`
- `BaseProvider`:
  - `resolve_api_key(explicit_key: Optional[str] = None, secrets_cfg: Optional[Dict[str, Any]] = None) -> Optional[str]`
  - `get_client(api_key: Optional[str] = None, client_factory: Any = AsyncOpenAI) -> Any`
  - `normalize_response(response: Any) -> NormalizedResponse`
  - `sanitize_error(exc: Exception, max_chars: int = 1024) -> str`
  - `fetch_catalog(api_key: Optional[str] = None) -> List[Dict[str, Any]]`
  - `get_credits(api_key: Optional[str] = None) -> CreditsInfo`
  - `get_generation_stats(generation_id: str, api_key: Optional[str] = None, timeout: float = 5.0) -> Optional[GenerationStats]`
- `ProviderRegistry`:
  - `register(name: str, provider_cls: Type[BaseProvider]) -> None`
  - `get(name: str, provider_cfg: Optional[Dict[str, Any]] = None) -> BaseProvider`
  - `list_providers() -> List[str]`
- Module-level Facade Functions:
  - `build_async_client(provider_name: str, provider_cfg: Dict[str, Any], api_key: Optional[str] = None, client_factory: Any = AsyncOpenAI) -> Any`
  - `normalize_response(response: Any) -> NormalizedResponse`
  - `resolve_api_key(explicit: Optional[str], provider_name: str, provider_cfg: Dict[str, Any], secrets_cfg: Optional[Dict[str, Any]] = None) -> Optional[str]`
  - `sanitize_provider_error(exc: Exception, max_chars: int = 1024) -> str`
  - `fetch_openrouter_generations_async(generation_ids: Sequence[str], provider_name: str = "openrouter", provider_cfg: Optional[Dict[str, Any]] = None, api_key: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]`

### `botex/catalog.py` & `botex/denylist.py`
- `ModelMetadata`:
  - `id: str`
  - `name: str`
  - `context_length: int`
  - `prompt_price_per_m: float`
  - `completion_price_per_m: float`
  - `supports_tools: bool`
  - `is_zdr_compliant: bool`
- `DenylistFilter`:
  - `is_allowed(model: str, provider: str, profile: Optional[str] = None) -> bool`
  - `filter_models(models: List[str], provider: str, profile: Optional[str] = None) -> List[str]`

### `botex/wallet.py`
- `CreditsInfo`:
  - `supported: bool`
  - `provider: str`
  - `balance_usd: Optional[float]`
  - `usage_usd: Optional[float]`
  - `spend_usd: Optional[float]`
  - `limit_usd: Optional[float]`
  - `limit_remaining_usd: Optional[float]`
  - `resets_at: Optional[str]`
  - `free_model_daily_requests: Optional[int]`
  - `is_free_tier: bool`
  - `label: Optional[str]`
- `WalletService`:
  - `get_credits(provider_name: str, provider_cfg: Dict[str, Any], api_key: Optional[str] = None) -> CreditsInfo`
  - `get_key_status(provider_name: str = "openrouter", provider_cfg: Optional[Dict[str, Any]] = None, api_key: Optional[str] = None) -> Dict[str, Any]`
  - `get_key_status_async(provider_name: str = "openrouter", provider_cfg: Optional[Dict[str, Any]] = None, api_key: Optional[str] = None) -> Dict[str, Any]`

### `botex/contracts.py`
- `TASK_COMPLETION_JSON_SCHEMA`: Strict JSON Schema (`additionalProperties: False`) for structured task completion envelopes.
- `parse_structured_completion(text: str) -> Optional[Dict[str, Any]]`

## Code Layout
- `botex/providers.py`: Provider registry, base provider, concrete provider adapters, generation telemetry, legacy facade.
- `botex/catalog.py`: Model catalog service, metadata models, caching.
- `botex/denylist.py`: Denylist filter engine (exact, glob, regex).
- `botex/wallet.py`: Wallet, credits, and API key budget health service.
- `botex/config.py`: Candidate resolution filtering integration and schema updates.
- `tests/test_providers.py`: Unit tests for ProviderRegistry and adapters.
- `tests/test_catalog.py`: Unit tests for catalog fetching, metadata, and caching.
- `tests/test_denylist.py`: Unit tests for denylist filtering.
- `tests/test_wallet.py`: Unit tests for wallet and credits queries.
- `tests/test_stage_4_6.py`: Unit and integration tests for Stage 4.6 OpenRouter live intelligence, routing, and structured outputs.


# BoteX Import Map (v2.1.0 modularization)

Legacy alias spellings are **load-bearing** for BoteX itself (`server.py`, `deploy/oracle-vps/botex-cloud.service:23` → `botex.server_cloud:app`). The shim in `botex/__init__.py` is test-guarded (`tests/test_module_aliases.py`); every future move must update both this map and the alias test.

**Rule for agents (incl. swarm workers):** in NEW code use the canonical (left) spelling. Both spellings resolve to the same module object (`sys.modules` graft, identity test-asserted).

| Canonical (new code) | Legacy alias (still valid) | Notes |
|---|---|---|
| `botex.core.engine` | `botex.engine` | agent loop, prompt-cache wiring |
| `botex.core.events` | `botex.events` | emitters, `drop_emitter` |
| `botex.core.approval` | `botex.approval` | agency policy |
| `botex.core.capabilities` | `botex.capabilities` | CAP_* model |
| `botex.core.context_guard` | `botex.context_guard` | budget knapsack |
| `botex.core.contracts` | `botex.contracts` | completion schema |
| `botex.core.meta_harness` | `botex.meta_harness` | — |
| `botex.core.tracing` | `botex.tracing` | — |
| `botex.core.context_inclusion` | `botex.context_inclusion` | — |
| `botex.core.state` | `botex.state` | in-flight checkpoints |
| `botex.memory.memory` | `botex.memory` | vault engine, FTS, recall context |
| `botex.memory.curator` | `botex.curator` | knowledge-graph curator |
| `botex.memory.embeddings` | `botex.embeddings` | — |
| `botex.memory.obsidian` | `botex.obsidian` | PKM export |
| `botex.providers.providers` | `botex.providers` | registry + adapters facade |
| `botex.providers.analytics` | `botex.analytics` | cache-aware estimates |
| `botex.providers.benchmarks` | `botex.benchmarks` | — |
| `botex.providers.catalog` | `botex.catalog` | TTL catalog |
| `botex.providers.denylist` | `botex.denylist` | — |
| `botex.providers.pricing` | `botex.pricing` | pre-flight guardrail |
| `botex.providers.prompt_cache` | — (new in v2.1.0) | cache policy/flags |
| `botex.providers.rate_limiter` | `botex.rate_limiter` | per-provider throttle |
| `botex.providers.retry` | `botex.retry` | — |
| `botex.providers.wallet` | `botex.wallet` | credits service |
| `botex.providers.http_pool` | — (new in v2.1.0) | pooled httpx2 clients |
| `botex.providers.adapters.zai` | — (new in v2.1.0) | Z.ai GLM Coding Plan |
| `botex.server.chat` | `botex.chat` | chat pipeline |
| `botex.server.config` | `botex.config` | **containment lives here** (`WorkspaceOutOfBounds`, `allowed_workspaces`) |
| `botex.server.i18n` | `botex.i18n` | — |
| `botex.server.plugins` | `botex.plugins` | ZeroTrust subprocess runner |
| `botex.server.server_cloud` | `botex.server_cloud` | FastAPI gateway (`botex-cloud.service`) |
| `botex.server.ui` | `botex.ui` | — |
| `botex.skills.recipes` | `botex.recipes` | recipe prompts |
| `botex.skills.skills` | `botex.skills` | skill vault |
| `botex.tools.file_tools` | `botex.file_tools` | — |
| `botex.tools.net_tools` | `botex.net_tools` | **SSRF guards live here** |
| `botex.tools.patch_engine` | `botex.patch_engine` | — |
| `botex.tools.security` | `botex.security` | masking, taint severing |
| `botex.tools.exec_tools` | `botex.exec_tools` | — |
| `botex.tools.vision` | — (new in v2.1.0) | vision triage |
| `botex.ops.curator` | `ops.curator` (**removed** — PyPI `ops` collision) | recall cascade + curator service; moved v2.1.0 fix-pass |

## Config keys with mandatory consumers

Arena lesson (enforce-or-delete): a config key without a code consumer is a dead
control, not documentation. Current guarded pairs:

- `cloud.allowed_workspaces` → consumed by `botex.server.config::allowed_workspaces` / `resolve_workspace` (raises `WorkspaceOutOfBounds`)
- `rate_limits.recall_rpm` → consumed by `server.py::_recall_rate_gate` (token bucket)

`tests/test_config_keys_enforced.py` fails CI if a guarded key loses its consumer.

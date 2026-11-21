# MCP Client Bridge (`botex mcp-bridge`)

Multiplexing stdio JSON-RPC bridge to BoteX Cloud. Replaces the legacy
unversioned `~/.ai/botex_mcp/server.py` with a pool of concurrent SSE
sessions plus a stateless `/mcp/http` read path.

## Architecture

```
MCP client (IDE/agent)
        │  stdin/stdout, NDJSON JSON-RPC
        ▼
botex mcp-bridge  (botex/mcp_bridge/__main__.py)
        │  dispatch loop — every request runs in its own task
        ▼
McpBridgeClient   (botex/mcp_bridge/client.py)
        │  least-loaded slot pick
        ├─► SSE slot 0 ─► POST https://<cloud>/mcp/sse  (ClientSession, owner task)
        ├─► SSE slot 1 ─► POST https://<cloud>/mcp/sse
        ├─► SSE slot N ─► POST https://<cloud>/mcp/sse
        └─► /mcp/http    ─► stateless streamable-http per call (read-class tools)
```

- **Pool of N sessions** (`BOTEX_MCP_POOL_SIZE`, default 3): each slot holds one
  `ClientSession` over `sse_client` inside a single owner task, so anyio
  cancel scopes stay task-local. A long `run_subagent` occupies one session
  and never blocks `check_health` on another.
- **Least-loaded dispatch**: calls land on the slot with fewest in-flight
  requests — the old single-session serializer is gone.
- **Retry semantics** preserved from the legacy bridge: idempotent tools may
  retry after send; mutating calls retry **once** and only on a transport
  error — a `TimeoutError` is never retried for mutating calls (the write may
  have succeeded).
- **`/mcp/http` fast path** (`BOTEX_MCP_HTTP_READS=1`, default on): read-class
  tools (`check_health`, `recall_memory`, `get_task_status`, `list_tasks`)
  take a stateless streamable-http exchange instead of holding an SSE slot;
  a fast-path failure falls back to the SSE pool.
- **Reconnect**: each slot lazily re-acquires with exponential backoff
  (1 s → 30 s cap, `BOTEX` connect retries default 3).
- **Schema cache**: `tools/list` and `resources/list` are primed at startup
  and cached `BOTEX_TOOLS_TTL_S` (default 300 s). A
  `notifications/tools/list_changed` server notification invalidates the
  cache and is relayed downstream.
- **stdio framing is NDJSON** — one JSON-RPC message per line; all logging
  goes to stderr, stdout is the protocol channel only.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `BOTEX_CLOUD_URL` | — (required) | Cloud base URL, e.g. `https://botex.example.com` |
| `BOTEX_ACCESS_TOKEN` | — (required) | Bearer token; **fails fast at startup when missing** |
| `BOTEX_AGENT_ID` | `unknown-agent` | `X-Agent-Id` provenance header |
| `SWARM_HOST` / `BOTEX_HOST` | hostname | `X-Host` provenance header |
| `BOTEX_MCP_POOL_SIZE` | `3` | Concurrent SSE sessions |
| `BOTEX_MCP_HTTP_READS` | `1` | Enable `/mcp/http` read fast path |
| `BOTEX_TOOLS_TTL_S` | `300` | `tools/list`/`resources/list` cache TTL |
| `BOTEX_CALL_TIMEOUT_S` | `600` | Per-call timeout |
| `BOTEX_LIST_TIMEOUT_S` | `30` | Schema-list timeout |
| `BOTEX_MCP_ENV` | — | Path to a local `.env` holding `BOTEX_CLOUD_URL`/`BOTEX_ACCESS_TOKEN` — keeps secrets out of the client registry |
| `BOTEX_DISABLE_DOTENV` | `0` | Set `1` to skip `.env` loading entirely |

Secrets resolution order: process env wins; then `BOTEX_MCP_ENV` (explicit
file); otherwise standard upward `.env` discovery from the spawned cwd.
When spawned via `server.py mcp-bridge`, `botex.server.config` already loads
`<repo>/.env`, so a token placed there is picked up with no extra setup.

## Toolport / registry entry

```json
"botex": {
  "command": "botex",
  "args": ["mcp-bridge"],
  "env": {
    "BOTEX_MCP_ENV": "C:\\Users\\<user>\\.ai\\botex_mcp\\.env",
    "BOTEX_AGENT_ID": "<agent>",
    "BOTEX_MCP_POOL_SIZE": "3"
  }
}
```

`BOTEX_MCP_ENV` stores only a **path** — the registry never sees the token.
Inline env (`BOTEX_CLOUD_URL`/`BOTEX_ACCESS_TOKEN` directly in `env`) still
works when a secrets-capable client manages the entry.

Each agent client spawns its own bridge process, so `X-Agent-Id`/`X-Host`
provenance and session pools are per-agent — no shared serialized pipe.

## Peripheral host manifest

A host that only needs Cloud access installs the package once and registers
**only** the bridge — no local `botex` harness entry:

```powershell
uv tool install git+ssh://git@github.com/kubainter/botex-harness.git
botex skills export botex --target ai   # caller skill for agents
# registry: "botex" -> botex mcp-bridge  (+ env above)
```

Dev-host alternative: run from the checkout
(`python -u <repo>\server.py mcp-bridge`) so the bridge always runs the
working-tree code — the same trade-off as `botex-local` today.

## Migration from `~/.ai/botex_mcp/server.py`

1. Point the registry entry at `botex mcp-bridge` (or
   `<repo>\.venv\Scripts\botex.exe mcp-bridge`) with the same env vars.
2. Keep `BOTEX_AGENT_ID`/`BOTEX_HOST` identical so Memory Vault provenance
   stays continuous.
3. Retire `~/.ai/botex_mcp/` once the new entry answers `check_health`.

## Notes

- `initialize`/`ping` are answered locally; `notifications/*` need no reply.
- The bridge never prints to stdout — any stray output would corrupt the
  JSON-RPC stream.
- Cloud accepts many concurrent SSE sessions already; the previous
  serialization was purely client-side (one shared pipe).

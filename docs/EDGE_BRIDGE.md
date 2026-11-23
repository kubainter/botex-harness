# BoteX Cloud-Edge Bridge (`botex edge`)

Guide for pairing a local workstation with **BoteX Cloud** over an outbound compressed WebSocket tunnel (`/api/edge/ws`), without opening inbound ports on the workstation or syncing provider keys locally.

## 0. Quick Workstation Onboarding (`uv tool` + Cloud MCP + Caller Skill)

Use this flow on any new machine that connects a local IDE/agent to an existing **BoteX Cloud** server (no local `server.py` checkout required):

1. **Install the CLI package (`botex`):**
   ```bash
   uv tool install git+https://github.com/kubainter/botex-harness.git
   ```
   *(Run `botex setup` at any time to print the interactive setup checklist.)*

2. **Export the caller-facing `botex` skill (`SKILL.md` + `references/`):**
   ```bash
   botex skills export --target ai
   ```
   Presets: `ai` (`~/.ai/skills`), `claude` (`~/.claude/skills`), `gemini` (`~/.gemini/skills`), `devin` (`~/.config/devin/skills`), or pass any custom directory path (`--force` to overwrite). MCP clients can also read the skill directly via `botex://skills/botex`.

3. **Configure Cloud MCP in your IDE / agent:**
   - **MCP SSE URL:** `https://<cloud-host>/mcp/sse` (or Streamable HTTP: `https://<cloud-host>/mcp/http`)
   - **HTTP Headers:**
     - `Authorization: Bearer <BOTEX_ACCESS_TOKEN>`
     - `X-Agent-Id: <agent-name>` *(optional session-level fallback, e.g. `antigravity`, `claude`, `cursor`, `devin`, `zcode`)*
     - `X-Host: <device-name>` *(optional, used for Cloud Memory Vault & BlackBox provenance)*
   - **Per-Call Provenance (Shared Bridges / Toolport):** When multiple local agents share one SSE bridge session, pass `origin="<agent>@<host>"` per tool call (`start_task`, `run_subagent`, `save_memory`, `patch_memory_section`, `botex_report_violation`). Reserved identity claims (`human`, `human-curated`, `operator`, `curator`) are rejected and fall back to the session headers.

4. **Issue a one-time Edge device token & enroll it locally (recommended):**
   Call `POST https://<cloud-host>/api/edge/tokens` (see Section 2) and store the returned `btxd_...` token with `botex edge enroll`:
   ```bash
   botex edge enroll --device-id <device-id> --workspace-id <workspace-id>
   ```
   Paste (or pipe) the token when prompted. Credentials live in the isolated cross-platform store `~/.botex/credentials/edge/` (override with `BOTEX_CREDENTIALS_DIR` for multi-instance setups or containers); raw tokens never touch the project `.env`. Pass the server per-run with `--server wss://<cloud-host>/api/edge/ws` or `BOTEX_EDGE_SERVER`.

5. **Start the Edge bridge in the project directory:**
   ```bash
   botex edge -w .
   ```
   *(Add `--allow-exec` / `--allow-destructive` only if Cloud tasks are permitted to run commands or delete/move files.)*

6. **Target the workstation from Cloud MCP tools (`start_task` / `run_subagent`):**
   Pass `workspace_dir="edge://<device-id>/<workspace-id>"`.

## Architecture Overview

- **Cloud Brain (`botex/server/server_cloud.py` + `botex/core/engine.py`)** runs on the VPS (`--workers 1`) behind Caddy TLS (`deploy/oracle-vps/Caddyfile.template`), terminating HTTPS/WSS on `:443` and forwarding to `127.0.0.1:8000`.
- **Edge Runner (`botex edge` in `server.py` + `botex/edge/runner.py`)** opens an outbound `wss://<host>/api/edge/ws` connection (`permessage-deflate`, 30s heartbeat), enforces `ROOT_JAIL` (`resolve_safe_path`), masks secrets before compression, and executes workspace tool calls with `base_sha256` optimistic concurrency control.
- **Per-Device Tokens (`botex/core/device_tokens.py`)** use prefix `btxd_<jti>.<secret>`; only the SHA-256 digest is persisted in `.botex/device_tokens.json` on the Cloud host.

## 1. One-Time VPS Setup (Caddy & Firewall)

CI/CD (`.github/workflows/deploy.yml`) updates `botex-cloud.service` and restarts the gateway on deploy, but does **not** overwrite `/etc/caddy/Caddyfile` or `iptables` on every deploy. Perform these once on the VPS:

1. Apply `deploy/oracle-vps/Caddyfile.template` to `/etc/caddy/Caddyfile` (adds `write 3600s` timeout, `25MB` request body limit, and `flush_interval -1` for `/api/edge/ws` and SSE streams):
   ```bash
   sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
   sudo cp /opt/botex/deploy/oracle-vps/Caddyfile.template /etc/caddy/Caddyfile
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
2. Close raw port `8000/tcp` from external interfaces and persist firewall rules (`netfilter-persistent save`), keeping `22`, `80`, and `443` open.

## 2. Issuing & Managing Device Tokens

Device tokens are issued via `POST /api/edge/tokens` (authenticated with the Cloud `Authorization: Bearer <BOTEX_ACCESS_TOKEN>` header) or `DeviceTokenStore.issue_token(...)`:

```json
{
  "device_id": "<device_id>",
  "workspace_id": "<workspace_id>",
  "scopes": [
    "task_pull:<workspace_id>",
    "result_push:<workspace_id>",
    "delegate_local:<workspace_id>"
  ],
  "ttl_s": 2592000,
  "label": "Workstation"
}
```

- **Default scopes** (when `scopes` is omitted): `["task_pull:<ws>", "result_push:<ws>"]`.
- **Default TTL:** 30 days (`2592000` seconds).
- **One-time secret:** The raw `btxd_...` token is returned once at creation. Enroll it in the isolated local credential store via `botex edge enroll` (stored under `~/.botex/credentials/edge/`, `BOTEX_CREDENTIALS_DIR` override) — see Section 3. CI/service accounts can instead pass `--token-file` or set `BOTEX_EDGE_TOKEN` per-process.
- **Inspect / Revoke:**
  - `GET /api/edge/tokens` — list issued tokens (hashes are never returned).
  - `DELETE /api/edge/tokens/{jti}` — revoke by JTI and immediately disconnect matching active WSS sessions.
  - `GET /api/edge/devices` — list currently connected edge devices.

## 3. Running the Local Edge Runner

From the target workspace directory, after enrolling a token (Section 2) with:

```bash
botex edge enroll --device-id <device-id> --workspace-id <workspace-id>
```

The runner resolves credentials in order: `--token`, `--token-file`, explicit process `BOTEX_EDGE_TOKEN`, the isolated store (`~/.botex/credentials/edge/`, `BOTEX_CREDENTIALS_DIR` override), then any legacy `.env` `BOTEX_EDGE_TOKEN` fallback. Manage stored credentials with:

- `botex edge tokens list` — show stored credentials (`device_id`, `workspace_id`, `jti`, `updated_at`; raw tokens are never displayed).
- `botex edge tokens remove --device-id <id> --workspace-id <id>` — delete a stored credential.

CI/service overrides: `BOTEX_EDGE_TOKEN` (env or `.env`) or `--token-file` still work unchanged. Optionally set `BOTEX_EDGE_SERVER`, `BOTEX_EDGE_DEVICE_ID`, `BOTEX_EDGE_WORKSPACE_ID` in your environment:

```sh
./bin/botex-edge.sh
```

Or in PowerShell:

```powershell
.\bin\botex-edge.ps1
```

`botex edge` automatically loads `.env` from `BOTEX_ENV_FILE`, `./.env`, or the repo `.env` (without overriding variables already set in the environment). You can also pass explicit flags:

```powershell
python server.py edge `
  --server wss://<cloud-domain>/api/edge/ws `
  -w . `
  --workspace-id <workspace_id> `
  --device-id <device_id>
```

Optional local capability flags:
- `--allow-exec` — permit `run_command` (still subject to local `exec` policy).
- `--allow-destructive` — permit `delete_file` and `move_file`.

Note: Only **one** `botex edge` process should be active per `(device_id, workspace_id)` pair; a second connection supersedes the previous one (`1000 Superseded`).

To dispatch an MCP task from BoteX Cloud onto the connected workstation, pass `workspace_dir="edge://<device_id>/<workspace_id>"` (or `"<workspace_id>"`) to `start_task` / `run_subagent`.

## 4. Optional: PWA Skill Delegation (`delegate_local`)

`botex/edge/delegation.py` enables invoking pre-approved local skills from the PWA (`POST /api/edge/delegate`). It is **disabled by default (`enabled: false`)** and fail-closed:

- Requires the `delegate_local:<workspace_id>` scope on the device token.
- Requires explicit opt-in in `<workspace>/.botex/edge_caps.json` (or `~/.botex/edge_caps.json`) under `"delegation": {"enabled": true, "allowed_skills": {...}}` with each skill pinned by its 64-hex `sha256`.
- Shell/execution parameter keys (`command`, `cmd`, `shell`, `script`, `code`, `eval`, `exec`, `argv`, `args`, `powershell`, `pwsh`, `bash`) are rejected recursively, and mutating skills require `ApprovalManager` confirmation.

## 5. Harness Inventory Discovery & Typed Control Lane (Plan 042)

### Harness Inventory (`hello` & `heartbeat`)

`EdgeRunner` reports non-blocking local harness discovery results on `hello` and periodic `heartbeat` frames:

```json
{
  "type": "heartbeat",
  "device_id": "workstation-1",
  "workspace_id": "harness",
  "last_seen": "2026-11-23T14:40:00+00:00",
  "harnesses": [
    {
      "harness_id": "devin",
      "transport": "acp",
      "status": "online",
      "last_seen": "2026-11-23T14:40:00+00:00",
      "capabilities": {"available": true}
    }
  ]
}
```

- Discovery probes only allowlisted CLI binaries on `PATH` and configured local ACP endpoints with strict timeouts; no `.env` files or raw filesystem paths are emitted.
- BoteX Cloud upserts each record into `host_harness_inventory` and evaluates TTL freshness on read (`stale_after_s`, default `120s`): records older than the TTL are surfaced with `status: "stale"` without losing the row.

### Typed WSS & Loopback Control Lane

Downward control frames on `/api/edge/ws` and `--edge-daemon` loopback `POST /control` are strictly allowlisted:
- Loopback control allowlist: `lease_heartbeat`, `fence_ack`, `ping`, `harness_pause`, `harness_resume`, `harness_cancel_task`.
- Downward typed harness frames (`harness_pause`, `harness_resume`, `harness_cancel_task`) MUST carry `order_id` and `fencing_token` (verified via constant-time comparison against the locally registered active order; optional `fencing_epoch` must not be stale).
- `EdgeRunner` executes the corresponding OS process-tree helper (`pause_process_tree`, `resume_process_tree`, `cancel_process_tree`) and responds on the Control Lane with explicit `control_ack` or `control_nack` (`reason`, `ok`, `request_id`, `order_id`). Arbitrary or untyped commands are always rejected.

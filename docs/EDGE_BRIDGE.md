# BoteX Cloud-Edge Bridge (`botex edge`)

Guide for pairing a local workstation with **BoteX Cloud** over an outbound compressed WebSocket tunnel (`/api/edge/ws`), without opening inbound ports on the workstation or syncing provider keys locally.

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
- **One-time secret:** The raw `btxd_...` token is returned once at creation. Store it in a protected file and pass it via `--token-file` (preferred over `--token` to avoid process-list exposure).
- **Inspect / Revoke:**
  - `GET /api/edge/tokens` — list issued tokens (hashes are never returned).
  - `DELETE /api/edge/tokens/{jti}` — revoke by JTI and immediately disconnect matching active WSS sessions.
  - `GET /api/edge/devices` — list currently connected edge devices.

## 3. Running the Local Edge Runner

From the target workspace directory:

```powershell
python server.py edge `
  --server wss://<cloud-domain>/api/edge/ws `
  -w . `
  --workspace-id <workspace_id> `
  --device-id <device_id> `
  --token-file <path-to-token-file>
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

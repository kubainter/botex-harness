# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""stdio entry point: NDJSON JSON-RPC <-> McpBridgeClient.

Reads one JSON-RPC message per stdin line, dispatches each request as its
own task (concurrent calls multiplex across the SSE pool), writes one JSON
response per stdout line. All logs go to stderr — stdout is the protocol
channel.
"""

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .client import DEFAULT_POOL_SIZE, McpBridgeClient

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _load_env_file() -> None:
    """Load bridge secrets from a local .env instead of client/registry env.

    BOTEX_MCP_ENV points at an explicit file (registry stores only the path,
    never the token); otherwise checks cwd upward and the repo root .env.
    Already-set env vars win (override=False) and BOTEX_DISABLE_DOTENV opts
    out entirely, matching botex.server.config.
    """
    if os.environ.get("BOTEX_DISABLE_DOTENV", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    if load_dotenv is None:
        return
    explicit = os.environ.get("BOTEX_MCP_ENV", "").strip()
    if explicit:
        load_dotenv(explicit, override=False)
    else:
        load_dotenv(override=False)
        repo_env = Path(__file__).resolve().parents[2] / ".env"
        if repo_env.exists():
            load_dotenv(dotenv_path=repo_env, override=False)


if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_write_lock = threading.Lock()
# Grace window for in-flight tool calls when stdin hits EOF — long calls get
# a bounded drain before the process exits instead of a hard cancel.
DRAIN_TIMEOUT_S = float(os.environ.get("BOTEX_MCP_DRAIN_S", "10"))


def _write(msg: Dict[str, Any]) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _reply(msg_id: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
    resp: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _write(resp)


async def _dispatch(bridge: McpBridgeClient, req: Dict[str, Any]) -> None:
    method = req.get("method", "")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        _reply(
            msg_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "botex-mcp-bridge", "version": "2.1.0"},
            },
        )
    elif method.startswith("notifications/"):
        return  # client->server notifications need no reply
    elif method == "ping":
        _reply(msg_id, {})
    elif method == "tools/list":
        _reply(msg_id, {"tools": await bridge.list_tools()})
    elif method == "resources/list":
        _reply(msg_id, {"resources": await bridge.list_resources()})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        text, is_err = await bridge.call_tool(tool_name, tool_args)
        _reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_err})
    elif msg_id is not None:
        _reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


async def _stdin_reader(
    queue: "asyncio.Queue[Optional[dict]]", loop: asyncio.AbstractEventLoop
) -> None:
    def _read_lines() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, json.loads(line))
            except json.JSONDecodeError:
                continue
        loop.call_soon_threadsafe(queue.put_nowait, None)

    await asyncio.to_thread(_read_lines)


async def _run(bridge: McpBridgeClient) -> None:
    queue: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
    bridge.on_client_notification = lambda note: _write(note)
    reader = asyncio.create_task(_stdin_reader(queue, asyncio.get_running_loop()))
    tasks: set = set()
    try:
        while True:
            req = await queue.get()
            if req is None:  # stdin EOF
                break
            t = asyncio.create_task(_dispatch(bridge, req))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    finally:
        reader.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT_S)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await bridge.stop()


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="botex-mcp-bridge")
    parser.add_argument(
        "--pool-size",
        type=int,
        default=0,
        help="Concurrent SSE sessions (default BOTEX_MCP_POOL_SIZE or 3)",
    )
    args = parser.parse_args(argv)

    _load_env_file()
    cloud_url = (os.environ.get("BOTEX_CLOUD_URL") or "").rstrip("/")
    access_token = (os.environ.get("BOTEX_ACCESS_TOKEN") or "").strip()
    if not cloud_url:
        print("Error: BOTEX_CLOUD_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)
    if not access_token:
        print("Error: BOTEX_ACCESS_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    pool_size = args.pool_size or int(os.environ.get("BOTEX_MCP_POOL_SIZE", DEFAULT_POOL_SIZE))
    agent_id = (os.environ.get("BOTEX_AGENT_ID") or "unknown-agent").strip()
    host_id = (
        os.environ.get("SWARM_HOST")
        or os.environ.get("BOTEX_HOST")
        or socket.gethostname()
        or "unknown"
    )

    bridge = McpBridgeClient(pool_size, cloud_url, access_token, agent_id, host_id)
    try:
        asyncio.run(_run_startup(bridge))
    except KeyboardInterrupt:
        pass


async def _run_startup(bridge: McpBridgeClient) -> None:
    async def _start_bg() -> None:
        try:
            await bridge.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("background bridge startup failed: %s", e)

    # Start SSE pool & cache priming in the background so stdin `initialize`
    # is answered immediately (<50ms) even if the Cloud handshake is slow.
    start_task = asyncio.create_task(_start_bg())
    try:
        await _run(bridge)
    finally:
        if not start_task.done():
            start_task.cancel()
            try:
                await start_task
            except BaseException:
                pass


if __name__ == "__main__":
    main()

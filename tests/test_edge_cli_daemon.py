# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for edge CLI: multi-workspace subcommands, tokens upgrade, --edge-daemon wiring."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import botex.edge
from botex.edge.credentials import extract_token_jti, load_edge_token, save_edge_token
from botex.edge.workspaces import (
    add_workspace,
    load_workspaces_registry,
    set_workspace_state,
)

import server

DEV = "dev-01"
WS_A = "ws-alpha"
TOK_OLD = "btxd_aaaa1111bbbb2222.secretA_xxxxxxxxxxxxxxxx"
TOK_NEW = "btxd_eeee5555ffff6666.secretE_zzzzzzzzzzzzzzzz"
CLOUD_BEARER = "cloud-bearer-token"


class _FakeLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeLegacyRunner:
    instances: ClassVar[list[Any]] = []
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(
        self,
        server_url: str,
        token: str = "",
        device_id: str = "",
        workspace_id: str = "",
        workspace_root: str = "",
        caps: Any = None,
    ) -> None:
        self.server = server_url
        self.token = token
        self.device_id = device_id
        self.workspace_id = workspace_id
        self.workspace_root = workspace_root
        self.caps = caps
        type(self).instances.append(self)

    async def start_loopback_ipc(self, port: int = 0) -> None:
        type(self).calls.append(("start", port))

    async def run_forever(self) -> None:
        type(self).calls.append(("run", None))

    async def stop_loopback_ipc(self) -> None:
        type(self).calls.append(("stop", None))

    def stop(self) -> None:
        type(self).calls.append(("stopped", None))


class _FakeHostDaemon:
    instances: ClassVar[list[Any]] = []
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, server_url: str, *, loopback_port: int = 0, **kwargs: Any) -> None:
        self.server_url = server_url
        self.loopback_port = loopback_port
        self.init_kwargs = kwargs
        type(self).instances.append(self)

    async def start(self) -> dict[str, Any]:
        type(self).calls.append(("start", self.loopback_port))
        return {"ok": True, "mode": "spawned", "loopback_port": self.loopback_port}

    async def run_forever(self) -> None:
        type(self).calls.append(("run", None))

    async def stop(self, drain_timeout_s: float = 10.0) -> None:
        type(self).calls.append(("stop", None))


class _InterruptingHostDaemon(_FakeHostDaemon):
    async def run_forever(self) -> None:
        type(self).calls.append(("run", None))
        raise KeyboardInterrupt


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    ws_file = tmp_path / "host_workspaces.json"
    cred_dir = tmp_path / "cred-store"
    monkeypatch.setenv("BOTEX_DISABLE_DOTENV", "1")
    monkeypatch.setenv("BOTEX_WORKSPACES_FILE", str(ws_file))
    monkeypatch.setenv("BOTEX_CREDENTIALS_DIR", str(cred_dir))
    for var in (
        "BOTEX_EDGE_SERVER",
        "BOTEX_EDGE_TOKEN",
        "BOTEX_ACCESS_TOKEN",
        "BOTEX_EDGE_DEVICE_ID",
        "BOTEX_EDGE_WORKSPACE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    _FakeLegacyRunner.instances.clear()
    _FakeLegacyRunner.calls.clear()
    _FakeHostDaemon.instances.clear()
    _FakeHostDaemon.calls.clear()
    return {"ws_file": ws_file, "cred_dir": cred_dir, "tmp": tmp_path}


def _run_cli(argv: list[str]) -> tuple[str, str]:
    out, err_out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_out):
        server._cli_edge(argv)
    return out.getvalue(), err_out.getvalue()


def _run_cli_expect_exit(argv: list[str], code: int) -> tuple[str, str]:
    out, err_out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_out):
        with pytest.raises(SystemExit) as excinfo:
            server._cli_edge(argv)
    assert excinfo.value.code == code
    return out.getvalue(), err_out.getvalue()


def _register_workspace(tmp: Path, name: str, state: str = "enrolled") -> dict[str, Any]:
    proj = tmp / name
    proj.mkdir(exist_ok=True)
    entry = add_workspace(str(proj), label=name)
    if state != "enrolled":
        set_workspace_state(entry["workspace_id"], state)
    return entry


class _FakeUpgradeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeUpgradeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _install_upgrade_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes | None = None,
    exc: Exception | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: Any = None) -> Any:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        if exc is not None:
            raise exc
        return _FakeUpgradeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return captured


# ---------------------------------------------------------------------------
# botex edge workspaces add|list|remove|discover
# ---------------------------------------------------------------------------


def test_workspaces_add_enrolled_prints_opaque_id(isolated_env: dict[str, Path]) -> None:
    proj = isolated_env["tmp"] / "proj-a"
    proj.mkdir()
    out, _ = _run_cli(["workspaces", "add", str(proj), "--label", "Proj A"])
    entries = load_workspaces_registry()["workspaces"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["workspace_id"].startswith("ws_")
    assert entry["state"] == "enrolled"
    assert entry["label"] == "Proj A"
    assert entry["root"] == str(proj.resolve())
    assert entry["workspace_id"] in out
    assert "state=enrolled" in out


def test_workspaces_add_candidate_and_local_admin(isolated_env: dict[str, Path]) -> None:
    proj = isolated_env["tmp"] / "proj-b"
    proj.mkdir()
    out, _ = _run_cli(["workspaces", "add", str(proj), "--candidate", "--local-admin"])
    entry = load_workspaces_registry()["workspaces"][0]
    assert entry["state"] == "candidate"
    assert entry["local_admin"] is True
    assert "state=candidate" in out


def test_workspaces_add_rejects_non_opaque_id(isolated_env: dict[str, Path]) -> None:
    proj = isolated_env["tmp"] / "proj-c"
    proj.mkdir()
    _, err = _run_cli_expect_exit(["workspaces", "add", str(proj), "--workspace-id", "myproj"], 2)
    assert "opaque" in err
    assert load_workspaces_registry()["workspaces"] == []


def test_workspaces_add_rejects_duplicate_root(isolated_env: dict[str, Path]) -> None:
    proj = isolated_env["tmp"] / "proj-d"
    proj.mkdir()
    _run_cli(["workspaces", "add", str(proj)])
    _, err = _run_cli_expect_exit(["workspaces", "add", str(proj)], 2)
    assert "already registered" in err


def test_workspaces_add_rejects_missing_dir(isolated_env: dict[str, Path]) -> None:
    missing = isolated_env["tmp"] / "does-not-exist"
    _, err = _run_cli_expect_exit(["workspaces", "add", str(missing)], 2)
    assert "Cannot add workspace" in err


def test_workspaces_list_hides_disabled_unless_flag(isolated_env: dict[str, Path]) -> None:
    tmp = isolated_env["tmp"]
    proj_a = tmp / "ws-proj-a"
    proj_a.mkdir()
    proj_b = tmp / "ws-proj-b"
    proj_b.mkdir()
    _run_cli(["workspaces", "add", str(proj_a), "--label", "Alpha"])
    _run_cli(["workspaces", "add", str(proj_b), "--label", "Beta"])
    entries = load_workspaces_registry()["workspaces"]
    disabled_id = entries[1]["workspace_id"]
    set_workspace_state(disabled_id, "disabled")

    out, _ = _run_cli(["workspaces", "list"])
    assert "WORKSPACE_ID" in out and "SOURCE" in out
    assert "Alpha" in out
    assert disabled_id not in out and "Beta" not in out

    out2, _ = _run_cli(["workspaces", "list", "--include-disabled"])
    assert disabled_id in out2 and "disabled" in out2


def test_workspaces_remove_then_missing(isolated_env: dict[str, Path]) -> None:
    proj = isolated_env["tmp"] / "ws-rm"
    proj.mkdir()
    _run_cli(["workspaces", "add", str(proj)])
    ws_id = load_workspaces_registry()["workspaces"][0]["workspace_id"]

    out, _ = _run_cli(["workspaces", "remove", "--workspace-id", ws_id])
    assert f"Removed workspace {ws_id}" in out
    assert load_workspaces_registry()["workspaces"] == []

    out2, _ = _run_cli(["workspaces", "remove", "--workspace-id", ws_id])
    assert "No workspace registered" in out2


def test_workspaces_discover_registers_candidates(isolated_env: dict[str, Path]) -> None:
    root = isolated_env["tmp"] / "scan"
    proj_a = root / "alpha"
    (proj_a / ".git").mkdir(parents=True)
    proj_b = root / "beta"
    proj_b.mkdir()
    (proj_b / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    out, _ = _run_cli(["workspaces", "discover", str(root)])
    entries = load_workspaces_registry()["workspaces"]
    assert len(entries) == 2
    assert all(e["state"] == "candidate" and e["source"] == "discover" for e in entries)
    for entry in entries:
        assert entry["workspace_id"] in out

    out2, _ = _run_cli(["workspaces", "discover", str(root)])
    assert "No new candidate workspaces discovered." in out2
    assert len(load_workspaces_registry()["workspaces"]) == 2


def test_workspaces_discover_defaults_to_cwd(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = isolated_env["tmp"]
    proj = tmp / "cwd-proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp)

    _run_cli(["workspaces", "discover"])
    entries = load_workspaces_registry()["workspaces"]
    assert len(entries) == 1
    assert entries[0]["root"] == str(proj.resolve())


# ---------------------------------------------------------------------------
# botex edge tokens upgrade
# ---------------------------------------------------------------------------


def test_tokens_upgrade_success_rotates_jti(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    save_edge_token(DEV, WS_A, TOK_OLD)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", CLOUD_BEARER)
    monkeypatch.setenv("BOTEX_EDGE_SERVER", "wss://edge.example.com/api/edge/ws")
    captured = _install_upgrade_mock(
        monkeypatch, body=json.dumps({"token": TOK_NEW}).encode("utf-8")
    )

    out, err = _run_cli(
        [
            "tokens",
            "upgrade",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "--label",
            "Workstation",
        ]
    )

    assert captured["url"] == "https://edge.example.com/api/edge/tokens/upgrade"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == f"Bearer {CLOUD_BEARER}"
    assert captured["timeout"] == 30
    assert captured["body"] == {
        "device_id": DEV,
        "workspace_id": WS_A,
        "replace_jti": extract_token_jti(TOK_OLD),
        "label": "Workstation",
    }
    assert load_edge_token(DEV, WS_A) == TOK_NEW
    assert extract_token_jti(TOK_OLD) in out and extract_token_jti(TOK_NEW) in out
    for secret_part in ("secretA", "secretE"):
        assert secret_part not in out
        assert secret_part not in err
    assert TOK_OLD not in out and TOK_NEW not in out


def test_tokens_upgrade_without_stored_token_sends_null_jti(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", CLOUD_BEARER)
    captured = _install_upgrade_mock(
        monkeypatch, body=json.dumps({"token": TOK_NEW}).encode("utf-8")
    )

    _run_cli(
        [
            "tokens",
            "upgrade",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "--cloud-url",
            "wss://edge.example.com/api/edge/ws",
        ]
    )

    assert captured["body"]["replace_jti"] is None
    assert load_edge_token(DEV, WS_A) == TOK_NEW


def test_tokens_upgrade_requires_cloud_auth(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    save_edge_token(DEV, WS_A, TOK_OLD)
    monkeypatch.setenv("BOTEX_EDGE_SERVER", "wss://edge.example.com/api/edge/ws")
    calls: list[Any] = []

    def _forbidden_urlopen(req: Any, timeout: Any = None) -> Any:
        calls.append(req)
        raise AssertionError("urlopen must not run without BOTEX_ACCESS_TOKEN")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_urlopen)

    _, err = _run_cli_expect_exit(
        ["tokens", "upgrade", "--device-id", DEV, "--workspace-id", WS_A], 2
    )
    assert "BOTEX_ACCESS_TOKEN" in err
    assert not calls
    assert load_edge_token(DEV, WS_A) == TOK_OLD


def test_tokens_upgrade_requires_base_url(isolated_env: dict[str, Path]) -> None:
    _, err = _run_cli_expect_exit(
        [
            "tokens",
            "upgrade",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "--cloud-url",
            "not-a-url",
        ],
        2,
    )
    assert "Cannot derive the Cloud base URL" in err


def test_tokens_upgrade_requires_workspace_id(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", CLOUD_BEARER)
    _, err = _run_cli_expect_exit(
        [
            "tokens",
            "upgrade",
            "--device-id",
            DEV,
            "--cloud-url",
            "https://edge.example.com",
        ],
        2,
    )
    assert "--workspace-id" in err


def test_tokens_upgrade_http_error_keeps_old_token(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    save_edge_token(DEV, WS_A, TOK_OLD)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", CLOUD_BEARER)
    monkeypatch.setenv("BOTEX_EDGE_SERVER", "wss://edge.example.com/api/edge/ws")
    http_err = urllib.error.HTTPError(
        "https://edge.example.com/api/edge/tokens/upgrade", 403, "Forbidden", None, None
    )
    _install_upgrade_mock(monkeypatch, exc=http_err)

    _, err = _run_cli_expect_exit(
        ["tokens", "upgrade", "--device-id", DEV, "--workspace-id", WS_A], 1
    )
    assert "HTTP 403" in err
    assert load_edge_token(DEV, WS_A) == TOK_OLD


def test_tokens_upgrade_rejects_invalid_response_token(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    save_edge_token(DEV, WS_A, TOK_OLD)
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", CLOUD_BEARER)
    _install_upgrade_mock(monkeypatch, body=json.dumps({"token": "not-a-token"}).encode())

    _, err = _run_cli_expect_exit(
        [
            "tokens",
            "upgrade",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "--cloud-url",
            "https://edge.example.com",
        ],
        1,
    )
    assert "no valid edge token" in err
    assert load_edge_token(DEV, WS_A) == TOK_OLD


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("wss://host.example.com/api/edge/ws", "https://host.example.com"),
        ("ws://host.example.com/api/edge/ws", "http://host.example.com"),
        ("https://host.example.com", "https://host.example.com"),
        ("http://host.example.com:8443/edge", "http://host.example.com:8443"),
        ("wss://host.example.com:443/api/edge/ws", "https://host.example.com:443"),
        ("wss://user:pass@host.example.com/api/edge/ws", "https://host.example.com"),
        ("wss://[2001:db8::1]/api/edge/ws", "https://[2001:db8::1]"),
        ("", ""),
        ("not-a-url", ""),
        ("ftp://host.example.com/x", ""),
    ],
)
def test_edge_https_base_url_derivation(raw: str, expected: str) -> None:
    assert server._edge_https_base_url(raw) == expected


# ---------------------------------------------------------------------------
# botex edge --edge-daemon wiring (EdgeHostDaemon vs legacy single-workspace)
# ---------------------------------------------------------------------------


def test_edge_daemon_uses_host_daemon_when_enrolled(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_workspace(isolated_env["tmp"], "daemon-ws")
    monkeypatch.setattr(botex.edge, "EdgeRunner", _FakeLegacyRunner)
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)

    out, _ = _run_cli(
        [
            "--edge-daemon",
            "--server",
            "wss://edge.example.com/api/edge/ws",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "-w",
            str(isolated_env["tmp"] / "daemon-ws"),
        ]
    )

    assert len(_FakeHostDaemon.instances) == 1
    assert _FakeHostDaemon.instances[0].server_url == "wss://edge.example.com/api/edge/ws"
    assert _FakeHostDaemon.calls == [("start", 0), ("run", None)]
    assert _FakeLegacyRunner.instances == []
    assert "host daemon" in out


def test_edge_daemon_forwards_loopback_port(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_workspace(isolated_env["tmp"], "daemon-ws-lb")
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)

    _run_cli(
        [
            "--edge-daemon",
            "--loopback-port",
            "7777",
            "--server",
            "wss://edge.example.com/api/edge/ws",
        ]
    )

    assert _FakeHostDaemon.calls[0] == ("start", 7777)


def test_edge_daemon_keyboard_interrupt_is_quiet(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_workspace(isolated_env["tmp"], "daemon-ws-ki")
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _InterruptingHostDaemon, raising=False)

    _run_cli(["--edge-daemon", "--server", "wss://edge.example.com/api/edge/ws"])

    assert _InterruptingHostDaemon.calls == [("start", 0), ("run", None), ("stop", None)]


def test_edge_daemon_falls_back_without_registry(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", "btxd_env111122223333.envSecret_00000000")
    monkeypatch.setattr(botex.edge, "EdgeRunner", _FakeLegacyRunner)
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)
    monkeypatch.setattr(
        "botex.edge.runner.attach_or_spawn_edge_daemon",
        lambda loopback_port=0: {"mode": "attached", "pid": 4242, "port": 9001},
    )

    out, _ = _run_cli(
        [
            "--edge-daemon",
            "--server",
            "wss://edge.example.com/api/edge/ws",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
        ]
    )

    assert "Attached to existing BoteX edge daemon" in out
    assert _FakeHostDaemon.instances == []
    assert _FakeLegacyRunner.instances
    assert _FakeLegacyRunner.instances[0].device_id == DEV
    assert _FakeLegacyRunner.instances[0].workspace_id == WS_A


def test_edge_daemon_falls_back_when_only_candidates(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_workspace(isolated_env["tmp"], "cand-ws", state="candidate")
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", "btxd_env111122223333.envSecret_00000000")
    monkeypatch.setattr(botex.edge, "EdgeRunner", _FakeLegacyRunner)
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)
    monkeypatch.setattr(
        "botex.edge.runner.attach_or_spawn_edge_daemon",
        lambda loopback_port=0: {"mode": "attached", "pid": 4242, "port": 9001},
    )

    out, _ = _run_cli(
        [
            "--edge-daemon",
            "--server",
            "wss://edge.example.com/api/edge/ws",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
        ]
    )

    assert "Attached to existing BoteX edge daemon" in out
    assert _FakeHostDaemon.instances == []


def test_edge_daemon_legacy_spawned_full_flow(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", "btxd_env111122223333.envSecret_00000000")
    monkeypatch.setattr(botex.edge, "EdgeRunner", _FakeLegacyRunner)
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)
    lock = _FakeLock()
    monkeypatch.setattr(
        "botex.edge.runner.attach_or_spawn_edge_daemon",
        lambda loopback_port=0: {"ok": True, "mode": "spawned", "lock": lock},
    )

    _run_cli(
        [
            "--edge-daemon",
            "--server",
            "wss://edge.example.com/api/edge/ws",
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
            "-w",
            str(isolated_env["tmp"]),
        ]
    )

    assert _FakeHostDaemon.instances == []
    runner = _FakeLegacyRunner.instances[0]
    assert runner.server == "wss://edge.example.com/api/edge/ws"
    assert runner.workspace_root == str(isolated_env["tmp"])
    assert _FakeLegacyRunner.calls == [("start", 0), ("run", None), ("stop", None)]
    assert lock.released is True


# ---------------------------------------------------------------------------
# Wave 3 (064/S10): fail-closed singleton lock + graceful SIGTERM drain
# ---------------------------------------------------------------------------


class _DrainRecordingHostDaemon(_FakeHostDaemon):
    stop_kwargs: ClassVar[list[dict[str, Any]]] = []

    async def stop(self, drain_timeout_s: float = 10.0) -> None:
        type(self).stop_kwargs.append({"drain_timeout_s": drain_timeout_s})
        await super().stop(drain_timeout_s=drain_timeout_s)


class _SignalStoppedHostDaemon(_DrainRecordingHostDaemon):
    async def run_forever(self) -> None:
        type(self).calls.append(("run", None))
        # Mimics the SIGTERM/SIGINT handler raising into the running loop.
        raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("lock_contention", "lock_held_by_active_daemon"),
        ("spawn_failed_health", "spawn_failed_health"),
    ],
)
def test_edge_daemon_legacy_fails_closed_without_lock(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, mode: str, error: str
) -> None:
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", "btxd_env111122223333.envSecret_00000000")
    monkeypatch.setattr(botex.edge, "EdgeRunner", _FakeLegacyRunner)
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)
    monkeypatch.setattr(
        "botex.edge.runner.attach_or_spawn_edge_daemon",
        lambda loopback_port=0: {"ok": False, "mode": mode, "error": error},
    )

    argv = [
        "--edge-daemon",
        "--server",
        "wss://edge.example.com/api/edge/ws",
        "--device-id",
        DEV,
        "--workspace-id",
        WS_A,
    ]
    out, err_out = _run_cli_expect_exit(argv, 1)

    assert mode in out + err_out
    # Fail closed: the runner object is constructed earlier, but it must never
    # start loopback IPC or run without the singleton lock held.
    assert not _FakeLegacyRunner.calls


def test_edge_daemon_host_registers_sigterm_and_sigint_handlers(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    registered: dict[Any, Any] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.setdefault(sig, handler))
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FakeHostDaemon, raising=False)
    _register_workspace(isolated_env["tmp"], "daemon-ws-sig")

    out, _ = _run_cli(["--edge-daemon", "--server", "wss://edge.example.com/api/edge/ws"])

    assert hasattr(signal, "SIGINT")
    assert signal.SIGINT in registered
    if hasattr(signal, "SIGTERM"):
        assert signal.SIGTERM in registered
    for handler in registered.values():
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    assert "host daemon" in out


def test_edge_daemon_sigint_drains_with_timeout(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _DrainRecordingHostDaemon.stop_kwargs.clear()
    _register_workspace(isolated_env["tmp"], "daemon-ws-drain")
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _SignalStoppedHostDaemon, raising=False)

    _run_cli(["--edge-daemon", "--server", "wss://edge.example.com/api/edge/ws"])

    assert _DrainRecordingHostDaemon.stop_kwargs == [{"drain_timeout_s": 10.0}]
    assert _FakeHostDaemon.calls[:3] == [("start", 0), ("run", None), ("stop", None)]


def test_cli_edge_host_daemon_restores_signals_and_exits_nonzero_on_start_failure(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    class _FailStartDaemon(_FakeHostDaemon):
        async def start(self) -> dict[str, Any]:
            _FakeHostDaemon.calls.append(("start", 0))
            return {"ok": False, "mode": "error", "error": "lock_contention"}

    before_sigint = signal.getsignal(signal.SIGINT)
    _register_workspace(isolated_env["tmp"], "daemon-ws-fail")
    monkeypatch.setattr("botex.edge.runner.EdgeHostDaemon", _FailStartDaemon, raising=False)

    with pytest.raises(SystemExit) as exc:
        _run_cli(["--edge-daemon", "--server", "wss://edge.example.com/api/edge/ws"])
    assert exc.value.code == 1
    assert signal.getsignal(signal.SIGINT) == before_sigint

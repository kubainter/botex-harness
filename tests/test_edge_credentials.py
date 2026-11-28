# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for the cross-platform isolated Edge credential store."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from botex.edge.credentials import (
    delete_edge_token,
    edge_token_path,
    extract_token_jti,
    get_edge_credentials_dir,
    list_edge_credentials,
    load_edge_token,
    resolve_edge_token,
    save_edge_token,
    validate_credential_id,
)

DEV = "dev-01"
WS_A = "ws-alpha"
WS_B = "ws-beta"
TOK_A = "btxd_aaaa1111bbbb2222.secretA_xxxxxxxxxxxxxxxx"
TOK_B = "btxd_cccc3333dddd4444.secretC_yyyyyyyyyyyyyyyy"
TOK_B2 = "btxd_eeee5555ffff6666.secretE_zzzzzzzzzzzzzzzz"
TOK_CLI = "btxd_1111222233334444.cliSecret_00000000000"
TOK_ENV = "btxd_5555666677778888.envSecret_99999999999"


@pytest.fixture
def cred_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BOTEX_CREDENTIALS_DIR", raising=False)
    return tmp_path / "cred-store"


def test_validate_credential_id_accepts_normal_values():
    assert validate_credential_id("dev-01") == "dev-01"
    assert validate_credential_id("ws.name_1") == "ws.name_1"
    assert validate_credential_id("A.B_C-D") == "A.B_C-D"
    assert validate_credential_id("x" * 128) == "x" * 128


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        ".",
        "..",
        "a__b",
        "__x",
        "x__",
        "a/b",
        "a\\b",
        "a:b",
        "a b",
        " a",
        "a ",
        "a\tb",
        "a\x00b",
        "../escape",
        "..\\escape",
        "x" * 129,
        "*",
        "a?b",
        "é",
    ],
)
def test_validate_credential_id_rejects_bad_values(bad: str):
    with pytest.raises(ValueError):
        validate_credential_id(bad, "device_id")


def test_edge_token_path_builds_child_of_store(cred_dir: Path):
    path = edge_token_path(DEV, WS_A, base_dir=cred_dir)
    assert path.name == f"{DEV}__{WS_A}.token"
    assert path.parent == cred_dir.resolve()
    assert path.is_absolute()


@pytest.mark.parametrize("dev,ws", [("../x", WS_A), (DEV, "..\\x"), ("a/b", WS_A), (DEV, "a\\b")])
def test_edge_token_path_rejects_traversal(dev: str, ws: str, cred_dir: Path):
    with pytest.raises(ValueError):
        edge_token_path(dev, ws, base_dir=cred_dir)


def test_extract_token_jti_returns_only_public_prefix():
    assert extract_token_jti(TOK_A) == "aaaa1111bbbb2222"
    secret_part = TOK_A.split(".", 1)[1]
    assert secret_part not in extract_token_jti(TOK_A)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "btxd_", "btxd_noDotSecretless", "nope_a.b", "btxd_a/b.c", "btxd_ .c", ".btxd_a.b"],
)
def test_extract_token_jti_malformed_returns_empty(bad: str):
    assert extract_token_jti(bad) == ""


def test_save_and_load_roundtrip(cred_dir: Path):
    res = save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    assert res["ok"] is True
    assert res["device_id"] == DEV
    assert res["workspace_id"] == WS_A
    assert res["jti"] == "aaaa1111bbbb2222"
    assert Path(res["path"]).is_file()
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == TOK_A


@pytest.mark.parametrize("bad", ["", "   ", "notbtxd_a.b", "btxd_nodot", "x.y", "btxd_"])
def test_save_rejects_invalid_token_shape(bad: str, cred_dir: Path):
    with pytest.raises(ValueError):
        save_edge_token(DEV, WS_A, bad, base_dir=cred_dir)
    assert not cred_dir.exists() or not list(cred_dir.glob("*.token"))


def test_save_result_never_contains_secret(cred_dir: Path):
    res = save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    blob = json.dumps(res)
    assert TOK_A not in blob
    assert TOK_A.split(".", 1)[1] not in blob


def test_load_missing_returns_empty(cred_dir: Path):
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == ""


def test_delete_roundtrip(cred_dir: Path):
    assert delete_edge_token(DEV, WS_A, base_dir=cred_dir) is False
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    assert delete_edge_token(DEV, WS_A, base_dir=cred_dir) is True
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == ""
    assert delete_edge_token(DEV, WS_A, base_dir=cred_dir) is False


def test_multi_workspace_isolation(cred_dir: Path):
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    save_edge_token(DEV, WS_B, TOK_B, base_dir=cred_dir)
    save_edge_token(DEV, WS_B, TOK_B2, base_dir=cred_dir)
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == TOK_A
    assert load_edge_token(DEV, WS_B, base_dir=cred_dir) == TOK_B2


def test_list_edge_credentials_metadata_only(cred_dir: Path):
    save_edge_token(DEV, WS_B, TOK_B, base_dir=cred_dir)
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    rows = list_edge_credentials(base_dir=cred_dir)
    assert [r["device_id"] for r in rows] == [DEV, DEV]
    assert [r["workspace_id"] for r in rows] == [WS_A, WS_B]
    assert rows[0]["jti"] == "aaaa1111bbbb2222"
    assert rows[1]["jti"] == "cccc3333dddd4444"
    for row in rows:
        assert set(row) == {"device_id", "workspace_id", "jti", "updated_at"}
        assert row["updated_at"].endswith("+00:00")
    blob = json.dumps(rows)
    for tok in (TOK_A, TOK_B):
        assert tok not in blob
        assert tok.split(".", 1)[1] not in blob


def test_list_empty_when_dir_missing(cred_dir: Path):
    assert list_edge_credentials(base_dir=cred_dir) == []


def test_list_ignores_foreign_and_garbled_files(cred_dir: Path):
    cred_dir.mkdir(parents=True)
    (cred_dir / "plain.token").write_text("btxd_x.y", encoding="utf-8")
    (cred_dir / "a__b__c.token").write_text("btxd_x.y", encoding="utf-8")
    (cred_dir / "bad id__ws.token").write_text("btxd_x.y", encoding="utf-8")
    (cred_dir / "note.txt").write_text("btxd_x.y", encoding="utf-8")
    (cred_dir / "dev-9__ws-9.token").write_text("garbage", encoding="utf-8")
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    rows = list_edge_credentials(base_dir=cred_dir)
    assert {(r["device_id"], r["workspace_id"]) for r in rows} == {(DEV, WS_A), ("dev-9", "ws-9")}
    garbled = next(r for r in rows if r["device_id"] == "dev-9")
    assert garbled["jti"] == ""


def test_env_credentials_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_dir = tmp_path / "env-creds"
    monkeypatch.setenv("BOTEX_CREDENTIALS_DIR", str(env_dir))
    assert get_edge_credentials_dir() == env_dir
    save_edge_token(DEV, WS_A, TOK_A)
    assert (env_dir / f"{DEV}__{WS_A}.token").is_file()
    explicit = tmp_path / "explicit"
    save_edge_token(DEV, WS_B, TOK_B, base_dir=explicit)
    assert (explicit / f"{DEV}__{WS_B}.token").is_file()
    assert not (env_dir / f"{DEV}__{WS_B}.token").exists()


def test_default_dir_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOTEX_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert get_edge_credentials_dir() == Path(tmp_path) / ".botex" / "credentials" / "edge"


def test_resolve_precedence_full_chain(tmp_path: Path, cred_dir: Path):
    token_file = tmp_path / "tokfile"
    token_file.write_text(f"  {TOK_B}\n", encoding="utf-8")
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)

    tok, src = resolve_edge_token(
        DEV,
        WS_A,
        cli_token=f" {TOK_CLI} ",
        token_file=str(token_file),
        env_token=TOK_ENV,
        base_dir=cred_dir,
    )
    assert (tok, src) == (TOK_CLI, "cli")

    tok, src = resolve_edge_token(
        DEV, WS_A, cli_token=" ", token_file=str(token_file), env_token=TOK_ENV, base_dir=cred_dir
    )
    assert (tok, src) == (TOK_B, "token_file")

    tok, src = resolve_edge_token(DEV, WS_A, env_token=f"{TOK_ENV}\n", base_dir=cred_dir)
    assert (tok, src) == (TOK_ENV, "env")

    tok, src = resolve_edge_token(DEV, WS_A, base_dir=cred_dir)
    assert (tok, src) == (TOK_A, "store")

    tok, src = resolve_edge_token("other-dev", "other-ws", base_dir=cred_dir)
    assert (tok, src) == ("", "")


def test_resolve_token_file_unreadable_raises(tmp_path: Path, cred_dir: Path):
    with pytest.raises(OSError):
        resolve_edge_token(DEV, WS_A, token_file=str(tmp_path / "missing.txt"), base_dir=cred_dir)


def test_resolve_empty_token_file_no_silent_fallback(tmp_path: Path, cred_dir: Path):
    empty_file = tmp_path / "empty.tok"
    empty_file.write_text("  \n", encoding="utf-8")
    tok, src = resolve_edge_token(
        DEV, WS_A, token_file=str(empty_file), env_token=TOK_ENV, base_dir=cred_dir
    )
    assert (tok, src) == ("", "token_file")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_posix_permissions_0700_dir_0600_file(cred_dir: Path):
    res = save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    assert stat.S_IMODE(Path(res["path"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(cred_dir.stat().st_mode) == 0o700


def test_no_tmp_leftovers_after_save(cred_dir: Path):
    save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)
    leftovers = [p.name for p in cred_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_corrupted_non_utf8_token_file_degrades_safely(cred_dir: Path):
    cred_dir.mkdir(parents=True, exist_ok=True)
    bad_file = edge_token_path(DEV, WS_A, base_dir=cred_dir)
    bad_file.write_bytes(b"\xff\xfe\xfa\xfb")
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == ""
    rows = list_edge_credentials(base_dir=cred_dir)
    assert len(rows) == 1
    assert rows[0]["jti"] == ""


def test_cli_edge_enroll_tokens_list_remove_never_leak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import io

    import server as cli_server

    store_dir = tmp_path / "creds"
    monkeypatch.setenv("BOTEX_CREDENTIALS_DIR", str(store_dir))

    tok_file = tmp_path / "tok.txt"
    tok_file.write_text(TOK_A + "\n", encoding="utf-8")

    # 1. enroll via --token-file
    cli_server._cli_edge(
        ["enroll", "--device-id", DEV, "--workspace-id", WS_A, "--token-file", str(tok_file)]
    )
    out = capsys.readouterr()
    assert TOK_A not in out.out and TOK_A not in out.err
    assert "aaaa1111bbbb2222" in out.out

    # 2. enroll second workspace via stdin pipe (never touches WS_A or .env)
    fake_stdin = io.StringIO(TOK_B + "\n")
    monkeypatch.setattr("sys.stdin", fake_stdin)
    cli_server._cli_edge(["enroll", "--device-id", DEV, "--workspace-id", WS_B])
    out2 = capsys.readouterr()
    assert TOK_B not in out2.out and TOK_B not in out2.err
    assert load_edge_token(DEV, WS_A) == TOK_A
    assert load_edge_token(DEV, WS_B) == TOK_B

    # 3. tokens list shows JTI/IDs, never raw secrets
    cli_server._cli_edge(["tokens", "list"])
    out3 = capsys.readouterr()
    assert DEV in out3.out and WS_A in out3.out and WS_B in out3.out
    assert TOK_A not in out3.out and TOK_B not in out3.out

    # 4. tokens remove rejects traversal IDs with exit code 2
    with pytest.raises(SystemExit) as exc_info:
        cli_server._cli_edge(["tokens", "remove", "--device-id", "../evil", "--workspace-id", WS_A])
    assert exc_info.value.code == 2

    # 5. tokens remove deletes only target workspace
    cli_server._cli_edge(["tokens", "remove", "--device-id", DEV, "--workspace-id", WS_A])
    assert load_edge_token(DEV, WS_A) == ""
    assert load_edge_token(DEV, WS_B) == TOK_B


def test_cli_edge_runner_store_beats_stale_dotenv_while_process_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import server as cli_server

    store_dir = tmp_path / "creds"
    ws_dir = tmp_path / "proj"
    ws_dir.mkdir()
    monkeypatch.setenv("BOTEX_CREDENTIALS_DIR", str(store_dir))
    save_edge_token(DEV, WS_A, TOK_A, base_dir=store_dir)

    # Simulate a stale .env entry populated by _load_edge_env_files (not in initial process env)
    monkeypatch.delenv("BOTEX_EDGE_TOKEN", raising=False)
    monkeypatch.setattr(
        cli_server,
        "_load_edge_env_files",
        lambda: os.environ.setdefault("BOTEX_EDGE_TOKEN", TOK_ENV),
    )

    seen_tokens: list[str] = []

    class _FakeRunner:
        def __init__(self, server_url: str, *, token: str = "", **kwargs):
            seen_tokens.append(token)
            self.device_id = kwargs.get("device_id", "")
            self.workspace_id = kwargs.get("workspace_id", "")
            self.run_forever = lambda: None

    monkeypatch.setattr("botex.edge.EdgeRunner", _FakeRunner)
    monkeypatch.setattr("asyncio.run", lambda coro: coro.close())

    cli_server._cli_edge(
        [
            "--server",
            "wss://cloud.example/api/edge/ws",
            "-w",
            str(ws_dir),
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
        ]
    )
    assert seen_tokens[-1] == TOK_A

    # Explicit process env var before _load_edge_env_files overrides the isolated store
    monkeypatch.setenv("BOTEX_EDGE_TOKEN", TOK_B)
    cli_server._cli_edge(
        [
            "--server",
            "wss://cloud.example/api/edge/ws",
            "-w",
            str(ws_dir),
            "--device-id",
            DEV,
            "--workspace-id",
            WS_A,
        ]
    )
    assert seen_tokens[-1] == TOK_B


def test_windows_acl_failure_fails_closed_without_persisting_token(
    cred_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from botex.edge import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "tester")

    class _FailProc:
        returncode = 5
        stderr = "Access is denied."
        stdout = ""

    monkeypatch.setattr(cred_mod.subprocess, "run", lambda *a, **kw: _FailProc())
    with pytest.raises(OSError, match="icacls failed"):
        save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)

    assert not edge_token_path(DEV, WS_A, base_dir=cred_dir).exists()
    assert [p for p in cred_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_windows_acl_opt_out_allows_weak_acl_when_explicit(
    cred_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from botex.edge import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(
        cred_mod.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("icacls")),
    )
    res = save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir, allow_weak_acl=True)
    assert res["ok"] is True
    assert res["weak_acl"] is True
    assert load_edge_token(DEV, WS_A, base_dir=cred_dir) == TOK_A


def test_windows_acl_post_replace_failure_removes_target(
    cred_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from botex.edge import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "tester")
    calls = 0

    def _fail_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        return type(
            "_Res",
            (),
            {"returncode": 0 if calls < 3 else 5, "stderr": "denied", "stdout": ""},
        )()

    monkeypatch.setattr(cred_mod.subprocess, "run", _fail_third)
    with pytest.raises(OSError, match="icacls failed"):
        save_edge_token(DEV, WS_A, TOK_A, base_dir=cred_dir)

    assert not edge_token_path(DEV, WS_A, base_dir=cred_dir).exists()

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for the Cloud-Edge Bridge Phase 1 worker-B slice:

- ``file_sha256`` raw-byte digests returned by read_file / read_file_lines
- ``apply_patch`` optimistic concurrency via ``base_sha256`` under mutation_lock
- the PowerShell (pwsh / powershell / pwsh-preview) -File/-Command exec policy
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from botex.exec_tools import command_policy_error
from botex.file_tools import read_file, read_file_lines
from botex.patch_engine import apply_patch, snapshot_manager


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _purge_snapshot_tasks(*task_ids: str) -> None:
    """Drop persisted snapshot dirs for fixed test task ids (see test_botex)."""
    import shutil

    for tid in task_ids:
        shutil.rmtree(snapshot_manager._task_dir(tid), ignore_errors=True)


_TASK_IDS = (
    "t_sha_omit",
    "t_sha_match",
    "t_conflict",
    "t_nonutf8",
    "t_race_a",
    "t_race_b",
    "t_chain",
    "t_chain_b",
    "t_chain_c",
)


@pytest.fixture(autouse=True)
def _purge_test_snapshots():
    yield
    _purge_snapshot_tasks(*_TASK_IDS)


def test_read_file_returns_raw_byte_sha256(tmp_path: Path):
    root = tmp_path
    raw = "value = 1\r\nprint('x')\r\n".encode("utf-8") + b"\xff\xfe tail"
    (root / "app.txt").write_bytes(raw)

    res = read_file("app.txt", root)
    assert res["ok"] is True
    assert res["file_sha256"] == _sha256(raw)
    assert "value = 1" in res["content"]


def test_read_file_lines_returns_raw_byte_sha256_and_slice(tmp_path: Path):
    root = tmp_path
    raw = b"first\r\nsecond\nthird\xff\n"
    (root / "frag.txt").write_bytes(raw)

    res = read_file_lines("frag.txt", 2, 3, root)
    assert res["ok"] is True
    assert res["file_sha256"] == _sha256(raw)
    assert "2: second" in res["content"]
    assert "3: third" in res["content"]
    assert "1: first" not in res["content"]


def test_read_file_sha256_tracks_crlf_bytes(tmp_path: Path):
    """CRLF vs LF endings hash differently — the digest covers raw disk bytes."""
    root = tmp_path
    (root / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (root / "lf.txt").write_bytes(b"a\nb\n")

    res_crlf = read_file("crlf.txt", root)
    res_lf = read_file("lf.txt", root)
    assert res_crlf["file_sha256"] == _sha256(b"a\r\nb\r\n")
    assert res_lf["file_sha256"] == _sha256(b"a\nb\n")
    assert res_crlf["file_sha256"] != res_lf["file_sha256"]


def test_apply_patch_base_sha256_omitted_and_matching(tmp_path: Path):
    root = tmp_path
    target = root / "state.py"
    target.write_bytes(b"value = 1\n")

    res_none = apply_patch("state.py", "value = 1", "value = 2", root, task_id="t_sha_omit")
    assert res_none["ok"] is True
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    res_match = apply_patch(
        "state.py",
        "value = 2",
        "value = 3",
        root,
        task_id="t_sha_match",
        base_sha256=_sha256(target.read_bytes()),
    )
    assert res_match["ok"] is True
    assert target.read_text(encoding="utf-8") == "value = 3\n"


def test_apply_patch_base_sha256_conflict_preserves_file(tmp_path: Path):
    root = tmp_path
    target = root / "state.py"
    target.write_bytes(b"value = 1\n")
    sha_at_read = _sha256(target.read_bytes())

    # Another worker mutates the file between our read and the patch call.
    target.write_bytes(b"value = 2\n")

    res = apply_patch(
        "state.py",
        "value = 2",
        "value = 99",
        root,
        task_id="t_conflict",
        base_sha256=sha_at_read,
    )
    assert res["ok"] is False
    assert res["status"] == "conflict"
    assert res["current_sha256"] == _sha256(b"value = 2\n")
    assert "value = 2" in res["current_content"]
    assert "Conflict" in res["error"]

    # File untouched and no snapshot registry was created for the reject.
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert not snapshot_manager._task_dir("t_conflict").exists()
    assert snapshot_manager.verify_rollback("t_conflict") == []


def test_apply_patch_conflict_wraps_current_content(tmp_path: Path):
    """Conflict payload carries the on-disk bytes decoded with replacement,
    behind the masked ``untrusted_file_content`` wrapper like read_file."""
    root = tmp_path
    target = root / "blob.dat"
    raw = b"head\xffbinary\r\nblock"
    target.write_bytes(raw)

    res = apply_patch(
        "blob.dat",
        "head",
        "x",
        root,
        task_id="t_nonutf8",
        base_sha256=_sha256(b"stale-bytes"),
    )
    assert res["ok"] is False
    assert res["status"] == "conflict"
    assert res["current_sha256"] == _sha256(raw)
    wrapped = res["current_content"]
    assert wrapped.startswith("<untrusted_file_content")
    assert wrapped.endswith("</untrusted_file_content>")
    assert raw.decode("utf-8", errors="replace")[:8000] in wrapped
    assert target.read_bytes() == raw

    # Secrets inside the drifted content are masked before reaching the model.
    sk_pref = "".join(["s", "k", "-"])
    secret_file = root / "leak.txt"
    secret_file.write_bytes(f"{sk_pref}abcdefghij1234567890abcdefghij\n".encode("utf-8"))
    leak = apply_patch(
        "leak.txt",
        "x",
        "y",
        root,
        task_id="t_nonutf8",
        base_sha256=_sha256(b"stale-bytes"),
    )
    assert leak["status"] == "conflict"
    assert f"{sk_pref}abcdefghij" not in leak["current_content"]


def test_apply_patch_success_sha256_enables_occ_chain(tmp_path: Path):
    """The success payload carries the written-bytes digest, so consecutive
    patches chain without a re-read — and a stale digest still conflicts."""
    root = tmp_path
    target = root / "chain.py"
    target.write_bytes(b"def f():\r\n    return 1\r\n")

    read1 = read_file("chain.py", root)
    assert read1["ok"] is True
    assert read1["file_sha256"] == _sha256(target.read_bytes())

    first = apply_patch(
        "chain.py",
        "return 1",
        "return 2",
        root,
        task_id="t_chain",
        base_sha256=read1["file_sha256"],
    )
    assert first["ok"] is True
    assert first["file_sha256"] == _sha256(target.read_bytes())
    # CRLF style survives the patched write byte-exactly.
    assert b"\r\n" in target.read_bytes()

    second = apply_patch(
        "chain.py",
        "return 2",
        "return 3",
        root,
        task_id="t_chain_b",
        base_sha256=first["file_sha256"],
    )
    assert second["ok"] is True

    # The pre-first-patch digest is stale now: the chain must reject it.
    third = apply_patch(
        "chain.py",
        "return 3",
        "return 4",
        root,
        task_id="t_chain_c",
        base_sha256=read1["file_sha256"],
    )
    assert third["ok"] is False
    assert third["status"] == "conflict"
    assert b"return 3" in target.read_bytes()


def test_apply_patch_base_sha256_serializes_concurrent_writers(tmp_path: Path):
    """Two patches citing the same base digest cannot both win: the mutation
    lock serializes them and the loser sees a conflict, never a silent clobber."""
    root = tmp_path
    target = root / "race.py"
    target.write_text("x = 0\n", encoding="utf-8")
    base = _sha256(target.read_bytes())

    results: list[dict] = []

    def _patch(new_val: int, tid: str) -> None:
        results.append(
            apply_patch(
                "race.py",
                "x = 0",
                f"x = {new_val}",
                root,
                task_id=tid,
                base_sha256=base,
            )
        )

    t1 = threading.Thread(target=_patch, args=(1, "t_race_a"))
    t2 = threading.Thread(target=_patch, args=(2, "t_race_b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [r for r in results if r.get("ok")]
    conflicts = [r for r in results if r.get("status") == "conflict"]
    assert len(oks) == 1
    assert len(conflicts) == 1
    assert target.read_text(encoding="utf-8") in ("x = 1\n", "x = 2\n")


_PWSH_ALLOW = ["pwsh", "powershell", "pwsh-preview", "pytest"]


@pytest.mark.parametrize("binary", ["pwsh", "powershell", "pwsh-preview"])
@pytest.mark.parametrize(
    "cmd",
    [
        "{b} -Command Get-Process",
        '{b} -Command "Get-Process"',
        "{b} -c Get-Process",
        "{b} -co Get-Process",
        "{b} -EncodedCommand ZQB4AGkAdA==",
        "{b} -EncodedCommand=ZQB4AGkAdA==",
        "{b} -enc ZQB4AGkAdA==",
        "{b} -en ZQB4AGkAdA==",
        "{b} -e ZQB4AGkAdA==",
        "{b} -ec ZQB4AGkAdA==",
        "{b} -Command:Get-Process",
        "{b} -COMMAND Get-Process",
        "{b} /c Get-Process",
        "{b} Get-Process",
        "{b} evil.ps1",
        "{b}",
        "{b} -File",
        "{b} -NoProfile",
        "{b} -File ..\\evil.ps1",
        "{b} -File ../evil.ps1",
        "{b} -File sub\\..\\evil.ps1",
        "{b} -File evil.cmd",
        "{b} -F evil.exe",
        "{b} -File:evil.exe",
        "{b} -File x.ps1 -e ZQB4AA==",
        "{b} -File .\\..\\x.ps1",
        '{b} -File good.ps1 -Command "evil"',
        "{b} -File good.ps1 -EncodedCommand ZQB4AA==",
        "{b} -WindowStyle Hidden",
        "{b} -WorkingDirectory sub",
    ],
)
def test_pwsh_policy_denies_inline_and_traversal(binary: str, cmd: str):
    command = cmd.format(b=binary)
    err = command_policy_error(command, allowlist=_PWSH_ALLOW, deny_args=[])
    assert err, f"expected denial for: {command}"


@pytest.mark.parametrize("binary", ["pwsh", "powershell", "pwsh-preview"])
@pytest.mark.parametrize(
    "cmd",
    [
        "{b} -File script.ps1",
        "{b} -File ./scripts/run.ps1",
        "{b} -NoProfile -NonInteractive -File build\\deploy.ps1",
        "{b} -File run.ps1 -NoExit",
        "{b} -File run.ps1 --verbose extra-arg",
        "{b} -f run.ps1",
    ],
)
def test_pwsh_policy_allows_safe_file_invocation(binary: str, cmd: str):
    command = cmd.format(b=binary)
    assert command_policy_error(command, allowlist=_PWSH_ALLOW, deny_args=[]) == ""


def test_pwsh_policy_requires_allowlist_membership():
    """The PowerShell gate only applies after the allowlist admits the binary."""
    err = command_policy_error("pwsh -File x.ps1", allowlist=["pytest"], deny_args=[])
    assert "allowlist" in err


def test_pwsh_policy_binary_name_normalization():
    """Extension-insensitive basename matching covers pwsh.exe / powershell.exe."""
    assert command_policy_error("pwsh.exe -File x.ps1", allowlist=_PWSH_ALLOW, deny_args=[]) == ""
    assert command_policy_error(
        "powershell.exe -Command Get-Process", allowlist=_PWSH_ALLOW, deny_args=[]
    )


def test_pwsh_policy_denies_traversal_after_posix_tokenization(monkeypatch):
    """POSIX shlex eats backslashes ('..\\x.ps1' -> '..x.ps1') before the gate
    sees the token; the '..' guard must still fire under posix=True splitting."""
    monkeypatch.setattr(os, "name", "posix")
    for cmd in (
        r"pwsh -File ..\evil.ps1",
        r"pwsh -File sub\..\evil.ps1",
        r"pwsh -File .\..\x.ps1",
        r'pwsh -File "sub\..\evil.ps1"',
        r"pwsh -File ../evil.ps1",
    ):
        assert command_policy_error(cmd, allowlist=_PWSH_ALLOW, deny_args=[]), cmd

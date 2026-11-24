# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for the host-local Edge workspace registry."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from botex.edge.workspaces import (
    add_workspace,
    discover_candidate_workspaces,
    generate_daemon_device_id,
    generate_workspace_id,
    get_host_workspaces_path,
    get_workspace,
    is_opaque_host_id,
    is_opaque_workspace_id,
    list_workspaces,
    load_workspaces_registry,
    remove_workspace,
    save_workspaces_registry,
    set_workspace_state,
    to_wire_manifest,
    validate_workspace_root_realpath,
)

WS_ROOT_A = str(Path("/tmp/ws-test-a").resolve())
WS_ROOT_B = str(Path("/tmp/ws-test-b").resolve())


@pytest.fixture
def reg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BOTEX_WORKSPACES_FILE", raising=False)
    return tmp_path / "host_workspaces.json"


@pytest.fixture
def tmp_roots(tmp_path: Path) -> tuple[Path, Path]:
    a = tmp_path / "ws-test-a"
    b = tmp_path / "ws-test-b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    return a, b


# ---------------------------------------------------------------------------
# get_host_workspaces_path
# ---------------------------------------------------------------------------


def test_get_host_workspaces_path_explicit(reg_path: Path):
    assert get_host_workspaces_path(reg_path) == reg_path


def test_get_host_workspaces_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "env" / "host_workspaces.json"
    monkeypatch.setenv("BOTEX_WORKSPACES_FILE", str(custom))
    assert get_host_workspaces_path() == custom


def test_get_host_workspaces_path_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOTEX_WORKSPACES_FILE", raising=False)
    assert get_host_workspaces_path() == Path.home() / ".botex" / "host_workspaces.json"


# ---------------------------------------------------------------------------
# Opaque IDs
# ---------------------------------------------------------------------------


def test_generate_workspace_id_format():
    wid = generate_workspace_id()
    assert is_opaque_workspace_id(wid)
    assert wid.startswith("ws_")
    assert len(wid) == 3 + 12  # "ws_" + 12 hex


def test_generate_daemon_device_id_format():
    did = generate_daemon_device_id()
    assert is_opaque_host_id(did)
    assert did.startswith("host_")
    assert len(did) == 5 + 8  # "host_" + 8 hex


@pytest.mark.parametrize(
    "val",
    ["ws_00000000", "ws_ffffffffffff", "ws_a1b2c3d4e5f6", "ws_deadbeefcafe"],
)
def test_is_opaque_workspace_id_accepts(val: str):
    assert is_opaque_workspace_id(val) is True


@pytest.mark.parametrize(
    "val",
    [
        "",
        "ws_",
        "ws_abc",
        "ws_abcdefghij",
        "WS_ABCDEF01",
        "ws_gggggggg",
        "ws_abc def",
        "/tmp/foo",
        "host_00000000",
        None,
        123,
    ],
)
def test_is_opaque_workspace_id_rejects(val):
    assert is_opaque_workspace_id(val) is False


@pytest.mark.parametrize(
    "val",
    ["host_00000000", "host_ffffffff", "host_a1b2c3d4"],
)
def test_is_opaque_host_id_accepts(val: str):
    assert is_opaque_host_id(val) is True


@pytest.mark.parametrize(
    "val",
    ["", "host_", "host_abc", "host_abcdefghi", "HOST_ABCD1234", "ws_00000000", None, 123],
)
def test_is_opaque_host_id_rejects(val):
    assert is_opaque_host_id(val) is False


# ---------------------------------------------------------------------------
# load / save roundtrip
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(reg_path: Path):
    data = {
        "version": 1,
        "device_id": "host_00000001",
        "workspaces": [
            {
                "workspace_id": "ws_00000001",
                "root": "/tmp/ws-test-a",
                "label": "ws-a",
                "state": "enrolled",
                "added_at": "2026-01-01T00:00:00+00:00",
                "source": "manual",
                "local_admin": False,
                "caps_overrides": {},
            }
        ],
    }
    saved = save_workspaces_registry(data, path=reg_path)
    assert saved == reg_path
    assert reg_path.is_file()

    loaded = load_workspaces_registry(path=reg_path)
    assert loaded["device_id"] == "host_00000001"
    assert loaded["version"] == 1
    assert len(loaded["workspaces"]) == 1
    assert loaded["workspaces"][0]["workspace_id"] == "ws_00000001"


def test_save_auto_generates_device_id(reg_path: Path):
    data = {"version": 1, "workspaces": []}
    save_workspaces_registry(data, path=reg_path)
    loaded = load_workspaces_registry(path=reg_path)
    assert is_opaque_host_id(loaded["device_id"])


def test_load_missing_returns_default(reg_path: Path):
    assert not reg_path.exists()
    data = load_workspaces_registry(path=reg_path)
    assert data["version"] == 1
    assert is_opaque_host_id(data["device_id"])
    assert data["workspaces"] == []


def test_load_corrupted_returns_default(reg_path: Path):
    reg_path.write_text("{not valid json", encoding="utf-8")
    data = load_workspaces_registry(path=reg_path)
    assert data["version"] == 1
    assert data["workspaces"] == []


# ---------------------------------------------------------------------------
# Atomic write + permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits not enforced on Windows")
def test_posix_permissions_0600(reg_path: Path):
    save_workspaces_registry(
        {"version": 1, "device_id": "host_00000001", "workspaces": []}, path=reg_path
    )
    mode = stat.S_IMODE(reg_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_no_tmp_leftovers_after_save(reg_path: Path):
    save_workspaces_registry(
        {"version": 1, "device_id": "host_00000001", "workspaces": []}, path=reg_path
    )
    leftovers = [p.name for p in reg_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_returns_path(reg_path: Path):
    result = save_workspaces_registry(
        {"version": 1, "device_id": "host_00000001", "workspaces": []}, path=reg_path
    )
    assert result == reg_path


# ---------------------------------------------------------------------------
# add_workspace
# ---------------------------------------------------------------------------


def test_add_workspace_basic(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), label="ws-a", path=reg_path)
    assert entry["workspace_id"].startswith("ws_")
    assert entry["root"] == str(root_a.resolve())
    assert entry["label"] == "ws-a"
    assert entry["state"] == "enrolled"
    assert entry["source"] == "manual"
    assert entry["local_admin"] is False
    assert entry["caps_overrides"] == {}
    assert "added_at" in entry


def test_add_workspace_rejects_duplicate_root(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    add_workspace(str(root_a), path=reg_path)
    with pytest.raises(ValueError):
        add_workspace(str(root_a), path=reg_path)


def test_add_workspace_rejects_duplicate_workspace_id(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, root_b = tmp_roots
    add_workspace(str(root_a), workspace_id="ws_00000001", path=reg_path)
    with pytest.raises(ValueError):
        add_workspace(str(root_b), workspace_id="ws_00000001", path=reg_path)


def test_add_workspace_daemon_mode_rejects_non_opaque_id(
    reg_path: Path, tmp_roots: tuple[Path, Path]
):
    root_a, _ = tmp_roots
    with pytest.raises(ValueError):
        add_workspace(
            str(root_a), workspace_id="/tmp/derived/path", daemon_mode=True, path=reg_path
        )
    with pytest.raises(ValueError):
        add_workspace(str(root_a), workspace_id="not-opaque", daemon_mode=True, path=reg_path)


def test_add_workspace_daemon_mode_accepts_opaque_id(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), workspace_id="ws_00000001", daemon_mode=True, path=reg_path)
    assert entry["workspace_id"] == "ws_00000001"


def test_add_workspace_non_daemon_mode_accepts_any_id(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(
        str(root_a), workspace_id="my-custom-id", daemon_mode=False, path=reg_path
    )
    assert entry["workspace_id"] == "my-custom-id"


def test_add_workspace_state_normalization(
    reg_path: Path, tmp_roots: tuple[Path, Path], tmp_path: Path
):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), state="DISABLED", path=reg_path)
    assert entry["state"] == "disabled"

    dir_c = tmp_path / "ws-test-c"
    dir_c.mkdir()
    entry2 = add_workspace(str(dir_c.resolve()), state="Enrolled", path=reg_path)
    assert entry2["state"] == "enrolled"

    dir_d = tmp_path / "ws-test-d"
    dir_d.mkdir()
    entry3 = add_workspace(str(dir_d.resolve()), state="garbage", path=reg_path)
    assert entry3["state"] == "candidate"


def test_add_workspace_source_normalization(
    reg_path: Path, tmp_roots: tuple[Path, Path], tmp_path: Path
):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), source="DISCOVER", path=reg_path)
    assert entry["source"] == "discover"

    dir_e = tmp_path / "ws-test-e"
    dir_e.mkdir()
    entry2 = add_workspace(str(dir_e.resolve()), source="weird", path=reg_path)
    assert entry2["source"] == "manual"


def test_add_workspace_nonexistent_root_raises(reg_path: Path):
    with pytest.raises(ValueError):
        add_workspace("/tmp/definitely-does-not-exist-12345", path=reg_path)


def test_add_workspace_file_not_dir_raises(reg_path: Path, tmp_path: Path):
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        add_workspace(str(f), path=reg_path)


# ---------------------------------------------------------------------------
# list_workspaces / get_workspace
# ---------------------------------------------------------------------------


def test_list_and_get_workspace(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, root_b = tmp_roots
    ea = add_workspace(str(root_a), label="ws-a", path=reg_path)
    eb = add_workspace(str(root_b), label="ws-b", state="candidate", path=reg_path)

    all_ws = list_workspaces(path=reg_path)
    assert len(all_ws) == 2

    enabled_only = list_workspaces(include_disabled=False, path=reg_path)
    assert len(enabled_only) == 2

    got = get_workspace(ea["workspace_id"], path=reg_path)
    assert got is not None
    assert got["workspace_id"] == ea["workspace_id"]
    assert got["label"] == "ws-a"

    missing = get_workspace("ws_nonexistent", path=reg_path)
    assert missing is None


def test_list_excludes_disabled(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, root_b = tmp_roots
    add_workspace(str(root_a), label="ws-a", state="enrolled", path=reg_path)
    add_workspace(str(root_b), label="ws-b", state="disabled", path=reg_path)

    all_ws = list_workspaces(path=reg_path)
    assert len(all_ws) == 2

    active = list_workspaces(include_disabled=False, path=reg_path)
    assert len(active) == 1
    assert active[0]["label"] == "ws-a"


# ---------------------------------------------------------------------------
# set_workspace_state
# ---------------------------------------------------------------------------


def test_set_workspace_state(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), path=reg_path)
    wid = entry["workspace_id"]

    updated = set_workspace_state(wid, "disabled", path=reg_path)
    assert updated is not None
    assert updated["state"] == "disabled"

    got = get_workspace(wid, path=reg_path)
    assert got["state"] == "disabled"

    # Normalization
    set_workspace_state(wid, "ENROLLED", path=reg_path)
    assert get_workspace(wid, path=reg_path)["state"] == "enrolled"

    # Missing
    assert set_workspace_state("ws_nonexistent", "enrolled", path=reg_path) is None


# ---------------------------------------------------------------------------
# remove_workspace
# ---------------------------------------------------------------------------


def test_remove_workspace(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), path=reg_path)
    wid = entry["workspace_id"]

    assert remove_workspace(wid, path=reg_path) is True
    assert get_workspace(wid, path=reg_path) is None
    assert remove_workspace(wid, path=reg_path) is False
    assert remove_workspace("ws_nonexistent", path=reg_path) is False


# ---------------------------------------------------------------------------
# discover_candidate_workspaces
# ---------------------------------------------------------------------------


def test_discover_candidate_workspaces_adds_candidates_only(reg_path: Path, tmp_path: Path):
    # Create a project with .git marker
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".git").mkdir()

    # Pre-register a different workspace to verify skip logic
    other = tmp_path / "other"
    other.mkdir()
    add_workspace(str(other), workspace_id="ws_0000cafe", path=reg_path)

    found = discover_candidate_workspaces([str(tmp_path)], path=reg_path)

    assert len(found) == 1
    assert found[0]["state"] == "candidate"
    assert found[0]["source"] == "discover"
    assert found[0]["workspace_id"].startswith("ws_")

    # Verify it was added to the registry as candidate (not auto-enrolled)
    reg = load_workspaces_registry(path=reg_path)
    candidates = [ws for ws in reg["workspaces"] if ws["source"] == "discover"]
    assert len(candidates) == 1
    assert candidates[0]["state"] == "candidate"


def test_discover_skips_already_registered(reg_path: Path, tmp_path: Path):
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".git").mkdir()

    # Pre-register this exact root
    add_workspace(str(proj), workspace_id="ws_0000cafe", path=reg_path)

    found = discover_candidate_workspaces([str(tmp_path)], path=reg_path)
    assert found == []


def test_discover_never_auto_enrolls(reg_path: Path, tmp_path: Path):
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".git").mkdir()

    discover_candidate_workspaces([str(tmp_path)], path=reg_path)

    reg = load_workspaces_registry(path=reg_path)
    for ws in reg["workspaces"]:
        assert ws["state"] == "candidate"
        assert ws["source"] == "discover"


def test_discover_nonexistent_root_skipped(reg_path: Path):
    found = discover_candidate_workspaces(["/tmp/definitely-does-not-exist-99999"], path=reg_path)
    assert found == []


# ---------------------------------------------------------------------------
# validate_workspace_root_realpath (F4)
# ---------------------------------------------------------------------------


def test_validate_workspace_root_realpath_ok(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    result = validate_workspace_root_realpath(str(root_a))
    assert result == Path(str(root_a.resolve()))


def test_validate_workspace_root_realpath_nonexistent_raises(reg_path: Path):
    with pytest.raises(ValueError):
        validate_workspace_root_realpath("/tmp/definitely-does-not-exist-12345")


def test_validate_workspace_root_realpath_file_not_dir_raises(reg_path: Path, tmp_path: Path):
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_workspace_root_realpath(str(f))


def test_validate_workspace_root_realpath_rejects_symlink(reg_path: Path, tmp_path: Path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    try:
        link_dir.symlink_to(real_dir)
        with pytest.raises(ValueError):
            validate_workspace_root_realpath(str(link_dir))
    except OSError:
        pytest.skip("symlink creation not supported")


def test_validate_workspace_root_realpath_rejects_escape(
    reg_path: Path, tmp_roots: tuple[Path, Path]
):
    root_a, _ = tmp_roots
    # target outside root
    outside = tmp_roots[0].parent / "outside"
    with pytest.raises(ValueError):
        validate_workspace_root_realpath(str(root_a), target_path=str(outside))


def test_validate_workspace_root_realpath_accepts_child(
    reg_path: Path, tmp_roots: tuple[Path, Path]
):
    root_a, _ = tmp_roots
    child = root_a / "subdir"
    child.mkdir()
    result = validate_workspace_root_realpath(str(root_a), target_path=str(child))
    assert result == Path(str(root_a.resolve()))
    assert validate_workspace_root_realpath(root_a, target_path="subdir") == result


def test_validate_workspace_root_realpath_rejects_relative_escape(
    reg_path: Path, tmp_roots: tuple[Path, Path]
):
    root_a, _ = tmp_roots
    with pytest.raises(ValueError):
        validate_workspace_root_realpath(root_a, target_path="../outside")


def test_load_registry_drops_non_dict_entries(reg_path: Path):
    reg_path.write_text(
        '{"version": 1, "device_id": "host_00000001", "workspaces": ["bad", null, {"workspace_id": "ws_00000001"}]}',
        encoding="utf-8",
    )
    loaded = load_workspaces_registry(path=reg_path)
    assert len(loaded["workspaces"]) == 1
    assert loaded["workspaces"][0]["workspace_id"] == "ws_00000001"


# ---------------------------------------------------------------------------
# to_wire_manifest zero-path guarantee
# ---------------------------------------------------------------------------


def test_to_wire_manifest_from_registry(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, root_b = tmp_roots
    add_workspace(str(root_a), label="ws-a", path=reg_path)
    add_workspace(str(root_b), label="ws-b", path=reg_path)

    reg = load_workspaces_registry(path=reg_path)
    manifest = to_wire_manifest(reg, path=reg_path)

    assert len(manifest) == 2
    for entry in manifest:
        assert set(entry.keys()) == {"workspace_id", "label", "state"}
        assert "root" not in entry
        assert "caps_overrides" not in entry
        assert "local_admin" not in entry


def test_to_wire_manifest_from_list(reg_path: Path, tmp_roots: tuple[Path, Path]):
    root_a, _ = tmp_roots
    entry = add_workspace(str(root_a), label="ws-a", path=reg_path)

    manifest = to_wire_manifest([entry], path=reg_path)
    assert len(manifest) == 1
    assert set(manifest[0].keys()) == {"workspace_id", "label", "state"}
    assert manifest[0]["workspace_id"] == entry["workspace_id"]


def test_to_wire_manifest_empty():
    assert to_wire_manifest({"version": 1, "device_id": "host_00000001", "workspaces": []}) == []
    assert to_wire_manifest([]) == []
    assert to_wire_manifest(None) == []


def test_to_wire_manifest_skips_invalid_entries():
    data = {
        "version": 1,
        "device_id": "host_00000001",
        "workspaces": [
            {"workspace_id": "ws_00000001", "label": "ok", "state": "enrolled"},
            "not-a-dict",
            {"root": "/tmp/foo"},  # missing workspace_id
        ],
    }
    manifest = to_wire_manifest(data)
    assert len(manifest) == 1
    assert manifest[0]["workspace_id"] == "ws_00000001"

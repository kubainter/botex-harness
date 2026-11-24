# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Host-local workspace registry for Edge daemon multi-workspace mode."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from botex.edge.credentials import _tighten_acl_windows
except ImportError:  # pragma: no cover - standalone fallback

    def _tighten_acl_windows(path: Path) -> None:
        """No-op fallback when credentials module is unavailable."""
        return None


__all__ = [
    "add_workspace",
    "discover_candidate_workspaces",
    "generate_daemon_device_id",
    "generate_workspace_id",
    "get_host_workspaces_path",
    "get_workspace",
    "is_opaque_host_id",
    "is_opaque_workspace_id",
    "list_workspaces",
    "load_workspaces_registry",
    "remove_workspace",
    "save_workspaces_registry",
    "set_workspace_state",
    "to_wire_manifest",
    "validate_workspace_root_realpath",
]

_WORKSPACES_FILE_ENV = "BOTEX_WORKSPACES_FILE"
_DEFAULT_PATH = Path.home() / ".botex" / "host_workspaces.json"
_SCHEMA_VERSION = 1

_WS_ID_RE = re.compile(r"^ws_[0-9a-f]{8,12}$")
_HOST_ID_RE = re.compile(r"^host_[0-9a-f]{6,8}$")

# Static normalization maps — never return raw user .lower()/ .strip() strings.
_VALID_STATES_MAP: Dict[str, str] = {
    "enrolled": "enrolled",
    "candidate": "candidate",
    "disabled": "disabled",
}
_VALID_SOURCES_MAP: Dict[str, str] = {
    "manual": "manual",
    "discover": "discover",
}


def get_host_workspaces_path(path: Optional[str | Path] = None) -> Path:
    """Registry path: explicit ``path`` > ``BOTEX_WORKSPACES_FILE`` > default."""
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = str(os.environ.get(_WORKSPACES_FILE_ENV, "") or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return _DEFAULT_PATH


def generate_workspace_id() -> str:
    """Opaque workspace id: ``ws_`` + 12 hex chars (6 bytes)."""
    return f"ws_{secrets.token_hex(6)}"


def generate_daemon_device_id() -> str:
    """Opaque host id: ``host_`` + 8 hex chars (4 bytes)."""
    return f"host_{secrets.token_hex(4)}"


def is_opaque_workspace_id(val: Any) -> bool:
    """True when ``val`` matches ``^ws_[0-9a-f]{8,12}$``."""
    return isinstance(val, str) and bool(_WS_ID_RE.fullmatch(val))


def is_opaque_host_id(val: Any) -> bool:
    """True when ``val`` matches ``^host_[0-9a-f]{6,8}$``."""
    return isinstance(val, str) and bool(_HOST_ID_RE.fullmatch(val))


def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _normalize_state(state: Any) -> str:
    norm = str(state or "").strip().lower()
    return _VALID_STATES_MAP.get(norm, "candidate")


def _normalize_source(source: Any) -> str:
    norm = str(source or "").strip().lower()
    return _VALID_SOURCES_MAP.get(norm, "manual")


def _default_registry(device_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "version": _SCHEMA_VERSION,
        "device_id": device_id or generate_daemon_device_id(),
        "workspaces": [],
    }


def load_workspaces_registry(path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load the host workspace registry; return a fresh default when missing/invalid."""
    reg_path = get_host_workspaces_path(path)
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _default_registry()
    if not isinstance(data, dict):
        return _default_registry()
    data.setdefault("version", _SCHEMA_VERSION)
    data.setdefault("device_id", generate_daemon_device_id())
    data.setdefault("workspaces", [])
    if not isinstance(data["workspaces"], list):
        data["workspaces"] = []
    else:
        data["workspaces"] = [ws for ws in data["workspaces"] if isinstance(ws, dict)]
    return data


def save_workspaces_registry(data: Dict[str, Any], path: Optional[str | Path] = None) -> Path:
    """Atomically persist the registry; auto-generate ``device_id`` on first save."""
    reg_path = get_host_workspaces_path(path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    payload = dict(data)
    if not payload.get("device_id"):
        payload["device_id"] = generate_daemon_device_id()
    payload["version"] = _SCHEMA_VERSION

    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    if os.name != "nt":
        fd: Optional[int] = None
        tmp: Optional[Path] = None
        for _ in range(3):
            tmp = reg_path.parent / f".{reg_path.name}.{secrets.token_hex(4)}.tmp"
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                continue
        if fd is None:
            raise OSError("Could not allocate a unique temp file for the workspace registry")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, reg_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError:
                pass
    else:
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(reg_path.parent), prefix=f".{reg_path.name}.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, reg_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        _tighten_acl_windows(reg_path.parent)
        _tighten_acl_windows(reg_path)
    return reg_path


def _workspace_by_id(workspaces: List[Dict[str, Any]], workspace_id: str) -> Optional[int]:
    for i, ws in enumerate(workspaces):
        if ws.get("workspace_id") == workspace_id:
            return i
    return None


def _workspace_by_root(workspaces: List[Dict[str, Any]], root: str) -> Optional[int]:
    for i, ws in enumerate(workspaces):
        if ws.get("root") == root:
            return i
    return None


def _reject_symlinks_in_path(path: Path) -> None:
    """Reject if any path component is a symlink (F4 TOCTOU guard)."""
    parts = path.parts
    if not parts:
        return
    if path.anchor:
        acc = Path(path.anchor)
        start = 1
    else:
        acc = Path.cwd()
        start = 0
    for part in parts[start:]:
        acc = acc / part
        if acc.is_symlink():
            raise ValueError(f"Symlink detected in path component: {acc}")


def validate_workspace_root_realpath(
    expected_root: str | Path, target_path: Optional[str | Path] = None
) -> Path:
    """Resolve and guard ``expected_root`` against symlinks / TOCTOU escapes (F4).

    Ensures ``expected_root`` and every component up to ``target_path`` is not a symlink,
    resolves via ``os.path.realpath``, and checks ``os.path.commonpath`` containment.
    """
    raw_root = Path(expected_root).expanduser()
    root_path = raw_root if raw_root.is_absolute() else (Path.cwd() / raw_root)
    if not root_path.exists():
        raise ValueError(f"Workspace root does not exist: {expected_root}")
    if not root_path.is_dir():
        raise ValueError(f"Workspace root is not a directory: {expected_root}")

    _reject_symlinks_in_path(root_path)
    real_root = Path(os.path.realpath(root_path))

    if target_path is not None:
        raw_target = Path(target_path).expanduser()
        target = raw_target if raw_target.is_absolute() else (root_path / raw_target)
        _reject_symlinks_in_path(target)
        real_target = Path(os.path.realpath(target))
        norm_root = os.path.normcase(str(real_root))
        norm_target = os.path.normcase(str(real_target))
        try:
            common = os.path.commonpath([norm_root, norm_target])
        except ValueError as exc:
            raise ValueError("Target path escapes workspace root containment") from exc
        if common != norm_root:
            raise ValueError("Target path escapes workspace root containment")

    return real_root


def add_workspace(
    root: str,
    *,
    label: str = "",
    workspace_id: str = "",
    state: str = "enrolled",
    daemon_mode: bool = True,
    source: str = "manual",
    caps_overrides: Optional[Dict[str, Any]] = None,
    local_admin: bool = False,
    path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Add a workspace to the registry; reject duplicates and non-opaque ids in daemon mode."""
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]

    real_root = validate_workspace_root_realpath(root)

    norm_state = _normalize_state(state)
    norm_source = _normalize_source(source)

    if daemon_mode and workspace_id:
        if not is_opaque_workspace_id(workspace_id):
            raise ValueError(
                "daemon_mode requires an opaque workspace_id (^ws_[0-9a-f]{8,12}$); "
                "path-derived ids are rejected"
            )
    if not workspace_id:
        workspace_id = generate_workspace_id()

    if _workspace_by_root(workspaces, str(real_root)) is not None:
        raise ValueError(f"Workspace root already registered: {real_root}")
    if _workspace_by_id(workspaces, workspace_id) is not None:
        raise ValueError(f"Workspace id already registered: {workspace_id}")

    entry = {
        "workspace_id": workspace_id,
        "root": str(real_root),
        "label": str(label or ""),
        "state": norm_state,
        "added_at": _now_utc_iso(),
        "source": norm_source,
        "local_admin": bool(local_admin),
        "caps_overrides": caps_overrides if isinstance(caps_overrides, dict) else {},
    }
    workspaces.append(entry)
    save_workspaces_registry(reg, path=path)
    return entry


def remove_workspace(workspace_id: str, *, path: Optional[str | Path] = None) -> bool:
    """Remove a workspace by id; ``True`` when an entry was actually deleted."""
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]
    idx = _workspace_by_id(workspaces, workspace_id)
    if idx is None:
        return False
    workspaces.pop(idx)
    save_workspaces_registry(reg, path=path)
    return True


def set_workspace_state(
    workspace_id: str, state: str, *, path: Optional[str | Path] = None
) -> Optional[Dict[str, Any]]:
    """Set a workspace state; return the updated entry or ``None`` when absent."""
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]
    idx = _workspace_by_id(workspaces, workspace_id)
    if idx is None:
        return None
    workspaces[idx]["state"] = _normalize_state(state)
    save_workspaces_registry(reg, path=path)
    return dict(workspaces[idx])


def list_workspaces(
    *, include_disabled: bool = True, path: Optional[str | Path] = None
) -> List[Dict[str, Any]]:
    """List workspace entries; optionally exclude ``disabled`` ones."""
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]
    if include_disabled:
        return [dict(ws) for ws in workspaces]
    return [dict(ws) for ws in workspaces if ws.get("state") != "disabled"]


def get_workspace(
    workspace_id: str, *, path: Optional[str | Path] = None
) -> Optional[Dict[str, Any]]:
    """Return a workspace entry by id, or ``None`` when absent."""
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]
    idx = _workspace_by_id(workspaces, workspace_id)
    if idx is None:
        return None
    return dict(workspaces[idx])


def discover_candidate_workspaces(
    search_roots: List[str], *, path: Optional[str | Path] = None
) -> List[Dict[str, Any]]:
    """Scan roots 1-2 levels deep for project markers; add only ``state='candidate'``.

    Never auto-enrolls; skips already-registered canonical roots.
    """
    reg = load_workspaces_registry(path)
    workspaces = reg["workspaces"]
    registered_roots = {ws.get("root", "") for ws in workspaces}

    markers = (".git", ".hg", ".svn", "pyproject.toml", "setup.py", "package.json")
    found: List[Dict[str, Any]] = []

    for root in search_roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            continue
        try:
            real_root = Path(os.path.realpath(root_path))
        except OSError:
            continue

        # Walk 1-2 levels deep looking for project markers.
        for dirpath, dirnames, _filenames in os.walk(real_root):
            try:
                rel = Path(dirpath).relative_to(real_root)
            except ValueError:
                dirnames.clear()
                continue
            depth = len(rel.parts)
            if depth > 2:
                dirnames.clear()
                continue
            if depth == 2:
                dirnames.clear()  # do not descend past 2 levels

            has_marker = any((Path(dirpath) / m).exists() for m in markers)
            if not has_marker:
                continue

            canon = str(Path(dirpath).resolve())
            if canon in registered_roots:
                continue

            entry = {
                "workspace_id": generate_workspace_id(),
                "root": canon,
                "label": Path(dirpath).name,
                "state": "candidate",
                "added_at": _now_utc_iso(),
                "source": "discover",
                "local_admin": False,
                "caps_overrides": {},
            }
            workspaces.append(entry)
            registered_roots.add(canon)
            found.append(entry)

    if found:
        save_workspaces_registry(reg, path=path)
    return found


def to_wire_manifest(
    registry_or_list: Any = None, *, path: Optional[str | Path] = None
) -> List[Dict[str, Any]]:
    """Emit only ``{workspace_id, label, state}`` — never root, caps_overrides, or local_admin."""
    if registry_or_list is None:
        if path is None:
            return []
        registry_or_list = load_workspaces_registry(path=path)

    if isinstance(registry_or_list, dict):
        workspaces = registry_or_list.get("workspaces", [])
    elif isinstance(registry_or_list, list):
        workspaces = registry_or_list
    else:
        return []

    manifest: List[Dict[str, Any]] = []
    for ws in workspaces:
        if not isinstance(ws, dict):
            continue
        ws_id = ws.get("workspace_id", "")
        if not isinstance(ws_id, str) or not ws_id.strip():
            continue
        manifest.append(
            {
                "workspace_id": ws_id,
                "label": ws.get("label", ""),
                "state": _normalize_state(ws.get("state", "")),
            }
        )
    return manifest

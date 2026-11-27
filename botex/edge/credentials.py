# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Cross-platform isolated credential store for Edge device tokens (``btxd_``)."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "delete_edge_token",
    "edge_token_path",
    "extract_token_jti",
    "get_edge_credentials_dir",
    "list_edge_credentials",
    "load_edge_token",
    "resolve_edge_token",
    "save_edge_token",
    "validate_credential_id",
]

_CRED_DIR_ENV = "BOTEX_CREDENTIALS_DIR"
_TOKEN_PREFIX = "btxd_"
_SEPARATOR = "__"
_IS_WINDOWS = os.name == "nt"
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_JTI_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def get_edge_credentials_dir(base_dir: Optional[str | Path] = None) -> Path:
    """Store root: ``base_dir`` > ``BOTEX_CREDENTIALS_DIR`` > ``~/.botex/credentials/edge``."""
    if base_dir is not None and str(base_dir).strip():
        return Path(base_dir).expanduser()
    env_dir = str(os.environ.get(_CRED_DIR_ENV, "") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".botex" / "credentials" / "edge"


def validate_credential_id(value: str, label: str = "id") -> str:
    """Return a sanitized identifier or raise ``ValueError``.

    The allowlist charset already excludes whitespace, NULs and path separators;
    ``.``/``..`` segments and the ``__`` filename separator are rejected on top.
    """
    text = value if isinstance(value, str) else str(value or "")
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"Invalid {label}: expected ^[A-Za-z0-9._-]{{1,128}}$")
    if text in (".", "..") or _SEPARATOR in text:
        raise ValueError(f"Invalid {label}: reserved or ambiguous value")
    return text


def edge_token_path(
    device_id: str,
    workspace_id: str,
    *,
    base_dir: Optional[str | Path] = None,
) -> Path:
    """Resolved ``<cred_dir>/<device_id>__<workspace_id>.token`` confined to the store dir."""
    dev = validate_credential_id(device_id, "device_id")
    ws = validate_credential_id(workspace_id, "workspace_id")
    cred_dir = get_edge_credentials_dir(base_dir).resolve()
    target = (cred_dir / f"{dev}{_SEPARATOR}{ws}.token").resolve()
    if target.parent != cred_dir:
        raise ValueError("Resolved credential path escapes the store directory")
    return target


def extract_token_jti(token: str) -> str:
    """Public JTI from ``btxd_<jti>.<secret>``; ``""`` when malformed, never the secret part."""
    text = token if isinstance(token, str) else ""
    text = text.strip()
    if not text.startswith(_TOKEN_PREFIX) or "." not in text:
        return ""
    jti = text.split(".", 1)[0][len(_TOKEN_PREFIX) :]
    return jti if _JTI_RE.fullmatch(jti) else ""


def save_edge_token(
    device_id: str,
    workspace_id: str,
    token: str,
    *,
    base_dir: Optional[str | Path] = None,
    allow_weak_acl: bool = False,
) -> Dict[str, Any]:
    """Atomically persist a ``btxd_`` device token; the returned metadata never contains it."""
    dev = validate_credential_id(device_id, "device_id")
    ws = validate_credential_id(workspace_id, "workspace_id")
    target = edge_token_path(dev, ws, base_dir=base_dir)
    text = token if isinstance(token, str) else ""
    text = text.strip()
    if not text.startswith(_TOKEN_PREFIX) or "." not in text:
        raise ValueError("Edge token must look like 'btxd_<jti>.<secret>'")
    cred_dir = target.parent
    cred_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = text.encode("utf-8")
    weak_acl = False
    if not _IS_WINDOWS:
        os.chmod(cred_dir, 0o700)
        # O_EXCL guarantees the 0o600 mode applies to a fresh inode (no umask window).
        fd: Optional[int] = None
        tmp = cred_dir / ".tmp"
        for _ in range(3):
            tmp = cred_dir / f".{target.name}.{secrets.token_hex(4)}.tmp"
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                continue
        if fd is None:
            raise OSError("Could not allocate a unique temp file for the credential")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        try:
            _tighten_acl_windows(cred_dir)
        except OSError:
            if not allow_weak_acl:
                raise
            weak_acl = True
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(cred_dir), prefix=f".{target.name}.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                _tighten_acl_windows(tmp_path)
            except OSError:
                if not allow_weak_acl:
                    raise
                weak_acl = True
            os.replace(tmp_path, target)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        try:
            _tighten_acl_windows(target)
        except OSError as acl_exc:
            if not allow_weak_acl:
                try:
                    target.unlink()
                except OSError as unlink_exc:
                    raise OSError(
                        f"{acl_exc}; additionally failed removing unprotected target {target}: {unlink_exc}"
                    ) from acl_exc
                raise
            weak_acl = True
    meta: Dict[str, Any] = {
        "ok": True,
        "device_id": dev,
        "workspace_id": ws,
        "jti": extract_token_jti(text),
        "path": str(target),
    }
    if weak_acl:
        meta["weak_acl"] = True
    return meta


def load_edge_token(
    device_id: str,
    workspace_id: str,
    *,
    base_dir: Optional[str | Path] = None,
) -> str:
    """Stored token for ``(device_id, workspace_id)`` or ``""`` when absent/unreadable."""
    try:
        return (
            edge_token_path(device_id, workspace_id, base_dir=base_dir)
            .read_text(encoding="utf-8")
            .strip()
        )
    except (OSError, UnicodeDecodeError):
        return ""


def delete_edge_token(
    device_id: str,
    workspace_id: str,
    *,
    base_dir: Optional[str | Path] = None,
) -> bool:
    """Remove the stored token file; ``True`` when a file was actually deleted."""
    try:
        edge_token_path(device_id, workspace_id, base_dir=base_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def list_edge_credentials(*, base_dir: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """Metadata rows for stored tokens (device/workspace/jti/mtime) — never token material."""
    cred_dir = get_edge_credentials_dir(base_dir)
    if not cred_dir.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in cred_dir.glob("*.token"):
        parts = path.name[: -len(".token")].split(_SEPARATOR)
        if len(parts) != 2:
            continue
        try:
            dev = validate_credential_id(parts[0], "device_id")
            ws = validate_credential_id(parts[1], "workspace_id")
        except ValueError:
            continue
        jti = ""
        try:
            jti = extract_token_jti(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
        try:
            mtime = path.stat().st_mtime
            updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
        except OSError:
            updated_at = ""
        rows.append({"device_id": dev, "workspace_id": ws, "jti": jti, "updated_at": updated_at})
    rows.sort(key=lambda r: (r["device_id"], r["workspace_id"]))
    return rows


def resolve_edge_token(
    device_id: str,
    workspace_id: str,
    *,
    cli_token: str = "",
    token_file: str = "",
    env_token: str = "",
    base_dir: Optional[str | Path] = None,
) -> tuple[str, str]:
    """First-wins token resolution: CLI > ``--token-file`` > env override > isolated store."""
    tok = cli_token.strip() if isinstance(cli_token, str) else ""
    if tok:
        return tok, "cli"
    tf = token_file.strip() if isinstance(token_file, str) else ""
    if tf:
        # An explicit --token-file is authoritative: unreadable raises OSError, and an
        # empty file must not silently fall back to a different-source credential.
        return Path(tf).expanduser().read_text(encoding="utf-8").strip(), "token_file"
    env = env_token.strip() if isinstance(env_token, str) else ""
    if env:
        return env, "env"
    stored = load_edge_token(device_id, workspace_id, base_dir=base_dir)
    if stored:
        return stored, "store"
    return "", ""


def _tighten_acl_windows(path: Path) -> None:
    """Restrict ACL to the current Windows user via ``icacls``; fails closed on error."""
    username = str(os.environ.get("USERNAME", "") or "").strip()
    if not username:
        raise OSError("Cannot tighten Windows ACL without USERNAME environment variable")
    try:
        res = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F", "/Q"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Failed running icacls on {path}: {exc}") from exc
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip() or f"exit={res.returncode}"
        raise OSError(f"icacls failed for {path}: {detail}")

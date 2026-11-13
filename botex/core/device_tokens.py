# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Hashed per-device bearer tokens and JTI revocation denylist for BoteX Edge."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

_TOKEN_PREFIX = "btxd_"
_VALID_SCOPE_PREFIXES = frozenset({"task_pull", "result_push", "delegate_local"})
_DEFAULT_TTL_S = 30 * 24 * 3600


def _digest_secret(raw_value: str) -> str:
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _normalize_scope(scope: str) -> Optional[str]:
    text = str(scope or "").strip()
    if ":" not in text:
        return None
    kind, ws = text.split(":", 1)
    if kind not in _VALID_SCOPE_PREFIXES:
        return None
    clean_ws = ws.strip()
    if not clean_ws:
        return None
    return f"{kind}:{clean_ws}"


@dataclass
class DeviceTokenRecord:
    """Stored metadata for a hashed per-device token (never holds the raw credential)."""

    jti: str
    device_id: str
    workspace_id: str
    scopes: List[str] = field(default_factory=list)
    digest_sha256: str = ""
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    revoked: bool = False
    label: str = ""

    def has_scope(self, required_scope: str) -> bool:
        req = _normalize_scope(required_scope)
        if not req:
            return False
        if req in self.scopes:
            return True
        kind, _ = req.split(":", 1)
        return f"{kind}:*" in self.scopes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "jti": self.jti,
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "scopes": list(self.scopes),
            "digest_sha256": self.digest_sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked": bool(self.revoked),
            "label": self.label,
        }

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "jti": self.jti,
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "scopes": list(self.scopes),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked": bool(self.revoked),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[DeviceTokenRecord]:
        jti = sanitize_identifier(str(data.get("jti") or ""))
        device_id = str(data.get("device_id") or "").strip()
        workspace_id = str(data.get("workspace_id") or "").strip()
        digest = str(data.get("digest_sha256") or "").strip().lower()
        if not jti or not device_id or not workspace_id or len(digest) != 64:
            return None
        scopes = [s for s in (_normalize_scope(x) for x in (data.get("scopes") or [])) if s]
        return cls(
            jti=jti,
            device_id=device_id,
            workspace_id=workspace_id,
            scopes=scopes,
            digest_sha256=digest,
            issued_at=float(data.get("issued_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            revoked=bool(data.get("revoked", False)),
            label=str(data.get("label") or "")[:120],
        )


class DeviceTokenStore:
    """Thread-safe atomic JSON store for hashed device tokens and JTI revocations."""

    def __init__(self, store_path: str | Path) -> None:
        self.store_path = Path(store_path).resolve()
        self._lock = threading.RLock()
        self._records: Dict[str, DeviceTokenRecord] = {}
        self._revoked_jtis: set[str] = set()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.store_path.is_file():
                self._records = {}
                self._revoked_jtis = set()
                return
            try:
                raw = self.store_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, ValueError):
                return
            if not isinstance(parsed, dict):
                return
            records: Dict[str, DeviceTokenRecord] = {}
            for item in parsed.get("tokens") or []:
                if isinstance(item, dict):
                    rec = DeviceTokenRecord.from_dict(item)
                    if rec is not None:
                        records[rec.jti] = rec
            revoked = {
                clean
                for item in (parsed.get("revoked_jtis") or [])
                if (clean := sanitize_identifier(str(item or "")))
            }
            for rec in records.values():
                if rec.revoked:
                    revoked.add(rec.jti)
            self._records = records
            self._revoked_jtis = revoked

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload_obj = {
            "version": 1,
            "tokens": [rec.to_dict() for rec in self._records.values()],
            "revoked_jtis": sorted(self._revoked_jtis),
        }
        payload = coerce_text(json.dumps(payload_obj, ensure_ascii=False, indent=2))
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.store_path.parent),
                prefix=".device-tokens-",
                suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.store_path)
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def issue_token(
        self,
        device_id: str,
        workspace_id: str,
        *,
        scopes: Optional[List[str]] = None,
        ttl_s: int = _DEFAULT_TTL_S,
        label: str = "",
    ) -> Tuple[str, DeviceTokenRecord]:
        """Issue a new per-device token; raw secret is returned once and only its hash is persisted."""
        clean_dev = coerce_text(mask_secrets(str(device_id or ""))).strip()[:96]
        clean_ws = coerce_text(mask_secrets(str(workspace_id or ""))).strip()[:160]
        if not clean_dev or not clean_ws:
            raise ValueError("device_id and workspace_id must be non-empty.")

        default_scopes = [f"task_pull:{clean_ws}", f"result_push:{clean_ws}"]
        raw_scopes = default_scopes if scopes is None else list(scopes)
        norm_scopes: List[str] = []
        for s in raw_scopes:
            ns = _normalize_scope(s)
            if ns is None:
                raise ValueError(f"Invalid device token scope: {s!r}")
            if ns not in norm_scopes:
                norm_scopes.append(ns)
        if not norm_scopes:
            raise ValueError("At least one valid scope is required.")

        now = time.time()
        ttl_val = max(int(ttl_s), 60) if ttl_s is not None else _DEFAULT_TTL_S
        jti = secrets.token_hex(8)
        secret_part = secrets.token_urlsafe(32)
        raw_credential = f"{_TOKEN_PREFIX}{jti}.{secret_part}"

        record = DeviceTokenRecord(
            jti=jti,
            device_id=clean_dev,
            workspace_id=clean_ws,
            scopes=norm_scopes,
            digest_sha256=_digest_secret(raw_credential),
            issued_at=now,
            expires_at=now + ttl_val,
            revoked=False,
            label=coerce_text(mask_secrets(str(label or ""))).strip()[:120],
        )
        with self._lock:
            self._records[jti] = record
            self._save()
        return raw_credential, record

    def verify_token(
        self,
        raw_credential: Optional[str],
        *,
        required_scope: Optional[str] = None,
        required_scopes: Optional[List[str]] = None,
    ) -> Optional[DeviceTokenRecord]:
        """Verify raw credential via constant-time hash check, expiry, revocation, and scopes."""
        text = str(raw_credential or "").strip()
        if not text.startswith(_TOKEN_PREFIX) or "." not in text:
            return None
        prefix_and_jti, _ = text.split(".", 1)
        jti = sanitize_identifier(prefix_and_jti[len(_TOKEN_PREFIX) :])
        if not jti:
            return None

        with self._lock:
            if jti in self._revoked_jtis:
                return None
            rec = self._records.get(jti)
            if rec is None or rec.revoked:
                return None

        candidate_digest = _digest_secret(text)
        if not hmac.compare_digest(candidate_digest, rec.digest_sha256):
            return None

        if rec.expires_at > 0 and time.time() > rec.expires_at:
            return None

        needed = list(required_scopes or [])
        if required_scope:
            needed.append(required_scope)
        for req in needed:
            if not rec.has_scope(req):
                return None
        return rec

    def revoke_token(self, jti: str) -> bool:
        """Add ``jti`` to the persistent revocation denylist."""
        clean = sanitize_identifier(str(jti or ""))
        if not clean:
            return False
        with self._lock:
            existed = clean in self._records or clean in self._revoked_jtis
            self._revoked_jtis.add(clean)
            if clean in self._records:
                self._records[clean].revoked = True
            self._save()
            return existed

    def is_revoked(self, jti: str) -> bool:
        clean = sanitize_identifier(str(jti or ""))
        if not clean:
            return True
        with self._lock:
            if clean in self._revoked_jtis:
                return True
            rec = self._records.get(clean)
            return bool(rec and rec.revoked)

    def list_tokens(self, *, include_revoked: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [
                rec.to_public_dict()
                for rec in self._records.values()
                if include_revoked or (not rec.revoked and rec.jti not in self._revoked_jtis)
            ]
        rows.sort(key=lambda r: float(r.get("issued_at") or 0.0), reverse=True)
        return rows


def default_device_token_store(workspace_dir: str | Path = "") -> DeviceTokenStore:
    """Return the workspace-scoped ``DeviceTokenStore`` under ``.botex/device_tokens.json``."""
    base = Path(workspace_dir or ".").resolve()
    return DeviceTokenStore(base / ".botex" / "device_tokens.json")

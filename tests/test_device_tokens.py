# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Tests for hashed per-device bearer tokens and JTI revocation denylist."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from botex.core.device_tokens import DeviceTokenStore


def test_issue_verify_and_persistence_never_stores_raw_token(tmp_path: Path):
    store_file = tmp_path / ".botex" / "device_tokens.json"
    store = DeviceTokenStore(store_file)

    raw_tok, rec = store.issue_token("laptop-win11", "repo-alpha", label="Main DevBox")
    assert raw_tok.startswith("btxd_")
    assert rec.device_id == "laptop-win11"
    assert rec.workspace_id == "repo-alpha"
    assert rec.scopes == ["task_pull:repo-alpha", "result_push:repo-alpha"]

    disk_text = store_file.read_text(encoding="utf-8")
    assert raw_tok not in disk_text
    assert rec.digest_sha256 in disk_text

    verified = store.verify_token(raw_tok, required_scope="task_pull:repo-alpha")
    assert verified is not None
    assert verified.jti == rec.jti

    assert store.verify_token(raw_tok, required_scope="delegate_local:repo-alpha") is None
    assert store.verify_token(raw_tok, required_scope="task_pull:other-repo") is None
    assert store.verify_token(raw_tok + "tamper") is None
    assert store.verify_token("bad-prefix") is None

    # Reload from disk verifies durability without raw credential
    reloaded = DeviceTokenStore(store_file)
    assert reloaded.verify_token(raw_tok, required_scope="result_push:repo-alpha") is not None

    public_rows = reloaded.list_tokens()
    assert len(public_rows) == 1
    assert "digest_sha256" not in json.dumps(public_rows[0])
    assert raw_tok not in json.dumps(public_rows[0])


def test_revocation_and_expiry_and_wildcard_scope(tmp_path: Path):
    store = DeviceTokenStore(tmp_path / "tokens.json")
    tok1, rec1 = store.issue_token(
        "box-1",
        "ws-1",
        scopes=["task_pull:*", "result_push:ws-1", "delegate_local:ws-1"],
    )
    assert store.verify_token(tok1, required_scope="task_pull:any-ws") is not None
    assert store.verify_token(tok1, required_scope="delegate_local:ws-1") is not None

    assert store.revoke_token(rec1.jti) is True
    assert store.is_revoked(rec1.jti) is True
    assert store.verify_token(tok1) is None

    reloaded = DeviceTokenStore(tmp_path / "tokens.json")
    assert reloaded.is_revoked(rec1.jti) is True
    assert reloaded.verify_token(tok1) is None
    assert reloaded.list_tokens(include_revoked=False) == []

    # Expired token fails verification
    tok2, rec2 = store.issue_token("box-2", "ws-2", ttl_s=60)
    store._records[rec2.jti].expires_at = time.time() - 5.0
    assert store.verify_token(tok2) is None


def test_invalid_inputs_rejected(tmp_path: Path):
    store = DeviceTokenStore(tmp_path / "tokens.json")
    with pytest.raises(ValueError):
        store.issue_token("", "ws")
    with pytest.raises(ValueError):
        store.issue_token("dev", "ws", scopes=["admin:ws"])
    with pytest.raises(ValueError):
        store.issue_token("dev", "ws", scopes=[])

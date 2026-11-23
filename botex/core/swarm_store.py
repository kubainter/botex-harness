# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""BoteX Cloud Soul — versioned swarm identity store.

Single SQLite WAL store holding the swarm's bot registry, per-bot briefs
(souls) with etag/fencing metadata, host leases with fencing tokens,
durable work orders, and data-only agent-to-agent channels. Domain
knowledge search stays in the Memory Vault's ``index.db`` — this store
intentionally ships ZERO FTS5 tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    from botex.tools.security import (
        coerce_text,
        mask_secrets,
        sanitize_identifier,
        sanitize_origin_claim,
        wrap_untrusted_content,
    )
except (ImportError, ValueError):
    from security import (
        coerce_text,
        mask_secrets,
        sanitize_identifier,
        sanitize_origin_claim,
        wrap_untrusted_content,
    )

__all__ = [
    "SWARM_SCHEMA_VERSION",
    "get_swarm_store_conn",
    "migrate_swarm_store",
    "close_swarm_store_dbs",
    "register_bot",
    "get_bot",
    "list_bots",
    "set_bot_status",
    "pause_bot",
    "resume_bot",
    "get_bot_brief",
    "upsert_bot_brief",
    "record_bot_domain_turn",
    "get_bot_turn_counter",
    "list_bots_due_for_brief_review",
    "upsert_host_lease",
    "get_active_leases",
    "upsert_host_harnesses",
    "list_host_harnesses",
    "create_work_order",
    "get_work_order",
    "list_work_orders",
    "update_work_order_status",
    "list_expired_work_orders",
    "list_active_brief_payloads",
    "create_channel",
    "list_channels",
    "get_channel",
    "post_channel_message",
    "list_channel_messages",
]

SWARM_SCHEMA_VERSION = 4


# server/skills stay lazy-imported: core must not hard-depend on upper layers
# at module load (heavy import chains + import-order cycles).
def _swarm_store_db_path() -> Path:
    try:
        from botex.server.config import get_swarm_store_db_path
    except (ImportError, ValueError):
        from config import get_swarm_store_db_path
    return get_swarm_store_db_path()


def _bots_cfg() -> Dict[str, Any]:
    try:
        from botex.server.config import get_bots_config
    except (ImportError, ValueError):
        from config import get_bots_config
    return get_bots_config()


def _has_injection_pattern(text: str) -> bool:
    try:
        from botex.skills.skills import has_injection_pattern
    except (ImportError, ValueError):
        from skills import has_injection_pattern
    return has_injection_pattern(text)


def _work_order_fsm() -> Any:
    try:
        from botex.core import work_orders as fsm
    except (ImportError, ValueError):
        import work_orders as fsm
    return fsm


def _coerce_int(value: Any, default: int) -> int:
    """int-coerce a config/column value; non-numeric input (incl. None, bool) -> default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _default_brief_budget() -> int:
    return _coerce_int(_bots_cfg().get("brief_budget_tokens"), 250)


def _default_lease_ttl() -> int:
    return _coerce_int(_bots_cfg().get("default_lease_ttl_s"), 900)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# SQLite WAL pooled connections (same pattern as memory index.db)
# ============================================================================


class _PooledSwarmConnection(sqlite3.Connection):
    """Pooled SQLite connection where close() is a no-op to eliminate reopen overhead."""

    def close(self) -> None:
        pass

    def force_close(self) -> None:
        super().close()


_SWARM_CONNS = threading.local()
_ALL_SWARM_CONNS: "weakref.WeakSet[_PooledSwarmConnection]" = weakref.WeakSet()
_SWARM_LOCK = threading.Lock()
_SWARM_SCHEMA_READY: set = set()
_SWARM_SCHEMA_LOCK = threading.Lock()

_SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS bots_registry (
    slug TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    role_desc TEXT DEFAULT '',
    instructions_path TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    brief_budget_tokens INTEGER DEFAULT 250,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_briefs (
    slug TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    etag TEXT NOT NULL,
    fencing_epoch INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS host_leases (
    lease_key TEXT PRIMARY KEY,
    bot_slug TEXT NOT NULL,
    host_id TEXT NOT NULL,
    scope_path TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    summary TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS work_orders (
    order_id TEXT PRIMARY KEY,
    bot_slug TEXT,
    epoch INTEGER DEFAULT 0,
    status TEXT DEFAULT 'QUEUED',
    payload_json TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);
"""


_SCHEMA_V2_COUNTERS_SQL = """
CREATE TABLE IF NOT EXISTS bot_turn_counters (
    slug TEXT PRIMARY KEY,
    pending_turns INTEGER DEFAULT 0,
    last_activity_at REAL DEFAULT 0,
    last_review_at REAL DEFAULT 0
);
"""

_SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS host_harness_inventory (
    host_id TEXT NOT NULL,
    harness_id TEXT NOT NULL,
    transport TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'online',
    last_seen TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (host_id, harness_id)
);
"""

_SCHEMA_V4_SQL = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    participants_json TEXT NOT NULL DEFAULT '[]',
    acl_mode TEXT NOT NULL DEFAULT 'participants',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    sender_slug TEXT NOT NULL,
    sender_origin TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    untrusted INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_messages_chan_created
    ON channel_messages(channel_id, created_at);
"""


def migrate_swarm_store(conn: sqlite3.Connection) -> None:
    """Apply in-place schema migrations gated by ``PRAGMA user_version``; idempotent."""
    row = conn.execute("PRAGMA user_version;").fetchone()
    current = int(row[0]) if row else 0
    if current < 1:
        conn.executescript(_SCHEMA_V1_SQL)
        conn.execute("PRAGMA user_version = 1;")
        conn.commit()
    if current < 2:
        cols = {
            str(r["name"] if isinstance(r, sqlite3.Row) else r[1])
            for r in conn.execute("PRAGMA table_info(bot_briefs);").fetchall()
        }
        if "pinned" not in cols:
            conn.execute("ALTER TABLE bot_briefs ADD COLUMN pinned INTEGER DEFAULT 0;")
        if "author" not in cols:
            conn.execute("ALTER TABLE bot_briefs ADD COLUMN author TEXT DEFAULT 'human';")
        conn.executescript(_SCHEMA_V2_COUNTERS_SQL)
        conn.execute("PRAGMA user_version = 2;")
        conn.commit()
    if current < 3:
        conn.executescript(_SCHEMA_V3_SQL)
        conn.execute("PRAGMA user_version = 3;")
        conn.commit()
    if current < 4:
        conn.executescript(_SCHEMA_V4_SQL)
        conn.execute("PRAGMA user_version = 4;")
        conn.commit()


def get_swarm_store_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open or retrieve the pooled WAL connection for the swarm store."""
    path = Path(db_path) if db_path is not None else _swarm_store_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db_key = str(path.resolve())

    if not hasattr(_SWARM_CONNS, "conns"):
        _SWARM_CONNS.conns = {}

    conn = _SWARM_CONNS.conns.get(db_key)
    if conn is not None:
        try:
            conn.execute("SELECT 1;")
            return conn
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            conn = None

    conn = sqlite3.connect(
        db_key,
        timeout=5.0,
        check_same_thread=False,
        factory=_PooledSwarmConnection,
    )
    conn.row_factory = sqlite3.Row
    conn._db_path = db_key

    with _SWARM_LOCK:
        _ALL_SWARM_CONNS.add(conn)

    if db_key not in _SWARM_SCHEMA_READY:
        with _SWARM_SCHEMA_LOCK:
            if db_key not in _SWARM_SCHEMA_READY:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                migrate_swarm_store(conn)
                _SWARM_SCHEMA_READY.add(db_key)

    _SWARM_CONNS.conns[db_key] = conn
    return conn


def close_swarm_store_dbs() -> None:
    """Force close all pooled swarm-store connections and clear schema cache."""
    with _SWARM_LOCK:
        conns = list(_ALL_SWARM_CONNS)
        _ALL_SWARM_CONNS.clear()
    for c in conns:
        try:
            c.force_close()
        except Exception:
            pass
    with _SWARM_SCHEMA_LOCK:
        _SWARM_SCHEMA_READY.clear()
    if hasattr(_SWARM_CONNS, "conns"):
        _SWARM_CONNS.conns.clear()


# On Windows, wrap shutil.rmtree to release pooled SQLite file locks when a
# directory holding a swarm store is deleted (e.g. TemporaryDirectory in tests).
if not getattr(shutil.rmtree, "_botex_swarm_safe", False):
    _orig_rmtree = shutil.rmtree

    def _safe_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            resolved = str(Path(path).resolve())
            with _SWARM_LOCK:
                conns = list(_ALL_SWARM_CONNS)
            for c in conns:
                db_p = getattr(c, "_db_path", None)
                if db_p and (db_p == resolved or db_p.startswith(resolved + os.sep)):
                    try:
                        c.force_close()
                    except Exception:
                        pass
                    with _SWARM_LOCK:
                        _ALL_SWARM_CONNS.discard(c)
                    with _SWARM_SCHEMA_LOCK:
                        _SWARM_SCHEMA_READY.discard(db_p)
                    if hasattr(_SWARM_CONNS, "conns"):
                        _SWARM_CONNS.conns.pop(db_p, None)
        except Exception:
            pass
        return _orig_rmtree(path, *args, **kwargs)

    _safe_rmtree._botex_swarm_safe = True  # type: ignore[attr-defined]
    shutil.rmtree = _safe_rmtree


# ============================================================================
# Bots registry
# ============================================================================


_VALID_BOT_STATUSES: Dict[str, str] = {
    "active": "active",
    "paused": "paused",
    "dormant": "dormant",
    "disabled": "disabled",
}
_MIN_BRIEF_BUDGET_TOKENS = 32
_MAX_BRIEF_BUDGET_TOKENS = 2000
_MAX_ROLE_DESC_CHARS = 500
_MAX_NAMESPACE_CHARS = 128


def register_bot(
    slug: str,
    namespace: str,
    role_desc: str = "",
    instructions_path: str = "",
    status: str = "active",
    brief_budget_tokens: int = 250,
    db_path: Optional[Path] = None,
) -> dict:
    """Idempotently register (or re-register) a bot in the swarm registry."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return {"ok": False, "error": "INVALID_SLUG"}
    raw_ns = str(namespace or "")
    if raw_ns and _has_injection_pattern(raw_ns):
        return {"ok": False, "error": "INJECTION_DETECTED"}
    clean_ns = coerce_text(mask_secrets(raw_ns)).strip()[:_MAX_NAMESPACE_CHARS]
    if not clean_ns:
        return {"ok": False, "error": "INVALID_NAMESPACE"}
    raw_role = str(role_desc or "")
    if raw_role and _has_injection_pattern(raw_role):
        return {"ok": False, "error": "INJECTION_DETECTED"}
    clean_role = coerce_text(mask_secrets(raw_role)).strip()[:_MAX_ROLE_DESC_CHARS]
    clean_instructions = coerce_text(str(instructions_path or "")).strip()
    status_key = coerce_text(str(status or "active")).strip().lower() or "active"
    clean_status = _VALID_BOT_STATUSES.get(status_key)
    if clean_status is None:
        return {"ok": False, "error": "INVALID_STATUS"}
    budget = max(
        _MIN_BRIEF_BUDGET_TOKENS,
        min(_coerce_int(brief_budget_tokens, _default_brief_budget()), _MAX_BRIEF_BUDGET_TOKENS),
    )
    now_iso = _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK, conn:
        conn.execute(
            "INSERT INTO bots_registry"
            " (slug, namespace, role_desc, instructions_path, status,"
            "  brief_budget_tokens, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slug) DO UPDATE SET"
            "   namespace=excluded.namespace,"
            "   role_desc=excluded.role_desc,"
            "   instructions_path=excluded.instructions_path,"
            "   status=excluded.status,"
            "   brief_budget_tokens=excluded.brief_budget_tokens,"
            "   updated_at=excluded.updated_at;",
            (
                clean_slug,
                clean_ns,
                clean_role,
                clean_instructions,
                clean_status,
                budget,
                now_iso,
                now_iso,
            ),
        )
        row = conn.execute(
            "SELECT slug, namespace, role_desc, instructions_path, status,"
            " brief_budget_tokens, created_at, updated_at"
            " FROM bots_registry WHERE slug = ?;",
            (clean_slug,),
        ).fetchone()
    return {"ok": True, **dict(row)}


def get_bot(slug: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the registry row for ``slug`` or None."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return None
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT slug, namespace, role_desc, instructions_path, status,"
        " brief_budget_tokens, created_at, updated_at"
        " FROM bots_registry WHERE slug = ?;",
        (clean_slug,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_bots(
    namespace: str = "",
    status: str = "",
    db_path: Optional[Path] = None,
) -> list[dict]:
    """List registered bots, optionally filtered by namespace and/or status."""
    sql = (
        "SELECT slug, namespace, role_desc, instructions_path, status,"
        " brief_budget_tokens, created_at, updated_at FROM bots_registry"
    )
    clauses: List[str] = []
    params: List[str] = []
    if namespace and str(namespace).strip():
        clauses.append("namespace = ?")
        params.append(str(namespace).strip())
    if status and str(status).strip():
        clauses.append("status = ?")
        params.append(str(status).strip())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY slug;"
    conn = get_swarm_store_conn(db_path)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_bot_status(slug: str, status: str, db_path: Optional[Path] = None) -> dict:
    """Set a registered bot's status (``active``/``paused``/``dormant``/``disabled``)."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return {"ok": False, "error": "INVALID_SLUG"}
    clean_status = _VALID_BOT_STATUSES.get(coerce_text(str(status or "")).strip().lower())
    if clean_status is None:
        return {"ok": False, "error": "INVALID_STATUS"}
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK, conn:
        cur = conn.execute(
            "UPDATE bots_registry SET status = ?, updated_at = ? WHERE slug = ?;",
            (clean_status, _utc_now_iso(), clean_slug),
        )
        if cur.rowcount <= 0:
            return {"ok": False, "error": "BOT_NOT_FOUND"}
        row = conn.execute(
            "SELECT slug, namespace, role_desc, instructions_path, status,"
            " brief_budget_tokens, created_at, updated_at"
            " FROM bots_registry WHERE slug = ?;",
            (clean_slug,),
        ).fetchone()
    return {"ok": True, **dict(row)}


def pause_bot(slug: str, db_path: Optional[Path] = None) -> dict:
    """Freeze a bot: status -> ``paused`` (drops out of every ``active``-only query)."""
    return set_bot_status(slug, "paused", db_path=db_path)


def resume_bot(slug: str, db_path: Optional[Path] = None) -> dict:
    """Release a paused/dormant bot back to ``active``."""
    return set_bot_status(slug, "active", db_path=db_path)


# ============================================================================
# Bot briefs (souls)
# ============================================================================


_VALID_BRIEF_AUTHORS: Dict[str, str] = {
    "human": "human",
    "curator": "curator",
}


def get_bot_brief(slug: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the stored brief row for ``slug`` or None."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return None
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT slug, content, etag, fencing_epoch, updated_at,"
        " COALESCE(pinned, 0) AS pinned, COALESCE(author, 'human') AS author"
        " FROM bot_briefs WHERE slug = ?;",
        (clean_slug,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["pinned"] = bool(int(out.get("pinned") or 0))
    return out


def upsert_bot_brief(
    slug: str,
    content: str,
    *,
    expected_etag: Optional[str] = None,
    pinned: Optional[bool] = None,
    author: str = "human",
    decrement_turns: Optional[int] = None,
    review_ts: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Validate, mask and store a bot brief; enforces CAS/pinned guards inside BEGIN IMMEDIATE.

    Rejects with ``BOT_NOT_FOUND``, ``BOT_INACTIVE`` (when ``author == 'curator'``),
    ``INJECTION_DETECTED``, ``BRIEF_TOO_LARGE``, ``CURATOR_SKIPPED_PINNED``, or
    ``ETAG_MISMATCH``. When ``author == 'curator'`` or ``decrement_turns`` is
    provided, decrements ``bot_turn_counters.pending_turns`` atomically.
    """
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return {"ok": False, "error": "INVALID_SLUG"}
    clean_author = _VALID_BRIEF_AUTHORS.get(str(author or "human").strip().lower())
    if clean_author is None:
        return {"ok": False, "error": "INVALID_AUTHOR"}

    raw = str(content or "")
    if _has_injection_pattern(raw):
        return {"ok": False, "error": "INJECTION_DETECTED"}
    masked = coerce_text(mask_secrets(raw))
    etag = hashlib.sha256(masked.encode("utf-8")).hexdigest()[:32]
    now_iso = _utc_now_iso()
    rev_ts = float(review_ts) if review_ts is not None else time.time()

    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            bot = conn.execute(
                "SELECT slug, namespace, role_desc, status, brief_budget_tokens"
                " FROM bots_registry WHERE slug = ?;",
                (clean_slug,),
            ).fetchone()
            if bot is None:
                conn.rollback()
                return {"ok": False, "error": "BOT_NOT_FOUND"}
            if clean_author == "curator" and str(bot["status"] or "active").lower() != "active":
                conn.rollback()
                return {"ok": False, "error": "BOT_INACTIVE"}

            budget = _coerce_int(bot["brief_budget_tokens"], _default_brief_budget())
            est_tokens = math.ceil(len(masked) / 4) if masked else 0
            if est_tokens > budget:
                conn.rollback()
                return {
                    "ok": False,
                    "error": "BRIEF_TOO_LARGE",
                    "tokens": est_tokens,
                    "budget": budget,
                }

            existing = conn.execute(
                "SELECT fencing_epoch, etag, COALESCE(pinned, 0) AS pinned"
                " FROM bot_briefs WHERE slug = ?;",
                (clean_slug,),
            ).fetchone()
            existing_pinned = int(existing["pinned"] or 0) if existing is not None else 0
            existing_etag = str(existing["etag"] or "") if existing is not None else ""
            if clean_author == "curator" and existing_pinned == 1:
                conn.rollback()
                return {"ok": False, "error": "CURATOR_SKIPPED_PINNED"}
            if expected_etag is not None and existing_etag != str(expected_etag):
                conn.rollback()
                return {
                    "ok": False,
                    "error": "ETAG_MISMATCH",
                    "current_etag": existing_etag,
                }

            prev_epoch = _coerce_int(existing["fencing_epoch"], 0) if existing is not None else 0
            new_epoch = prev_epoch + 1
            new_pinned = (
                int(bool(pinned))
                if (pinned is not None and clean_author == "human")
                else existing_pinned
            )

            conn.execute(
                "INSERT INTO bot_briefs"
                " (slug, content, etag, fencing_epoch, updated_at, pinned, author)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET"
                "   content=excluded.content,"
                "   etag=excluded.etag,"
                "   fencing_epoch=excluded.fencing_epoch,"
                "   updated_at=excluded.updated_at,"
                "   pinned=excluded.pinned,"
                "   author=excluded.author;",
                (clean_slug, masked, etag, new_epoch, now_iso, new_pinned, clean_author),
            )
            if decrement_turns is not None or clean_author == "curator":
                dec = max(0, _coerce_int(decrement_turns, 0))
                conn.execute(
                    "INSERT INTO bot_turn_counters"
                    " (slug, pending_turns, last_activity_at, last_review_at)"
                    " VALUES (?, 0, 0, ?)"
                    " ON CONFLICT(slug) DO UPDATE SET"
                    "   pending_turns=MAX(0, pending_turns - ?),"
                    "   last_review_at=?;",
                    (clean_slug, rev_ts, dec, rev_ts),
                )
            else:
                conn.execute(
                    "INSERT INTO bot_turn_counters"
                    " (slug, pending_turns, last_activity_at, last_review_at)"
                    " VALUES (?, 0, 0, ?)"
                    " ON CONFLICT(slug) DO UPDATE SET"
                    "   pending_turns=0,"
                    "   last_review_at=?;",
                    (clean_slug, rev_ts, rev_ts),
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return {
        "ok": True,
        "slug": clean_slug,
        "namespace": bot["namespace"],
        "role_desc": bot["role_desc"],
        "status": bot["status"],
        "pinned": bool(new_pinned),
        "author": clean_author,
        "content": masked,
        "etag": etag,
        "fencing_epoch": new_epoch,
        "updated_at": now_iso,
    }


def record_bot_domain_turn(
    namespace_or_slug: str,
    inc: int = 1,
    *,
    now: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Increment ``pending_turns`` for active bots matching ``slug`` or ``namespace``."""
    target = coerce_text(str(namespace_or_slug or "")).strip()
    if not target:
        return 0
    step = max(1, _coerce_int(inc, 1))
    now_val = float(now) if now is not None else time.time()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            rows = conn.execute(
                "SELECT slug FROM bots_registry"
                " WHERE status = 'active' AND (slug = ? OR namespace = ?);",
                (target, target),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO bot_turn_counters"
                    " (slug, pending_turns, last_activity_at, last_review_at)"
                    " VALUES (?, ?, ?, 0)"
                    " ON CONFLICT(slug) DO UPDATE SET"
                    "   pending_turns=pending_turns + excluded.pending_turns,"
                    "   last_activity_at=MAX(last_activity_at, excluded.last_activity_at);",
                    (str(r["slug"]), step, now_val),
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return len(rows)


def get_bot_turn_counter(slug: str, db_path: Optional[Path] = None) -> dict:
    """Return turn-counter metadata for ``slug`` (zeros when not yet tracked)."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return {"slug": "", "pending_turns": 0, "last_activity_at": 0.0, "last_review_at": 0.0}
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT slug, pending_turns, last_activity_at, last_review_at"
        " FROM bot_turn_counters WHERE slug = ?;",
        (clean_slug,),
    ).fetchone()
    if row is None:
        return {
            "slug": clean_slug,
            "pending_turns": 0,
            "last_activity_at": 0.0,
            "last_review_at": 0.0,
        }
    return {
        "slug": str(row["slug"]),
        "pending_turns": int(row["pending_turns"] or 0),
        "last_activity_at": float(row["last_activity_at"] or 0.0),
        "last_review_at": float(row["last_review_at"] or 0.0),
    }


def list_bots_due_for_brief_review(
    now: Optional[float] = None,
    *,
    turn_threshold: Optional[int] = None,
    idle_s: Optional[int] = None,
    min_idle_turns: Optional[int] = None,
    min_review_interval_s: Optional[int] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return active, unpinned bots satisfying turn-threshold or idle-debounce triggers."""
    cfg = _bots_cfg()
    now_val = float(now) if now is not None else time.time()
    t_thresh = max(
        1,
        int(turn_threshold)
        if turn_threshold is not None
        else _coerce_int(cfg.get("brief_review_turn_threshold"), 20),
    )
    idle_win = max(
        1,
        int(idle_s) if idle_s is not None else _coerce_int(cfg.get("brief_review_idle_s"), 900),
    )
    min_idle = max(
        1,
        int(min_idle_turns)
        if min_idle_turns is not None
        else _coerce_int(cfg.get("brief_review_min_idle_turns"), 3),
    )
    cooldown = max(
        0,
        int(min_review_interval_s)
        if min_review_interval_s is not None
        else _coerce_int(cfg.get("brief_review_cooldown_s"), 300),
    )
    sql = (
        "SELECT r.slug, r.namespace, r.role_desc, r.brief_budget_tokens,"
        " COALESCE(b.etag, '') AS current_etag,"
        " COALESCE(b.fencing_epoch, 0) AS fencing_epoch,"
        " COALESCE(c.pending_turns, 0) AS pending_turns,"
        " COALESCE(c.last_activity_at, 0.0) AS last_activity_at,"
        " COALESCE(c.last_review_at, 0.0) AS last_review_at"
        " FROM bots_registry r"
        " JOIN bot_turn_counters c ON c.slug = r.slug"
        " LEFT JOIN bot_briefs b ON b.slug = r.slug"
        " WHERE r.status = 'active'"
        "   AND COALESCE(b.pinned, 0) = 0"
        "   AND (COALESCE(c.last_review_at, 0.0) <= 0"
        "        OR (? - COALESCE(c.last_review_at, 0.0)) >= ?)"
        "   AND ("
        "       COALESCE(c.pending_turns, 0) >= ?"
        "       OR ("
        "           COALESCE(c.pending_turns, 0) >= ?"
        "           AND COALESCE(c.last_activity_at, 0.0) > 0"
        "           AND (? - COALESCE(c.last_activity_at, 0.0)) >= ?"
        "       )"
        "   )"
        " ORDER BY c.pending_turns DESC, r.slug ASC"
    )
    params: list[Any] = [now_val, cooldown, t_thresh, min_idle, now_val, idle_win]
    if limit is not None:
        lim = max(1, _coerce_int(limit, 1))
        sql += " LIMIT ?"
        params.append(lim)
    sql += ";"
    conn = get_swarm_store_conn(db_path)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Host leases (fencing)
# ============================================================================


def _scope_key(scope_raw: str) -> str:
    """Deterministic lease-key scope component: identifier scopes stay readable,
    path-like scopes fold to a short SHA-256 digest."""
    clean = sanitize_identifier(scope_raw)
    if clean:
        return clean
    return hashlib.sha256(scope_raw.encode("utf-8")).hexdigest()[:16]


def upsert_host_lease(
    bot_slug: str,
    host_id: str,
    scope_path: str,
    summary: str,
    *,
    fencing_token: int = 0,
    ttl_s: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Acquire/refresh a fenced lease ``<slug>:<scope>`` for a host.

    On an active lease (``expires_at > now``), the same ``host_id`` may refresh
    with an equal or higher ``fencing_token``; another host must supply a
    strictly higher token or receive ``lease_held`` / ``stale_epoch``. On an
    expired lease, the stored token advances monotonically to at least
    ``existing_token + 1`` so a returning zombie host cannot reclaim it.
    """
    clean_slug = sanitize_identifier(str(bot_slug or ""))
    if not clean_slug:
        return {"ok": False, "error": "INVALID_SLUG"}
    clean_host = coerce_text(str(host_id or "")).strip()
    if not clean_host:
        return {"ok": False, "error": "INVALID_HOST"}
    scope_raw = coerce_text(str(scope_path or "")).strip()
    if not scope_raw:
        return {"ok": False, "error": "INVALID_SCOPE"}
    clean_summary = coerce_text(mask_secrets(str(summary or ""))).strip()
    lease_key = clean_slug + ":" + _scope_key(scope_raw)
    token = int(fencing_token) if fencing_token is not None else 0
    ttl = int(ttl_s) if ttl_s is not None else _default_lease_ttl()
    now = time.time()
    expires_at = now + ttl

    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            bot_row = conn.execute(
                "SELECT status FROM bots_registry WHERE slug = ?;",
                (clean_slug,),
            ).fetchone()
            if bot_row is not None and str(bot_row["status"] or "active").lower() != "active":
                conn.rollback()
                return {
                    "ok": False,
                    "status": "bot_inactive",
                    "bot_status": str(bot_row["status"]),
                }
            existing = conn.execute(
                "SELECT host_id, fencing_token, expires_at FROM host_leases WHERE lease_key = ?;",
                (lease_key,),
            ).fetchone()
            effective_token = token
            if existing is not None:
                existing_host = str(existing["host_id"] or "")
                existing_token = int(existing["fencing_token"])
                existing_exp = float(existing["expires_at"])
                if existing_exp > now:
                    if token < existing_token or (
                        token == existing_token and clean_host != existing_host
                    ):
                        conn.rollback()
                        return {
                            "ok": False,
                            "status": "stale_epoch" if token < existing_token else "lease_held",
                            "holder_host": existing_host,
                            "current_fencing_token": existing_token,
                        }
                elif effective_token <= existing_token:
                    effective_token = existing_token + 1

            conn.execute(
                "INSERT INTO host_leases"
                " (lease_key, bot_slug, host_id, scope_path, fencing_token, expires_at, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(lease_key) DO UPDATE SET"
                "   bot_slug=excluded.bot_slug,"
                "   host_id=excluded.host_id,"
                "   scope_path=excluded.scope_path,"
                "   fencing_token=excluded.fencing_token,"
                "   expires_at=excluded.expires_at,"
                "   summary=excluded.summary;",
                (
                    lease_key,
                    clean_slug,
                    clean_host,
                    scope_raw,
                    effective_token,
                    expires_at,
                    clean_summary,
                ),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return {
        "ok": True,
        "status": "active",
        "lease_key": lease_key,
        "fencing_token": effective_token,
        "expires_at": expires_at,
    }


def get_active_leases(
    bot_slug: str = "",
    now: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """List leases with ``expires_at > now``, optionally scoped to one bot."""
    now_val = float(now) if now is not None else time.time()
    sql = (
        "SELECT lease_key, bot_slug, host_id, scope_path, fencing_token,"
        " expires_at, summary FROM host_leases WHERE expires_at > ?"
    )
    params: List[Any] = [now_val]
    if bot_slug and str(bot_slug).strip():
        sql += " AND bot_slug = ?"
        params.append(sanitize_identifier(str(bot_slug).strip()))
    sql += " ORDER BY lease_key;"
    conn = get_swarm_store_conn(db_path)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ============================================================================
# Host harness inventory (Edge-reported live harnesses)
# ============================================================================


_VALID_HARNESS_STATUSES: Dict[str, str] = {
    "online": "online",
    "degraded": "degraded",
    "offline": "offline",
    "stale": "stale",
}
_MAX_TRANSPORT_CHARS = 64


def _coerce_capabilities_json(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return coerce_text(mask_secrets(json.dumps(parsed, ensure_ascii=False)))
        return "{}"
    if isinstance(value, (dict, list)):
        try:
            return coerce_text(mask_secrets(json.dumps(value, ensure_ascii=False)))
        except (ValueError, TypeError):
            pass
    return "{}"


def _parse_capabilities(raw: Any) -> Any:
    try:
        return json.loads(str(raw or "{}"))
    except (ValueError, TypeError):
        return {}


def upsert_host_harnesses(
    host_id: str,
    harnesses: List[dict],
    *,
    last_seen: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Upsert the live-harness inventory reported by an Edge host; returns stored rows."""
    clean_host = coerce_text(str(host_id or "")).strip()
    if not clean_host:
        return []
    seen_default = coerce_text(str(last_seen or "")).strip() or _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    stored: list[dict] = []
    with _SWARM_LOCK, conn:
        for entry in harnesses or ():
            if not isinstance(entry, Mapping):
                continue
            harness_id = sanitize_identifier(str(entry.get("harness_id") or ""))
            transport = coerce_text(str(entry.get("transport") or "")).strip()
            if not harness_id or not transport:
                continue
            transport = transport[:_MAX_TRANSPORT_CHARS]
            status_key = coerce_text(str(entry.get("status") or "online")).strip().lower()
            clean_status = _VALID_HARNESS_STATUSES.get(status_key, "online")
            seen = coerce_text(str(entry.get("last_seen") or seen_default)).strip() or seen_default
            caps_json = _coerce_capabilities_json(
                entry.get("capabilities", entry.get("capabilities_json"))
            )
            conn.execute(
                "INSERT INTO host_harness_inventory"
                " (host_id, harness_id, transport, status, last_seen, capabilities_json)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(host_id, harness_id) DO UPDATE SET"
                "   transport=excluded.transport,"
                "   status=excluded.status,"
                "   last_seen=excluded.last_seen,"
                "   capabilities_json=excluded.capabilities_json;",
                (clean_host, harness_id, transport, clean_status, seen, caps_json),
            )
            stored.append(
                {
                    "host_id": clean_host,
                    "harness_id": harness_id,
                    "transport": transport,
                    "status": clean_status,
                    "last_seen": seen,
                    "capabilities": _parse_capabilities(caps_json),
                    "capabilities_json": caps_json,
                }
            )
    return stored


def list_host_harnesses(
    host_id: Optional[str] = None,
    *,
    stale_after_s: int = 120,
    now: Optional[datetime] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """List reported harnesses; rows idle beyond ``stale_after_s`` surface as ``stale``."""
    conn = get_swarm_store_conn(db_path)
    sql = (
        "SELECT host_id, harness_id, transport, status, last_seen, capabilities_json"
        " FROM host_harness_inventory"
    )
    params: List[Any] = []
    if host_id and str(host_id).strip():
        sql += " WHERE host_id = ?"
        params.append(coerce_text(str(host_id).strip()))
    sql += " ORDER BY host_id, harness_id;"
    now_dt = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    stale_after = max(0, _coerce_int(stale_after_s, 120))
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["capabilities"] = _parse_capabilities(item.get("capabilities_json"))
        age_s: Optional[float] = None
        try:
            seen_dt = datetime.fromisoformat(str(item.get("last_seen") or ""))
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            age_s = max(0.0, (now_dt - seen_dt).total_seconds())
        except ValueError:
            age_s = None
        item["age_s"] = age_s
        if age_s is not None and age_s > stale_after:
            item["status"] = "stale"
        out.append(item)
    return out


# ============================================================================
# Work orders persistence (Wave 2 REST backing store)
# ============================================================================


_MAX_ORDER_REASON_CHARS = 400


def _order_row_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    raw = item.pop("payload_json", None)
    payload: Dict[str, Any] = {}
    try:
        parsed = json.loads(str(raw or "{}"))
        if isinstance(parsed, dict):
            payload = parsed
    except (ValueError, TypeError):
        payload = {}
    out = dict(payload)
    out.update(item)
    return out


def _norm_order_status(fsm: Any, value: Any) -> str:
    norm = str(value or "").strip().upper()
    return {s: s for s in fsm.ALL_STATUSES}.get(norm, "")


def create_work_order(order_dict: dict, db_path: Optional[Path] = None) -> dict:
    """Persist a new work order; refuses to overwrite an existing ``order_id``."""
    d = order_dict if isinstance(order_dict, Mapping) else {}
    clean_id = sanitize_identifier(str(d.get("order_id") or ""))
    if not clean_id:
        return {"ok": False, "error": "INVALID_ORDER_ID"}
    fsm = _work_order_fsm()
    clean_status = _norm_order_status(fsm, d.get("status") or "QUEUED")
    if not clean_status:
        return {"ok": False, "error": "INVALID_STATUS"}
    bot_slug = sanitize_identifier(str(d.get("bot_slug") or ""))
    try:
        epoch = int(d.get("epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    try:
        payload_str = coerce_text(mask_secrets(json.dumps(dict(d), ensure_ascii=False)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "INVALID_PAYLOAD"}
    now_iso = _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK, conn:
        exists = conn.execute(
            "SELECT 1 FROM work_orders WHERE order_id = ?;", (clean_id,)
        ).fetchone()
        if exists is not None:
            return {"ok": False, "error": "ORDER_EXISTS", "order_id": clean_id}
        conn.execute(
            "INSERT INTO work_orders"
            " (order_id, bot_slug, epoch, status, payload_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (clean_id, bot_slug, epoch, clean_status, payload_str, now_iso, now_iso),
        )
    return {"ok": True, "order_id": clean_id, "status": clean_status, "epoch": epoch}


def get_work_order(order_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the stored order (columns + parsed payload) or None."""
    clean_id = sanitize_identifier(str(order_id or ""))
    if not clean_id:
        return None
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT order_id, bot_slug, epoch, status, payload_json, created_at, updated_at"
        " FROM work_orders WHERE order_id = ?;",
        (clean_id,),
    ).fetchone()
    return _order_row_dict(row) if row is not None else None


def list_work_orders(
    *,
    status: Optional[str] = None,
    bot_slug: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """List stored orders, optionally filtered by status (normalized) and/or bot."""
    sql = (
        "SELECT order_id, bot_slug, epoch, status, payload_json, created_at, updated_at"
        " FROM work_orders"
    )
    clauses: List[str] = []
    params: List[Any] = []
    if status and str(status).strip():
        clean_status = _norm_order_status(_work_order_fsm(), status)
        if not clean_status:
            return []
        clauses.append("status = ?")
        params.append(clean_status)
    if bot_slug and str(bot_slug).strip():
        clauses.append("bot_slug = ?")
        params.append(sanitize_identifier(str(bot_slug).strip()))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, order_id ASC LIMIT ?;"
    params.append(max(1, min(_coerce_int(limit, 100), 1000)))
    conn = get_swarm_store_conn(db_path)
    return [_order_row_dict(r) for r in conn.execute(sql, params).fetchall()]


def update_work_order_status(
    order_id: str,
    target_status: str,
    *,
    reason: str = "",
    fencing_epoch: Optional[int] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """CAS-guarded status transition enforcing the WorkOrder state machine.

    When ``fencing_epoch`` is supplied it must equal the stored epoch — a
    mismatch means a stale writer. ``PAUSED`` orders refuse ``FENCED``
    transitions unless ``force=True`` (operator lease-freeze).
    """
    clean_id = sanitize_identifier(str(order_id or ""))
    if not clean_id:
        return {"ok": False, "error": "INVALID_ORDER_ID"}
    fsm = _work_order_fsm()
    target = _norm_order_status(fsm, target_status)
    if not target:
        return {"ok": False, "error": "INVALID_STATUS"}
    clean_reason = coerce_text(mask_secrets(str(reason or ""))).strip()[:_MAX_ORDER_REASON_CHARS]
    now_iso = _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            row = conn.execute(
                "SELECT order_id, epoch, status, payload_json FROM work_orders WHERE order_id = ?;",
                (clean_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"ok": False, "error": "ORDER_NOT_FOUND", "order_id": clean_id}
            current = str(row["status"] or "")
            cur_epoch = _coerce_int(row["epoch"], 0)
            if fencing_epoch is not None and _coerce_int(fencing_epoch, -1) != cur_epoch:
                conn.rollback()
                return {
                    "ok": False,
                    "error": "EPOCH_MISMATCH",
                    "order_id": clean_id,
                    "current_epoch": cur_epoch,
                }
            if (
                current == fsm.WorkOrderStatus.PAUSED
                and target == fsm.WorkOrderStatus.FENCED
                and not force
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "status": current,
                    "reason": "paused_lease_frozen",
                    "order_id": clean_id,
                }
            if current in fsm.TERMINAL_STATUSES:
                conn.rollback()
                return {
                    "ok": False,
                    "status": current,
                    "reason": "order_terminal",
                    "order_id": clean_id,
                }
            if not fsm.can_transition(current, target):
                conn.rollback()
                return {
                    "ok": False,
                    "status": current,
                    "reason": "illegal_transition",
                    "from": current,
                    "to": target,
                    "order_id": clean_id,
                }
            payload: Dict[str, Any] = {}
            if row["payload_json"]:
                try:
                    parsed = json.loads(str(row["payload_json"]))
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            if clean_reason:
                payload["status_reason"] = clean_reason
            new_epoch = cur_epoch + 1
            conn.execute(
                "UPDATE work_orders SET status = ?, epoch = ?, payload_json = ?, updated_at = ?"
                " WHERE order_id = ?;",
                (
                    target,
                    new_epoch,
                    coerce_text(mask_secrets(json.dumps(payload, ensure_ascii=False))),
                    now_iso,
                    clean_id,
                ),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return {
        "ok": True,
        "order_id": clean_id,
        "status": target,
        "previous_status": current,
        "epoch": new_epoch,
        "fencing_epoch": new_epoch,
        "reason": clean_reason,
        "updated_at": now_iso,
    }


def list_expired_work_orders(
    now: Optional[datetime] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Non-terminal, non-PAUSED orders whose bot holds an expired host lease.

    These are the rows a lease-expiry sweep may fence; ``PAUSED`` orders are
    lease-frozen and deliberately excluded alongside terminal statuses.
    """
    fsm = _work_order_fsm()
    if isinstance(now, datetime):
        now_dt = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        now_ts = now_dt.timestamp()
    else:
        now_ts = time.time()
    skipped = sorted({fsm.WorkOrderStatus.PAUSED} | fsm.TERMINAL_STATUSES)
    placeholders = ", ".join("?" for _ in skipped)
    conn = get_swarm_store_conn(db_path)
    rows = conn.execute(
        "SELECT w.order_id, w.bot_slug, w.epoch, w.status, w.payload_json,"
        " w.created_at, w.updated_at,"
        " l.lease_key, l.host_id, l.scope_path, l.fencing_token, l.expires_at"
        " FROM work_orders w"
        " JOIN host_leases l ON l.bot_slug = w.bot_slug"
        f" WHERE w.status NOT IN ({placeholders})"
        " AND l.expires_at <= ?"
        " ORDER BY w.order_id, l.lease_key;",
        (*skipped, now_ts),
    ).fetchall()
    return [_order_row_dict(r) for r in rows]


# ============================================================================
# Fan-out payloads
# ============================================================================


def list_active_brief_payloads(db_path: Optional[Path] = None) -> list[dict]:
    """Join ``bots_registry`` + ``bot_briefs`` for every ``active`` bot."""
    conn = get_swarm_store_conn(db_path)
    rows = conn.execute(
        "SELECT r.slug AS slug, r.namespace AS namespace, r.role_desc AS role_desc,"
        " b.etag AS etag, b.fencing_epoch AS fencing_epoch, b.content AS content,"
        " b.updated_at AS updated_at"
        " FROM bots_registry r"
        " JOIN bot_briefs b ON b.slug = r.slug"
        " WHERE r.status = 'active'"
        " ORDER BY r.slug;",
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Agent-to-agent channels (isolated data-only bus)
# ============================================================================
#
# Channel rows and messages are pure data: no code path in this module parses a
# message body into ``work_orders``/``host_leases`` mutations or Edge control
# frames. The only tables touched below are ``channels``/``channel_messages``.


_VALID_CHANNEL_ACL_MODES: Dict[str, str] = {
    "participants": "participants",
    "open": "open",
}
_MAX_CHANNEL_NAME_CHARS = 128
_MAX_CHANNEL_TOPIC_CHARS = 512
_MAX_CHANNEL_MESSAGE_CHARS = 8000
_MAX_CHANNEL_METADATA_CHARS = 4000
_MAX_CHANNEL_PARTICIPANTS = 256
_CHANNEL_LIST_SCAN_CAP = 1000
_UNTRUSTED_CHANNEL_TAG = "untrusted_channel_message"


def _parse_participants(raw: Any) -> List[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(s) for s in parsed if sanitize_identifier(str(s or ""))]


def _channel_row_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["participants"] = _parse_participants(item.get("participants_json"))
    return item


def _message_row_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["untrusted"] = bool(int(item.get("untrusted") or 0))
    try:
        parsed = json.loads(str(item.get("metadata_json") or "{}"))
        item["metadata"] = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        item["metadata"] = {}
    return item


def create_channel(
    channel_id: str,
    name: str,
    *,
    topic: str = "",
    participants: Optional[List[str]] = None,
    acl_mode: str = "participants",
    created_by: str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """Create a data-only coordination channel; refuses duplicate ``channel_id``."""
    clean_id = sanitize_identifier(str(channel_id or ""))
    if not clean_id:
        return {"ok": False, "error": "INVALID_CHANNEL_ID"}
    clean_name = coerce_text(mask_secrets(str(name or ""))).strip()[:_MAX_CHANNEL_NAME_CHARS]
    if not clean_name:
        return {"ok": False, "error": "INVALID_NAME"}
    clean_acl = _VALID_CHANNEL_ACL_MODES.get(
        coerce_text(str(acl_mode or "participants")).strip().lower() or "participants"
    )
    if clean_acl is None:
        return {"ok": False, "error": "INVALID_ACL_MODE"}
    clean_topic = coerce_text(mask_secrets(str(topic or ""))).strip()[:_MAX_CHANNEL_TOPIC_CHARS]
    clean_creator = sanitize_origin_claim(str(created_by or ""))
    clean_participants: List[str] = []
    for entry in participants or ():
        slug = sanitize_identifier(str(entry or ""))
        if slug and slug not in clean_participants:
            clean_participants.append(slug)
        if len(clean_participants) >= _MAX_CHANNEL_PARTICIPANTS:
            break
    participants_json = json.dumps(clean_participants, ensure_ascii=False)
    now_iso = _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK, conn:
        exists = conn.execute(
            "SELECT 1 FROM channels WHERE channel_id = ?;", (clean_id,)
        ).fetchone()
        if exists is not None:
            return {"ok": False, "error": "CHANNEL_EXISTS", "channel_id": clean_id}
        conn.execute(
            "INSERT INTO channels"
            " (channel_id, name, topic, participants_json, acl_mode,"
            "  created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (
                clean_id,
                clean_name,
                clean_topic,
                participants_json,
                clean_acl,
                clean_creator,
                now_iso,
                now_iso,
            ),
        )
    return {
        "ok": True,
        "channel_id": clean_id,
        "name": clean_name,
        "topic": clean_topic,
        "participants": clean_participants,
        "acl_mode": clean_acl,
        "created_by": clean_creator,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def get_channel(channel_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the channel row (with parsed ``participants``) or None."""
    clean_id = sanitize_identifier(str(channel_id or ""))
    if not clean_id:
        return None
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT channel_id, name, topic, participants_json, acl_mode,"
        " created_by, created_at, updated_at FROM channels WHERE channel_id = ?;",
        (clean_id,),
    ).fetchone()
    return _channel_row_dict(row) if row is not None else None


def list_channels(
    *,
    participant: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """List channels, optionally filtered to those containing ``participant``."""
    lim = max(1, min(_coerce_int(limit, 100), 500))
    conn = get_swarm_store_conn(db_path)
    rows = conn.execute(
        "SELECT channel_id, name, topic, participants_json, acl_mode,"
        " created_by, created_at, updated_at FROM channels"
        " ORDER BY created_at ASC, channel_id ASC LIMIT ?;",
        (_CHANNEL_LIST_SCAN_CAP,),
    ).fetchall()
    out = [_channel_row_dict(r) for r in rows]
    if participant is not None and str(participant).strip():
        clean_p = sanitize_identifier(str(participant).strip())
        out = [c for c in out if clean_p and clean_p in c["participants"]]
    return out[:lim]


def post_channel_message(
    channel_id: str,
    sender_slug: str,
    content: str,
    *,
    sender_origin: str = "",
    untrusted: bool = True,
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Append a message to a channel under the participant ACL.

    ``untrusted=True`` (default for external/cross-agent payloads) stores the
    body inside a canonical ``<untrusted_channel_message>`` boundary tag; every
    stored body is secret-masked. ``acl_mode='participants'`` channels reject
    senders outside the participants list (``ACL_DENIED``); an empty list or
    ``acl_mode='open'`` admits any valid sender.
    """
    clean_chan = sanitize_identifier(str(channel_id or ""))
    if not clean_chan:
        return {"ok": False, "error": "INVALID_CHANNEL_ID"}
    clean_sender = sanitize_identifier(str(sender_slug or ""))
    if not clean_sender:
        return {"ok": False, "error": "INVALID_SENDER"}
    raw_content = str(content or "")
    if not raw_content.strip():
        return {"ok": False, "error": "EMPTY_CONTENT"}
    if len(raw_content) > _MAX_CHANNEL_MESSAGE_CHARS:
        return {"ok": False, "error": "MESSAGE_TOO_LARGE"}
    clean_origin = sanitize_origin_claim(str(sender_origin or ""))
    metadata_json = "{}"
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            return {"ok": False, "error": "INVALID_METADATA"}
        try:
            dumped = json.dumps(dict(metadata), ensure_ascii=False)
        except (TypeError, ValueError):
            return {"ok": False, "error": "INVALID_METADATA"}
        if len(dumped) > _MAX_CHANNEL_METADATA_CHARS:
            return {"ok": False, "error": "INVALID_METADATA"}
        metadata_json = coerce_text(mask_secrets(dumped))
    if untrusted:
        stored_content = wrap_untrusted_content(
            raw_content,
            tag=_UNTRUSTED_CHANNEL_TAG,
            sender=clean_sender,
            channel=clean_chan,
            origin=clean_origin,
        )
    else:
        stored_content = coerce_text(mask_secrets(raw_content))
    message_id = "cm_" + uuid.uuid4().hex
    now_iso = _utc_now_iso()
    conn = get_swarm_store_conn(db_path)
    with _SWARM_LOCK:
        if conn.in_transaction:
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            chan = conn.execute(
                "SELECT channel_id, participants_json, acl_mode"
                " FROM channels WHERE channel_id = ?;",
                (clean_chan,),
            ).fetchone()
            if chan is None:
                conn.rollback()
                return {"ok": False, "error": "CHANNEL_NOT_FOUND", "channel_id": clean_chan}
            if str(chan["acl_mode"] or "participants") == "participants":
                plist = _parse_participants(chan["participants_json"])
                if plist and clean_sender not in plist:
                    conn.rollback()
                    return {"ok": False, "error": "ACL_DENIED", "channel_id": clean_chan}
            conn.execute(
                "INSERT INTO channel_messages"
                " (message_id, channel_id, sender_slug, sender_origin, content,"
                "  untrusted, metadata_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    message_id,
                    clean_chan,
                    clean_sender,
                    clean_origin,
                    stored_content,
                    int(bool(untrusted)),
                    metadata_json,
                    now_iso,
                ),
            )
            conn.execute(
                "UPDATE channels SET updated_at = ? WHERE channel_id = ?;",
                (now_iso, clean_chan),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    return {
        "ok": True,
        "message_id": message_id,
        "channel_id": clean_chan,
        "sender_slug": clean_sender,
        "sender_origin": clean_origin,
        "content": stored_content,
        "untrusted": bool(untrusted),
        "metadata": json.loads(metadata_json),
        "created_at": now_iso,
    }


def list_channel_messages(
    channel_id: str,
    *,
    limit: int = 50,
    before: Optional[str] = None,
    after: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return up to ``limit`` channel messages in chronological order.

    Default page is the most recent ``limit`` messages; ``before``/``after``
    are ``created_at`` cursors for paging older/newer ranges.
    """
    clean_chan = sanitize_identifier(str(channel_id or ""))
    if not clean_chan:
        return []
    lim = max(1, min(_coerce_int(limit, 50), 500))
    before_ts = coerce_text(str(before or "")).strip() if before is not None else ""
    after_ts = coerce_text(str(after or "")).strip() if after is not None else ""
    sql = (
        "SELECT message_id, channel_id, sender_slug, sender_origin, content,"
        " untrusted, metadata_json, created_at"
        " FROM channel_messages WHERE channel_id = ?"
    )
    params: List[Any] = [clean_chan]
    if before_ts:
        sql += " AND created_at < ?"
        params.append(before_ts)
    if after_ts:
        sql += " AND created_at > ?"
        params.append(after_ts)
    fetch_newest_first = not after_ts
    sql += (
        " ORDER BY created_at DESC, rowid DESC LIMIT ?;"
        if fetch_newest_first
        else " ORDER BY created_at ASC, rowid ASC LIMIT ?;"
    )
    params.append(lim)
    conn = get_swarm_store_conn(db_path)
    rows = [_message_row_dict(r) for r in conn.execute(sql, params).fetchall()]
    if fetch_newest_first:
        rows.reverse()
    return rows

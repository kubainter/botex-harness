# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""BoteX Cloud Soul — versioned swarm identity store (Plan 035).

Single SQLite WAL store holding the swarm's bot registry, per-bot briefs
(souls) with etag/fencing metadata, host leases with fencing tokens, and
durable work orders. Domain knowledge search stays in the Memory Vault's
``index.db`` — this store intentionally ships ZERO FTS5 tables.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import threading
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

__all__ = [
    "SWARM_SCHEMA_VERSION",
    "get_swarm_store_conn",
    "migrate_swarm_store",
    "close_swarm_store_dbs",
    "register_bot",
    "get_bot",
    "list_bots",
    "get_bot_brief",
    "upsert_bot_brief",
    "upsert_host_lease",
    "get_active_leases",
    "list_active_brief_payloads",
]

SWARM_SCHEMA_VERSION = 1


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


def migrate_swarm_store(conn: sqlite3.Connection) -> None:
    """Apply in-place schema migrations gated by ``PRAGMA user_version``; idempotent."""
    row = conn.execute("PRAGMA user_version;").fetchone()
    current = int(row[0]) if row else 0
    if current < 1:
        conn.executescript(_SCHEMA_V1_SQL)
        conn.execute("PRAGMA user_version = " + str(int(SWARM_SCHEMA_VERSION)) + ";")
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
    "dormant": "dormant",
    "disabled": "disabled",
}
_MIN_BRIEF_BUDGET_TOKENS = 32
_MAX_BRIEF_BUDGET_TOKENS = 2000
_MAX_ROLE_DESC_CHARS = 500


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
    clean_ns = coerce_text(str(namespace or "")).strip()
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


# ============================================================================
# Bot briefs (souls)
# ============================================================================


def get_bot_brief(slug: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Return the stored brief row for ``slug`` or None."""
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return None
    conn = get_swarm_store_conn(db_path)
    row = conn.execute(
        "SELECT slug, content, etag, fencing_epoch, updated_at FROM bot_briefs WHERE slug = ?;",
        (clean_slug,),
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_bot_brief(
    slug: str,
    content: str,
    *,
    db_path: Optional[Path] = None,
) -> dict:
    """Validate, mask and store a bot brief; bumps ``fencing_epoch`` per write.

    Rejects with a structured error when the bot is unknown
    (``BOT_NOT_FOUND``), the content trips the prompt-injection heuristics
    (``INJECTION_DETECTED``), or the secret-masked payload exceeds the bot's
    ``brief_budget_tokens`` (``BRIEF_TOO_LARGE``; tokens estimated as
    ``ceil(len/4)``).
    """
    clean_slug = sanitize_identifier(str(slug or ""))
    if not clean_slug:
        return {"ok": False, "error": "INVALID_SLUG"}

    raw = str(content or "")
    if _has_injection_pattern(raw):
        return {"ok": False, "error": "INJECTION_DETECTED"}
    masked = coerce_text(mask_secrets(raw))
    etag = hashlib.sha256(masked.encode("utf-8")).hexdigest()[:32]
    now_iso = _utc_now_iso()

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
                "SELECT fencing_epoch FROM bot_briefs WHERE slug = ?;",
                (clean_slug,),
            ).fetchone()
            prev_epoch = _coerce_int(existing["fencing_epoch"], 0) if existing is not None else 0
            new_epoch = prev_epoch + 1

            conn.execute(
                "INSERT INTO bot_briefs (slug, content, etag, fencing_epoch, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET"
                "   content=excluded.content,"
                "   etag=excluded.etag,"
                "   fencing_epoch=excluded.fencing_epoch,"
                "   updated_at=excluded.updated_at;",
                (clean_slug, masked, etag, new_epoch, now_iso),
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
        "content": masked,
        "etag": etag,
        "fencing_epoch": new_epoch,
        "updated_at": now_iso,
    }


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

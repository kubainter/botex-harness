# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""BoteX BlackBox (``botex.penal.v1``) -- isolated cross-agent incident ledger.

Dedicated SQLite registry for peer-reported and engine-logged violations
(SEC_01, SKILL_01, SCOPE_01, HAL_01, LOOP_01). Deliberately separated from
the ``ecc.memory.v1`` vault: unverified allegations must never flow through
lesson recall, confidence scoring, or curator promotion into injected
context.

Security invariants:
- ``BOTEX_PENAL_DB_PATH`` is re-read per call (never cached at import) so
  tests can ``monkeypatch.setenv`` and operators can relocate the vault.
- ``format_*`` helpers emit aggregate-only text: normalized actor ids,
  violation codes, counts, and static descriptions -- never
  ``evidence_snippet`` or ``skill_recipe``. Callers control those fields;
  echoing them into future worker prompts would be a stored
  prompt-injection channel.
- ``sqlite3.Connection.__exit__`` commits/rolls back but never closes, so
  every connection is wrapped in ``contextlib.closing``.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any

from botex.server.config import resolve_workspace
from botex.tools.security import mask_secrets

# Static lookup tables are the CodeQL-safe way to echo caller input: callers
# get back the immutable dict value, never their own (possibly hostile) text.
VIOLATION_CODES: dict[str, str] = {
    "SEC_01": "SEC_01",
    "SKILL_01": "SKILL_01",
    "SCOPE_01": "SCOPE_01",
    "HAL_01": "HAL_01",
    "LOOP_01": "LOOP_01",
}
VIOLATION_DESCRIPTIONS: dict[str, str] = {
    "SEC_01": "Security boundary or secret-access violation",
    "SKILL_01": "Skill contract or verification bypass / test theater",
    "SCOPE_01": "Minimal-diff or file-scope violation",
    "HAL_01": "Hallucinated tool or non-existent file path",
    "LOOP_01": "Repetitive compilation/test failure loop (3+ stagnant turns)",
}
SEVERITIES: dict[str, str] = {"WARN": "WARN", "REJECT": "REJECT", "CRITICAL": "CRITICAL"}
ENFORCEMENT_ACTIONS: dict[str, str] = {
    "PENALTY_LOGGED": "PENALTY_LOGGED",
    "HANDOFF_REJECTED": "HANDOFF_REJECTED",
    "ESCALATED": "ESCALATED",
}
SOURCES: dict[str, str] = {"engine": "engine", "peer": "peer"}

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS agent_incidents (
    incident_id TEXT PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    actor_agent TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'peer',
    workspace_id TEXT NOT NULL,
    skill_recipe TEXT,
    violation_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    evidence_snippet TEXT,
    enforcement_action TEXT NOT NULL
);
"""
_SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_incidents_actor_ws "
    "ON agent_incidents (actor_agent, workspace_id, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_incidents_ws_ts ON agent_incidents (workspace_id, timestamp);",
)
_INCIDENT_FIELDS = (
    "incident_id",
    "timestamp",
    "actor_agent",
    "reported_by",
    "source",
    "workspace_id",
    "skill_recipe",
    "violation_code",
    "severity",
    "evidence_snippet",
    "enforcement_action",
)

_DB_LOCK = threading.Lock()
_ACTOR_STRIP = re.compile(r"[^a-z0-9_.:@/-]")
_RECIPE_STRIP = re.compile(r"[^a-zA-Z0-9_.-]")


def _resolve_db_path() -> Path:
    env_path = os.environ.get("BOTEX_PENAL_DB_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return Path(resolve_workspace("")) / ".botex" / "penal_vault.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA_TABLE)
    for stmt in _SCHEMA_INDEXES:
        conn.execute(stmt)


def normalize_actor_id(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw or "")).strip().lower()
    cleaned = _ACTOR_STRIP.sub("", text)[:64]
    if not cleaned:
        raise ValueError("actor identity must contain at least one valid character")
    return cleaned


def normalize_workspace_id(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text:
        return ""
    posix_text = text.replace("\\", "/")
    if posix_text.startswith("edge://"):
        return posix_text.rstrip("/")
    return Path(posix_text).as_posix().rstrip("/")


def _clean_recipe(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    return _RECIPE_STRIP.sub("", text)[:64]


def _lookup(
    table: dict[str, str],
    raw: str,
    field: str,
    *,
    upper: bool,
    default: str = "",
) -> str:
    key = unicodedata.normalize("NFKC", str(raw or "")).strip()
    key = key.upper() if upper else key.lower()
    if not key:
        key = default
    if not key or key not in table:
        raise ValueError(f"Unknown {field}: {key!r}")
    return table[key]


def _windowed_clause() -> str:
    # Fresh-window rows count always; CRITICAL and SEC_01 stay visible for a
    # fixed 30-day grace period so severe incidents outlive the decay window.
    return (
        "((timestamp >= datetime('now', ?)) OR "
        "((severity = 'CRITICAL' OR violation_code = 'SEC_01') "
        "AND timestamp >= datetime('now', '-30 days')))"
    )


def record_incident(
    actor_agent: str,
    reported_by: str,
    violation_code: str,
    severity: str = "REJECT",
    workspace_id: str = "",
    skill_recipe: str = "",
    evidence_snippet: str = "",
    enforcement_action: str = "PENALTY_LOGGED",
    source: str = "peer",
) -> dict[str, Any]:
    actor = normalize_actor_id(actor_agent)
    reporter = normalize_actor_id(reported_by)
    if actor == reporter:
        raise ValueError("Self-reporting violations is not allowed")
    code = _lookup(VIOLATION_CODES, violation_code, "violation_code", upper=True)
    sev = _lookup(SEVERITIES, severity, "severity", upper=True, default="REJECT")
    action = _lookup(
        ENFORCEMENT_ACTIONS,
        enforcement_action,
        "enforcement_action",
        upper=True,
        default="PENALTY_LOGGED",
    )
    src = _lookup(SOURCES, source, "source", upper=False, default="peer")
    if str(workspace_id or "").strip():
        ws = normalize_workspace_id(workspace_id)
    else:
        ws = normalize_workspace_id(resolve_workspace(""))
    recipe_clean = _clean_recipe(skill_recipe)
    snippet = mask_secrets(str(evidence_snippet or ""))[:2000]

    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "ok": True,
        "actor_agent": actor,
        "reported_by": reporter,
        "source": src,
        "workspace_id": ws,
        "skill_recipe": recipe_clean,
        "violation_code": code,
        "severity": sev,
        "enforcement_action": action,
    }
    with _DB_LOCK:
        with contextlib.closing(_connect(db_path)) as conn:
            with conn:
                _init_schema(conn)
                row = conn.execute(
                    "SELECT incident_id FROM agent_incidents "
                    "WHERE reported_by = ? AND actor_agent = ? AND violation_code = ? "
                    "AND workspace_id = ? AND date(timestamp) = date('now') LIMIT 1",
                    (reporter, actor, code, ws),
                ).fetchone()
                if row is not None:
                    return {"incident_id": row["incident_id"], "deduplicated": True, **receipt}
                incident_id = ""
                for attempt in range(3):
                    try:
                        incident_id = f"inc_{secrets.token_hex(6)}"
                        conn.execute(
                            "INSERT INTO agent_incidents (incident_id, actor_agent, "
                            "reported_by, source, workspace_id, skill_recipe, "
                            "violation_code, severity, evidence_snippet, "
                            "enforcement_action) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                incident_id,
                                actor,
                                reporter,
                                src,
                                ws,
                                recipe_clean,
                                code,
                                sev,
                                snippet,
                                action,
                            ),
                        )
                        break
                    except sqlite3.IntegrityError:
                        if attempt == 2:
                            raise
                return {"incident_id": incident_id, "deduplicated": False, **receipt}


def get_rap_sheet(
    actor_agent: str = "",
    workspace_id: str = "",
    limit: int = 20,
    window_days: int = 30,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    window_days = max(1, min(int(window_days), 365))
    norm_actor = normalize_actor_id(actor_agent) if str(actor_agent or "").strip() else ""
    norm_ws = normalize_workspace_id(workspace_id)
    result: dict[str, Any] = {
        "ok": True,
        "actor_agent": norm_actor,
        "workspace_id": norm_ws,
        "window_days": window_days,
        "total_incidents": 0,
        "counts_by_code": {},
        "counts_by_severity": {},
        "incidents": [],
    }
    db_path = _resolve_db_path()
    if not db_path.exists():
        return result
    clauses = ["timestamp >= datetime('now', ?)"]
    params: list[Any] = [f"-{window_days} days"]
    if norm_actor:
        clauses.append("actor_agent = ?")
        params.append(norm_actor)
    if norm_ws:
        clauses.append("workspace_id = ?")
        params.append(norm_ws)
    sql = (
        "SELECT "
        + ", ".join(_INCIDENT_FIELDS)
        + " FROM agent_incidents WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp DESC, rowid DESC LIMIT ?"
    )
    params.append(limit)
    with _DB_LOCK:
        with contextlib.closing(_connect(db_path)) as conn:
            with conn:
                _init_schema(conn)
                rows = conn.execute(sql, params).fetchall()
    counts_by_code: dict[str, int] = {}
    counts_by_severity: dict[str, int] = {}
    incidents = []
    for row in rows:
        item = {field: row[field] for field in _INCIDENT_FIELDS}
        incidents.append(item)
        counts_by_code[item["violation_code"]] = counts_by_code.get(item["violation_code"], 0) + 1
        counts_by_severity[item["severity"]] = counts_by_severity.get(item["severity"], 0) + 1
    result["total_incidents"] = len(incidents)
    result["counts_by_code"] = counts_by_code
    result["counts_by_severity"] = counts_by_severity
    result["incidents"] = incidents
    return result


def _fetch_windowed(
    db_path: Path,
    sql_where: str,
    params: list[Any],
) -> list[sqlite3.Row]:
    with _DB_LOCK:
        with contextlib.closing(_connect(db_path)) as conn:
            with conn:
                _init_schema(conn)
                return conn.execute(sql_where, params).fetchall()


def format_rap_sheet_warning(
    actor_agent: str,
    workspace_id: str,
    min_count: int = 2,
    window_days: int = 14,
) -> str:
    """Aggregate-only advisory string for prompt injection; "" when silent."""
    try:
        actor = normalize_actor_id(actor_agent)
        ws = normalize_workspace_id(workspace_id)
    except ValueError:
        return ""
    if not ws:
        return ""
    window_days = max(1, min(int(window_days), 365))
    min_count = max(1, int(min_count))
    db_path = _resolve_db_path()
    if not db_path.exists():
        return ""
    try:
        rows = _fetch_windowed(
            db_path,
            "SELECT actor_agent, violation_code, severity FROM agent_incidents "
            "WHERE actor_agent = ? AND workspace_id = ? AND " + _windowed_clause(),
            [actor, ws, f"-{window_days} days"],
        )
    except (OSError, sqlite3.Error):
        return ""  # advisory path must never break task execution
    counts: dict[str, int] = {}
    critical_codes: set[str] = set()
    for row in rows:
        rcode = str(row["violation_code"])
        if rcode not in VIOLATION_CODES:
            continue  # foreign-written row: never echo unknown codes into prompts
        counts[rcode] = counts.get(rcode, 0) + 1
        if str(row["severity"]) == "CRITICAL":
            critical_codes.add(rcode)
    triggered = sorted(
        (
            (c, n)
            for c, n in counts.items()
            if n >= min_count or c == "SEC_01" or c in critical_codes
        ),
        key=lambda item: (-item[1], item[0]),
    )
    if not triggered:
        return ""
    total = sum(n for _, n in triggered)
    summary = f"{total} recorded incident(s): " + ", ".join(f"{n}x {c}" for c, n in triggered)
    details = "; ".join(VIOLATION_DESCRIPTIONS[c] for c, _ in triggered)
    return (
        f"[BLACKBOX RAP SHEET]: In this workspace ({ws}), {actor} has "
        f"{summary} ({details}). Enforce skill contracts strictly; "
        "Cross-Review will audit carefully."
    )


def format_audit_hint(
    workspace_id: str,
    target_actor: str = "",
    window_days: int = 14,
) -> str:
    """Aggregate-only cross-review hint for a workspace; "" when silent."""
    try:
        ws = normalize_workspace_id(workspace_id)
        actor = normalize_actor_id(target_actor) if str(target_actor or "").strip() else ""
    except ValueError:
        return ""
    if not ws:
        return ""
    window_days = max(1, min(int(window_days), 365))
    db_path = _resolve_db_path()
    if not db_path.exists():
        return ""
    clauses = ["workspace_id = ?", _windowed_clause()]
    params: list[Any] = [ws, f"-{window_days} days"]
    if actor:
        clauses.append("actor_agent = ?")
        params.append(actor)
    try:
        rows = _fetch_windowed(
            db_path,
            "SELECT actor_agent, violation_code FROM agent_incidents WHERE "
            + " AND ".join(clauses),
            params,
        )
    except (OSError, sqlite3.Error):
        return ""
    agg: dict[tuple[str, str], int] = {}
    for row in rows:
        rcode = str(row["violation_code"])
        if rcode not in VIOLATION_CODES:
            continue
        try:
            # Stored actor ids are re-normalized on read: a foreign writer with
            # file access could otherwise smuggle whitespace/instructions into
            # prompt-bound output.
            actor_display = normalize_actor_id(row["actor_agent"])
        except ValueError:
            continue
        key = (actor_display, rcode)
        agg[key] = agg.get(key, 0) + 1
    if not agg:
        return ""
    ordered = sorted(agg.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    items = "; ".join(f"{a}: {n}x {c} ({VIOLATION_DESCRIPTIONS[c]})" for (a, c), n in ordered)
    return (
        f"[AUDIT HINT]: Recent workspace incidents — {items}. "
        "Prioritize verifying tests, security boundaries, and diff scope."
    )

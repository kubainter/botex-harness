# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Autonomous Knowledge Graph Curator Service
=================================================
Option A: Deterministic + Multi-Tier LLM Batch Curation with Zero Recipe Pollution.
Guarantees:
1. Non-destructive: Never alters original user notes or technical conclusions.
2. Zero Memory Vault Bloat: Updates exactly ONE living beacon note (curator_health_status.md).
3. Concurrency Protection: Lockfile (.botex/curator/curator.lock) + Active task deferral.
4. Multi-Tier Fallback: NVIDIA Nemotron -> Local Bielik/Ollama -> Deterministic Heuristics.
5. Persistent Audit Trail: Stores run records in .botex/curator/runs/ and status in .botex/curator/status.json.
6. Telegram Notifications: Dispatches run telemetry to Telegram if configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from botex import memory, obsidian
from botex.config import resolve_workspace
from botex.ops.curator.prompts import (
    COMPILED_TRUTH_SYSTEM_PROMPT,
    CURATOR_SYSTEM_PROMPT,
    build_compiled_truth_user_prompt,
    build_curator_user_prompt,
)
from botex.ops.curator.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Default model configurations
DEFAULT_PRIMARY_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_LOCAL_MODEL = "bielik:latest"
LOCK_MAX_STALE_SECONDS = 1800.0  # 30 minutes


@dataclass
class CuratorResult:
    run_id: str
    status: str  # "COMPLETED", "LOCKED", "DEFERRED", "FAILED"
    is_healthy: bool
    total_nodes: int
    total_edges: int
    orphan_count: int
    broken_count: int
    broken_links: List[Dict[str, str]]
    tier_used: str  # "nvidia-nemotron", "local-llm", "deterministic", "none"
    duration_seconds: float
    summary: str
    beacon_path: str
    telegram_status: Dict[str, Any]
    error: Optional[str] = None
    ledger_count: int = 0
    severity: str = "ok"
    degraded_reasons: List[str] = field(default_factory=list)
    orphan_ratio: float = 0.0
    links_applied: int = 0
    batches_processed: int = 0
    truth_clusters_compiled: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not process:
                return False
            kernel32.CloseHandle(process)
            return True
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class CuratorLock:
    """File-based advisory lock for Curator processes."""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self.acquired = False

    def acquire(self, run_id: str, force: bool = False) -> bool:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()

        if self.lock_file.exists() and not force:
            try:
                data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
                started_at = float(data.get("started_at", 0))
                heartbeat_at = float(data.get("heartbeat_at") or started_at or 0)

                # Never break a lock held by a live PID, even past LOCK_MAX_STALE_SECONDS.
                if pid > 0 and _is_pid_alive(pid):
                    logger.warning(
                        "Curator lock active by PID %s (started %ss ago, heartbeat %ss ago)",
                        pid,
                        int(now - started_at),
                        int(now - heartbeat_at),
                    )
                    return False
                logger.info("Breaking stale or orphaned curator lock from dead PID %s", pid)
            except Exception:
                pass

        payload = {
            "pid": os.getpid(),
            "started_at": now,
            "heartbeat_at": now,
            "run_id": run_id,
        }
        self.lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.acquired = True
        return True

    def heartbeat(self) -> None:
        """Refresh ``heartbeat_at`` in the lockfile while held by this process."""
        if not self.acquired or not self.lock_file.exists():
            return
        try:
            data = json.loads(self.lock_file.read_text(encoding="utf-8"))
            if int(data.get("pid", 0)) != os.getpid():
                return
            data["heartbeat_at"] = time.time()
            tmp = self.lock_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.lock_file)
        except Exception:
            pass

    def release(self, force: bool = False) -> None:
        if not self.acquired and not force:
            return
        try:
            if self.lock_file.exists():
                try:
                    data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                    # Only unlink if held by current process PID unless forced
                    if force or int(data.get("pid", 0)) == os.getpid():
                        self.lock_file.unlink()
                except Exception:
                    if force or self.acquired:
                        self.lock_file.unlink()
        except Exception as exc:
            logger.warning("Failed to release curator lock: %s", exc)
        finally:
            self.acquired = False


def check_active_agent_tasks() -> bool:
    """Return True if any BoteX background tasks are currently running or queued."""
    try:
        import server as botex_server

        tasks = getattr(botex_server, "_TASKS", {})
        for t in tasks.values():
            if isinstance(t, dict) and t.get("status") in ("RUNNING", "QUEUED"):
                return True
    except Exception:
        pass

    if os.environ.get("BOTEX_CURATOR_POLL_HTTP", "1").strip() == "0":
        return False

    tasks_url = os.environ.get("BOTEX_TASKS_URL", "http://127.0.0.1:8000/api/tasks").strip()
    if not tasks_url:
        return False
    try:
        req = urllib.request.Request(tasks_url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                items = payload.get("tasks", []) if isinstance(payload, dict) else payload
                if isinstance(items, list):
                    for t in items:
                        if isinstance(t, dict) and t.get("status") in ("RUNNING", "QUEUED"):
                            return True
    except Exception:
        pass
    return False


def _has_temp_handoff_tag(node: Dict[str, Any]) -> bool:
    return any(str(t).strip().lower() == "temp-handoff" for t in node.get("tags") or [])


def _compute_deterministic_recommendations(
    orphan_nodes: List[Dict[str, Any]],
    all_nodes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Heuristic link recommendations based on overlapping tags and title tokens."""
    recommended_links = []
    orphan_insights = []

    orphan_ids = {o.get("id") for o in orphan_nodes}
    # Ephemeral temp-handoff nodes are never valid link targets: they expire and
    # would leave broken_links behind.
    non_orphans = [
        n for n in all_nodes if n.get("id") not in orphan_ids and not _has_temp_handoff_tag(n)
    ]
    node_by_id = {n.get("id"): n for n in non_orphans}

    # Map words & tags to non-orphan nodes
    tag_to_nodes: Dict[str, List[str]] = {}
    for node in non_orphans:
        nid = node.get("id")
        for tag in node.get("tags") or []:
            norm_tag = str(tag).strip().lower()
            if norm_tag:
                tag_to_nodes.setdefault(norm_tag, []).append(nid)

    for orphan in orphan_nodes:
        oid = orphan.get("id")
        otitle = orphan.get("title", "")

        # An orphaned temp-handoff is pending sweep, not integration.
        if _has_temp_handoff_tag(orphan):
            orphan_insights.append(
                {
                    "note_id": oid,
                    "observation": (
                        f"Notatka tymczasowa '{otitle}' (temp-handoff) — podlega sweepowi "
                        "po dacie `expires`; trwałe notatki nie powinny do niej linkować."
                    ),
                }
            )
            continue

        otags = [str(t).strip().lower() for t in orphan.get("tags") or []]
        o_project = str(orphan.get("project") or "").strip()

        candidates: List[tuple] = []  # (target_id, rationale, same_project)
        seen: set = set()
        for t in otags:
            for matched in tag_to_nodes.get(t, [])[:3]:
                if matched == oid or matched in seen:
                    continue
                seen.add(matched)
                target_project = str((node_by_id.get(matched) or {}).get("project") or "").strip()
                same_proj = bool(o_project) and o_project == target_project
                rationale = f"Wspólny tag #{t}"
                if same_proj:
                    rationale += " (ten sam project)"
                candidates.append((matched, rationale, same_proj))

        # If no tag match, check words in title (> 4 chars)
        if not candidates and otitle:
            words = {w.lower() for w in re.findall(r"\b[a-zA-Z0-9_-]{4,}\b", otitle)}
            for node in non_orphans[:15]:
                ntitle = node.get("title", "").lower()
                matched_words = [w for w in words if w in ntitle]
                if matched_words:
                    target_project = str(node.get("project") or "").strip()
                    same_proj = bool(o_project) and o_project == target_project
                    rationale = f"Zbieżność tematyczna: {', '.join(matched_words)}"
                    if same_proj:
                        rationale += " (ten sam project)"
                    candidates.append((node.get("id"), rationale, same_proj))
                    if len(candidates) >= 2:
                        break

        # Same-project candidates take precedence; stable ordering otherwise.
        candidates.sort(key=lambda c: not c[2])

        for target_id, rationale, _same_proj in candidates[:2]:
            recommended_links.append(
                {
                    "source_id": oid,
                    "target_id": target_id,
                    "rationale": rationale,
                }
            )

        orphan_insights.append(
            {
                "note_id": oid,
                "observation": (
                    f"Notatka osierocona '{otitle}'. Sugerowane powiązanie z istniejącymi wpisami w grafie."
                    if candidates
                    else f"Notatka osierocona '{otitle}' bez wyraźnych powiązań tagowych."
                ),
            }
        )

    return {
        "summary": (
            f"Analiza deterministyczna: zidentyfikowano {len(orphan_nodes)} węzłów osieroconych. "
            f"Zaproponowano {len(recommended_links)} potencjalnych relacji na podstawie wspólnych tagów i tematów."
        ),
        "cluster_themes": list(tag_to_nodes.keys())[:5],
        "recommended_links": recommended_links,
        "orphan_insights": orphan_insights,
    }


def _compute_dead_link_repairs(
    broken_links: List[Dict[str, Any]],
    all_nodes: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Deterministic dead-link reconciliation: for each broken [[target]]
    suggest the nearest existing note id (suggest-only, never mutates).

    A broken target often survives under a corrected id — renamed stems,
    redacted ids repaired to stem (repair_frontmatter_ids), or kind
    prefixes. difflib keeps this conservative: only close matches surface.
    """
    import difflib

    existing = [str(n.get("id") or "") for n in all_nodes if n.get("id")]
    if not existing:
        return []
    broken_targets = {str(b.get("target") or "") for b in broken_links}
    repairs: List[Dict[str, str]] = []
    for target in sorted(t for t in broken_targets if t):
        matches = difflib.get_close_matches(target, existing, n=1, cutoff=0.75)
        if not matches:
            continue
        srcs = sorted(
            str(b.get("source") or "") for b in broken_links if str(b.get("target") or "") == target
        )
        for src in srcs:
            repairs.append(
                {
                    "source_id": src,
                    "dead_target": target,
                    "suggested_target": matches[0],
                }
            )
    return repairs


STALE_AFTER_SECONDS = 93600.0  # 26 hours (nightly cycle + jitter)
DEFAULT_BATCH_SIZE = 12
DEFAULT_ANCHOR_LIMIT = 30
DEFAULT_MAX_LINKS_PER_RUN = 50


def _compute_health_assessment(health: Dict[str, Any]) -> Dict[str, Any]:
    """Derive severity, degraded_reasons, orphan_ratio, and ledger_count from health dict."""
    total_nodes = int(health.get("total_nodes", 0) or 0)
    orphan_count = int(health.get("orphan_count", 0) or 0)
    broken_count = int(health.get("broken_count", 0) or 0)
    ledger_count = int(health.get("ledger_count", 0) or 0)
    non_ledger = max(total_nodes - ledger_count, 1) if total_nodes > 0 else 1
    orphan_ratio = round(orphan_count / non_ledger, 4) if total_nodes > 0 else 0.0

    degraded_reasons: List[str] = []
    if broken_count > 0:
        degraded_reasons.append(f"{broken_count} broken wikilink(s)")
    if orphan_count >= 10 and orphan_ratio >= 0.5:
        degraded_reasons.append(f"High orphan ratio ({orphan_ratio:.0%}, {orphan_count} orphans)")
    elif orphan_count > 0 and orphan_ratio >= 0.25:
        degraded_reasons.append(
            f"Elevated orphan ratio ({orphan_ratio:.0%}, {orphan_count} orphans)"
        )

    if broken_count > 0 or (orphan_count >= 10 and orphan_ratio >= 0.5):
        severity = "degraded"
    elif orphan_count > 0 and orphan_ratio >= 0.25:
        severity = "warn"
    else:
        severity = "ok"

    return {
        "ledger_count": ledger_count,
        "orphan_ratio": orphan_ratio,
        "severity": severity,
        "degraded_reasons": degraded_reasons,
    }


def _compute_deadline(
    window_end: Optional[str] = None,
    max_duration_s: Optional[float] = None,
    *,
    now_dt: Optional[datetime] = None,
    now_mono: Optional[float] = None,
) -> Optional[float]:
    """Compute monotonic deadline from ``--window-end HH:MM`` and/or ``--max-duration-s``."""
    cur_mono = now_mono if now_mono is not None else time.time()
    deadlines: List[float] = []
    if max_duration_s is not None and max_duration_s > 0:
        deadlines.append(cur_mono + float(max_duration_s))

    if window_end:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(window_end))
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                cur_dt = now_dt or datetime.now()
                target = cur_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= cur_dt:
                    # Overnight wrap (e.g. started at 23:30 with window_end=05:30)
                    if cur_dt.hour >= 18:
                        target = target + timedelta(days=1)
                remaining = (target - cur_dt).total_seconds()
                deadlines.append(cur_mono + max(0.0, remaining))

    return min(deadlines) if deadlines else None


def _mtime_hash(node: Dict[str, Any]) -> str:
    """Stable hash of a node's mtime for drain_state checkpointing."""
    raw = f"{node.get('id', '')}:{float(node.get('mtime', 0.0) or 0.0):.3f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _cluster_mtime_hash(cluster_nodes: List[Dict[str, Any]]) -> str:
    """Stable hash of cluster members' mtimes to detect if constituent notes changed."""
    items = sorted(
        f"{str(n.get('id', ''))}:{float(n.get('mtime', 0.0) or 0.0):.3f}" for n in cluster_nodes
    )
    raw = "|".join(items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _clean_truth_citations(text: str, allowed_ids: set[str]) -> str:
    """Drop any [[id]] citation not present in cluster + target."""
    allowed_lower = {aid.lower(): aid for aid in allowed_ids}

    def _repl(match: re.Match) -> str:
        full_wikilink = match.group(0)
        inner = match.group(1).strip()
        mid = inner.split("|")[0].strip()
        if mid in allowed_ids:
            return full_wikilink
        if mid.lower() in allowed_lower:
            canon = allowed_lower[mid.lower()]
            if "|" in inner:
                alias = inner.split("|", 1)[1]
                return f"[[{canon}|{alias}]]"
            return f"[[{canon}]]"
        return ""

    cleaned = re.sub(r"\[\[([^\]]+)\]\]", _repl, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.:;?!])", r"\1", cleaned)
    return cleaned.strip()


def _build_project_batches(
    orphans: List[Dict[str, Any]],
    all_nodes: List[Dict[str, Any]],
    drain_state: Dict[str, str],
    batch_size: int = DEFAULT_BATCH_SIZE,
    anchor_limit: int = DEFAULT_ANCHOR_LIMIT,
) -> List[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Partition pending orphans by canonical project into batches with top-30 anchors."""
    pending = [
        o
        for o in orphans
        if not memory._is_ledger_node(o)
        and drain_state.get(str(o.get("id") or "")) != _mtime_hash(o)
    ]
    if not pending:
        return []

    orphan_ids = {str(o.get("id") or "") for o in orphans}
    anchors_all = [
        n
        for n in all_nodes
        if str(n.get("id") or "") not in orphan_ids and not memory._is_ledger_node(n)
    ]
    anchors_by_project: Dict[str, List[Dict[str, Any]]] = {}
    for a in anchors_all:
        proj = str(a.get("project") or "")
        anchors_by_project.setdefault(proj, []).append(a)
    for plist in anchors_by_project.values():
        plist.sort(
            key=lambda n: (float(n.get("mtime") or 0.0), float(n.get("confidence") or 1.0)),
            reverse=True,
        )
    fallback_anchors = sorted(
        (
            a
            for a in anchors_all
            if str(a.get("kind") or "") in ("decision", "lesson")
            and float(a.get("confidence") or 1.0) >= 0.8
        ),
        key=lambda n: (float(n.get("confidence") or 1.0), float(n.get("mtime") or 0.0)),
        reverse=True,
    )

    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for o in pending:
        proj = str(o.get("project") or "")
        by_project.setdefault(proj, []).append(o)

    batches: List[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]] = []
    step = max(1, int(batch_size))
    for proj in sorted(by_project.keys()):
        proj_orphans = sorted(by_project[proj], key=lambda n: str(n.get("id") or ""))
        proj_anchors = list(anchors_by_project.get(proj, [])[:anchor_limit])
        if len(proj_anchors) < anchor_limit:
            seen_a = {str(a.get("id") or "") for a in proj_anchors}
            for fa in fallback_anchors:
                if str(fa.get("id") or "") not in seen_a:
                    proj_anchors.append(fa)
                    seen_a.add(str(fa.get("id") or ""))
                    if len(proj_anchors) >= anchor_limit:
                        break
        for i in range(0, len(proj_orphans), step):
            batches.append((proj, proj_orphans[i : i + step], proj_anchors))
    return batches


def _apply_recommended_links(
    workspace_dir: str,
    recommended_links: List[Dict[str, Any]],
    node_by_id: Dict[str, Dict[str, Any]],
    existing_edges: Optional[set[Tuple[str, str]]] = None,
    max_links: int = DEFAULT_MAX_LINKS_PER_RUN,
) -> Dict[str, Any]:
    """Safely apply append-only wikilinks to ``## Related`` with strict project/ledger gates."""
    applied: List[Dict[str, str]] = []
    skipped = 0
    seen_edges = set(existing_edges or ())

    for rec in recommended_links:
        if len(applied) >= max_links:
            break
        if not isinstance(rec, dict):
            skipped += 1
            continue
        src_id = str(rec.get("source_id") or "").strip()
        tgt_id = str(rec.get("target_id") or "").strip()
        rationale = str(rec.get("rationale") or "").strip()

        if not src_id or not tgt_id or src_id == tgt_id:
            skipped += 1
            continue
        src_node = node_by_id.get(src_id)
        tgt_node = node_by_id.get(tgt_id)
        if not src_node or not tgt_node:
            skipped += 1
            continue
        if memory._is_ledger_node(src_node) or memory._is_ledger_node(tgt_node):
            skipped += 1
            continue

        src_proj = memory._normalize_project_slug(src_node.get("project"))
        tgt_proj = memory._normalize_project_slug(tgt_node.get("project"))
        same_project = bool(src_proj and tgt_proj and src_proj == tgt_proj)
        cross_project_ok = (
            str(src_node.get("kind") or "") in ("decision", "lesson")
            and str(tgt_node.get("kind") or "") in ("decision", "lesson")
            and float(src_node.get("confidence", 1.0) or 1.0) >= 0.8
            and float(tgt_node.get("confidence", 1.0) or 1.0) >= 0.8
        )
        if not (same_project or cross_project_ok):
            skipped += 1
            continue

        if (src_id, tgt_id) in seen_edges:
            skipped += 1
            continue

        line = f"- [[{tgt_id}]] — {rationale}" if rationale else f"- [[{tgt_id}]]"
        patch_res = memory.patch_memory_section(
            workspace_dir=workspace_dir,
            memory_id=src_id,
            section="Related",
            new_content=line,
            scope="all",
            append=True,
        )
        if patch_res.get("ok"):
            seen_edges.add((src_id, tgt_id))
            applied.append({"source_id": src_id, "target_id": tgt_id})
        else:
            skipped += 1

    return {"applied": len(applied), "skipped": skipped, "applied_links": applied}


def _parse_json_analysis(raw_output: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse structured JSON curator output from an LLM response."""
    if not raw_output:
        return None
    try:
        cleaned = raw_output.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        else:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                cleaned = match.group(0)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "summary" in parsed:
            return parsed
    except Exception as exc:
        logger.warning("Failed to parse LLM curator output: %s", exc)
    return None


class CuratorService:
    """Autonomous Knowledge Graph Curator engine."""

    def __init__(
        self,
        workspace_dir: str = ".",
        primary_model: Optional[str] = None,
        local_model: Optional[str] = None,
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.workspace_dir = resolve_workspace(workspace_dir)
        self.ws_path = Path(self.workspace_dir).resolve()
        self.primary_model = (
            primary_model or os.environ.get("CURATOR_PRIMARY_MODEL") or DEFAULT_PRIMARY_MODEL
        )
        self.local_model = (
            local_model or os.environ.get("CURATOR_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL
        )
        self.notifier = notifier or TelegramNotifier()

        self.curator_dir = self.ws_path / ".botex" / "curator"
        self.runs_dir = self.curator_dir / "runs"
        self.status_file = self.curator_dir / "status.json"
        self.lock_file = self.curator_dir / "curator.lock"
        self.drain_state_file = self.curator_dir / "drain_state.json"

        self.curator_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _load_drain_state(self) -> Dict[str, str]:
        try:
            data = json.loads(self.drain_state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k and v}
        except Exception:
            pass
        return {}

    def _save_drain_state(self, state: Dict[str, str]) -> None:
        try:
            self.drain_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.drain_state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.drain_state_file)
        except Exception as exc:
            logger.debug("Failed to persist drain_state.json: %s", exc)

    def get_status(self) -> Dict[str, Any]:
        """Read latest curator status or return empty topology baseline."""
        lock_active = False
        if self.lock_file.exists():
            try:
                data = json.loads(self.lock_file.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
                if _is_pid_alive(pid):
                    lock_active = True
            except Exception:
                pass

        if self.status_file.exists():
            try:
                status_data = json.loads(self.status_file.read_text(encoding="utf-8"))
                status_data["lock_active"] = lock_active
                ts_raw = status_data.get("last_run_timestamp")
                stale_after = float(status_data.get("stale_after_seconds") or STALE_AFTER_SECONDS)
                status_data.setdefault("stale_after_seconds", stale_after)
                if ts_raw:
                    try:
                        parsed_dt = datetime.fromisoformat(str(ts_raw))
                        if parsed_dt.tzinfo is None:
                            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                        age_s = max(0.0, time.time() - parsed_dt.timestamp())
                        status_data["age_seconds"] = round(age_s, 1)
                        status_data["is_stale"] = age_s > stale_after
                    except Exception:
                        status_data.setdefault("is_stale", False)
                else:
                    status_data.setdefault("is_stale", False)
                return status_data
            except Exception:
                pass

        # If no previous run file, compute live quick topology baseline
        try:
            health = memory.find_graph_health(workspace_dir=str(self.ws_path), scope="all")
            assess = _compute_health_assessment(health)
            return {
                "last_run_id": None,
                "last_run_timestamp": None,
                "status": "INITIAL" if health.get("is_healthy") else "DEGRADED",
                "is_healthy": health.get("is_healthy", True),
                "severity": assess["severity"],
                "degraded_reasons": assess["degraded_reasons"],
                "orphan_ratio": assess["orphan_ratio"],
                "ledger_count": assess["ledger_count"],
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "is_stale": False,
                "total_nodes": health.get("total_nodes", 0),
                "total_edges": health.get("total_edges", 0),
                "orphan_count": health.get("orphan_count", 0),
                "broken_count": health.get("broken_count", 0),
                "tier_used": "none",
                "duration_seconds": 0.0,
                "lock_active": lock_active,
                "beacon_path": str(self.ws_path / ".botex" / "memory" / "curator_health_status.md"),
            }
        except Exception:
            return {
                "last_run_id": None,
                "last_run_timestamp": None,
                "status": "UNKNOWN",
                "is_healthy": True,
                "severity": "ok",
                "degraded_reasons": [],
                "orphan_ratio": 0.0,
                "ledger_count": 0,
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "is_stale": False,
                "total_nodes": 0,
                "total_edges": 0,
                "orphan_count": 0,
                "broken_count": 0,
                "tier_used": "none",
                "duration_seconds": 0.0,
                "lock_active": lock_active,
                "beacon_path": None,
            }

    async def _try_nemotron_inference(
        self, prompt: str, system_prompt: str = CURATOR_SYSTEM_PROMPT
    ) -> Optional[str]:
        """Tier 1: NVIDIA Nemotron batch inference via NvidiaProvider."""
        api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from botex.providers import ProviderRegistry

            provider = ProviderRegistry.get("nvidia")
            client = provider.get_client(api_key=api_key)
            extra_body = (
                {"chat_template_kwargs": {"thinking": False}}
                if "nemotron-3" in self.primary_model
                else None
            )
            resp = await client.chat.completions.create(
                model=self.primary_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=2048,
                timeout=30.0,
                extra_body=extra_body,
            )
            content = resp.choices[0].message.content
            return content
        except Exception as exc:
            logger.info("Tier 1 Nemotron inference unavailable or failed: %s", exc)
            return None

    async def _try_local_llm_inference(
        self, prompt: str, system_prompt: str = CURATOR_SYSTEM_PROMPT
    ) -> Optional[str]:
        """Tier 2: Local Bielik / Ollama / llama.cpp inference via OpenAI compatible API."""
        local_url = (
            os.environ.get("LOCAL_LLM_URL")
            or os.environ.get("BIELIK_URL")
            or "http://127.0.0.1:8080/v1"
        )
        try:
            from urllib.parse import urlparse

            parsed = urlparse(local_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.25)
            try:
                s.connect((host, port))
            finally:
                s.close()
        except Exception:
            return None

        try:
            from openai import AsyncOpenAI

            from botex.providers.providers import resolve_model_for_openai_client

            client = AsyncOpenAI(base_url=local_url, api_key="not-needed", timeout=45.0)
            model_id = await resolve_model_for_openai_client(client, self.local_model)
            model_low = model_id.lower()
            if "qwen" in model_low:
                extra_body: Optional[Dict[str, Any]] = {
                    "chat_template_kwargs": {"enable_thinking": False}
                }
            elif "bielik" in model_low or ":8080" in local_url:
                extra_body = {
                    "chat_template_kwargs": {"decision_mode": True, "reasoning_effort": "none"}
                }
            else:
                extra_body = None

            resp = await client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=2048,
                extra_body=extra_body,
            )
            content = resp.choices[0].message.content
            return content
        except Exception as exc:
            logger.info("Tier 2 Local LLM inference unavailable or failed: %s", exc)
            return None

    async def _execute_batch_llm_analysis(
        self,
        health: Dict[str, Any],
        recent_nodes: List[Dict[str, Any]],
        orphan_nodes: List[Dict[str, Any]],
    ) -> tuple[str, Dict[str, Any]]:
        """Run multi-tier LLM analysis with fallback down to deterministic heuristics."""
        total_nodes = health.get("total_nodes", 0)
        total_edges = health.get("total_edges", 0)
        broken_links = health.get("broken_links", [])

        # If zero nodes or zero orphans and 100% healthy, deterministic is instant
        if total_nodes == 0:
            return "deterministic", {
                "summary": "Graf wiedzy jest pusty. Brak notatek do analizy.",
                "cluster_themes": [],
                "recommended_links": [],
                "orphan_insights": [],
            }

        prompt = build_curator_user_prompt(
            total_nodes=total_nodes,
            total_edges=total_edges,
            orphan_nodes=orphan_nodes,
            broken_links=broken_links,
            recent_nodes=recent_nodes,
        )

        # 1. Try Tier 1: Nemotron
        raw_output = await self._try_nemotron_inference(prompt)
        tier_used = "nvidia-nemotron"

        # 2. Try Tier 2: Local Bielik
        if not raw_output:
            raw_output = await self._try_local_llm_inference(prompt)
            tier_used = "local-bielik"

        # 3. Parse JSON or fall back to Tier 3: Deterministic
        if raw_output:
            try:
                cleaned = raw_output.strip()
                if "```json" in cleaned:
                    cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
                elif "```" in cleaned:
                    cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
                else:
                    match = re.search(r"\{[\s\S]*\}", cleaned)
                    if match:
                        cleaned = match.group(0)
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "summary" in parsed:
                    return tier_used, parsed
            except Exception as exc:
                logger.warning("Failed to parse LLM curator output: %s", exc)

        # Tier 3: Deterministic heuristic analysis
        deterministic_res = _compute_deterministic_recommendations(orphan_nodes, recent_nodes)
        return "deterministic", deterministic_res

    def _load_providers_config(self) -> Dict[str, Any]:
        try:
            from botex.config import load_config

            cfg = load_config()
            provs = cfg.get("providers") if isinstance(cfg, dict) else None
            return provs if isinstance(provs, dict) else {}
        except Exception as exc:
            logger.debug("Failed to load providers config: %s", exc)
            return {}

    def _is_local_llm_reachable(self) -> bool:
        local_url = (
            os.environ.get("LOCAL_LLM_URL")
            or os.environ.get("BIELIK_URL")
            or "http://127.0.0.1:8080/v1"
        )
        try:
            from urllib.parse import urlparse

            parsed = urlparse(local_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.25)
            try:
                s.connect((host, port))
                return True
            finally:
                s.close()
        except Exception:
            return False

    def _available_provider_lanes(self) -> List[str]:
        """Return active provider lanes for parallel nightly graph draining."""
        lanes: List[str] = []
        if os.environ.get("NVIDIA_API_KEY", "").strip():
            lanes.append("nvidia")
        if (
            os.environ.get("OPENROUTER_API_FREE_KEY", "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip()
        ):
            lanes.append("openrouter-free")
        if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip():
            lanes.append("google")
        if self._is_local_llm_reachable():
            lanes.append("local-bielik")
        return lanes

    async def _try_cloud_lane_inference(
        self,
        lane: str,
        prompt: str,
        providers_cfg: Dict[str, Any],
        limiter: Any,
        system_prompt: str = CURATOR_SYSTEM_PROMPT,
    ) -> Optional[str]:
        """Execute one curator batch on a cloud provider lane with rate-limiter & provider_cfg."""
        from botex.providers import ProviderRegistry

        lane_cfg = providers_cfg.get(lane) if isinstance(providers_cfg.get(lane), dict) else {}
        if lane == "nvidia":
            api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
            model = (
                self.primary_model
                or (lane_cfg.get("models") or {}).get("fast")
                or DEFAULT_PRIMARY_MODEL
            )
            extra_body = (
                {"chat_template_kwargs": {"thinking": False}} if "nemotron-3" in model else None
            )
        elif lane == "openrouter-free":
            api_key = (
                os.environ.get("OPENROUTER_API_FREE_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or ""
            ).strip()
            model = (lane_cfg.get("models") or {}).get("fast") or "google/gemma-4-31b-it:free"
            extra_body = lane_cfg.get("extra_body") or {"provider": {"data_collection": "allow"}}
        elif lane == "google":
            api_key = (
                os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
            ).strip()
            model = (lane_cfg.get("models") or {}).get("fast") or "gemini-2.5-flash"
            extra_body = None
        else:
            return None

        if not api_key:
            return None

        try:
            if limiter is not None:
                await limiter.acquire(lane)
            provider = ProviderRegistry.get(lane, provider_cfg=lane_cfg or None)
            client = provider.get_client(api_key=api_key)
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 2048,
                "timeout": 35.0,
            }
            if extra_body is not None:
                kwargs["extra_body"] = extra_body
            resp = await client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content
        except Exception as exc:
            err_str = str(exc)
            if limiter is not None and ("429" in err_str or "rate" in err_str.lower()):
                limiter.report_limited(lane)
            logger.info("Curator lane '%s' failed: %s", lane, type(exc).__name__)
            return None

    async def _drain_batches_parallel(
        self,
        *,
        batches: List[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]],
        health: Dict[str, Any],
        lock: CuratorLock,
        force: bool,
        deadline: Optional[float],
        max_batches: Optional[int],
        drain_state: Dict[str, str],
    ) -> Dict[str, Any]:
        """Process project-partitioned orphan batches across parallel provider lanes."""
        from botex.providers.rate_limiter import get_rate_limiter

        providers_cfg = self._load_providers_config()
        try:
            limiter = get_rate_limiter()
        except Exception:
            limiter = None

        lanes = self._available_provider_lanes()
        local_sem = asyncio.Semaphore(1)
        total_nodes = health.get("total_nodes", 0)
        total_edges = health.get("total_edges", 0)
        broken_links = health.get("broken_links", [])

        queue: asyncio.Queue[Optional[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]] = (
            asyncio.Queue()
        )
        capped = batches[:max_batches] if (max_batches is not None and max_batches > 0) else batches
        for b in capped:
            queue.put_nowait(b)

        results_lock = asyncio.Lock()
        all_recs: List[Dict[str, Any]] = []
        all_insights: List[Dict[str, Any]] = []
        themes_set: List[str] = []
        tiers_used: List[str] = []
        processed = 0
        yielded = False

        async def _worker(lane: str) -> None:
            nonlocal processed, yielded
            while True:
                if deadline is not None and time.time() >= deadline:
                    break
                if not force and await asyncio.to_thread(check_active_agent_tasks):
                    yielded = True
                    break
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    queue.task_done()
                    break

                _proj, batch_orphans, batch_anchors = item
                prompt = build_curator_user_prompt(
                    total_nodes=total_nodes,
                    total_edges=total_edges,
                    orphan_nodes=batch_orphans,
                    broken_links=broken_links,
                    recent_nodes=batch_anchors,
                )

                raw_output: Optional[str] = None
                tier_label = lane
                if lane == "local-bielik":
                    async with local_sem:
                        raw_output = await self._try_local_llm_inference(prompt)
                elif lane == "deterministic":
                    raw_output = None
                else:
                    raw_output = await self._try_cloud_lane_inference(
                        lane, prompt, providers_cfg, limiter
                    )

                parsed = _parse_json_analysis(raw_output)
                if parsed is None:
                    parsed = _compute_deterministic_recommendations(batch_orphans, batch_anchors)
                    tier_label = "deterministic"

                async with results_lock:
                    all_recs.extend(parsed.get("recommended_links") or [])
                    all_insights.extend(parsed.get("orphan_insights") or [])
                    for th in parsed.get("cluster_themes") or []:
                        if th not in themes_set:
                            themes_set.append(th)
                    tiers_used.append(tier_label)
                    processed += 1
                    for o in batch_orphans:
                        oid = str(o.get("id") or "")
                        if oid:
                            drain_state[oid] = _mtime_hash(o)
                    self._save_drain_state(drain_state)
                    lock.heartbeat()

                queue.task_done()

        active_lanes = lanes if lanes else ["deterministic"]
        await asyncio.gather(*(_worker(lane) for lane in active_lanes))

        unique_tiers = list(dict.fromkeys(tiers_used))
        primary_tier = (
            unique_tiers[0]
            if len(unique_tiers) == 1
            else ("+".join(unique_tiers) if unique_tiers else "deterministic")
        )
        summary = (
            f"Przetworzono {processed} paczek projektowych ({len(all_recs)} rekomendacji linków"
            f"{', przerwano przez aktywne zadania' if yielded else ''})."
        )
        return {
            "tier_used": primary_tier,
            "batches_processed": processed,
            "yielded": yielded,
            "analysis": {
                "summary": summary,
                "cluster_themes": themes_set[:10],
                "recommended_links": all_recs,
                "orphan_insights": all_insights,
            },
        }

    def _render_beacon_markdown(
        self,
        run_id: str,
        timestamp_str: str,
        tier_used: str,
        duration: float,
        health: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> str:
        """Format the single living beacon note content."""
        total_nodes = health.get("total_nodes", 0)
        total_edges = health.get("total_edges", 0)
        orphans = health.get("orphans") or health.get("orphan_nodes", [])
        broken = health.get("broken_links", [])
        is_healthy = health.get("is_healthy", True)

        status_label = "ZDROWY (PASSED)" if is_healthy else "WYMAGA UWAGI (ATTENTION)"

        lines = [
            f"# Raport Kuratora Grafu Wiedzy BoteX - {timestamp_str}",
            "",
            "> **Living Beacon Note**: Ta notatka jest pojedynczym, stale aktualizowanym punktem "
            "odniesienia stanu zdrowia grafu wiedzy (Zero Memory Vault Bloat).",
            "",
            "## Podsumowanie Przebiegu",
            f"- **Identyfikator Przebiegu (Run ID):** `{run_id}`",
            f"- **Status Ogólny:** **{status_label}**",
            f"- **Warstwa Analityczna:** `{tier_used}`",
            f"- **Czas trwania:** {duration:.2f} s",
            "",
            "## Metryki Topologii Grafu",
            f"- Łączna liczba notatek (Nodes): **{total_nodes}**",
            f"- Łączna liczba krawędzi (Edges/Wikilinks): **{total_edges}**",
            f"- Węzły osierocone (Orphans): **{len(orphans)}**",
            f"- Martwe odnośniki (Broken Links): **{len(broken)}**",
            "",
            "## Diagnoza Analityczna i Rekomendacje Kuratora",
            analysis.get("summary", "Brak uwag analitycznych."),
            "",
        ]

        themes = analysis.get("cluster_themes", [])
        if themes:
            lines.append("### Główne Klastry Tematyczne")
            for th in themes:
                lines.append(f"- #{th}")
            lines.append("")

        recs = analysis.get("recommended_links", [])
        if recs:
            lines.append("### Sugerowane Powiązania Wikilink")
            for rec in recs[:15]:
                src = rec.get("source_id")
                tgt = rec.get("target_id")
                rat = rec.get("rationale", "")
                lines.append(f"- [[{src}]] ➔ [[{tgt}]]: _{rat}_")
            lines.append("")

        if broken:
            lines.append("### Wykryte Martwe Odnośniki")
            for bl in broken[:20]:
                lines.append(
                    f"- [[{bl.get('source')}]] wskazuje na nieistniejący `[[{bl.get('target')}]]`"
                )
            if len(broken) > 20:
                lines.append(f"- ... i jeszcze {len(broken) - 20} innych martwych odnośników")
            lines.append("")

        repairs = analysis.get("dead_link_repairs") or []
        if repairs:
            lines.append("### Sugerowane Naprawy Martwych Odnośników (do review)")
            for rep in repairs[:20]:
                lines.append(
                    f"- [[{rep.get('source_id')}]]: `{rep.get('dead_target')}` ➔ "
                    f"`[[{rep.get('suggested_target')}]]`"
                )
            lines.append("")

        compiled_info = analysis.get("compiled_truth")
        if compiled_info and compiled_info.get("clusters_compiled", 0) > 0:
            lines.append("### Zsyntetyzowana Prawda (Compiled Truth)")
            for item in compiled_info.get("compiled", []):
                tid = item.get("target_id")
                c_size = item.get("cluster_size", 0)
                lines.append(f"- [[{tid}]]: zsyntetyzowano z klastra {c_size} notatek składowych.")
            lines.append("")

        lines.extend(
            [
                "## Gwarancje Integralności (Invariants)",
                "- **Zasada nienaruszalności (Zero-Destruction Invariant):** Treść notatek technicznych, kod oraz wnioski agentów pozostały nienaruszone.",
                "- **Zasada jednego beaconu (Zero-Bloat Invariant):** Kurator nadpisuje wyłącznie niniejszą notatkę `curator_health_status.md`.",
            ]
        )

        return "\n".join(lines)

    async def harvest_inactive_chat_sessions(
        self,
        inactive_threshold_seconds: float = 86400.0,
        min_messages: int = 6,
    ) -> List[Dict[str, Any]]:
        """Harvest key decisions and context from inactive chat sessions into Memory Vault."""
        from botex.tools.security import mask_secrets, sanitize_identifier

        sessions_dir = self.ws_path / ".botex" / "chat_sessions"
        env_dir = os.environ.get("BOTEX_CHAT_SESSIONS_DIR", "").strip()
        if env_dir:
            sessions_dir = Path(os.path.expanduser(env_dir)).resolve()
        if not sessions_dir.is_dir():
            return []

        ledger_file = self.curator_dir / "harvested_chats.json"
        ledger: Dict[str, float] = {}
        if ledger_file.is_file():
            try:
                loaded = json.loads(ledger_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    ledger = {str(k): float(v) for k, v in loaded.items() if v is not None}
            except Exception:
                ledger = {}

        now = time.time()
        harvested: List[Dict[str, Any]] = []

        for file_path in sorted(sessions_dir.glob("chat_*.json")):
            if not file_path.is_file():
                continue
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                raw_sid = str(raw.get("session_id") or file_path.stem)
                safe_sid = sanitize_identifier(raw_sid)
                if not safe_sid:
                    continue
                messages = raw.get("messages")
                if not isinstance(messages, list) or len(messages) < min_messages:
                    continue
                updated_at = float(raw.get("updated_at") or file_path.stat().st_mtime)
                if (now - updated_at) < inactive_threshold_seconds:
                    continue
                if updated_at <= float(ledger.get(safe_sid, 0.0)):
                    continue

                title = mask_secrets(str(raw.get("title") or f"Chat {safe_sid}"))[:80]
                user_points: List[str] = []
                assistant_points: List[str] = []
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role")
                    c = mask_secrets(str(m.get("content") or "").strip())
                    if not c:
                        continue
                    if role == "user" and len(user_points) < 5:
                        user_points.append(c[:300])
                    elif role == "assistant" and not m.get("tool_calls"):
                        assistant_points.append(c[:500])

                if not user_points and not assistant_points:
                    continue

                digest_lines = [
                    f"# Destylacja Sesji Czatu: {title}",
                    "",
                    f"- **Session ID:** `{safe_sid}`",
                    f"- **Liczba wiadomości:** {len(messages)}",
                    "",
                    "## Kluczowe Zagadnienia Użytkownika",
                ]
                for up in user_points[:5]:
                    digest_lines.append(f"- {up}")
                digest_lines.append("")
                digest_lines.append("## Wnioski i Ustalenia Asystenta")
                for ap in assistant_points[-3:]:
                    digest_lines.append(f"- {ap}")

                note_id = f"chat_harvest_{safe_sid}"
                save_res = memory.save_memory(
                    workspace_dir=str(self.ws_path),
                    memory_id=note_id,
                    title=f"Chat Harvest: {title}",
                    content="\n".join(digest_lines),
                    kind="decision",
                    tags=["chat-harvest", "chat"],
                    origin="curator",
                    scope="local",
                    status="active",
                )
                ledger[safe_sid] = updated_at
                harvested.append(
                    {
                        "session_id": safe_sid,
                        "memory_id": note_id,
                        "file_path": save_res.get("file_path", ""),
                    }
                )
            except Exception as exc:
                logger.debug("Chat session harvest skipped for %s: %s", file_path.name, exc)

        if harvested:
            try:
                ledger_file.write_text(
                    json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass
        return harvested

    async def _infer_compiled_truth(self, prompt: str) -> Optional[str]:
        lanes = self._available_provider_lanes()
        if "nvidia" in lanes:
            out = await self._try_nemotron_inference(
                prompt, system_prompt=COMPILED_TRUTH_SYSTEM_PROMPT
            )
            if out:
                return out
        if "local-bielik" in lanes:
            out = await self._try_local_llm_inference(
                prompt, system_prompt=COMPILED_TRUTH_SYSTEM_PROMPT
            )
            if out:
                return out
        providers_cfg = self._load_providers_config()
        for lane in lanes:
            if lane in ("nvidia", "local-bielik"):
                continue
            out = await self._try_cloud_lane_inference(
                lane,
                prompt,
                providers_cfg,
                limiter=None,
                system_prompt=COMPILED_TRUTH_SYSTEM_PROMPT,
            )
            if out:
                return out
        return None

    async def _run_compiled_truth_phase(
        self,
        health: Dict[str, Any],
        apply_truth: bool = True,
        max_clusters: int = 3,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Synthesize authoritative ## Compiled Truth on mature hub entities/decisions."""
        edges = health.get("edges", []) or []
        all_nodes = health.get("all_nodes", []) or []
        node_by_id = {str(n.get("id")): n for n in all_nodes if n.get("id")}

        # 1. Compute in-degree from edges (deduplicating incoming edges and excluding self-loops)
        in_deg: Dict[str, int] = {}
        seen_incoming: set[tuple[str, str]] = set()
        for e in edges:
            src = str(e.get("source") or "")
            tgt = str(e.get("target") or "")
            if tgt and (not src or src != tgt) and (src, tgt) not in seen_incoming:
                seen_incoming.add((src, tgt))
                in_deg[tgt] = in_deg.get(tgt, 0) + 1

        # 2. Candidate notes selection:
        # filter: status == "active", kind in ("decision", "context"), not _is_ledger_node(node), in_degree >= 3
        candidates = []
        for n in all_nodes:
            nid = str(n.get("id") or "")
            status = str(n.get("status") or "active").strip().lower()
            kind = str(n.get("kind") or "").strip().lower()
            if (
                status == "active"
                and kind in ("decision", "context")
                and not memory._is_ledger_node(n)
                and in_deg.get(nid, 0) >= 3
            ):
                candidates.append(n)

        candidates.sort(
            key=lambda x: (in_deg.get(str(x.get("id") or ""), 0), float(x.get("mtime") or 0.0)),
            reverse=True,
        )

        compiled_records: List[Dict[str, Any]] = []
        skipped_records: List[Dict[str, Any]] = []

        for cand in candidates:
            if deadline is not None and time.time() >= deadline:
                logger.info("Compiled truth phase reached window deadline.")
                break

            if len(compiled_records) >= max_clusters:
                break

            target_id = str(cand.get("id") or "")
            target_path = None
            for s_dir, _ in memory._resolve_scope_dirs(str(self.ws_path), "all"):
                c_path = memory._find_existing_memory_file(s_dir, target_id)
                if c_path is not None:
                    target_path = c_path
                    break

            if not target_path or not target_path.exists():
                skipped_records.append({"target_id": target_id, "reason": "file_not_found"})
                continue

            try:
                raw_text = target_path.read_text(encoding="utf-8", errors="replace")
                meta, body = memory._parse_frontmatter(raw_text)
            except Exception as exc:
                skipped_records.append({"target_id": target_id, "reason": f"read_error: {exc}"})
                continue

            # Human Freeze Gate:
            # if source/origin == "human" or tags contain "human-curated" or "frozen",
            # or note contains ## Compiled Truth WITHOUT the marker <!-- curator:ct , SKIP IT!
            source_val = (
                str(
                    meta.get("source")
                    or meta.get("origin")
                    or cand.get("source")
                    or cand.get("origin")
                    or ""
                )
                .strip()
                .lower()
            )
            if source_val == "human":
                skipped_records.append({"target_id": target_id, "reason": "human_freeze_source"})
                continue

            tags_set = set()
            for src_tags in (meta.get("tags"), cand.get("tags")):
                if isinstance(src_tags, list):
                    for t in src_tags:
                        tags_set.add(str(t).strip().lower().lstrip("#"))
                elif isinstance(src_tags, str):
                    for t in re.split(r"[\s,]+", src_tags):
                        if t.strip():
                            tags_set.add(t.strip().lower().lstrip("#"))

            if "human-curated" in tags_set or "frozen" in tags_set:
                skipped_records.append({"target_id": target_id, "reason": "human_freeze_tag"})
                continue

            has_ct_section = bool(
                re.search(r"^#{1,6}\s*Compiled Truth\b", raw_text, re.MULTILINE | re.IGNORECASE)
            )
            has_curator_marker = "<!-- curator:ct " in raw_text
            if (has_ct_section or "## Compiled Truth" in raw_text) and not has_curator_marker:
                skipped_records.append(
                    {"target_id": target_id, "reason": "human_freeze_manual_section"}
                )
                continue

            # Cluster formation:
            # 1-hop in+out neighbours from health.get("edges", []) with same project, status == "active", non-ledger;
            # cap 6 notes; truncate each note content to 1000 chars.
            target_project = memory._normalize_project_slug(
                cand.get("project") or meta.get("project") or ""
            )
            if target_project == "_unassigned":
                target_project = ""
            neighbour_ids: set[str] = set()
            for e in edges:
                s = str(e.get("source") or "")
                t = str(e.get("target") or "")
                if s == target_id and t and t != target_id:
                    neighbour_ids.add(t)
                elif t == target_id and s and s != target_id:
                    neighbour_ids.add(s)

            valid_neighbours: List[Dict[str, Any]] = []
            for nid in neighbour_ids:
                nn = node_by_id.get(nid)
                if not nn:
                    continue
                proj = memory._normalize_project_slug(nn.get("project") or "")
                if proj == "_unassigned":
                    proj = ""
                if proj != target_project:
                    continue
                n_status = str(nn.get("status") or "active").strip().lower()
                if n_status != "active":
                    continue
                if memory._is_ledger_node(nn):
                    continue
                valid_neighbours.append(nn)

            if not valid_neighbours:
                skipped_records.append({"target_id": target_id, "reason": "no_cluster_members"})
                continue

            valid_neighbours.sort(
                key=lambda x: (
                    float(x.get("confidence") or 1.0),
                    float(x.get("mtime") or 0.0),
                    str(x.get("id")),
                ),
                reverse=True,
            )
            cluster_nodes = valid_neighbours[:6]
            cluster_hash = _cluster_mtime_hash(cluster_nodes)

            # Marker checking:
            # If marker <!-- curator:ct {hash} --> exists: verify if cluster member mtime hash changed. If unchanged, skip.
            marker_match = re.search(r"<!--\s*curator:ct\s+([a-zA-Z0-9]+)\s*-->", raw_text)
            if marker_match:
                existing_hash = marker_match.group(1).strip()
                if existing_hash == cluster_hash:
                    skipped_records.append({"target_id": target_id, "reason": "hash_unchanged"})
                    continue

            # Load constituent note contents (truncated to 1000 chars)
            cluster_for_prompt: List[Dict[str, Any]] = []
            for cn in cluster_nodes:
                cid = str(cn.get("id"))
                loaded = memory._load_recall_node(
                    workspace_dir=str(self.ws_path), memory_id=cid, scope="all"
                )
                content = (loaded.get("content") or "") if loaded else ""
                cn_copy = dict(cn)
                cn_copy["content"] = content[:1000]
                cluster_for_prompt.append(cn_copy)

            target_dict = dict(cand)
            target_dict["content"] = body[:1000]

            user_prompt = build_compiled_truth_user_prompt(
                target_node=target_dict, cluster_nodes=cluster_for_prompt
            )

            raw_output = await self._infer_compiled_truth(user_prompt)
            if not raw_output:
                skipped_records.append(
                    {"target_id": target_id, "reason": "inference_failed_or_unavailable"}
                )
                continue

            try:
                cleaned = raw_output.strip()
                if "```json" in cleaned:
                    cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
                elif "```" in cleaned:
                    cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
                else:
                    match = re.search(r"\{[\s\S]*\}", cleaned)
                    if match:
                        cleaned = match.group(0)
                parsed = json.loads(cleaned)
                if not isinstance(parsed, dict):
                    skipped_records.append({"target_id": target_id, "reason": "non_dict_json"})
                    continue
                raw_truth = str(parsed.get("compiled_truth") or "").strip()
                if not raw_truth:
                    skipped_records.append(
                        {"target_id": target_id, "reason": "empty_compiled_truth"}
                    )
                    continue
            except Exception as exc:
                skipped_records.append(
                    {"target_id": target_id, "reason": f"json_parse_error: {exc}"}
                )
                continue

            allowed_ids = {target_id} | {str(c.get("id")) for c in cluster_nodes}
            allowed_lower = {aid.lower(): aid for aid in allowed_ids}
            truth_text = _clean_truth_citations(raw_truth, allowed_ids)

            clean_sources: List[str] = []
            for s in parsed.get("sources") or []:
                s_id = str(s).strip().strip("[]").split("|")[0].strip()
                if s_id in allowed_ids and s_id not in clean_sources:
                    clean_sources.append(s_id)
                elif (
                    s_id.lower() in allowed_lower
                    and allowed_lower[s_id.lower()] not in clean_sources
                ):
                    clean_sources.append(allowed_lower[s_id.lower()])
            sources = clean_sources

            if apply_truth:
                patch_res = memory.patch_memory_section(
                    workspace_dir=str(self.ws_path),
                    memory_id=target_id,
                    section="Compiled Truth",
                    new_content=f"{truth_text}\n\n<!-- curator:ct {cluster_hash} -->",
                    append=False,
                    scope="all",
                )
                if not patch_res.get("ok"):
                    skipped_records.append(
                        {
                            "target_id": target_id,
                            "reason": f"patch_failed: {patch_res.get('error')}",
                        }
                    )
                    continue

            compiled_records.append(
                {
                    "target_id": target_id,
                    "cluster_size": len(cluster_nodes),
                    "cluster_hash": cluster_hash,
                    "sources": sources,
                    "applied": apply_truth,
                }
            )

        return {
            "ok": True,
            "candidates_found": len(candidates),
            "clusters_compiled": len(compiled_records),
            "compiled": compiled_records,
            "skipped": skipped_records,
        }

    async def run(
        self,
        scope: str = "all",
        force: bool = False,
        model_override: Optional[str] = None,
        notify_telegram: Optional[bool] = None,
        window_end: Optional[str] = None,
        max_duration_s: Optional[float] = None,
        max_batches: Optional[int] = None,
        apply_links: bool = False,
        max_links: int = DEFAULT_MAX_LINKS_PER_RUN,
        apply_truth: Optional[bool] = None,
        max_clusters: int = 3,
    ) -> CuratorResult:
        """Execute a full curation cycle."""
        start_time = time.time()
        now_dt = datetime.now(timezone.utc)
        run_id = f"run_{now_dt.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        deadline = _compute_deadline(window_end=window_end, max_duration_s=max_duration_s)

        if model_override:
            self.primary_model = model_override

        # 1. Concurrency collision guard (active agent tasks)
        if not force and check_active_agent_tasks():
            msg = "Active agent tasks detected. Deferring curation cycle."
            logger.info(msg)
            return CuratorResult(
                run_id=run_id,
                status="DEFERRED",
                is_healthy=True,
                total_nodes=0,
                total_edges=0,
                orphan_count=0,
                broken_count=0,
                broken_links=[],
                tier_used="none",
                duration_seconds=time.time() - start_time,
                summary=msg,
                beacon_path="",
                telegram_status={"ok": True, "skipped": True, "reason": "Deferred"},
                error=None,
            )

        # 2. Lockfile acquisition
        lock = CuratorLock(self.lock_file)
        if not lock.acquire(run_id, force=force):
            msg = "Another curator cycle is currently running."
            return CuratorResult(
                run_id=run_id,
                status="LOCKED",
                is_healthy=True,
                total_nodes=0,
                total_edges=0,
                orphan_count=0,
                broken_count=0,
                broken_links=[],
                tier_used="none",
                duration_seconds=time.time() - start_time,
                summary=msg,
                beacon_path="",
                telegram_status={"ok": False, "skipped": True, "reason": "Locked"},
                error=msg,
            )

        try:
            mem_dir = self.ws_path / ".botex" / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)

            await self.harvest_inactive_chat_sessions()

            # 3. Deterministic Maintenance: Index synchronization & SQLite WAL optimize
            memory.sync_memory_index(mem_dir)
            conn = memory._get_index_db(mem_dir)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.execute("PRAGMA optimize;")
            except Exception as exc:
                logger.debug("SQLite WAL optimize notice: %s", exc)
            finally:
                conn.close()

            # 4. Refresh Obsidian markdown graph index
            try:
                obsidian.export_graph_index_markdown(workspace_dir=str(self.ws_path), scope=scope)
            except Exception as exc:
                logger.warning("Obsidian index refresh notice: %s", exc)

            # 5. Graph Topology Health Check
            health = memory.find_graph_health(workspace_dir=str(self.ws_path), scope=scope)
            orphans = health.get("orphans") or health.get("orphan_nodes", [])
            broken = health.get("broken_links", [])
            total_nodes = health.get("total_nodes", 0)
            total_edges = health.get("total_edges", 0)
            all_vault_nodes = health.get("all_nodes") or []
            is_healthy = health.get("is_healthy", True)

            # Fetch sample recent nodes for fallback context
            recent_nodes = []
            try:
                search_res = memory.search_memory(
                    query="",
                    workspace_dir=str(self.ws_path),
                    scope=scope,
                    limit=40,
                )
                recent_nodes = search_res.get("memories", [])
            except Exception:
                pass

            # 6. Project-Partitioned Parallel Lane Drain + Checkpoint
            drain_state = self._load_drain_state()
            batches = _build_project_batches(
                orphans=orphans,
                all_nodes=all_vault_nodes or recent_nodes or orphans,
                drain_state=drain_state,
            )

            batches_processed = 0
            if batches:
                drain_res = await self._drain_batches_parallel(
                    batches=batches,
                    health=health,
                    lock=lock,
                    force=force,
                    deadline=deadline,
                    max_batches=max_batches,
                    drain_state=drain_state,
                )
                tier_used = drain_res["tier_used"]
                analysis = drain_res["analysis"]
                batches_processed = int(drain_res.get("batches_processed", 0))
            else:
                tier_used = "deterministic"
                analysis = {
                    "summary": (
                        "Graf wiedzy jest pusty. Brak notatek do analizy."
                        if total_nodes == 0
                        else "Wszystkie osierocone notatki są już zcheckpointowane w drain_state."
                    ),
                    "cluster_themes": [],
                    "recommended_links": [],
                    "orphan_insights": [],
                }

            # 6b. Safe Autonomous Append-Only Linking (claude-obsidian / gbrain style)
            links_applied = 0
            if apply_links and analysis.get("recommended_links"):
                node_by_id = {
                    str(n.get("id")): n
                    for n in (all_vault_nodes or recent_nodes or orphans)
                    if n.get("id")
                }
                existing_edges = {
                    (str(e.get("source") or ""), str(e.get("target") or ""))
                    for e in (health.get("edges") or [])
                }
                link_res = _apply_recommended_links(
                    workspace_dir=str(self.ws_path),
                    recommended_links=analysis.get("recommended_links") or [],
                    node_by_id=node_by_id,
                    existing_edges=existing_edges,
                    max_links=max_links,
                )
                links_applied = int(link_res.get("applied", 0))
                analysis["links_applied"] = links_applied
                analysis["applied_links"] = link_res.get("applied_links", [])
                if links_applied > 0:
                    health = memory.find_graph_health(workspace_dir=str(self.ws_path), scope=scope)
                    orphans = health.get("orphans") or health.get("orphan_nodes", [])
                    broken = health.get("broken_links", [])
                    total_nodes = health.get("total_nodes", 0)
                    total_edges = health.get("total_edges", 0)
                    all_vault_nodes = health.get("all_nodes") or all_vault_nodes
                    is_healthy = health.get("is_healthy", True)

            # 6b-2. Autonomous Compiled Truth Synthesis (Phase B)
            truth_clusters_compiled = 0
            effective_apply_truth = bool(apply_links) if apply_truth is None else bool(apply_truth)
            if effective_apply_truth and (deadline is None or time.time() < deadline):
                truth_res = await self._run_compiled_truth_phase(
                    health=health,
                    apply_truth=effective_apply_truth,
                    max_clusters=max_clusters,
                    deadline=deadline,
                )
                analysis["compiled_truth"] = truth_res
                truth_clusters_compiled = int(truth_res.get("clusters_compiled", 0))
                if truth_clusters_compiled > 0:
                    health = memory.find_graph_health(workspace_dir=str(self.ws_path), scope=scope)
                    orphans = health.get("orphans") or health.get("orphan_nodes", [])
                    broken = health.get("broken_links", [])
                    total_nodes = health.get("total_nodes", 0)
                    total_edges = health.get("total_edges", 0)
                    all_vault_nodes = health.get("all_nodes") or all_vault_nodes
                    is_healthy = health.get("is_healthy", True)

            # 6c. Dead-link reconciliation (uses full vault node_metadata, not recent_nodes[:40]):
            try:
                analysis["dead_link_repairs"] = _compute_dead_link_repairs(
                    broken, all_vault_nodes or recent_nodes or orphans
                )
            except Exception as exc:
                logger.debug("dead-link reconciliation notice: %s", exc)

            assess = _compute_health_assessment(health)
            duration = time.time() - start_time

            # 7. Write / Update Single Living Beacon Note (Zero Vault Bloat)
            beacon_text = self._render_beacon_markdown(
                run_id=run_id,
                timestamp_str=now_dt.strftime("%Y-%m-%d %H:%M UTC"),
                tier_used=tier_used,
                duration=duration,
                health=health,
                analysis=analysis,
            )

            beacon_save_res = memory.save_memory(
                workspace_dir=str(self.ws_path),
                memory_id="curator_health_status",
                title="Curator Health Status & Graph Topology",
                content=beacon_text,
                kind="system",
                tags=["curator", "health", "system", "graph-audit"],
                origin="curator",
                scope="local",
                status="active",
            )
            beacon_path = beacon_save_res.get("file_path") or str(
                mem_dir / "curator_health_status.md"
            )

            # 8. Telegram Notification
            report_payload = {
                "run_id": run_id,
                "is_healthy": is_healthy,
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "orphan_count": len(orphans),
                "broken_count": len(broken),
                "broken_links": broken,
                "tier_used": tier_used,
                "duration_seconds": duration,
                "summary": analysis.get("summary", ""),
            }

            force_tele = bool(notify_telegram) if notify_telegram is not None else False
            telegram_res = self.notifier.notify_curator_run(report_payload, force_notify=force_tele)

            # 9. Persist Run History Record & Latest Status
            run_record = {
                **report_payload,
                "timestamp": now_dt.isoformat(),
                "beacon_path": beacon_path,
                "analysis": analysis,
                "telegram": telegram_res,
                "status": "COMPLETED",
            }

            run_file = self.runs_dir / f"{run_id}.json"
            run_file.write_text(
                json.dumps(run_record, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            status_payload = {
                "last_run_id": run_id,
                "last_run_timestamp": now_dt.isoformat(),
                "status": "HEALTHY" if is_healthy else "DEGRADED",
                "is_healthy": is_healthy,
                "severity": assess["severity"],
                "degraded_reasons": assess["degraded_reasons"],
                "orphan_ratio": assess["orphan_ratio"],
                "ledger_count": assess["ledger_count"],
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "is_stale": False,
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "orphan_count": len(orphans),
                "broken_count": len(broken),
                "links_applied": links_applied,
                "truth_clusters_compiled": truth_clusters_compiled,
                "batches_processed": batches_processed,
                "tier_used": tier_used,
                "duration_seconds": round(duration, 3),
                "lock_active": False,
                "beacon_path": beacon_path,
                "summary": analysis.get("summary", ""),
                "last_telegram_status": telegram_res,
            }
            self.status_file.write_text(
                json.dumps(status_payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            return CuratorResult(
                run_id=run_id,
                status="COMPLETED",
                is_healthy=is_healthy,
                total_nodes=total_nodes,
                total_edges=total_edges,
                orphan_count=len(orphans),
                broken_count=len(broken),
                broken_links=broken,
                tier_used=tier_used,
                duration_seconds=duration,
                summary=analysis.get("summary", ""),
                beacon_path=beacon_path,
                telegram_status=telegram_res,
                error=None,
                ledger_count=assess["ledger_count"],
                severity=assess["severity"],
                degraded_reasons=assess["degraded_reasons"],
                orphan_ratio=assess["orphan_ratio"],
                links_applied=links_applied,
                batches_processed=batches_processed,
                truth_clusters_compiled=truth_clusters_compiled,
            )

        except Exception as exc:
            duration = time.time() - start_time
            err_msg = f"Curator run failed: {exc}"
            logger.exception(err_msg)

            # Attempt to record failure status
            try:
                fail_status = {
                    "last_run_id": run_id,
                    "last_run_timestamp": now_dt.isoformat(),
                    "status": "FAILED",
                    "is_healthy": False,
                    "severity": "degraded",
                    "degraded_reasons": [str(exc)],
                    "stale_after_seconds": STALE_AFTER_SECONDS,
                    "is_stale": False,
                    "tier_used": "failed",
                    "duration_seconds": round(duration, 3),
                    "lock_active": False,
                    "error": str(exc),
                }
                self.status_file.write_text(
                    json.dumps(fail_status, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

            return CuratorResult(
                run_id=run_id,
                status="FAILED",
                is_healthy=False,
                total_nodes=0,
                total_edges=0,
                orphan_count=0,
                broken_count=0,
                broken_links=[],
                tier_used="none",
                duration_seconds=duration,
                summary="Run failed with exception.",
                beacon_path="",
                telegram_status={"ok": False, "skipped": True},
                error=str(exc),
            )
        finally:
            lock.release()


# Global facade functions for CLI and cloud server
def get_curator_status(workspace_dir: str = ".") -> Dict[str, Any]:
    service = CuratorService(workspace_dir=workspace_dir)
    return service.get_status()


async def run_curation_async(
    workspace_dir: str = ".",
    scope: str = "all",
    force: bool = False,
    model_override: Optional[str] = None,
    notify_telegram: Optional[bool] = None,
    window_end: Optional[str] = None,
    max_duration_s: Optional[float] = None,
    max_batches: Optional[int] = None,
    apply_links: bool = False,
    max_links: int = DEFAULT_MAX_LINKS_PER_RUN,
    apply_truth: Optional[bool] = None,
    max_clusters: int = 3,
) -> Dict[str, Any]:
    service = CuratorService(workspace_dir=workspace_dir)
    result = await service.run(
        scope=scope,
        force=force,
        model_override=model_override,
        notify_telegram=notify_telegram,
        window_end=window_end,
        max_duration_s=max_duration_s,
        max_batches=max_batches,
        apply_links=apply_links,
        max_links=max_links,
        apply_truth=apply_truth,
        max_clusters=max_clusters,
    )
    return result.to_dict()


def run_curation_sync(
    workspace_dir: str = ".",
    scope: str = "all",
    force: bool = False,
    model_override: Optional[str] = None,
    notify_telegram: Optional[bool] = None,
    window_end: Optional[str] = None,
    max_duration_s: Optional[float] = None,
    max_batches: Optional[int] = None,
    apply_links: bool = False,
    max_links: int = DEFAULT_MAX_LINKS_PER_RUN,
    apply_truth: Optional[bool] = None,
    max_clusters: int = 3,
) -> Dict[str, Any]:
    return asyncio.run(
        run_curation_async(
            workspace_dir=workspace_dir,
            scope=scope,
            force=force,
            model_override=model_override,
            notify_telegram=notify_telegram,
            window_end=window_end,
            max_duration_s=max_duration_s,
            max_batches=max_batches,
            apply_links=apply_links,
            max_links=max_links,
            apply_truth=apply_truth,
            max_clusters=max_clusters,
        )
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BoteX Knowledge Graph Curator Runner")
    parser.add_argument("--workspace", default=".", help="Workspace directory")
    parser.add_argument("--scope", default="all", choices=["local", "global", "all"])
    parser.add_argument(
        "--force", action="store_true", help="Force run ignoring active tasks or locks"
    )
    parser.add_argument("--notify", action="store_true", help="Force Telegram notification")
    parser.add_argument("--model", default=None, help="Override LLM model")
    parser.add_argument(
        "--window-end",
        default=None,
        help="Optional local HH:MM stop time for nightly drain (e.g. 05:30)",
    )
    parser.add_argument(
        "--max-duration-s",
        type=float,
        default=None,
        help="Optional max duration in seconds",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Optional cap on project batches processed in this run",
    )
    parser.add_argument(
        "--apply-links",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply safe append-only wikilinks to ## Related (default: True for CLI)",
    )
    parser.add_argument(
        "--max-links",
        type=int,
        default=DEFAULT_MAX_LINKS_PER_RUN,
        help="Maximum wikilinks to apply in one run",
    )
    parser.add_argument(
        "--apply-truth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Synthesize authoritative ## Compiled Truth sections (default: True when --apply-links is set)",
    )
    parser.add_argument(
        "--max-clusters",
        type=int,
        default=3,
        help="Maximum candidate clusters to synthesize compiled truth for",
    )
    args = parser.parse_args()

    eff_truth = args.apply_links if args.apply_truth is None else args.apply_truth
    res = run_curation_sync(
        workspace_dir=args.workspace,
        scope=args.scope,
        force=args.force,
        model_override=args.model,
        notify_telegram=args.notify,
        window_end=args.window_end,
        max_duration_s=args.max_duration_s,
        max_batches=args.max_batches,
        apply_links=args.apply_links,
        max_links=args.max_links,
        apply_truth=eff_truth,
        max_clusters=args.max_clusters,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))

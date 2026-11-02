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
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from botex import memory, obsidian
from botex.config import resolve_workspace
from ops.curator.prompts import CURATOR_SYSTEM_PROMPT, build_curator_user_prompt
from ops.curator.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Default model configurations
DEFAULT_PRIMARY_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"
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

                # Check if stale or dead
                if (now - started_at < LOCK_MAX_STALE_SECONDS) and _is_pid_alive(pid):
                    logger.warning(
                        "Curator lock active by PID %s (started %ss ago)",
                        pid,
                        int(now - started_at),
                    )
                    return False
                else:
                    logger.info("Breaking stale or orphaned curator lock from PID %s", pid)
            except Exception:
                pass

        payload = {
            "pid": os.getpid(),
            "started_at": now,
            "run_id": run_id,
        }
        self.lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.acquired = True
        return True

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
    return False


def _compute_deterministic_recommendations(
    orphan_nodes: List[Dict[str, Any]],
    all_nodes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Heuristic link recommendations based on overlapping tags and title tokens."""
    recommended_links = []
    orphan_insights = []

    # Map words & tags to non-orphan nodes
    tag_to_nodes: Dict[str, List[str]] = {}
    non_orphans = [n for n in all_nodes if n.get("id") not in {o.get("id") for o in orphan_nodes}]

    for node in non_orphans:
        nid = node.get("id")
        for tag in node.get("tags") or []:
            norm_tag = str(tag).strip().lower()
            if norm_tag:
                tag_to_nodes.setdefault(norm_tag, []).append(nid)

    for orphan in orphan_nodes:
        oid = orphan.get("id")
        otitle = orphan.get("title", "")
        otags = [str(t).strip().lower() for t in orphan.get("tags") or []]

        candidates = set()
        for t in otags:
            for matched in tag_to_nodes.get(t, [])[:3]:
                if matched != oid:
                    candidates.add((matched, f"Wspólny tag #{t}"))

        # If no tag match, check words in title (> 4 chars)
        if not candidates and otitle:
            words = {w.lower() for w in re.findall(r"\b[a-zA-Z0-9_-]{4,}\b", otitle)}
            for node in non_orphans[:15]:
                ntitle = node.get("title", "").lower()
                matched_words = [w for w in words if w in ntitle]
                if matched_words:
                    candidates.add(
                        (node.get("id"), f"Zbieżność tematyczna: {', '.join(matched_words)}")
                    )
                    if len(candidates) >= 2:
                        break

        for target_id, rationale in list(candidates)[:2]:
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

        self.curator_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

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
                return status_data
            except Exception:
                pass

        # If no previous run file, compute live quick topology baseline
        try:
            health = memory.find_graph_health(workspace_dir=str(self.ws_path), scope="all")
            return {
                "last_run_id": None,
                "last_run_timestamp": None,
                "status": "INITIAL" if health.get("is_healthy") else "DEGRADED",
                "is_healthy": health.get("is_healthy", True),
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
                "total_nodes": 0,
                "total_edges": 0,
                "orphan_count": 0,
                "broken_count": 0,
                "tier_used": "none",
                "duration_seconds": 0.0,
                "lock_active": lock_active,
                "beacon_path": None,
            }

    async def _try_nemotron_inference(self, prompt: str) -> Optional[str]:
        """Tier 1: NVIDIA Nemotron batch inference via NvidiaProvider."""
        api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from botex.providers import ProviderRegistry

            provider = ProviderRegistry.get("nvidia")
            client = provider.get_client(api_key=api_key)
            resp = await client.chat.completions.create(
                model=self.primary_model,
                messages=[
                    {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=2048,
                timeout=30.0,
            )
            content = resp.choices[0].message.content
            return content
        except Exception as exc:
            logger.info("Tier 1 Nemotron inference unavailable or failed: %s", exc)
            return None

    async def _try_local_llm_inference(self, prompt: str) -> Optional[str]:
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
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=0.25
                )
                writer.close()
                await writer.wait_closed()
            except Exception:
                return None
        except Exception:
            return None

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=local_url, api_key="not-needed", timeout=15.0)

            resp = await client.chat.completions.create(
                model=self.local_model,
                messages=[
                    {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=2048,
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

        lines.extend(
            [
                "## Gwarancje Integralności (Invariants)",
                "- **Zasada nienaruszalności (Zero-Destruction Invariant):** Treść notatek technicznych, kod oraz wnioski agentów pozostały nienaruszone.",
                "- **Zasada jednego beaconu (Zero-Bloat Invariant):** Kurator nadpisuje wyłącznie niniejszą notatkę `curator_health_status.md`.",
            ]
        )

        return "\n".join(lines)

    async def run(
        self,
        scope: str = "all",
        force: bool = False,
        model_override: Optional[str] = None,
        notify_telegram: Optional[bool] = None,
    ) -> CuratorResult:
        """Execute a full curation cycle."""
        start_time = time.time()
        now_dt = datetime.now(timezone.utc)
        run_id = f"run_{now_dt.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

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

            is_healthy = health.get("is_healthy", True)

            # Fetch sample recent nodes for context
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

            # 6. Multi-Tier Batch LLM Analysis (Nemotron -> Bielik -> Deterministic)
            tier_used, analysis = await self._execute_batch_llm_analysis(
                health=health,
                recent_nodes=recent_nodes,
                orphan_nodes=orphans,
            )

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
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "orphan_count": len(orphans),
                "broken_count": len(broken),
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
) -> Dict[str, Any]:
    service = CuratorService(workspace_dir=workspace_dir)
    result = await service.run(
        scope=scope,
        force=force,
        model_override=model_override,
        notify_telegram=notify_telegram,
    )
    return result.to_dict()


def run_curation_sync(
    workspace_dir: str = ".",
    scope: str = "all",
    force: bool = False,
    model_override: Optional[str] = None,
    notify_telegram: Optional[bool] = None,
) -> Dict[str, Any]:
    return asyncio.run(
        run_curation_async(
            workspace_dir=workspace_dir,
            scope=scope,
            force=force,
            model_override=model_override,
            notify_telegram=notify_telegram,
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
    args = parser.parse_args()

    res = run_curation_sync(
        workspace_dir=args.workspace,
        scope=args.scope,
        force=args.force,
        model_override=args.model,
        notify_telegram=args.notify,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Analytics & Quota Ledger
==============================
Tracks token consumption, estimated USD costs, lines modified, and enforces daily budget limits.
Provides rich CLI reporting and snapshot storage inspection.
"""
import hashlib
import json
import sys
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

try:
    from .config import (
        analytics_file as _cfg_analytics_file, snapshot_dir as _cfg_snapshot_dir,
        analytics_setting,
    )
    from .contracts import STATUS_FAILURE_KIND
    from .security import mask_secrets
except (ImportError, ValueError):
    from config import (
        analytics_file as _cfg_analytics_file, snapshot_dir as _cfg_snapshot_dir,
        analytics_setting,
    )
    from contracts import STATUS_FAILURE_KIND
    from security import mask_secrets

ANALYTICS_FILE = _cfg_analytics_file()
SNAPSHOT_DIR = _cfg_snapshot_dir()

# Approximate pricing per 1M tokens for common OpenRouter models (fallback estimates)
MODEL_PRICING_ESTIMATES = {
    "openrouter/auto": {"prompt": 0.20, "completion": 0.80},
    "qwen/qwen-2.5-coder-32b-instruct": {"prompt": 0.07, "completion": 0.16},
    "deepseek/deepseek-chat": {"prompt": 0.14, "completion": 0.28},
    "anthropic/claude-3.7-sonnet": {"prompt": 3.00, "completion": 15.00},
    "google/gemini-2.5-pro": {"prompt": 1.25, "completion": 5.00},
    "google/gemini-2.5-flash": {"prompt": 0.075, "completion": 0.30},
}


def calculate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate cost in USD based on model pricing."""
    pricing = MODEL_PRICING_ESTIMATES.get(model, {"prompt": 0.20, "completion": 0.80})
    p_cost = (prompt_tokens / 1_000_000) * pricing["prompt"]
    c_cost = (completion_tokens / 1_000_000) * pricing["completion"]
    return round(p_cost + c_cost, 6)


def load_analytics() -> List[Dict[str, Any]]:
    """Load all run records from .agent_analytics.json."""
    if not ANALYTICS_FILE.exists():
        return []
    try:
        data = json.loads(ANALYTICS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_analytics(records: List[Dict[str, Any]]):
    """Persist records to .agent_analytics.json."""
    ANALYTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANALYTICS_FILE.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def log_run(
    task_id: str,
    model: str,
    duration_s: float,
    steps: int,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    files_touched: List[str],
    lines_added: int,
    lines_removed: int,
    status: str = "DONE",
    summary: str = "",
    cost_usd: Optional[float] = None,
    failure_kind: Optional[str] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a completed BoteX run into the ledger.

    Privacy: the summary is secret-masked before persisting. Unless
    ``analytics.store_full_text`` is enabled, only a truncated preview is
    stored; the complete text survives solely as ``summary_sha256``.
    """
    if cost_usd is None:
        cost_usd = calculate_cost_usd(model, prompt_tokens, completion_tokens)

    masked_summary = mask_secrets(summary or "")
    stored_summary = masked_summary
    if not analytics_setting("store_full_text"):
        preview_chars = int(analytics_setting("preview_chars") or 160)
        stored_summary = masked_summary[:preview_chars]
        if len(masked_summary) > preview_chars:
            stored_summary += "…"

    if failure_kind is None:
        failure_kind = STATUS_FAILURE_KIND.get(status)

    entry = {
        "task_id": task_id,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "provider": provider,
        "failure_kind": failure_kind,
        "duration_s": round(duration_s, 2),
        "steps": steps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cost_usd": cost_usd,
        "files_touched": files_touched,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "status": status,
        "summary": stored_summary,
        "summary_sha256": hashlib.sha256(masked_summary.encode("utf-8")).hexdigest(),
    }

    records = load_analytics()
    records.append(entry)
    save_analytics(records)
    return entry


def check_budget_limit(daily_limit_usd: float) -> tuple[bool, float]:
    """
    Check if today's total spending exceeds daily_limit_usd.
    Returns (is_exceeded, today_spend_usd).
    """
    if daily_limit_usd <= 0.0:
        return False, 0.0

    today_str = datetime.now().strftime("%Y-%m-%d")
    records = load_analytics()
    today_spend = sum(
        r.get("cost_usd", 0.0)
        for r in records
        if r.get("timestamp", "").startswith(today_str)
    )
    return today_spend >= daily_limit_usd, round(today_spend, 4)


# ---------------------------------------------------------------------------
# CLI Presentation (Rich or Clean ASCII)
# ---------------------------------------------------------------------------
def print_run_summary(entry: Dict[str, Any]):
    """Pretty print run stats to stderr."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel

        console = Console(stderr=True)
        table = Table(title=f"BoteX Run Summary — Task: {entry['task_id']}", show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Status", entry.get("status", "DONE"))
        table.add_row("Model", entry.get("model", "unknown"))
        table.add_row("Duration", f"{entry.get('duration_s', 0)}s ({entry.get('steps', 0)} steps)")
        table.add_row(
            "Tokens",
            f"Prompt: {entry.get('prompt_tokens', 0):,} | "
            f"Comp: {entry.get('completion_tokens', 0):,} | "
            f"Cached: {entry.get('cached_tokens', 0):,}"
        )
        table.add_row("Cost (USD)", f"${entry.get('cost_usd', 0.0):.5f}")
        table.add_row("Files Modified", ", ".join(entry.get("files_touched", [])) or "(none)")
        table.add_row(
            "Line Balance",
            f"[green]+{entry.get('lines_added', 0)}[/green] / [red]-{entry.get('lines_removed', 0)}[/red]"
        )

        console.print(table)
        if entry.get("summary"):
            console.print(Panel(entry["summary"], title="Summary", style="dim"))
    except ImportError:
        # Fallback to plain ASCII
        lines = [
            f"=== BOTEX RUN: {entry.get('task_id')} ===",
            f"Status: {entry.get('status')} | Model: {entry.get('model')} | Time: {entry.get('duration_s')}s | Steps: {entry.get('steps')}",
            f"Tokens: prompt={entry.get('prompt_tokens')} comp={entry.get('completion_tokens')} cached={entry.get('cached_tokens')}",
            f"Cost: ${entry.get('cost_usd'):.5f} | Lines: +{entry.get('lines_added')}/-{entry.get('lines_removed')}",
            f"Files: {', '.join(entry.get('files_touched', [])) or '(none)'}",
            f"Summary: {entry.get('summary', '')}",
            "=" * 40
        ]
        sys.stderr.write("\n".join(lines) + "\n")


def cli_stats(period: str = "week"):
    """CLI stats view: weekly or monthly aggregation."""
    from botex.i18n import t
    records = load_analytics()
    if not records:
        print(t("an.no_data"))
        return

    days = 30 if period == "month" else 7
    cutoff = datetime.now() - timedelta(days=days)

    filtered = [
        r for r in records
        if datetime.fromisoformat(r["timestamp"]) >= cutoff
    ]

    total_runs = len(filtered)
    total_cost = sum(r.get("cost_usd", 0.0) for r in filtered)
    total_prompt = sum(r.get("prompt_tokens", 0) for r in filtered)
    total_comp = sum(r.get("completion_tokens", 0) for r in filtered)
    total_cached = sum(r.get("cached_tokens", 0) for r in filtered)
    total_added = sum(r.get("lines_added", 0) for r in filtered)
    total_removed = sum(r.get("lines_removed", 0) for r in filtered)

    # Per model breakdown
    by_model = {}
    for r in filtered:
        m = r.get("model", "other")
        if m not in by_model:
            by_model[m] = {"runs": 0, "done_runs": 0, "cost": 0.0, "tokens": 0, "failures": {}}
        by_model[m]["runs"] += 1
        st = r.get("status", "")
        if st == "DONE":
            by_model[m]["done_runs"] += 1
        else:
            fk = r.get("failure_kind") or st or "unknown"
            by_model[m]["failures"][fk] = by_model[m]["failures"].get(fk, 0) + 1
        by_model[m]["cost"] += r.get("cost_usd", 0.0)
        by_model[m]["tokens"] += (r.get("prompt_tokens", 0) + r.get("completion_tokens", 0))

    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        tbl = Table(title=t("an.title", days=days, period=period.upper()))
        tbl.add_column(t("an.metric"), style="cyan")
        tbl.add_column(t("an.value"), style="green")

        tbl.add_row(t("an.tasks"), str(total_runs))
        tbl.add_row(t("an.total_cost"), f"${total_cost:.4f}")
        tbl.add_row(t("an.prompt_tok"), f"{total_prompt:,}")
        tbl.add_row(t("an.comp_tok"), f"{total_comp:,}")
        tbl.add_row(t("an.cached_tok"), f"{total_cached:,}")
        tbl.add_row(t("an.line_bal"), f"+{total_added:,} / -{total_removed:,}")
        console.print(tbl)

        m_table = Table(title=t("an.model_eff"))
        m_table.add_column("Model", style="bold")
        m_table.add_column(t("an.runs"), justify="right")
        m_table.add_column(t("an.done_rate"), justify="right")
        m_table.add_column(t("an.top_failure"), justify="left")
        m_table.add_column(t("an.tokens"), justify="right")
        m_table.add_column(t("an.cost"), justify="right")

        for m, stats in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
            done_pct = (stats["done_runs"] / stats["runs"] * 100) if stats["runs"] > 0 else 0.0
            top_fail = (
                max(stats["failures"].items(), key=lambda x: x[1])[0]
                if stats["failures"]
                else "-"
            )
            m_table.add_row(
                m,
                str(stats["runs"]),
                f"{done_pct:.1f}%",
                top_fail,
                f"{stats['tokens']:,}",
                f"${stats['cost']:.4f}",
            )
        console.print(m_table)
    except ImportError:
        print(f"=== {t('an.title', days=days, period=period.upper())} ===")
        print(f"{t('an.tasks')}: {total_runs}")
        print(f"{t('an.total_cost')}: ${total_cost:.4f} USD")
        print(f"{t('an.tokens')}: prompt={total_prompt:,}, comp={total_comp:,}, cached={total_cached:,}")
        print(f"{t('an.line_bal')}: +{total_added:,} / -{total_removed:,}")
        print("\n" + t("an.models"))
        for m, stats in by_model.items():
            done_pct = (stats["done_runs"] / stats["runs"] * 100) if stats["runs"] > 0 else 0.0
            top_fail = (
                max(stats["failures"].items(), key=lambda x: x[1])[0]
                if stats["failures"]
                else "-"
            )
            print(f"  * {m}: " + t("an.runs_fmt", runs=stats['runs'], tokens=f"{stats['tokens']:,}", cost=f"{stats['cost']:.4f}") + f" | DONE rate: {done_pct:.1f}% | Top failure: {top_fail}")


def cli_history(task_id: Optional[str] = None):
    """View task history or details of a specific task."""
    from botex.i18n import t
    records = load_analytics()
    if not records:
        print(t("an.no_history"))
        return

    if task_id:
        match = [r for r in records if r.get("task_id") == task_id or r.get("task_id", "").startswith(task_id)]
        if not match:
            print(t("an.not_found", id=task_id))
            return
        entry = match[-1]
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return

    # List last 10 tasks
    recent = records[-10:]
    recent.reverse()
    print(t("an.recent"))
    for r in recent:
        ts = r.get("timestamp", "")[:16].replace("T", " ")
        print(f"[{r.get('task_id')}] {ts} | {r.get('model')} | {r.get('status')} | ${r.get('cost_usd', 0):.4f} | {r.get('summary', '')[:60]}")

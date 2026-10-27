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
from typing import Any, Dict, List, Optional

try:
    from .config import (
        analytics_file as _cfg_analytics_file,
    )
    from .config import (
        analytics_setting,
    )
    from .config import (
        resolve_provider as _resolve_provider,
    )
    from .config import (
        snapshot_dir as _cfg_snapshot_dir,
    )
    from .contracts import STATUS_FAILURE_KIND
    from .security import coerce_text, mask_secrets
except (ImportError, ValueError):
    from config import (
        analytics_file as _cfg_analytics_file,
    )
    from config import (
        analytics_setting,
    )
    from config import (
        resolve_provider as _resolve_provider,
    )
    from config import (
        snapshot_dir as _cfg_snapshot_dir,
    )
    from contracts import STATUS_FAILURE_KIND
    from security import coerce_text, mask_secrets

ANALYTICS_FILE = _cfg_analytics_file()
SNAPSHOT_DIR = _cfg_snapshot_dir()

# Approximate pricing per 1M tokens for common OpenRouter models (fallback estimates)
MODEL_PRICING_ESTIMATES = {
    "openrouter/auto": {"prompt": 0.20, "completion": 0.80},
    "openrouter/pareto-code": {"prompt": 0.25, "completion": 1.00},
    "qwen/qwen-2.5-coder-32b-instruct": {"prompt": 0.07, "completion": 0.16},
    "deepseek/deepseek-chat": {"prompt": 0.14, "completion": 0.28},
    "anthropic/claude-3.7-sonnet": {"prompt": 3.00, "completion": 15.00},
    "google/gemini-2.5-pro": {"prompt": 1.25, "completion": 5.00},
    "google/gemini-2.5-flash": {"prompt": 0.075, "completion": 0.30},
    # Bare slugs served by the Google AI Studio OpenAI-compat endpoint
    # (provider "google"); OpenRouter carries the same models under
    # "google/" prefixes above.
    "gemini-2.5-pro": {"prompt": 1.25, "completion": 10.00},
    "gemini-2.5-flash": {"prompt": 0.30, "completion": 2.50},
    "gemini-2.5-flash-lite": {"prompt": 0.10, "completion": 0.40},
}


_FREE_MODEL_MARKERS = (":free", "openrouter/free")
_FREE_MODEL_PREFIXES = ("openrouter-free/", "ollama/", "nvidia/")
_FREE_PROVIDER_NAMES = frozenset({"ollama"})


def is_free_model(
    model: str,
    provider_cfg: Optional[Dict[str, Any]] = None,
    provider_name: str = "",
) -> bool:
    """Whether a model run is guaranteed zero-cost.

    Covers free-tier slugs (``:free``, ``openrouter/free``, ``openrouter-free/``),
    zero-cost namespaces (``ollama/``, ``nvidia/``), and providers that cannot
    bill per request — ``local: true`` endpoints (e.g. local Ollama / Bielik)
    or any provider carrying a static ``api_key_fallback``.
    """
    slug = (model or "").strip().lower()
    if any(marker in slug for marker in _FREE_MODEL_MARKERS):
        return True
    if slug.startswith(_FREE_MODEL_PREFIXES):
        return True
    if (provider_name or "").strip().lower() in _FREE_PROVIDER_NAMES:
        return True
    cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
    return bool(cfg.get("local") or cfg.get("api_key_fallback"))


# Cached prompt reads are billed at a discount on OpenAI-compatible endpoints
# (~50% on OpenAI upstreams; varies by provider). Free models and providers
# that never price cached reads (NVIDIA NIM, local) yield $0 savings.
CACHE_READ_DISCOUNT = 0.50


def calculate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    provider_name: str = "",
) -> float:
    """Estimate cost in USD based on model pricing.

    Full-price fallback: cached prompt reads (billed at ~50% on supporting
    providers) are not discounted here — the authoritative amount comes from
    generation telemetry when available.
    """
    if is_free_model(model, provider_name=provider_name):
        return 0.0
    pricing = MODEL_PRICING_ESTIMATES.get(model, {"prompt": 0.20, "completion": 0.80})
    p_cost = (prompt_tokens / 1_000_000) * pricing["prompt"]
    c_cost = (completion_tokens / 1_000_000) * pricing["completion"]
    return round(p_cost + c_cost, 6)


def _cache_savings_usd(
    model: str,
    cached_tokens: int,
    provider_name: str = "",
    provider_cfg: Optional[Dict[str, Any]] = None,
) -> float:
    """Estimated USD saved by cached prompt reads for one model."""
    if cached_tokens <= 0 or is_free_model(model, provider_cfg, provider_name):
        return 0.0
    pricing = MODEL_PRICING_ESTIMATES.get(model, {"prompt": 0.20, "completion": 0.80})
    return (cached_tokens / 1_000_000) * pricing["prompt"] * CACHE_READ_DISCOUNT


def load_analytics() -> List[Dict[str, Any]]:
    """Load all run records from .agent_analytics.json."""
    target_file = _cfg_analytics_file()
    if not target_file.exists():
        return []
    try:
        data = json.loads(target_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_analytics(records: List[Dict[str, Any]]):
    """Persist records to .agent_analytics.json."""
    target_file = _cfg_analytics_file()
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


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
    cache_write_tokens: int = 0,
    queue_time_ms: Optional[float] = None,
    generation_time_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Record a completed BoteX run into the ledger.

    Privacy: the summary is secret-masked before persisting. Unless
    ``analytics.store_full_text`` is enabled, only a truncated preview is
    stored; the complete text survives solely as ``summary_sha256``.
    """
    if cost_usd is None:
        cost_usd = calculate_cost_usd(model, prompt_tokens, completion_tokens, provider or "")
    else:
        cost_usd = round(float(cost_usd), 6)

    masked_summary = mask_secrets(summary or "")
    stored_summary = masked_summary
    if not analytics_setting("store_full_text"):
        preview_chars = int(analytics_setting("preview_chars") or 160)
        stored_summary = masked_summary[:preview_chars]
        if len(masked_summary) > preview_chars:
            stored_summary += "…"

    if failure_kind is None:
        failure_kind = STATUS_FAILURE_KIND.get(status)

    entry: Dict[str, Any] = {
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
        "cache_write_tokens": int(cache_write_tokens or 0),
        "cost_usd": cost_usd,
        "files_touched": files_touched,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "status": status,
        "summary": stored_summary,
        "summary_sha256": hashlib.sha256(masked_summary.encode("utf-8")).hexdigest(),
    }
    if queue_time_ms is not None:
        entry["queue_time_ms"] = round(float(queue_time_ms), 2)
    if generation_time_ms is not None:
        entry["generation_time_ms"] = round(float(generation_time_ms), 2)

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
        r.get("cost_usd", 0.0) for r in records if r.get("timestamp", "").startswith(today_str)
    )
    return today_spend >= daily_limit_usd, round(today_spend, 4)


def aggregate_stats(records: List[Dict[str, Any]], days: Optional[int] = None) -> Dict[str, Any]:
    """Pure aggregation over run records — shared by CLI reports and the REST API.

    ``days=None`` aggregates all records; a positive value keeps only runs newer
    than ``now - days``. Returns JSON-serializable totals, per-model breakdown,
    daily time series, and the most recent runs.
    """
    cutoff = datetime.now() - timedelta(days=days) if days and days > 0 else None
    filtered: List[Dict[str, Any]] = []
    for r in records:
        if cutoff is None:
            filtered.append(r)
            continue
        try:
            if datetime.fromisoformat(r.get("timestamp", "")) >= cutoff:
                filtered.append(r)
        except (TypeError, ValueError):
            continue

    by_model: Dict[str, Dict[str, Any]] = {}
    daily: Dict[str, Dict[str, Any]] = {}
    status_counts: Dict[str, int] = {}
    durations: List[float] = []
    gen_times: List[float] = []
    prov_cfgs: Dict[str, Dict[str, Any]] = {}

    for r in filtered:
        st = r.get("status") or "UNKNOWN"
        status_counts[st] = status_counts.get(st, 0) + 1

        m = r.get("model") or "other"
        bm = by_model.setdefault(
            m,
            {
                "model": m,
                "runs": 0,
                "done_runs": 0,
                "cost_usd": 0.0,
                "tokens": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "cache_savings_usd": 0.0,
                "failures": {},
            },
        )
        bm["runs"] += 1
        if st == "DONE":
            bm["done_runs"] += 1
        else:
            fk = r.get("failure_kind") or st or "unknown"
            bm["failures"][fk] = bm["failures"].get(fk, 0) + 1
        cost = r.get("cost_usd", 0.0) or 0.0
        toks = (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0)
        bm["cost_usd"] += cost
        bm["tokens"] += toks
        bm["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        cached_i = r.get("cached_tokens", 0) or 0
        bm["cached_tokens"] += cached_i
        pname = (r.get("provider") or "").strip().lower()
        if pname and pname not in prov_cfgs:
            try:
                _pname, pcfg = _resolve_provider(pname)
                prov_cfgs[pname] = pcfg if isinstance(pcfg, dict) else {}
            except Exception:
                prov_cfgs[pname] = {}
        bm["cache_savings_usd"] += _cache_savings_usd(
            m, cached_i, pname, prov_cfgs.get(pname) if pname else None
        )

        day = str(r.get("timestamp", ""))[:10]
        if day:
            d = daily.setdefault(day, {"date": day, "runs": 0, "cost_usd": 0.0, "tokens": 0})
            d["runs"] += 1
            d["cost_usd"] += cost
            d["tokens"] += toks

        if isinstance(r.get("duration_s"), (int, float)):
            durations.append(float(r["duration_s"]))
        if isinstance(r.get("generation_time_ms"), (int, float)):
            gen_times.append(float(r["generation_time_ms"]))

    models_out: List[Dict[str, Any]] = []
    for bm in sorted(by_model.values(), key=lambda x: x["cost_usd"], reverse=True):
        bm["done_pct"] = round(bm["done_runs"] / bm["runs"] * 100, 1) if bm["runs"] else 0.0
        bm["cache_hit_pct"] = (
            round(min(bm["cached_tokens"] / bm["prompt_tokens"], 1.0) * 100, 1)
            if bm["prompt_tokens"]
            else 0.0
        )
        bm["cost_usd"] = round(bm["cost_usd"], 6)
        bm["top_failure"] = (
            max(bm["failures"].items(), key=lambda x: x[1])[0] if bm["failures"] else None
        )
        bm["cache_savings_usd"] = round(bm["cache_savings_usd"], 6)
        models_out.append(bm)

    prompt_tok = sum(r.get("prompt_tokens", 0) or 0 for r in filtered)
    cached_tok = sum(r.get("cached_tokens", 0) or 0 for r in filtered)
    done = status_counts.get("DONE", 0)

    recent = sorted(filtered, key=lambda r: str(r.get("timestamp", "")), reverse=True)[:12]
    recent_out = [
        {
            "task_id": r.get("task_id"),
            "timestamp": r.get("timestamp"),
            "model": r.get("model"),
            "provider": r.get("provider"),
            "status": r.get("status"),
            "failure_kind": r.get("failure_kind"),
            "cost_usd": r.get("cost_usd", 0.0),
            "duration_s": r.get("duration_s"),
            "tokens": (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0),
        }
        for r in recent
    ]

    return {
        "runs": len(filtered),
        "done_runs": done,
        "failed_runs": len(filtered) - done,
        "done_pct": round(done / len(filtered) * 100, 1) if filtered else 0.0,
        "cost_usd": round(sum(r.get("cost_usd", 0.0) or 0.0 for r in filtered), 6),
        "prompt_tokens": prompt_tok,
        "completion_tokens": sum(r.get("completion_tokens", 0) or 0 for r in filtered),
        "cached_tokens": cached_tok,
        "cache_savings_usd": round(sum(bm["cache_savings_usd"] for bm in models_out), 6),
        # prompt_tokens already includes cached reads for OpenAI-schema providers;
        # clamped at 100% for providers that report prompt excluding cached.
        "cache_hit_pct": (round(min(cached_tok / prompt_tok, 1.0) * 100, 1) if prompt_tok else 0.0),
        "lines_added": sum(r.get("lines_added", 0) or 0 for r in filtered),
        "lines_removed": sum(r.get("lines_removed", 0) or 0 for r in filtered),
        "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "avg_generation_ms": round(sum(gen_times) / len(gen_times), 1) if gen_times else None,
        "status_counts": status_counts,
        "by_model": models_out,
        "daily": [daily[k] for k in sorted(daily)],
        "recent": recent_out,
    }


# ---------------------------------------------------------------------------
# CLI Presentation (Rich or Clean ASCII)
# ---------------------------------------------------------------------------
def print_run_summary(entry: Dict[str, Any]):
    """Pretty print run stats to stderr."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

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
            f"Cached: {entry.get('cached_tokens', 0):,}",
        )
        table.add_row("Cost (USD)", f"${entry.get('cost_usd', 0.0):.5f}")
        table.add_row("Files Modified", ", ".join(entry.get("files_touched", [])) or "(none)")
        table.add_row(
            "Line Balance",
            f"[green]+{entry.get('lines_added', 0)}[/green] / [red]-{entry.get('lines_removed', 0)}[/red]",
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
            "=" * 40,
        ]
        output_str = "\n".join(lines) + "\n"
        sys.stderr.write(coerce_text(output_str))


def cli_stats(period: str = "week"):
    """CLI stats view: weekly or monthly aggregation."""
    from botex.i18n import t

    records = load_analytics()
    if not records:
        print(t("an.no_data"))
        return

    days = 30 if period == "month" else 7
    stats = aggregate_stats(records, days)

    total_runs = stats["runs"]
    total_cost = stats["cost_usd"]
    total_prompt = stats["prompt_tokens"]
    total_comp = stats["completion_tokens"]
    total_cached = stats["cached_tokens"]
    total_added = stats["lines_added"]
    total_removed = stats["lines_removed"]
    by_model = {m["model"]: m for m in stats["by_model"]}

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

        for m, stats in by_model.items():
            top_fail = stats["top_failure"] or "-"
            m_table.add_row(
                m,
                str(stats["runs"]),
                f"{stats['done_pct']:.1f}%",
                top_fail,
                f"{stats['tokens']:,}",
                f"${stats['cost_usd']:.4f}",
            )
        console.print(m_table)
    except ImportError:
        print(f"=== {t('an.title', days=days, period=period.upper())} ===")
        print(f"{t('an.tasks')}: {total_runs}")
        print(f"{t('an.total_cost')}: ${total_cost:.4f} USD")
        print(
            f"{t('an.tokens')}: prompt={total_prompt:,}, comp={total_comp:,}, cached={total_cached:,}"
        )
        print(f"{t('an.line_bal')}: +{total_added:,} / -{total_removed:,}")
        print("\n" + t("an.models"))
        for m, stats in by_model.items():
            done_pct = (stats["done_runs"] / stats["runs"] * 100) if stats["runs"] > 0 else 0.0
            top_fail = (
                max(stats["failures"].items(), key=lambda x: x[1])[0] if stats["failures"] else "-"
            )
            print(
                f"  * {m}: "
                + t(
                    "an.runs_fmt",
                    runs=stats["runs"],
                    tokens=f"{stats['tokens']:,}",
                    cost=f"{stats['cost_usd']:.4f}",
                )
                + f" | DONE rate: {done_pct:.1f}% | Top failure: {top_fail}"
            )


def cli_history(task_id: Optional[str] = None):
    """View task history or details of a specific task."""
    from botex.i18n import t

    records = load_analytics()
    if not records:
        print(t("an.no_history"))
        return

    if task_id:
        match = [
            r
            for r in records
            if r.get("task_id") == task_id or r.get("task_id", "").startswith(task_id)
        ]
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
        print(
            f"[{r.get('task_id')}] {ts} | {r.get('model')} | {r.get('status')} | ${r.get('cost_usd', 0):.4f} | {r.get('summary', '')[:60]}"
        )

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Structural local triage over the analytics ledger (Plan 060 Step 3).

Reads ``.agent_analytics.jsonl`` (and optional persisted task result JSON from
``--tasks-dir``) and emits ONLY structural counts/distributions/medians.
Never prints task text, summaries, file contents, or paths beyond extension
category. Error evidence is reduced to masked, bounded categorical tokens.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_LEDGER = ".agent_analytics.jsonl"

# Categorical error-code tokens only; matched against already-masked text and
# reduced to short codes, never free text.
_HTTP_STATUS_RE = re.compile(r"\b(?:status(?:_code)?|http)\D{0,3}(\d{3})\b", re.IGNORECASE)
_ERROR_TOKEN_RE = re.compile(
    r"\b([a-z][a-z0-9_]{3,40}(?:error|exception|timeout|denied|quota|limit))\b", re.IGNORECASE
)


def _categorize_error(text: str) -> str:
    from botex.tools.security import mask_secrets

    masked = mask_secrets(text or "")
    statuses = sorted(set(_HTTP_STATUS_RE.findall(masked)))
    tokens = sorted({t.lower() for t in _ERROR_TOKEN_RE.findall(masked)})
    parts = []
    if statuses:
        parts.append("http_" + "/".join(statuses[:3]))
    if tokens:
        parts.extend(tokens[:3])
    return "+".join(parts) if parts else "unclassified"


def _iter_ledger(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError as e:
        print(f"cannot read ledger {path}: {type(e).__name__}", file=sys.stderr)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def analyze_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    combo = Counter()
    syntax_total = 0
    rollback_total = 0
    rollback_reason_cats: Counter = Counter()
    taxonomy_dist: Counter = Counter()
    mode_dist: Counter = Counter()
    durations: dict[tuple, list[float]] = {}
    steps: dict[tuple, list[float]] = {}
    error_cats: Counter = Counter()

    for rec in records:
        provider = str(rec.get("provider") or "unknown")
        model = str(rec.get("model") or "unknown")
        status = str(rec.get("status") or "unknown")
        failure_kind = rec.get("failure_kind")
        key = (provider, model, status, str(failure_kind) if failure_kind else None)
        combo[key] += 1
        syntax_total += int(rec.get("syntax_rejections") or 0)
        rollback_total += int(rec.get("rollback_count") or 0)
        if rec.get("taxonomy"):
            taxonomy_dist[str(rec["taxonomy"])] += 1
        if rec.get("mode"):
            mode_dist[str(rec["mode"])] += 1
        dur = rec.get("duration_s")
        if isinstance(dur, (int, float)):
            durations.setdefault((provider, model, status), []).append(float(dur))
        st = rec.get("steps")
        if isinstance(st, (int, float)):
            steps.setdefault((provider, model, status), []).append(float(st))
        summary = rec.get("summary")
        if summary and status not in ("DONE",):
            cat = _categorize_error(str(summary))
            if cat:
                error_cats[cat] += 1

    medians = {
        "|".join(k): {
            "median_duration_s": _median(durations.get(k, [])),
            "median_steps": _median(steps.get(k, [])),
            "samples": len(durations.get(k, [])) or len(steps.get(k, [])),
        }
        for k in sorted(set(durations) | set(steps))
    }
    return {
        "records": len(records),
        "counts_by_provider_model_status_failure_kind": {
            "|".join(k for k in key if k): n for key, n in combo.most_common()
        },
        "syntax_rejections_total": syntax_total,
        "rollback_count_total": rollback_total,
        "rollback_reason_categories": dict(rollback_reason_cats),
        "taxonomy_distribution": dict(taxonomy_dist.most_common()),
        "mode_distribution": dict(mode_dist.most_common()),
        "medians_by_provider_model_status": medians,
        "recurring_error_categories": dict(error_cats.most_common(10)),
    }


def analyze_task_files(tasks_dir: Path) -> dict[str, Any] | None:
    """Structural roll-up of persisted task result JSON files, counts only."""
    if not tasks_dir.is_dir():
        return None
    statuses: Counter = Counter()
    failure_kinds: Counter = Counter()
    rollback_total = 0
    syntax_total = 0
    reason_cats: Counter = Counter()
    files = sorted(tasks_dir.rglob("*.json"))[:2000]
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        rec = data.get("result") if isinstance(data, dict) else None
        if not isinstance(rec, dict):
            rec = data if isinstance(data, dict) else {}
        statuses[str(rec.get("status") or "unknown")] += 1
        if rec.get("failure_kind"):
            failure_kinds[str(rec["failure_kind"])] += 1
        rollback_total += int(rec.get("rollback_count") or 0)
        syntax_total += int(rec.get("syntax_rejections") or 0)
        for reason in rec.get("rollback_reasons") or []:
            cat = _categorize_error(str(reason))
            if cat:
                reason_cats[cat] += 1
    return {
        "task_files_scanned": len(files),
        "status_counts": dict(statuses.most_common()),
        "failure_kind_counts": dict(failure_kinds.most_common()),
        "syntax_rejections_total": syntax_total,
        "rollback_count_total": rollback_total,
        "rollback_reason_categories": dict(reason_cats.most_common(10)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Structural, content-free triage of the BoteX analytics ledger."
    )
    parser.add_argument("--ledger", default=_DEFAULT_LEDGER)
    parser.add_argument("--tasks-dir", default=None)
    args = parser.parse_args(argv)

    records = list(_iter_ledger(Path(args.ledger)))
    out: dict[str, Any] = analyze_records(records)
    if args.tasks_dir:
        tasks_out = analyze_task_files(Path(args.tasks_dir))
        if tasks_out is not None:
            out["tasks_dir"] = tasks_out
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

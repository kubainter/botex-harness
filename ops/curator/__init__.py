# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Autonomous Knowledge Graph Curator
========================================
Option A: Deterministic + Multi-Tier LLM Batch Curation with Zero Recipe Pollution.
Living Beacon note: curator_health_status.md in Memory Vault.
"""

from .curator_service import (
    CuratorResult,
    CuratorService,
    get_curator_status,
    run_curation_async,
    run_curation_sync,
)
from .recall_curator import synthesize_recall
from .telegram import TelegramNotifier, send_telegram_notification

__all__ = [
    "CuratorResult",
    "CuratorService",
    "get_curator_status",
    "run_curation_async",
    "run_curation_sync",
    "synthesize_recall",
    "TelegramNotifier",
    "send_telegram_notification",
]

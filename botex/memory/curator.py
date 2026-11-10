# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Curator Module Facade
===========================
Exports Curator service methods from botex/ops/curator/.
Preserves zero recipe pollution and avoids circular dependencies.
"""

from botex.ops.curator import (
    CuratorResult,
    CuratorService,
    TelegramNotifier,
    get_curator_status,
    run_curation_async,
    run_curation_sync,
    send_telegram_notification,
)

__all__ = [
    "CuratorResult",
    "CuratorService",
    "TelegramNotifier",
    "get_curator_status",
    "run_curation_async",
    "run_curation_sync",
    "send_telegram_notification",
]

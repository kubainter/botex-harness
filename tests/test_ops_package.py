# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Guard tests for the ``ops`` → ``botex.ops`` package rename (arena B2)."""

import importlib


def test_botex_ops_curator_importable():
    assert importlib.import_module("botex.ops.curator") is not None


def test_botex_ops_reexports():
    mod = importlib.import_module("botex.ops.curator")
    for name in (
        "CuratorResult",
        "CuratorService",
        "get_curator_status",
        "run_curation_async",
        "run_curation_sync",
        "synthesize_recall",
        "TelegramNotifier",
        "send_telegram_notification",
    ):
        assert callable(getattr(mod, name)), f"botex.ops.curator.{name} missing"


def test_memory_curator_facade_uses_botex_ops():
    mod = importlib.import_module("botex.memory.curator")
    assert callable(mod.run_curation_async)
    assert callable(mod.get_curator_status)


def test_recall_curator_module_path():
    mod = importlib.import_module("botex.ops.curator.recall_curator")
    assert mod.__name__ == "botex.ops.curator.recall_curator"

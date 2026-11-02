# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Telegram Notification Dispatcher for BoteX Curator
===================================================
Provides SSRF-safe, resilient notifications to Telegram chats via official Bot API.
Respects Zero .env Access Protocol (reads solely from os.environ).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


def _is_truthy(val: Optional[str]) -> bool:
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on", "t")


def get_telegram_config() -> Dict[str, Any]:
    """Retrieve Telegram configuration from system environment variables."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    alert_on_success = _is_truthy(os.environ.get("TELEGRAM_ALERT_ON_SUCCESS", "false"))
    silent = _is_truthy(os.environ.get("TELEGRAM_SILENT", "false"))
    return {
        "bot_token": bot_token,
        "chat_id": chat_id,
        "alert_on_success": alert_on_success,
        "silent": silent,
        "is_configured": bool(bot_token and chat_id),
    }


def mask_telegram_token(text: str) -> str:
    """Mask Telegram bot token from URLs or error logs to prevent secret leaks (CWE-312)."""
    if not text:
        return ""
    # Pattern matching /bot<token>/
    return re.sub(r"(bot[0-9]{6,12}:[A-Za-z0-9_-]{20,})", r"bot***MASKED_TOKEN***", text)


def format_curator_telegram_message(report: Dict[str, Any]) -> str:
    """Format curator report dictionary into a concise Telegram Markdown message."""
    is_healthy = report.get("is_healthy", False)
    status_emoji = "✅" if is_healthy else "⚠️"
    status_text = "PASSED (Healthy)" if is_healthy else "NEEDS ATTENTION"

    total_nodes = report.get("total_nodes", 0)
    total_edges = report.get("total_edges", 0)
    orphan_count = report.get("orphan_count", 0)
    broken_count = report.get("broken_count", 0)
    tier_used = report.get("tier_used", "deterministic")
    duration = report.get("duration_seconds", 0.0)
    run_id = report.get("run_id", "manual")

    lines = [
        f"{status_emoji} *BoteX Knowledge Graph Curator*",
        f"• Status: *{status_text}*",
        f"• Run ID: `{run_id}`",
        f"• Tier: `{tier_used}` (duration: {duration:.2f}s)",
        "",
        "*Graph Topology Metrics:*",
        f"• Total Notes: *{total_nodes}*",
        f"• Total Edges: *{total_edges}*",
        f"• Orphan Notes: *{orphan_count}*",
        f"• Broken Links: *{broken_count}*",
    ]

    broken_links = report.get("broken_links", [])
    if broken_links:
        lines.append("")
        lines.append("*Broken Links Detected:*")
        for bl in broken_links[:5]:
            src = bl.get("source", "?")
            tgt = bl.get("target", "?")
            lines.append(f"  - `{src}` ➔ `[[{tgt}]]` (missing)")
        if len(broken_links) > 5:
            lines.append(f"  - ... i jeszcze {len(broken_links) - 5} innych")

    lines.append("")
    lines.append("📄 Beacon: `.botex/memory/curator_health_status.md`")
    return "\n".join(lines)


def send_telegram_notification(
    text: str,
    parse_mode: str = "Markdown",
    silent: Optional[bool] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Synchronously deliver a message to a Telegram chat.
    Safe against crashes: if telegram credentials are not configured, returns skipped status.
    """
    cfg = get_telegram_config()
    token = bot_token or cfg["bot_token"]
    target_chat = chat_id or cfg["chat_id"]
    is_silent = silent if silent is not None else cfg["silent"]

    if not token or not target_chat:
        return {
            "ok": False,
            "skipped": True,
            "reason": "Telegram credentials not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing).",
        }

    url = _TELEGRAM_API_URL_TEMPLATE.format(token=token)
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_notification": bool(is_silent),
        "disable_web_page_preview": True,
    }

    try:
        raw_body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "BoteX-Curator/2.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_code = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            res_data = json.loads(body)
            return {
                "ok": res_data.get("ok", resp_code == 200),
                "skipped": False,
                "status_code": resp_code,
                "message_id": res_data.get("result", {}).get("message_id"),
            }
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", errors="replace")
        safe_msg = mask_telegram_token(err_msg)
        if exc.code == 400 and parse_mode:
            logger.warning(
                "Telegram Markdown parsing failed (%s), retrying as plain text fallback...",
                safe_msg,
            )
            return send_telegram_notification(
                text=text,
                parse_mode="",
                silent=is_silent,
                bot_token=token,
                chat_id=target_chat,
                timeout=timeout,
            )
        logger.warning("Telegram notification failed (HTTP %s): %s", exc.code, safe_msg)
        return {
            "ok": False,
            "skipped": False,
            "status_code": exc.code,
            "error": safe_msg,
        }
    except Exception as exc:
        safe_exc = mask_telegram_token(str(exc))
        logger.warning("Telegram notification encountered unexpected error: %s", safe_exc)
        return {
            "ok": False,
            "skipped": False,
            "error": safe_exc,
        }


class TelegramNotifier:
    """Class wrapper for Telegram notifications."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        alert_on_success: Optional[bool] = None,
        silent: Optional[bool] = None,
    ) -> None:
        cfg = get_telegram_config()
        self.bot_token = bot_token or cfg["bot_token"]
        self.chat_id = chat_id or cfg["chat_id"]
        self.alert_on_success = (
            alert_on_success if alert_on_success is not None else cfg["alert_on_success"]
        )
        self.silent = silent if silent is not None else cfg["silent"]

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str, silent: Optional[bool] = None) -> Dict[str, Any]:
        return send_telegram_notification(
            text=text,
            silent=self.silent if silent is None else silent,
            bot_token=self.bot_token,
            chat_id=self.chat_id,
        )

    def notify_curator_run(
        self, report: Dict[str, Any], force_notify: bool = False
    ) -> Dict[str, Any]:
        """Notify Telegram about curator run result if conditions met."""
        is_healthy = report.get("is_healthy", False)
        # Notify if: unhealthy/broken OR alert_on_success is True OR force_notify is True
        should_send = force_notify or (not is_healthy) or self.alert_on_success
        if not should_send:
            return {
                "ok": True,
                "skipped": True,
                "reason": "Run healthy and TELEGRAM_ALERT_ON_SUCCESS is false.",
            }
        text = format_curator_telegram_message(report)
        return self.send(text)

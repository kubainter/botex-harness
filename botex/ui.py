# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX CLI styling helpers
=========================
Minimal ANSI accent colors for terminal output. No dependencies — colors are
applied only at the presentation layer (CLI prints). Strings returned by MCP
tools stay plain. Degrades gracefully: disabled when stdout is not a TTY or
when NO_COLOR is set.
"""
import os
import sys

_enabled = None

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def colors_enabled() -> bool:
    global _enabled
    if _enabled is None:
        ok = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if ok and os.name == "nt":
            # Enable ANSI VT processing on Windows 10+ consoles.
            try:
                os.system("")
            except Exception:
                pass
        _enabled = ok
    return _enabled


def paint(style: str, text: str) -> str:
    if not colors_enabled():
        return text
    return f"{_CODES.get(style, '')}{text}{_RESET}"


def title(text: str) -> str:
    return paint("bold", paint("cyan", text)) if colors_enabled() else text


def accent(text: str) -> str:
    return paint("cyan", text)


def ok(text: str) -> str:
    return paint("green", text)


def warn(text: str) -> str:
    return paint("yellow", text)


def err(text: str) -> str:
    return paint("red", text)


def dim(text: str) -> str:
    return paint("dim", text)


def mark_status(text: str) -> str:
    """Colorize common status markers inside a plain status string."""
    if not colors_enabled():
        return text
    for token, style in (
        ("[SUKCES]", "green"), ("[SUCCESS]", "green"), ("[OK]", "green"),
        ("DONE", "green"),
        ("[BŁĄD]", "red"), ("[FAILURE]", "red"), ("[BRAK]", "red"),
        ("[MISSING]", "red"), ("ERROR", "red"),
        ("STAGNANT", "yellow"), ("[default]", "cyan"),
    ):
        text = text.replace(token, f"{_CODES[style]}{token}{_RESET}")
    return text

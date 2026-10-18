# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Recipes & Personas Registry
=================================
Predefined operational personas and specialized task workflows.
"""

from pathlib import Path
from typing import List, Optional

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"


try:
    from .security import sanitize_identifier
except (ImportError, ValueError):
    from security import sanitize_identifier


def list_recipes() -> List[str]:
    """Return list of available recipe names (without .md extension)."""
    if not RECIPES_DIR.exists() or not RECIPES_DIR.is_dir():
        return []
    return sorted([f.stem for f in RECIPES_DIR.glob("*.md") if f.is_file()])


def load_recipe(name: str) -> Optional[str]:
    """Load recipe markdown content by name. Returns None if not found."""
    if not name or not isinstance(name, str):
        return None
    raw = name.strip()
    if raw.endswith(".md"):
        raw = raw[:-3]
    clean_name = sanitize_identifier(raw)
    if not clean_name or not RECIPES_DIR.exists() or not RECIPES_DIR.is_dir():
        return None

    for entry in RECIPES_DIR.glob("*.md"):
        if entry.stem == clean_name and entry.is_file():
            try:
                return entry.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                return None
    return None

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
    from .security import resolve_safe_path, sanitize_identifier
except (ImportError, ValueError):
    from security import resolve_safe_path, sanitize_identifier


def list_recipes() -> List[str]:
    """Return list of available recipe names (without .md extension)."""
    if not RECIPES_DIR.exists() or not RECIPES_DIR.is_dir():
        return []
    return sorted([f.stem for f in RECIPES_DIR.glob("*.md") if f.is_file()])


def load_recipe(name: str) -> Optional[str]:
    """Load recipe markdown content by name. Returns None if not found."""
    if not name or not isinstance(name, str):
        return None
    clean_name = sanitize_identifier(name[:-3] if name.strip().endswith(".md") else name.strip())
    if not clean_name:
        return None

    try:
        target = resolve_safe_path(RECIPES_DIR, f"{clean_name}.md")
        if target.exists() and target.is_file():
            return target.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None
    return None

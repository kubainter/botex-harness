# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Recipes & Personas Registry
=================================
Predefined operational personas and specialized task workflows.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"


try:
    from .security import sanitize_identifier
except (ImportError, ValueError):
    from security import sanitize_identifier


def _split_recipe(content: str) -> Tuple[Dict[str, Any], str]:
    """Extract optional JSON metadata frontmatter and clean system prompt body.

    Supports frontmatter fence format:
    ```json
    { ... }
    ```
    or
    ---
    { ... }
    ---
    Returns (metadata_dict, clean_system_prompt).
    """
    if not content:
        return {}, ""

    # Match ```json ... ``` at top
    json_block = re.match(r"^\s*```(?:json)?\s*\n(.*?)\n```\s*\n?", content, re.DOTALL)
    if json_block:
        raw_json = json_block.group(1).strip()
        body = content[json_block.end() :].strip()
        try:
            meta = json.loads(raw_json)
            if isinstance(meta, dict):
                return meta, body
        except Exception:
            pass

    # Match --- ... --- at top containing JSON
    dash_block = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if dash_block:
        raw_dash = dash_block.group(1).strip()
        body = content[dash_block.end() :].strip()
        try:
            meta = json.loads(raw_dash)
            if isinstance(meta, dict):
                return meta, body
        except Exception:
            pass

    return {}, content.strip()


def list_recipes() -> List[str]:
    """Return list of available recipe names (without .md extension)."""
    if not RECIPES_DIR.exists() or not RECIPES_DIR.is_dir():
        return []
    return sorted([f.stem for f in RECIPES_DIR.glob("*.md") if f.is_file()])


def _read_recipe_raw(name: str) -> Optional[str]:
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


def load_recipe(name: str) -> Optional[str]:
    """Load recipe markdown content by name with frontmatter stripped. Returns None if not found."""
    raw = _read_recipe_raw(name)
    if raw is None:
        return None
    _meta, body = _split_recipe(raw)
    return body


def load_recipe_metadata(name: str) -> Dict[str, Any]:
    """Load recipe JSON frontmatter metadata. Returns empty dict if not found or unparseable."""
    raw = _read_recipe_raw(name)
    if raw is None:
        return {}
    meta, _body = _split_recipe(raw)
    return meta

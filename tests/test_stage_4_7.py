# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for Stage 4.7 Prep features: recipes metadata, dynamic benchmarks, and Ollama adapter."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from botex.benchmarks import fetch_openrouter_benchmarks, get_recommended_models
from botex.recipes import _split_recipe, list_recipes, load_recipe, load_recipe_metadata
from botex.server_cloud import app


def test_recipes_split_and_metadata():
    """Verify _split_recipe strips JSON frontmatter and load_recipe_metadata extracts it."""
    sample = """```json
{
  "preferred_tier": "high",
  "weights": {"coding": 0.8, "speed": 0.1, "cost": 0.1}
}
```
# Test Persona
Role: Expert
Instructions go here.
"""
    meta, body = _split_recipe(sample)
    assert meta["preferred_tier"] == "high"
    assert meta["weights"]["coding"] == 0.8
    assert "# Test Persona" in body
    assert "```json" not in body

    # Verify existing and newly added recipes
    recipes = list_recipes()
    assert "ui-craftsman" in recipes
    assert "security-taint-hardener" in recipes
    assert "architecture-adr" in recipes

    ui_meta = load_recipe_metadata("ui-craftsman")
    assert ui_meta.get("preferred_tier") == "standard"
    assert ui_meta.get("weights", {}).get("coding") == 0.4
    ui_body = load_recipe("ui-craftsman")
    assert ui_body is not None
    assert "```json" not in ui_body
    assert "Role: Modern Frontend & Interface Craft Specialist" in ui_body

    sec_meta = load_recipe_metadata("security-taint-hardener")
    assert sec_meta.get("preferred_tier") == "high"
    assert sec_meta.get("weights", {}).get("coding") == 0.7

    adr_meta = load_recipe_metadata("architecture-adr")
    assert adr_meta.get("preferred_tier") == "high"
    assert adr_meta.get("weights", {}).get("cost") == 0.4


def test_benchmarks_atomic_cache_and_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify benchmarks cache saves atomically and falls back to stale cache on network error."""
    cache_file = tmp_path / "benchmarks_test.json"
    monkeypatch.setenv("BOTEX_BENCHMARKS_CACHE_FILE", str(cache_file))

    # 1. Mock successful API fetch
    mock_payload = {
        "data": [
            {
                "model_id": "anthropic/claude-3.5-sonnet",
                "coding_index": 82.5,
                "speed": {"tokens_per_second": 75.0},
            },
            {
                "model_id": "google/gemini-2.5-flash",
                "coding_index": 71.0,
                "speed": {"tokens_per_second": 140.0},
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        data = fetch_openrouter_benchmarks(cache_ttl_hours=24.0)
        assert "anthropic/claude-3.5-sonnet" in data
        assert data["anthropic/claude-3.5-sonnet"]["coding_index"] == 82.5
        assert data["anthropic/claude-3.5-sonnet"]["tokens_per_second"] == 75.0

    assert cache_file.is_file()

    # 2. Verify cache hit without calling urlopen
    with patch("urllib.request.urlopen", side_effect=RuntimeError("Should not be called")):
        cached = fetch_openrouter_benchmarks(cache_ttl_hours=24.0)
        assert cached == data

    # 3. Verify stale-forever fallback when cache is expired but network fails
    # Age the cache file beyond TTL
    old_time = time.time() - (48 * 3600)
    import os

    os.utime(cache_file, (old_time, old_time))

    with patch("urllib.request.urlopen", side_effect=Exception("Network error 429")):
        stale = fetch_openrouter_benchmarks(cache_ttl_hours=24.0)
        assert "anthropic/claude-3.5-sonnet" in stale


def test_recommend_models_with_recipe_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify get_recommended_models incorporates recipe weights and benchmark metadata."""
    cache_file = tmp_path / "benchmarks_test2.json"
    monkeypatch.setenv("BOTEX_BENCHMARKS_CACHE_FILE", str(cache_file))

    # Pre-populate cache with benchmark metrics
    benchmarks_cache = {
        "qwen/qwen-2.5-coder-32b-instruct": {
            "coding_index": 85.0,
            "tokens_per_second": 60.0,
        },
        "google/gemini-2.5-flash": {
            "coding_index": 70.0,
            "tokens_per_second": 150.0,
        },
    }
    cache_file.write_text(json.dumps(benchmarks_cache), encoding="utf-8")

    models_payload = {
        "data": [
            {
                "id": "qwen/qwen-2.5-coder-32b-instruct",
                "name": "Qwen 2.5 Coder 32B",
                "pricing": {"prompt": "0.0000008", "completion": "0.0000016"},
                "context_length": 65536,
                "supported_parameters": ["tools"],
            },
            {
                "id": "google/gemini-2.5-flash",
                "name": "Gemini 2.5 Flash",
                "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
                "context_length": 100000,
                "supported_parameters": ["tools"],
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(models_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        # 1. Recommend for ui-craftsman recipe
        rec_ui = get_recommended_models(task_type="coding", recipe="ui-craftsman")
        assert len(rec_ui) > 0
        first = rec_ui[0]
        assert "benchmark" in first
        assert "coding_index" in first["benchmark"]
        assert "tokens_per_second" in first["benchmark"]


def test_cloud_endpoints_recipes_and_recommendations():
    """Verify /api/recipes and /api/models/recommended REST endpoints."""
    client = TestClient(app)

    # 1. /api/recipes
    r_resp = client.get("/api/recipes")
    assert r_resp.status_code == 200
    recipes = r_resp.json()
    assert isinstance(recipes, list)
    r_names = [r["name"] for r in recipes]
    assert "ui-craftsman" in r_names
    assert "security-taint-hardener" in r_names
    assert "architecture-adr" in r_names

    # 2. /api/models/recommended with mocked model list
    mock_models = {
        "data": [
            {
                "id": "meta/llama-3.3-70b-instruct",
                "name": "Llama 3.3 70B",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "context_length": 131072,
                "supported_parameters": ["tools"],
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_models).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        rec_resp = client.get("/api/models/recommended?recipe=ui-craftsman&task_type=coding")
        assert rec_resp.status_code == 200
        recs = rec_resp.json()
        assert isinstance(recs, list)
        assert len(recs) == 1
        assert recs[0]["id"] == "meta/llama-3.3-70b-instruct"

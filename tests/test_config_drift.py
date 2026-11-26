# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Config drift computation tests (plan 053, step 3)
=================================================
Pure-function suite for ``compute_config_drift``: ``None`` vs ``False``
distinction, fallback inclusion, dedupe semantics, and id normalization.
No I/O, no network, no clock.
"""

from __future__ import annotations

from typing import Dict

import pytest

from botex.catalog import ModelMetadata, compute_config_drift


def make_catalog(*ids: str) -> Dict[str, ModelMetadata]:
    return {mid: ModelMetadata(id=mid) for mid in ids}


# ---------------------------------------------------------------------------
# None vs False distinction
# ---------------------------------------------------------------------------


def test_missing_catalog_yields_none_not_false():
    """A provider absent from catalogs is 'no data' (None), never drift (False)."""
    config = {"nvidia": {"models": {"default": "nvidia/some-model"}}}
    records = compute_config_drift(config, {})
    assert len(records) == 1
    assert records[0]["present_in_catalog"] is None


def test_empty_catalog_yields_none():
    """An empty catalog cannot confirm anything — treated as no data (None).

    sync_provider_catalogs never writes empty catalogs, so an empty mapping
    means the fetch effectively produced no usable data rather than a
    definitive empty model list.
    """
    config = {"nvidia": {"models": {"default": "nvidia/some-model"}}}
    records = compute_config_drift(config, {"nvidia": {}})
    assert records[0]["present_in_catalog"] is None


def test_present_model_is_true_and_absent_is_false():
    config = {
        "nvidia": {
            "models": {
                "default": "nvidia/live-model",
                "coding": "nvidia/dead-model",
            }
        }
    }
    catalogs = {"nvidia": make_catalog("nvidia/live-model")}
    records = compute_config_drift(config, catalogs)
    by_slot = {r["slot"]: r for r in records}
    assert by_slot["default"]["present_in_catalog"] is True
    assert by_slot["coding"]["present_in_catalog"] is False


def test_none_and_false_coexist_across_providers():
    config = {
        "nvidia": {"models": {"default": "nvidia/dead-model"}},
        "groq": {"models": {"default": "qwen/qwen3.8-27b"}},
    }
    catalogs = {"nvidia": make_catalog("nvidia/other-model")}
    records = compute_config_drift(config, catalogs)
    by_provider = {r["provider"]: r for r in records}
    assert by_provider["nvidia"]["present_in_catalog"] is False
    assert by_provider["groq"]["present_in_catalog"] is None


# ---------------------------------------------------------------------------
# Fallback inclusion
# ---------------------------------------------------------------------------


def test_model_fallbacks_dict_form_included():
    config = {
        "groq": {
            "models": {"default": "openai/gpt-oss-20b"},
            "model_fallbacks": {"default": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]},
        }
    }
    catalogs = {"groq": make_catalog("openai/gpt-oss-20b")}
    records = compute_config_drift(config, catalogs)
    slots = {(r["slot"], r["model_id"]) for r in records}
    assert ("default", "openai/gpt-oss-20b") in slots
    assert ("fallback:default", "qwen/qwen3.8-27b") in slots
    assert ("fallback:default", "openai/gpt-oss-120b") in slots
    fb = [r for r in records if r["slot"] == "fallback:default"]
    assert all(r["present_in_catalog"] is False for r in fb)


def test_model_fallbacks_bare_list_form():
    """A bare list fallback form is conserved under the generic 'fallback' slot."""
    config = {
        "nvidia": {
            "models": {"fast": "nvidia/model-a"},
            "model_fallbacks": ["nvidia/model-b", "nvidia/model-c"],
        }
    }
    catalogs = {"nvidia": make_catalog("nvidia/model-a", "nvidia/model-b")}
    records = compute_config_drift(config, catalogs)
    fb = [r for r in records if r["slot"] == "fallback"]
    assert len(fb) == 2
    by_id = {r["model_id"]: r for r in fb}
    assert by_id["nvidia/model-b"]["present_in_catalog"] is True
    assert by_id["nvidia/model-c"]["present_in_catalog"] is False


def test_fallback_single_string_value_tolerated():
    config = {
        "groq": {
            "models": {},
            "model_fallbacks": {"coding": "qwen/qwen3.8-27b"},
        }
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/qwen3.8-27b")})
    assert len(records) == 1
    assert records[0]["slot"] == "fallback:coding"
    assert records[0]["present_in_catalog"] is True


# ---------------------------------------------------------------------------
# Dedupe semantics
# ---------------------------------------------------------------------------


def test_duplicate_id_same_slot_deduped():
    """Same id listed twice in one slot collapses to a single record.

    Dedupe key is (provider, slot, normalized id): a duplicated entry in one
    fallback list carries no extra information, while the same id in distinct
    slots stays distinct so night-pulse can report each affected slot.
    """
    config = {
        "groq": {
            "models": {},
            "model_fallbacks": {"default": ["qwen/x", "qwen/x", "qwen/y"]},
        }
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    assert len(records) == 2
    assert [r["model_id"] for r in records].count("qwen/x") == 1


def test_same_id_distinct_slots_not_deduped():
    config = {
        "groq": {
            "models": {"default": "qwen/x", "coding": "qwen/x"},
        }
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    assert len(records) == 2
    assert {r["slot"] for r in records} == {"default", "coding"}


def test_same_id_in_models_and_fallback_distinct_slots():
    config = {
        "groq": {
            "models": {"default": "qwen/x"},
            "model_fallbacks": {"default": ["qwen/x"]},
        }
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    assert len(records) == 2
    assert {r["slot"] for r in records} == {"default", "fallback:default"}


# ---------------------------------------------------------------------------
# Normalization & robustness
# ---------------------------------------------------------------------------


def test_matching_is_case_insensitive_and_models_prefix_agnostic():
    config = {
        "google": {
            "models": {
                "coding": "Gemini-2.5-Flash",
                "fast": "models/gemini-2.5-pro",
            }
        }
    }
    catalogs = {"google": make_catalog("gemini-2.5-flash", "gemini-2.5-pro")}
    records = compute_config_drift(config, catalogs)
    assert all(r["present_in_catalog"] is True for r in records)


def test_non_dict_provider_config_skipped():
    config = {
        "nvidia": "not-a-dict",
        "groq": {"models": {"default": "qwen/x"}},
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    assert len(records) == 1
    assert records[0]["provider"] == "groq"


def test_non_string_model_values_skipped():
    config = {
        "groq": {
            "models": {"default": None, "coding": 42, "fast": "qwen/x"},
            "model_fallbacks": {"default": [None, 7, "qwen/y"]},
        }
    }
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    ids = {(r["slot"], r["model_id"]) for r in records}
    assert ids == {("fast", "qwen/x"), ("fallback:default", "qwen/y")}


def test_missing_models_and_fallbacks_keys_yield_no_records():
    config = {"nvidia": {"api_key_env": "NVIDIA_API_KEY"}}
    records = compute_config_drift(config, {"nvidia": make_catalog("a")})
    assert records == []


def test_record_shape():
    config = {"groq": {"models": {"default": "qwen/x"}}}
    records = compute_config_drift(config, {"groq": make_catalog("qwen/x")})
    assert set(records[0].keys()) == {"provider", "model_id", "slot", "present_in_catalog"}


def test_models_bare_list_form_tolerated():
    """A list-form `models` value degrades conservatively to the 'models' slot."""
    config = {"ollama": {"models": ["bielik:latest", "qwen3:8b"]}}
    catalogs = {"ollama": make_catalog("bielik:latest")}
    records = compute_config_drift(config, catalogs)
    assert len(records) == 2
    assert all(r["slot"] == "models" for r in records)
    by_id = {r["model_id"]: r for r in records}
    assert by_id["bielik:latest"]["present_in_catalog"] is True
    assert by_id["qwen3:8b"]["present_in_catalog"] is False


def test_realistic_config_shape():
    """Mirror of the real botex.config.json providers subtree shape."""
    config = {
        "nvidia": {
            "api_key_env": "NVIDIA_API_KEY",
            "models": {
                "default": "nvidia/nemotron-3-ultra-550b-a55b",
                "coding": "poolside/laguna-xs-2.1",
                "fast": "nvidia/nemotron-3.5-lightning-30b-a3b",
            },
        },
        "openrouter": {
            "models": {"review": "z-ai/glm-4.6", "fast": "qwen/qwen3-coder"},
            "model_fallbacks": {
                "default": ["z-ai/glm-4.6", "qwen/qwen3-coder", "deepseek/deepseek-v3.2"],
                "coding": ["z-ai/glm-4.6", "qwen/qwen3-coder"],
            },
        },
    }
    catalogs = {
        "nvidia": make_catalog("nvidia/nemotron-3-ultra-550b-a55b"),
    }
    records = compute_config_drift(config, catalogs)
    nvidia = [r for r in records if r["provider"] == "nvidia"]
    assert len(nvidia) == 3
    by_slot = {r["slot"]: r for r in nvidia}
    assert by_slot["default"]["present_in_catalog"] is True
    assert by_slot["coding"]["present_in_catalog"] is False
    assert by_slot["fast"]["present_in_catalog"] is False
    # openrouter absent from catalogs -> every record is None, never False.
    openrouter = [r for r in records if r["provider"] == "openrouter"]
    assert len(openrouter) == 7  # 2 models + 3 default fallbacks + 2 coding fallbacks
    assert all(r["present_in_catalog"] is None for r in openrouter)


@pytest.mark.parametrize("empty_catalogs", [{}, {"nvidia": None}], ids=["no-key", "none-value"])
def test_no_catalog_data_variants(empty_catalogs):
    config = {"nvidia": {"models": {"default": "nvidia/x"}}}
    records = compute_config_drift(config, empty_catalogs)
    assert records[0]["present_in_catalog"] is None

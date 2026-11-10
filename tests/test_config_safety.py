from __future__ import annotations

import os

from botex.config import (
    _dotenv_is_disabled,
    normalize_provider_name,
    resolve_model,
    resolve_provider,
)


def test_test_harness_disables_dotenv_loading():
    assert os.environ["BOTEX_DISABLE_DOTENV"] == "1"
    assert _dotenv_is_disabled()


def test_normalize_provider_aliases():
    assert normalize_provider_name("nvmi") == "nvidia"
    assert normalize_provider_name("NIM") == "nvidia"
    assert normalize_provider_name("or-free") == "openrouter-free"
    assert normalize_provider_name("openrouter:free") == "openrouter-free"
    assert normalize_provider_name("gemini") == "google"
    assert normalize_provider_name("bielik") == "ollama"
    prov_name, prov_cfg = resolve_provider("nvmi")
    assert prov_name == "nvidia"
    assert isinstance(prov_cfg, dict) and prov_cfg


def test_resolve_model_provider_binding():
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct", provider="openrouter-free")
        == "meta-llama/llama-3.3-70b-instruct:free"
    )
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct:free", provider="nvmi")
        == "meta/llama-3.3-70b-instruct"
    )
    assert (
        resolve_model("meta-llama/llama-3.3-70b-instruct", provider="openrouter")
        == "meta-llama/llama-3.3-70b-instruct"
    )

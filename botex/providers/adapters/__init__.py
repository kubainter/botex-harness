# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Provider adapters package."""

from .zai import ProviderQuotaExceededError, ZaiGLMProvider

__all__ = ["ProviderQuotaExceededError", "ZaiGLMProvider"]

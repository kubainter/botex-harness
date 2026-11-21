# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Multiplexing stdio MCP client bridge to BoteX Cloud (SSE pool + HTTP reads)."""

from .client import McpBridgeClient

__all__ = ["McpBridgeClient"]

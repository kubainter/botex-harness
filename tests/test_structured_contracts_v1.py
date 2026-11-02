# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for Canonical JSON Schema v1 and format_handoff_response."""

from botex.contracts import (
    CANONICAL_HANDOFF_SCHEMA_V1,
    format_handoff_response,
    parse_structured_completion,
)


def test_canonical_handoff_schema_structure():
    assert CANONICAL_HANDOFF_SCHEMA_V1["title"] == "CanonicalHandoffV1"
    assert "version" in CANONICAL_HANDOFF_SCHEMA_V1["required"]
    assert "status" in CANONICAL_HANDOFF_SCHEMA_V1["required"]
    assert "summary" in CANONICAL_HANDOFF_SCHEMA_V1["required"]


def test_format_handoff_response():
    resp = format_handoff_response(
        status="DONE",
        summary="Refactored auth module",
        artifact_refs=["file:///src/auth.py"],
        memory_keys=["task/auth-123"],
        trace_id="00-test-trace-id-01",
    )
    assert resp["version"] == "1.0"
    assert resp["status"] == "DONE"
    assert resp["summary"] == "Refactored auth module"
    assert resp["artifact_refs"] == ["file:///src/auth.py"]
    assert resp["memory_keys"] == ["task/auth-123"]
    assert resp["trace_id"] == "00-test-trace-id-01"
    assert resp["blocking_reason"] is None


def test_parse_structured_completion_extracts_v1_fields():
    raw_json = """```json
    {
        "version": "1.0",
        "status": "DONE",
        "summary": "Completed successfully",
        "artifact_refs": ["file:///path/to/file.py"],
        "memory_keys": ["mem_123"],
        "trace_id": "trace-999"
    }
    ```"""
    parsed = parse_structured_completion(raw_json)
    assert parsed is not None
    assert parsed["version"] == "1.0"
    assert parsed["status"] == "DONE"
    assert parsed["summary"] == "Completed successfully"
    assert parsed["artifact_refs"] == ["file:///path/to/file.py"]
    assert parsed["memory_keys"] == ["mem_123"]
    assert parsed["trace_id"] == "trace-999"

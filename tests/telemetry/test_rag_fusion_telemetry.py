"""Tests for TelemetryEntry RAG Fusion fields (Task 3.3)."""

from __future__ import annotations

import inspect

import pytest

from archon_search.telemetry.entry import TelemetryEntry


def test_telemetry_entry_rag_fusion_fields_default_none() -> None:
    """TelemetryEntry created without new kwargs has rag_fusion fields at None."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=[],
        latency_ms=1.0,
    )
    assert entry.rag_fusion_applied is None
    assert entry.rag_fusion_queries_used is None


def test_telemetry_entry_from_search_tool_result_with_rag_fusion() -> None:
    """from_search_tool_result sets both rag_fusion fields when provided."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=["a", "b"],
        latency_ms=10.0,
        rag_fusion_applied=True,
        rag_fusion_queries_used=2,
    )
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == 2


def test_telemetry_entry_from_search_tool_result_rag_fusion_false() -> None:
    """from_search_tool_result correctly stores rag_fusion_applied=False."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=[],
        latency_ms=5.0,
        rag_fusion_applied=False,
        rag_fusion_queries_used=0,
    )
    assert entry.rag_fusion_applied is False
    assert entry.rag_fusion_queries_used == 0


def test_telemetry_entry_from_search_multi_result_with_rag_fusion() -> None:
    """from_search_multi_result sets both rag_fusion fields when provided."""
    entry = TelemetryEntry.from_search_multi_result(
        collections=["docs", "code"],
        fanout_count=2,
        result_count=5,
        latency_ms=20.0,
        excluded_count=0,
        rag_fusion_applied=True,
        rag_fusion_queries_used=3,
    )
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == 3


def test_telemetry_entry_from_search_multi_result_defaults_to_none() -> None:
    """from_search_multi_result with no rag_fusion kwargs -> fields stay None."""
    entry = TelemetryEntry.from_search_multi_result(
        collections=["docs"],
        fanout_count=1,
        result_count=2,
        latency_ms=5.0,
        excluded_count=0,
    )
    assert entry.rag_fusion_applied is None
    assert entry.rag_fusion_queries_used is None


def test_telemetry_entry_from_explain_result_with_rag_fusion() -> None:
    """from_explain_result sets both rag_fusion fields when provided."""
    entry = TelemetryEntry.from_explain_result(
        collection="docs",
        result_count=3,
        latency_ms=15.0,
        rag_fusion_applied=True,
        rag_fusion_queries_used=2,
    )
    assert entry.rag_fusion_applied is True
    assert entry.rag_fusion_queries_used == 2


def test_telemetry_entry_from_explain_result_defaults_to_none() -> None:
    """from_explain_result with no rag_fusion kwargs -> fields stay None."""
    entry = TelemetryEntry.from_explain_result(
        collection="docs",
        result_count=0,
        latency_ms=1.0,
    )
    assert entry.rag_fusion_applied is None
    assert entry.rag_fusion_queries_used is None


def test_telemetry_entry_no_query_param() -> None:
    """Static inspection: all three updated factory methods have no 'query' parameter."""
    factories = [
        TelemetryEntry.from_search_tool_result,
        TelemetryEntry.from_search_multi_result,
        TelemetryEntry.from_explain_result,
    ]
    for factory in factories:
        params = inspect.signature(factory).parameters
        assert "query" not in params, (
            f"{factory.__name__} must not accept a 'query' parameter"
        )


def test_telemetry_rag_fusion_fields_not_present_when_none_in_jsonl() -> None:
    """Fields with None value are omitted or null in serialized form."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=[],
        latency_ms=1.0,
    )
    data = entry.model_dump()
    # None means not set — both fields should exist but be None
    assert "rag_fusion_applied" in data
    assert data["rag_fusion_applied"] is None
    assert "rag_fusion_queries_used" in data
    assert data["rag_fusion_queries_used"] is None


def test_telemetry_entry_search_with_context_accepts_rag_fusion_fields() -> None:
    """search_with_context endpoint variant also supports rag_fusion fields."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search_with_context",
        collection="docs",
        result_doc_ids=["x"],
        latency_ms=8.0,
        rag_fusion_applied=False,
        rag_fusion_queries_used=0,
    )
    assert entry.endpoint == "search_with_context"
    assert entry.rag_fusion_applied is False
    assert entry.rag_fusion_queries_used == 0

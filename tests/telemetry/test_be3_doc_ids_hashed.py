"""Tests for BE-3: doc_ids_hashed field + doc_id_hasher factory param (D8).

Unit tests covering:
- TelemetryEntry.doc_ids_hashed field default
- from_search_tool_result with/without hasher
- Edge cases: None result_doc_ids, empty list
- DOCUMENTED_SCHEMA_FIELDS inclusion
- Other factory signatures unchanged (S16)

Integration test:
- JSONL round-trip with doc_ids_hashed=True
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from archon_search.telemetry.entry import (
    DOCUMENTED_SCHEMA_FIELDS,
    TelemetryEntry,
)


# ---------------------------------------------------------------------------
# unit: doc_ids_hashed field default
# ---------------------------------------------------------------------------


def test_doc_ids_hashed_field_defaults_false_in_model() -> None:
    """TelemetryEntry model default for doc_ids_hashed is False."""
    entry = TelemetryEntry(
        query_id="deadbeef" * 4,
        timestamp="2026-06-25T00:00:00Z",
        endpoint="search",
        latency_ms=1.0,
        status="ok",
    )
    assert entry.doc_ids_hashed is False


# ---------------------------------------------------------------------------
# unit: from_search_tool_result without hasher (S1)
# ---------------------------------------------------------------------------


def test_from_search_tool_result_no_hasher_raw_ids_and_false_flag() -> None:
    """No hasher → raw ids passed through, doc_ids_hashed=False (S1)."""
    raw_ids = ["raw-doc-id-1", "raw-doc-id-2"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=5.0,
    )
    assert entry.result_doc_ids == raw_ids
    assert entry.doc_ids_hashed is False


# ---------------------------------------------------------------------------
# unit: from_search_tool_result with hasher (S2)
# ---------------------------------------------------------------------------


def test_from_search_tool_result_with_hasher_hashes_ids_and_sets_true() -> None:
    """Hasher provided → each id transformed, doc_ids_hashed=True (S2)."""
    transformed: list[str] = []

    def _hasher(doc_id: str) -> str:
        result = f"hashed-{doc_id}"
        transformed.append(result)
        return result

    raw_ids = ["doc-a", "doc-b", "doc-c"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=3.0,
        doc_id_hasher=_hasher,
    )
    assert entry.doc_ids_hashed is True
    assert entry.result_doc_ids == ["hashed-doc-a", "hashed-doc-b", "hashed-doc-c"]
    assert entry.result_count == 3  # count still reflects number of ids


# ---------------------------------------------------------------------------
# unit: from_search_tool_result with hasher — empty list (S7)
# ---------------------------------------------------------------------------


def test_from_search_tool_result_empty_list_with_hasher() -> None:
    """Empty result_doc_ids + hasher → [], doc_ids_hashed=True (S7)."""
    hasher_called = []

    def _hasher(doc_id: str) -> str:
        hasher_called.append(doc_id)
        return f"h-{doc_id}"

    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search_with_context",
        collection="docs",
        result_doc_ids=[],
        latency_ms=2.0,
        doc_id_hasher=_hasher,
    )
    assert entry.result_doc_ids == []
    assert entry.doc_ids_hashed is True  # mode was active even with empty list
    assert entry.result_count == 0
    assert hasher_called == []  # hasher not called for empty list


# ---------------------------------------------------------------------------
# unit: entry with None result_doc_ids has doc_ids_hashed=False (S6)
# ---------------------------------------------------------------------------


def test_entry_with_none_result_doc_ids_has_hashed_false() -> None:
    """Model default: result_doc_ids=None → doc_ids_hashed=False (S6).

    from_search_tool_result always receives a list[str], never None.
    None arises only from the model default on non-search factories.
    """
    # Construct directly via model (not factory) to get None result_doc_ids
    entry = TelemetryEntry(
        query_id="deadbeef" * 4,
        timestamp="2026-06-25T00:00:00Z",
        endpoint="route",
        latency_ms=1.0,
        status="ok",
        result_doc_ids=None,  # model default
    )
    assert entry.result_doc_ids is None
    assert entry.doc_ids_hashed is False


# ---------------------------------------------------------------------------
# unit: doc_ids_hashed in DOCUMENTED_SCHEMA_FIELDS
# ---------------------------------------------------------------------------


def test_doc_ids_hashed_in_documented_schema_fields() -> None:
    """'doc_ids_hashed' must be in DOCUMENTED_SCHEMA_FIELDS."""
    assert "doc_ids_hashed" in DOCUMENTED_SCHEMA_FIELDS


def test_doc_ids_hashed_is_strict_bool_rejects_non_bool() -> None:
    """doc_ids_hashed uses StrictBool — int 1 must raise ValidationError, not coerce to True.

    Verifies the StrictBool type annotation is effective (not silently a plain bool).
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TelemetryEntry(
            query_id="deadbeef" * 4,
            timestamp="2026-06-25T00:00:00Z",
            endpoint="search",
            latency_ms=1.0,
            status="ok",
            doc_ids_hashed=1,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# unit: DOCUMENTED_SCHEMA_FIELDS matches model fields exactly (regression)
# ---------------------------------------------------------------------------


def test_documented_schema_fields_matches_model_fields_exactly() -> None:
    """DOCUMENTED_SCHEMA_FIELDS == set of TelemetryEntry model fields (keeps in sync)."""
    model_fields = set(TelemetryEntry.model_fields.keys())
    assert model_fields == DOCUMENTED_SCHEMA_FIELDS, (
        f"DOCUMENTED_SCHEMA_FIELDS is out of sync with TelemetryEntry.model_fields. "
        f"Missing from schema: {model_fields - DOCUMENTED_SCHEMA_FIELDS}. "
        f"Extra in schema: {DOCUMENTED_SCHEMA_FIELDS - model_fields}."
    )


# ---------------------------------------------------------------------------
# unit: other factories do not accept doc_id_hasher param (S16)
# ---------------------------------------------------------------------------


def test_other_factories_have_no_doc_id_hasher_param() -> None:
    """from_explain_result, from_error, from_route_response, from_search_multi_result
    signatures must not gain a doc_id_hasher param (S16)."""
    untouched_factories = [
        TelemetryEntry.from_explain_result,
        TelemetryEntry.from_error,
        TelemetryEntry.from_route_response,
        TelemetryEntry.from_search_multi_result,
    ]
    for factory in untouched_factories:
        params = inspect.signature(factory).parameters
        assert "doc_id_hasher" not in params, (
            f"{factory.__name__} must not accept 'doc_id_hasher' (S16)"
        )


def test_other_factories_still_accept_no_hasher_and_produce_false_flag() -> None:
    """All factories except from_search_tool_result produce doc_ids_hashed=False (S16)."""
    entries = [
        TelemetryEntry.from_explain_result(
            collection="docs", result_count=1, latency_ms=1.0
        ),
        TelemetryEntry.from_error(
            endpoint="search", status="internal_error", error_kind="other", latency_ms=1.0
        ),
        TelemetryEntry.from_route_response(
            collections=["docs"], decomposer_invoked=False, latency_ms=1.0
        ),
        TelemetryEntry.from_search_multi_result(
            collections=["a", "b"], fanout_count=2, result_count=3,
            latency_ms=2.0, excluded_count=0
        ),
    ]
    for entry in entries:
        assert entry.doc_ids_hashed is False, (
            f"{entry.endpoint} entry must have doc_ids_hashed=False"
        )


# ---------------------------------------------------------------------------
# unit: hasher callable type (Callable[[str], str] | None)
# ---------------------------------------------------------------------------


def test_from_search_tool_result_hasher_signature() -> None:
    """doc_id_hasher param is present, keyword-only, and defaults to None."""
    sig = inspect.signature(TelemetryEntry.from_search_tool_result)
    params = sig.parameters
    assert "doc_id_hasher" in params, "doc_id_hasher param must be added to from_search_tool_result"
    param = params["doc_id_hasher"]
    assert param.default is None, "doc_id_hasher must default to None"
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, "doc_id_hasher must be keyword-only"


# ---------------------------------------------------------------------------
# unit: hasher applied with real HMAC (integration-ish pure-function check)
# ---------------------------------------------------------------------------


def test_from_search_tool_result_with_real_hasher() -> None:
    """Wire hash_doc_id as the hasher; verify output differs from raw input and is 64-char hex."""
    import hmac

    salt = b"\xca\xfe\xba\xbe" * 8  # 32 bytes

    def hasher(doc_id: str) -> str:
        return hmac.digest(salt, doc_id.encode(), "sha256").hex()

    raw_ids = ["sha256-of-path-abc", "sha256-of-path-xyz"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=10.0,
        doc_id_hasher=hasher,
    )
    assert entry.doc_ids_hashed is True
    for i, hashed_id in enumerate(entry.result_doc_ids or []):
        assert len(hashed_id) == 64
        assert hashed_id != raw_ids[i], "Hashed id must differ from raw id"
        assert all(c in "0123456789abcdef" for c in hashed_id)


# ---------------------------------------------------------------------------
# integration: JSONL round-trip with doc_ids_hashed=True
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_telemetry_entry_jsonl_round_trip_with_doc_ids_hashed(
    tmp_path: Path,
) -> None:
    """Entry with doc_ids_hashed=True serialises and deserialises correctly via writer + reader."""
    from archon_search.telemetry.writer import TelemetryWriter
    import hmac

    log_dir = tmp_path / "search-logs"
    salt = b"\xab" * 32

    def hasher(doc_id: str) -> str:
        return hmac.digest(salt, doc_id.encode(), "sha256").hex()

    raw_ids = ["path-derived-doc-id-1", "path-derived-doc-id-2"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=42.0,
        doc_id_hasher=hasher,
    )
    assert entry.doc_ids_hashed is True

    async def _run() -> None:
        writer = TelemetryWriter(log_dir)
        await writer.start()
        writer.enqueue(entry)
        await writer.drain_and_stop()

    asyncio.run(_run())

    # Find the JSONL file and parse it
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, "Expected a JSONL file to be written"
    lines = [
        json.loads(line)
        for line in jsonl_files[0].read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    data = lines[0]

    # doc_ids_hashed must be serialised as True
    assert data.get("doc_ids_hashed") is True

    # result_doc_ids must be hashed, not raw
    assert "result_doc_ids" in data
    for raw_id, hashed_id in zip(raw_ids, data["result_doc_ids"]):
        assert hashed_id != raw_id, "Serialised id must be hashed, not raw"
        assert len(hashed_id) == 64

    # Round-trip: model_validate must restore doc_ids_hashed=True
    restored = TelemetryEntry.model_validate(data)
    assert restored.doc_ids_hashed is True
    assert restored.result_doc_ids == entry.result_doc_ids


# ---------------------------------------------------------------------------
# integration: JSONL round-trip with doc_ids_hashed=False (no hasher)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_telemetry_entry_jsonl_round_trip_without_hasher(
    tmp_path: Path,
) -> None:
    """Entry without hasher serialises doc_ids_hashed=False and round-trips correctly.

    This guards against a future serialization change (e.g. exclude_defaults=True)
    that would silently drop doc_ids_hashed=False from JSONL output.
    """
    from archon_search.telemetry.writer import TelemetryWriter

    log_dir = tmp_path / "search-logs"
    raw_ids = ["raw-doc-id-alpha", "raw-doc-id-beta"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=5.0,
    )
    assert entry.doc_ids_hashed is False

    async def _run() -> None:
        writer = TelemetryWriter(log_dir)
        await writer.start()
        writer.enqueue(entry)
        await writer.drain_and_stop()

    asyncio.run(_run())

    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, "Expected a JSONL file to be written"
    lines = [
        json.loads(line)
        for line in jsonl_files[0].read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    data = lines[0]

    # doc_ids_hashed must be present as False (not omitted by exclude_none/exclude_defaults)
    assert "doc_ids_hashed" in data, "doc_ids_hashed must be present even when False"
    assert data["doc_ids_hashed"] is False

    # result_doc_ids must be raw (not hashed)
    assert data["result_doc_ids"] == raw_ids

    # Round-trip: model_validate must restore correctly
    restored = TelemetryEntry.model_validate(data)
    assert restored.doc_ids_hashed is False
    assert restored.result_doc_ids == raw_ids


# ---------------------------------------------------------------------------
# unit: backward compatibility — pre-D8 JSONL without doc_ids_hashed field
# ---------------------------------------------------------------------------


def test_backward_compat_pre_d8_jsonl_missing_doc_ids_hashed() -> None:
    """Pre-D8 JSONL entries without doc_ids_hashed deserialise with default False.

    This verifies the backward-compatibility claim in the plan's Known Limitations:
    new code reading old JSONL (field absent) produces doc_ids_hashed=False.
    """
    pre_d8_data = {
        "query_id": "deadbeef" * 4,
        "timestamp": "2025-01-15T10:00:00Z",
        "endpoint": "search",
        "latency_ms": 12.5,
        "status": "ok",
        "collection": "docs",
        "result_count": 2,
        "result_doc_ids": ["old-hash-1", "old-hash-2"],
        "filter_flags": {
            "file_type": False,
            "source_path_prefix": False,
            "source_path_glob": False,
            "indexed_after": False,
            "indexed_before": False,
            "include_metadata": False,
            "language_filter_used": False,
        },
        # doc_ids_hashed intentionally absent — simulates pre-D8 entry
    }
    entry = TelemetryEntry.model_validate(pre_d8_data)
    assert entry.doc_ids_hashed is False, (
        "Pre-D8 JSONL without doc_ids_hashed must deserialise with default False"
    )
    assert entry.result_doc_ids == ["old-hash-1", "old-hash-2"]


# ---------------------------------------------------------------------------
# unit: result_count uses pre-hash input count (not unique hash count)
# ---------------------------------------------------------------------------


def test_result_count_tracks_input_length_not_unique_hash_count() -> None:
    """result_count reflects the number of input doc_ids, not unique hashed values.

    Uses a collapsing hasher (all inputs → same hash) to prove result_count
    tracks input cardinality even when hashed outputs are non-unique.
    """
    # Collapsing hasher: all ids map to the same constant hash
    def collapsing_hasher(_: str) -> str:
        return "a" * 64

    raw_ids = ["doc-1", "doc-2", "doc-3"]
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=raw_ids,
        latency_ms=1.0,
        doc_id_hasher=collapsing_hasher,
    )
    # result_count must be 3 (input cardinality), not 1 (unique hash count)
    assert entry.result_count == 3
    # All hashed ids are the same constant
    assert entry.result_doc_ids == ["a" * 64, "a" * 64, "a" * 64]
    assert entry.doc_ids_hashed is True

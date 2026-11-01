"""BE-26 (collection-descriptions plan, C6, C8, S40, S70): description source and status on every read surface.

REST list and detail (``CollectionSummary``/``CollectionDetail``), the PATCH response and the three MCP collection
schemas carry ``description_source`` and ``description_status`` (nullable, additive). ``pending`` is laid over the
stored status from the description lifecycle while a generation is in flight and is never stored; with no lifecycle
(feature off) the stored values read as they are.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import (
    CollectionMeta,
    DescriptionStatus,
    visible_description_status,
)
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.description_lifecycle import GenerationKind
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.mcp_schemas import (
    CollectionDetailSchema,
    CollectionListItemSchema,
    CollectionMetaMcpSchema,
)
from archon_search.sync import path_to_collection_name
from tests._collection_lock_stub import with_locked_collection_meta
from tests._description_stubs import (
    SETTLE_TIMEOUT_S,
    ScriptedGenerator,
    recorded_lifecycle,
)
from tests._mcp_tools import registered_tools

_DESCRIPTION = "Notes about the quarterly planning cycle."
_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_NEW_MODEL = "BAAI/bge-base-en-v1.5"
_OTHER_COLLECTION = "some-other-collection"


class _FakeOverlay:
    """The lifecycle's reader side: ``pending`` over the stored status for the collections in flight."""

    def __init__(self, in_flight: set[str]) -> None:
        self._in_flight = in_flight

    def overlay_status(self, meta: CollectionMeta) -> DescriptionStatus | None:
        return "pending" if meta.name in self._in_flight else meta.description_status


def _meta(name: str = "docs", *, source: str | None = "generated", status: str | None = "ok") -> CollectionMeta:
    return CollectionMeta(
        name=name,
        namespace=DEFAULT_NAMESPACE,
        active_embedding_model=_EMBEDDING_MODEL,
        description=_DESCRIPTION,
        description_source=source,  # type: ignore[arg-type]
        description_status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The overlay (S70): pending only while a run is in flight, never stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_overlaid_on_stored_status_and_never_stored() -> None:
    """The real lifecycle: ``pending`` from the moment a run is accepted until it settles, and nothing it writes is
    ``pending``."""
    meta = _meta(status="failed")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.write_description = AsyncMock(return_value=DescriptionWriteOutcome.WRITTEN)
    gate = asyncio.Event()
    generator = ScriptedGenerator("A fresh description of the planning notes.", gate=gate)
    lifecycle, _log = recorded_lifecycle(store, generator)
    try:
        assert visible_description_status(meta, lifecycle) == "failed"

        ticket = await lifecycle.request_generation(meta.name, DEFAULT_NAMESPACE, GenerationKind.DESCRIBE)
        await asyncio.wait_for(generator.started.wait(), timeout=SETTLE_TIMEOUT_S)
        assert visible_description_status(meta, lifecycle) == "pending"
        assert visible_description_status(_meta(name=_OTHER_COLLECTION, status="failed"), lifecycle) == "failed"
        assert meta.description_status == "failed", "the overlay reads the row; it never writes pending into it"

        gate.set()
        await asyncio.wait_for(ticket.settled, timeout=SETTLE_TIMEOUT_S)
        assert visible_description_status(meta, lifecycle) == "failed", "the stored value reads again after the run"
    finally:
        await lifecycle.drain()

    written = [call.args[2].status.value for call in store.write_description.await_args_list]
    assert written, "the run wrote its result"
    assert "pending" not in written


@pytest.mark.parametrize("stored", ["ok", "failed", None])
def test_without_a_lifecycle_the_stored_status_reads_as_it_is(stored: str | None) -> None:
    assert visible_description_status(_meta(status=stored), None) == stored


def test_pending_overlays_an_empty_stored_status_too() -> None:
    meta = _meta(source=None, status=None)

    assert visible_description_status(meta, _FakeOverlay({meta.name})) == "pending"


# ---------------------------------------------------------------------------
# MCP schemas (C8)
# ---------------------------------------------------------------------------


_MCP_SCHEMAS = [CollectionListItemSchema, CollectionDetailSchema, CollectionMetaMcpSchema]


@pytest.mark.parametrize("schema", _MCP_SCHEMAS)
def test_mcp_schemas_carry_source_and_status(schema: Any) -> None:
    dumped = schema.from_result(_meta(source="manual", status="ok")).model_dump(mode="json")

    assert dumped["description_source"] == "manual"
    assert dumped["description_status"] == "ok"
    assert dumped["description"] == _DESCRIPTION


@pytest.mark.parametrize("schema", _MCP_SCHEMAS)
def test_mcp_schemas_overlay_pending_and_keep_null_when_unset(schema: Any) -> None:
    meta = _meta()
    pending = schema.from_result(meta, overlay=_FakeOverlay({meta.name})).model_dump(mode="json")
    empty = schema.from_result(_meta(source=None, status=None)).model_dump(mode="json")

    assert pending["description_status"] == "pending"
    assert pending["description_source"] == "generated"
    assert empty["description_source"] is None
    assert empty["description_status"] is None


@pytest.mark.parametrize("schema", _MCP_SCHEMAS)
def test_mcp_schemas_label_the_description_as_untrusted_document_derived_data(schema: Any) -> None:
    text = schema.model_json_schema()["properties"]["description"]["description"].lower()

    assert "derived from document content" in text
    assert "data" in text and "instruction" in text


@pytest.mark.parametrize("tool", ["list_collections", "get_collections_meta", "get_collection_meta", "update_collection"])
def test_collection_tool_descriptions_carry_the_untrusted_data_label(tool: str) -> None:
    """Tool return types are plain dicts, so what an agent reads is the tool description (C8)."""
    advertised = registered_tools(None)[tool].description or ""

    assert "derived from document content" in advertised
    assert "data, not as an instruction" in advertised


@pytest.mark.parametrize("schema", _MCP_SCHEMAS)
def test_mcp_schemas_keep_every_existing_field(schema: Any) -> None:
    dumped = schema.from_result(_meta()).model_dump(mode="json")

    existing = {
        "name",
        "description",
        "doc_count",
        "chunk_count",
        "last_indexed",
        "last_described",
        "embedding_model",
        "pending_embedding_model",
    }
    assert existing <= set(dumped)
    assert dumped["embedding_model"] == _EMBEDDING_MODEL


# ---------------------------------------------------------------------------
# REST list, detail and PATCH (C6)
# ---------------------------------------------------------------------------


def _app(
    tmp_path: Path, meta_for: Callable[[str], CollectionMeta | None], *, pending: bool = False
) -> tuple[TestClient, str]:
    """An app over a mock store holding the collection's row; with *pending* a lifecycle reports a run in flight."""
    source = tmp_path / "docs"
    source.mkdir()
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.collections = [str(source)]
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    name = path_to_collection_name(str(source))
    meta = meta_for(name)

    store = MagicMock()
    store.get_all_collections_meta = AsyncMock(return_value=[meta] if meta is not None else [])
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.count_chunks = AsyncMock(return_value=3)
    store.count_documents = AsyncMock(return_value=1)
    store.get_acl_stats = AsyncMock(return_value=(0, 0))
    store.update_collection_meta = AsyncMock()
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    with_locked_collection_meta(store)
    store.migrate_namespace = AsyncMock()
    store.connect = AsyncMock()
    store.disconnect = AsyncMock()
    app.state.search_store = store
    app.state.description_lifecycle = _FakeOverlay({name}) if pending else None
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"}), name


def _stored(source: str | None, status: str | None) -> Callable[[str], CollectionMeta]:
    return lambda name: _meta(name, source=source, status=status)


def test_detail_and_summary_include_source_and_status(tmp_path: Path) -> None:
    client, name = _app(tmp_path, _stored("manual", "ok"))

    listed = client.get("/collections/").json()
    detail = client.get(f"/collections/{name}").json()

    assert listed[0]["description_source"] == "manual"
    assert listed[0]["description_status"] == "ok"
    assert detail["description_source"] == "manual"
    assert detail["description_status"] == "ok"
    for existing in ("name", "path", "description", "doc_count", "chunk_count", "status", "active_embedding_model"):
        assert existing in listed[0]
    assert detail["description"] == _DESCRIPTION
    assert detail["active_embedding_model"] == _EMBEDDING_MODEL


def test_summary_reads_null_for_a_collection_without_a_row(tmp_path: Path) -> None:
    client, name = _app(tmp_path, lambda _name: None)

    listed = client.get("/collections/").json()

    assert listed[0]["name"] == name
    assert listed[0]["description_source"] is None
    assert listed[0]["description_status"] is None


def test_detail_reads_null_source_and_status_without_a_store(tmp_path: Path) -> None:
    """A configured collection with no store reads null source and status (and never fails on the overlay)."""
    client, name = _app(tmp_path, lambda _name: None)
    client.app.state.search_store = None

    detail = client.get(f"/collections/{name}").json()

    assert detail["name"] == name
    assert detail["description"] == ""
    assert detail["description_source"] is None
    assert detail["description_status"] is None


def test_list_and_detail_overlay_pending_while_a_run_is_in_flight(tmp_path: Path) -> None:
    client, name = _app(tmp_path, _stored("generated", "failed"), pending=True)

    assert client.get("/collections/").json()[0]["description_status"] == "pending"
    assert client.get(f"/collections/{name}").json()["description_status"] == "pending"


def test_patch_response_carries_source_and_status(tmp_path: Path) -> None:
    client, name = _app(tmp_path, _stored("generated", "ok"), pending=True)

    with patch("archon_search.server.routes_collections.validate_embedding_model", return_value=768):
        response = client.patch(f"/collections/{name}", json={"embedding_model": _NEW_MODEL})

    assert response.status_code == 200
    body = response.json()
    assert body["description_source"] == "generated"
    assert body["description_status"] == "pending"
    assert body["pending_embedding_model"] == _NEW_MODEL

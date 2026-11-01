"""BE-26 (collection-descriptions plan, C8, S40, S70): the three MCP collection tools return source and status.

A real app with MCP mounted and ``[description]`` on (so the lifecycle exists and the MCP app received it). A document is
ingested through MCP, a manual description is written through the store, and ``list_collections``,
``get_collections_meta`` and ``get_collection_meta`` are called over JSON-RPC. The status reads ``pending`` once the app's
own lifecycle says a run is in flight (``is_pending`` is stubbed here; the real in-flight run is held open in
``tests/test_be26_description_read_exposure.py``) and the stored value again after it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.description_lifecycle import DescriptionLifecycle
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    FieldChange,
)
from tests._fake_model_server import unreachable_port
from tests.integration.conftest import make_real_app
from tests.integration.test_mcp_roundtrip_t2 import (
    _extract_tool_text,
    _mcp_call_tool,
    _mcp_initialize,
)

pytestmark = pytest.mark.integration

_COLLECTION = "be26-mcp-fields"
_SETTLE_SECONDS = 30.0
_POLL_SECONDS = 0.02
_MANUAL_TEXT = "Hand written note about the planning documents."
_GENERATED_TEXT = "A description the model wrote about the planning documents."
_DOCUMENT_TEXT = "Planning notes for the quarter, kept for the search tests.\n" * 5


def _description_toml(base_url: str) -> str:
    return (
        "[description]\nenabled = true\nprovider = \"ollama\"\nmodel = \"description-model\"\n"
        f'ollama_base_url = "{base_url}"\nollama_context_tokens = 8192\n'
    )


def _manual_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.set_to(_MANUAL_TEXT),
        source=FieldChange.set_to("manual"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(datetime.now(UTC)),
        described_at_doc_count=FieldChange.keep(),
        description_embedding=FieldChange.clear(),
    )


def _generated_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.set_to(_GENERATED_TEXT),
        source=FieldChange.set_to("generated"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(datetime.now(UTC)),
        described_at_doc_count=FieldChange.keep(),
        description_embedding=FieldChange.clear(),
    )


def _entry(payload: Any, name: str) -> dict[str, Any]:
    entries = payload if isinstance(payload, list) else [payload]
    return next(item for item in entries if item["name"] == name)


def test_mcp_collection_tools_return_source_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = tmp_path / "corpus" / "plan.txt"
    document.parent.mkdir(parents=True)
    document.write_text(_DOCUMENT_TEXT)

    with unreachable_port() as port:
        toml = _description_toml(f"http://127.0.0.1:{port}")
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, toml_content=toml) as (client, _config, api_key):
            assert isinstance(client.app.state.description_lifecycle, DescriptionLifecycle)
            session = _mcp_initialize(client, api_key)

            def call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
                return _extract_tool_text(_mcp_call_tool(client, api_key, session, tool, arguments or {}), tool)

            ingested = call("ingest_file", {"collection": _COLLECTION, "path": str(document)})
            assert ingested["status"] == "ok"
            # The ingest asked for a description (S17); the unreachable provider fails it. Let that run end, or its
            # `pending` overlay would hide the stored status this test is about.
            lifecycle_at_ingest: DescriptionLifecycle = client.app.state.description_lifecycle
            deadline = time.monotonic() + _SETTLE_SECONDS
            while lifecycle_at_ingest.is_pending(_COLLECTION):
                assert time.monotonic() < deadline, "the ingest-triggered generation did not settle"
                time.sleep(_POLL_SECONDS)
            store = client.app.state.search_store
            outcome = client.portal.call(  # type: ignore[union-attr]
                lambda: store.write_description(_COLLECTION, DEFAULT_NAMESPACE, _manual_fields(), UNCONDITIONAL)
            )
            assert outcome == "written"

            def read_all() -> list[Any]:
                return [
                    _entry(call("list_collections"), _COLLECTION),
                    _entry(call("get_collections_meta"), _COLLECTION),
                    call("get_collection_meta", {"name": _COLLECTION}),
                ]

            for item in read_all():
                assert item["description"] == _MANUAL_TEXT
                assert item["description_source"] == "manual"
                assert item["description_status"] == "ok"
                assert item["embedding_model"]

            lifecycle: DescriptionLifecycle = client.app.state.description_lifecycle
            with monkeypatch.context() as patched:
                patched.setattr(lifecycle, "is_pending", lambda collection: collection == _COLLECTION)
                for item in read_all():
                    # A manual description is never pending: a generation in flight over it is refused at its write (BE-31).
                    assert item["description_status"] == "ok"
                    assert item["description_source"] == "manual"

            outcome = client.portal.call(  # type: ignore[union-attr]
                lambda: store.write_description(_COLLECTION, DEFAULT_NAMESPACE, _generated_fields(), UNCONDITIONAL)
            )
            assert outcome == "written"
            for item in read_all():
                assert item["description_source"] == "generated"
                assert item["description_status"] == "ok"
            with monkeypatch.context() as patched:
                patched.setattr(lifecycle, "is_pending", lambda collection: collection == _COLLECTION)
                for item in read_all():
                    assert item["description_status"] == "pending"
                    assert item["description_source"] == "generated"

            for item in read_all():
                assert item["description_status"] == "ok"

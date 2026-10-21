"""REST routes never echo an internal exception message in a wire-facing ``detail``.

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` for a
caught exception whose message embeds internals. Each test raises an exception
carrying an internal-looking path at the real raise point (the HyDE generator's
embedder, networkx inside ``to_graphml``) and asserts the path never reaches the
HTTP response, with a presence anchor (status code / exact body / the raise point
was hit) so the absence check cannot pass vacuously.

The missing-extra tests pin the fixed ``*_DEPENDENCY_DETAIL`` texts: the exceptions'
own messages embed a receipt-derived install command (possibly index credentials),
so it is logged and never sent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import networkx
import pytest
from fastapi.testclient import TestClient

from archon_search import model_validation
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.graph_inspector import GRAPHML_DEPENDENCY_DETAIL, CollectionGraphView, CrossCollectionGraphView
from archon_search.hyde import HYDE_DEPENDENCY_DETAIL
from archon_search.jobs.store import JobStore
from archon_search.key_manager import (
    ROTATE_KEYS_JSON_FAILED_DETAIL,
    ROTATE_WRITE_FAILED_DETAIL,
)
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
    ExplainPipelineResult,
    ExplainStageError,
    SearchPipelineResult,
)
from archon_search.rag_fusion import RAG_FUSION_DEPENDENCY_DETAIL
from archon_search.server.app import create_app
from archon_search.server.routes_explain import (
    EXPLAIN_FAILED_DETAIL,
    ROUTING_UNAVAILABLE_CODE,
    ROUTING_UNAVAILABLE_DETAIL,
)
from archon_search.server.routes_search import SEARCH_FAILED_DETAIL
from tests._install_hint_leak import (
    assert_install_hint_logged_not_sent,
    patch_install_hint,
    rag_fusion_dependency_raiser,
)
from tests._internal_error_leak import (
    LANCE_MSG,
    LEAK_NEEDLE,
    ONNX_MSG,
    assert_logged_with_traceback,
    hyde_generator,
    hyde_generator_without_provider,
    metadata_lookup_error,
    mock_pipeline,
    permission_error,
)

_NETWORKX_IMPORT_MSG = (
    "cannot import name 'lxml_etree' from 'networkx.readwrite.graphml' "
    f"({LEAK_NEEDLE}/.venv/lib/python3.13/site-packages/networkx/readwrite/graphml.py)"
)
_HTTP_UNPROCESSABLE = 422
_HTTP_INTERNAL_ERROR = 500
# Starlette's own plain-text body for an exception no route handler caught.
_STARLETTE_UNHANDLED_BODY = "Internal Server Error"


def _make_app(tmp_path: Path) -> tuple[Any, TestClient]:
    """App + authenticated client; the chunker's tokenizer download is skipped."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _pipeline() -> MagicMock:
    pipeline = mock_pipeline()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    )
    return pipeline


def _wire_app(tmp_path: Path) -> tuple[Any, TestClient]:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _pipeline()
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    app.state.embedder_cache = embedder_cache
    return app, client


# ---------------------------------------------------------------------------
# C — a HyDE failure other than HyDEDependencyError -> 500 without the exception text
# ---------------------------------------------------------------------------


def test_search_hyde_runtime_error_answers_json_500_with_telemetry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``/search`` answers like ``/explain``: JSON ``SEARCH_FAILED_DETAIL`` and an error telemetry entry."""
    app, client = _wire_app(tmp_path)
    app.state.config.hyde.enabled = True
    app.state.hyde_generator, embed_one = hyde_generator(app.state.config.hyde, RuntimeError(ONNX_MSG))
    writer = MagicMock()
    app.state.telemetry_writer = writer

    with caplog.at_level(logging.ERROR):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    embed_one.assert_awaited()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": SEARCH_FAILED_DETAIL}
    writer.enqueue.assert_called_once()
    assert writer.enqueue.call_args.args[0].endpoint == "search"
    assert writer.enqueue.call_args.args[0].status == "internal_error"
    assert writer.enqueue.call_args.args[0].error_kind == "other"
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, ONNX_MSG)


def test_explain_hyde_runtime_error_answers_json_500_with_telemetry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``/explain`` mirrors its own catch-all: JSON ``EXPLAIN_FAILED_DETAIL`` and an error telemetry entry."""
    app, client = _wire_app(tmp_path)
    app.state.config.hyde.enabled = True
    app.state.hyde_generator, embed_one = hyde_generator(app.state.config.hyde, RuntimeError(ONNX_MSG))
    writer = MagicMock()
    app.state.telemetry_writer = writer

    with caplog.at_level(logging.ERROR):
        response = client.post("/explain", json={"collection": "col", "query": "q", "hyde": True})

    embed_one.assert_awaited()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": EXPLAIN_FAILED_DETAIL}
    writer.enqueue.assert_called_once()
    assert writer.enqueue.call_args.args[0].endpoint == "explain"
    assert writer.enqueue.call_args.args[0].status == "internal_error"
    assert writer.enqueue.call_args.args[0].error_kind == "other"
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, ONNX_MSG)


_EXPLAIN_TARGETS = [{"collections": ["a", "b"]}, {"collection": "col"}]
_EXPLAIN_TARGET_IDS = ["explain-multi", "explain"]


def _assert_one_explain_internal_error_entry(writer: MagicMock) -> None:
    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args.args[0]
    assert (entry.endpoint, entry.status, entry.error_kind) == ("explain", "internal_error", "other")


def _raised(exc: Exception) -> Exception:
    """*exc* with a traceback, as the pipeline hands it to ``ExplainStageError``."""
    try:
        raise exc
    except Exception as caught:  # noqa: BLE001
        return caught


@pytest.mark.parametrize("target", _EXPLAIN_TARGETS, ids=_EXPLAIN_TARGET_IDS)
def test_explain_pipeline_failure_answers_fixed_detail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: dict[str, Any]
) -> None:
    """``/explain``'s non-stage catch-all answers ``EXPLAIN_FAILED_DETAIL``, never the store error."""
    app, client = _wire_app(tmp_path)
    raiser = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    app.state.pipeline.explain = raiser
    writer = MagicMock()
    app.state.telemetry_writer = writer

    with caplog.at_level(logging.ERROR):
        response = client.post("/explain", json={**target, "query": "q"})

    raiser.assert_awaited_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": EXPLAIN_FAILED_DETAIL}
    _assert_one_explain_internal_error_entry(writer)
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, LANCE_MSG)


@pytest.mark.parametrize("target", _EXPLAIN_TARGETS, ids=_EXPLAIN_TARGET_IDS)
def test_explain_stage_failure_answers_stage_and_type_only_with_telemetry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: dict[str, Any]
) -> None:
    """An ``ExplainStageError`` answers ``<stage> error: <type>`` — never the original message."""
    app, client = _wire_app(tmp_path)
    raiser = AsyncMock(side_effect=ExplainStageError("store", _raised(RuntimeError(LANCE_MSG))))
    app.state.pipeline.explain = raiser
    writer = MagicMock()
    app.state.telemetry_writer = writer

    with caplog.at_level(logging.WARNING):
        response = client.post("/explain", json={**target, "query": "q"})

    raiser.assert_awaited_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": "store error: RuntimeError"}
    _assert_one_explain_internal_error_entry(writer)
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, LANCE_MSG)


# ``/explain``'s 503 bodies for a failed collection lookup / routing call (routes_explain.py).
_METADATA_STORE_UNAVAILABLE = {"detail": METADATA_STORE_UNAVAILABLE_DETAIL, "code": METADATA_STORE_UNAVAILABLE_CODE}
_ROUTING_UNAVAILABLE = {"detail": ROUTING_UNAVAILABLE_DETAIL, "code": ROUTING_UNAVAILABLE_CODE}


def _fail_meta_lookup(pipeline: MagicMock) -> AsyncMock:
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    return pipeline.get_collection_meta


def _fail_all_meta_lookup(pipeline: MagicMock) -> AsyncMock:
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    return pipeline.get_all_collections_meta


def _fail_routing_embed(pipeline: MagicMock) -> AsyncMock:
    pipeline.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="col", namespace="default", centroid=[1.0, 0.0])]
    )
    pipeline.global_embedder.embed_one = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    return pipeline.global_embedder.embed_one


@pytest.mark.parametrize(
    ("target", "break_pipeline", "expected"),
    [
        pytest.param({"collection": "col"}, _fail_meta_lookup, _METADATA_STORE_UNAVAILABLE, id="meta-lookup"),
        pytest.param({}, _fail_all_meta_lookup, _METADATA_STORE_UNAVAILABLE, id="all-meta-lookup"),
        pytest.param({}, _fail_routing_embed, _ROUTING_UNAVAILABLE, id="routing"),
    ],
)
def test_explain_lookup_or_routing_failure_answers_503_and_logs_traceback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    target: dict[str, Any],
    break_pipeline: Callable[[MagicMock], AsyncMock],
    expected: dict[str, str],
) -> None:
    """The 503 handlers log only the exception type in the message, so the traceback is the
    only place the cause survives — it must be attached (``exc_info``)."""
    app, client = _wire_app(tmp_path)
    raiser = break_pipeline(app.state.pipeline)

    with caplog.at_level(logging.ERROR):
        response = client.post("/explain", json={**target, "query": "q"})

    raiser.assert_awaited_once()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json() == expected
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, LANCE_MSG)


@pytest.mark.parametrize(("route", "pipeline_method"), [("/search", "search_many"), ("/explain", "explain")])
def test_multi_collection_metadata_lookup_error_answers_503_and_logs_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, route: str, pipeline_method: str
) -> None:
    """The fan-out's ``MetadataLookupError`` answers the fixed 503 body; its message embeds the
    store error, so the handler must log it with its traceback rather than drop the cause."""
    app, client = _wire_app(tmp_path)
    raiser = AsyncMock(side_effect=metadata_lookup_error())
    setattr(app.state.pipeline, pipeline_method, raiser)

    with caplog.at_level(logging.ERROR):
        response = client.post(route, json={"collections": ["a", "b"], "query": "q"})

    raiser.assert_awaited_once()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json() == _METADATA_STORE_UNAVAILABLE
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, LANCE_MSG)


def test_explain_failure_outside_its_handlers_is_a_plain_text_500_without_telemetry(tmp_path: Path) -> None:
    """``/explain`` loads the collection's embedder outside its try blocks: a failure there is
    Starlette's plain-text 500 and records no telemetry entry (140_error_handling_strategy.md)."""
    app, client = _wire_app(tmp_path)
    get_or_load = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    app.state.embedder_cache.get_or_load = get_or_load
    writer = MagicMock()
    app.state.telemetry_writer = writer

    response = client.post("/explain", json={"collection": "col", "query": "q"})

    get_or_load.assert_awaited_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.text == _STARLETTE_UNHANDLED_BODY
    writer.enqueue.assert_not_called()


def test_add_collection_model_load_failure_answers_422_and_logs_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``POST /collections`` validates ``embedding_model`` first; a backend load failure answers
    the fixed ``_MODEL_LOAD_FAILED_DETAIL`` and keeps the cause in the log with its traceback."""
    _, client = _make_app(tmp_path)
    embedder = MagicMock()
    embedder.embed = AsyncMock(side_effect=RuntimeError(ONNX_MSG))

    # The suite's fastembed stub has no ``list_supported_models``, so the registry fast path
    # falls through to instantiating the model, as it does for a name outside the registry.
    with (
        patch.object(model_validation, "make_embedder", return_value=embedder),
        caplog.at_level(logging.WARNING),
    ):
        response = client.post(
            "/collections/", json={"path": str(tmp_path), "embedding_model": "unknown/model"}
        )

    embedder.embed.assert_awaited_once()
    assert response.status_code == _HTTP_UNPROCESSABLE, response.text
    assert response.json() == {"detail": model_validation._MODEL_LOAD_FAILED_DETAIL}
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, ONNX_MSG)


def test_search_hyde_runtime_error_with_failing_telemetry_still_answers_json_500(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A telemetry enqueue failure inside the HyDE failure branch never replaces the sanitized answer."""
    app, client = _wire_app(tmp_path)
    app.state.config.hyde.enabled = True
    app.state.hyde_generator, embed_one = hyde_generator(app.state.config.hyde, RuntimeError(ONNX_MSG))
    writer = MagicMock()
    writer.enqueue.side_effect = RuntimeError(f"telemetry queue at {LEAK_NEEDLE}/telemetry.jsonl is full")
    app.state.telemetry_writer = writer

    with caplog.at_level(logging.WARNING):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    embed_one.assert_awaited()
    writer.enqueue.assert_called_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": SEARCH_FAILED_DETAIL}
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert "telemetry enqueue failed: RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# D — GraphML ``except ImportError`` -> 500 detail=str(e)
# ---------------------------------------------------------------------------


def _graph_app(tmp_path: Path) -> tuple[Any, TestClient]:
    app, client = _wire_app(tmp_path)
    app.state.config.graph.enabled = True
    app.state.graph_store = MagicMock()
    return app, client


def _empty_view() -> CollectionGraphView:
    return CollectionGraphView(nodes=[], edges=[], node_count=0, edge_count=0, truncated=False)


def _empty_cross_view() -> CrossCollectionGraphView:
    return CrossCollectionGraphView(
        collections=["a", "b"], nodes=[], edges=[], node_count=0, edge_count=0, truncated=False
    )


_GRAPHML_ROUTES: list[tuple[str, str, Any]] = [
    ("/graph/col?format=graphml", "inspect_collection", _empty_view),
    ("/graph/cross-collection?collections=a,b&format=graphml", "inspect_cross_collection", _empty_cross_view),
]


@pytest.mark.parametrize(("url", "inspect_name", "view_factory"), _GRAPHML_ROUTES, ids=["single", "cross"])
def test_graphml_import_error_does_not_echo_module_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, inspect_name: str, view_factory: Any
) -> None:
    _, client = _graph_app(tmp_path)
    monkeypatch.setattr(f"archon_search.server.routes_graph.{inspect_name}", AsyncMock(return_value=view_factory()))
    digraph = MagicMock(side_effect=ImportError(_NETWORKX_IMPORT_MSG))
    monkeypatch.setattr(networkx, "DiGraph", digraph)

    response = client.get(url)

    digraph.assert_called_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.text == _STARLETTE_UNHANDLED_BODY
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"


# ---------------------------------------------------------------------------
# E — POST /keys/rotate: an OSError echoes the key-file path
# ---------------------------------------------------------------------------


def _rotate_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, TestClient]:
    """App whose default key is managed by ``.search.env`` (rotation answers 409 under the env var)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    app, _ = _make_app(tmp_path)
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {app.state.api_key}"})
    return app, client


_KEY_DIR_BLOCKER = "a regular file where the key file's directory belongs"


def _regular_file_as_key_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the key file under a regular file, so creating its directory raises a real OSError."""
    key_dir = tmp_path / "not-a-dir"
    key_dir.write_text(_KEY_DIR_BLOCKER)
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_dir / ".search.env"))
    return key_dir


def test_rotate_key_file_dir_creation_failure_does_not_echo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, client = _rotate_app(tmp_path, monkeypatch)
    initial_key = app.state.api_key
    key_dir = _regular_file_as_key_dir(tmp_path, monkeypatch)
    rotate = AsyncMock()
    monkeypatch.setattr(app.state.key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        response = client.post("/keys/rotate", json={})

    rotate.assert_not_awaited()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": ROTATE_WRITE_FAILED_DETAIL}
    assert str(tmp_path) not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, str(key_dir))
    assert key_dir.read_text() == _KEY_DIR_BLOCKER, "the blocking regular file must be left untouched"
    assert app.state.api_key == initial_key


def test_rotate_bad_key_file_env_answers_the_fixed_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable ARCHON_SEARCH_KEY_FILE is a key-file write failure, not a plain-text 500."""
    app, client = _rotate_app(tmp_path, monkeypatch)
    initial_key = app.state.api_key
    bad_key_file = f"relative{LEAK_NEEDLE}/.search.env"
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", bad_key_file)

    with caplog.at_level(logging.ERROR):
        response = client.post("/keys/rotate", json={})

    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": ROTATE_WRITE_FAILED_DETAIL}
    assert_logged_with_traceback(caplog, bad_key_file)
    assert app.state.api_key == initial_key
    assert asyncio.run(app.state.key_store.active_keys()) == [], "nothing was rotated"


def test_rotate_keys_json_failure_answers_fixed_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, client = _rotate_app(tmp_path, monkeypatch)
    initial_key = app.state.api_key
    rotate = AsyncMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(app.state.key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        response = client.post("/keys/rotate", json={})

    rotate.assert_awaited_once()
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": ROTATE_KEYS_JSON_FAILED_DETAIL}
    assert LEAK_NEEDLE not in response.text, f"internal path leaked into the response: {response.text!r}"
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)
    assert app.state.api_key == initial_key


def test_rotate_invalid_input_is_rejected_before_writing_the_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A blank server-held default key is a server fault: 500 with the curated text, before .search.env is written."""
    app, _ = _rotate_app(tmp_path, monkeypatch)
    managed = asyncio.run(app.state.key_store.create(ns="default", label=None, expires_at=None))
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {managed['token']}"})
    app.state.api_key = "   "  # a blank current default key
    write = MagicMock()
    monkeypatch.setattr("archon_search.key_manager.atomic_write_bytes", write)
    rotate = AsyncMock()
    monkeypatch.setattr(app.state.key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        response = client.post("/keys/rotate", json={})

    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": "current_token must not be empty"}
    write.assert_not_called()
    rotate.assert_not_awaited()
    assert app.state.api_key == "   "
    assert "rotate: rejected before writing .search.env: current_token must not be empty" in caplog.text


def test_rotate_keys_json_failure_after_the_key_file_write_is_an_incomplete_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the pre-write validation, .search.env already holds the new key: never claim nothing changed."""
    app, client = _rotate_app(tmp_path, monkeypatch)
    initial_key = app.state.api_key
    rotate = AsyncMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(app.state.key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        response = client.post("/keys/rotate", json={})

    rotate.assert_awaited_once()
    new_token = rotate.call_args.kwargs["new_token"]
    assert new_token in (tmp_path / ".search.env").read_text(), "precondition: the new key was written first"
    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": ROTATE_KEYS_JSON_FAILED_DETAIL}
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)
    assert app.state.api_key == initial_key


# ---------------------------------------------------------------------------
# F — missing optional extra: fixed detail on the wire, install command in the log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/search", "/explain"])
def test_hyde_missing_extra_answers_fixed_detail_and_logs_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, route: str
) -> None:
    patch_install_hint(monkeypatch, "archon_search.hyde")
    app, client = _wire_app(tmp_path)
    app.state.config.hyde.enabled = True
    app.state.hyde_generator = hyde_generator_without_provider(app.state.config.hyde)

    with caplog.at_level(logging.WARNING):
        response = client.post(route, json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == _HTTP_UNPROCESSABLE, response.text
    assert response.json() == {"detail": HYDE_DEPENDENCY_DETAIL}
    assert_install_hint_logged_not_sent(caplog, response.text)


# (route, request body extras, pipeline method the route awaits)
_RAG_FUSION_ROUTES: list[tuple[str, dict[str, Any], str]] = [
    ("/search", {"collection": "col"}, "search"),
    ("/search", {"collections": ["a", "b"]}, "search_many"),
    ("/explain", {"collection": "col"}, "explain"),
    ("/explain", {"collections": ["a", "b"]}, "explain"),
]


@pytest.mark.parametrize(
    ("route", "body", "method"), _RAG_FUSION_ROUTES,
    ids=["search", "search-multi", "explain", "explain-multi"],
)
def test_rag_fusion_missing_extra_answers_fixed_detail_and_logs_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    route: str, body: dict[str, Any], method: str,
) -> None:
    patch_install_hint(monkeypatch, "archon_search.rag_fusion")
    app, client = _wire_app(tmp_path)
    app.state.config.rag_fusion.enabled = True
    raiser = rag_fusion_dependency_raiser()
    setattr(app.state.pipeline, method, raiser)

    with caplog.at_level(logging.WARNING):
        response = client.post(route, json={**body, "query": "q", "rag_fusion": True})

    raiser.assert_awaited_once()
    assert response.status_code == _HTTP_UNPROCESSABLE, response.text
    assert response.json() == {"detail": RAG_FUSION_DEPENDENCY_DETAIL}
    assert_install_hint_logged_not_sent(caplog, response.text)


@pytest.mark.parametrize(("url", "inspect_name", "view_factory"), _GRAPHML_ROUTES, ids=["single", "cross"])
def test_graphml_missing_networkx_answers_fixed_detail_and_logs_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    url: str, inspect_name: str, view_factory: Any,
) -> None:
    patch_install_hint(monkeypatch, "archon_search.graph_inspector")
    _, client = _graph_app(tmp_path)
    monkeypatch.setattr(f"archon_search.server.routes_graph.{inspect_name}", AsyncMock(return_value=view_factory()))
    monkeypatch.setitem(sys.modules, "networkx", None)  # `import networkx` raises ImportError

    with caplog.at_level(logging.WARNING):
        response = client.get(url)

    assert response.status_code == _HTTP_INTERNAL_ERROR, response.text
    assert response.json() == {"detail": GRAPHML_DEPENDENCY_DETAIL}
    assert_install_hint_logged_not_sent(caplog, response.text)

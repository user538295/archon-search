"""MCP tools never echo an internal exception message on the wire.

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``error`` field
for a caught exception whose message embeds internals. Each test raises an
exception carrying an internal-looking path at the real raise point the handler
wraps (pipeline / store / embedder / response-schema construction) and asserts
the path never reaches the MCP wire output, while a presence anchor (error code,
and that the injected raise point was actually hit) keeps the absence check from
passing vacuously.

The missing-extra tests pin the fixed ``*_DEPENDENCY_DETAIL`` texts (the exceptions'
own messages embed a receipt-derived install command, so it is logged, never sent);
the KEEP test pins the deliberately user-facing store-busy message.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Callable, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import fastmcp.server.server
import pytest
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from archon_search import key_manager
from archon_search._types import SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.hyde import HYDE_DEPENDENCY_DETAIL
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION
from archon_search.key_manager import (
    ENV_VAR,
    ROTATE_KEYS_JSON_FAILED_DETAIL,
    ROTATE_WRITE_FAILED_DETAIL,
    KeyStore,
)
from archon_search.pipeline import (
    ExplainPipelineResult,
    ExplainStageError,
    SearchPipelineResult,
    SearchWithContextResult,
)
from archon_search.rag_fusion import RAG_FUSION_DEPENDENCY_DETAIL
from archon_search.server.mcp import (
    _EXPLAIN_FAILED_DETAIL,
    _INVALID_EXPLAIN_RESPONSE_DETAIL,
    _INVALID_SEARCH_FILTERS_DETAIL,
    _INVALID_SEARCH_RESPONSE_DETAIL,
    _SEARCH_FAILED_DETAIL,
    CREATE_KEY_FAILED_DETAIL,
    INTERNAL_ERROR_DETAIL,
    SCHEMA_VALIDATION_DETAIL,
)
from archon_search.server.mcp_schemas import (
    CollectionDetailSchema,
    CollectionListItemSchema,
    CollectionMetaMcpSchema,
    DocumentInfoSchema,
    IngestResultSchema,
    McpSearchResultSchema,
)
from archon_search.server.routes_explain import ExplainResponse
from archon_search.store import StoreBusyError
from tests._install_hint_leak import (
    assert_install_hint_logged_not_sent,
    patch_install_hint,
    rag_fusion_dependency_raiser,
)
from tests._internal_error_leak import (
    LANCE_MSG,
    LANCE_PATH,
    LEAK_NEEDLE,
    ONNX_MSG,
    assert_logged_with_traceback,
    hyde_generator,
    hyde_generator_without_provider,
    mock_pipeline,
    permission_error,
)

pytestmark = pytest.mark.xdist_group("mcp")

_SCHEMA_ERROR_CODE = "schema_validation_error"
_INTERNAL_ERROR_CODE = "internal_error"
_STORE_BUSY_TIMEOUT_S = 30.0
_EMBEDDING_DIM = 384


class _ResponseProbe(BaseModel):
    """Stand-in response model: validating an internal source path as ``int`` fails."""

    source_path: int


def _schema_validation_error() -> ValidationError:
    """A real pydantic ValidationError whose ``input_value`` carries the internal path."""
    try:
        _ResponseProbe.model_validate({"source_path": f"{LANCE_PATH}/doc.md"})
    except ValidationError as exc:
        assert LEAK_NEEDLE in str(exc), "precondition: pydantic must echo input_value"
        return exc
    raise AssertionError("precondition: _ResponseProbe must reject a string source_path")


# ---------------------------------------------------------------------------
# FastMCP stub — collects the tool closures so they can be awaited directly.
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, name: str) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func
        return decorator

    def custom_route(self, path: str, methods: list[str] | None = None) -> Any:
        def decorator(func: Any) -> Any:
            return func
        return decorator


class _FakeFastMCP:
    def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
        return _FakeApp(name)


def _mcp_module() -> Any:
    """The live ``archon_search.server.mcp`` (sibling modules pop and reload it)."""
    return importlib.import_module("archon_search.server.mcp")


def _tools(pipeline: MagicMock, **kwargs: Any) -> dict[str, Any]:
    module = _mcp_module()
    kwargs.setdefault("writer", None)
    with patch.object(module, "FastMCP", _FakeFastMCP):
        app = module.create_app(pipeline, "default", **kwargs)
    return app.tools


async def _wire(tools: dict[str, Any], tool_name: str, /, **kwargs: Any) -> tuple[Any, str]:
    """Call a tool and return ``(result, wire_text)`` as FastMCP would serialise it.

    An exception escaping the tool is rendered the way an unmasked FastMCP does
    (``mask_error_details=False``, ``fastmcp/server/server.py``): ``"Error calling tool
    {name!r}: {e}"``. ``create_app`` turns masking on, but these tests do not rely on
    it — a re-raise still counts as a leak, not an escape hatch.
    """
    try:
        result = await tools[tool_name](**kwargs)
    except Exception as exc:  # noqa: BLE001 — emulating FastMCP's own catch-all
        return exc, f"Error calling tool {tool_name!r}: {exc}"
    return result, json.dumps(result, default=str)


def _graph_config() -> SearchConfig:
    config = SearchConfig()
    config.graph.enabled = True
    return config


def _search_result() -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="chunk text",
        score=0.9,
        source_path=f"{LANCE_PATH}/doc.md",
    )


def _assert_error_without_leak(result: Any, wire: str, code: str, detail: str) -> None:
    assert isinstance(result, dict), f"tool must return an error dict, got {wire!r}"
    assert result.get("code") == code, wire
    assert result.get("error") == detail, wire
    assert LEAK_NEEDLE not in wire, f"internal path leaked onto the MCP wire: {wire!r}"


# The tools that record an ``internal_error`` telemetry entry on failure; the others have no telemetry.
_TELEMETRY_TOOLS = frozenset({"search", "search_with_context", "explain"})


def _assert_internal_error_enqueued(writer: MagicMock, tool: str) -> None:
    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args.args[0]
    assert (entry.endpoint, entry.status, entry.error_kind) == (tool, _INTERNAL_ERROR_CODE, "other")


# ---------------------------------------------------------------------------
# A — catch-all ``except Exception`` -> code internal_error
# ---------------------------------------------------------------------------

class _CatchAllCase(NamedTuple):
    id: str
    tool: str
    kwargs: dict[str, Any]
    raiser_attr: str  # pipeline attribute that raises; a ``store.`` prefix means ``pipeline.store``
    needs_graph: bool
    detail: str


_CATCH_ALL_CASES: list[_CatchAllCase] = [
    # search / search_with_context / explain answer their own fixed text, not INTERNAL_ERROR_DETAIL.
    _CatchAllCase("search-multi", "search", {"query": "q", "collections": ["a", "b"]}, "search_many", False,
                  _SEARCH_FAILED_DETAIL),
    _CatchAllCase("search", "search", {"query": "q", "collection": "col"}, "search", False, _SEARCH_FAILED_DETAIL),
    _CatchAllCase("search_with_context", "search_with_context", {"query": "q", "collection": "col"},
                  "search_with_context", False, _SEARCH_FAILED_DETAIL),
    _CatchAllCase("explain-multi", "explain", {"query": "q", "collections": ["a", "b"]}, "explain", False,
                  _EXPLAIN_FAILED_DETAIL),
    _CatchAllCase("explain", "explain", {"query": "q", "collection": "col"}, "explain", False, _EXPLAIN_FAILED_DETAIL),
    _CatchAllCase("ingest_file", "ingest_file", {"path": "/data/doc.md", "collection": "col"}, "ingest_file", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("ingest_directory", "ingest_directory", {"path": "/data", "collection": "col"}, "ingest_directory",
                  False, INTERNAL_ERROR_DETAIL),
    _CatchAllCase("list_collections", "list_collections", {}, "get_all_collections_meta", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("get_collections_meta", "get_collections_meta", {}, "get_all_collections_meta", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("get_collection_meta", "get_collection_meta", {"name": "col"}, "get_collection_meta", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("list_documents", "list_documents", {"collection": "col"}, "list_documents", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("delete_document", "delete_document", {"doc_id": "d", "collection": "col"}, "delete_document",
                  False, INTERNAL_ERROR_DETAIL),
    _CatchAllCase("update_collection", "update_collection",
                  {"collection_name": "col", "embedding_model": "m", "ctx": None}, "store.get_collection_meta", False,
                  INTERNAL_ERROR_DETAIL),
    _CatchAllCase("get_graph", "get_graph", {"collection": "col"}, "get_collection_meta", True, INTERNAL_ERROR_DETAIL),
    _CatchAllCase("get_graph_cross_collection", "get_graph_cross_collection", {"collections": ["a", "b"]},
                  "get_collection_meta", True, INTERNAL_ERROR_DETAIL),
    _CatchAllCase("graph_impact", "graph_impact", {"collection": "col", "symbol": "f"}, "get_collection_meta", True,
                  INTERNAL_ERROR_DETAIL),
]


@pytest.mark.parametrize("case", _CATCH_ALL_CASES, ids=[c.id for c in _CATCH_ALL_CASES])
async def test_catch_all_internal_error_does_not_echo_exception(
    case: _CatchAllCase, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = mock_pipeline()
    raiser = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    owner = pipeline.store if case.raiser_attr.startswith("store.") else pipeline
    setattr(owner, case.raiser_attr.removeprefix("store."), raiser)
    extra: dict[str, Any] = {"config": _graph_config(), "graph_store": MagicMock()} if case.needs_graph else {}
    writer = MagicMock()

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(pipeline, writer=writer, **extra), case.tool, **case.kwargs)

    raiser.assert_awaited()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, case.detail)
    assert_logged_with_traceback(caplog, LANCE_MSG)
    if case.tool in _TELEMETRY_TOOLS:
        _assert_internal_error_enqueued(writer, case.tool)
    else:
        # A tool that starts recording telemetry must join _TELEMETRY_TOOLS (and its assertions).
        writer.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# A2 — explain's ExplainStageError -> "<stage> error: <ExceptionType>", code internal_error
# ---------------------------------------------------------------------------

_EXPLAIN_TARGETS: list[tuple[str, dict[str, Any]]] = [
    ("explain-multi", {"collections": ["a", "b"]}),
    ("explain", {"collection": "col"}),
]


def _store_stage_error() -> ExplainStageError:
    """Wrapped the way the pipeline does, so ``original`` carries a real traceback."""
    try:
        raise RuntimeError(LANCE_MSG)
    except RuntimeError as exc:
        return ExplainStageError("store", exc)


@pytest.mark.parametrize("target", [k for _, k in _EXPLAIN_TARGETS], ids=[i for i, _ in _EXPLAIN_TARGETS])
async def test_explain_stage_error_answers_stage_and_type_only(
    target: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = mock_pipeline()
    raiser = AsyncMock(side_effect=_store_stage_error())
    pipeline.explain = raiser
    writer = MagicMock()

    with caplog.at_level(logging.WARNING):
        result, wire = await _wire(_tools(pipeline, writer=writer), "explain", query="q", **target)

    raiser.assert_awaited_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, "store error: RuntimeError")
    assert_logged_with_traceback(caplog, LANCE_MSG)
    _assert_internal_error_enqueued(writer, "explain")


# ---------------------------------------------------------------------------
# B — server-built response-schema ValidationError -> code schema_validation_error
# ---------------------------------------------------------------------------


def _patch_from_result(cls: type) -> Callable[[], Any]:
    return lambda: patch.object(cls, "from_result", MagicMock(side_effect=_schema_validation_error()))


def _setup_search(pipeline: MagicMock) -> None:
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[_search_result()], acl_filtered=False))


def _setup_search_many(pipeline: MagicMock) -> None:
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(results=[_search_result()], acl_filtered=False)
    )


def _setup_search_with_context(pipeline: MagicMock) -> None:
    pipeline.search_with_context = AsyncMock(
        return_value=SearchWithContextResult(
            results=[{"result": _search_result(), "context_before": [], "context_after": []}],
            pipeline_result=SearchPipelineResult(results=[], acl_filtered=False),
        )
    )


def _setup_explain(pipeline: MagicMock) -> None:
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    )


def _setup_collections(pipeline: MagicMock) -> None:
    pipeline.get_all_collections_meta = AsyncMock(return_value=[CollectionMeta(name="col", namespace="default")])


def _setup_documents(pipeline: MagicMock) -> None:
    pipeline.list_documents = AsyncMock(return_value=([MagicMock()], None, 1))


def _setup_delete(pipeline: MagicMock) -> None:
    pipeline.delete_document = AsyncMock(return_value=1)


def _setup_ingest_file(pipeline: MagicMock) -> None:
    pipeline.ingest_file = AsyncMock(return_value=MagicMock())


def _setup_ingest_directory(pipeline: MagicMock) -> None:
    pipeline.ingest_directory = AsyncMock(return_value=[MagicMock()])


def _setup_update_collection(pipeline: MagicMock) -> None:
    pipeline.store.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default", active_embedding_model="m")
    )
    pipeline.store.get_stored_vector_dimension = AsyncMock(return_value=None)


class _SchemaCase(NamedTuple):
    id: str
    tool: str
    kwargs: dict[str, Any]
    setup: Callable[[MagicMock], None]
    patcher: Callable[[], Any]  # patches the response-schema construction to raise
    detail: str = SCHEMA_VALIDATION_DETAIL


def _patch_explain_response() -> Any:
    return patch.object(ExplainResponse, "from_pipeline_result", MagicMock(side_effect=_schema_validation_error()))


_SCHEMA_CASES: list[_SchemaCase] = [
    _SchemaCase("search-multi", "search", {"query": "q", "collections": ["a", "b"]}, _setup_search_many,
                _patch_from_result(McpSearchResultSchema), _INVALID_SEARCH_RESPONSE_DETAIL),
    _SchemaCase("search", "search", {"query": "q", "collection": "col"}, _setup_search,
                _patch_from_result(McpSearchResultSchema), _INVALID_SEARCH_RESPONSE_DETAIL),
    _SchemaCase("search_with_context", "search_with_context", {"query": "q", "collection": "col"},
                _setup_search_with_context, _patch_from_result(McpSearchResultSchema),
                _INVALID_SEARCH_RESPONSE_DETAIL),
    _SchemaCase("explain-multi", "explain", {"query": "q", "collections": ["a", "b"]}, _setup_explain,
                _patch_explain_response, _INVALID_EXPLAIN_RESPONSE_DETAIL),
    _SchemaCase("explain", "explain", {"query": "q", "collection": "col"}, _setup_explain, _patch_explain_response,
                _INVALID_EXPLAIN_RESPONSE_DETAIL),
    _SchemaCase("ingest_file", "ingest_file", {"path": "/data/doc.md", "collection": "col"}, _setup_ingest_file,
                _patch_from_result(IngestResultSchema)),
    _SchemaCase("ingest_directory", "ingest_directory", {"path": "/data", "collection": "col"},
                _setup_ingest_directory, _patch_from_result(IngestResultSchema)),
    _SchemaCase("list_collections", "list_collections", {}, _setup_collections,
                _patch_from_result(CollectionListItemSchema)),
    _SchemaCase("get_collections_meta", "get_collections_meta", {}, _setup_collections,
                _patch_from_result(CollectionMetaMcpSchema)),
    _SchemaCase("get_collection_meta", "get_collection_meta", {"name": "col"}, lambda p: None,
                _patch_from_result(CollectionDetailSchema)),
    _SchemaCase("list_documents", "list_documents", {"collection": "col"}, _setup_documents,
                _patch_from_result(DocumentInfoSchema)),
    _SchemaCase("delete_document", "delete_document", {"doc_id": "d", "collection": "col"}, _setup_delete,
                lambda: patch.object(_mcp_module(), "DeleteDocumentSchema",
                                     MagicMock(side_effect=_schema_validation_error()))),
    _SchemaCase("update_collection", "update_collection",
                {"collection_name": "col", "embedding_model": "m", "ctx": None}, _setup_update_collection,
                _patch_from_result(CollectionDetailSchema)),
]


@pytest.mark.parametrize("case", _SCHEMA_CASES, ids=[c.id for c in _SCHEMA_CASES])
async def test_schema_validation_error_does_not_echo_input_value(
    case: _SchemaCase, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = mock_pipeline()
    case.setup(pipeline)
    module = _mcp_module()
    with patch.object(module, "validate_embedding_model", AsyncMock(return_value=_EMBEDDING_DIM)), \
            case.patcher() as hook, caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(pipeline), case.tool, **case.kwargs)

    assert hook.called, "the response-schema construction must have been reached"
    _assert_error_without_leak(result, wire, _SCHEMA_ERROR_CODE, case.detail)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


# ---------------------------------------------------------------------------
# B2 — a filter argument the SearchFilters model rejects -> code validation_error
# ---------------------------------------------------------------------------

# The rejected value is caller input; the ValidationError text would echo it back verbatim.
_BAD_FILTER_VALUE = "not-a-date-" + LEAK_NEEDLE

_FILTER_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("search-multi", "search", {"query": "q", "collections": ["a", "b"]}),
    ("search", "search", {"query": "q", "collection": "col"}),
    ("search_with_context", "search_with_context", {"query": "q", "collection": "col"}),
]


@pytest.mark.parametrize(("tool", "kwargs"), [(t, k) for _, t, k in _FILTER_CASES], ids=[i for i, _, _ in _FILTER_CASES])
async def test_filter_validation_error_does_not_echo_input_value(tool: str, kwargs: dict[str, Any]) -> None:
    pipeline = mock_pipeline()
    _setup_search(pipeline)
    _setup_search_many(pipeline)
    _setup_search_with_context(pipeline)

    result, wire = await _wire(_tools(pipeline), tool, indexed_after=_BAD_FILTER_VALUE, **kwargs)

    _assert_error_without_leak(result, wire, "validation_error", _INVALID_SEARCH_FILTERS_DETAIL)
    pipeline.search.assert_not_awaited()
    pipeline.search_many.assert_not_awaited()
    pipeline.search_with_context.assert_not_awaited()


# ---------------------------------------------------------------------------
# C — a HyDE failure other than HyDEDependencyError -> code internal_error
# ---------------------------------------------------------------------------


def _hyde_config() -> SearchConfig:
    config = SearchConfig()
    config.hyde.enabled = True
    return config


def _hyde_pipeline() -> MagicMock:
    pipeline = mock_pipeline()
    _setup_search(pipeline)
    _setup_search_with_context(pipeline)
    _setup_explain(pipeline)
    return pipeline


# Each tool answers like its REST counterpart's HyDE catch-all (/search, /explain).
_HYDE_TOOLS: list[tuple[str, str]] = [
    ("search", _SEARCH_FAILED_DETAIL),
    ("search_with_context", _SEARCH_FAILED_DETAIL),
    ("explain", _EXPLAIN_FAILED_DETAIL),
]


@pytest.mark.parametrize(("tool", "detail"), _HYDE_TOOLS, ids=[t for t, _ in _HYDE_TOOLS])
async def test_hyde_runtime_error_does_not_echo_embedder_failure(
    tool: str, detail: str, caplog: pytest.LogCaptureFixture
) -> None:
    config = _hyde_config()
    generator, embed_one = hyde_generator(config.hyde, RuntimeError(ONNX_MSG))
    writer = MagicMock()

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(
            _tools(_hyde_pipeline(), config=config, hyde_generator=generator, writer=writer),
            tool, query="q", collection="col", hyde=True,
        )

    embed_one.assert_awaited()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, detail)
    assert_logged_with_traceback(caplog, ONNX_MSG)
    _assert_internal_error_enqueued(writer, tool)


@pytest.mark.parametrize(("tool", "detail"), _HYDE_TOOLS, ids=[t for t, _ in _HYDE_TOOLS])
async def test_hyde_runtime_error_with_failing_telemetry_still_answers_sanitized_error(
    tool: str, detail: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A telemetry enqueue failure inside the HyDE failure branch never replaces the coded error."""
    config = _hyde_config()
    generator, embed_one = hyde_generator(config.hyde, RuntimeError(ONNX_MSG))
    writer = MagicMock()
    writer.enqueue.side_effect = RuntimeError(f"telemetry queue at {LEAK_NEEDLE}/telemetry.jsonl is full")

    with caplog.at_level(logging.WARNING):
        result, wire = await _wire(
            _tools(_hyde_pipeline(), config=config, hyde_generator=generator, writer=writer),
            tool, query="q", collection="col", hyde=True,
        )

    embed_one.assert_awaited()
    writer.enqueue.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, detail)
    assert f"telemetry: {tool} error entry enqueue failed" in caplog.text


# ---------------------------------------------------------------------------
# E — key-management tools: an OSError echoes the key-file path
# ---------------------------------------------------------------------------


def _key_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KeyStore:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)
    return KeyStore(path=tmp_path / "keys.json")


async def test_rotate_key_write_failure_does_not_echo_key_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    write = MagicMock(side_effect=permission_error(".search.env"))
    monkeypatch.setattr(key_manager, "atomic_write_bytes", write)
    tools = _tools(mock_pipeline(), key_store=key_store)
    # The real loader would auto-generate the current key through the patched writer too.
    read = MagicMock(return_value=("aa" * 32, "file"))

    with patch.object(_mcp_module(), "load_or_generate_key", read), caplog.at_level(logging.ERROR):
        result, wire = await _wire(tools, "rotate_key", grace_seconds=None)

    write.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, ROTATE_WRITE_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


async def test_rotate_key_current_key_read_failure_does_not_echo_key_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    read = MagicMock(side_effect=permission_error(".search.env"))
    tools = _tools(mock_pipeline(), key_store=key_store)

    with patch.object(_mcp_module(), "load_or_generate_key", read), caplog.at_level(logging.ERROR):
        result, wire = await _wire(tools, "rotate_key", grace_seconds=None)

    read.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, INTERNAL_ERROR_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)
    assert "rotate_key failed" in caplog.text


async def test_rotate_key_unexpected_failure_after_the_key_read_logs_a_neutral_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The catch-all covers every rotation step, so its log line must not blame the key read (C5-B-2)."""
    key_store = _key_store(tmp_path, monkeypatch)
    verify = AsyncMock(side_effect=RuntimeError(f"unexpected {LEAK_NEEDLE}"))
    monkeypatch.setattr(key_store, "verify_readable", verify)
    read = MagicMock(return_value=("aa" * 32, "file"))
    tools = _tools(mock_pipeline(), key_store=key_store)

    with patch.object(_mcp_module(), "load_or_generate_key", read), caplog.at_level(logging.ERROR):
        result, wire = await _wire(tools, "rotate_key", grace_seconds=None)

    read.assert_called_once()
    verify.assert_awaited_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, INTERNAL_ERROR_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)
    assert "rotate_key failed" in caplog.text
    assert "could not read the current default key" not in caplog.text


async def test_rotate_key_keys_json_failure_does_not_echo_key_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    rotate = AsyncMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(mock_pipeline(), key_store=key_store), "rotate_key", grace_seconds=None)

    rotate.assert_awaited_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, ROTATE_KEYS_JSON_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, "Permission denied")


async def test_rotate_key_keys_json_validation_error_does_not_echo_input_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pydantic ValidationError is a ValueError, but only KeyRotationInputError text is curated."""
    key_store = _key_store(tmp_path, monkeypatch)
    rotate = AsyncMock(side_effect=_schema_validation_error())
    monkeypatch.setattr(key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(mock_pipeline(), key_store=key_store), "rotate_key", grace_seconds=None)

    rotate.assert_awaited_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, ROTATE_KEYS_JSON_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


async def test_rotate_key_invalid_input_is_rejected_before_writing_the_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A blank server-held default key is a server fault: internal_error, before .search.env is written."""
    key_store = _key_store(tmp_path, monkeypatch)
    write = MagicMock()
    monkeypatch.setattr(key_manager, "atomic_write_bytes", write)
    rotate = AsyncMock()
    monkeypatch.setattr(key_store, "record_rotation", rotate)
    read = MagicMock(return_value=("   ", "file"))  # a blank current default key
    tools = _tools(mock_pipeline(), key_store=key_store)

    with patch.object(_mcp_module(), "load_or_generate_key", read), caplog.at_level(logging.ERROR):
        result, _ = await _wire(tools, "rotate_key", grace_seconds=None)

    read.assert_called_once()
    assert result == {"error": "current_token must not be empty", "code": _INTERNAL_ERROR_CODE}
    write.assert_not_called()
    rotate.assert_not_awaited()
    assert "rotate: rejected before writing .search.env: current_token must not be empty" in caplog.text


async def test_rotate_key_out_of_range_grace_is_rejected_before_the_current_key_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_or_generate_key may create .search.env: an invalid grace_seconds must fail before it runs."""
    key_store = _key_store(tmp_path, monkeypatch)
    write = MagicMock()
    monkeypatch.setattr(key_manager, "atomic_write_bytes", write)
    read = MagicMock(return_value=("aa" * 32, "file"))
    tools = _tools(mock_pipeline(), key_store=key_store)

    with patch.object(_mcp_module(), "load_or_generate_key", read):
        result, _ = await _wire(tools, "rotate_key", grace_seconds=-1)

    assert result == {"error": "grace_seconds must be >= 0, got -1", "code": "validation_error"}
    read.assert_not_called()
    write.assert_not_called()


async def test_rotate_key_bad_key_file_env_answers_the_fixed_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    bad_key_file = f"relative{LEAK_NEEDLE}/.search.env"
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", bad_key_file)
    tools = _tools(mock_pipeline(), key_store=key_store)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(tools, "rotate_key", grace_seconds=None)

    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, ROTATE_WRITE_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, bad_key_file)
    assert await key_store.active_keys() == [], "nothing was rotated"


async def test_rotate_key_keys_json_failure_after_the_key_file_write_is_an_incomplete_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the pre-write validation, .search.env already holds the new key: never claim nothing changed."""
    key_store = _key_store(tmp_path, monkeypatch)
    rotate = AsyncMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(key_store, "record_rotation", rotate)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(mock_pipeline(), key_store=key_store), "rotate_key", grace_seconds=None)

    rotate.assert_awaited_once()
    new_token = rotate.call_args.kwargs["new_token"]
    assert new_token in (tmp_path / ".search.env").read_text(), "precondition: the new key was written first"
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, ROTATE_KEYS_JSON_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


_KEY_DIR_BLOCKER = "a regular file where the key file's directory belongs"


async def test_rotate_key_file_dir_creation_failure_does_not_echo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    key_dir = tmp_path / "not-a-dir"
    key_dir.write_text(_KEY_DIR_BLOCKER)
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_dir / ".search.env"))
    rotate = AsyncMock()
    monkeypatch.setattr(key_store, "record_rotation", rotate)
    tools = _tools(mock_pipeline(), key_store=key_store)
    # The real loader would read the patched key file too; the current key is not under test.
    read = MagicMock(return_value=("aa" * 32, "file"))

    with patch.object(_mcp_module(), "load_or_generate_key", read), caplog.at_level(logging.ERROR):
        result, wire = await _wire(tools, "rotate_key", grace_seconds=None)

    read.assert_called_once()
    rotate.assert_not_awaited()
    assert result == {"error": ROTATE_WRITE_FAILED_DETAIL, "code": _INTERNAL_ERROR_CODE}
    assert str(tmp_path) not in wire, f"internal path leaked into the response: {wire!r}"
    assert_logged_with_traceback(caplog, str(key_dir))
    assert key_dir.read_text() == _KEY_DIR_BLOCKER, "the blocking regular file must be left untouched"


async def test_revoke_key_write_failure_does_not_echo_key_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    created = await key_store.create(ns="default", label=None, expires_at=None)
    write = MagicMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(key_manager, "atomic_write_bytes", write)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(mock_pipeline(), key_store=key_store), "revoke_key", key_id=created["id"])

    write.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, INTERNAL_ERROR_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


async def test_create_key_write_failure_does_not_echo_key_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key_store = _key_store(tmp_path, monkeypatch)
    write = MagicMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(key_manager, "atomic_write_bytes", write)

    with caplog.at_level(logging.ERROR):
        result, wire = await _wire(_tools(mock_pipeline(), key_store=key_store), "create_key", namespace="default")

    write.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, CREATE_KEY_FAILED_DETAIL)
    assert_logged_with_traceback(caplog, "Permission denied")


# ---------------------------------------------------------------------------
# H — export_collection / import_collection: collection lookup and job creation
# ---------------------------------------------------------------------------


def _import_archive(tmp_path: Path, embedding_model: str) -> Path:
    """A minimal archive that passes every import_collection check before the collection lookup."""
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "collection": "col",
        "exported_at": "2024-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": embedding_model,
    }
    archive_path = tmp_path / "import.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, payload in (("manifest.json", json.dumps(manifest).encode()), ("documents.jsonl", b"")):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return archive_path


class _BulkCase(NamedTuple):
    tool: str
    create_method: str  # the JobStore method that enqueues the job
    existing_meta: Any  # get_collection_meta result that lets the tool reach job creation


_BULK_CASES: list[_BulkCase] = [
    _BulkCase("export_collection", "create_export", CollectionMeta(name="col", namespace="default")),
    _BulkCase("import_collection", "create_import", None),
]


def _bulk_kwargs(tool: str, tmp_path: Path, config: SearchConfig) -> dict[str, Any]:
    if tool == "export_collection":
        return {"collection": "col"}
    return {"collection": "col", "path": str(_import_archive(tmp_path, config.embedding_model))}


async def _call_bulk_tool(case: _BulkCase, tmp_path: Path, pipeline: MagicMock, job_store: MagicMock) -> tuple[Any, str]:
    config = SearchConfig()
    tools = _tools(pipeline, config=config, job_store=job_store)
    with patch.object(_mcp_module(), "get_data_dir", return_value=tmp_path):
        return await _wire(tools, case.tool, **_bulk_kwargs(case.tool, tmp_path, config))


@pytest.mark.parametrize("case", _BULK_CASES, ids=[c.tool for c in _BULK_CASES])
async def test_bulk_job_collection_lookup_failure_does_not_echo_store_error(
    case: _BulkCase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = mock_pipeline()
    lookup = AsyncMock(side_effect=RuntimeError(LANCE_MSG))
    pipeline.store.get_collection_meta = lookup

    with caplog.at_level(logging.ERROR):
        result, wire = await _call_bulk_tool(case, tmp_path, pipeline, MagicMock())

    lookup.assert_awaited_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, INTERNAL_ERROR_DETAIL)
    assert_logged_with_traceback(caplog, LANCE_MSG)


@pytest.mark.parametrize("case", _BULK_CASES, ids=[c.tool for c in _BULK_CASES])
async def test_bulk_job_creation_failure_does_not_echo_job_store_error(
    case: _BulkCase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = mock_pipeline()
    pipeline.store.get_collection_meta = AsyncMock(return_value=case.existing_meta)
    job_store = MagicMock()
    create = MagicMock(side_effect=permission_error("jobs.json"))
    setattr(job_store, case.create_method, create)

    with caplog.at_level(logging.ERROR):
        result, wire = await _call_bulk_tool(case, tmp_path, pipeline, job_store)

    create.assert_called_once()
    _assert_error_without_leak(result, wire, _INTERNAL_ERROR_CODE, INTERNAL_ERROR_DETAIL)
    assert_logged_with_traceback(caplog, LEAK_NEEDLE)


# ---------------------------------------------------------------------------
# I — an exception escaping every tool handler is masked by FastMCP itself
# ---------------------------------------------------------------------------


async def test_fastmcp_masks_an_exception_escaping_a_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``list_keys`` has no catch-all, so its exception reaches FastMCP's own handler."""
    key_store = _key_store(tmp_path, monkeypatch)
    list_keys = AsyncMock(side_effect=permission_error("keys.json"))
    monkeypatch.setattr(key_store, "list_keys", list_keys)
    module = _mcp_module()
    # Sibling modules can leave a stub FastMCP on the module; build with the real server class.
    with patch.object(module, "FastMCP", fastmcp.server.server.FastMCP):
        app = module.create_app(mock_pipeline(), "default", writer=None, key_store=key_store)

    with pytest.raises(ToolError) as excinfo:
        await app.call_tool("list_keys", {})

    list_keys.assert_awaited_once()
    assert str(excinfo.value) == "Error calling tool 'list_keys'"
    assert LEAK_NEEDLE not in str(excinfo.value), f"internal path leaked onto the MCP wire: {excinfo.value!s}"


def test_create_app_turns_fastmcp_error_masking_on() -> None:
    captured: dict[str, Any] = {}

    class _CapturingFastMCP:
        def __new__(cls, name: str, **kwargs: Any) -> _FakeApp:  # type: ignore[misc]
            captured.update(kwargs)
            return _FakeApp(name)

    module = _mcp_module()
    with patch.object(module, "FastMCP", _CapturingFastMCP):
        module.create_app(mock_pipeline(), "default", writer=None)

    assert captured.get("mask_error_details") is True, captured


# ---------------------------------------------------------------------------
# KEEP — deliberately user-facing messages still pass through unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", [t for t, _ in _HYDE_TOOLS])
async def test_hyde_missing_extra_answers_fixed_detail_and_logs_install_command(
    tool: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    patch_install_hint(monkeypatch, "archon_search.hyde")
    config = _hyde_config()
    generator = hyde_generator_without_provider(config.hyde)

    with caplog.at_level(logging.WARNING):
        result, wire = await _wire(
            _tools(_hyde_pipeline(), config=config, hyde_generator=generator),
            tool, query="q", collection="col", hyde=True,
        )

    assert result == {"error": HYDE_DEPENDENCY_DETAIL, "code": "validation_error"}, wire
    assert_install_hint_logged_not_sent(caplog, wire)


# (tool, tool kwargs, pipeline method the tool awaits)
_RAG_FUSION_TOOLS: list[tuple[str, dict[str, Any], str]] = [
    ("search", {"collection": "col"}, "search"),
    ("search", {"collections": ["a", "b"]}, "search_many"),
    ("search_with_context", {"collection": "col"}, "search_with_context"),
    ("explain", {"collection": "col"}, "explain"),
    ("explain", {"collections": ["a", "b"]}, "explain"),
]


@pytest.mark.parametrize(
    ("tool", "kwargs", "method"), _RAG_FUSION_TOOLS,
    ids=["search", "search-multi", "search_with_context", "explain", "explain-multi"],
)
async def test_rag_fusion_missing_extra_answers_fixed_detail_and_logs_install_command(
    tool: str, kwargs: dict[str, Any], method: str, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_install_hint(monkeypatch, "archon_search.rag_fusion")
    config = SearchConfig()
    config.rag_fusion.enabled = True
    pipeline = mock_pipeline()
    raiser = rag_fusion_dependency_raiser()
    setattr(pipeline, method, raiser)

    with caplog.at_level(logging.WARNING):
        result, wire = await _wire(_tools(pipeline, config=config), tool, query="q", rag_fusion=True, **kwargs)

    raiser.assert_awaited_once()
    assert result == {"error": RAG_FUSION_DEPENDENCY_DETAIL, "code": "validation_error"}, wire
    assert_install_hint_logged_not_sent(caplog, wire)


async def test_keep_store_busy_message_reaches_store_busy() -> None:
    pipeline = mock_pipeline()
    pipeline.ingest_file = AsyncMock(side_effect=StoreBusyError(_STORE_BUSY_TIMEOUT_S))

    result, wire = await _wire(_tools(pipeline), "ingest_file", path="/data/doc.md", collection="col")

    assert isinstance(result, dict) and result.get("code") == "store_busy", wire
    assert result["error"] == str(StoreBusyError(_STORE_BUSY_TIMEOUT_S))

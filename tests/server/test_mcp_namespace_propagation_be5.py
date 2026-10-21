"""D9 / BE-5 — Asymmetry fix #2: thread authenticated namespace into all tool closures.

Tests that every tool closure that makes pipeline calls uses the namespace resolved
by APIKeyMiddleware (via _get_request_namespace()) instead of hardcoding DEFAULT_NAMESPACE.

Mechanism (ADR 17, which partially supersedes ADR 09's K-1 sketch):
  fastmcp.server.dependencies.get_http_request() returns the current Starlette Request.
  APIKeyMiddleware writes request.state.namespace before call_next, so any tool that
  calls _get_request_namespace() → get_http_request().state.namespace sees the
  authenticated namespace. Outside an HTTP request get_http_request() raises
  RuntimeError and _get_request_namespace() falls back to DEFAULT_NAMESPACE.

Scenarios completed: S8 (namespace propagation correct for all tool closures), C4.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# ---------------------------------------------------------------------------
# Local FastMCP stand-in: patched over archon_search.server.mcp.FastMCP per test
# so create_app() registers its tool closures in _FakeApp.tools.
# ---------------------------------------------------------------------------

class _FakeApp:
    """Minimal FastMCP-like registry used in unit tests to capture tool functions."""

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


# ---------------------------------------------------------------------------
# Helpers: a fake Request for get_http_request(), and a patch of
# _get_request_namespace() for tests that only check tool-closure wiring.
# ---------------------------------------------------------------------------

def _make_fake_request(namespace: str) -> MagicMock:
    """Return a mock Starlette Request whose .state.namespace is set."""
    req = MagicMock()
    req.state = MagicMock()
    req.state.namespace = namespace
    return req


def _patch_namespace(ns: str):
    """Context manager that patches _get_request_namespace() in mcp module to return ns.

    ContextVar.get is a read-only slot — cannot be patched with patch.object.
    Instead we patch the module-level helper function _get_request_namespace() directly,
    which is the single call site tested by all tool closures.
    """
    return patch("archon_search.server.mcp._get_request_namespace", return_value=ns)


# ---------------------------------------------------------------------------
# Minimal pipeline mocks shared by tests
# ---------------------------------------------------------------------------

def _make_search_result() -> Any:
    from archon_search._types import SearchResult
    from archon_search.pipeline import SearchPipelineResult

    sr = SearchResult(
        doc_id="d" * 64,
        chunk_id="d" * 64 + "-000001",
        text="x",
        score=0.5,
        source_path="/tmp/f.md",
        file_type="md",
        language="",
        metadata={},
        ingested_by="cli",
    )
    return SearchPipelineResult(results=[sr], acl_filtered=False)


# ---------------------------------------------------------------------------
# test_search_tool_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_uses_resolved_namespace() -> None:
    """search tool calls pipeline.search with namespace from _get_request_namespace()."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    pipeline.search = AsyncMock(return_value=_make_search_result())
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline._global_embedder = MagicMock()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["search"]

    with _patch_namespace("ns-a"):
        await fn(query="hello", collection="col1")

    pipeline.search.assert_called_once()
    call_kwargs = pipeline.search.call_args
    assert call_kwargs.args[2] == "ns-a" or call_kwargs.kwargs.get("namespace") == "ns-a", (
        f"pipeline.search must be called with namespace='ns-a', got args={call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_search_with_context_tool_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_context_tool_uses_resolved_namespace() -> None:
    """search_with_context tool calls pipeline.search_with_context with resolved namespace."""
    import archon_search.server.mcp as mcp_module
    from archon_search.pipeline import SearchWithContextResult

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=MagicMock())
    swc_result = SearchWithContextResult(results=[], pipeline_result=_make_search_result())
    pipeline.search_with_context = AsyncMock(return_value=swc_result)
    pipeline._global_embedder = MagicMock()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["search_with_context"]

    with _patch_namespace("ns-b"):
        await fn(query="hello", collection="col1")

    pipeline.search_with_context.assert_called_once()
    call_kwargs = pipeline.search_with_context.call_args
    # search_with_context(query, collection, context_window, namespace=...)
    ns_passed = call_kwargs.kwargs.get("namespace") or (
        call_kwargs.args[3] if len(call_kwargs.args) > 3 else None
    )
    assert ns_passed == "ns-b", (
        f"pipeline.search_with_context must use namespace='ns-b', got {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_list_documents_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_uses_resolved_namespace() -> None:
    """list_documents tool calls pipeline.list_documents with resolved namespace."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.list_documents = AsyncMock(return_value=([], None, 0))

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["list_documents"]

    with _patch_namespace("ns-a"):
        await fn(collection="col1")

    pipeline.list_documents.assert_called_once()
    call_args = pipeline.list_documents.call_args
    ns_passed = call_args.kwargs.get("namespace") or (
        call_args.args[2] if len(call_args.args) > 2 else None
    )
    assert ns_passed == "ns-a", (
        f"pipeline.list_documents must be called with namespace='ns-a', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_explain_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_uses_resolved_namespace() -> None:
    """explain tool calls pipeline.get_all_collections_meta / explain with resolved namespace."""
    import archon_search.server.mcp as mcp_module
    from archon_search.pipeline import ExplainPipelineResult

    pipeline = MagicMock()
    # Provide get_all_collections_meta so routing code runs and explain is called
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[], near_misses=[], acl_filtered=False,
            rag_fusion_applied=False,
            rag_fusion_queries_used=0,
            rag_fusion_attempted=False,
            rag_fusion_failure_reason=None,
            rag_fusion_sub_query_results=None,
        )
    )
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.0])

    from archon_search.config import SearchConfig
    cfg = SearchConfig()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None, config=cfg)
        fn = app.tools["explain"]

    with _patch_namespace("ns-explain"):
        await fn(query="hello", collection=None)

    # When routing runs, get_all_collections_meta must use the resolved namespace.
    pipeline.get_all_collections_meta.assert_called_once()
    call_args = pipeline.get_all_collections_meta.call_args
    ns_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("namespace")
    assert ns_passed == "ns-explain", (
        f"pipeline.get_all_collections_meta must be called with namespace='ns-explain', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_list_collections_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_collections_uses_resolved_namespace() -> None:
    """list_collections tool calls pipeline.get_all_collections_meta with resolved namespace."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["list_collections"]

    with _patch_namespace("ns-list"):
        await fn()

    pipeline.get_all_collections_meta.assert_called_once()
    call_args = pipeline.get_all_collections_meta.call_args
    ns_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("namespace")
    assert ns_passed == "ns-list", (
        f"pipeline.get_all_collections_meta must be called with namespace='ns-list', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_get_collections_meta_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_collections_meta_uses_resolved_namespace() -> None:
    """get_collections_meta tool calls pipeline.get_all_collections_meta with resolved namespace."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["get_collections_meta"]

    with _patch_namespace("ns-gcm"):
        await fn()

    pipeline.get_all_collections_meta.assert_called_once()
    call_args = pipeline.get_all_collections_meta.call_args
    ns_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("namespace")
    assert ns_passed == "ns-gcm", (
        f"pipeline.get_all_collections_meta must be called with namespace='ns-gcm', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_get_collection_meta_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_collection_meta_uses_resolved_namespace() -> None:
    """get_collection_meta tool calls pipeline.get_collection_meta with resolved namespace."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)  # not found → McpErrorResponse

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["get_collection_meta"]

    with _patch_namespace("ns-single"):
        await fn(name="mycol")

    pipeline.get_collection_meta.assert_called_once()
    call_args = pipeline.get_collection_meta.call_args
    ns_passed = call_args.kwargs.get("namespace") or (
        call_args.args[1] if len(call_args.args) > 1 else None
    )
    assert ns_passed == "ns-single", (
        f"pipeline.get_collection_meta must be called with namespace='ns-single', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_ingest_file_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_file_uses_resolved_namespace(tmp_path) -> None:
    """ingest_file tool calls pipeline.ingest_file with resolved namespace."""
    import archon_search.server.mcp as mcp_module
    from archon_search.pipeline import IngestResult

    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(return_value=IngestResult(doc_id="d" * 64, chunks_created=1, status="ok"))
    pipeline._global_embedder = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)

    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello world")

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["ingest_file"]

    with _patch_namespace("ns-ingest"):
        await fn(path=str(test_file), collection="col1")

    pipeline.ingest_file.assert_called_once()
    call_kwargs = pipeline.ingest_file.call_args
    ns_passed = call_kwargs.kwargs.get("namespace")
    assert ns_passed == "ns-ingest", (
        f"pipeline.ingest_file must be called with namespace='ns-ingest', got {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_ingest_directory_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_directory_uses_resolved_namespace(tmp_path) -> None:
    """ingest_directory tool calls pipeline.ingest_directory with resolved namespace."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])
    pipeline._global_embedder = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["ingest_directory"]

    with _patch_namespace("ns-dir"):
        await fn(path=str(tmp_path), collection="col1")

    pipeline.ingest_directory.assert_called_once()
    call_kwargs = pipeline.ingest_directory.call_args
    ns_passed = call_kwargs.kwargs.get("namespace")
    assert ns_passed == "ns-dir", (
        f"pipeline.ingest_directory must be called with namespace='ns-dir', got {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_export_collection_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_collection_uses_resolved_namespace(tmp_path, monkeypatch) -> None:
    """export_collection passes the resolved namespace to store.get_collection_meta
    and to job_store.create_export."""
    import archon_search.server.mcp as mcp_module
    from archon_search.collection_meta import CollectionMeta

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    pipeline = MagicMock()
    store = MagicMock()
    meta = MagicMock(spec=CollectionMeta)
    store.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.store = store

    job_store = MagicMock()
    from archon_search.types import ExportJob, JobStatus

    fake_job = ExportJob(
        job_id="j1",
        status=JobStatus.QUEUED,
        created_at="2026-01-01",
        updated_at="2026-01-01",
        namespace="ns-export",
        collection="col1",
        output_path=str(tmp_path / "out.tar.gz"),
        tmp_path=str(tmp_path / "tmp.jsonl"),
    )
    job_store.create_export = MagicMock(return_value=fake_job)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None, job_store=job_store)
        fn = app.tools["export_collection"]

    with _patch_namespace("ns-export"):
        await fn(collection="col1", output_path=str(tmp_path / "exports"))

    # store.get_collection_meta must have been called with namespace="ns-export"
    store.get_collection_meta.assert_called_once()
    store_call = store.get_collection_meta.call_args
    ns_in_store = store_call.args[1] if len(store_call.args) > 1 else store_call.kwargs.get("namespace")
    assert ns_in_store == "ns-export", (
        f"store.get_collection_meta must be called with namespace='ns-export', got {store_call}"
    )

    # job_store.create_export must also use the resolved namespace
    job_store.create_export.assert_called_once()
    job_call = job_store.create_export.call_args
    ns_in_job = job_call.kwargs.get("namespace")
    assert ns_in_job == "ns-export", (
        f"job_store.create_export must be called with namespace='ns-export', got {job_call}"
    )


# ---------------------------------------------------------------------------
# test_import_collection_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_collection_uses_resolved_namespace(tmp_path, monkeypatch) -> None:
    """import_collection passes resolved namespace to store.get_collection_meta and job_store.create_import.

    The archive must be inside get_data_dir() for validate_export_path to allow it.
    We redirect ARCHON_SEARCH_DATA_DIR to tmp_path and place the archive there.
    """
    import io
    import json
    import tarfile

    import archon_search.server.mcp as mcp_module
    from archon_search.config import SearchConfig
    from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION

    # Redirect data dir to tmp_path so validate_export_path allows paths inside tmp_path
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    cfg = SearchConfig()
    cfg.embedding_model = "BAAI/bge-small-en-v1.5"

    # Create a valid tar.gz archive inside the data dir (so validate_export_path passes)
    archive_path = tmp_path / "col1.tar.gz"
    manifest = {
        "collection": "col1",
        "active_embedding_model": "BAAI/bge-small-en-v1.5",
        "schema_version": EXPORT_SCHEMA_VERSION,
    }
    with tarfile.open(archive_path, "w:gz") as tf:
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

    pipeline = MagicMock()
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)  # not found → allow import
    pipeline.store = store

    job_store = MagicMock()
    from archon_search.types import ImportJob, JobStatus

    fake_job = ImportJob(
        job_id="j2",
        status=JobStatus.QUEUED,
        created_at="2026-01-01",
        updated_at="2026-01-01",
        namespace="ns-import",
        collection="col1",
        archive_path=str(archive_path),
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
    )
    job_store.create_import = MagicMock(return_value=fake_job)

    with (
        patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP),
        # Bypass archive member validation so we can focus on namespace threading
        patch("archon_search.server.mcp.validate_archive_members"),
        # Bypass ImportArchiveReader so we control the manifest returned
        patch(
            "archon_search.server.mcp.ImportArchiveReader",
            return_value=MagicMock(
                read_manifest=MagicMock(return_value={
                    "collection": "col1",
                    "active_embedding_model": "BAAI/bge-small-en-v1.5",
                    "schema_version": EXPORT_SCHEMA_VERSION,
                })
            ),
        ),
    ):
        app = mcp_module.create_app(pipeline, "default", writer=None, config=cfg, job_store=job_store)
        fn = app.tools["import_collection"]

        with _patch_namespace("ns-import"):
            await fn(
                collection="col1",
                path=str(archive_path),
                force_overwrite=False,
                ignore_schema_version=False,
                on_error="fail",
            )

    # store.get_collection_meta must have been called with namespace="ns-import"
    store.get_collection_meta.assert_called_once()
    store_call = store.get_collection_meta.call_args
    ns_in_store = store_call.args[1] if len(store_call.args) > 1 else store_call.kwargs.get("namespace")
    assert ns_in_store == "ns-import", (
        f"store.get_collection_meta must be called with namespace='ns-import', got {store_call}"
    )

    # job_store.create_import must also use the resolved namespace
    job_store.create_import.assert_called_once()
    job_call = job_store.create_import.call_args
    ns_in_job = job_call.kwargs.get("namespace")
    assert ns_in_job == "ns-import", (
        f"job_store.create_import must be called with namespace='ns-import', got {job_call}"
    )


# ---------------------------------------------------------------------------
# test_delete_document_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_document_uses_resolved_namespace() -> None:
    """delete_document tool falls back to resolved namespace when namespace param is not given."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.delete_document = AsyncMock(return_value=1)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["delete_document"]

    with _patch_namespace("ns-del"):
        # Call without explicit namespace parameter — should use resolved namespace
        await fn(doc_id="d" * 64, collection="col1")

    pipeline.delete_document.assert_called_once()
    call_kwargs = pipeline.delete_document.call_args
    ns_passed = call_kwargs.kwargs.get("namespace")
    assert ns_passed == "ns-del", (
        f"pipeline.delete_document must fall back to resolved namespace 'ns-del', got {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_delete_document_namespace_mismatch_returns_forbidden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_document_namespace_mismatch_returns_forbidden() -> None:
    """delete_document returns forbidden when caller supplies a namespace that differs from authenticated."""
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.delete_document = AsyncMock(return_value=1)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["delete_document"]

    # Authenticated namespace is "ns-real"; caller passes "ns-attacker"
    with _patch_namespace("ns-real"):
        result = await fn(doc_id="d" * 64, collection="col1", namespace="ns-attacker")

    assert isinstance(result, dict), "Result must be a dict (error response)"
    assert result.get("code") == "forbidden", (
        f"delete_document with mismatched namespace must return code='forbidden', got {result}"
    )
    # pipeline must NOT have been called — rejection is upfront
    pipeline.delete_document.assert_not_called()


# ---------------------------------------------------------------------------
# test_update_collection_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_uses_resolved_namespace() -> None:
    """update_collection tool uses _get_request_namespace() instead of ctx.meta."""
    import archon_search.server.mcp as mcp_module
    from archon_search.collection_meta import CollectionMeta

    pipeline = MagicMock()
    store = MagicMock()
    pipeline.store = store

    meta = MagicMock(spec=CollectionMeta)
    meta.reindex_job_id = None
    meta.active_embedding_model = "BAAI/bge-small-en-v1.5"
    meta.pending_embedding_model = None
    meta.needs_reindex = False
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.count_chunks = AsyncMock(return_value=0)
    store.update_collection_meta = AsyncMock()
    store.get_stored_vector_dimension = AsyncMock(return_value=None)

    with (
        patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP),
        patch("archon_search.server.mcp.validate_embedding_model", AsyncMock(return_value=384)),
    ):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["update_collection"]

    ctx = MagicMock()
    ctx.meta = {}  # empty meta — namespace must come from _get_request_namespace(), not ctx.meta

    with _patch_namespace("ns-update"):
        await fn(collection_name="col1", embedding_model="BAAI/bge-small-en-v1.5", ctx=ctx)

    store.get_collection_meta.assert_called_once()
    call_args = store.get_collection_meta.call_args
    ns_passed = call_args.kwargs.get("namespace") or (
        call_args.args[1] if len(call_args.args) > 1 else None
    )
    assert ns_passed == "ns-update", (
        f"update_collection must use _get_request_namespace() → 'ns-update', got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_resolve_embedder_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_embedder_uses_resolved_namespace() -> None:
    """_resolve_embedder passes namespace to pipeline.get_collection_meta."""
    import archon_search.server.mcp as mcp_module
    from archon_search.embedder_cache import EmbedderCache

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)  # no meta → fallback to global embedder
    pipeline._global_embedder = MagicMock()

    embedder_cache = MagicMock(spec=EmbedderCache)

    from archon_search.config import SearchConfig
    cfg = SearchConfig()

    # Call _resolve_embedder directly with a namespace argument.
    # This tests the helper signature change introduced by BE-5.
    await mcp_module._resolve_embedder(
        pipeline, embedder_cache, "my-collection", cfg, namespace="ns-emb"
    )

    pipeline.get_collection_meta.assert_called_once()
    call_args = pipeline.get_collection_meta.call_args
    ns_passed = call_args.kwargs.get("namespace") or (
        call_args.args[1] if len(call_args.args) > 1 else None
    )
    assert ns_passed == "ns-emb", (
        f"_resolve_embedder must pass namespace='ns-emb' to get_collection_meta, got {call_args}"
    )


# ---------------------------------------------------------------------------
# test_resolve_embedder_skips_meta_fetch_when_meta_provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_embedder_skips_meta_fetch_when_meta_provided() -> None:
    """_resolve_embedder does NOT call pipeline.get_collection_meta when meta= is provided.

    The BE-7 namespace gate pre-fetches CollectionMeta before calling _resolve_embedder
    to avoid a redundant get_collection_meta call. This test verifies that when meta=
    is passed with a truthy value, the internal pipeline.get_collection_meta call is
    NOT made.

    Regression guard: if the `meta` shortcut is removed, pipeline.get_collection_meta
    would be called and the assertion would fail.
    """
    from unittest.mock import AsyncMock, MagicMock

    import archon_search.server.mcp as mcp_module
    from archon_search.collection_meta import CollectionMeta
    from archon_search.embedder_cache import EmbedderCache

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock()  # must NOT be called
    pipeline._global_embedder = MagicMock()

    embedder_cache = MagicMock(spec=EmbedderCache)
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    from archon_search.config import SearchConfig
    cfg = SearchConfig()

    pre_fetched_meta = CollectionMeta(
        name="my-collection",
        active_embedding_model="BAAI/bge-small-en-v1.5",
        doc_count=1,
        chunk_count=1,
        namespace="ns-a",
    )

    # Call with meta= provided — pipeline.get_collection_meta must NOT be called
    await mcp_module._resolve_embedder(
        pipeline, embedder_cache, "my-collection", cfg, namespace="ns-a", meta=pre_fetched_meta
    )

    pipeline.get_collection_meta.assert_not_called(), (
        "When meta= is provided to _resolve_embedder, it must NOT call "
        "pipeline.get_collection_meta again (double-fetch eliminated by BE-7 fix)."
    )


# ---------------------------------------------------------------------------
# test_get_request_namespace_falls_back_to_default
# ---------------------------------------------------------------------------


def test_get_request_namespace_falls_back_to_default() -> None:
    """_get_request_namespace() returns DEFAULT_NAMESPACE when no request is active.

    Exercised end to end through a real tool closure: list_collections runs with
    _get_request_namespace NOT patched and no HTTP request context, so the real
    get_http_request() raises RuntimeError and the tool must receive DEFAULT_NAMESPACE.
    """
    from unittest.mock import AsyncMock, MagicMock

    import archon_search.server.mcp as mcp_module
    from archon_search.constants import DEFAULT_NAMESPACE

    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["list_collections"]

    # Do NOT patch _get_request_namespace — let it fall back naturally.
    # Since no HTTP request context is active, it should return DEFAULT_NAMESPACE.
    asyncio.run(fn())

    pipeline.get_all_collections_meta.assert_called_once()
    call_args = pipeline.get_all_collections_meta.call_args
    ns_passed = call_args.args[0] if call_args.args else call_args.kwargs.get("namespace")
    assert ns_passed == DEFAULT_NAMESPACE, (
        f"Without an active HTTP request, _get_request_namespace() must return "
        f"DEFAULT_NAMESPACE={DEFAULT_NAMESPACE!r}, got {ns_passed!r}"
    )


# ---------------------------------------------------------------------------
# test_get_request_namespace_returns_state_namespace
# ---------------------------------------------------------------------------


def test_get_request_namespace_returns_state_namespace() -> None:
    """_get_request_namespace() reads .state.namespace from the active HTTP request.

    Exercised through a real tool closure with _get_request_namespace NOT patched:
    only get_http_request() (as imported into archon_search.server.mcp) is replaced
    by one returning a request whose .state.namespace is set, as APIKeyMiddleware does.
    """
    import archon_search.server.mcp as mcp_module

    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["list_collections"]

    with patch(
        "archon_search.server.mcp.get_http_request",
        return_value=_make_fake_request("ns-request"),
    ):
        asyncio.run(fn())

    pipeline.get_all_collections_meta.assert_called_once()
    call_args = pipeline.get_all_collections_meta.call_args
    ns_passed = (
        call_args.args[0] if call_args.args else call_args.kwargs.get("namespace")
    )
    assert ns_passed == "ns-request", (
        f"_get_request_namespace() must return request.state.namespace, got {ns_passed!r}"
    )


# ---------------------------------------------------------------------------
# test_get_request_namespace_propagates_non_runtime_errors
# ---------------------------------------------------------------------------


def test_get_request_namespace_propagates_non_runtime_errors() -> None:
    """Only RuntimeError ("no active HTTP request") falls back to DEFAULT_NAMESPACE.

    Any other failure inside get_http_request() is a real fault and must surface
    instead of silently serving the request from the default namespace.
    """
    import archon_search.server.mcp as mcp_module

    with (
        patch(
            "archon_search.server.mcp.get_http_request",
            side_effect=LookupError("request context corrupted"),
        ),
        pytest.raises(LookupError, match="request context corrupted"),
    ):
        mcp_module._get_request_namespace()


# ---------------------------------------------------------------------------
# test_get_request_namespace_defaults_when_state_lacks_namespace
# ---------------------------------------------------------------------------


def test_get_request_namespace_defaults_when_state_lacks_namespace() -> None:
    """An active request whose state carries no namespace resolves to DEFAULT_NAMESPACE.

    get_http_request() succeeds here, so the fallback comes from
    getattr(req.state, "namespace", DEFAULT_NAMESPACE), not from the RuntimeError path.
    """
    import types

    import archon_search.server.mcp as mcp_module
    from archon_search.constants import DEFAULT_NAMESPACE

    request_without_namespace = types.SimpleNamespace(state=types.SimpleNamespace())

    with patch(
        "archon_search.server.mcp.get_http_request",
        return_value=request_without_namespace,
    ) as fake_get_http_request:
        resolved = mcp_module._get_request_namespace()

    fake_get_http_request.assert_called_once_with()
    assert resolved == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# test_fastmcp_get_http_request_raises_runtime_error_outside_request
# ---------------------------------------------------------------------------


def test_fastmcp_get_http_request_raises_runtime_error_outside_request() -> None:
    """Upstream contract the DEFAULT_NAMESPACE fallback relies on.

    _get_request_namespace() catches only RuntimeError from
    fastmcp.server.dependencies.get_http_request(). This is the FastMCP-upgrade guard
    ADR 17 names (it replaces ADR 09's private _current_http_request import test): it
    fails in CI if archon_search.server.mcp stops using the public get_http_request, or
    if FastMCP changes the exception raised outside an HTTP request.
    """
    from fastmcp.server.dependencies import get_http_request

    import archon_search.server.mcp as mcp_module

    assert mcp_module.get_http_request is get_http_request, (
        "archon_search.server.mcp must use fastmcp.server.dependencies.get_http_request"
    )
    with pytest.raises(RuntimeError):
        get_http_request()


# ---------------------------------------------------------------------------
# test_search_tool_multi_collection_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_multi_collection_uses_resolved_namespace() -> None:
    """search tool calls pipeline.search_many with namespace from _get_request_namespace()
    when the collections parameter (multi-collection fan-out path) is provided."""
    import archon_search.server.mcp as mcp_module
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )
    pipeline.warmup_models = AsyncMock(return_value=True)
    pipeline._global_embedder = MagicMock()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None)
        fn = app.tools["search"]

    with _patch_namespace("ns-fanout"):
        await fn(query="hello", collections=["col1", "col2"])

    pipeline.search_many.assert_called_once()
    call_kwargs = pipeline.search_many.call_args
    ns_passed = call_kwargs.kwargs.get("namespace") or (
        call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
    )
    assert ns_passed == "ns-fanout", (
        f"pipeline.search_many must be called with namespace='ns-fanout', got {call_kwargs}"
    )


# ---------------------------------------------------------------------------
# test_explain_multi_collection_uses_resolved_namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_multi_collection_uses_resolved_namespace() -> None:
    """explain tool calls pipeline.explain with namespace from _get_request_namespace()
    when the collections parameter (multi-collection fan-out path) is provided."""
    import archon_search.server.mcp as mcp_module
    from archon_search.pipeline import ExplainPipelineResult

    pipeline = MagicMock()
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[],
            near_misses=[],
            acl_filtered=False,
            rag_fusion_applied=False,
            rag_fusion_queries_used=0,
            rag_fusion_attempted=False,
            rag_fusion_failure_reason=None,
            rag_fusion_sub_query_results=None,
        )
    )
    pipeline._global_embedder = MagicMock()

    from archon_search.config import SearchConfig
    cfg = SearchConfig()

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        app = mcp_module.create_app(pipeline, "default", writer=None, config=cfg)
        fn = app.tools["explain"]

    with _patch_namespace("ns-mc-explain"):
        await fn(query="hello", collections=["col1", "col2"])

    pipeline.explain.assert_called_once()
    call_kwargs = pipeline.explain.call_args
    ns_passed = call_kwargs.kwargs.get("namespace") or (
        call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
    )
    assert ns_passed == "ns-mc-explain", (
        f"pipeline.explain must be called with namespace='ns-mc-explain', got {call_kwargs}"
    )

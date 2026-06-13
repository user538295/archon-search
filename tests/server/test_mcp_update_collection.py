"""Tests for MCP update_collection tool (Task 10.1).

Verifies:
- Tool #11 is registered.
- Returns updated meta dict with needs_reindex=True and pending_embedding_model set.
- Returns an error dict on unknown model (ModelValidationError → 422-equivalent).
- Returns an error dict when a reindex job is active (409-equivalent).
- Namespace isolation: returns an error when collection not found in caller's namespace.
- No-op cases: no store write when already in requested state.
- Revert case: clears pending model.
- Empty collection: sets active_embedding_model directly.
- Dimension mismatch: returns error dict.
- PENDING job status: returns 409-equivalent error.
- Stale job + no-op: store IS written to clear stale reindex_job_id.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# ---------------------------------------------------------------------------
# FastMCP stub — same pattern as test_mcp_embedder_dispatch.py
# ---------------------------------------------------------------------------
if "fastmcp" not in sys.modules:
    _fastmcp = types.ModuleType("fastmcp")
    _fastmcp.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    _fastmcp.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _fastmcp


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


def _make_mcp_app(
    pipeline: Any,
    *,
    config: Any = None,
    embedder_cache: Any = None,
    job_store: Any = None,
) -> _FakeApp:
    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        return mcp_module.create_app(
            pipeline, "default", config=config, embedder_cache=embedder_cache, job_store=job_store
        )


def _make_meta(
    name: str = "col",
    namespace: str = "default",
    active_model: str = "old-model",
    pending_model: str | None = None,
    needs_reindex: bool = False,
    reindex_job_id: str | None = None,
) -> Any:
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(
        name=name,
        namespace=namespace,
        active_embedding_model=active_model,
        pending_embedding_model=pending_model,
        needs_reindex=needs_reindex,
        reindex_job_id=reindex_job_id,
    )


def _make_ctx(namespace: str = "default") -> MagicMock:
    ctx = MagicMock()
    ctx.meta = {"namespace": namespace}
    return ctx


def _make_config(collections: list[str] | None = None) -> Any:
    from archon_search.config import SearchConfig

    cfg = SearchConfig()
    # Ensure the collection name "col" maps via path_to_collection_name.
    # The simplest way: set collections to a single path whose name resolves to "col".
    # path_to_collection_name uses the last path component (stem).
    if collections is not None:
        cfg.collections = collections
    else:
        cfg.collections = ["/data/col"]
    cfg.pinned_collections = []
    return cfg


# ---------------------------------------------------------------------------
# Test: tool count is 11
# ---------------------------------------------------------------------------


def test_mcp_tool_count_is_13() -> None:
    """create_app must register exactly 13 tools."""
    pipeline = MagicMock()
    pipeline.store = MagicMock()

    job_store = MagicMock()

    app = _make_mcp_app(pipeline, job_store=job_store)
    assert len(app.tools) == 13, f"Expected 13 tools, got {len(app.tools)}: {list(app.tools)}"


# ---------------------------------------------------------------------------
# Test 1: happy path — returns updated meta with needs_reindex=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_returns_updated_meta() -> None:
    """update_collection returns a dict with needs_reindex=True and pending_embedding_model set."""
    meta = _make_meta(active_model="old-model", needs_reindex=False)

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.count_chunks = AsyncMock(return_value=5)  # > 0 → needs_reindex path
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" not in result, f"Unexpected error: {result}"
    # After Task 2.6 migration, update_collection returns CollectionDetailSchema (internal fields excluded).
    # pending_embedding_model is a public field and must be present.
    assert result.get("pending_embedding_model") == "model-X"
    # needs_reindex is an internal field and must NOT appear in the response.
    assert "needs_reindex" not in result
    # Happy path writes meta to store
    store.update_collection_meta.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: unknown model → error with 422-equivalent message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_unknown_model_returns_error() -> None:
    """validate_embedding_model raises ModelValidationError → returned dict has 'error' key."""
    from archon_search.model_validation import ModelValidationError

    meta = _make_meta()

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(side_effect=ModelValidationError("unknown model")),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="bad-model", ctx=ctx
        )

    assert "error" in result
    assert "422" in str(result.get("code", "")) or "validation" in str(result.get("code", "")).lower() or "model" in result["error"].lower()


# ---------------------------------------------------------------------------
# Test 3: active reindex job (RUNNING) → error with 409-equivalent message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_running_reindex_returns_error() -> None:
    """When meta.reindex_job_id points to a RUNNING job, return a 409-equivalent error."""
    from archon_search.types import JobStatus

    meta = _make_meta(reindex_job_id="job-123")

    running_job = MagicMock()
    running_job.status = JobStatus.RUNNING

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=running_job)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" in result
    code = result.get("code", "")
    assert "409" in str(code) or "conflict" in str(code).lower() or "reindex" in result["error"].lower()


# ---------------------------------------------------------------------------
# Test 4: namespace isolation — meta not found for caller's namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_namespace_isolation() -> None:
    """When get_collection_meta returns None for caller's namespace, return an error."""
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)  # not in caller's namespace

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    config = _make_config()

    app = _make_mcp_app(pipeline, config=config, job_store=job_store)
    ctx = _make_ctx(namespace="tenant-A")
    result = await app.tools["update_collection"](
        collection_name="col", embedding_model="model-X", ctx=ctx
    )

    assert "error" in result
    # The store should have been called with the caller's namespace
    store.get_collection_meta.assert_awaited_once_with("col", namespace="tenant-A")


# ---------------------------------------------------------------------------
# Test 5: no-op case 1 — active == requested and pending is None → no store write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_noop_active_equals_requested() -> None:
    """When active == requested and pending is None, no store write and returns meta."""
    meta = _make_meta(active_model="model-X", pending_model=None)

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" not in result
    # No-op: store must NOT be written
    store.update_collection_meta.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 6: no-op case 2 — pending == requested → no store write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_noop_pending_equals_requested() -> None:
    """When pending == requested, no store write and returns meta."""
    meta = _make_meta(active_model="old-model", pending_model="model-X")

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" not in result
    # No-op: store must NOT be written
    store.update_collection_meta.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 7: revert case — pending is not None and active == requested → clears pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_revert_clears_pending() -> None:
    """When pending != None and active == requested, clears pending_embedding_model and writes store."""
    meta = _make_meta(active_model="model-A", pending_model="model-B")

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        # Request active model — this reverts the pending change
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-A", ctx=ctx
        )

    assert "error" not in result
    assert result.get("pending_embedding_model") is None
    # needs_reindex is an internal field and must NOT appear in the response.
    assert "needs_reindex" not in result
    store.update_collection_meta.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 8: empty collection (chunk_count == 0) — model set directly as active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_empty_collection_sets_active_model() -> None:
    """When chunk_count == 0, model is set directly as active_embedding_model, no reindex needed."""
    meta = _make_meta(active_model="old-model")

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.count_chunks = AsyncMock(return_value=0)  # empty collection
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="new-model", ctx=ctx
        )

    assert "error" not in result
    # After Task 2.6 migration, active_embedding_model is renamed to embedding_model.
    assert result.get("embedding_model") == "new-model"
    assert result.get("pending_embedding_model") is None
    # needs_reindex is an internal field and must NOT appear in the response.
    assert "needs_reindex" not in result
    store.update_collection_meta.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 9: dimension mismatch → error dict returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_dimension_mismatch_returns_error() -> None:
    """When stored_dim != new_dim, return an error dict."""
    meta = _make_meta(active_model="old-model")

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=768)  # stored dimension

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=None)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),  # new model produces different dimension
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="new-model", ctx=ctx
        )

    assert "error" in result
    assert "dimension" in result["error"].lower() or "mismatch" in result["error"].lower()


# ---------------------------------------------------------------------------
# Test 10: PENDING job status → 409-equivalent error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_pending_reindex_returns_error() -> None:
    """When meta.reindex_job_id points to a PENDING job, return a 409-equivalent error."""
    from archon_search.types import JobStatus

    meta = _make_meta(reindex_job_id="job-456")

    pending_job = MagicMock()
    pending_job.status = JobStatus.PENDING

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=pending_job)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" in result
    code = result.get("code", "")
    assert "conflict" in str(code).lower() or "reindex" in result["error"].lower()


# ---------------------------------------------------------------------------
# Test 11: stale job + no-op → store IS written to clear stale reindex_job_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_stale_job_noop_writes_store() -> None:
    """Stale reindex_job_id (job DONE) + no-op branch → store IS written to clear stale ID."""
    from archon_search.types import JobStatus

    meta = _make_meta(
        active_model="model-X",
        pending_model=None,
        reindex_job_id="job-stale",  # stale job
    )

    done_job = MagicMock()
    done_job.status = JobStatus.DONE

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.get_stored_vector_dimension = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = MagicMock()
    pipeline.store = store

    job_store = MagicMock()
    job_store.get = MagicMock(return_value=done_job)

    config = _make_config()

    with patch(
        "archon_search.server.mcp.validate_embedding_model",
        new=AsyncMock(return_value=384),
    ):
        app = _make_mcp_app(pipeline, config=config, job_store=job_store)
        ctx = _make_ctx()
        # Request the same model as active → no-op branch, but stale_cleared=True
        result = await app.tools["update_collection"](
            collection_name="col", embedding_model="model-X", ctx=ctx
        )

    assert "error" not in result
    # stale_cleared=True → store MUST be written even though it's a no-op for the model state
    store.update_collection_meta.assert_awaited_once()
    # reindex_job_id is an internal field and must NOT appear in the response after Task 2.6 migration.
    assert "reindex_job_id" not in result

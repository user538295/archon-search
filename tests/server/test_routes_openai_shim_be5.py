"""Tests for POST /v1/chat/completions non-streaming path (BE-5)."""
from __future__ import annotations

import asyncio
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_VALID_KEY = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_app(
    tmp_path,
    monkeypatch,
    *,
    openai_shim_enabled: bool,
    collections: list[str] | None = None,
    inject_citations: bool = True,
    max_fanout: int = 8,
):
    """Build a TestClient-wrapped app with a stub pipeline.

    Uses ``monkeypatch.setenv`` so env vars auto-revert after each test.
    """
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _VALID_KEY)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.openai_shim.enabled = openai_shim_enabled
    cfg.openai_shim.inject_citations = inject_citations
    cfg.mcp.enabled = False
    cfg.max_fanout = max_fanout

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)

    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    metas: list[CollectionMeta] = []
    for name in (collections or []):
        m = CollectionMeta(
            name=name,
            namespace=DEFAULT_NAMESPACE,
            description=None,
            chunk_count=0,
        )
        metas.append(m)

    stub_pipeline = MagicMock()
    stub_pipeline.get_all_collections_meta = AsyncMock(return_value=metas)
    app.state.pipeline = stub_pipeline

    return app


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_VALID_KEY}"}


def _chat_body(model: str, user_msg: str, **extra) -> dict:
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": user_msg}],
    }
    body.update(extra)
    return body


def _make_search_result(text: str = "chunk text", source_path: str = "doc.txt") -> MagicMock:
    """Return a mock SearchResult with the given text and source_path."""
    from archon_search._types import SearchResult

    r = MagicMock(spec=SearchResult)
    r.text = text
    r.source_path = source_path
    return r


def _make_pipeline_result(results=None, excluded_collections=None):
    """Return a mock SearchPipelineResult."""
    from archon_search._types import ExcludedCollection
    from archon_search.pipeline import SearchPipelineResult

    pipeline_result = MagicMock(spec=SearchPipelineResult)
    pipeline_result.results = results if results is not None else []
    pipeline_result.excluded_collections = excluded_collections if excluded_collections is not None else []
    pipeline_result.acl_filtered = False
    return pipeline_result


def _make_collection_meta(name: str = "col", active_embedding_model: str | None = None):
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta = CollectionMeta(
        name=name,
        namespace=DEFAULT_NAMESPACE,
        description=None,
        chunk_count=1,
    )
    meta.active_embedding_model = active_embedding_model
    return meta


# ---------------------------------------------------------------------------
# Unit tests — stub pipeline
# ---------------------------------------------------------------------------


class TestExtractLastUserMessage:
    def test_extract_last_user_message(self, tmp_path, monkeypatch):
        """Last role='user' message is extracted as query, ignoring prior system/assistant."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        result = _make_pipeline_result([_make_search_result("found")])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        body = {
            "model": "archon-search/col",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "actual query"},
            ],
        }

        with TestClient(app) as client:
            resp = client.post("/v1/chat/completions", json=body, headers=_auth_headers())

        assert resp.status_code == 200
        # The pipeline should have been called with the LAST user message
        call_args = app.state.pipeline.search.call_args
        assert call_args[0][0] == "actual query" or call_args.kwargs.get("query", call_args[0][0]) == "actual query"


class TestNoUserMessageReturns422:
    def test_no_user_message_returns_422(self, tmp_path, monkeypatch):
        """Empty messages list → 422 with OpenAI error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "archon-search/col", "messages": []},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["message"] == "messages must contain at least one user message"
        assert body["error"]["type"] == "invalid_request_error"

    def test_no_user_role_returns_422(self, tmp_path, monkeypatch):
        """Messages with only system role → 422 with same error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "archon-search/col", "messages": [{"role": "system", "content": "sys"}]},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["message"] == "messages must contain at least one user message"
        assert body["error"]["type"] == "invalid_request_error"


class TestFormatChunksWithCitations:
    def test_format_chunks_with_citations(self, tmp_path, monkeypatch):
        """inject_citations=True wraps each chunk with citation text."""
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col"],
            inject_citations=True,
        )

        meta = _make_collection_meta("col")
        sr = _make_search_result("chunk content here", "my/doc.txt")
        result = _make_pipeline_result([sr])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "chunk content here" in content
        assert "my/doc.txt" in content
        assert "[Source:" in content


class TestFormatChunksNoCitations:
    def test_format_chunks_no_citations(self, tmp_path, monkeypatch):
        """inject_citations=False returns chunk text only."""
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col"],
            inject_citations=False,
        )

        meta = _make_collection_meta("col")
        sr = _make_search_result("plain chunk text", "doc.txt")
        result = _make_pipeline_result([sr])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "plain chunk text" in content
        assert "[Source:" not in content


class TestZeroResultsReturnsEmptyContent:
    def test_zero_results_returns_empty_content(self, tmp_path, monkeypatch):
        """Empty results produce content='' and finish_reason='stop'."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        result = _make_pipeline_result([])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == ""
        assert body["choices"][0]["finish_reason"] == "stop"


class TestUnknownCollectionReturnsOpenAI404:
    def test_unknown_collection_returns_openai_404(self, tmp_path, monkeypatch):
        """model='archon-search/ghost' when get_collection_meta returns None → 404 OpenAI error."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=None)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/ghost", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "The model 'archon-search/ghost' does not exist."
        assert body["error"]["type"] == "invalid_request_error"


class TestNoCollectionsReturnsOpenAI404:
    def test_no_collections_returns_openai_404(self, tmp_path, monkeypatch):
        """model='archon-search', get_all_collections_meta returns [] → 404 with 'No collections available.'"""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=[])

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "No collections available."
        assert body["error"]["type"] == "invalid_request_error"


class TestEmbedderResolutionUsesCollectionModel:
    def test_embedder_resolution_uses_collection_model(self, tmp_path, monkeypatch):
        """When meta.active_embedding_model differs from global, per-collection embedder is resolved."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col", active_embedding_model="custom-model")
        result = _make_pipeline_result([_make_search_result("text")])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        mock_embedder = MagicMock()
        mock_embedder_cache = MagicMock()
        mock_embedder_cache.get_or_load = AsyncMock(return_value=mock_embedder)

        with TestClient(app) as client:
            # Set AFTER lifespan startup (which sets the real embedder_cache on app.state)
            client.app.state.embedder_cache = mock_embedder_cache
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # embedder_cache.get_or_load should have been called with the per-collection model
        mock_embedder_cache.get_or_load.assert_awaited_once_with("custom-model")


class TestFanoutCapTruncatesAndWarns:
    def test_fanout_cap_truncates_and_warns(self, tmp_path, monkeypatch, caplog):
        """When namespace returns more than max_fanout collections, handler slices and warns."""
        import logging

        max_fanout = 3
        all_cols = ["col-a", "col-b", "col-c", "col-d", "col-e"]  # 5 > max_fanout=3
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=all_cols,
            max_fanout=max_fanout,
        )

        result = _make_pipeline_result([_make_search_result("result text")])
        app.state.pipeline.search_many = AsyncMock(return_value=result)

        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json=_chat_body("archon-search", "query"),
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        # Omitted collections: col-d, col-e
        warning_text = " ".join(caplog.messages)
        assert "col-d" in warning_text
        assert "col-e" in warning_text


class TestFanoutCapExactBoundaryPasses:
    def test_fanout_cap_exact_boundary_passes(self, tmp_path, monkeypatch, caplog):
        """Exactly max_fanout collections: no WARNING logged; all searched."""
        import logging

        max_fanout = 3
        all_cols = ["col-a", "col-b", "col-c"]  # exactly max_fanout
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=all_cols,
            max_fanout=max_fanout,
        )

        result = _make_pipeline_result([_make_search_result("result text")])
        app.state.pipeline.search_many = AsyncMock(return_value=result)

        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json=_chat_body("archon-search", "query"),
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        # No WARNING about truncation
        truncation_warnings = [m for m in caplog.messages if "fanout" in m.lower() or "truncat" in m.lower() or "omit" in m.lower()]
        assert not truncation_warnings


class TestSearchManyCollectionNotFoundReturns404:
    def test_search_many_collection_not_found_returns_404(self, tmp_path, monkeypatch):
        """search_many raises CollectionNotFoundError → 404 OpenAI error."""
        from archon_search.pipeline import CollectionNotFoundError

        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])
        app.state.pipeline.search_many = AsyncMock(side_effect=CollectionNotFoundError(["col"]))

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"


class TestUnrecognizedModelReturns404:
    def test_unrecognized_model_returns_404(self, tmp_path, monkeypatch):
        """model='gpt-4' → 404 with OpenAI error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("gpt-4", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "The model 'gpt-4' does not exist."
        assert body["error"]["type"] == "invalid_request_error"


class TestDirectSearchTimeoutReturnsOpenAI504:
    def test_direct_search_timeout_returns_openai_504(self, tmp_path, monkeypatch):
        """asyncio.TimeoutError from pipeline.search → 504 with OpenAI error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(side_effect=asyncio.TimeoutError())

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 504
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "Request timeout."
        assert body["error"]["type"] == "server_error"


class TestFanoutTimeoutReturnsOpenAI504:
    def test_fanout_timeout_returns_openai_504(self, tmp_path, monkeypatch):
        """FanoutTimeoutError from search_many → 504 with OpenAI error shape."""
        from archon_search.pipeline import FanoutTimeoutError

        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])
        app.state.pipeline.search_many = AsyncMock(side_effect=FanoutTimeoutError())

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 504
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "Request timeout."
        assert body["error"]["type"] == "server_error"


class TestMetadataLookupErrorReturnsOpenAI503:
    def test_metadata_lookup_error_returns_openai_503(self, tmp_path, monkeypatch):
        """search_many raises MetadataLookupError → 503 OpenAI error shape."""
        from archon_search.pipeline import MetadataLookupError

        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])
        app.state.pipeline.search_many = AsyncMock(
            side_effect=MetadataLookupError(RuntimeError("db unavailable"))
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 503
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "server_error"


class TestDirectSearchStoreErrorReturnsOpenAI503:
    def test_direct_search_store_error_returns_openai_503(self, tmp_path, monkeypatch):
        """Unexpected exception from pipeline.search → 503 OpenAI error shape."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(side_effect=RuntimeError("store crashed"))

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 503
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "server_error"


class TestTopKRequestFieldIgnored:
    def test_top_k_request_field_ignored(self, tmp_path, monkeypatch):
        """POST with top_k=99: pipeline called with its own top_k, not request-supplied."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        result = _make_pipeline_result([_make_search_result("text")])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=result)

        body = {"model": "archon-search/col", "messages": [{"role": "user", "content": "q"}], "top_k": 99}

        with TestClient(app) as client:
            resp = client.post("/v1/chat/completions", json=body, headers=_auth_headers())

        assert resp.status_code == 200
        app.state.pipeline.search.assert_awaited_once()
        call_kwargs = app.state.pipeline.search.call_args.kwargs
        assert "top_k" not in call_kwargs


class TestTrailingSlashModelReturns404:
    def test_trailing_slash_model_returns_404(self, tmp_path, monkeypatch):
        """model='archon-search/' → 404 OpenAI error (empty collection name)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=None)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/", "query"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# Integration tests — real app + real store
# ---------------------------------------------------------------------------


class TestChatCompletionsDirectCollection:
    def test_chat_completions_direct_collection(self, tmp_path, monkeypatch):
        """Ingest doc into my-col; POST with model='archon-search/my-col' returns 200 with text."""
        from tests.integration.conftest import ingest_file_via_path, make_real_app

        doc = tmp_path / "content.txt"
        doc.write_text("The archon retrieval system supports hybrid search.")

        with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            ingest_file_via_path(client, "my-col", str(doc), api_key=api_key)

            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/my-col", "hybrid search"),
                headers=headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "archon-search/my-col"
        assert len(body["choices"]) == 1
        choice = body["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert "hybrid" in choice["message"]["content"].lower() or choice["message"]["content"] != ""
        assert body["id"].startswith("chatcmpl-")


class TestChatCompletionsRouterPath:
    def test_chat_completions_router_path(self, tmp_path, monkeypatch):
        """Two collections with docs; model='archon-search' returns 200 with valid chat.completion structure."""
        from tests.integration.conftest import ingest_file_via_path, make_real_app

        doc_a = tmp_path / "doc_a.txt"
        doc_a.write_text("Alpha content about machine learning.")
        doc_b = tmp_path / "doc_b.txt"
        doc_b.write_text("Beta content about vector search systems.")

        with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            ingest_file_via_path(client, "col-alpha", str(doc_a), api_key=api_key)
            ingest_file_via_path(client, "col-beta", str(doc_b), api_key=api_key)

            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "vector search"),
                headers=headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")


class TestNamespaceIsolationReturns404:
    def test_namespace_isolation_returns_404(self, tmp_path, monkeypatch):
        """Query ns2-col with ns1 token → get_collection_meta returns None → 404 OpenAI error."""
        from tests.integration.conftest import ingest_file_via_path, make_real_app
        import secrets

        ns1_key = secrets.token_hex(32)
        ns2_key = secrets.token_hex(32)

        with make_real_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            namespaces={ns1_key: "ns1", ns2_key: "ns2"},
        ) as (client, cfg, api_key):
            # api_key is the default-ns key; use ns2_key to ingest into ns2
            doc = tmp_path / "ns2_doc.txt"
            doc.write_text("NS2 content.")
            ingest_file_via_path(client, "ns2-col", str(doc), api_key=ns2_key)

            # Query ns2-col using ns1 token → collection does not exist in ns1
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/ns2-col", "query"),
                headers={"Authorization": f"Bearer {ns1_key}"},
            )

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"


class TestAllCollectionsExcludedReturnsEmpty200:
    def test_all_collections_excluded_returns_empty_200(self, tmp_path, monkeypatch, caplog):
        """All collections excluded → 200 with content='', finish_reason='stop'; WARNING logged."""
        import logging
        from archon_search._types import ExcludedCollection
        from archon_search.pipeline import SearchPipelineResult

        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col-one", "col-two"],
        )

        excluded = [
            MagicMock(name_attr="col-one", reason="embedder_mismatch"),
            MagicMock(name_attr="col-two", reason="embedder_mismatch"),
        ]
        excluded[0].name = "col-one"
        excluded[0].reason = "embedder_mismatch"
        excluded[1].name = "col-two"
        excluded[1].reason = "embedder_mismatch"

        result = _make_pipeline_result([], excluded)

        app.state.pipeline.search_many = AsyncMock(return_value=result)

        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json=_chat_body("archon-search", "query"),
                    headers=_auth_headers(),
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == ""
        assert body["choices"][0]["finish_reason"] == "stop"

        # WARNING should mention excluded collections
        warning_text = " ".join(caplog.messages)
        assert "col-one" in warning_text
        assert "col-two" in warning_text

"""Tests for POST /v1/chat/completions streaming path (BE-7)."""
from __future__ import annotations

import json
import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_VALID_KEY = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_routes_openai_shim_be5.py)
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
    from archon_search._types import SearchResult

    r = MagicMock(spec=SearchResult)
    r.text = text
    r.source_path = source_path
    return r


def _make_pipeline_result(results=None, excluded_collections=None):
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


def _parse_sse(text: str) -> tuple[list[dict], bool]:
    """Parse SSE response text into (list_of_data_frames, done_present)."""
    lines = text.split("\n")
    data_lines = [line[6:] for line in lines if line.startswith("data: ") and line != "data: [DONE]"]
    done_present = "data: [DONE]" in lines
    frames = [json.loads(d) for d in data_lines]
    return frames, done_present


# ---------------------------------------------------------------------------
# Unit tests — streaming generator behaviour (stub pipeline)
# ---------------------------------------------------------------------------


class TestStreamGeneratorYieldsSSEEvents:
    def test_stream_generator_yields_sse_events(self, tmp_path, monkeypatch):
        """Three result chunks → three data events + stop frame + [DONE]; same id across all."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        results = [
            _make_search_result("chunk A", "a.txt"),
            _make_search_result("chunk B", "b.txt"),
            _make_search_result("chunk C", "c.txt"),
        ]
        pipeline_result = _make_pipeline_result(results)

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        frames, done_present = _parse_sse(resp.text)

        # Three content frames + one stop frame
        assert len(frames) == 4
        assert done_present

        # All frames share the same id
        ids = [f["id"] for f in frames]
        assert len(set(ids)) == 1
        assert ids[0].startswith("chatcmpl-")

        # First three are content frames; last is stop
        for i in range(3):
            assert frames[i]["choices"][0]["finish_reason"] is None
        assert frames[3]["choices"][0]["finish_reason"] == "stop"


class TestStreamZeroResultsSingleEvent:
    def test_stream_zero_results_single_event(self, tmp_path, monkeypatch):
        """Empty results → one empty delta + stop frame + [DONE]."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        pipeline_result = _make_pipeline_result([])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        frames, done_present = _parse_sse(resp.text)

        # One empty content delta + stop frame
        assert len(frames) == 2
        assert done_present

        # First frame has empty content
        assert frames[0]["choices"][0]["delta"].get("content") == ""
        assert frames[0]["choices"][0]["finish_reason"] is None
        # Stop frame
        assert frames[1]["choices"][0]["finish_reason"] == "stop"
        assert frames[1]["choices"][0]["delta"] == {}


class TestStreamEventFormat:
    def test_stream_event_format(self, tmp_path, monkeypatch):
        """Each data frame has correct SSE structure; role only on first frame."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        results = [
            _make_search_result("first chunk", "f.txt"),
            _make_search_result("second chunk", "s.txt"),
        ]
        pipeline_result = _make_pipeline_result(results)

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        frames, done_present = _parse_sse(resp.text)

        assert done_present
        # 2 content frames + 1 stop frame
        assert len(frames) == 3

        for frame in frames:
            assert frame["object"] == "chat.completion.chunk"
            assert isinstance(frame["created"], int)
            assert len(frame["choices"]) == 1

        # First frame must include role="assistant"
        first_delta = frames[0]["choices"][0]["delta"]
        assert first_delta.get("role") == "assistant"
        assert frames[0]["choices"][0]["finish_reason"] is None

        # Frames 2..N must NOT include role key in delta
        for frame in frames[1:-1]:
            assert "role" not in frame["choices"][0]["delta"]
            assert frame["choices"][0]["finish_reason"] is None

        # Stop frame: delta == {}, finish_reason == "stop"
        stop_frame = frames[-1]
        assert stop_frame["choices"][0]["delta"] == {}
        assert stop_frame["choices"][0]["finish_reason"] == "stop"


class TestStreamNoCitations:
    def test_stream_no_citations(self, tmp_path, monkeypatch):
        """inject_citations=False → delta content has chunk text only, no [Source: ...] lines."""
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col"],
            inject_citations=False,
        )

        meta = _make_collection_meta("col")
        results = [_make_search_result("plain text chunk", "doc.txt")]
        pipeline_result = _make_pipeline_result(results)

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        frames, _ = _parse_sse(resp.text)

        # Content frames (all but the stop frame)
        content_frames = [f for f in frames if f["choices"][0]["finish_reason"] is None]
        assert content_frames  # at least one

        all_content = "".join(f["choices"][0]["delta"].get("content", "") for f in content_frames)
        assert "plain text chunk" in all_content
        assert "[Source:" not in all_content


class TestStreamCitationsInline:
    def test_stream_citations_inline(self, tmp_path, monkeypatch):
        """inject_citations=True → delta content includes Context block and [Source: ...] line."""
        app = _make_stub_app(
            tmp_path,
            monkeypatch,
            openai_shim_enabled=True,
            collections=["col"],
            inject_citations=True,
        )

        meta = _make_collection_meta("col")
        results = [_make_search_result("retrieved text", "my/doc.txt")]
        pipeline_result = _make_pipeline_result(results)

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        frames, _ = _parse_sse(resp.text)

        content_frames = [f for f in frames if f["choices"][0]["finish_reason"] is None]
        assert content_frames

        all_content = "".join(f["choices"][0]["delta"].get("content", "") for f in content_frames)
        assert "retrieved text" in all_content
        assert "[Source:" in all_content
        assert "my/doc.txt" in all_content


class TestStreamDirectTimeoutReturns504:
    def test_stream_direct_timeout_returns_504(self, tmp_path, monkeypatch):
        """Direct path: asyncio.TimeoutError with stream=True → JSON 504, not broken SSE."""
        import asyncio
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(side_effect=asyncio.TimeoutError())

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 504
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "server_error"
        assert "data: " not in resp.text


class TestStreamDirectStoreErrorReturns503:
    def test_stream_direct_store_error_returns_503(self, tmp_path, monkeypatch):
        """Direct path: store exception with stream=True → JSON 503, not broken SSE."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(side_effect=RuntimeError("store crashed"))

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 503
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "server_error"
        assert "data: " not in resp.text


class TestStreamFanoutTimeoutReturns504:
    def test_stream_fanout_timeout_returns_504(self, tmp_path, monkeypatch):
        """Fanout path: FanoutTimeoutError with stream=True → JSON 504, not broken SSE."""
        from archon_search.pipeline import FanoutTimeoutError

        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])
        app.state.pipeline.search_many = AsyncMock(side_effect=FanoutTimeoutError())

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 504
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "server_error"
        assert "data: " not in resp.text


# ---------------------------------------------------------------------------
# Integration tests — real app + real store
# ---------------------------------------------------------------------------


class TestStreamingReturnsSSEEvents:
    def test_streaming_returns_sse_events(self, tmp_path, monkeypatch):
        """Ingest doc; POST with stream=True returns SSE stream with data frames."""
        from tests.integration.conftest import ingest_file_via_path, make_real_app

        doc = tmp_path / "content.txt"
        doc.write_text("The archon retrieval system supports hybrid search.")

        with make_real_app(tmp_path, monkeypatch, openai_shim_enabled=True) as (client, cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            ingest_file_via_path(client, "my-col", str(doc), api_key=api_key)

            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/my-col", "hybrid search", stream=True),
                headers=headers,
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        frames, done_present = _parse_sse(resp.text)
        assert done_present

        # At minimum: at least one content frame + stop frame
        assert len(frames) >= 2
        stop_frame = frames[-1]
        assert stop_frame["choices"][0]["finish_reason"] == "stop"
        assert stop_frame["choices"][0]["delta"] == {}

        # All frames have the same id
        ids = [f["id"] for f in frames]
        assert len(set(ids)) == 1
        assert ids[0].startswith("chatcmpl-")


class TestStreamingZeroResults:
    def test_streaming_zero_results(self, tmp_path, monkeypatch):
        """Zero results from pipeline with stream=True → empty delta + stop + [DONE]; no hang."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])

        meta = _make_collection_meta("col")
        pipeline_result = _make_pipeline_result([])

        app.state.pipeline.get_collection_meta = AsyncMock(return_value=meta)
        app.state.pipeline.search = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/col", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        frames, done_present = _parse_sse(resp.text)
        assert done_present
        # One empty content delta + stop frame
        assert len(frames) == 2
        assert frames[0]["choices"][0]["delta"].get("content") == ""
        assert frames[0]["choices"][0]["finish_reason"] is None
        assert frames[1]["choices"][0]["delta"] == {}
        assert frames[1]["choices"][0]["finish_reason"] == "stop"


class TestStreamingRaceCollectionDeleted:
    def test_streaming_race_collection_deleted(self, tmp_path, monkeypatch):
        """search_many raises CollectionNotFoundError with stream=True → JSON 404 (not broken SSE)."""
        from archon_search.pipeline import CollectionNotFoundError

        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col"])
        app.state.pipeline.search_many = AsyncMock(side_effect=CollectionNotFoundError(["col"]))

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query", stream=True),
                headers=_auth_headers(),
            )

        # Must be JSON 404, not a partial SSE stream
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"
        # Response must NOT be SSE (no "data: " prefix lines)
        assert "data: " not in resp.text


class TestStreamFanoutHappyPath:
    def test_stream_fanout_happy_path(self, tmp_path, monkeypatch):
        """Fanout path with stream=True and results → SSE frames for each result chunk."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True, collections=["col-a", "col-b"])

        results = [
            _make_search_result("result from col-a", "a.txt"),
            _make_search_result("result from col-b", "b.txt"),
        ]
        pipeline_result = _make_pipeline_result(results)
        app.state.pipeline.search_many = AsyncMock(return_value=pipeline_result)

        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        frames, done_present = _parse_sse(resp.text)
        assert done_present
        # 2 content frames + 1 stop frame
        assert len(frames) == 3
        # All frames share the same id
        ids = [f["id"] for f in frames]
        assert len(set(ids)) == 1
        # First frame carries role="assistant"
        assert frames[0]["choices"][0]["delta"].get("role") == "assistant"
        # Content frames carry result text
        all_content = "".join(
            f["choices"][0]["delta"].get("content", "") for f in frames if f["choices"][0]["finish_reason"] is None
        )
        assert "result from col-a" in all_content
        assert "result from col-b" in all_content
        # Stop frame last
        assert frames[-1]["choices"][0]["finish_reason"] == "stop"
        assert frames[-1]["choices"][0]["delta"] == {}


class TestStreamUnknownModelReturnsJson404:
    def test_stream_unknown_model_returns_json_404(self, tmp_path, monkeypatch):
        """stream=True + unknown model prefix → JSON 404, not broken SSE (materialization before StreamingResponse)."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("gpt-4", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "The model 'gpt-4' does not exist."
        assert body["error"]["type"] == "invalid_request_error"
        assert "data: " not in resp.text


class TestStreamMissingCollectionReturnsJson404:
    def test_stream_missing_collection_returns_json_404(self, tmp_path, monkeypatch):
        """stream=True + archon-search/nonexistent (meta=None) → JSON 404, not broken SSE."""
        app = _make_stub_app(tmp_path, monkeypatch, openai_shim_enabled=True)
        app.state.pipeline.get_collection_meta = AsyncMock(return_value=None)

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json=_chat_body("archon-search/nonexistent", "query", stream=True),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert "error" in body
        assert body["error"]["message"] == "The model 'archon-search/nonexistent' does not exist."
        assert body["error"]["type"] == "invalid_request_error"
        assert "data: " not in resp.text

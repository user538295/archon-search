"""E2E tests for the G9 OpenAI-compatible shim using the real openai SDK.

These tests wire the ``openai.AsyncOpenAI`` client against the in-process
FastAPI app via ``httpx.AsyncClient(transport=httpx.ASGITransport(app))``.
This is the closest automated approximation of how Cursor (or any OpenAI SDK
client) would talk to archon-search — the full SDK serialization, auth
header injection, and SSE parsing all run end-to-end.

The pipeline is stubbed (mock AsyncMock) so no real LanceDB / embedding model
is required; the shim routing logic is exercised for real.
"""
from __future__ import annotations

import asyncio
import secrets
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("openai_shim")]

_VALID_KEY = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_result(text: str = "chunk text", source_path: str = "doc.txt"):
    from archon_search._types import SearchResult

    r = MagicMock(spec=SearchResult)
    r.text = text
    r.source_path = source_path
    return r


def _make_pipeline_result(results=None, excluded_collections=None):
    from archon_search.pipeline import SearchPipelineResult

    pr = MagicMock(spec=SearchPipelineResult)
    pr.results = results if results is not None else []
    pr.excluded_collections = excluded_collections if excluded_collections is not None else []
    return pr


def _make_collection_meta(name: str):
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    return CollectionMeta(name=name, namespace=DEFAULT_NAMESPACE, description=None, chunk_count=0)


def _make_stub_app(
    tmp_path,
    monkeypatch,
    *,
    collections: list[str] | None = None,
    inject_citations: bool = True,
):
    """Return a FastAPI app with shim enabled and a mock pipeline."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", _VALID_KEY)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.openai_shim.enabled = True
    cfg.openai_shim.inject_citations = inject_citations
    cfg.mcp.enabled = False

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )

    app = create_app(cfg, job_store, scheduler=scheduler)

    metas = [_make_collection_meta(n) for n in (collections or [])]
    stub = MagicMock()
    stub.get_all_collections_meta = AsyncMock(return_value=metas)
    app.state.pipeline = stub

    return app


async def _openai_client(app, api_key: str = _VALID_KEY):
    """Return an AsyncOpenAI client wired to ``app`` via in-process ASGI transport."""
    import openai

    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return openai.AsyncOpenAI(base_url="http://test/v1", api_key=api_key, http_client=http)


# ---------------------------------------------------------------------------
# Tests — GET /v1/models
# ---------------------------------------------------------------------------


def test_list_models_via_sdk_always_has_catch_all(tmp_path, monkeypatch):
    """SDK client.models.list() always includes the 'archon-search' catch-all."""
    app = _make_stub_app(tmp_path, monkeypatch)

    async def _run():
        client = await _openai_client(app)
        return await client.models.list()

    result = asyncio.run(_run())
    ids = [m.id for m in result.data]
    assert "archon-search" in ids


def test_list_models_via_sdk_includes_collections(tmp_path, monkeypatch):
    """SDK client.models.list() returns one entry per collection plus the catch-all."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs", "code"])

    async def _run():
        client = await _openai_client(app)
        return await client.models.list()

    result = asyncio.run(_run())
    ids = [m.id for m in result.data]
    assert "archon-search" in ids
    assert "archon-search/docs" in ids
    assert "archon-search/code" in ids


# ---------------------------------------------------------------------------
# Tests — POST /v1/chat/completions non-streaming
# ---------------------------------------------------------------------------


def test_chat_completion_nonstreaming_returns_valid_response(tmp_path, monkeypatch):
    """Non-streaming completion returns a ChatCompletion with one assistant choice."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])
    pr = _make_pipeline_result([_make_search_result("retrieved text", "doc.txt")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "what is archon?"}],
            stream=False,
        )

    completion = asyncio.run(_run())
    assert completion.object == "chat.completion"
    assert len(completion.choices) == 1
    assert completion.choices[0].message.role == "assistant"
    assert "retrieved text" in completion.choices[0].message.content


def test_chat_completion_fanout_nonstreaming(tmp_path, monkeypatch):
    """Fanout model 'archon-search' fans across all collections and merges results."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["a", "b"])
    pr = _make_pipeline_result([_make_search_result("fanout chunk", "x.txt")])
    app.state.pipeline.search_many = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search",
            messages=[{"role": "user", "content": "tell me"}],
            stream=False,
        )

    completion = asyncio.run(_run())
    assert completion.choices[0].message.role == "assistant"
    assert "fanout chunk" in completion.choices[0].message.content


# ---------------------------------------------------------------------------
# Tests — POST /v1/chat/completions streaming
# ---------------------------------------------------------------------------


def test_chat_completion_streaming_yields_content_chunks(tmp_path, monkeypatch):
    """Streaming completion delivers content via SSE chunks and ends with finish_reason='stop'."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])
    pr = _make_pipeline_result([_make_search_result("streamed chunk", "a.txt")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        content_parts: list[str] = []
        finish_reasons: list[str | None] = []
        # create(stream=True) yields ChatCompletionChunk directly
        response = await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        async for chunk in response:
            for choice in chunk.choices:
                if choice.delta.content:
                    content_parts.append(choice.delta.content)
                if choice.finish_reason:
                    finish_reasons.append(choice.finish_reason)
        return content_parts, finish_reasons

    content_parts, finish_reasons = asyncio.run(_run())
    assert "streamed chunk" in "".join(content_parts)
    assert "stop" in finish_reasons


def test_chat_completion_streaming_zero_results(tmp_path, monkeypatch):
    """Zero search results: stream still yields a valid (empty) assistant message + stop."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])
    pr = _make_pipeline_result([])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        finish_reasons: list[str | None] = []
        response = await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "anything"}],
            stream=True,
        )
        async for chunk in response:
            for choice in chunk.choices:
                if choice.finish_reason:
                    finish_reasons.append(choice.finish_reason)
        return finish_reasons

    finish_reasons = asyncio.run(_run())
    assert "stop" in finish_reasons


# ---------------------------------------------------------------------------
# Tests — error paths via SDK
# ---------------------------------------------------------------------------


def test_auth_failure_raises_authentication_error(tmp_path, monkeypatch):
    """Wrong API key → openai.AuthenticationError (not a raw HTTP exception)."""
    import openai

    app = _make_stub_app(tmp_path, monkeypatch)

    async def _run():
        client = await _openai_client(app, api_key="wrong-key")
        return await client.models.list()

    with pytest.raises(openai.AuthenticationError):
        asyncio.run(_run())


def test_unknown_model_raises_not_found_error(tmp_path, monkeypatch):
    """Non-archon model name → openai.NotFoundError."""
    import openai

    app = _make_stub_app(tmp_path, monkeypatch)

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
        )

    with pytest.raises(openai.NotFoundError):
        asyncio.run(_run())


def test_no_user_message_raises_unprocessable_error(tmp_path, monkeypatch):
    """Messages with no user role → 422 surfaced as openai.UnprocessableEntityError."""
    import openai

    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "system", "content": "you are a bot"}],
        )

    with pytest.raises(openai.UnprocessableEntityError):
        asyncio.run(_run())


def test_whitespace_user_message_raises_bad_request_error(tmp_path, monkeypatch):
    """Whitespace-only user message → 400 surfaced as openai.BadRequestError."""
    import openai

    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "   "}],
        )

    with pytest.raises(openai.BadRequestError) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.code == "no_user_message"


# ---------------------------------------------------------------------------
# Real user scenarios
# ---------------------------------------------------------------------------


def test_multi_turn_conversation_uses_last_user_message(tmp_path, monkeypatch):
    """Cursor sends full history; shim must extract the LAST user message as the query.

    Conversation: system → user (Q1) → assistant (A1) → user (Q2).
    The pipeline must be called with Q2, not Q1.
    """
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"])
    pr = _make_pipeline_result([_make_search_result("context for Q2", "doc.txt")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is archon?"},
        {"role": "assistant", "content": "Archon is a retrieval server."},
        {"role": "user", "content": "How does the router work?"},
    ]

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=messages,
            stream=False,
        )

    asyncio.run(_run())

    query_used = app.state.pipeline.search.call_args.args[0]
    assert query_used == "How does the router work?"


def test_citation_format_in_response_content(tmp_path, monkeypatch):
    """With inject_citations=True (default) the response content contains the citation block."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"], inject_citations=True)
    pr = _make_pipeline_result([_make_search_result("vector store internals", "architecture.md")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "how does the store work?"}],
            stream=False,
        )

    completion = asyncio.run(_run())
    content = completion.choices[0].message.content
    assert "vector store internals" in content
    assert "[Source: architecture.md]" in content


def test_streaming_citation_appears_in_assembled_chunks(tmp_path, monkeypatch):
    """Streaming + inject_citations=True: [Source: ...] appears in assembled SSE content."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"], inject_citations=True)
    pr = _make_pipeline_result([_make_search_result("vector internals", "arch.md")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        parts: list[str] = []
        response = await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "how does it work?"}],
            stream=True,
        )
        async for chunk in response:
            for choice in chunk.choices:
                if choice.delta.content:
                    parts.append(choice.delta.content)
        return "".join(parts)

    assembled = asyncio.run(_run())
    assert "vector internals" in assembled
    assert "[Source: arch.md]" in assembled


def test_no_citations_when_disabled(tmp_path, monkeypatch):
    """With inject_citations=False the response contains only raw chunk text, no [Source:] line."""
    app = _make_stub_app(tmp_path, monkeypatch, collections=["docs"], inject_citations=False)
    pr = _make_pipeline_result([_make_search_result("raw chunk text", "private.md")])
    app.state.pipeline.get_collection_meta = AsyncMock(return_value=_make_collection_meta("docs"))
    app.state.pipeline.search = AsyncMock(return_value=pr)

    async def _run():
        client = await _openai_client(app)
        return await client.chat.completions.create(
            model="archon-search/docs",
            messages=[{"role": "user", "content": "tell me something"}],
            stream=False,
        )

    completion = asyncio.run(_run())
    content = completion.choices[0].message.content
    assert "raw chunk text" in content
    assert "[Source:" not in content

"""Unit tests for CommunityBuilder — E1b BE-3b (MMR + LLM summary)."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.graph_types import EntityType, GraphNode
from archon_search.llm_client.budget import exact_budget
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
)


def make_node(node_id: str, source_doc_id: str = "doc-1") -> GraphNode:
    return GraphNode(
        id=node_id,
        entity_name=node_id,
        entity_type=EntityType.concept,
        source_doc_id=source_doc_id,
        collection_name="test",
    )


def make_chunk(chunk_id: str, vector: list[float] | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": "doc-1",
        "text": f"text of {chunk_id}",
        "vector": vector or [1.0, 0.0, 0.0],
    }


def make_graph_store(nodes, edges):
    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=nodes)
    store.get_all_edges = AsyncMock(return_value=edges)
    store.write_communities = AsyncMock(return_value=None)
    return store


def make_search_store(chunks_by_doc: dict[str, list[dict]]):
    """Return a mock SearchStore where get_chunks_for_doc returns chunks_by_doc[doc_id]."""
    store = MagicMock()

    async def _get_chunks_for_doc(collection, doc_id):
        return chunks_by_doc.get(doc_id, [])

    store.get_chunks_for_doc = _get_chunks_for_doc
    return store


@pytest.mark.asyncio
async def test_mmr_selects_diverse_representatives():
    """MMR output has K items, each selected for diversity; no duplicate chunk IDs."""
    from archon_search.community_builder import _mmr_select

    # Create 5 chunks with diverse vectors
    chunks = [
        make_chunk("c1", [1.0, 0.0, 0.0]),
        make_chunk("c2", [0.0, 1.0, 0.0]),
        make_chunk("c3", [0.0, 0.0, 1.0]),
        make_chunk("c4", [0.7, 0.7, 0.0]),
        make_chunk("c5", [0.0, 0.7, 0.7]),
    ]

    result = _mmr_select(chunks, k=3)

    assert len(result) == 3
    assert len(set(result)) == 3, "No duplicate chunk IDs"
    assert all(cid in {"c1", "c2", "c3", "c4", "c5"} for cid in result)


class _GraphClient:
    """A shared-client stand-in for the graph feature: answers ``reply`` (or raises ``failure``) and records each request.

    ``budget`` is what ``input_budget`` returns; ``budget_templates`` records the template text it was asked about.
    Like the ``ollama`` and ``llama_cpp`` clients it counts tokens itself (``count_tokens``, one per word, so a test
    knows every size exactly).
    """

    def __init__(
        self,
        reply: str = "a summary",
        failure: Exception | None = None,
        budget: InputBudget | None = None,
    ) -> None:
        self.reply = reply
        self.failure = failure
        self.budget = budget if budget is not None else InputBudget(UNBOUNDED)
        self.requests: list[LlmCallRequest] = []
        self.budget_templates: list[str] = []

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return LlmCallResult(text=self.reply)

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(self, template_text: str) -> InputBudget:
        self.budget_templates.append(template_text)
        return self.budget

    async def count_tokens(self, text: str) -> int:
        return len(text.split())


class _NonCountingClient(_GraphClient):
    """A client with a bounded budget that cannot count tokens (it breaks the sizing contract)."""

    count_tokens = None  # type: ignore[assignment]


def _two_communities_setup(client: _GraphClient):
    from archon_search.community_builder import CommunityBuilder

    nodes = [make_node("a", "doc-1"), make_node("b", "doc-2"), make_node("c", "doc-3"), make_node("d", "doc-4")]
    graph_store = make_graph_store(nodes, [])
    search_store = make_search_store({
        "doc-1": [make_chunk("c1", [1.0, 0.0, 0.0])],
        "doc-2": [make_chunk("c2", [0.0, 1.0, 0.0])],
        "doc-3": [make_chunk("c3", [0.0, 0.0, 1.0])],
        "doc-4": [make_chunk("c4", [0.7, 0.7, 0.0])],
    })
    config = GraphConfig(
        enabled=True,
        provider="anthropic",
        extraction_model="claude-3-5-sonnet-20241022",
        community_summary_chunks=1,
    )
    builder = CommunityBuilder(graph_store, config, search_store=search_store, graph_client=client)
    return builder, graph_store


def _build_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The WARNINGs the community builder itself logged."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "archon_search.community_builder"
    ]


@pytest.mark.asyncio
async def test_graph_failure_keeps_no_summary_and_one_warning(caplog):
    """S4: the shared client raising for every community leaves each community with its MMR
    representatives and no summary, the build completes, and ONE WARNING is logged per process
    (not one per community, and not again on a later build)."""
    client = _GraphClient(failure=LlmCallFailure("anthropic", "boom"))
    builder, graph_store = _two_communities_setup(client)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[["a", "b"], ["c", "d"]],
    ), caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")
        again = await builder.build("test-col", ns="default")

    assert len(communities) == 2
    for comm in communities + again:
        assert comm.representative_chunk_ids != [], f"Community {comm.community_id} has no representative chunks"
        assert comm.summary_text is None, "the client raised, so summary_text must be None"
    assert len(client.requests) == 4, "every community still attempts its summary"
    warnings = _build_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "anthropic: boom" in warnings[0]
    assert "text of c" not in caplog.text, "no chunk text in the log"
    assert graph_store.write_communities.await_count == 2
    assert len(graph_store.write_communities.call_args[0][1]) == 2


@pytest.mark.asyncio
async def test_unexpected_summary_error_keeps_no_summary_and_logs_only_its_type(caplog):
    """Anything else that goes wrong while summarising (here a counter failing on a text) is the same fallback;
    the log names the exception type, never its text."""

    class _BrokenCountingClient(_GraphClient):
        async def count_tokens(self, text: str) -> int:
            raise RuntimeError("secret text of c1")

    client = _BrokenCountingClient(budget=InputBudget(1000))
    builder, _ = _two_communities_setup(client)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"]]
    ), caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    assert communities[0].summary_text is None
    assert client.requests == []
    assert "RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


@pytest.mark.asyncio
async def test_a_bounded_client_that_cannot_count_tokens_keeps_no_summary_with_one_warning(caplog):
    """A client with a budget must count, or its passages cannot be sized: the community keeps no summary and the
    WARNING says why, without a call."""
    client = _NonCountingClient(budget=InputBudget(1000))
    builder, _ = _two_communities_setup(client)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"], ["c", "d"]]
    ), caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    assert [c.summary_text for c in communities] == [None, None]
    assert client.requests == []
    (warning,) = _build_warnings(caplog)
    assert "cannot count tokens" in warning


@pytest.mark.asyncio
async def test_community_builder_sends_the_template_prompt_when_client_injected():
    """The gate is open (provider + extraction_model + client): one call per community carrying the moved-in
    template, the entity names and the representative chunk text, with the configured timeout and answer ceiling."""
    client = _GraphClient(reply="  a summary \n")
    builder, _ = _two_communities_setup(client)
    builder._config.extraction_timeout_seconds = 12.5
    builder._config.extraction_token_budget = 321

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[["a", "b"]],
    ):
        communities = await builder.build("test-col", ns="default")

    assert len(communities) == 1
    assert communities[0].summary_text == "a summary"
    (request,) = client.requests
    assert request.timeout_seconds == 12.5
    assert request.max_output_tokens == 321
    assert "knowledge-graph summariser" in request.prompt
    assert "Entities: a, b" in request.prompt
    assert "text of c" in request.prompt
    # Sizing asked for the budget with the template the prompt is rendered from.
    (template,) = client.budget_templates
    assert "{entity_names}" in template and "{chunk_texts}" in template


@pytest.mark.asyncio
async def test_empty_answer_leaves_no_summary():
    client = _GraphClient(reply="   ")
    builder, _ = _two_communities_setup(client)

    with patch("archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"]]):
        communities = await builder.build("test-col", ns="default")

    assert communities[0].summary_text is None
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_community_builder_skips_enrichment_when_client_none():
    """graph_client=None (default) -> no call is attempted, even when provider and
    extraction_model are both set. summary_text stays None; build() still succeeds."""
    from archon_search.community_builder import CommunityBuilder

    node_a = make_node("a", source_doc_id="doc-1")
    node_b = make_node("b", source_doc_id="doc-2")
    nodes = [node_a, node_b]

    graph_store = make_graph_store(nodes, [])
    search_store = make_search_store({
        "doc-1": [make_chunk("c1", [1.0, 0.0, 0.0])],
        "doc-2": [make_chunk("c2", [0.0, 1.0, 0.0])],
    })

    config = GraphConfig(
        enabled=True,
        provider="anthropic",
        extraction_model="claude-3-5-sonnet-20241022",
        community_summary_chunks=1,
    )
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[["a", "b"]],
    ):
        communities = await builder.build("test-col", ns="default")

    assert len(communities) == 1
    assert communities[0].summary_text is None


_WINDOW_TOKENS = 1500
_WORDS_PER_CHUNK = 150


def _word_chunk(chunk_id: str, vector: list[float]) -> dict:
    return {"chunk_id": chunk_id, "doc_id": "doc-1", "text": f"{chunk_id} " + "word " * _WORDS_PER_CHUNK, "vector": vector}


def _two_chunk_builder(client: _GraphClient, store_chunks: dict[str, list[dict]], *, summary_chunks: int, **config: object):
    """A builder over nodes ``a`` and ``b`` whose docs hold *store_chunks* (keyed by doc id)."""
    from archon_search.community_builder import CommunityBuilder

    nodes = [make_node("a", "doc-1"), make_node("b", "doc-2")]
    settings = GraphConfig(
        enabled=True, provider="ollama", extraction_model="m", community_summary_chunks=summary_chunks, **config
    )
    return CommunityBuilder(
        make_graph_store(nodes, []), settings, search_store=make_search_store(store_chunks), graph_client=client
    )


@pytest.mark.asyncio
async def test_community_chunks_sized_to_budget():
    """S4/S9-S11: the chunks sent fit the window minus the answer reserve minus the template; passages from the first
    one that does not fit on are left out, and the template is the one the budget was asked about."""
    from archon_search.community_builder import CommunityBuilder

    class _WindowedClient(_GraphClient):
        async def input_budget(self, template_text: str) -> InputBudget:
            self.budget_templates.append(template_text)
            return await exact_budget(
                ContextWindowTokens(_WINDOW_TOKENS, ContextWindowSource.CONFIGURED), template_text, self.count_tokens
            )

    client = _WindowedClient()
    nodes = [make_node(name, f"doc-{name}") for name in "abcde"]
    vectors = {
        "a": [1.0, 0.0, 0.0, 0.0, 0.0],
        "b": [0.0, 1.0, 0.0, 0.0, 0.0],
        "c": [0.0, 0.0, 1.0, 0.0, 0.0],
        "d": [0.0, 0.0, 0.0, 1.0, 0.0],
        "e": [0.0, 0.0, 0.0, 0.0, 1.0],
    }
    search_store = make_search_store({f"doc-{name}": [_word_chunk(f"chunk-{name}", vectors[name])] for name in "abcde"})
    config = GraphConfig(enabled=True, provider="ollama", extraction_model="m", community_summary_chunks=5)
    builder = CommunityBuilder(make_graph_store(nodes, []), config, search_store=search_store, graph_client=client)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync", return_value=[list("abcde")]
    ):
        await builder.build("test-col", ns="default")

    (request,) = client.requests
    sent_chunks = [name for name in "abcde" if f"chunk-{name} " in request.prompt]
    # The budget is 1500 - 1024 - the template's words (about 50): room for the five entity names and two 151-word passages.
    assert len(sent_chunks) == 2, sent_chunks
    assert await client.count_tokens(request.prompt) <= _WINDOW_TOKENS - ANSWER_RESERVE_TOKENS


@pytest.mark.asyncio
async def test_no_chunk_fits_still_sends_the_entity_names_with_one_warning(caplog):
    """A budget too small for any passage (but not for the entity names) sends the prompt without passages, and says so
    once: a summary from entity names alone may be a guess."""
    client = _GraphClient(budget=InputBudget(_WORDS_PER_CHUNK - 1))
    builder = _two_chunk_builder(
        client,
        {"doc-1": [_word_chunk("chunk-a", [1.0, 0.0])], "doc-2": [_word_chunk("chunk-b", [0.0, 1.0])]},
        summary_chunks=2,
    )

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"]]
    ), caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    (request,) = client.requests
    assert "Entities: a, b" in request.prompt
    assert "chunk-" not in request.prompt
    assert communities[0].summary_text == "a summary"
    (warning,) = _build_warnings(caplog)
    assert "no representative passage fits" in warning
    assert "chunk-" not in caplog.text


@pytest.mark.asyncio
async def test_budget_too_small_for_the_entity_names_skips_the_call_with_one_warning(caplog):
    """Nothing can be sent when even the entity names exceed the budget: no call, no summary, one WARNING naming the
    budget (never a chunk text)."""
    client = _GraphClient(budget=InputBudget(0))
    builder, _ = _two_communities_setup(client)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"], ["c", "d"]]
    ), caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    assert client.requests == []
    assert [c.summary_text for c in communities] == [None, None]
    warnings = _build_warnings(caplog)
    assert len(warnings) == 1 and "budget" in warnings[0]
    assert "text of c" not in caplog.text


@pytest.mark.asyncio
async def test_a_budget_that_drops_passages_keeps_the_most_representative_first():
    """The passages go in MMR rank order, so a budget that holds one keeps the centroid-nearest chunk (here "chunk-z"),
    never whichever chunk the store happened to return first."""
    client = _GraphClient(budget=InputBudget(_WORDS_PER_CHUNK + 10))
    builder = _two_chunk_builder(
        client,
        {
            "doc-1": [_word_chunk("chunk-x", [1.0, 0.0]), _word_chunk("chunk-y", [0.0, 1.0])],
            "doc-2": [_word_chunk("chunk-z", [0.7, 0.7])],
        },
        summary_chunks=3,
    )

    with patch("archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"]]):
        await builder.build("test-col", ns="default")

    (request,) = client.requests
    assert "chunk-z " in request.prompt
    assert "chunk-x " not in request.prompt and "chunk-y " not in request.prompt


@pytest.mark.asyncio
async def test_an_answer_ceiling_above_the_reserve_comes_out_of_the_room_for_passages():
    """The budget holds ``ANSWER_RESERVE_TOKENS`` back for the answer. A cloud client (no budget) gets the configured
    ceiling and every passage; under a budget the ceiling is still the configured one, and what it takes beyond the
    reserve is taken off the passages' room, so prompt and answer together still fit the window."""
    extra = 100
    ceiling = ANSWER_RESERVE_TOKENS + extra
    room = _WORDS_PER_CHUNK + 10  # holds the entity names and one passage
    results: list[tuple[int, bool]] = []
    for budget in (InputBudget(UNBOUNDED), InputBudget(room), InputBudget(room + extra)):
        client = _GraphClient(budget=budget)
        builder = _two_chunk_builder(
            client, {"doc-1": [_word_chunk("chunk-a", [1.0, 0.0])]}, summary_chunks=1, extraction_token_budget=ceiling
        )
        with patch("archon_search.community_builder._run_leiden_partition_sync", return_value=[["a", "b"]]):
            await builder.build("test-col", ns="default")
        (request,) = client.requests
        results.append((request.max_output_tokens, "chunk-a " in request.prompt))

    # No budget: the passage goes. A budget of `room` less the extra 100 holds no passage; `room + extra` less it holds one.
    assert results == [(ceiling, True), (ceiling, False), (ceiling, True)]


# ---------------------------------------------------------------------------
# _cosine_similarity direct tests
# ---------------------------------------------------------------------------


def test_cosine_similarity_both_zero_vectors():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_one_zero_vector():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_orthogonal():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_parallel():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)


def test_cosine_similarity_antiparallel():
    from archon_search.community_builder import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# _mmr_select edge cases
# ---------------------------------------------------------------------------


def test_mmr_select_empty_input():
    from archon_search.community_builder import _mmr_select
    assert _mmr_select([], k=3) == []


def test_mmr_select_no_valid_vectors():
    from archon_search.community_builder import _mmr_select
    chunks = [{"chunk_id": "c1", "vector": None}, {"chunk_id": "c2", "vector": []}]
    assert _mmr_select(chunks, k=2) == []


def test_mmr_select_k_zero():
    from archon_search.community_builder import _mmr_select
    chunks = [make_chunk("c1"), make_chunk("c2")]
    assert _mmr_select(chunks, k=0) == []


def test_mmr_select_k_larger_than_candidates():
    from archon_search.community_builder import _mmr_select
    chunks = [make_chunk("c1", [1.0, 0.0]), make_chunk("c2", [0.0, 1.0])]
    result = _mmr_select(chunks, k=10)
    assert len(result) == 2
    assert set(result) == {"c1", "c2"}


def test_mmr_select_k_one():
    from archon_search.community_builder import _mmr_select
    chunks = [
        make_chunk("c1", [1.0, 0.0]),
        make_chunk("c2", [0.0, 1.0]),
        make_chunk("c3", [0.5, 0.5]),
    ]
    result = _mmr_select(chunks, k=1)
    assert len(result) == 1
    assert result[0] in {"c1", "c2", "c3"}


def test_mmr_selects_diverse_subset_from_mixed_input():
    """MMR selects k diverse items; result has no duplicates and comes from the candidate set."""
    from archon_search.community_builder import _mmr_select

    chunks = [
        make_chunk("c1", [1.0, 0.0, 0.0]),
        make_chunk("c2", [0.0, 1.0, 0.0]),
        make_chunk("c3", [0.0, 0.0, 1.0]),
        make_chunk("c4", [0.7, 0.7, 0.0]),  # between c1 and c2
        make_chunk("c5", [0.0, 0.7, 0.7]),  # between c2 and c3
    ]
    result = _mmr_select(chunks, k=3)
    # Must return exactly k items with no duplicates, all from the candidate set
    assert len(result) == 3
    assert len(set(result)) == 3, "No duplicate chunk IDs"
    all_ids = {"c1", "c2", "c3", "c4", "c5"}
    assert all(cid in all_ids for cid in result)


# ---------------------------------------------------------------------------
# Single-node short-circuit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_single_node_uses_mmr_and_writes(caplog):
    """Single-node graph builds one community via short-circuit path; MMR runs; write_communities called."""
    from archon_search.community_builder import CommunityBuilder

    node_a = make_node("a", source_doc_id="doc-1")
    graph_store = make_graph_store([node_a], [])
    search_store = make_search_store({
        "doc-1": [make_chunk("c1", [1.0, 0.0])],
    })
    config = GraphConfig(enabled=True, community_summary_chunks=1)
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    assert len(communities) == 1
    assert communities[0].entity_ids == ["a"]
    assert communities[0].representative_chunk_ids == ["c1"]
    graph_store.write_communities.assert_awaited_once()
    # Warning about single node should be logged
    assert any(record.levelno >= logging.WARNING for record in caplog.records)

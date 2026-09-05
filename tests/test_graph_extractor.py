"""Unit and integration tests for GraphExtractor — E1a BE-4, rewired onto the
prose extraction backend by BE-11.

Tests cover:
- Entity type is the prose engine's own label, used verbatim (no mapping table)
- C3 code-symbol path (symbol_type present → code_symbol entity; the prose
  engine is NOT called for code chunks)
- Directed typed relations from the prose engine are persisted additively
  alongside co-occurrence edges, preserving head/tail direction (no sorted());
  the engine's own `related_to` relation is excluded (co-occurrence already
  covers it); self-loops, dangling, and off-vocabulary entity/relation labels
  are skipped without raising
- Both configured thresholds (ner_confidence, relation_confidence) reach
  ``inference()`` unswapped
- A relations-incapable (but loadable) backend does not degrade the ingest
- LLM relationship labeling AND-gate (LLCP BE-7): gate closed → silent skip;
  gate open → per-chunk label_relationships call, additive typed edges; call
  failure → per-chunk fallback to whatever edges already exist + WARNING
- Backend load/inference failure degrades (never fatal_error)
- stable entity IDs match make_stable_entity_id formula
- Co-occurrence edge count: 3 entities in one chunk → exactly 3 edges (N*(N-1)/2)
- Code-symbol name fallback: containing_function > containing_class > source_path basename
- Same-process double-ingest is id-stable (S28's plumbing half)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.graph_types import (
    ChunkInput,
    EntityType,
    GraphMention,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)
from archon_search.prose_extraction_backend import (
    BatchExtraction,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)


# ---------------------------------------------------------------------------
# Prose extraction backend stub helper
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Stand-in for ``ProseExtractionBackend`` at the ``GraphExtractor`` seam.

    ``chunks_by_text`` maps input text -> ``ChunkExtraction``; texts absent
    from the map yield an empty extraction (no entities, no relations).
    """

    def __init__(
        self,
        chunks_by_text: dict[str, ChunkExtraction] | None = None,
        *,
        default: ChunkExtraction | None = None,
        load_error: BaseException | None = None,
        infer_error: BaseException | None = None,
    ) -> None:
        self._chunks_by_text = chunks_by_text or {}
        self._default = default if default is not None else ChunkExtraction()
        self._load_error = load_error
        self._infer_error = infer_error
        self.load_calls = 0
        self.infer_calls: list[tuple[tuple[str, ...], float, float]] = []

    async def load(self) -> None:
        self.load_calls += 1
        if self._load_error is not None:
            raise self._load_error

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        self.infer_calls.append((tuple(texts), ner_confidence, relation_confidence))
        if self._infer_error is not None:
            raise self._infer_error
        chunks = [self._chunks_by_text.get(t, self._default) for t in texts]
        return BatchExtraction(chunks=chunks, truncated=False)


def _entity(text: str, label: str, start: int = 0, score: float = 0.9) -> ExtractedEntity:
    return ExtractedEntity(text=text, label=label, start=start, end=start + len(text), score=score)


def _relation(head: str, tail: str, label: str, score: float = 0.9) -> ExtractedRelation:
    return ExtractedRelation(head=head, tail=tail, label=label, score=score)


def _make_extractor(**config_kwargs):
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig(**config_kwargs))
    return extractor


# ---------------------------------------------------------------------------
# 1. Entity type is the engine's own label, used verbatim (S2)
# ---------------------------------------------------------------------------


def test_entity_type_is_the_engine_label_verbatim() -> None:
    """Each node's entity_type equals the engine's own label directly — no
    intermediate spaCy-style mapping table."""
    extractor = _make_extractor()

    text_map = {
        "t1": ChunkExtraction(entities=[_entity("Alice", EntityType.person.value)]),
        "t2": ChunkExtraction(entities=[_entity("Widget", EntityType.concept.value)]),
        "t3": ChunkExtraction(entities=[_entity("Acme", EntityType.system.value)]),
        "t4": ChunkExtraction(entities=[_entity("Olympics", EntityType.event.value)]),
    }
    extractor._backend = _FakeBackend(text_map)

    chunks = [
        ChunkInput(chunk_id=f"c{i}", text=t, symbol_type=None, symbol_subtype=None)
        for i, t in enumerate(text_map.keys())
    ]

    async def _run():
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())
    entity_types = {n.entity_name: n.entity_type for n in result.nodes}

    assert entity_types["Alice"] == EntityType.person
    assert entity_types["Widget"] == EntityType.concept
    assert entity_types["Acme"] == EntityType.system
    assert entity_types["Olympics"] == EntityType.event
    assert not hasattr(
        __import__("archon_search.graph_extractor", fromlist=["_LABEL_TO_ENTITY_TYPE"]),
        "_LABEL_TO_ENTITY_TYPE",
    ), "_LABEL_TO_ENTITY_TYPE must be deleted (BE-11)"


# ---------------------------------------------------------------------------
# 2. C3 code-symbol path — the prose engine is never called
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_from_c3() -> None:
    """Chunk with symbol_type='class' → code_symbol entity; the prose engine is not called."""
    extractor = _make_extractor()
    backend = _FakeBackend()
    extractor._backend = backend

    code_chunk = ChunkInput(
        chunk_id="c1",
        text="class MyService: ...",
        symbol_type="class",
        symbol_subtype="python-class",
        containing_class="MyService",
    )

    async def _run():
        return await extractor.extract([code_chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert backend.load_calls == 0, "the prose engine must not even be loaded for code chunks"
    assert backend.infer_calls == [], "the prose engine must not be called for code chunks"

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.entity_type == EntityType.code_symbol
    assert node.entity_name == "MyService"
    assert node.entity_subtype == "python-class"
    assert node.id == make_stable_entity_id(EntityType.code_symbol.value, "MyService")


# ---------------------------------------------------------------------------
# 3. LLM stub warning (LLCP BE-7 AND-gate, unrelated to the prose engine)
# ---------------------------------------------------------------------------


def test_extractor_extraction_model_without_provider_skips_enrichment_silently() -> None:
    """extraction_model set but provider=None (incomplete AND-gate) -> no
    enrichment call attempted, no warning."""
    extractor = _make_extractor(extraction_model="gpt-4")
    extractor._backend = _FakeBackend()

    chunk = ChunkInput(chunk_id="c1", text="Hello world.", symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.warnings == []
    assert result.fatal_error is None


def test_graph_extractor_calls_label_relationships_per_chunk() -> None:
    """AND-gate open (provider + extraction_model + client all set) -> label_relationships
    is called once per text chunk with 2+ entities; the LLM-typed edge is persisted
    additively alongside the related_to co-occurrence edge."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    text = "Alice uses Acme."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Alice", target_entity="Acme", relationship_type="uses"
            )
        ]
    )
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    mock_client.label_relationships.assert_awaited_once()
    called_chunk_text = mock_client.label_relationships.await_args.args[1]
    assert called_chunk_text == text

    relationship_types = {e.relationship_type for e in result.edges}
    assert RelationshipType.uses in relationship_types
    assert RelationshipType.related_to in relationship_types, (
        "LLM-typed edges must be additive, not replace the co-occurrence edge"
    )
    assert len(result.edges) == 2


def test_resolve_labeled_pair_recovers_merged_source_name() -> None:
    """Small local models occasionally merge both entity names into one field
    (e.g. ``source_entity="Bob / Google"``, ``target_entity="Google"``) instead
    of keeping them separate. When one side resolves directly and the other
    splits into exactly two known names including the resolved side, recover
    the missing side as the other split part; otherwise return (None, None)."""
    from archon_search.graph_extractor import _resolve_labeled_pair

    name_to_id = {"Alice": "id_alice", "Bob": "id_bob", "Google": "id_google"}

    assert _resolve_labeled_pair("Alice", "Bob", name_to_id) == ("id_alice", "id_bob")
    assert _resolve_labeled_pair("Bob / Google", "Google", name_to_id) == ("id_bob", "id_google")
    assert _resolve_labeled_pair("Bob / Alice", "Alice", name_to_id) == ("id_bob", "id_alice")
    assert _resolve_labeled_pair("Alice, Google", "Google", name_to_id) == ("id_alice", "id_google")
    assert _resolve_labeled_pair("Alice", "Bob and Google", name_to_id) == (None, None), (
        "ambiguous recovery (neither split part matches the resolved side) must not guess"
    )
    assert _resolve_labeled_pair("Charlie", "Bob", name_to_id) == (None, None)
    assert _resolve_labeled_pair("Alice / Bob / Google", "Google", name_to_id) == (None, None), (
        "three-way merge is not a recoverable two-name pattern"
    )


def test_graph_extractor_recovers_garbled_relationship_entity_name() -> None:
    """When the LLM merges both names into ``source_entity`` (a real failure mode
    observed with small local models), GraphExtractor still resolves and persists
    the typed edge instead of discarding it."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    text = "Alice and Bob both work at Google."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Bob / Google", target_entity="Google", relationship_type="uses"
            )
        ]
    )
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Bob", EntityType.person.value),
                    _entity("Google", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    relationship_types = {e.relationship_type for e in result.edges}
    assert RelationshipType.uses in relationship_types, (
        "the garbled-but-recoverable relationship must still produce a typed edge"
    )


def test_graph_extractor_catches_enrichment_error() -> None:
    """label_relationships raising -> per-chunk WARNING + fallback for that
    chunk; extract() does not raise and the co-occurrence edge is still produced."""
    text = "Alice uses Acme."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(side_effect=RuntimeError("boom"))
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert len(result.edges) == 1
    assert result.edges[0].relationship_type == RelationshipType.related_to
    assert any("boom" in w or "chunk" in w.lower() for w in result.warnings)


def test_llm_related_to_relation_does_not_double_the_cooccurrence_edge() -> None:
    """C2-I-1: the same related_to-doubling guard applied to the engine's own
    relations (C1-I-2) must also apply to LLM-labeled relations -- an LLM
    echoing `related_to` for a pair whose co-occurrence edge is already
    sorted()-normalised must not produce a second, doubled edge."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    alice_id = make_stable_entity_id(EntityType.person.value, "Alice")
    bob_id = make_stable_entity_id(EntityType.person.value, "Bob")
    assert bob_id < alice_id, (
        "test precondition: Bob's stable ID must sort before Alice's for the "
        "reverse-direction relation below to actually exercise the guard"
    )

    text = "Bob and Alice."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Alice", target_entity="Bob", relationship_type="related_to"
            )
        ]
    )
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Bob", EntityType.person.value),
                    _entity("Alice", EntityType.person.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    mock_client.label_relationships.assert_awaited_once()

    related = [e for e in result.edges if e.relationship_type == RelationshipType.related_to]
    assert len(related) == 1, "only the normalised co-occurrence edge may exist, no duplicate"
    assert related[0].source_node_id < related[0].target_node_id


def test_llm_self_loop_relation_is_skipped() -> None:
    """C2-I-2: an LLM echoing the same entity name for both source and target
    is a self-loop and carries no graph signal; it must be skipped, not
    persisted as an edge. Includes an unrelated second pair so a non-empty
    baseline edge exists (distinguishing "actively skipped" from "loop never
    ran")."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    text = "Alice talks to herself while Carol watches."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Alice", target_entity="Alice", relationship_type="uses"
            )
        ]
    )
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Carol", EntityType.person.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    mock_client.label_relationships.assert_awaited_once()

    assert len(result.edges) == 1, (
        "the unrelated Alice/Carol co-occurrence edge must exist -- proves the "
        "LLM relation loop ran and the self-loop was actively skipped"
    )
    assert result.edges[0].relationship_type == RelationshipType.related_to


def test_llm_unknown_relationship_type_is_skipped_without_raising() -> None:
    """C2-B-5: an off-vocabulary relationship_type from the LLM must be
    skipped -- one bad label must not discard every other LLM-labeled
    relation for the chunk (mirrors the engine-path guard, C1-B-2/C1-CC-1)."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    text = "Alice uses Acme."
    extractor = _make_extractor(provider="llama_cpp", extraction_model="model-x")
    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Alice", target_entity="Acme", relationship_type="not_a_real_label"
            )
        ]
    )
    extractor._enrichment_client = mock_client
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    mock_client.label_relationships.assert_awaited_once()

    assert result.fatal_error is None
    assert len(result.edges) == 1
    assert result.edges[0].relationship_type == RelationshipType.related_to


# ---------------------------------------------------------------------------
# 4. Stable entity IDs match make_stable_entity_id formula
# ---------------------------------------------------------------------------


def test_extractor_stable_ids_match_formula() -> None:
    """Entity IDs in extraction result match make_stable_entity_id() formula."""
    extractor = _make_extractor()
    text = "Alice works at Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())
    node_map = {n.entity_name: n.id for n in result.nodes}

    assert node_map["Alice"] == make_stable_entity_id(EntityType.person.value, "Alice")
    assert node_map["Acme"] == make_stable_entity_id(EntityType.system.value, "Acme")


# ---------------------------------------------------------------------------
# 5. Integration: stub backend returns fixed entities → nodes/edges populated
# ---------------------------------------------------------------------------


def test_extractor_extract_from_real_chunks() -> None:
    """Stub backend returns fixed entities; assert nodes and edges populated correctly."""
    extractor = _make_extractor()
    text = "Alice works with Bob at Acme Corp."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Bob", EntityType.person.value),
                    _entity("Acme Corp", EntityType.system.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    names = {n.entity_name for n in result.nodes}
    assert names == {"Alice", "Bob", "Acme Corp"}

    # 3 entities in one chunk → 3 co-occurrence edges (N*(N-1)/2 = 3)
    assert len(result.edges) == 3
    for edge in result.edges:
        assert edge.relationship_type == RelationshipType.related_to


# ---------------------------------------------------------------------------
# 6. Co-occurrence edge count: N*(N-1)/2
# ---------------------------------------------------------------------------


def test_extractor_cooccurrence_edge_count() -> None:
    """Chunk with 3 entities A, B, C → exactly 3 edges, not 6 (no directed doubling)."""
    extractor = _make_extractor()
    text = "A, B, and C."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("A", EntityType.person.value),
                    _entity("B", EntityType.person.value),
                    _entity("C", EntityType.person.value),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 3
    assert len(result.edges) == 3
    for edge in result.edges:
        assert edge.source_node_id < edge.target_node_id


# ---------------------------------------------------------------------------
# 7. Code-symbol name fallback (no prose engine involvement)
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_name_fallback() -> None:
    """Three code chunks: containing_function → containing_class → source_path basename."""
    extractor = _make_extractor()

    chunk_fn = ChunkInput(
        chunk_id="c1",
        text="def process(): ...",
        symbol_type="function",
        symbol_subtype="python-function",
        containing_function="process",
        containing_class="",
        source_path="/repo/handler.py",
    )
    chunk_cls = ChunkInput(
        chunk_id="c2",
        text="class Handler: ...",
        symbol_type="class",
        symbol_subtype="python-class",
        containing_function="",
        containing_class="Handler",
        source_path="/repo/handler.py",
    )
    chunk_fallback = ChunkInput(
        chunk_id="c3",
        text="# module-level code",
        symbol_type="module",
        symbol_subtype="python-module",
        containing_function="",
        containing_class="",
        source_path="/repo/utils.py",
    )

    async def _run():
        return await extractor.extract(
            [chunk_fn, chunk_cls, chunk_fallback], "doc-1", "col"
        )

    result = asyncio.run(_run())

    names = {n.entity_name for n in result.nodes}
    assert "process" in names, f"Expected 'process' from containing_function; got {names}"
    assert "Handler" in names, f"Expected 'Handler' from containing_class; got {names}"
    assert "utils" in names, f"Expected 'utils' (basename of utils.py); got {names}"


# ---------------------------------------------------------------------------
# 8. Empty chunks list → empty result, no error
# ---------------------------------------------------------------------------


def test_extractor_empty_chunks_list() -> None:
    """extract() with an empty list of chunks returns empty result with no error."""
    extractor = _make_extractor()
    backend = _FakeBackend()
    extractor._backend = backend

    async def _run():
        return await extractor.extract([], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert result.nodes == []
    assert result.edges == []
    assert result.warnings == []
    assert backend.load_calls == 0, "the backend must not load when there are no text chunks"


# ---------------------------------------------------------------------------
# 9. Duplicate entity in same chunk → single node, zero edges
# ---------------------------------------------------------------------------


def test_extractor_duplicate_entity_same_chunk() -> None:
    """The engine returning the same entity span twice in one chunk → 1 node, 0 edges."""
    extractor = _make_extractor()
    text = "Alice met Alice at Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value, start=0),
                    _entity("Alice", EntityType.person.value, start=10),
                ]
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 1, f"Expected 1 node (deduplicated), got {len(result.nodes)}"
    assert result.nodes[0].entity_name == "Alice"
    assert len(result.edges) == 0


# ---------------------------------------------------------------------------
# 10. Same entity in two chunks → single node, edges from each chunk
# ---------------------------------------------------------------------------


def test_extractor_entity_across_multiple_chunks() -> None:
    """Same entity in two chunks with different co-occurring entities → one node, edges from both."""
    extractor = _make_extractor()
    text1 = "Alice works with Bob."
    text2 = "Alice also knows Carol."
    extractor._backend = _FakeBackend(
        {
            text1: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Bob", EntityType.person.value),
                ]
            ),
            text2: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Carol", EntityType.person.value),
                ]
            ),
        }
    )
    chunks = [
        ChunkInput(chunk_id="c1", text=text1, symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="c2", text=text2, symbol_type=None, symbol_subtype=None),
    ]

    async def _run():
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    names = {n.entity_name for n in result.nodes}
    assert names == {"Alice", "Bob", "Carol"}
    assert len(result.edges) == 2
    edge_ids = [e.id for e in result.edges]
    assert len(edge_ids) == len(set(edge_ids)), "Duplicate edge IDs found"


# ---------------------------------------------------------------------------
# 11. Mixed code + text chunks in a single extract() call
# ---------------------------------------------------------------------------


def test_extractor_mixed_code_and_text_chunks() -> None:
    """Mixed code chunks and text chunks in one call → both entity types produced."""
    extractor = _make_extractor()
    text = "Alice uses AuthService."
    extractor._backend = _FakeBackend(
        {text: ChunkExtraction(entities=[_entity("Alice", EntityType.person.value)])}
    )

    code_chunk = ChunkInput(
        chunk_id="c1",
        text="class AuthService: ...",
        symbol_type="class",
        symbol_subtype="python-class",
        containing_class="AuthService",
    )
    text_chunk = ChunkInput(chunk_id="c2", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([code_chunk, text_chunk], "doc-1", "col")

    result = asyncio.run(_run())

    entity_types = {n.entity_name: n.entity_type for n in result.nodes}
    assert "AuthService" in entity_types
    assert entity_types["AuthService"] == EntityType.code_symbol
    assert "Alice" in entity_types
    assert entity_types["Alice"] == EntityType.person

    # Each chunk has only 1 entity → no co-occurrence edges possible.
    assert len(result.edges) == 0


# ---------------------------------------------------------------------------
# BE-4: Mentions extraction (E2b entity incidence tracking)
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_mentions() -> None:
    """Code-symbol chunk produces GraphMention with correct chunk_id and entity_id."""
    extractor = _make_extractor()

    chunk_id = "doc-1-000000"
    doc_id = "doc-1"
    code_chunk = ChunkInput(
        chunk_id=chunk_id,
        text="class MyService: ...",
        symbol_type="class",
        symbol_subtype="python-class",
        containing_class="MyService",
    )

    async def _run():
        return await extractor.extract([code_chunk], doc_id, "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 1
    node = result.nodes[0]
    entity_id = node.id

    assert len(result.mentions) == 1, f"Expected 1 mention, got {len(result.mentions)}"
    mention = result.mentions[0]
    assert mention.entity_id == entity_id
    assert mention.chunk_id == chunk_id
    assert mention.doc_id == doc_id


def test_extractor_ner_mentions() -> None:
    """Mentions correctly pair entities with their chunk_ids via zip alignment.

    Three chunks: entities in chunks 0 and 2 (not 1). Assert mentions contain
    exactly two GraphMention objects referencing the correct chunk_ids.
    """
    extractor = _make_extractor()
    doc_id = "doc-1"
    chunks = [
        ChunkInput(chunk_id="doc-1-000000", text="Alice works here.", symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="doc-1-000001", text="No entities here.", symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="doc-1-000002", text="Bob is great.", symbol_type=None, symbol_subtype=None),
    ]

    extractor._backend = _FakeBackend(
        {
            "Alice works here.": ChunkExtraction(
                entities=[_entity("Alice", EntityType.person.value)]
            ),
            "No entities here.": ChunkExtraction(),
            "Bob is great.": ChunkExtraction(entities=[_entity("Bob", EntityType.person.value)]),
        }
    )

    async def _run():
        return await extractor.extract(chunks, doc_id, "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 2
    node_map = {n.entity_name: n.id for n in result.nodes}
    alice_id = node_map["Alice"]
    bob_id = node_map["Bob"]

    assert len(result.mentions) == 2

    alice_mentions = [m for m in result.mentions if m.entity_id == alice_id]
    assert len(alice_mentions) == 1
    assert alice_mentions[0].chunk_id == "doc-1-000000"
    assert alice_mentions[0].doc_id == doc_id

    bob_mentions = [m for m in result.mentions if m.entity_id == bob_id]
    assert len(bob_mentions) == 1
    assert bob_mentions[0].chunk_id == "doc-1-000002"
    assert bob_mentions[0].doc_id == doc_id

    chunk1_mentions = [m for m in result.mentions if m.chunk_id == "doc-1-000001"]
    assert len(chunk1_mentions) == 0


# ---------------------------------------------------------------------------
# E2g BE-2, Critical #2: code_symbol node identity is file-qualified
# ---------------------------------------------------------------------------


def test_sameNameDifferentFiles_produceDistinctNodes() -> None:
    """Two unrelated same-named code symbols in different files get distinct node IDs."""
    extractor = _make_extractor()

    chunk_a = ChunkInput(
        chunk_id="a-000000",
        text="def run(): ...",
        symbol_type="function",
        symbol_subtype="python-function",
        containing_function="run",
        source_path="/repo/a.py",
    )
    chunk_b = ChunkInput(
        chunk_id="b-000000",
        text="def run(): ...",
        symbol_type="function",
        symbol_subtype="python-function",
        containing_function="run",
        source_path="/repo/b.py",
    )

    async def _run():
        return await extractor.extract([chunk_a, chunk_b], "doc-1", "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 2
    ids = {n.id for n in result.nodes}
    assert len(ids) == 2

    for node in result.nodes:
        assert node.entity_name == "run"

    expected_id_a = make_stable_entity_id(EntityType.code_symbol.value, "run::/repo/a.py")
    expected_id_b = make_stable_entity_id(EntityType.code_symbol.value, "run::/repo/b.py")
    assert ids == {expected_id_a, expected_id_b}


# ---------------------------------------------------------------------------
# BE-11: backend load failure degrades, never fatal
# ---------------------------------------------------------------------------


def test_extractor_backend_load_failure_degrades_instead_of_fatal() -> None:
    """A missing/unloadable model artifact is an auxiliary failure — degrade,
    never fatal_error. Code-symbol extraction survives."""
    extractor = _make_extractor()
    extractor._backend = _FakeBackend(load_error=RuntimeError("model download failed"))

    chunks = [
        ChunkInput(
            chunk_id="c1",
            text="def parse_config(): ...",
            symbol_type="function",
            symbol_subtype="python-function",
            containing_function="parse_config",
            source_path="conf.py",
        ),
        ChunkInput(chunk_id="c2", text="Alice met Bob.", symbol_type=None, symbol_subtype=None),
    ]

    async def _run():
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert result.degraded is True
    assert result.warnings != []
    assert "parse_config" in {n.entity_name for n in result.nodes}


def test_extractor_backend_load_cancelled_propagates_and_is_not_a_warning() -> None:
    """CancelledError from the backend load must propagate, not degrade."""
    extractor = _make_extractor()
    extractor._backend = _FakeBackend(load_error=asyncio.CancelledError())

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())


def test_extractor_inference_failure_degrades_and_preserves_code_symbols() -> None:
    """A raising inference() call degrades — fatal_error stays None, the
    warning is present, code-symbol nodes/mentions survive, and no
    co-occurrence edges are fabricated from a partial/absent result."""
    extractor = _make_extractor()
    extractor._backend = _FakeBackend(infer_error=RuntimeError("boom: inference failed"))

    chunks = [
        ChunkInput(
            chunk_id="c1",
            text="def parse_config(): ...",
            symbol_type="function",
            symbol_subtype="python-function",
            containing_function="parse_config",
            source_path="conf.py",
        ),
        ChunkInput(chunk_id="c2", text="Alice met Bob.", symbol_type=None, symbol_subtype=None),
    ]

    async def _run():
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert result.degraded is True
    assert result.warnings != []
    assert "parse_config" in {n.entity_name for n in result.nodes}
    assert result.mentions and all(
        m.entity_id in {n.id for n in result.nodes} for m in result.mentions
    )
    assert result.edges == []


# ---------------------------------------------------------------------------
# BE-11 required tests
# ---------------------------------------------------------------------------


def test_typed_edge_keeps_head_to_tail_direction() -> None:
    """A directed relation from the prose engine is persisted head→tail with
    no lexicographic normalisation, while the co-occurrence edge for the same
    pair stays normalised (source_id < target_id)."""
    extractor = _make_extractor()
    text = "Bob uses Alice's library."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Bob", EntityType.person.value),
                    _entity("Alice", EntityType.person.value),
                ],
                relations=[_relation(head="Bob", tail="Alice", label=RelationshipType.uses.value)],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    bob_id = make_stable_entity_id(EntityType.person.value, "Bob")
    alice_id = make_stable_entity_id(EntityType.person.value, "Alice")

    typed = [e for e in result.edges if e.relationship_type == RelationshipType.uses]
    assert len(typed) == 1
    assert typed[0].source_node_id == bob_id
    assert typed[0].target_node_id == alice_id
    assert typed[0].id == make_stable_edge_id(bob_id, alice_id, RelationshipType.uses.value)

    cooccurrence = [e for e in result.edges if e.relationship_type == RelationshipType.related_to]
    assert len(cooccurrence) == 1
    assert cooccurrence[0].source_node_id < cooccurrence[0].target_node_id


def test_both_thresholds_reach_inference_unswapped() -> None:
    """GraphExtractor passes both configured thresholds through to
    inference() as (ner_confidence, relation_confidence) — not swapped, not
    hardcoded. This is a wiring assertion, not threshold arithmetic."""
    extractor = _make_extractor(ner_confidence=0.31, relation_confidence=0.92)
    backend = _FakeBackend({"hi": ChunkExtraction()})
    extractor._backend = backend
    chunk = ChunkInput(chunk_id="c1", text="hi", symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    asyncio.run(_run())

    assert len(backend.infer_calls) == 1
    _texts, ner_confidence, relation_confidence = backend.infer_calls[0]
    assert ner_confidence == 0.31
    assert relation_confidence == 0.92


def test_typed_and_cooccurrence_edges_coexist_with_distinct_ids() -> None:
    """S3/S4: with no enrichment provider configured, a chunk yielding both a
    typed relation and a co-occurrence pair over the same two entities
    persists both edges with distinct ids; neither overrides the other."""
    extractor = _make_extractor()  # provider=None, extraction_model=None
    text = "Alice uses Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ],
                relations=[
                    _relation(head="Alice", tail="Acme", label=RelationshipType.uses.value)
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    relationship_types = {e.relationship_type for e in result.edges}
    assert RelationshipType.uses in relationship_types
    assert RelationshipType.related_to in relationship_types
    assert len(result.edges) == 2
    edge_ids = {e.id for e in result.edges}
    assert len(edge_ids) == 2, "typed and co-occurrence edges must have distinct stable ids"


def test_code_symbol_chunks_never_reach_the_prose_engine() -> None:
    """C3-enriched code-symbol chunks never reach the prose engine — the AST
    path is unaffected, and the backend is never loaded or called."""
    extractor = _make_extractor()
    backend = _FakeBackend()
    extractor._backend = backend

    chunks = [
        ChunkInput(
            chunk_id="c1",
            text="class AuthService: ...",
            symbol_type="class",
            symbol_subtype="python-class",
            containing_class="AuthService",
        ),
        ChunkInput(
            chunk_id="c2",
            text="def login(): ...",
            symbol_type="function",
            symbol_subtype="python-function",
            containing_function="login",
        ),
    ]

    async def _run():
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    assert backend.load_calls == 0
    assert backend.infer_calls == []
    assert {n.entity_type for n in result.nodes} == {EntityType.code_symbol}


def test_relations_incapable_artifact_does_not_degrade_ingest() -> None:
    """S59: a loadable backend that returns entities and no relations (e.g. a
    relations-incapable checkpoint) does NOT degrade the ingest — entities
    persist, degraded is False, no typed edge is written, and no wire-facing
    warning is emitted."""
    extractor = _make_extractor()
    text = "Alice works with Bob."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Bob", EntityType.person.value),
                ],
                relations=[],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.degraded is False
    assert result.warnings == []
    names = {n.entity_name for n in result.nodes}
    assert names == {"Alice", "Bob"}
    assert all(e.relationship_type == RelationshipType.related_to for e in result.edges), (
        "no typed edge may be fabricated when the engine returns no relations"
    )


def test_engine_related_to_relation_does_not_double_the_cooccurrence_edge() -> None:
    """C1-I-2: the engine's own `related_to` relation is itself a prompted,
    valid relation label -- not just the co-occurrence fallback. A reverse-
    direction `related_to` relation for a pair whose co-occurrence edge is
    already sorted()-normalised must not produce a second, doubled edge."""
    # Precondition this test relies on (C2-T-01): the co-occurrence loop
    # sorts by stable ID, not by name -- "reverse direction" below only
    # exercises the doubling guard when Bob's ID actually sorts before
    # Alice's (NOT name-lexicographic order, which would mislead here).
    # Assert it rather than relying on unasserted hash ordering.
    alice_id = make_stable_entity_id(EntityType.person.value, "Alice")
    bob_id = make_stable_entity_id(EntityType.person.value, "Bob")
    assert bob_id < alice_id, (
        "test precondition: Bob's stable ID must sort before Alice's for the "
        "reverse-direction relation below to actually exercise the guard"
    )

    extractor = _make_extractor()
    text = "Bob and Alice."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Bob", EntityType.person.value),
                    _entity("Alice", EntityType.person.value),
                ],
                # Reverse-direction relative to the sorted() co-occurrence
                # normalisation (bob_id < alice_id, asserted above).
                relations=[
                    _relation(head="Alice", tail="Bob", label=RelationshipType.related_to.value)
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    related = [e for e in result.edges if e.relationship_type == RelationshipType.related_to]
    assert len(related) == 1, "only the normalised co-occurrence edge may exist, no duplicate"
    assert related[0].source_node_id < related[0].target_node_id


def test_self_loop_relation_is_skipped() -> None:
    """A head==tail relation from the engine is a self-loop and carries no
    graph signal; it must be skipped, not persisted as an edge.

    Includes a second, genuinely valid Alice/Carol relation (C3-T-04) so the
    typed edge it produces proves the typed-relation loop actually ran and
    processed relations -- not just that the co-occurrence loop (which runs
    regardless) produced an edge for the pair."""
    extractor = _make_extractor()
    text = "Alice talks to herself while Carol watches."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Carol", EntityType.person.value),
                ],
                relations=[
                    _relation(head="Alice", tail="Alice", label=RelationshipType.uses.value),
                    _relation(head="Alice", tail="Carol", label=RelationshipType.uses.value),
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    alice_id = make_stable_entity_id(EntityType.person.value, "Alice")
    carol_id = make_stable_entity_id(EntityType.person.value, "Carol")

    typed = [e for e in result.edges if e.relationship_type == RelationshipType.uses]
    assert len(typed) == 1, "the self-loop must be skipped; the valid Alice->Carol edge must persist"
    assert typed[0].source_node_id == alice_id
    assert typed[0].target_node_id == carol_id
    assert typed[0].id == make_stable_edge_id(alice_id, carol_id, RelationshipType.uses.value)

    cooccurrence = [e for e in result.edges if e.relationship_type == RelationshipType.related_to]
    assert len(cooccurrence) == 1


def test_unknown_relation_label_is_skipped_without_raising() -> None:
    """C1-B-2/C1-CC-1: an off-vocabulary relation label from the engine must
    be skipped with a debug log -- ValueError must never escape extract()."""
    extractor = _make_extractor()
    text = "Alice uses Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ],
                relations=[_relation(head="Alice", tail="Acme", label="not_a_real_label")],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert len(result.edges) == 1
    assert result.edges[0].relationship_type == RelationshipType.related_to


def test_unknown_entity_label_is_skipped_without_raising() -> None:
    """C1-B-2/C1-CC-1: an off-vocabulary entity label from the engine must be
    skipped with a debug log -- ValueError must never escape extract()."""
    extractor = _make_extractor()
    text = "Alice uses Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", "not_a_real_label"),
                    _entity("Acme", EntityType.system.value),
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    names = {n.entity_name for n in result.nodes}
    assert names == {"Acme"}, "the entity with the unknown label must be skipped, not raise"


def test_dangling_relation_is_skipped() -> None:
    """A relation whose head or tail text is not present in the entity set
    (dangling) must be skipped gracefully, not raise or fabricate a node.

    Includes a second, genuinely valid Alice/Carol relation (C3-T-04) so the
    typed edge it produces proves the typed-relation loop actually ran and
    processed relations -- not just that the co-occurrence loop (which runs
    regardless) produced an edge for the pair."""
    extractor = _make_extractor()
    text = "Alice uses Acme while Carol watches."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Carol", EntityType.person.value),
                ],
                relations=[
                    _relation(head="Alice", tail="Ghost", label=RelationshipType.uses.value),
                    _relation(head="Alice", tail="Carol", label=RelationshipType.uses.value),
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    alice_id = make_stable_entity_id(EntityType.person.value, "Alice")
    carol_id = make_stable_entity_id(EntityType.person.value, "Carol")

    assert result.fatal_error is None
    typed = [e for e in result.edges if e.relationship_type == RelationshipType.uses]
    assert len(typed) == 1, "the dangling relation must be skipped; the valid Alice->Carol edge must persist"
    assert typed[0].source_node_id == alice_id
    assert typed[0].target_node_id == carol_id
    assert typed[0].id == make_stable_edge_id(alice_id, carol_id, RelationshipType.uses.value)

    cooccurrence = [e for e in result.edges if e.relationship_type == RelationshipType.related_to]
    assert len(cooccurrence) == 1


def test_same_process_double_ingest_is_id_stable() -> None:
    """S28 (plumbing half): the same corpus, ingested twice in the same
    process with the same shared GraphExtractor instance, yields identical
    node/edge id sets and per-entity mention counts."""
    extractor = _make_extractor()
    text = "Alice uses Acme."
    extractor._backend = _FakeBackend(
        {
            text: ChunkExtraction(
                entities=[
                    _entity("Alice", EntityType.person.value),
                    _entity("Acme", EntityType.system.value),
                ],
                relations=[
                    _relation(head="Alice", tail="Acme", label=RelationshipType.uses.value)
                ],
            )
        }
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        first = await extractor.extract([chunk], "doc-1", "col")
        second = await extractor.extract([chunk], "doc-1", "col")
        return first, second

    first, second = asyncio.run(_run())

    assert {n.id for n in first.nodes} == {n.id for n in second.nodes}
    assert {e.id for e in first.edges} == {e.id for e in second.edges}
    assert len(first.mentions) == len(second.mentions)

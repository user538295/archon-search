"""Unit and integration tests for GraphExtractor — E1a BE-4.

Tests cover:
- spaCy label → entity type mapping (PERSON→person, ORG→system, CARDINAL skipped, etc.)
- C3 code-symbol path (symbol_type present → code_symbol entity; spaCy NER NOT called)
- LLM extraction stub (extraction_model set → llm_fallback_used=True + WARNING)
- spaCy absent → fatal_error result with actionable message
- stable entity IDs match make_stable_entity_id formula
- spaCy model auto-download: when model not in installed list, download is triggered + INFO logged
- asyncio.to_thread wrapping: _run_ner_sync called inside asyncio.to_thread
- Integration: stub spaCy returns fixed entities → nodes/edges populated correctly
- Co-occurrence edge count: 3 entities in one chunk → exactly 3 edges (N*(N-1)/2)
- Code-symbol name fallback: containing_function > containing_class > source_path basename
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.graph_types import (
    ChunkInput,
    EntityType,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# spaCy stub helpers
# ---------------------------------------------------------------------------


def _make_spacy_stub(
    entities_by_text: dict[str, list[tuple[str, str]]] | None = None,
    installed_models: list[str] | None = None,
    download_calls: list[str] | None = None,
) -> dict[str, types.ModuleType]:
    """Return a sys.modules patch dict with a fake spaCy stack.

    Args:
        entities_by_text: map from text → list of (entity_text, label) pairs
            returned by the fake NLP callable.  Texts not in the map return [].
        installed_models: list returned by ``spacy.util.get_installed_models()``.
            Defaults to ``["en_core_web_sm"]`` (model already installed).
        download_calls: optional mutable list that records ``spacy.cli.download()``
            call arguments for assertion.
    """
    entities_by_text = entities_by_text or {}
    if installed_models is None:
        installed_models = ["en_core_web_sm"]
    if download_calls is None:
        download_calls = []

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list[_FakeEnt]) -> None:
            self.ents = ents

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [_FakeEnt(t, lb) for t, lb in entities_by_text.get(text, [])]
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: list(installed_models)  # type: ignore[attr-defined]

    _captured_downloads = download_calls

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: _captured_downloads.append(model)  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    return {
        "spacy": fake_spacy,
        "spacy.util": fake_util,
        "spacy.cli": fake_cli,
    }


# ---------------------------------------------------------------------------
# 1. Label mapping
# ---------------------------------------------------------------------------


def test_extractor_label_mapping() -> None:
    """PERSON→person, ORG→system, EVENT→event, WORK_OF_ART→concept; CARDINAL skipped."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    # One text with one entity per label category we care about
    text_map: dict[str, list[tuple[str, str]]] = {
        "t1": [("Alice", "PERSON")],
        "t2": [("Acme Corp", "ORG")],
        "t3": [("London", "GPE")],
        "t4": [("Stadium", "FAC")],
        "t5": [("iPhone", "PRODUCT")],
        "t6": [("Olympics", "EVENT")],
        "t7": [("Mona Lisa", "WORK_OF_ART")],
        "t8": [("42", "CARDINAL")],  # should be SKIPPED
        "t9": [("2024-01-01", "DATE")],  # should be SKIPPED
    }

    stub = _make_spacy_stub(text_map)

    chunks = [
        ChunkInput(chunk_id=f"c{i}", text=t, symbol_type=None, symbol_subtype=None)
        for i, t in enumerate(text_map.keys())
    ]

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        with patch.dict(sys.modules, stub):
            return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    entity_types = {n.entity_name: n.entity_type for n in result.nodes}

    assert entity_types["Alice"] == EntityType.person
    assert entity_types["Acme Corp"] == EntityType.system
    assert entity_types["London"] == EntityType.system
    assert entity_types["Stadium"] == EntityType.system
    assert entity_types["iPhone"] == EntityType.system
    assert entity_types["Olympics"] == EntityType.event
    assert entity_types["Mona Lisa"] == EntityType.concept
    # CARDINAL and DATE must be absent
    assert "42" not in entity_types
    assert "2024-01-01" not in entity_types


# ---------------------------------------------------------------------------
# 2. C3 code-symbol path
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_from_c3() -> None:
    """Chunk with symbol_type='class' → code_symbol entity; spaCy NER not called."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    ner_calls: list[str] = []

    class _TrackingDoc:
        ents: list = []

    class _TrackingNLP:
        def __call__(self, text: str) -> _TrackingDoc:
            ner_calls.append(text)
            return _TrackingDoc()

    # Pre-load a tracking NLP so we can assert it is NOT called for code chunks
    extractor._nlp = _TrackingNLP()

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

    # spaCy NER must NOT have been called for the code chunk
    assert ner_calls == [], f"spaCy NER was called for code chunk: {ner_calls}"

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.entity_type == EntityType.code_symbol
    assert node.entity_name == "MyService"
    assert node.entity_subtype == "python-class"
    assert node.id == make_stable_entity_id(EntityType.code_symbol.value, "MyService")


# ---------------------------------------------------------------------------
# 3. LLM stub warning
# ---------------------------------------------------------------------------


def test_extractor_llm_stub_warning() -> None:
    """extraction_model set → llm_fallback_used=True; WARNING in result.warnings."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig(extraction_model="gpt-4")
    extractor = GraphExtractor(config)

    # Pre-load a stub NLP; no real entities returned
    class _NullDoc:
        ents: list = []

    extractor._nlp = lambda text: _NullDoc()

    chunk = ChunkInput(chunk_id="c1", text="Hello world.", symbol_type=None, symbol_subtype=None)

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.llm_fallback_used is True
    assert any("LLM" in w or "extraction_model" in w or "gpt-4" in w for w in result.warnings), (
        f"Expected LLM fallback warning, got: {result.warnings}"
    )
    # No fatal error — spaCy path ran fine
    assert result.fatal_error is None


# ---------------------------------------------------------------------------
# 4. spaCy absent → fatal_error
# ---------------------------------------------------------------------------


def test_extractor_spacy_absent_returns_error() -> None:
    """When spaCy is absent from sys.modules, extract() returns a fatal_error result."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)
    # Ensure _nlp is not pre-set so the import probe runs
    extractor._nlp = None

    chunk = ChunkInput(chunk_id="c1", text="Hello world.", symbol_type=None, symbol_subtype=None)

    async def _run():
        # Setting sys.modules["spacy"] = None causes ImportError
        with patch.dict(sys.modules, {"spacy": None}):  # type: ignore[dict-item]
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is not None, "Expected fatal_error to be non-None when spaCy absent"
    assert result.nodes == []
    assert result.edges == []
    assert len(result.warnings) > 0
    # Actionable install hint must be present
    assert "archon-search[graph]" in result.fatal_error


# ---------------------------------------------------------------------------
# 5. Stable entity IDs match make_stable_entity_id formula
# ---------------------------------------------------------------------------


def test_extractor_stable_ids_match_formula() -> None:
    """Entity IDs in extraction result match make_stable_entity_id() formula."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    stub = _make_spacy_stub({"Alice works at Acme.": [("Alice", "PERSON"), ("Acme", "ORG")]})
    chunk = ChunkInput(
        chunk_id="c1", text="Alice works at Acme.", symbol_type=None, symbol_subtype=None
    )

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    node_map = {n.entity_name: n.id for n in result.nodes}

    assert node_map["Alice"] == make_stable_entity_id(EntityType.person.value, "Alice")
    assert node_map["Acme"] == make_stable_entity_id(EntityType.system.value, "Acme")


# ---------------------------------------------------------------------------
# 6. spaCy model auto-download + INFO log
# ---------------------------------------------------------------------------


def test_extractor_spacy_model_download_logs_info() -> None:
    """When en_core_web_sm not in installed models, download is triggered and INFO logged."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)
    extractor._nlp = None  # ensure lazy load is triggered

    download_calls: list[str] = []
    stub = _make_spacy_stub(
        entities_by_text={},
        installed_models=[],  # model NOT installed → download triggered
        download_calls=download_calls,
    )

    chunk = ChunkInput(chunk_id="c1", text="No entities here.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, stub):
            with patch("archon_search.graph_extractor._logger") as mock_logger:
                await extractor.extract([chunk], "doc-1", "col")
        return mock_logger

    mock_logger = asyncio.run(_run())

    # spacy.cli.download("en_core_web_sm") must have been called
    assert "en_core_web_sm" in download_calls, (
        f"Expected en_core_web_sm download, got: {download_calls}"
    )

    # An INFO log about the download must have been emitted
    info_msgs = [str(call_args) for call_args in mock_logger.info.call_args_list]
    assert any("en_core_web_sm" in m for m in info_msgs), (
        f"Expected INFO log mentioning en_core_web_sm; got calls: {info_msgs}"
    )


# ---------------------------------------------------------------------------
# 7. asyncio.to_thread wrapping of spaCy NER call
# ---------------------------------------------------------------------------


def test_extractor_spacy_call_wrapped_in_asyncio_to_thread() -> None:
    """GraphExtractor._run_ner_sync must be called via asyncio.to_thread in extract()."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    # Pre-set stub NLP so model loading doesn't trigger a separate to_thread call
    class _NullDoc:
        ents: list = []

    extractor._nlp = lambda text: _NullDoc()

    chunk = ChunkInput(chunk_id="c1", text="Hello world.", symbol_type=None, symbol_subtype=None)
    to_thread_fns: list = []

    async def _tracking_to_thread(fn, *args, **kwargs):
        to_thread_fns.append(fn)
        # Actually run the function synchronously so the coroutine behaves correctly
        return fn(*args, **kwargs)

    async def _run():
        with patch("asyncio.to_thread", side_effect=_tracking_to_thread):
            return await extractor.extract([chunk], "doc-1", "col")

    asyncio.run(_run())

    ner_fn = extractor._run_ner_sync
    assert any(fn == ner_fn for fn in to_thread_fns), (
        f"asyncio.to_thread was not called with _run_ner_sync. "
        f"Got: {[getattr(f, '__name__', str(f)) for f in to_thread_fns]}"
    )


# ---------------------------------------------------------------------------
# 8. Integration: stub spaCy returns fixed entities → nodes/edges populated
# ---------------------------------------------------------------------------


def test_extractor_extract_from_real_chunks() -> None:
    """Stub spaCy returns fixed entities; assert nodes and edges populated correctly."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text = "Alice works with Bob at Acme Corp."
    stub = _make_spacy_stub({
        text: [("Alice", "PERSON"), ("Bob", "PERSON"), ("Acme Corp", "ORG")],
    })
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    names = {n.entity_name for n in result.nodes}
    assert names == {"Alice", "Bob", "Acme Corp"}

    # 3 entities in one chunk → 3 co-occurrence edges (N*(N-1)/2 = 3)
    assert len(result.edges) == 3

    # All edges use RELATED_TO relationship
    for edge in result.edges:
        assert edge.relationship_type == RelationshipType.related_to


# ---------------------------------------------------------------------------
# 9. Co-occurrence edge count: N*(N-1)/2
# ---------------------------------------------------------------------------


def test_extractor_cooccurrence_edge_count() -> None:
    """Chunk with 3 entities A, B, C → exactly 3 edges, not 6 (no directed doubling)."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text = "A, B, and C."
    stub = _make_spacy_stub({text: [("A", "PERSON"), ("B", "PERSON"), ("C", "PERSON")]})
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 3
    assert len(result.edges) == 3, (
        f"Expected 3 edges for 3 entities (N*(N-1)/2), got {len(result.edges)}"
    )

    # Verify edges are sorted (source_id < target_id lexicographically)
    for edge in result.edges:
        assert edge.source_node_id < edge.target_node_id, (
            f"Edge source {edge.source_node_id[:8]} is not < target {edge.target_node_id[:8]}"
        )


# ---------------------------------------------------------------------------
# 10. Code-symbol name fallback
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_name_fallback() -> None:
    """Three code chunks: containing_function → containing_class → source_path basename."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

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
# 11. Label mapping completeness — LOC, LAW, LANGUAGE, NORP
# ---------------------------------------------------------------------------


def test_extractor_label_mapping_extended() -> None:
    """LOC→system, LAW→concept, LANGUAGE→concept, NORP→concept are all mapped."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text_map: dict[str, list[tuple[str, str]]] = {
        "t1": [("Mississippi River", "LOC")],
        "t2": [("GDPR", "LAW")],
        "t3": [("Python", "LANGUAGE")],
        "t4": [("Americans", "NORP")],
    }
    stub = _make_spacy_stub(text_map)
    chunks = [
        ChunkInput(chunk_id=f"c{i}", text=t, symbol_type=None, symbol_subtype=None)
        for i, t in enumerate(text_map.keys())
    ]

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())
    entity_types = {n.entity_name: n.entity_type for n in result.nodes}

    assert entity_types["Mississippi River"] == EntityType.system
    assert entity_types["GDPR"] == EntityType.concept
    assert entity_types["Python"] == EntityType.concept
    assert entity_types["Americans"] == EntityType.concept


# ---------------------------------------------------------------------------
# 12. Empty chunks list → empty result, no error
# ---------------------------------------------------------------------------


def test_extractor_empty_chunks_list() -> None:
    """extract() with an empty list of chunks returns empty result with no error."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    async def _run():
        return await extractor.extract([], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert result.nodes == []
    assert result.edges == []
    assert result.warnings == []


# ---------------------------------------------------------------------------
# 13. Duplicate entity in same chunk → single node, zero edges
# ---------------------------------------------------------------------------


def test_extractor_duplicate_entity_same_chunk() -> None:
    """spaCy returning the same entity twice in one chunk → 1 node, 0 edges."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text = "Alice met Alice at Acme."
    # spaCy returns "Alice" twice — simulates span-level duplicates
    stub = _make_spacy_stub({text: [("Alice", "PERSON"), ("Alice", "PERSON")]})
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert len(result.nodes) == 1, f"Expected 1 node (deduplicated), got {len(result.nodes)}"
    assert result.nodes[0].entity_name == "Alice"
    assert len(result.edges) == 0, f"Expected 0 edges (single unique entity), got {len(result.edges)}"


# ---------------------------------------------------------------------------
# 14. Same entity in two chunks → single node, edges from each chunk
# ---------------------------------------------------------------------------


def test_extractor_entity_across_multiple_chunks() -> None:
    """Same entity in two chunks with different co-occurring entities → one node, edges from both."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text1 = "Alice works with Bob."
    text2 = "Alice also knows Carol."
    stub = _make_spacy_stub({
        text1: [("Alice", "PERSON"), ("Bob", "PERSON")],
        text2: [("Alice", "PERSON"), ("Carol", "PERSON")],
    })
    chunks = [
        ChunkInput(chunk_id="c1", text=text1, symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="c2", text=text2, symbol_type=None, symbol_subtype=None),
    ]

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    names = {n.entity_name for n in result.nodes}
    assert names == {"Alice", "Bob", "Carol"}, f"Expected 3 unique nodes, got {names}"

    # Each chunk contributes 1 edge (2 entities per chunk → 1 pair each)
    assert len(result.edges) == 2, (
        f"Expected 2 edges (one per chunk), got {len(result.edges)}"
    )

    # Edge IDs must be unique
    edge_ids = [e.id for e in result.edges]
    assert len(edge_ids) == len(set(edge_ids)), "Duplicate edge IDs found"


# ---------------------------------------------------------------------------
# 15. Mixed code + text chunks in a single extract() call
# ---------------------------------------------------------------------------


def test_extractor_mixed_code_and_text_chunks() -> None:
    """Mixed code chunks and text chunks in one call → both entity types produced."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    text = "Alice uses AuthService."
    stub = _make_spacy_stub({text: [("Alice", "PERSON")]})

    code_chunk = ChunkInput(
        chunk_id="c1",
        text="class AuthService: ...",
        symbol_type="class",
        symbol_subtype="python-class",
        containing_class="AuthService",
    )
    text_chunk = ChunkInput(chunk_id="c2", text=text, symbol_type=None, symbol_subtype=None)

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract([code_chunk, text_chunk], "doc-1", "col")

    result = asyncio.run(_run())

    entity_types = {n.entity_name: n.entity_type for n in result.nodes}
    assert "AuthService" in entity_types
    assert entity_types["AuthService"] == EntityType.code_symbol
    assert "Alice" in entity_types
    assert entity_types["Alice"] == EntityType.person

    # code chunks and text chunks are in separate chunk_entity_ids lists,
    # so co-occurrence edges are scoped per-chunk — no cross-chunk edges.
    # AuthService (code, 1 entity) → 0 edges from its chunk
    # Alice (text, 1 entity) → 0 edges from its chunk
    assert len(result.edges) == 0, (
        "Expected 0 edges: each chunk has only 1 entity (no co-occurrence possible). "
        f"Got {len(result.edges)} edges."
    )

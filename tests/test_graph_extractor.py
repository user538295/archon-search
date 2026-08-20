"""Unit and integration tests for GraphExtractor — E1a BE-4.

Tests cover:
- spaCy label → entity type mapping (PERSON→person, ORG→system, CARDINAL skipped, etc.)
- C3 code-symbol path (symbol_type present → code_symbol entity; spaCy NER NOT called)
- LLM relationship labeling AND-gate (LLCP BE-7): gate closed → silent skip; gate open →
  per-chunk label_relationships call, additive typed edges; call failure → per-chunk
  spaCy-only fallback with llm_fallback_used=True + WARNING
- spaCy absent → fatal_error result with actionable message
- stable entity IDs match make_stable_entity_id formula
- spaCy model resolution: installed package → data-dir path → latched degrade; runtime never downloads
- asyncio.to_thread wrapping: _run_ner_sync called inside asyncio.to_thread
- Integration: stub spaCy returns fixed entities → nodes/edges populated correctly
- Co-occurrence edge count: 3 entities in one chunk → exactly 3 edges (N*(N-1)/2)
- Code-symbol name fallback: containing_function > containing_class > source_path basename
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.graph_types import (
    ChunkInput,
    EntityType,
    GraphMention,
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
    *,
    probe_calls: list[None] | None = None,
    spacy_version: str = "3.8.15",
    poison_cli: bool = False,
    download_raises: BaseException | None = None,
) -> dict[str, types.ModuleType]:
    """Return a sys.modules patch dict with a fake spaCy stack.

    The single shared spaCy stub factory for this module and
    ``tests/test_graph_ner_model_visibility.py`` (2026-08-19-030 review:
    three independently hand-rolled spaCy stubs across this change set was
    the exact mechanism that produced a fabricated-fixture Critical
    elsewhere — consolidated here instead).

    Args:
        entities_by_text: map from text → list of (entity_text, label) pairs
            returned by the fake NLP callable.  Texts not in the map return [].
        installed_models: list returned by ``spacy.util.get_installed_models()``.
            Defaults to ``["en_core_web_sm"]`` (model already installed).
        download_calls: optional mutable list that records ``spacy.cli.download()``
            call arguments for assertion. The runtime never calls this
            (2026-08-19-030: the downloader is gone from ``graph_extractor``),
            so in every real code path this list stays empty — it exists only
            as a historical regression guard. Ignored when ``poison_cli=True``.
        probe_calls: optional mutable list that ``get_installed_models()``
            appends a sentinel to on every call, so a test can assert the
            resolver probes installed-package metadata at most once per
            process (2026-08-19-030 T1).
        spacy_version: value exposed as ``spacy.__version__``, for model
            compatibility tests (C1-I-3).
        poison_cli: when ``True``, ``spacy.cli`` raises ``AssertionError`` on
            ANY attribute access instead of merely recording calls — a
            behavioural guard (T11) proving the runtime never reaches for
            ``spacy.cli`` at all, pairing the weaker source-text scan in
            ``test_extractor_never_downloads_spacy_model_at_runtime``.
        download_raises: if set, ``spacy.cli.download`` raises this instead of
            recording the call and returning. Used by the latch tests, which
            simulate the historical downloader that blew up with
            ``SystemExit(1)``. Ignored when ``poison_cli=True``.
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

    def _get_installed_models() -> list[str]:
        if probe_calls is not None:
            probe_calls.append(None)
        return list(installed_models)

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = _get_installed_models  # type: ignore[attr-defined]

    fake_cli: types.ModuleType
    if poison_cli:

        class _PoisonedCli(types.ModuleType):
            """Records the access before raising.

            The raise alone is not a guard: `_ensure_nlp` catches
            `BaseException`, so an AssertionError from here degrades exactly
            like a missing model and leaves no trace in the result. The
            `accesses` list is what a test can actually assert on
            (2026-08-19-030 C2-T-1).
            """

            accesses: list[str] = []

            def __getattr__(self, name: str) -> object:
                _PoisonedCli.accesses.append(name)
                raise AssertionError(
                    f"spacy.cli.{name} must never be accessed at runtime "
                    "(2026-08-19-030: the downloader is gone)"
                )

        _PoisonedCli.accesses = []
        fake_cli = _PoisonedCli("spacy.cli")
    else:
        _captured_downloads = download_calls

        def _download(model: str) -> None:
            _captured_downloads.append(model)
            if download_raises is not None:
                raise download_raises

        fake_cli = types.ModuleType("spacy.cli")
        fake_cli.download = _download  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.__version__ = spacy_version  # type: ignore[attr-defined]
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


def test_extractor_extraction_model_without_provider_skips_enrichment_silently() -> None:
    """LLCP BE-7: the AND-gate (provider + extraction_model + client) replaces the
    old E1a stub. extraction_model set but provider=None (default, incomplete gate)
    -> no enrichment call attempted, no warning, llm_fallback_used stays False."""
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

    assert result.llm_fallback_used is False
    assert result.warnings == []
    # No fatal error — spaCy path ran fine
    assert result.fatal_error is None


def test_graph_extractor_calls_label_relationships_per_chunk() -> None:
    """AND-gate open (provider + extraction_model + client all set) -> label_relationships
    is called once per text chunk with 2+ entities; the LLM-typed edge is persisted
    additively alongside the related_to co-occurrence edge (S9, S3)."""
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LabeledRelationship
    from archon_search.graph_extractor import GraphExtractor

    text = "Alice uses Acme."
    stub = _make_spacy_stub({text: [("Alice", "PERSON"), ("Acme", "ORG")]})
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Alice", target_entity="Acme", relationship_type="uses"
            )
        ]
    )

    config = GraphConfig(provider="llama_cpp", extraction_model="model-x")
    extractor = GraphExtractor(config, enrichment_client=mock_client)
    extractor._nlp = stub["spacy"].load("en_core_web_sm")

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
    assert result.llm_fallback_used is False


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
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LabeledRelationship
    from archon_search.graph_extractor import GraphExtractor

    text = "Alice and Bob both work at Google."
    stub = _make_spacy_stub(
        {text: [("Alice", "PERSON"), ("Bob", "PERSON"), ("Google", "ORG")]}
    )
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(
        return_value=[
            LabeledRelationship(
                source_entity="Bob / Google", target_entity="Google", relationship_type="uses"
            )
        ]
    )

    config = GraphConfig(provider="llama_cpp", extraction_model="model-x")
    extractor = GraphExtractor(config, enrichment_client=mock_client)
    extractor._nlp = stub["spacy"].load("en_core_web_sm")

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    relationship_types = {e.relationship_type for e in result.edges}
    assert RelationshipType.uses in relationship_types, (
        "the garbled-but-recoverable relationship must still produce a typed edge"
    )


def test_graph_extractor_catches_enrichment_error() -> None:
    """label_relationships raising -> per-chunk WARNING + spaCy-only fallback for that
    chunk; extract() does not raise and the co-occurrence edge is still produced (S9)."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    text = "Alice uses Acme."
    stub = _make_spacy_stub({text: [("Alice", "PERSON"), ("Acme", "ORG")]})
    chunk = ChunkInput(chunk_id="c1", text=text, symbol_type=None, symbol_subtype=None)

    mock_client = MagicMock()
    mock_client.label_relationships = AsyncMock(side_effect=RuntimeError("boom"))

    config = GraphConfig(provider="llama_cpp", extraction_model="model-x")
    extractor = GraphExtractor(config, enrichment_client=mock_client)
    extractor._nlp = stub["spacy"].load("en_core_web_sm")

    async def _run():
        return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert result.llm_fallback_used is True
    assert len(result.edges) == 1
    assert result.edges[0].relationship_type == RelationshipType.related_to
    assert any("boom" in w or "chunk" in w.lower() for w in result.warnings)


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
# 6. Runtime never downloads the spaCy model (2026-08-19-030)
# ---------------------------------------------------------------------------


def test_extractor_never_downloads_spacy_model_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing model degrades with a WARNING — the runtime never invokes the downloader.

    `spacy.cli.download` can never work in a pip-less `uv tool install` venv; the
    wizard provisions the model into the data dir instead.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    # T10: pin an empty data dir — the shared per-worker fixture data dir could
    # otherwise carry a leftover model dir from an unrelated test, turning this
    # into a cross-module flake under `-n 8 --dist=loadgroup`.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    config = GraphConfig()
    extractor = GraphExtractor(config)
    extractor._nlp = None  # ensure lazy load is triggered

    download_calls: list[str] = []
    stub = _make_spacy_stub(
        entities_by_text={},
        installed_models=[],  # model NOT installed → old code downloaded here
        download_calls=download_calls,
    )

    chunk = ChunkInput(chunk_id="c1", text="No entities here.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, stub):
            with patch("archon_search.graph_extractor._logger") as mock_logger:
                result = await extractor.extract([chunk], "doc-1", "col")
        return result, mock_logger

    result, mock_logger = asyncio.run(_run())

    assert download_calls == [], (
        f"Runtime must never call spacy.cli.download; got: {download_calls}"
    )
    assert "spacy.cli" not in inspect.getsource(sys.modules[GraphExtractor.__module__]), (
        "the runtime downloader must be gone from graph_extractor, not merely unused"
    )
    assert result.fatal_error is None, "A missing model degrades; it is not fatal"
    warn_msgs = [str(call_args) for call_args in mock_logger.warning.call_args_list]
    assert any("en_core_web_sm" in m for m in warn_msgs), (
        f"Expected a WARNING naming the unavailable model; got calls: {warn_msgs}"
    )


def test_extractor_never_touches_spacy_cli_even_if_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural pair (T11) for the source-scan assertion above.

    `"spacy.cli" not in inspect.getsource(...)` is defeated by anything that
    references `spacy.cli` without matching that literal substring (e.g.
    `from spacy import cli`) and false-fails on an unrelated comment mention.
    Here `spacy.cli` raises `AssertionError` on ANY attribute access — proving
    the runtime never reaches for it at all, regardless of how the source
    happens to read.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))  # T10

    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    from archon_search.graph_extractor import _MODEL_ABSENT_WARNING

    extractor = GraphExtractor(GraphConfig())
    stub = _make_spacy_stub(installed_models=[], poison_cli=True)

    chunk = ChunkInput(chunk_id="c1", text="No entities here.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, stub):
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    # The AssertionError from _PoisonedCli would be swallowed by _ensure_nlp's
    # `except BaseException` and degrade exactly like a missing model, so
    # asserting on the result alone cannot detect a reintroduced downloader
    # (2026-08-19-030 C2-T-1). Assert on the recorded accesses instead — that
    # list survives the swallow.
    assert stub["spacy"].cli.accesses == [], (
        "the runtime reached for spacy.cli at "
        f"{stub['spacy'].cli.accesses!r} — the downloader must be gone"
    )
    assert result.fatal_error is None, "A missing model degrades; it is not fatal"
    assert result.warnings == [_MODEL_ABSENT_WARNING], (
        "a swallowed spacy.cli AssertionError would masquerade as a missing "
        f"model; got warnings={result.warnings!r}"
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


# ---------------------------------------------------------------------------
# BE-4: Mentions extraction (E2b entity incidence tracking)
# ---------------------------------------------------------------------------


def test_extractor_code_symbol_mentions() -> None:
    """Code-symbol chunk produces GraphMention with correct chunk_id and entity_id."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

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

    # Must have exactly one node (MyService)
    assert len(result.nodes) == 1
    node = result.nodes[0]
    entity_id = node.id

    # Must have exactly one mention linking MyService to the chunk
    assert len(result.mentions) == 1, f"Expected 1 mention, got {len(result.mentions)}"
    mention = result.mentions[0]
    assert mention.entity_id == entity_id, (
        f"Mention entity_id {mention.entity_id} does not match node id {entity_id}"
    )
    assert mention.chunk_id == chunk_id, (
        f"Mention chunk_id {mention.chunk_id} does not match input chunk_id {chunk_id}"
    )
    assert mention.doc_id == doc_id, f"Mention doc_id {mention.doc_id} does not match {doc_id}"


def test_extractor_ner_mentions() -> None:
    """NER mentions correctly pair entities with their chunk_ids via zip alignment.

    Three chunks: entities in chunks 0 and 2 (not 1).
    Assert mentions contain exactly two GraphMention objects referencing the correct chunk_ids.
    This verifies the zip(text_chunks, ner_per_chunk) alignment is correct.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

    doc_id = "doc-1"
    chunks = [
        ChunkInput(chunk_id="doc-1-000000", text="Alice works here.", symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="doc-1-000001", text="No entities here.", symbol_type=None, symbol_subtype=None),
        ChunkInput(chunk_id="doc-1-000002", text="Bob is great.", symbol_type=None, symbol_subtype=None),
    ]

    # spaCy stub returns entities only in chunks 0 and 2
    stub = _make_spacy_stub({
        "Alice works here.": [("Alice", "PERSON")],
        "No entities here.": [],
        "Bob is great.": [("Bob", "PERSON")],
    })

    async def _run():
        extractor._nlp = stub["spacy"].load("en_core_web_sm")
        return await extractor.extract(chunks, doc_id, "col")

    result = asyncio.run(_run())

    # Must have exactly 2 nodes (Alice, Bob)
    assert len(result.nodes) == 2, f"Expected 2 nodes, got {len(result.nodes)}"
    node_map = {n.entity_name: n.id for n in result.nodes}
    alice_id = node_map["Alice"]
    bob_id = node_map["Bob"]

    # Must have exactly 2 mentions (one for Alice, one for Bob)
    assert len(result.mentions) == 2, f"Expected 2 mentions, got {len(result.mentions)}"

    # Verify Alice mention is linked to chunk 0
    alice_mentions = [m for m in result.mentions if m.entity_id == alice_id]
    assert len(alice_mentions) == 1, f"Expected 1 mention for Alice, got {len(alice_mentions)}"
    alice_mention = alice_mentions[0]
    assert alice_mention.chunk_id == "doc-1-000000", (
        f"Alice mention should be in chunk-0 (doc-1-000000), got {alice_mention.chunk_id}"
    )
    assert alice_mention.doc_id == doc_id

    # Verify Bob mention is linked to chunk 2 (not chunk 1)
    bob_mentions = [m for m in result.mentions if m.entity_id == bob_id]
    assert len(bob_mentions) == 1, f"Expected 1 mention for Bob, got {len(bob_mentions)}"
    bob_mention = bob_mentions[0]
    assert bob_mention.chunk_id == "doc-1-000002", (
        f"Bob mention should be in chunk-2 (doc-1-000002), got {bob_mention.chunk_id}"
    )
    assert bob_mention.doc_id == doc_id

    # Crucially: no mention should reference chunk 1
    chunk1_mentions = [m for m in result.mentions if m.chunk_id == "doc-1-000001"]
    assert len(chunk1_mentions) == 0, (
        f"No mentions should be in chunk-1 (no entities), but got {len(chunk1_mentions)}"
    )


def test_extractor_early_exit_mentions_empty() -> None:
    """When spaCy not importable, mentions=[] on returned result."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)
    extractor._nlp = None  # ensure import probe runs

    chunk = ChunkInput(chunk_id="c1", text="Hello world.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, {"spacy": None}):  # type: ignore[dict-item]
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    # When extraction fails with fatal_error, mentions must be empty
    assert result.fatal_error is not None
    assert result.mentions == [], f"Expected empty mentions on fatal error, got {result.mentions}"


# ---------------------------------------------------------------------------
# E2g BE-2, Critical #2: code_symbol node identity is file-qualified
# ---------------------------------------------------------------------------


def test_sameNameDifferentFiles_produceDistinctNodes() -> None:
    """Two unrelated same-named code symbols in different files get distinct node IDs.

    Covers the graph_extractor.py:211 call site specifically (the DefRefExtractor
    call site is covered separately in tests/test_defref_extractor.py). Both chunks
    use ``containing_function="run"`` so ``entity_name`` is identical ("run"); only
    ``source_path`` differs, which must be reflected in the hashed ID while
    ``entity_name`` stays the bare, non-file-qualified name in both nodes.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)

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

    assert len(result.nodes) == 2, f"Expected 2 distinct nodes, got {len(result.nodes)}"
    ids = {n.id for n in result.nodes}
    assert len(ids) == 2, "Same-named symbols in different files must hash to distinct IDs"

    for node in result.nodes:
        assert node.entity_name == "run", (
            f"entity_name must stay the bare symbol name, got {node.entity_name!r}"
        )

    expected_id_a = make_stable_entity_id(EntityType.code_symbol.value, "run::/repo/a.py")
    expected_id_b = make_stable_entity_id(EntityType.code_symbol.value, "run::/repo/b.py")
    assert ids == {expected_id_a, expected_id_b}


# ---------------------------------------------------------------------------
# Regression: a pip-less venv must degrade, never raise SystemExit
# ---------------------------------------------------------------------------


def test_extractor_missing_model_degrades_without_systemexit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pip-less venv (`uv tool install`) must degrade, not kill the server.

    Originally a regression for `spacy.cli.download` raising SystemExit(1) — which
    is BaseException, so it escaped `except Exception` and killed uvicorn. The
    downloader is gone from the runtime (2026-08-19-030), so the failure class is
    now "the model is simply not there": degrade with a warning, never fatal.
    """
    import types

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))  # T10

    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    config = GraphConfig()
    extractor = GraphExtractor(config)
    extractor._nlp = None  # force lazy load path

    # A downloader that would blow up if the runtime ever reached for it.
    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: []  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: (_ for _ in ()).throw(SystemExit(1))  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]
    # spacy.load should never be reached: no model is installed or provisioned.
    fake_spacy.load = lambda model: (_ for _ in ()).throw(AssertionError("spacy.load must not be called"))  # type: ignore[attr-defined]

    stub = {"spacy": fake_spacy, "spacy.util": fake_util, "spacy.cli": fake_cli}

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, stub):
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None, (
        f"A missing model is auxiliary — degrade, never abort; got {result.fatal_error!r}"
    )
    assert any("en_core_web_sm" in w for w in result.warnings), (
        f"Expected a warning naming the model; got {result.warnings!r}"
    )
    # Server must survive — no SystemExit propagated
    assert result.nodes == []
    assert result.edges == []


# ---------------------------------------------------------------------------
# Regression: 2026-08-19-030 — spaCy model-load failure must latch and degrade
# ---------------------------------------------------------------------------
#
# Two coupled defects reproduced below:
#   1. No failure latch — `_load_nlp_sync` is re-run (probe + download) on every
#      extract() call because `self._nlp` stays None.  Production log shows the
#      "(first call only)" INFO 734 times in 27 minutes.
#   2. Auxiliary failure kills the primary operation — the load failure becomes
#      `fatal_error`, which aborts ingest before persist (see the companion
#      integration test in tests/integration/test_bug030_graph_spacy_latch_ingest.py).


def test_extractor_model_load_failure_latches_no_second_load_attempt() -> None:
    """Defect 1: after a failing model load, the next extract() must not retry the load."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    load_mock = MagicMock(
        side_effect=RuntimeError(
            "spaCy model download failed (no package installer found; exit code 1)."
        )
    )
    extractor._load_nlp_sync = load_mock  # type: ignore[method-assign]

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run() -> None:
        # spaCy itself is importable; only the model load fails.
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            await extractor.extract([chunk], "doc-1", "col")
            await extractor.extract([chunk], "doc-2", "col")

    asyncio.run(_run())

    assert load_mock.call_count == 1, (
        "Model-load failure must latch: the second extract() re-ran the probe + download "
        f"(_load_nlp_sync called {load_mock.call_count} times, expected 1)"
    )


def test_extractor_model_load_failure_degrades_instead_of_fatal() -> None:
    """Defect 2: a missing model is an auxiliary failure — degrade, never fatal_error."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(
            "spaCy model download failed (no package installer found; exit code 1)."
        )
    )

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
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            return await extractor.extract(chunks, "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None, (
        "A missing spaCy model must degrade, not abort the ingest; got fatal_error: "
        f"{result.fatal_error!r}"
    )
    assert any("en_core_web_sm" in w for w in result.warnings), (
        f"Expected a warning naming the model; got warnings: {result.warnings!r}"
    )
    assert "parse_config" in {n.entity_name for n in result.nodes}, (
        "Code-symbol extraction works without spaCy and must survive the degraded path; "
        f"got nodes: {[n.entity_name for n in result.nodes]}"
    )


def test_extractor_model_load_failure_warns_exactly_once_across_files(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 1: the model-unavailable WARNING must fire once, not once per file."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))  # T10

    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    download_calls: list[str] = []
    # Pip-less venv: model absent everywhere, and (historically) the downloader
    # exits with SystemExit(1) — dead machinery today (2026-08-19-030: the
    # runtime never calls spacy.cli.download), kept only so a regression that
    # re-introduces the call would still surface as a recorded attempt.
    stub = _make_spacy_stub(installed_models=[], download_calls=download_calls, download_raises=SystemExit(1))

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run() -> None:
        with patch.dict(sys.modules, stub):
            for i in range(3):
                await extractor.extract([chunk], f"doc-{i}", "col")

    with caplog.at_level(logging.INFO, logger="archon_search.graph_extractor"):
        asyncio.run(_run())

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "en_core_web_sm" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        "Expected exactly one WARNING about the unavailable model across 3 files; got "
        f"{len(warnings)}: {[r.getMessage() for r in warnings]}"
    )


def test_extractor_model_load_failure_does_not_reprobe_per_file(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 1 (production evidence): 734 INFO lines / 734 download attempts in 27 min.

    T1: strengthened past the post-fix code's dead counters (`spacy.cli` is
    gone entirely, so `download_calls` could never increment even with the
    latch removed) — `probe_calls` tracks the ACTIVE resolution path
    (`get_installed_models()`, called by `resolve_spacy_model()`), which the
    latch genuinely governs, and asserts it fires exactly once across 3
    documents rather than merely `<= 1`.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))  # T10

    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    download_calls: list[str] = []
    probe_calls: list[None] = []
    stub = _make_spacy_stub(
        installed_models=[],
        download_calls=download_calls,
        probe_calls=probe_calls,
        download_raises=SystemExit(1),
    )

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run() -> None:
        with patch.dict(sys.modules, stub):
            for i in range(3):
                await extractor.extract([chunk], f"doc-{i}", "col")

    with caplog.at_level(logging.INFO, logger="archon_search.graph_extractor"):
        asyncio.run(_run())

    assert download_calls == [], (
        f"The download must never be invoked (dead code, 2026-08-19-030); got {download_calls}"
    )
    assert len(probe_calls) == 1, (
        "The installed-models probe must run at most once per process — "
        f"got {len(probe_calls)} calls across 3 documents"
    )

    retry_logs = [r for r in caplog.records if "auto-downloading" in r.getMessage()]
    assert len(retry_logs) <= 1, (
        '"(first call only)" must be true: the line was logged '
        f"{len(retry_logs)} times across 3 files"
    )


def test_extractor_model_load_survives_systemexit_from_load() -> None:
    """Relocated regression guard (T12): `_ensure_nlp` catches `BaseException`,
    not `Exception`, around the load call specifically so a `SystemExit`
    raised inside it — the historical shape of this bug, when
    `spacy.cli.download` called `sys.exit(1)` on failure — degrades instead
    of escaping uncaught and killing the process. The downloader itself is
    gone from the runtime now, so this pins the general guarantee directly
    against `_load_nlp_sync`, independent of what happens to raise it.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=SystemExit(1)
    )

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None, (
        "A SystemExit from the load path must degrade, not propagate and crash "
        f"the process; got fatal_error={result.fatal_error!r}"
    )
    assert any("en_core_web_sm" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# C1-B-4: the "[graph] extra missing" (ImportError) probe must also latch —
# same unlatched-reprobe shape as the model-load failure, triggered by a
# missing spaCy PACKAGE rather than a missing model.
# ---------------------------------------------------------------------------


def test_extractor_spacy_not_importable_latches_no_second_probe() -> None:
    """The ImportError probe runs at most once per process, not once per file.

    Before the fix, `_ensure_nlp` returned the fatal message without setting
    any latch, so the `import spacy` probe (and the ``ConfigError``-shaped
    per-file abort) re-ran on every `extract()` call — the identical defect
    shape the model-load latch exists to fix, just triggered by a missing
    `[graph]` extra instead of a missing model.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run() -> list[bool]:
        results = []
        with patch.dict(sys.modules, {"spacy": None}):  # type: ignore[dict-item]
            for i in range(3):
                result = await extractor.extract([chunk], f"doc-{i}", "col")
                results.append(result.fatal_error is not None)
        return results

    fatal_flags = asyncio.run(_run())

    assert fatal_flags == [True, True, True], (
        "every call must still report the fatal ImportError condition"
    )
    assert extractor._spacy_not_importable is True, (
        "the ImportError probe must latch so it is not re-run per file"
    )


# ---------------------------------------------------------------------------
# C1-I-4 / T2: the spaCy NER-CALL failure (as opposed to the model-LOAD
# failure above) must degrade too, and its traceback log must latch the same
# way — this path had zero coverage before.
# ---------------------------------------------------------------------------


def test_extractor_ner_call_failure_degrades_and_preserves_code_symbols() -> None:
    """T2: a raising NER call degrades — fatal_error stays None, the warning
    names the failure, code-symbol nodes/mentions survive, and no co-occurrence
    edges are fabricated from a partial/absent NER result."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())

    def _raising_nlp(text: str) -> None:
        raise RuntimeError("boom: NER call failed")

    extractor._nlp = _raising_nlp

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
    assert any("spaCy NER failed" in w for w in result.warnings), (
        f"expected the NER-failure warning; got {result.warnings!r}"
    )
    assert "parse_config" in {n.entity_name for n in result.nodes}, (
        "code-symbol extraction must survive an NER-call failure"
    )
    assert result.mentions and all(
        m.entity_id in {n.id for n in result.nodes} for m in result.mentions
    ), "the code-symbol mention must still be recorded"
    assert result.edges == [], (
        "no co-occurrence edges may be fabricated from a failed NER call"
    )


def test_extractor_ner_call_failure_log_latches_across_documents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T2: the NER-call-failure traceback logs once per process (C1-I-4), but
    the per-document `warnings` entry fires every time."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())

    def _raising_nlp(text: str) -> None:
        raise RuntimeError("boom: NER call failed")

    extractor._nlp = _raising_nlp

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run() -> list[list[str]]:
        return [
            (await extractor.extract([chunk], f"doc-{i}", "col")).warnings for i in range(3)
        ]

    with caplog.at_level(logging.WARNING, logger="archon_search.graph_extractor"):
        per_doc_warnings = asyncio.run(_run())

    assert all(
        any("spaCy NER failed" in w for w in doc_warnings) for doc_warnings in per_doc_warnings
    ), "every document must still get the warning in its own result"

    warning_logs = [r for r in caplog.records if "spaCy NER failed" in r.getMessage()]
    assert len(warning_logs) == 1, (
        f"expected exactly one WARNING-level NER-failure log across 3 documents; got "
        f"{len(warning_logs)}: {[r.getMessage() for r in warning_logs]}"
    )


# ---------------------------------------------------------------------------
# 2026-08-19-030: model resolution order — package → data-dir path → degrade
# ---------------------------------------------------------------------------


def _place_data_dir_model(
    data_dir: Path, version: str = "3.8.0", spacy_version_spec: str | None = None
) -> Path:
    """Create a wizard-provisioned model directory under *data_dir* and return it.

    ``spacy_version_spec``, when given, writes a ``meta.json`` with that
    ``spacy_version`` compatibility range (e.g. ``">=3.9.0,<3.10.0"``) — the
    field :func:`resolve_spacy_model` checks (C1-I-3). Omitted by default:
    most tests don't care about compatibility and a missing ``meta.json``
    reads as compatible (fail open).
    """
    import json as _json

    model_dir = data_dir / "models" / "spacy" / f"en_core_web_sm-{version}"
    model_dir.mkdir(parents=True)
    (model_dir / "config.cfg").write_text("[nlp]\nlang = 'en'\n")
    if spacy_version_spec is not None:
        (model_dir / "meta.json").write_text(
            _json.dumps({"version": version, "spacy_version": spacy_version_spec})
        )
    return model_dir


def test_find_spacy_model_prefers_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed package wins over a data-dir copy — back-compat for pip installs."""
    from archon_search.graph_extractor import find_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path)

    with patch.dict(sys.modules, _make_spacy_stub(installed_models=["en_core_web_sm"])):
        assert find_spacy_model() == "en_core_web_sm"


def test_find_spacy_model_falls_back_to_data_dir_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no installed package, the wizard-provisioned directory is used."""
    from archon_search.graph_extractor import find_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    model_dir = _place_data_dir_model(tmp_path)

    with patch.dict(sys.modules, _make_spacy_stub(installed_models=[])):
        assert find_spacy_model() == str(model_dir)


def test_find_spacy_model_picks_newest_data_dir_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several provisioned versions → the newest one loads, by VERSION not lexicographic order.

    2026-08-19-030 C1-I-2: ``3.9.0`` vs ``3.10.0`` is a genuine discriminator —
    lexicographic string comparison ranks ``"3.10.0" < "3.9.0"`` (`'1' < '9'`),
    so a test using e.g. 3.7.0/3.8.0 would pass under BOTH orderings and
    certify nothing about the fix.
    """
    from archon_search.graph_extractor import find_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path, version="3.9.0")
    newest = _place_data_dir_model(tmp_path, version="3.10.0")

    with patch.dict(sys.modules, _make_spacy_stub(installed_models=[])):
        assert find_spacy_model() == str(newest)


def test_find_spacy_model_rejects_lexicographic_near_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The near-term reachable case from C1-I-2: 3.8.2 vs 3.8.10 — lexicographic
    string sort ranks '3.8.10' < '3.8.2' (`'1' < '2'`), so a buggy resolver would
    pick 3.8.2 as "newest". The version-aware key must pick 3.8.10.
    """
    from archon_search.graph_extractor import find_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path, version="3.8.2")
    newest = _place_data_dir_model(tmp_path, version="3.8.10")

    with patch.dict(sys.modules, _make_spacy_stub(installed_models=[])):
        assert find_spacy_model() == str(newest)


def test_resolve_spacy_model_rejects_incompatible_data_dir_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-I-3: a stale model dir whose meta.json spacy_version excludes the
    installed spaCy must not resolve — GET /status must not report it healthy.
    """
    from archon_search.graph_extractor import resolve_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path, version="3.7.0", spacy_version_spec=">=3.7.0,<3.8.0")

    with patch.dict(
        sys.modules, _make_spacy_stub(installed_models=[], spacy_version="3.9.4")
    ):
        resolution = resolve_spacy_model()

    assert resolution.target is None
    assert resolution.incompatible_versions == ["en_core_web_sm-3.7.0"]


def test_resolve_spacy_model_skips_incompatible_and_uses_compatible_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compatible version alongside an incompatible one still resolves —
    incompatible candidates are excluded, not merely deprioritized."""
    from archon_search.graph_extractor import resolve_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path, version="3.7.0", spacy_version_spec=">=3.7.0,<3.8.0")
    compatible = _place_data_dir_model(
        tmp_path, version="3.9.0", spacy_version_spec=">=3.9.0,<3.10.0"
    )

    with patch.dict(
        sys.modules, _make_spacy_stub(installed_models=[], spacy_version="3.9.4")
    ):
        resolution = resolve_spacy_model()

    assert resolution.target == str(compatible)
    assert resolution.incompatible_versions == ["en_core_web_sm-3.7.0"]


def test_extractor_incompatible_model_warns_distinctly_from_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-I-3: the operator-facing warning distinguishes "present but
    incompatible" from "absent entirely" — the fix (re-provision vs provision)
    differs."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    _place_data_dir_model(tmp_path, version="3.7.0", spacy_version_spec=">=3.7.0,<3.8.0")

    extractor = GraphExtractor(GraphConfig())
    stub = _make_spacy_stub(installed_models=[], spacy_version="3.9.4")
    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, stub):
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert result.fatal_error is None
    assert any("incompatible" in w for w in result.warnings), (
        f"Expected an incompatible-version warning distinct from 'absent'; got {result.warnings!r}"
    )


def test_find_spacy_model_returns_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither installed nor provisioned → None, so the caller degrades."""
    from archon_search.graph_extractor import find_spacy_model

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    with patch.dict(sys.modules, _make_spacy_stub(installed_models=[])):
        assert find_spacy_model() is None


def test_extractor_loads_model_from_data_dir_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: extract() runs NER off the data-dir model with nothing installed."""
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    model_dir = _place_data_dir_model(tmp_path)

    load_args: list[str] = []
    stub = _make_spacy_stub(
        entities_by_text={"Alice works at Acme.": [("Alice", "PERSON")]},
        installed_models=[],
    )
    _inner_load = stub["spacy"].load

    def _recording_load(name: str):
        load_args.append(name)
        return _inner_load(name)

    stub["spacy"].load = _recording_load  # type: ignore[attr-defined]

    extractor = GraphExtractor(GraphConfig())
    chunk = ChunkInput(
        chunk_id="c1", text="Alice works at Acme.", symbol_type=None, symbol_subtype=None
    )

    async def _run():
        with patch.dict(sys.modules, stub):
            return await extractor.extract([chunk], "doc-1", "col")

    result = asyncio.run(_run())

    assert load_args == [str(model_dir)], (
        f"spacy.load must be called with the data-dir path; got {load_args}"
    )
    assert result.fatal_error is None
    assert {n.entity_name for n in result.nodes} == {"Alice"}


def test_extractor_cancelled_load_propagates_and_does_not_latch() -> None:
    """C2-I-1: `_ensure_nlp` catches `BaseException` around the load so a
    `SystemExit` degrades (see the test above) — but `asyncio.CancelledError`
    is a `BaseException` too, and swallowing it would be wrong twice over.

    Cancellation says nothing about the model. If it were caught, (a) the
    cancelled task would return normally instead of unwinding, breaking
    structured concurrency, and (b) `_nlp_unavailable` would latch for the
    whole process, disabling prose NER for a model that is present and
    healthy — because a shutdown or job-cancel happened to land in the load
    window, which is the slowest part of the whole ingest path.
    """
    from archon_search.config import GraphConfig
    from archon_search.graph_extractor import GraphExtractor

    extractor = GraphExtractor(GraphConfig())
    extractor._load_nlp_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError()
    )

    chunk = ChunkInput(chunk_id="c1", text="Alice met Bob.", symbol_type=None, symbol_subtype=None)

    async def _run():
        with patch.dict(sys.modules, {"spacy": types.ModuleType("spacy")}):
            return await extractor.extract([chunk], "doc-1", "col")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert extractor._nlp_unavailable is False, (
        "A cancelled load must not latch the extractor as degraded — the model "
        "was never determined to be unavailable"
    )
    assert extractor._nlp_unavailable_warning is None, (
        "A cancelled load must not arm a wire-facing 'model unavailable' notice"
    )

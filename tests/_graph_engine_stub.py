"""Shared ``ProseExtractionBackend``-boundary stub for graph-enabled tests.

BE-11 rewired ``GraphExtractor`` from spaCy onto ``ProseExtractionBackend``/
gliner. Before that, integration tests stubbed a fake ``spacy`` module into
``sys.modules`` (patching the ``import spacy`` inside ``_check_graph_deps``)
to get deterministic entities without a real model load. That stub has had
zero effect since BE-11 — ``GraphExtractor`` never imports spacy — so those
tests were silently falling through to the real gliner engine (hanging or
timing out before a shared model cache existed, or diverging in output from
what the spaCy fake used to produce).

This module patches the real seam instead: ``ProseExtractionBackend``, which
``GraphExtractor.__init__`` constructs directly
(``archon_search/graph_extractor.py``). Patching the class means every
``GraphExtractor`` created anywhere during the test — including deep inside
``create_app()`` via ``make_real_app()`` — gets the stub, with no real gliner
import, no model load, and no network access.
"""
from __future__ import annotations

import pytest

from archon_search.prose_extraction_backend import (
    BatchExtraction,
    ChunkExtraction,
    ExtractedEntity,
)

#: Labels must be valid ``EntityType`` enum values (``archon_search.graph_types``)
#: — the real engine's own vocabulary, lowercase, with no "ORG"/"PERSON"
#: spaCy-style categories. GraphExtractor silently skips any label that
#: doesn't map to EntityType (BE-11), so a spaCy-style label here would
#: produce zero graph nodes with no error.
_DEFAULT_ENTITY_MAP: list[tuple[str, str]] = [("Alice", "person"), ("Google", "concept")]


class _UnconditionalStubBackend:
    """Returns the same fixed entity list for every chunk, regardless of
    content — mirrors the original ``_install_spacy_stub`` helper's
    (imprecise, content-blind) behavior exactly, for tests that rely on it."""

    def __init__(self, entities: list[ExtractedEntity]) -> None:
        self._entities = entities

    async def load(self) -> None:
        return None

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        chunks = [ChunkExtraction(entities=list(self._entities)) for _ in texts]
        return BatchExtraction(chunks=chunks, truncated=False)


class _CallbackStubBackend:
    """Delegates per-chunk entity selection to an arbitrary callable — for
    stubs whose routing logic is more than a plain substring match (e.g. a
    fake static-analysis extractor that infers a dependency even when its
    name doesn't literally appear in the chunk text)."""

    def __init__(self, entities_for_text) -> None:
        self._entities_for_text = entities_for_text

    async def load(self) -> None:
        return None

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        chunks = []
        for text in texts:
            entities = [
                ExtractedEntity(text=name, label=label, start=0, end=len(name), score=0.9)
                for name, label in self._entities_for_text(text)
            ]
            chunks.append(ChunkExtraction(entities=entities))
        return BatchExtraction(chunks=chunks, truncated=False)


class _ContentAwareStubBackend:
    """Returns entities whose surface text literally occurs in each chunk —
    mirrors real gliner behavior (an entity is only tagged where its surface
    form actually occurs) without running a real model. Mirrors the original
    ``_install_spacy_stub_multi_entity`` helper's behavior."""

    def __init__(self, entity_map: list[tuple[str, str]]) -> None:
        self._entity_map = entity_map

    async def load(self) -> None:
        return None

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        chunks = []
        for text in texts:
            entities = []
            for name, label in self._entity_map:
                idx = text.find(name)
                if idx != -1:
                    entities.append(
                        ExtractedEntity(
                            text=name, label=label, start=idx, end=idx + len(name), score=0.9
                        )
                    )
            chunks.append(ChunkExtraction(entities=entities))
        return BatchExtraction(chunks=chunks, truncated=False)


def _patch(monkeypatch: pytest.MonkeyPatch, stub: object) -> None:
    def _factory(*args: object, **kwargs: object) -> object:
        return stub

    monkeypatch.setattr("archon_search.graph_extractor.ProseExtractionBackend", _factory)


def install_graph_engine_stub(
    monkeypatch: pytest.MonkeyPatch,
    entity_map: list[tuple[str, str]] | None = None,
) -> None:
    """Every GraphExtractor built during this test returns two fixed
    entities — Alice (person) and Google (concept) by default, or the pairs in
    ``entity_map`` — for ANY chunk, regardless of its text. Matches the
    original ``_install_spacy_stub``/``_install_spacy_stub_with_entities``
    helpers.
    """
    entities = [
        ExtractedEntity(text=name, label=label, start=0, end=len(name), score=0.9)
        for name, label in (entity_map or _DEFAULT_ENTITY_MAP)
    ]
    _patch(monkeypatch, _UnconditionalStubBackend(entities))


def install_graph_engine_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every GraphExtractor built during this test returns zero entities for
    any chunk. Matches the original ``_install_spacy_stub_no_entities``
    helper.
    """
    _patch(monkeypatch, _UnconditionalStubBackend([]))


def install_graph_engine_stub_custom(monkeypatch: pytest.MonkeyPatch, entities_for_text) -> None:
    """Every GraphExtractor built during this test tags whatever
    ``entities_for_text(text) -> list[(name, label)]`` returns for each
    chunk's text. Use this only when substring matching
    (``install_graph_engine_stub_content_aware``) can't express the routing
    logic a test needs — e.g. a fake static-analysis extractor that infers a
    dependency even when its name doesn't literally appear in the text.
    ``label`` must be a valid ``EntityType`` value (lowercase: "person",
    "concept", "system", "event", "code_symbol").
    """
    _patch(monkeypatch, _CallbackStubBackend(entities_for_text))


def install_graph_engine_stub_content_aware(
    monkeypatch: pytest.MonkeyPatch,
    entity_map: list[tuple[str, str]] | None = None,
) -> None:
    """Every GraphExtractor built during this test tags only the entities in
    ``entity_map`` (default: Alice/person, Google/concept) whose surface text
    literally occurs in a given chunk — so a code chunk with neither name
    yields zero entities. Matches the original
    ``_install_spacy_stub_multi_entity`` helper.
    """
    _patch(monkeypatch, _ContentAwareStubBackend(entity_map or _DEFAULT_ENTITY_MAP))

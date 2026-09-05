"""Shared engine stubs for graph-enabled tests — one parameterised factory.

``GraphExtractor`` extracts prose entities/relations through
``ProseExtractionBackend``/gliner (``archon_search/graph_extractor.py``). Tests
need deterministic extraction without a real model load, at one of two depths
(Q33):

* **Shallow** — ``install_graph_engine_stub`` patches the
  ``ProseExtractionBackend`` *class* that ``GraphExtractor.__init__``
  constructs, so every ``GraphExtractor`` built anywhere during the test —
  including deep inside ``create_app()`` via ``make_real_app()`` — returns a
  fixed ``BatchExtraction`` with no real gliner import, model load, or network.
  One parameterised entry point (payload injectable, explicit empty mode,
  entity/relationship type set directly) replaces the per-file stub factories
  the migration deleted.

* **Deep** — ``install_deep_gliner_stub`` injects a fake ``gliner`` module at
  the package boundary and lets ``ProseExtractionBackend`` run its *real*
  sub-batching / truncation / threshold-passthrough logic against a recording
  model. Use this only for the scenarios whose assertions are about the
  backend's own behaviour (S7, S20, S21, S29, S53); the fake's return shape
  mirrors gliner's real ``(entities, relations)`` per-text shape, recorded
  verbatim from the installed package (K2d) — never invented.

Labels are the engine's own vocabulary — valid ``EntityType`` values
(lowercase: ``person``/``concept``/``system``/``event``/``code_symbol``) set
directly, not a mapping-table category. ``GraphExtractor`` silently skips any
label that is not an ``EntityType``, so a wrong label yields zero graph nodes
with no error.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Callable

import pytest

from archon_search.prose_extraction_backend import (
    BatchExtraction,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)

#: ``(surface_text, entity_type)`` — the entity payload a caller injects.
EntitySpec = tuple[str, str]
#: ``(head_text, tail_text, relationship_type)`` — an injected relation triple.
RelationSpec = tuple[str, str, str]
#: A caller may inject a fixed list, or a callable computing specs per chunk.
EntityPayload = list[EntitySpec] | Callable[[str], list[EntitySpec]]
RelationPayload = list[RelationSpec] | Callable[[str], list[RelationSpec]]

_DEFAULT_ENTITIES: list[EntitySpec] = [("Alice", "person"), ("Google", "concept")]


# ---------------------------------------------------------------------------
# Shallow fake — at the ``ProseExtractionBackend`` seam
# ---------------------------------------------------------------------------


def _spans_for_chunk(
    text: str, entities: list[EntitySpec], content_aware: bool, *, drop_absent: bool = True
) -> list[ExtractedEntity]:
    spans: list[ExtractedEntity] = []
    for name, label in entities:
        start = text.find(name) if content_aware else 0
        if start == -1:
            if drop_absent:
                continue
            start = 0
        spans.append(
            ExtractedEntity(text=name, label=label, start=start, end=start + len(name), score=0.9)
        )
    return spans


def _relations_for_chunk(specs: list[RelationSpec]) -> list[ExtractedRelation]:
    return [
        ExtractedRelation(head=head, tail=tail, label=label, score=0.9)
        for head, tail, label in specs
    ]


class _StubBackend:
    """Content-blind or content-aware ``ProseExtractionBackend`` replacement.

    Resolves the injected entity/relation payloads — a fixed list applied to
    every chunk, or a callable computing them from each chunk's text — into an
    index-aligned ``BatchExtraction``.
    """

    def __init__(
        self,
        entities: EntityPayload,
        relations: RelationPayload,
        content_aware: bool,
    ) -> None:
        self._entities = entities
        self._relations = relations
        self._content_aware = content_aware

    async def load(self) -> None:
        return None

    async def inference(
        self, texts: list[str], ner_confidence: float, relation_confidence: float
    ) -> BatchExtraction:
        chunks: list[ChunkExtraction] = []
        for text in texts:
            if callable(self._entities):
                # A callable owns its own routing: compute real offsets where the
                # surface text occurs, fall back to start=0 when it does not, but
                # never drop — content_aware does not apply.
                entity_spans = _spans_for_chunk(
                    text, self._entities(text), content_aware=True, drop_absent=False
                )
            else:
                entity_spans = _spans_for_chunk(text, self._entities, self._content_aware)
            relations = (
                self._relations(text) if callable(self._relations) else self._relations
            )
            chunks.append(
                ChunkExtraction(
                    entities=entity_spans,
                    relations=_relations_for_chunk(relations),
                )
            )
        return BatchExtraction(chunks=chunks, truncated=False)


def install_graph_engine_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entities: EntityPayload | None = None,
    relations: RelationPayload | None = None,
    content_aware: bool = False,
    empty: bool = False,
) -> None:
    """Make every ``GraphExtractor`` built during this test extract a fixed
    payload, with no real gliner load.

    * ``empty=True`` — zero entities and relations for any chunk.
    * ``entities`` — a list of ``(text, entity_type)`` applied to every chunk,
      or a callable ``(chunk_text) -> list[(text, entity_type)]``. Defaults to
      ``Alice``/person + ``Google``/concept.
    * ``relations`` — a list of ``(head, tail, relationship_type)`` or a
      callable; default none.
    * ``content_aware=True`` — tag a list entity only where its surface text
      literally occurs in the chunk (real offsets), mirroring the real engine;
      otherwise it is tagged unconditionally at offset 0. Ignored for a
      callable payload, which owns its own routing.

    ``entity_type``/``relationship_type`` are set directly — the engine's own
    lowercase vocabulary, never a mapping-table category.
    """
    if empty:
        resolved_entities: EntityPayload = []
    elif entities is None:
        resolved_entities = _DEFAULT_ENTITIES
    else:
        resolved_entities = entities
    resolved_relations: RelationPayload = [] if (empty or relations is None) else relations
    stub = _StubBackend(resolved_entities, resolved_relations, content_aware and not empty)

    def _factory(*args: object, **kwargs: object) -> _StubBackend:
        return stub

    monkeypatch.setattr("archon_search.graph_extractor.ProseExtractionBackend", _factory)


# ---------------------------------------------------------------------------
# Deep fake — at the ``gliner`` module boundary
# ---------------------------------------------------------------------------

# Sample-payload factories in gliner's real return shape, recorded verbatim from
# the installed package (K2d): entities are a per-text list of
# ``{start, end, text, label, score}`` (ONNX path); relations are
# ``{head, tail, relation, score}`` where each nested span keys its type as
# ``type`` — not ``label`` — a real, observed naming inconsistency, preserved here.


def gliner_entity(text: str, label: str, *, start: int = 0, score: float = 0.9) -> dict:
    """One entity dict in gliner's real shape (start, end, text, label, score)."""
    return {"start": start, "end": start + len(text), "text": text, "label": label, "score": score}


def gliner_relation_span(text: str, type_: str, *, entity_idx: int = 0, start: int = 0) -> dict:
    """One relation-endpoint span in gliner's real shape (start, end, text, type, entity_idx)."""
    return {"start": start, "end": start + len(text), "text": text, "type": type_, "entity_idx": entity_idx}


def gliner_relation(
    head: str,
    tail: str,
    relation: str,
    *,
    score: float = 0.9,
    head_type: str = "system",
    tail_type: str = "system",
) -> dict:
    """One relation dict in gliner's real shape (head, tail, relation, score)."""
    return {
        "head": gliner_relation_span(head, head_type, entity_idx=0),
        "tail": gliner_relation_span(tail, tail_type, entity_idx=1),
        "relation": relation,
        "score": score,
    }


class RecordingGlinerModel:
    """Deep fake positioned at the gliner module boundary — records every
    ``inference()`` call so a test can assert the real backend's sub-batching,
    truncation, and threshold pass-through, and returns injected payloads in
    gliner's real ``(entities, relations)`` per-text shape.

    The signature mirrors the installed ``gliner==0.2.28``
    ``UniEncoderSpanRelexGLiNER.inference`` declaration explicitly rather than
    swallowing arguments in ``**kwargs``, so a threshold/label wiring
    regression surfaces as a captured value and an argument gliner does not
    accept fails loudly.
    """

    def __init__(
        self,
        entities_for_text: Callable[[str], list[dict]] | None = None,
        relations_for_text: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.device: object | None = None
        self._entities_for_text = entities_for_text or (lambda _text: [])
        self._relations_for_text = relations_for_text or (lambda _text: [])

    def to(self, device: object) -> RecordingGlinerModel:
        # Record the provider-resolved device so S29 wiring is inspectable.
        self.device = device
        return self

    def inference(
        self,
        texts,
        labels,
        relations=(),
        flat_ner=True,
        threshold=0.5,
        adjacency_threshold=None,
        relation_threshold=None,
        multi_label=False,
        batch_size=8,
        packing_config=None,
        input_spans=None,
        return_relations=True,
        return_class_probs=False,
    ):
        self.calls.append(list(texts))
        self.kwargs.append(
            {
                "labels": list(labels),
                "relations": list(relations),
                "threshold": threshold,
                "relation_threshold": relation_threshold,
                "adjacency_threshold": adjacency_threshold,
                "batch_size": batch_size,
                "return_relations": return_relations,
            }
        )
        entities = [self._entities_for_text(text) for text in texts]
        rels = [self._relations_for_text(text) for text in texts]
        return entities, rels


def install_deep_gliner_stub(monkeypatch: pytest.MonkeyPatch, model: object) -> None:
    """Inject a fake ``gliner`` module whose ``GLiNER.from_pretrained`` returns
    ``model``, so ``ProseExtractionBackend.load()`` runs its real logic against
    the deep fake — no real model download, but every sub-batch, truncation,
    and threshold decision is the backend's own. torch stays real (it is a dev
    dependency and the load only pins its thread counts).
    """
    gliner_mod = types.ModuleType("gliner")
    gliner_cls = types.SimpleNamespace(from_pretrained=lambda *a, **k: model)
    setattr(gliner_mod, "GLiNER", gliner_cls)
    monkeypatch.setitem(sys.modules, "gliner", gliner_mod)

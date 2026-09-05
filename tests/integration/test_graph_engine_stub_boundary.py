"""Integration test: the deep graph-engine stub is pinned at the ``gliner``
module boundary, so unit scenarios exercise the *real* ``ProseExtractionBackend``
seam (sub-batching, threshold pass-through) rather than the stub's own logic
(BE-16 / Q33 / S7 / S21)."""
from __future__ import annotations

import math

import pytest

from archon_search.prose_extraction_backend import (
    GRAPH_NER_SUB_BATCH_SIZE,
    ProseExtractionBackend,
)
from tests._graph_engine_stub import (
    RecordingGlinerModel,
    gliner_entity,
    gliner_relation,
    install_deep_gliner_stub,
)

pytestmark = pytest.mark.integration


async def test_stub_is_pinned_at_the_package_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deep fake injected at ``sys.modules['gliner']`` lets the real backend
    load and sub-batch. The recording model proves the backend's own logic ran:
    it is invoked ``ceil(N / GRAPH_NER_SUB_BATCH_SIZE)`` times with the configured
    thresholds — behaviour a shallow ``ProseExtractionBackend`` stub could not
    exercise."""
    model = RecordingGlinerModel(
        entities_for_text=lambda _t: [gliner_entity("Acme", "system")],
        relations_for_text=lambda _t: [gliner_relation("Acme", "Beta", "uses")],
    )
    install_deep_gliner_stub(monkeypatch, model)

    n_chunks = GRAPH_NER_SUB_BATCH_SIZE * 2 + 1
    texts = [f"Acme depends on Beta in service {i}." for i in range(n_chunks)]

    backend = ProseExtractionBackend()
    await backend.load()  # imports the fake gliner, runs the real loader path
    result = await backend.inference(texts, ner_confidence=0.31, relation_confidence=0.42)

    # Real sub-batching ran at the gliner boundary — not one call per chunk,
    # not a single shallow call.
    assert len(model.calls) == math.ceil(n_chunks / GRAPH_NER_SUB_BATCH_SIZE)
    assert all(len(call) <= GRAPH_NER_SUB_BATCH_SIZE for call in model.calls)
    # Configured thresholds are passed straight through to the engine.
    assert model.kwargs[0]["threshold"] == 0.31
    assert model.kwargs[0]["relation_threshold"] == 0.42
    # The real backend decoded the fake's gliner-shaped payload into the domain type.
    assert len(result.chunks) == n_chunks
    assert result.chunks[0].entities[0].text == "Acme"
    assert result.chunks[0].relations[0].label == "uses"

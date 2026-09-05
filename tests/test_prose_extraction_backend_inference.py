"""tests/test_prose_extraction_backend_inference.py — unit/integration tests for
ProseExtractionBackend.inference() (Task BE-9).

Deep fake at the gliner boundary (Q33): tests set `backend._model` directly to a
fake object whose `.inference()` matches gliner's real return shape — a
(entities, relations) tuple of per-text lists, where each entity dict is
{start, end, text, label, score} and each relation dict is
{head: {..., text}, tail: {..., text}, relation, score} — recorded verbatim
from the installed `gliner==0.2.28` package (UniEncoderSpanRelexGLiNER.inference,
gliner/model.py:5434, map_entities_to_text:5382-5388, _process_relations:5680-5698).
"""
from __future__ import annotations

import logging

import pytest

from archon_search.prose_extraction_backend import (
    GRAPH_NER_SUB_BATCH_SIZE,
    GRAPH_NER_TOKEN_WINDOW_WORDS,
    ExtractedRelation,
    ProseExtractionBackend,
    _TRUNCATION_LOG_MESSAGE,
)
from tests._graph_engine_stub import RecordingGlinerModel


def _entity(text: str, label: str, start: int, end: int, score: float = 0.9) -> dict:
    return {"start": start, "end": end, "text": text, "label": label, "score": score}


def _relation(
    head: str,
    tail: str,
    relation: str,
    score: float = 0.8,
    head_type: str = "concept",
    tail_type: str = "concept",
) -> dict:
    return {
        "head": {"start": 0, "end": len(head), "text": head, "type": head_type, "entity_idx": 0},
        "tail": {"start": 0, "end": len(tail), "text": tail, "type": tail_type, "entity_idx": 1},
        "relation": relation,
        "score": score,
    }


def _backend_with_model(model) -> ProseExtractionBackend:
    backend = ProseExtractionBackend()
    backend._model = model  # bypass load() — deep fake at the gliner boundary
    return backend


# ---------------------------------------------------------------------------
# S7 — ceil(N / GRAPH_NER_SUB_BATCH_SIZE) calls at the gliner boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_yields_ceil_n_over_sub_batch_calls() -> None:
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)
    n_chunks = GRAPH_NER_SUB_BATCH_SIZE * 3 + 2  # not an exact multiple
    texts = [f"chunk {i}" for i in range(n_chunks)]

    await backend.inference(texts, ner_confidence=0.5, relation_confidence=0.5)

    import math

    assert len(fake_model.calls) == math.ceil(n_chunks / GRAPH_NER_SUB_BATCH_SIZE)
    assert len(fake_model.calls) < n_chunks  # never once per chunk


# ---------------------------------------------------------------------------
# S53 — no single call exceeds the sub-batch constant; not a config key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_single_call_exceeds_the_sub_batch_constant() -> None:
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)
    n_chunks = GRAPH_NER_SUB_BATCH_SIZE * 4 + 1
    texts = [f"chunk {i}" for i in range(n_chunks)]

    await backend.inference(texts, ner_confidence=0.5, relation_confidence=0.5)

    assert all(len(call) <= GRAPH_NER_SUB_BATCH_SIZE for call in fake_model.calls)


def test_sub_batch_size_is_not_a_graph_config_key() -> None:
    """Introspective, so it fails if ANY `*sub_batch_size*` field is ever added
    to GraphConfig — not just three guessed names."""
    from archon_search.config import GraphConfig

    offending = [
        name
        for name in GraphConfig.__dataclass_fields__
        if "sub_batch_size" in name or "batch_size" in name
    ]
    assert offending == [], f"GRAPH_NER_SUB_BATCH_SIZE leaked into [graph] config: {offending}"


# ---------------------------------------------------------------------------
# "other" decoy discard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_labelled_spans_are_discarded() -> None:
    class _FakeModel:
        def inference(self, texts, labels, relations=None, **kwargs):
            assert "other" in labels  # the decoy is actually prompted
            entities = [
                [
                    _entity("Acme Corp", "system <> a named software system, product, service, or platform", 0, 9),
                    _entity("some noise", "other", 10, 20),
                ]
            ]
            return entities, [[]]

    backend = _backend_with_model(_FakeModel())

    result = await backend.inference(["Acme Corp handles some noise."], 0.5, 0.5)

    assert len(result.chunks) == 1
    labels = [e.label for e in result.chunks[0].entities]
    assert "other" not in labels
    assert labels == ["system"]


# ---------------------------------------------------------------------------
# Relation-triple deduplication — dedupe key is (head, tail, label)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_relation_triples_are_deduplicated() -> None:
    class _FakeModel:
        def inference(self, texts, labels, relations=None, **kwargs):
            dup = _relation("Kafka", "Zookeeper", "depends_on <> the head requires the tail to function", 0.77)
            return [[]], [[dup, dict(dup), dict(dup)]]

    backend = _backend_with_model(_FakeModel())

    result = await backend.inference(["Kafka depends on Zookeeper."], 0.5, 0.5)

    assert len(result.chunks[0].relations) == 1
    assert result.chunks[0].relations[0] == ExtractedRelation(
        head="Kafka", tail="Zookeeper", label="depends_on", score=0.77
    )


@pytest.mark.asyncio
async def test_distinct_triples_over_the_same_pair_both_survive() -> None:
    class _FakeModel:
        def inference(self, texts, labels, relations=None, **kwargs):
            uses = _relation("ServiceA", "ServiceB", "uses <> the head uses or depends on the tail at runtime")
            depends = _relation("ServiceA", "ServiceB", "depends_on <> the head requires the tail to function")
            return [[]], [[uses, depends]]

    backend = _backend_with_model(_FakeModel())

    result = await backend.inference(["ServiceA uses and depends on ServiceB."], 0.5, 0.5)

    labels = {r.label for r in result.chunks[0].relations}
    assert labels == {"uses", "depends_on"}
    assert len(result.chunks[0].relations) == 2


@pytest.mark.asyncio
async def test_duplicate_triples_do_not_inflate_edge_count() -> None:
    """Integration-shaped WITHIN BE-9's own scope: dedup holds across a real
    multi-sub-batch ``inference()`` call (several chunks, several relations
    per chunk, each with duplicate engine output), not just a single relation
    in a single chunk.

    NOTE — deliberately narrower than the task file's Tests-block description
    ("persists the same edge count ... exercising C2's edge-id idempotence").
    That phrasing describes a real `GraphStore`-backed ingest, which does not
    exist on this seam yet: BE-9 only produces `ExtractedRelation` objects,
    and `graph_extractor.py` is rewired onto this backend in BE-11 (needs
    BE-9, BE-10). Asserting persisted edge counts here would require faking
    GraphStore behaviour, which is out of BE-9's scope. C2's edge-id
    idempotence guard belongs to BE-11's own integration tests once that
    wiring lands; this test is BE-9's guard that the *deduplicated relation
    objects themselves* are correct across realistic multi-chunk, multi-batch
    conditions.
    """

    class _DupModel:
        def inference(self, texts, labels, relations=None, **kwargs):
            entities = [[] for _ in texts]
            rels = []
            for text in texts:
                # Chunk index is recovered from the text itself, not sub-batch
                # position — a sub-batch call only ever sees its own slice, so
                # an index derived from `enumerate(texts)` would restart at 0
                # for every sub-batch instead of tracking the whole document.
                idx = text.split()[0][1:]  # "A7 uses ..." -> "7"
                uses = _relation(f"A{idx}", f"B{idx}", "uses <> the head uses or depends on the tail at runtime")
                depends = _relation(f"A{idx}", f"B{idx}", "depends_on <> the head requires the tail to function")
                # Each relation duplicated 3x, mirroring gliner's measured 2x-4x
                # duplicate-triple behaviour (K2d) — across every sub-batch.
                rels.append([uses, dict(uses), dict(uses), depends, dict(depends)])
            return entities, rels

    n_chunks = GRAPH_NER_SUB_BATCH_SIZE + 3  # forces >1 sub-batch call
    texts = [f"A{i} uses and depends on B{i}." for i in range(n_chunks)]
    backend = _backend_with_model(_DupModel())

    result = await backend.inference(texts, 0.5, 0.5)

    assert len(result.chunks) == n_chunks
    for i, chunk in enumerate(result.chunks):
        labels = {r.label for r in chunk.relations}
        assert labels == {"uses", "depends_on"}
        assert len(chunk.relations) == 2  # each duplicate group collapsed to 1
        heads = {r.head for r in chunk.relations}
        assert heads == {f"A{i}"}


# ---------------------------------------------------------------------------
# Truncation — once-per-process, sanitized, never carries chunk text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncation_logs_once_and_never_interpolates_chunk_text(caplog) -> None:
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)
    long_text = " ".join(f"word{i}" for i in range(GRAPH_NER_TOKEN_WINDOW_WORDS + 500))
    another_long_text = " ".join(f"other{i}" for i in range(GRAPH_NER_TOKEN_WINDOW_WORDS + 10))

    with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
        await backend.inference([long_text, "short chunk"], 0.5, 0.5)
        await backend.inference([another_long_text], 0.5, 0.5)

    truncation_records = [
        r for r in caplog.records if r.name == "archon_search.prose_extraction_backend"
        and r.getMessage() == _TRUNCATION_LOG_MESSAGE
    ]
    assert len(truncation_records) == 1
    # The emitted record equals the pinned sanitized constant exactly — no
    # interpolated chunk text anywhere in the message.
    assert "word0" not in truncation_records[0].getMessage()
    assert "other0" not in truncation_records[0].getMessage()

    # Actually sent to the model must be truncated to the pinned word window.
    sent_texts = [t for call in fake_model.calls for t in call]
    assert all(len(t.split()) <= GRAPH_NER_TOKEN_WINDOW_WORDS for t in sent_texts)


@pytest.mark.asyncio
async def test_truncated_text_is_an_exact_prefix_of_the_original_chunk() -> None:
    """Whitespace runs must survive truncation byte-for-byte: gliner's returned
    char offsets index the truncated string, which must therefore be a real
    substring of the chunk text the rest of the pipeline stores."""
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)
    # Irregular whitespace: double spaces, a newline, a tab.
    chunk = "".join(
        f"word{i}{'  ' if i % 3 == 0 else chr(10) if i % 7 == 0 else chr(9) if i % 5 == 0 else ' '}"
        for i in range(GRAPH_NER_TOKEN_WINDOW_WORDS + 50)
    )

    await backend.inference([chunk], 0.5, 0.5)

    sent = fake_model.calls[0][0]
    assert sent != chunk  # truncation actually fired
    assert chunk.startswith(sent)  # exact prefix, not a normalised rebuild
    assert len(sent.split()) == GRAPH_NER_TOKEN_WINDOW_WORDS
    assert "\n" in sent and "  " in sent  # the irregular runs are preserved


def test_truncation_latch_is_per_instance_not_shared_between_instances(caplog) -> None:
    """The latch is instance-level; the "once per process" property comes from
    the backend being a shared singleton, not from module state."""
    first = ProseExtractionBackend()
    second = ProseExtractionBackend()
    long_text = " ".join(f"w{i}" for i in range(GRAPH_NER_TOKEN_WINDOW_WORDS + 5))

    with caplog.at_level(logging.WARNING, logger="archon_search.prose_extraction_backend"):
        first._truncate_to_window(long_text)
        first._truncate_to_window(long_text)
        second._truncate_to_window(long_text)

    records = [r for r in caplog.records if r.getMessage() == _TRUNCATION_LOG_MESSAGE]
    assert len(records) == 2  # once per instance: 1 from `first`, 1 from `second`


# ---------------------------------------------------------------------------
# Relations whose endpoints are not real entities ("other" decoy leak)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relation_with_an_other_typed_endpoint_is_discarded() -> None:
    class _FakeModel(RecordingGlinerModel):
        def inference(self, texts, labels, **kwargs):
            super().inference(texts, labels, **kwargs)
            bad_tail = _relation(
                "Acme Corp", "some noise", "uses <> the head uses or depends on the tail at runtime",
                tail_type="other",
            )
            bad_head = _relation(
                "some noise", "Acme Corp", "uses <> the head uses or depends on the tail at runtime",
                head_type="other",
            )
            unknown = _relation(
                "Acme Corp", "Foo", "uses <> the head uses or depends on the tail at runtime",
                tail_type="code_symbol",
            )
            return [[]], [[bad_tail, bad_head, unknown]]

    backend = _backend_with_model(_FakeModel())

    result = await backend.inference(["Acme Corp handles some noise."], 0.5, 0.5)

    assert result.chunks[0].relations == []


@pytest.mark.asyncio
async def test_relation_between_two_real_labelled_entities_survives() -> None:
    class _FakeModel(RecordingGlinerModel):
        def inference(self, texts, labels, **kwargs):
            super().inference(texts, labels, **kwargs)
            good = _relation(
                "archon-search",
                "LanceDB",
                "uses <> the head uses or depends on the tail at runtime",
                score=0.98,
                head_type="system <> a named software system, product, service, or platform",
                tail_type="system <> a named software system, product, service, or platform",
            )
            return [[]], [[good]]

    backend = _backend_with_model(_FakeModel())

    result = await backend.inference(["archon-search uses LanceDB."], 0.5, 0.5)

    assert result.chunks[0].relations == [
        ExtractedRelation(head="archon-search", tail="LanceDB", label="uses", score=0.98)
    ]


# ---------------------------------------------------------------------------
# Threshold and label wiring reaching the gliner boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_thresholds_reach_the_model_unswapped() -> None:
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)

    await backend.inference(["a chunk"], ner_confidence=0.3, relation_confidence=0.9)

    assert fake_model.kwargs[0]["threshold"] == 0.3
    assert fake_model.kwargs[0]["relation_threshold"] == 0.9


@pytest.mark.asyncio
async def test_adjacency_threshold_is_not_passed_to_the_model() -> None:
    """K2g proved `adjacency_threshold` inert on this checkpoint (Q29 closed —
    "REMOVED, not pinned"), so it is deliberately never sent."""
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)

    await backend.inference(["a chunk"], 0.5, 0.5)

    assert fake_model.kwargs[0]["adjacency_threshold"] is None


@pytest.mark.asyncio
async def test_prompted_labels_and_relations_are_the_graphs_own() -> None:
    fake_model = RecordingGlinerModel()
    backend = _backend_with_model(fake_model)

    await backend.inference(["a chunk"], 0.5, 0.5)

    labels = fake_model.kwargs[0]["labels"]
    relations = fake_model.kwargs[0]["relations"]

    assert labels == [
        "person <> a named individual person",
        "concept <> an abstract idea, topic, or named concept",
        "system <> a named software system, product, service, or platform",
        "event <> a named occurrence or incident",
        "other",  # the bare decoy, no description
    ]
    assert relations == [
        "uses <> the head uses or depends on the tail at runtime",
        "implements <> the head implements the tail",
        "depends_on <> the head requires the tail to function",
        "related_to <> the head is generically related to the tail",
        "calls <> the head calls or invokes the tail",
        "imports <> the head imports the tail",
        "defines <> the head defines the tail",
        "inherits <> the head inherits from the tail",
    ]


# ---------------------------------------------------------------------------
# chunks/texts alignment invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_per_text_result_from_the_model_raises_rather_than_misaligning() -> None:
    class _ShortModel:
        def inference(self, texts, labels, **kwargs):
            # One fewer per-text entry than input texts.
            return [[] for _ in texts[:-1]], [[] for _ in texts[:-1]]

    backend = _backend_with_model(_ShortModel())

    with pytest.raises(RuntimeError, match="input texts"):
        await backend.inference(["one", "two", "three"], 0.5, 0.5)


@pytest.mark.asyncio
async def test_inference_before_load_raises() -> None:
    backend = ProseExtractionBackend()
    with pytest.raises(RuntimeError):
        await backend.inference(["text"], 0.5, 0.5)

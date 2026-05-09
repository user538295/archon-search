"""Tests for eval fixture dataclasses (Task 1.1 — FEAT-039)."""
from __future__ import annotations

import pytest

from archon_search.eval.fixtures import (
    EvalCorpus,
    EvalDocument,
    EvalQuery,
    RelevanceLabel,
)


# ---------------------------------------------------------------------------
# RelevanceLabel
# ---------------------------------------------------------------------------


def test_relevance_label_default_grade() -> None:
    label = RelevanceLabel(query_id="q1", doc_id="d1")
    assert label.grade == 1


def test_relevance_label_rejects_negative_grade() -> None:
    with pytest.raises(ValueError):
        RelevanceLabel(query_id="q1", doc_id="d1", grade=-1)


# ---------------------------------------------------------------------------
# EvalDocument
# ---------------------------------------------------------------------------


def test_eval_document_requires_relative_path() -> None:
    doc = EvalDocument(doc_id="doc-001", relative_path="corpus/intro.md")
    assert doc.doc_id == "doc-001"
    assert doc.relative_path == "corpus/intro.md"


# ---------------------------------------------------------------------------
# EvalQuery
# ---------------------------------------------------------------------------


def test_eval_query_supports_optional_collection() -> None:
    q = EvalQuery(
        query_id="q1",
        text="What is X?",
        collection=None,
        metric_scope="routing",
    )
    assert q.collection is None


def test_eval_query_collection_none_is_routing_only() -> None:
    q = EvalQuery(
        query_id="q1",
        text="What is X?",
        collection=None,
        metric_scope="routing",
    )
    assert q.metric_scope == "routing"


def test_eval_query_metric_scope_separates_retrieval_from_routing() -> None:
    retrieval_q = EvalQuery(
        query_id="q-ret",
        text="Find doc about Y",
        collection="docs",
        metric_scope="retrieval",
    )
    routing_q = EvalQuery(
        query_id="q-rout",
        text="General question",
        collection=None,
        metric_scope="routing",
    )
    assert retrieval_q.metric_scope == "retrieval"
    assert retrieval_q.collection == "docs"
    assert routing_q.metric_scope == "routing"
    assert routing_q.collection is None


def test_eval_query_rejects_invalid_metric_scope() -> None:
    with pytest.raises(ValueError):
        EvalQuery(
            query_id="q1",
            text="Some query",
            collection=None,
            metric_scope="unknown",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# EvalCorpus
# ---------------------------------------------------------------------------


def test_eval_corpus_holds_documents_and_queries() -> None:
    docs = [EvalDocument(doc_id="d1", relative_path="corpus/a.md")]
    queries = [
        EvalQuery(
            query_id="q1",
            text="What is A?",
            collection="col",
            metric_scope="retrieval",
        )
    ]
    corpus = EvalCorpus(documents=docs, queries=queries)
    assert len(corpus.documents) == 1
    assert len(corpus.queries) == 1

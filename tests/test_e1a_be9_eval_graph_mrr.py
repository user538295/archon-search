"""Tests for BE-9: graph_mrr metric in the eval harness.

Three tests:
  1. EvalMetrics() has graph_mrr=None by default.
  2. thresholds.toml without graph_mrr still loads without error.
  3. run_eval_suite with graph-mode queries computes graph_mrr as a float.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_eval_metrics_graph_mrr_none_by_default() -> None:
    """EvalMetrics() has graph_mrr=None by default."""
    from archon_search.eval.types import EvalMetrics

    m = EvalMetrics(
        recall_at_1=0.9,
        recall_at_3=0.9,
        recall_at_5=1.0,
        mrr=1.0,
        ndcg_at_5=0.95,
        ndcg_at_10=0.95,
        reranker_lift=None,
        routing_accuracy=None,
        latency_p50_ms=5.0,
        latency_p95_ms=10.0,
    )
    assert m.graph_mrr is None


def test_graph_mrr_optional_field_does_not_break_load_thresholds(tmp_path: Path) -> None:
    """thresholds.toml without graph_mrr key still loads without error."""
    from archon_search.eval.runner import load_thresholds

    content = """
[quality_floors]
recall_at_1 = 0.8
recall_at_3 = 0.9
recall_at_5 = 1.0
mrr = 1.0
ndcg_at_5 = 0.95
ndcg_at_10 = 0.95
"""
    path = tmp_path / "thresholds.toml"
    path.write_text(content)
    thresholds = load_thresholds(path)
    assert thresholds.quality_floors.graph_mrr is None


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_eval_suite_graph_mrr_computed(tmp_path: Path) -> None:
    """run_eval_suite with graph-mode queries computes graph_mrr as a float."""
    from archon_search.eval.runner import run_eval_suite

    # --- minimal corpus -------------------------------------------------------
    corpus_dir = tmp_path / "corpus" / "graph"
    corpus_dir.mkdir(parents=True)

    (corpus_dir / "auth_service.md").write_text(
        "# AuthService\n\n"
        "AuthService handles authentication and login operations. "
        "It validates user credentials and issues session tokens.\n"
    )
    (corpus_dir / "token_validator.md").write_text(
        "# TokenValidator\n\n"
        "TokenValidator verifies JWT tokens and validates token expiry. "
        "It is called by the authentication pipeline to confirm token integrity.\n"
    )

    docs = [
        {"doc_id": "graph-eval-001", "collection": "graph", "relative_path": "graph/auth_service.md"},
        {"doc_id": "graph-eval-002", "collection": "graph", "relative_path": "graph/token_validator.md"},
    ]
    (tmp_path / "documents.jsonl").write_text(
        "\n".join(json.dumps(d) for d in docs) + "\n"
    )

    queries = [
        {
            "query_id": "q-graph-eval-01",
            "text": "AuthService authentication",
            "collection": "graph",
            "metric_scope": "retrieval",
            "graph_mode": "naive",
        }
    ]
    (tmp_path / "queries.jsonl").write_text(
        "\n".join(json.dumps(q) for q in queries) + "\n"
    )

    labels = [
        {"query_id": "q-graph-eval-01", "doc_id": "graph-eval-001", "grade": 2},
        {"query_id": "q-graph-eval-01", "doc_id": "graph-eval-002", "grade": 1},
    ]
    (tmp_path / "labels.jsonl").write_text(
        "\n".join(json.dumps(lb) for lb in labels) + "\n"
    )

    (tmp_path / "runtime.toml").write_text(
        "[search]\n"
        "candidate_depth = 15\n"
        "return_depth = 10\n"
        "metric_depth = 10\n"
        "[routing]\n"
        "contract_enabled = false\n"
    )

    report = await run_eval_suite(
        corpus_root=tmp_path,
        runtime_config_path=tmp_path / "runtime.toml",
        thresholds_path=None,
        baseline_path=None,
    )

    assert report.metrics.graph_mrr is not None, (
        "Expected graph_mrr to be a float, got None. "
        f"graph_traces in report traces: {len([t for t in report.traces if t.collection == 'graph'])}"
    )
    assert isinstance(report.metrics.graph_mrr, float), (
        f"Expected graph_mrr to be a float, got {type(report.metrics.graph_mrr)}"
    )
    assert report.metrics.graph_mrr > 0.0, (
        f"Expected graph_mrr > 0.0, got {report.metrics.graph_mrr}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_eval_suite_graph_mrr_none_without_graph_queries(tmp_path: Path) -> None:
    """graph_mrr is None when the corpus has no graph-mode queries."""
    from archon_search.eval.runner import run_eval_suite

    corpus_dir = tmp_path / "corpus" / "testcol"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "doc1.md").write_text("# Doc1\n\nContent about authentication.\n")

    docs = [{"doc_id": "doc-001", "collection": "testcol", "relative_path": "testcol/doc1.md"}]
    (tmp_path / "documents.jsonl").write_text(json.dumps(docs[0]) + "\n")

    queries = [{"query_id": "q-001", "text": "authentication", "collection": "testcol", "metric_scope": "retrieval"}]
    (tmp_path / "queries.jsonl").write_text(json.dumps(queries[0]) + "\n")

    labels = [{"query_id": "q-001", "doc_id": "doc-001", "grade": 2}]
    (tmp_path / "labels.jsonl").write_text(json.dumps(labels[0]) + "\n")

    (tmp_path / "runtime.toml").write_text(
        "[search]\ncandidate_depth = 15\nreturn_depth = 10\nmetric_depth = 10\n"
        "[routing]\ncontract_enabled = false\n"
    )

    report = await run_eval_suite(
        corpus_root=tmp_path,
        runtime_config_path=tmp_path / "runtime.toml",
        thresholds_path=None,
        baseline_path=None,
    )

    assert report.metrics.graph_mrr is None, (
        f"Expected graph_mrr=None when no graph-mode queries, got {report.metrics.graph_mrr}"
    )


def test_load_eval_corpus_parses_graph_mode(tmp_path: Path) -> None:
    """load_eval_corpus propagates graph_mode from queries.jsonl to EvalQuery."""
    from archon_search.eval.fixtures import load_eval_corpus

    corpus_dir = tmp_path / "corpus" / "graph"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "doc.md").write_text("# Doc\n\nContent.\n")

    (tmp_path / "documents.jsonl").write_text(
        '{"doc_id": "d-1", "collection": "graph", "relative_path": "graph/doc.md"}\n'
    )
    (tmp_path / "queries.jsonl").write_text(
        '{"query_id": "q-1", "text": "test", "collection": "graph", '
        '"metric_scope": "retrieval", "graph_mode": "naive"}\n'
    )
    (tmp_path / "labels.jsonl").write_text(
        '{"query_id": "q-1", "doc_id": "d-1", "grade": 2}\n'
    )

    corpus = load_eval_corpus(tmp_path)
    assert len(corpus.queries) == 1
    assert corpus.queries[0].graph_mode == "naive"

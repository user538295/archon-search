"""Tests for BE-1: 4 new graph recall fields on EvalMetrics and EvalQualityFloors.

Three tests:
  1. test_eval_metrics_has_four_new_recall_fields  — all 4 fields exist and default to None.
  2. test_eval_quality_floors_has_four_new_recall_fields — all 4 fields exist and default to None.
  3. test_eval_metrics_field_set — full field set of EvalMetrics includes the 4 new fields.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Test 1: EvalMetrics has four new graph recall fields, all defaulting to None
# ---------------------------------------------------------------------------


def test_eval_metrics_has_four_new_recall_fields() -> None:
    """All four new graph recall fields exist on EvalMetrics and default to None."""
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
    assert m.graph_naive_recall_at_5 is None
    assert m.graph_local_recall_at_5 is None
    assert m.graph_global_recall_at_5 is None
    assert m.graph_negative_control_recall_at_5 is None


# ---------------------------------------------------------------------------
# Test 2: EvalQualityFloors has four new graph recall fields, all defaulting to None
# ---------------------------------------------------------------------------


def test_eval_quality_floors_has_four_new_recall_fields() -> None:
    """All four new graph recall fields exist on EvalQualityFloors and default to None."""
    from archon_search.eval.runner import EvalQualityFloors

    floors = EvalQualityFloors(
        recall_at_1=0.8,
        recall_at_3=0.9,
        recall_at_5=1.0,
        mrr=1.0,
        ndcg_at_5=0.95,
        ndcg_at_10=0.95,
    )
    assert floors.graph_naive_recall_at_5 is None
    assert floors.graph_local_recall_at_5 is None
    assert floors.graph_global_recall_at_5 is None
    assert floors.graph_negative_control_recall_at_5 is None


# ---------------------------------------------------------------------------
# Test 3: Full field set of EvalMetrics includes the 4 new fields
# ---------------------------------------------------------------------------


def test_eval_metrics_field_set() -> None:
    """EvalMetrics field set contains exactly the expected fields (guards against renames/drops)."""
    from dataclasses import fields

    from archon_search.eval.types import EvalMetrics

    field_names = {f.name for f in fields(EvalMetrics)}
    expected = {
        "recall_at_1", "recall_at_3", "recall_at_5", "mrr",
        "ndcg_at_5", "ndcg_at_10", "reranker_lift", "routing_accuracy",
        "latency_p50_ms", "latency_p95_ms",
        "routing_mrr_centroid", "routing_mrr_hybrid",
        "routing_precision_at_1_centroid", "routing_precision_at_1_hybrid",
        "graph_mrr", "graph_local_mrr", "graph_global_mrr",
        "graph_naive_recall_at_5", "graph_local_recall_at_5",
        "graph_global_recall_at_5", "graph_negative_control_recall_at_5",
        "synonym_bridge_recall_at_5",
        "code_chunking_recall_at_5", "code_defref_recall_at_5",
    }
    assert field_names == expected

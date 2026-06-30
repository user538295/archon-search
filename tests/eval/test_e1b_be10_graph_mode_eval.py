"""BE-10 + T-4: Tests for graph_mode eval fixtures, per-mode MRR partitioning,
and the gated eval gate for graph_local_mrr and graph_global_mrr.

Tests:
- test_eval_fixture_graph_query_schema: all graph_mode queries have required
  fields; graph_mode is one of {"naive", "local", "global"}; both "local" and
  "global" are present.
- test_eval_suite_graph_mode_smoke: full eval suite run without --thresholds-path
  produces graph_local_mrr and graph_global_mrr as separate metric keys (not merged
  into a single graph_mrr).
- test_eval_gate_graph_local_mrr: gated; graph_local_mrr meets threshold (S16/T-4).
- test_eval_gate_graph_global_mrr: gated; graph_global_mrr meets threshold (S16/T-4).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.eval.runner import assert_thresholds, load_thresholds, render_report, run_eval_suite


CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"

_VALID_GRAPH_MODES = {"naive", "local", "global"}


# ---------------------------------------------------------------------------
# Unit: fixture schema validation
# ---------------------------------------------------------------------------


def test_eval_fixture_graph_query_schema() -> None:
    """All queries.jsonl entries with graph_mode have required fields;
    graph_mode value is valid (one of 'naive', 'local', 'global');
    both 'local' and 'global' modes are present.
    """
    queries_path = CORPUS_ROOT / "queries.jsonl"
    rows: list[dict] = []
    with open(queries_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    graph_rows = [r for r in rows if r.get("graph_mode") is not None]
    assert len(graph_rows) >= 1, (
        "Expected at least one entry with graph_mode set in queries.jsonl"
    )

    required_fields = {"query_id", "text", "collection", "metric_scope"}
    for row in graph_rows:
        missing = required_fields - set(row.keys())
        assert not missing, (
            f"Graph-mode query {row.get('query_id')!r} missing required fields: {missing}"
        )
        assert row["graph_mode"] in _VALID_GRAPH_MODES, (
            f"graph_mode={row['graph_mode']!r} in query {row.get('query_id')!r} "
            f"is not one of {_VALID_GRAPH_MODES}"
        )
        assert row["metric_scope"] == "retrieval", (
            f"graph_mode queries must have metric_scope='retrieval', "
            f"got {row['metric_scope']!r} in query {row.get('query_id')!r}"
        )
        assert row["collection"] is not None, (
            f"graph_mode query {row.get('query_id')!r} has collection=None; "
            "graph-mode retrieval queries must specify a collection"
        )

    modes_present = {r["graph_mode"] for r in graph_rows}
    assert "local" in modes_present, (
        f"No graph_mode='local' query found in queries.jsonl; "
        f"modes present: {modes_present}"
    )
    assert "global" in modes_present, (
        f"No graph_mode='global' query found in queries.jsonl; "
        f"modes present: {modes_present}"
    )


# ---------------------------------------------------------------------------
# Integration: eval suite smoke — separate metric keys
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_eval_suite_graph_mode_smoke() -> None:
    """Eval suite runs without --thresholds-path; report contains
    graph_local_mrr and graph_global_mrr as separate metric keys (not merged
    into a single graph_mrr metric).
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=None,
        baseline_path=None,
    )

    # Both new metrics must exist as attributes on EvalMetrics
    assert hasattr(report.metrics, "graph_local_mrr"), (
        "report.metrics missing 'graph_local_mrr' attribute. "
        "EvalMetrics must declare graph_local_mrr: float | None = None."
    )
    assert hasattr(report.metrics, "graph_global_mrr"), (
        "report.metrics missing 'graph_global_mrr' attribute. "
        "EvalMetrics must declare graph_global_mrr: float | None = None."
    )

    # With local and global fixtures present, both must be non-None floats
    assert report.metrics.graph_local_mrr is not None, (
        "graph_local_mrr is None after running eval suite with graph_mode=local "
        "fixtures. Check that q-graph-local-01 is in queries.jsonl and its label "
        "is in labels.jsonl, and that _execute_graph_retrieval_query handles "
        "graph_mode='local' traces."
    )
    assert report.metrics.graph_global_mrr is not None, (
        "graph_global_mrr is None after running eval suite with graph_mode=global "
        "fixtures. Check that q-graph-global-01 is in queries.jsonl and its label "
        "is in labels.jsonl, and that _execute_graph_retrieval_query handles "
        "graph_mode='global' traces."
    )

    # Rendered report must contain both metric names as separate output lines
    rendered = render_report(report)
    assert "graph_local_mrr" in rendered, (
        f"Rendered report does not contain 'graph_local_mrr'.\n"
        f"First 1000 chars:\n{rendered[:1000]}"
    )
    assert "graph_global_mrr" in rendered, (
        f"Rendered report does not contain 'graph_global_mrr'.\n"
        f"First 1000 chars:\n{rendered[:1000]}"
    )


# ---------------------------------------------------------------------------
# T-4: Gated eval gate — graph_local_mrr and graph_global_mrr (S16)
# ---------------------------------------------------------------------------

BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"


@pytest.mark.eval
async def test_eval_gate_graph_local_mrr(thresholds_path: Path) -> None:
    """Gated: graph_local_mrr meets the floor configured in thresholds.toml (S16).

    Requires --thresholds-path; skips gracefully without it (non-CI).
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection. The targeted assertion below then provides a
    # more actionable message when graph_local_mrr specifically is the failing metric.
    assert_thresholds(report)
    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_local_mrr
    actual = report.metrics.graph_local_mrr

    assert floor is not None, (
        "graph_local_mrr floor is not set in thresholds.toml — "
        "add [quality_floors] graph_local_mrr = <value>"
    )
    assert actual is not None, (
        "graph_local_mrr metric is None — check that q-graph-local-01 is in "
        "queries.jsonl and its label is in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_local_mrr={actual:.4f} < floor={floor:.4f} "
        f"(threshold not met — S16 eval gate failed)"
    )


@pytest.mark.eval
async def test_eval_gate_graph_global_mrr(thresholds_path: Path) -> None:
    """Gated: graph_global_mrr meets the floor configured in thresholds.toml (S16).

    Requires --thresholds-path; skips gracefully without it (non-CI).
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # Enforce the full production gate contract first: staleness checks, floor-drop policy,
    # calibration-only baseline rejection. The targeted assertion below then provides a
    # more actionable message when graph_global_mrr specifically is the failing metric.
    assert_thresholds(report)
    thresholds = load_thresholds(thresholds_path)
    floor = thresholds.quality_floors.graph_global_mrr
    actual = report.metrics.graph_global_mrr

    assert floor is not None, (
        "graph_global_mrr floor is not set in thresholds.toml — "
        "add [quality_floors] graph_global_mrr = <value>"
    )
    assert actual is not None, (
        "graph_global_mrr metric is None — check that q-graph-global-01 is in "
        "queries.jsonl and its label is in labels.jsonl"
    )
    assert actual >= floor, (
        f"graph_global_mrr={actual:.4f} < floor={floor:.4f} "
        f"(threshold not met — S16 eval gate failed)"
    )

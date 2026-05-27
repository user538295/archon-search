Baseline refresh (2026-05-27): metrics recalibrated to the values measured by the
pytest gate (`test_eval_baseline_unchanged.py`). All changed metrics IMPROVED —
recall_at_3 0.94→0.96, recall_at_5 0.98→1.00, ndcg_at_5 0.9757→0.9870,
ndcg_at_10 0.9794→0.9870, reranker_lift 0.0230→0.0273 (recall_at_1, mrr,
routing_accuracy unchanged). No threshold floors were lowered, so no waiver is
required (see tests/eval/README.md).

Rationale for keeping floors below the refreshed baseline: the `quality_floors`
in `thresholds.toml` are intentionally left at their prior (pre-refresh) values
rather than raised to track the improved baseline. This preserves regression
headroom — the improved recall/ndcg figures depend on the FTS index being fully
used, which the offline `regenerate.py` path does not reliably reproduce (see
tech-debt note below); pinning the floors to the higher values would make the
gate brittle against that environment sensitivity. The floors remain at or below
the measured baseline as the contract requires. Root cause of the prior drift: the offline
`regenerate.py` calibration path measured worse retrieval than the pytest gate
because the FTS index was not consistently used in that path; the gate (FTS
working) reflects the correct, better-recall behavior. KNOWN TECH DEBT:
`regenerate.py` does not reproduce the gate environment — refresh the baseline
from the gate values until that fidelity gap is closed.

=== Archon Search Eval Report ===
generated_at: 2026-05-27T15:03:44.704643+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      27 (routing_disabled=0, routing_bypassed=0)
documents:    50

Quality metrics:
  recall_at_1        = 0.8600
  recall_at_3        = 0.9600
  recall_at_5        = 1.0000
  mrr                = 1.0000
  ndcg_at_5          = 0.9870
  ndcg_at_10         = 0.9870
  reranker_lift      = 0.0273
  routing_accuracy   = 0.9259

Latency (ms):
  latency_p50_ms     = 10.27
  latency_p95_ms     = 13.31

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

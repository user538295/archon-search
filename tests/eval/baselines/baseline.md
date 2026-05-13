=== Archon Search Eval Report ===
generated_at: 2026-05-13T10:17:00.699870+00:00
corpus_root:  /Users/manczg/Documents/development/archon/packages/archon-search/tests/eval
queries:      27 (routing_disabled=0, routing_bypassed=0)
documents:    50

Quality metrics:
  recall_at_1        = 0.8600
  recall_at_3        = 0.9400
  recall_at_5        = 0.9800
  mrr                = 1.0000
  ndcg_at_5          = 0.9757
  ndcg_at_10         = 0.9794
  reranker_lift      = 0.0230
  routing_accuracy   = 0.9259

Latency (ms):
  latency_p50_ms     = 7.72
  latency_p95_ms     = 9.64

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

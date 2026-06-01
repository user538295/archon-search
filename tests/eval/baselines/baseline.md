=== Archon Search Eval Report ===
generated_at: 2026-06-01T19:58:54.736885+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      33 (routing_disabled=0, routing_bypassed=0)
documents:    57

Quality metrics:
  recall_at_1        = 0.8704
  recall_at_3        = 0.9630
  recall_at_5        = 1.0000
  mrr                = 1.0000
  ndcg_at_5          = 0.9879
  ndcg_at_10         = 0.9879
  reranker_lift      = 0.0253
  routing_accuracy   = 0.9394
  routing_mrr_centroid     = 0.6667
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.6667
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 7.80
  latency_p95_ms     = 9.48

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

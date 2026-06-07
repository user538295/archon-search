=== Archon Search Eval Report ===
generated_at: 2026-06-07T12:00:41.198722+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      40 (routing_disabled=0, routing_bypassed=0)
documents:    66

Quality metrics:
  recall_at_1        = 0.8676
  recall_at_3        = 0.9706
  recall_at_5        = 0.9853
  mrr                = 1.0000
  ndcg_at_5          = 0.9852
  ndcg_at_10         = 0.9879
  reranker_lift      = 0.0232
  routing_accuracy   = 1.0000
  routing_mrr_centroid     = 0.7500
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.7500
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 8.40
  latency_p95_ms     = 11.56

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

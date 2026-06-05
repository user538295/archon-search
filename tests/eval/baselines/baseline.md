=== Archon Search Eval Report ===
generated_at: 2026-06-05T17:06:53.937706+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      38 (routing_disabled=0, routing_bypassed=0)
documents:    62

Quality metrics:
  recall_at_1        = 0.8594
  recall_at_3        = 0.9688
  recall_at_5        = 1.0000
  mrr                = 1.0000
  ndcg_at_5          = 0.9887
  ndcg_at_10         = 0.9887
  reranker_lift      = 0.0220
  routing_accuracy   = 1.0000
  routing_mrr_centroid     = 0.7917
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.7917
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 7.36
  latency_p95_ms     = 10.39

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

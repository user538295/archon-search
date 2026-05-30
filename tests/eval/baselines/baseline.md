=== Archon Search Eval Report ===
generated_at: 2026-05-28T21:16:53.585044+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      31 (routing_disabled=0, routing_bypassed=0)
documents:    55

Quality metrics:
  recall_at_1        = 0.8600
  recall_at_3        = 0.9600
  recall_at_5        = 1.0000
  mrr                = 1.0000
  ndcg_at_5          = 0.9870
  ndcg_at_10         = 0.9870
  reranker_lift      = 0.0273
  routing_accuracy   = 0.9032
  routing_mrr_centroid     = 0.6667
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.6667
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 8.21
  latency_p95_ms     = 10.05

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

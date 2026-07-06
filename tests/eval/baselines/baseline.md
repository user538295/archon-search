=== Archon Search Eval Report ===
generated_at: 2026-07-06T08:48:54.051878+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      147 (routing_disabled=0, routing_bypassed=0)
documents:    192

Quality metrics:
  recall_at_1        = 0.8676
  recall_at_3        = 0.9706
  recall_at_5        = 0.9853
  mrr                = 1.0000
  ndcg_at_5          = 0.9852
  ndcg_at_10         = 0.9879
  reranker_lift      = 0.0232
  routing_accuracy   = 1.0000
  graph_mrr          = 0.2714
  graph_local_mrr    = 1.0000
  graph_global_mrr   = 0.6667
  graph_naive_recall_at_5 = 0.5000
  graph_local_recall_at_5 = 1.0000
  graph_global_recall_at_5 = 1.0000
  graph_negative_control_recall_at_5 = 0.4000
  routing_mrr_centroid     = 0.7222
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.7222
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 8.83
  latency_p95_ms     = 10.80

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

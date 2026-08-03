=== Archon Search Eval Report ===
generated_at: 2026-07-20T06:51:29.582253+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      162 (routing_disabled=0, routing_bypassed=0)
documents:    216

Quality metrics:
  recall_at_1        = 0.8636
  recall_at_3        = 0.9697
  recall_at_5        = 0.9848
  mrr                = 1.0000
  ndcg_at_5          = 0.9847
  ndcg_at_10         = 0.9875
  reranker_lift      = 0.0230
  routing_accuracy   = 1.0000
  graph_mrr          = 0.3087
  graph_local_mrr    = 0.9167
  graph_global_mrr   = 0.6944
  graph_naive_recall_at_5 = 0.5000
  graph_local_recall_at_5 = 1.0000
  graph_global_recall_at_5 = 1.0000
  graph_negative_control_recall_at_5 = 0.4100
  synonym_bridge_recall_at_5 = 0.5000
  code_chunking_recall_at_5 = 1.0000
  code_defref_recall_at_5 = 0.6667
  graph_ppr_bridge_recall_at_5 = 1.0000
  graph_ppr_negative_control_recall_at_5 = 1.0000
  routing_mrr_centroid     = 0.7183
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.7183
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 10.36
  latency_p95_ms     = 12.33

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

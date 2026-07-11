=== Archon Search Eval Report ===
generated_at: 2026-07-11T06:50:13.014854+00:00
corpus_root:  /Users/manczg/Documents/development/archon-search/tests/eval
queries:      163 (routing_disabled=0, routing_bypassed=0)
documents:    217

Quality metrics:
  recall_at_1        = 0.8676
  recall_at_3        = 0.9706
  recall_at_5        = 0.9853
  mrr                = 1.0000
  ndcg_at_5          = 0.9852
  ndcg_at_10         = 0.9879
  reranker_lift      = 0.0227
  routing_accuracy   = 1.0000
  graph_mrr          = 0.3072
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
  routing_mrr_centroid     = 0.7113
  routing_p@1_centroid     = 0.6667
  routing_mrr_hybrid       = 0.7113
  routing_p@1_hybrid       = 0.6667

Latency (ms):
  latency_p50_ms     = 8.98
  latency_p95_ms     = 11.36

Note: latency was measured using deterministic eval backends (EvalEmbedderBackend, EvalRerankerBackend); values are not comparable to production runtime latency.

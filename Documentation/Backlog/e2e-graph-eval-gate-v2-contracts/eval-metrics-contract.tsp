namespace EvalMetricsContract;

/** Four new Recall-at-5 fields added to EvalMetrics (archon_search/eval/types.py).
    All nullable: None when no matching graph_mode traces are present.
    Mirrored as optional floats on EvalQualityFloors (archon_search/eval/runner.py). */
model EvalMetricsExtension {
  graph_naive_recall_at_5: float32 | null;
  graph_local_recall_at_5: float32 | null;
  graph_global_recall_at_5: float32 | null;
  graph_negative_control_recall_at_5: float32 | null;
}

/** Optional floor values added to EvalQualityFloors.
    Set after calibration run; absent until BE-9 thresholds update. */
model EvalQualityFloorsExtension {
  graph_naive_recall_at_5?: float32;
  graph_local_recall_at_5?: float32;
  graph_global_recall_at_5?: float32;
  graph_negative_control_recall_at_5?: float32;
}

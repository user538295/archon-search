namespace E2fGraphConfig;

// E2f: new synonym-detection fields added to [graph] config section.
// All three fields are forward-only (existing collections unaffected until next enrichment).
// These fields are additions to the existing GraphConfig; they are not a separate model.
model GraphConfig {
  // Cosine similarity floor for ANN-based synonym edge creation. Default 0.85.
  // Recommended tuning range 0.82–0.90; monitor synonym_link_rate in GET /status.
  synonym_threshold?: float64 = 0.85;

  // Path to TOML alias file of form: "K8s" = "Kubernetes".
  // null = no manual aliases; missing file logs WARNING and is treated as null.
  alias_file?: string | null = null;

  // When true (default), synonym detection triggers automatically after every ingest.
  // Set false to disable without disabling the whole graph subsystem.
  enrichment_auto?: boolean = true;
}

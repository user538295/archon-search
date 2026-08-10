## Bug: `wizard --routing-strategy centroid`: which key the wizard writes when a flag repeats the default

**ID**: S561-explicit_centroid_flag_writes_only_centroid
**Scenario**: S561
**Severity**: medium
**Version**: archon-search, version 26.8.1945

### What happened
Routing strategy:
centroid: routes queries to collections using centroid similarity (fast, default).
hybrid: combines centroid with keyword scoring (slightly slower, more accurate
for mixed corpora with distinct topic clusters).
Default: centroid.

Log format:
text: human-readable log lines (default).
json: structured JSON logs, suitable for log aggregation pipelines.
Default: text.
Installing: Minimal · English
Embedder:   BAAI/bge-small-en-v1.5
Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
Chunk size: 512 tokens
Providers:  CoreML (Apple Silicon)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
Download:   ~147 MB

Note: Model files are downloaded now. ONNX session initialization happens in the
server process on first query — expect ~5–15s latency on first search.
[5/5] Starting search service...
Waiting for search service........ ready.

archon-search is running on http://127.0.0.1:8765

Next steps:
archon-search ingest --path <path>    # add documents to search
archon-search status                  # check service health
archon-search sync                    # sync watched directories
archon-search stop                    # stop the service
archon-search wizard --top-k 20       # increase results per query (default: 5)

API key: (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
Config:  /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/archon-search.toml
API key: [REDACTED]  (keep this key private; also stored at: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion (HyDE + RAG Fusion) next run.
archon-search installed and running. Profile: Minimal · English.

stderr: 2026-08-10 12:43:05.253 python[28925:55332234] 2026-08-10 12:43:05.253356 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-10 12:43:05.253 python[28925:55332234] 2026-08-10 12:43:05.253396 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
2026-08-10 12:43:05.311 python[28925:55332234] 2026-08-10 12:43:05.311041 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
2026-08-10 12:43:06.056 python[28925:55332234] 2026-08-10 12:43:06.056387 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-10 12:43:06.056 python[28925:55332234] 2026-08-10 12:43:06.056411 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.

assert None == 'centroid'

### What should happen
- Step 1: exit code `0`.
- Step 2 (flag half, `:665` sentence 3): `[routing].routing_strategy` is present and is exactly the string `"centroid"` — the value passed. It is never `"hybrid"`, the only value `:644` names, and never any other value. Under the row's original reading the key would be absent instead; that reading and this one agree that the key can hold no value other than `centroid`, which is why the value is asserted whenever the key exists.
- Step 2 (accepted-default half, `:665` sentences 1–2): `[logging].format` is **absent** from the written config. No `--log-format` flag was passed, `text` is the accepted non-interactive default (`:506`), and `:665` names that exact case — "keep … `text` log format … that key is not written to the file". `[collections].watch` and `[telemetry].enabled` are absent for the same reason (`:502`, `:503`, `:631`, `:637`).
- A failure of the accepted-default half is a **bug** against `20_wizard.md:665`, not a doc gap: the sentence is explicit and names its own example. It is reported with the observed config as evidence.

### Steps to reproduce
1. `ARCHON_SEARCH_CONFIG="$TMP/archon-search.toml" ARCHON_SEARCH_DATA_DIR="$TMP/data" ARCHON_SEARCH_KEY_FILE="$TMP/data/.search.env" archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/data/search" --profile minimal --non-interactive --skip-preload --routing-strategy centroid`
2. Read the TOML at `$TMP/archon-search.toml`.

### Evidence
```
      Default: disabled.
E         
E         Routing strategy:
E           centroid: routes queries to collections using centroid similarity (fast, default).
E           hybrid: combines centroid with keyword scoring (slightly slower, more accurate
E           for mixed corpora with distinct topic clusters).
E           Default: centroid.
E         
E         Log format:
E           text: human-readable log lines (default).
E           json: structured JSON logs, suitable for log aggregation pipelines.
E           Default: text.
E           Installing: Minimal · English
E           Embedder:   BAAI/bge-small-en-v1.5
E           Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
E           Chunk size: 512 tokens
E           Providers:  CoreML (Apple Silicon)
E           Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/search
E           Server:     http://127.0.0.1:8765
E           API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
E           Download:   ~147 MB
E         
E           Note: Model files are downloaded now. ONNX session initialization happens in the
E           server process on first query — expect ~5–15s latency on first search.
E         [5/5] Starting search service...
E         Waiting for search service........ ready.
E         
E         archon-search is running on http://127.0.0.1:8765
E         
E         Next steps:
E           archon-search ingest --path <path>    # add documents to search
E           archon-search status                  # check service health
E           archon-search sync                    # sync watched directories
E           archon-search stop                    # stop the service
E           archon-search wizard --top-k 20       # increase results per query (default: 5)
E         
E         API key: (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
E         Config:  /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/archon-search.toml
E           API key: [REDACTED]  (keep this key private; also stored at: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-eaa9h69c/data/.search.env)
E         Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion (HyDE + RAG Fusion) next run.
E         archon-search installed and running. Profile: Minimal · English.
E         
E         stderr: 2026-08-10 12:43:05.253 python[28925:55332234] 2026-08-10 12:43:05.253356 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E         2026-08-10 12:43:05.253 python[28925:55332234] 2026-08-10 12:43:05.253396 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E         2026-08-10 12:43:05.311 python[28925:55332234] 2026-08-10 12:43:05.311041 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
E         2026-08-10 12:43:06.056 python[28925:55332234] 2026-08-10 12:43:06.056387 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E         2026-08-10 12:43:06.056 python[28925:55332234] 2026-08-10 12:43:06.056411 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E         
E       assert None == 'centroid'
```

## Bug: Multilingual minimal non-interactive with fasttext license accepted

**ID**: S201-multilingual_minimal_configured
**Scenario**: S201
**Severity**: medium
**Version**: archon-search, version 26.8.1815

### What happened
Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),
and enables graph.enabled in the generated config. Both are set up together
automatically so code graphing works out of the box. Recommended if your
corpus includes source code. Default: disabled.

Filesystem watcher:
Monitors watched directories and automatically re-indexes files when they change.
Uses watchdog. Increases background CPU usage slightly.
Default: disabled.

Local telemetry:
Logs per-query metadata (collection, result count, latency) to
~/.archon-search/search-logs/. No query text is stored. Opt-in.
Default: disabled.

Eager embedder loading:
Pre-loads the embedding model at server startup instead of on the first query.
Eliminates first-query latency (~5-15s on first search without this).
Default: disabled.

Routing strategy:
centroid: routes queries to collections using centroid similarity (fast, default).
hybrid: combines centroid with keyword scoring (slightly slower, more accurate
for mixed corpora with distinct topic clusters).
Default: centroid.

Log format:
text: human-readable log lines (default).
json: structured JSON logs, suitable for log aggregation pipelines.
Default: text.
Installing: Minimal · Multilingual
Embedder:   sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
Reranker:   (none)
Chunk size: 512 tokens
Providers:  CoreML (Apple Silicon)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-w2gb59me/data/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-w2gb59me/data/.search.env)
Download:   ~220 MB

Note: Model files are downloaded now. ONNX session initialization happens in the
server process on first query — expect ~5–15s latency on first search.

Optional features:
• Language detection (fasttext)
Installing multilingual language detection...
Multilingual language detection installed.
Removed legacy service file: /Users/manczg/Library/LaunchAgents/com.archon.search.plist
[5/5] Starting search service...
Waiting for search service............................................................ timed out.
Warning: Search service did not become ready within 60 seconds.

stderr: /Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_validation.py:112: UserWarning: The model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 now uses mean pooling instead of CLS embedding. In order to preserve the previous behaviour, consider either pinning fastembed version to 0.5.1 or using `add_custom_model` functionality.
model = TextEmbedding(embedding_model, providers=providers or None)
2026-08-03 11:43:30.780 python[76334:39827652] 2026-08-03 11:43:30.780616 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-03 11:43:30.780 python[76334:39827652] 2026-08-03 11:43:30.780662 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.

assert 1 == 0

### What should happen
- Wizard exits 0 (no Jina license prompt for minimal multilingual).
- `archon-search.toml` contains `multilingual = true`.
- Health endpoint returns HTTP 200.

### Steps to reproduce
1. `archon-search wizard --profile minimal --multilingual --accept-fasttext-license --non-interactive --skip-preload`
2. `cat ~/.archon-search/archon-search.toml`
3. `curl -s http://127.0.0.1:8765/health`

### Evidence
```
 classes, docstrings.
E       Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),
E       and enables graph.enabled in the generated config. Both are set up together
E       automatically so code graphing works out of the box. Recommended if your
E       corpus includes source code. Default: disabled.
E     
E     Filesystem watcher:
E       Monitors watched directories and automatically re-indexes files when they change.
E       Uses watchdog. Increases background CPU usage slightly.
E       Default: disabled.
E     
E     Local telemetry:
E       Logs per-query metadata (collection, result count, latency) to
E       ~/.archon-search/search-logs/. No query text is stored. Opt-in.
E       Default: disabled.
E     
E     Eager embedder loading:
E       Pre-loads the embedding model at server startup instead of on the first query.
E       Eliminates first-query latency (~5-15s on first search without this).
E       Default: disabled.
E     
E     Routing strategy:
E       centroid: routes queries to collections using centroid similarity (fast, default).
E       hybrid: combines centroid with keyword scoring (slightly slower, more accurate
E       for mixed corpora with distinct topic clusters).
E       Default: centroid.
E     
E     Log format:
E       text: human-readable log lines (default).
E       json: structured JSON logs, suitable for log aggregation pipelines.
E       Default: text.
E       Installing: Minimal · Multilingual
E       Embedder:   sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
E       Reranker:   (none)
E       Chunk size: 512 tokens
E       Providers:  CoreML (Apple Silicon)
E       Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-w2gb59me/data/search
E       Server:     http://127.0.0.1:8765
E       API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-w2gb59me/data/.search.env)
E       Download:   ~220 MB
E     
E       Note: Model files are downloaded now. ONNX session initialization happens in the
E       server process on first query — expect ~5–15s latency on first search.
E     
E       Optional features:
E         • Language detection (fasttext)
E     Installing multilingual language detection...
E     Multilingual language detection installed.
E     Removed legacy service file: /Users/manczg/Library/LaunchAgents/com.archon.search.plist
E     [5/5] Starting search service...
E     Waiting for search service............................................................ timed out.
E     Warning: Search service did not become ready within 60 seconds.
E     
E     stderr: /Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/model_validation.py:112: UserWarning: The model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 now uses mean pooling instead of CLS embedding. In order to preserve the previous behaviour, consider either pinning fastembed version to 0.5.1 or using `add_custom_model` functionality.
E       model = TextEmbedding(embedding_model, providers=providers or None)
E     2026-08-03 11:43:30.780 python[76334:39827652] 2026-08-03 11:43:30.780616 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-03 11:43:30.780 python[76334:39827652] 2026-08-03 11:43:30.780662 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=('archon-search', 'wizard', '--config', '/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon...89 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
').returncode
```

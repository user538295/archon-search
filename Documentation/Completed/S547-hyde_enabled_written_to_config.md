## Bug: `wizard --enable-hyde` turns HyDE query expansion on

**ID**: S547-hyde_enabled_written_to_config
**Scenario**: S547
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
Embedder:   BAAI/bge-small-en-v1.5
Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
Chunk size: 512 tokens
Providers:  CoreML (Apple Silicon)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
Download:   ~147 MB

Note: Model files are downloaded now. ONNX session initialization happens in the
server process on first query — expect ~5–15s latency on first search.

Optional features:
• HyDE: enabled (provider: anthropic)
Installing AI query expansion...
Ai query expansion installed.
Created:    /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.secrets.env
Add ANTHROPIC_API_KEY=<key> to this file so the managed service
can source it at start time for AI query expansion.
[5/5] Starting search service...
Waiting for search service................. ready.

archon-search is running on http://127.0.0.1:8765

Next steps:
archon-search ingest --path <path>    # add documents to search
archon-search status                  # check service health
archon-search sync                    # sync watched directories
archon-search stop                    # stop the service
archon-search wizard --top-k 20       # increase results per query (default: 5)

API key: (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
Config:  /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/archon-search.toml
API key: [REDACTED]  (keep this key private; also stored at: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
archon-search installed and running. Profile: Minimal · English.

stderr: 2026-09-11 23:44:44.229 python[47531:15855406] 2026-09-11 23:44:44.229558 [W:onnxruntime:, session_state.cc:1397 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-09-11 23:44:44.229 python[47531:15855406] 2026-09-11 23:44:44.229603 [W:onnxruntime:, session_state.cc:1399 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
2026-09-11 23:44:44.274 python[47531:15855406] 2026-09-11 23:44:44.274599 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
2026-09-11 23:44:45.417 python[47531:15855406] 2026-09-11 23:44:45.417380 [W:onnxruntime:, session_state.cc:1397 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-09-11 23:44:45.417 python[47531:15855406] 2026-09-11 23:44:45.417436 [W:onnxruntime:, session_state.cc:1399 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
libc++abi: terminating due to uncaught exception of type std::__1::system_error: recursive_mutex lock failed: Invalid argument

assert -6 == 0

### What should happen
- Step 3: **exit 0** — `--enable-hyde` is an accepted wizard flag (`20_wizard.md:519`).
- Step 2 output: a line matching **`HyDE: enabled (provider: …)`** — `:382` documents it word for word as the wizard's "visible proof the setting took effect", and `:378` prints it in the "Optional features:" block.
- Step 2 output: the provider named on that line is **`anthropic`** — `:481`, "Provider defaults to `\"anthropic\"`", and `:378` shows `(provider: anthropic)`.
- Step 4 output (control): **no** `HyDE: enabled` line — the non-interactive default is "Skipped" (`:506`) and only non-default optional features are listed (`:382`). This is what attributes step 2's line to the flag.
- Step 5: **no config file** at `$TMP/archon-search.toml` (`:462`, "without executing any of them") — the `--dry-run` contract, and the reason the config key needs the separate completing run of step 6.
- Step 6: **exit 0**.
- Step 7: `[hyde].enabled` is the boolean `true` — the exact mapping and example value of `:657`. Enabling it is the non-default choice (`:506`, `30_configuration.md:161`), so `:665` requires the key to be written. The provider key is not asserted: `:657` maps only `enabled`.

### Steps to reproduce
1. `TMP=$(mktemp -d)`
2. `ARCHON_SEARCH_DATA_DIR="$TMP" ARCHON_SEARCH_CONFIG="$TMP/archon-search.toml" archon-search wizard --config "$TMP/archon-search.toml" --profile minimal --non-interactive --skip-preload --enable-hyde --dry-run`
3. `echo "exit=$?"`
4. Control — the same command with `--enable-hyde` removed.
5. `ls "$TMP/archon-search.toml"`
6. Completing run in a second throwaway tree: `ARCHON_SEARCH_CONFIG="$TMP2/archon-search.toml" ARCHON_SEARCH_DATA_DIR="$TMP2/data" ARCHON_SEARCH_KEY_FILE="$TMP2/data/.search.env" archon-search wizard --config "$TMP2/archon-search.toml" --db-path "$TMP2/data/search" --profile minimal --non-interactive --skip-preload --enable-hyde`
7. Read the TOML at `$TMP2/archon-search.toml`.

### Evidence
```
l · English
E       Embedder:   BAAI/bge-small-en-v1.5
E       Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
E       Chunk size: 512 tokens
E       Providers:  CoreML (Apple Silicon)
E       Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/search
E       Server:     http://127.0.0.1:8765
E       API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
E       Download:   ~147 MB
E     
E       Note: Model files are downloaded now. ONNX session initialization happens in the
E       server process on first query — expect ~5–15s latency on first search.
E     
E       Optional features:
E         • HyDE: enabled (provider: anthropic)
E     Installing AI query expansion...
E     Ai query expansion installed.
E       Created:    /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.secrets.env
E       Add ANTHROPIC_API_KEY=<key> to this file so the managed service
E       can source it at start time for AI query expansion.
E     [5/5] Starting search service...
E     Waiting for search service................. ready.
E     
E     archon-search is running on http://127.0.0.1:8765
E     
E     Next steps:
E       archon-search ingest --path <path>    # add documents to search
E       archon-search status                  # check service health
E       archon-search sync                    # sync watched directories
E       archon-search stop                    # stop the service
E       archon-search wizard --top-k 20       # increase results per query (default: 5)
E     
E     API key: (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
E     Config:  /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/archon-search.toml
E       API key: [REDACTED]  (keep this key private; also stored at: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-full-l70p5718/data/.search.env)
E     archon-search installed and running. Profile: Minimal · English.
E     
E     stderr: 2026-09-11 23:44:44.229 python[47531:15855406] 2026-09-11 23:44:44.229558 [W:onnxruntime:, session_state.cc:1397 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-09-11 23:44:44.229 python[47531:15855406] 2026-09-11 23:44:44.229603 [W:onnxruntime:, session_state.cc:1399 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     2026-09-11 23:44:44.274 python[47531:15855406] 2026-09-11 23:44:44.274599 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
E     2026-09-11 23:44:45.417 python[47531:15855406] 2026-09-11 23:44:45.417380 [W:onnxruntime:, session_state.cc:1397 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-09-11 23:44:45.417 python[47531:15855406] 2026-09-11 23:44:45.417436 [W:onnxruntime:, session_state.cc:1399 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     libc++abi: terminating due to uncaught exception of type std::__1::system_error: recursive_mutex lock failed: Invalid argument
E     
E   assert -6 == 0
E    +  where -6 = CompletedProcess(args=('archon-search', 'wizard', '--config', '/var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon...terminating due to uncaught exception of type std::__1::system_error: recursive_mutex lock failed: Invalid argument
').returncode
```

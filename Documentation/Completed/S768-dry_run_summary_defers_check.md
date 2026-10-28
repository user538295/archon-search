## Bug: The wizard's accelerator check runs only after the summary and `Proceed?`: the summary `Device:` line says `checked after you confirm; CPU if it fails`, `n` writes no device, and `--dry-run` prints `Would set [database] device = 'auto'.`

**ID**: S768-dry_run_summary_defers_check
**Scenario**: S768
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S768-dry_run_summary_defers_check_and_prints_would_set.md — UserManual/20_wizard.md:453 says the summary's Device line reads 'Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails' while the check is pending; the --dry-run summary prints no such suffix:
=== DRY-RUN MODE: No changes will be made ===

Code enrichment (tree-sitter) + code graphing:
Parses and indexes code files structurally — functions, classes, docstrings,
and enables graph.enabled in the generated config. Both features below are set
up together automatically so code graphing works out of the box. Recommended if
your corpus includes source code. Default: disabled.
Estimated download sizes, shown before anything is fetched:
- Code enrichment: tree-sitter language parsers, about 18 MB, installed now.
- Graph extraction: a prose entity/relation model, an estimated 1218 MB (approximate, not a verified size), fetched on first use, licensed under Apache-2.0.

Reranker:
A second-stage cross-encoder model that re-scores results for better precision.
Disabling it reduces latency and RAM but lowers recall quality.
Default: enabled (for profiles that include a reranker).

Filesystem watcher:
Monitors watched directories and automatically re-indexes files when they change.
Uses watchdog. Increases background CPU usage slightly.
Default: disabled.

Local telemetry:
Logs per-query metadata (collection, result count, latency) to
~/.archon-search/search-logs/. No query text is stored. Opt-in.
Default: disabled.

Eager embedder loading:
The server always pre-loads the default embedding model and the reranker at startup,
and /ready returns 503 until model warm-up and the background model validation finish
(a failed warm-up ends the wait) — after a successful warm-up no query pays the cold-load cost.
This option widens that warm-up to every per-collection embedding model as well,
at the cost of a longer startup. Default: disabled.

Routing strategy:
centroid: routes queries to collections using centroid similarity (fast, default).
hybrid: combines centroid with keyword scoring (slightly slower, more accurate
for mixed corpora with distinct topic clusters).
Default: centroid.

Log format:
text: human-readable log lines (default).
json: structured JSON logs, suitable for log aggregation pipelines.
Default: text.
[DRY RUN] Would create db_path directory: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/search
[DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/archon-search.toml
[DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/archon-search.toml.bak
Installing: Minimal · English
Embedder:   BAAI/bge-small-en-v1.5
Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
Chunk size: 512 tokens
Device:     Apple GPU (mps)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/.search.env)
Download:   ~225 MB

Note: Models are downloaded and test-loaded before the service starts (unless
--skip-preload); an embedder that cannot load stops the install. The server
loads them again at startup.
[DRY RUN] Would set [database] device = 'auto'.
[DRY RUN] Would create the search data directory.
[DRY RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.

### What should happen
- Step 1: exit `0`; the summary has `Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails`; the output contains `[DRY RUN] Would set [database] device = 'auto'.` (20:453, 20:144).
- Step 2: the summary has the same `Device:` line; the only `Device:` line in the output is the pending one (no settled outcome — the check never ran), there is no `validation failed` warning and no model download output after `Proceed?`; and the written config has no `[database].device` key (20:155 "no device written", 20:483).

### Steps to reproduce
1. `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --no-multilingual --skip-preload --dry-run --non-interactive`
2. Interactive run (stdin: `y` to the GPU prompt, defaults for the optional-feature prompts, `n` at `Proceed?`): `archon-search wizard --config "$TMP/archon-search.toml" --db-path "$TMP/search" --profile minimal --no-multilingual --skip-preload`; then read the TOML.

### Evidence
```
E   Failed: bug: bugs/S768-dry_run_summary_defers_check_and_prints_would_set.md — UserManual/20_wizard.md:453 says the summary's Device line reads 'Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails' while the check is pending; the --dry-run summary prints no such suffix:
E   === DRY-RUN MODE: No changes will be made ===
E   
E   Code enrichment (tree-sitter) + code graphing:
E     Parses and indexes code files structurally — functions, classes, docstrings,
E     and enables graph.enabled in the generated config. Both features below are set
E     up together automatically so code graphing works out of the box. Recommended if
E     your corpus includes source code. Default: disabled.
E     Estimated download sizes, shown before anything is fetched:
E       - Code enrichment: tree-sitter language parsers, about 18 MB, installed now.
E       - Graph extraction: a prose entity/relation model, an estimated 1218 MB (approximate, not a verified size), fetched on first use, licensed under Apache-2.0.
E   
E   Reranker:
E     A second-stage cross-encoder model that re-scores results for better precision.
E     Disabling it reduces latency and RAM but lowers recall quality.
E     Default: enabled (for profiles that include a reranker).
E   
E   Filesystem watcher:
E     Monitors watched directories and automatically re-indexes files when they change.
E     Uses watchdog. Increases background CPU usage slightly.
E     Default: disabled.
E   
E   Local telemetry:
E     Logs per-query metadata (collection, result count, latency) to
E     ~/.archon-search/search-logs/. No query text is stored. Opt-in.
E     Default: disabled.
E   
E   Eager embedder loading:
E     The server always pre-loads the default embedding model and the reranker at startup,
E     and /ready returns 503 until model warm-up and the background model validation finish
E     (a failed warm-up ends the wait) — after a successful warm-up no query pays the cold-load cost.
E     This option widens that warm-up to every per-collection embedding model as well,
E     at the cost of a longer startup. Default: disabled.
E   
E   Routing strategy:
E     centroid: routes queries to collections using centroid similarity (fast, default).
E     hybrid: combines centroid with keyword scoring (slightly slower, more accurate
E     for mixed corpora with distinct topic clusters).
E     Default: centroid.
E   
E   Log format:
E     text: human-readable log lines (default).
E     json: structured JSON logs, suitable for log aggregation pipelines.
E     Default: text.
E   [DRY RUN] Would create db_path directory: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/search
E   [DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/archon-search.toml
E   [DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/archon-search.toml.bak
E     Installing: Minimal · English
E     Embedder:   BAAI/bge-small-en-v1.5
E     Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
E     Chunk size: 512 tokens
E     Device:     Apple GPU (mps)
E     Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/search
E     Server:     http://127.0.0.1:8765
E     API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s768-vsue1588/.search.env)
E     Download:   ~225 MB
E   
E     Note: Models are downloaded and test-loaded before the service starts (unless
E     --skip-preload); an embedder that cannot load stops the install. The server
E     loads them again at startup.
E   [DRY RUN] Would set [database] device = 'auto'.
E   [DRY RUN] Would create the search data directory.
E   [DRY RUN] Would register and start the search service.
E   archon-search installed and running. Profile: Minimal · English.
```

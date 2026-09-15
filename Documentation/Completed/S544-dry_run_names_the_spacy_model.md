> **Resolution (2026-09-15): stale, not a defect.** This ticket's pass
> criteria require the wizard's dry-run output to mention the spaCy
> model `en-core-web-sm` (line 76, citing `30_configuration.md:208`).
> The project migrated its prose-extraction engine from spaCy to
> GLiNER before this ticket was filed (see
> `Documentation/Completed/2026-08-19-035-multilingual-graph-ner-brief.md`
> and the now-superseded `03_world_class_roadmap.md:125`); the cited
> doc line and the wizard's actual dry-run output both describe GLiNER,
> not spaCy — confirmed by reproducing this scenario live against
> current `HEAD` and against the exact `26.9.2111` tag the ticket
> cites. No code or doc change was made. Archived as stale.

## Bug: `wizard --code` installs the tree-sitter and graph enrichment extras

**ID**: S544-dry_run_names_the_spacy_model
**Scenario**: S544
**Severity**: medium
**Version**: archon-search, version 26.9.2111

### What happened
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
Pre-loads the embedding model and reranker at server startup instead of on the first query.
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
[DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml
[DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml.bak
[DRY RUN] Would configure [database] execution providers.
[DRY RUN] Would create the search data directory.
Installing: Minimal · English
Embedder:   BAAI/bge-small-en-v1.5
Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
Chunk size: 512 tokens
Providers:  (CPU default)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/.search.env)
Download:   ~1383 MB

Note: Model files are downloaded now. ONNX session initialization happens in the
server process on first query — expect ~5–15s latency on first search.

Optional features:
• Code enrichment (tree-sitter)
• Graph enrichment (code graphing)
Tune prose extraction for your corpus with [graph].ner_confidence
and [graph].relation_confidence in /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml
[dry-run] Would install archon-search[code]
[dry-run] Would install archon-search[graph]
[DRY RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.

stderr: 
assert 'en-core-web-sm' in '=== DRY-RUN MODE: No changes will be made ===

### What should happen
- Step 3: **exit 0** — `--code` is an accepted wizard flag (`10_installation.md:153`, `20_wizard.md:465`).
- Step 2 output: the summary's "Optional features:" block lists **`Code enrichment (tree-sitter)`** — the exact line the docs print at `20_wizard.md:376`, listed because it is a non-default optional feature (`:382`).
- Step 2 output: names the **`archon-search[code]`** extra as an install the run would perform — `10_installation.md:153` defines `--code` as installing "tree-sitter code enrichment packages (`archon-search[code]`)", and `:462` says `--dry-run` prints every action it would take.
- Step 2 output: names the **`archon-search[graph]`** extra as an install the run would perform — `30_configuration.md:204` documents this extra as required for graph enrichment.
- Step 2 output: mentions the **`en-core-web-sm`** spaCy model — `30_configuration.md:208` documents `[graph].enabled = true` with spaCy absent raising `ConfigError`.
- Step 2 output: announces **`graph.enabled`** in the generated config — `20_wizard.md:731` explicitly documents this.
- Step 4: **no config file** at `$TMP/archon-search.toml` (`:462`, "without executing any of them"). This is also the **blocked-path reopening gate**: a config written here would make the TOML half of this row reachable and the scenario must then be rewritten to assert what `--code` puts in the file.

### Steps to reproduce
1. `TMP=$(mktemp -d)`
2. `ARCHON_SEARCH_DATA_DIR="$TMP" ARCHON_SEARCH_CONFIG="$TMP/archon-search.toml" archon-search wizard --config "$TMP/archon-search.toml" --profile minimal --non-interactive --skip-preload --code --dry-run`
3. `echo "exit=$?"`
4. `ls "$TMP/archon-search.toml"`

### Evidence
```
ee-sitter language parsers, about 18 MB, installed now.
E         - Graph extraction: a prose entity/relation model, an estimated 1218 MB (approximate, not a verified size), fetched on first use, licensed under Apache-2.0.
E     
E     Reranker:
E       A second-stage cross-encoder model that re-scores results for better precision.
E       Disabling it reduces latency and RAM but lowers recall quality.
E       Default: enabled (for profiles that include a reranker).
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
E       Pre-loads the embedding model and reranker at server startup instead of on the first query.
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
E     [DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml
E     [DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml.bak
E     [DRY RUN] Would configure [database] execution providers.
E     [DRY RUN] Would create the search data directory.
E       Installing: Minimal · English
E       Embedder:   BAAI/bge-small-en-v1.5
E       Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
E       Chunk size: 512 tokens
E       Providers:  (CPU default)
E       Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/search
E       Server:     http://127.0.0.1:8765
E       API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/.search.env)
E       Download:   ~1383 MB
E     
E       Note: Model files are downloaded now. ONNX session initialization happens in the
E       server process on first query — expect ~5–15s latency on first search.
E     
E       Optional features:
E         • Code enrichment (tree-sitter)
E         • Graph enrichment (code graphing)
E           Tune prose extraction for your corpus with [graph].ner_confidence
E           and [graph].relation_confidence in /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-wiz-7716jtsk/archon-search.toml
E     [dry-run] Would install archon-search[code]
E     [dry-run] Would install archon-search[graph]
E     [DRY RUN] Would register and start the search service.
E     archon-search installed and running. Profile: Minimal · English.
E     
E     stderr: 
E   assert 'en-core-web-sm' in '=== DRY-RUN MODE: No changes will be made ===

Code enrichment (tree-sitter) + code graphing:
  Parses and indexes... RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.
'
E    +  where '=== DRY-RUN MODE: No changes will be made ===

Code enrichment (tree-sitter) + code graphing:
  Parses and indexes... RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.
' = _out(CompletedProcess(args=('archon-search', 'wizard', '--profile', 'minimal', '--non-interactive', '--skip-preload', '--co...register and start the search service.
archon-search installed and running. Profile: Minimal · English.
', stderr=''))
```

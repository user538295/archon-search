## Bug: Wizard interactive prompts

**ID**: S03-step5e_eager_load_prompt_appears
**Scenario**: S03
**Severity**: medium
**Version**: archon-search, version 26.8.1969

### What happened
Reranker:
A second-stage cross-encoder model that re-scores results for better precision.
Disabling it reduces latency and RAM but lowers recall quality.
Default: enabled (for profiles that include a reranker).
Keep reranker enabled? [Y/n]: 
Filesystem watcher:
Monitors watched directories and automatically re-indexes files when they change.
Uses watchdog. Increases background CPU usage slightly.
Default: disabled.
Auto-watch directories and re-index on file changes? [y/N]: 
Local telemetry:
Logs per-query metadata (collection, result count, latency) to
~/.archon-search/search-logs/. No query text is stored. Opt-in.
Default: disabled.
Enable local query telemetry? [y/N]: 
Eager embedder loading:
Pre-loads the embedding model and reranker at server startup instead of on the first query.
Eliminates first-query latency (~5-15s on first search without this).
Default: disabled.
Pre-load embedding models and reranker at startup (eliminates first-query latency)? [y/N]: 
Routing strategy:
centroid: routes queries to collections using centroid similarity (fast, default).
hybrid: combines centroid with keyword scoring (slightly slower, more accurate
for mixed corpora with distinct topic clusters).
Default: centroid.
Routing strategy (centroid/hybrid) [centroid]: 
Log format:
text: human-readable log lines (default).
json: structured JSON logs, suitable for log aggregation pipelines.
Default: text.
Log format (text/json) [text]: 
AI query expansion (HyDE + RAG Fusion):
HyDE generates hypothetical answers to improve embedding recall.
RAG Fusion runs multiple query reformulations and merges results.
Providers:
anthropic  - Anthropic API (needs ANTHROPIC_API_KEY)
openai     - OpenAI API (needs OPENAI_API_KEY)
ollama     - runs locally, no API key
claude_cli - uses Claude Code's login, no API key
Default: disabled.
Enable AI query expansion (HyDE + RAG Fusion)? [y/N]: 
LLM-backed graph enrichment:
Uses an LLM to write community summaries and label relationship types
during graph community builds. Optional — the graph subsystem (entity
extraction, PPR, communities) works without it.
Default: disabled.
Enable LLM-backed graph enrichment? [y/N]: [DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/archon-search.toml
[DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/archon-search.toml.bak
[DRY RUN] Would create the search data directory.
Installing: Minimal · English
Embedder:   BAAI/bge-small-en-v1.5
Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
Chunk size: 512 tokens
Providers:  (CPU default)
Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/search
Server:     http://127.0.0.1:8765
API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/.search.env)
Download:   ~147 MB

Note: Model files are downloaded now. ONNX session initialization happens in the
server process on first query — expect ~5–15s latency on first search.
Proceed? [Y/n]: [DRY RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.

assert 'Pre-load embedding models at startup' in '=== DRY-RUN MODE: No changes will be made ===

### What should happen
- Every prompt appears in the order described in `UserManual/20_wizard.md`:
  Step 1 multilingual, Step 2 profile, Step 5a code enrichment (now includes graph enrichment in its description), Step 5b reranker (now "Keep reranker enabled? [Y/n]:"), Step 5c watcher, Step 5d telemetry, Step 5e eager load, Step 5f routing, Step 5g log format, Step 5h AI query expansion, Step 5i LLM-backed graph enrichment, Step 6 proceed.
- No unexpected prompts appear.
- Wizard exits 0 and the service is reachable at `http://127.0.0.1:8765/health`.

### Steps to reproduce
1. Run `archon-search wizard` interactively.
2. At the corpus and profile prompts, choose: English only, minimal profile.
3. Accept GPU detection default.
4. At each optional-feature prompt, choose: no code enrichment, keep reranker enabled, no watcher, no telemetry, no eager loading, centroid routing, text log format, no AI query expansion, no LLM-backed graph enrichment.
5. Verify service starts.

### Evidence
```
stalls tree-sitter + graph enrichment, enables graph)? [y/N]: 
E     Reranker:
E       A second-stage cross-encoder model that re-scores results for better precision.
E       Disabling it reduces latency and RAM but lowers recall quality.
E       Default: enabled (for profiles that include a reranker).
E     Keep reranker enabled? [Y/n]: 
E     Filesystem watcher:
E       Monitors watched directories and automatically re-indexes files when they change.
E       Uses watchdog. Increases background CPU usage slightly.
E       Default: disabled.
E     Auto-watch directories and re-index on file changes? [y/N]: 
E     Local telemetry:
E       Logs per-query metadata (collection, result count, latency) to
E       ~/.archon-search/search-logs/. No query text is stored. Opt-in.
E       Default: disabled.
E     Enable local query telemetry? [y/N]: 
E     Eager embedder loading:
E       Pre-loads the embedding model and reranker at server startup instead of on the first query.
E       Eliminates first-query latency (~5-15s on first search without this).
E       Default: disabled.
E     Pre-load embedding models and reranker at startup (eliminates first-query latency)? [y/N]: 
E     Routing strategy:
E       centroid: routes queries to collections using centroid similarity (fast, default).
E       hybrid: combines centroid with keyword scoring (slightly slower, more accurate
E       for mixed corpora with distinct topic clusters).
E       Default: centroid.
E     Routing strategy (centroid/hybrid) [centroid]: 
E     Log format:
E       text: human-readable log lines (default).
E       json: structured JSON logs, suitable for log aggregation pipelines.
E       Default: text.
E     Log format (text/json) [text]: 
E     AI query expansion (HyDE + RAG Fusion):
E       HyDE generates hypothetical answers to improve embedding recall.
E       RAG Fusion runs multiple query reformulations and merges results.
E       Providers:
E         anthropic  - Anthropic API (needs ANTHROPIC_API_KEY)
E         openai     - OpenAI API (needs OPENAI_API_KEY)
E         ollama     - runs locally, no API key
E         claude_cli - uses Claude Code's login, no API key
E       Default: disabled.
E     Enable AI query expansion (HyDE + RAG Fusion)? [y/N]: 
E     LLM-backed graph enrichment:
E       Uses an LLM to write community summaries and label relationship types
E       during graph community builds. Optional — the graph subsystem (entity
E       extraction, PPR, communities) works without it.
E       Default: disabled.
E     Enable LLM-backed graph enrichment? [y/N]: [DRY RUN] Would write config: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/archon-search.toml
E     [DRY RUN] Would write .bak: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/archon-search.toml.bak
E     [DRY RUN] Would create the search data directory.
E       Installing: Minimal · English
E       Embedder:   BAAI/bge-small-en-v1.5
E       Reranker:   Xenova/ms-marco-MiniLM-L-6-v2
E       Chunk size: 512 tokens
E       Providers:  (CPU default)
E       Database:   /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/search
E       Server:     http://127.0.0.1:8765
E       API key:    (not yet generated)  (full key: /var/folders/gs/sbbzb00933x9j4738dgrlv5r0000gp/T/archon-s03-qfv1tfi0/.search.env)
E       Download:   ~147 MB
E     
E       Note: Model files are downloaded now. ONNX session initialization happens in the
E       server process on first query — expect ~5–15s latency on first search.
E     Proceed? [Y/n]: [DRY RUN] Would register and start the search service.
E     archon-search installed and running. Profile: Minimal · English.
E     
E   assert 'Pre-load embedding models at startup' in '=== DRY-RUN MODE: No changes will be made ===
Will your corpus include non-English documents? [y/N]:   Profile      ... RUN] Would register and start the search service.
archon-search installed and running. Profile: Minimal · English.
'
```

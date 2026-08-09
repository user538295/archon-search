## Bug: Wizard interactive prompts

**ID**: S03-wizard_exits_zero
**Scenario**: S03
**Severity**: medium
**Version**: archon-search, version 26.8.1931

### What happened
3) BAAI/bge-large-en-v1.5 + BAAI/bge-reranker-base

Best for:
1) Personal use, <10k docs, fast responses, low RAM
2) Team use, 10k–200k docs, good recall, ~1 GB RAM
3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM

Add --multilingual to use multilingual models instead.
Choice [1-3, default 1]: Apple Silicon detected — enable Metal acceleration? [Y/n]: 
Code enrichment (tree-sitter) + code graphing:
Parses and indexes code files structurally — functions, classes, docstrings.
Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),
and enables graph.enabled in the generated config. Both are set up together
automatically so code graphing works out of the box. Recommended if your
corpus includes source code. Default: disabled.
Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]: 
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
Pre-loads the embedding model at server startup instead of on the first query.
Eliminates first-query latency (~5-15s on first search without this).
Default: disabled.
Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: 
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
Enable LLM-backed graph enrichment? [y/N]: Which provider for graph enrichment? (anthropic/openai/ollama/llama_cpp) [anthropic]: Model name for graph enrichment (required for non-Anthropic providers): WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: Aborted.

assert 1 == 0

### What should happen
- Every prompt appears in the order described in `UserManual/20_wizard.md`.
- No unexpected prompts appear.
- Wizard exits 0 and the service is reachable at `http://127.0.0.1:8765/health`.

### Steps to reproduce
1. Run `archon-search wizard` interactively.
2. At the corpus and profile prompts, choose: English only, minimal profile.
3. Accept GPU detection default.
4. At each optional-feature prompt, choose: no code enrichment, reranker enabled, no watcher, no telemetry, no eager loading, centroid routing, text log format, no AI query expansion.
5. Verify service starts.

### Evidence
```
2
E       3) BAAI/bge-large-en-v1.5 + BAAI/bge-reranker-base
E     
E       Best for:
E       1) Personal use, <10k docs, fast responses, low RAM
E       2) Team use, 10k–200k docs, good recall, ~1 GB RAM
E       3) Large corpora, 200k+ docs, highest precision, ~2.5 GB RAM
E     
E       Add --multilingual to use multilingual models instead.
E     Choice [1-3, default 1]: Apple Silicon detected — enable Metal acceleration? [Y/n]: 
E     Code enrichment (tree-sitter) + code graphing:
E       Parses and indexes code files structurally — functions, classes, docstrings.
E       Installs tree-sitter language parsers (~50 MB) and graph enrichment (spaCy),
E       and enables graph.enabled in the generated config. Both are set up together
E       automatically so code graphing works out of the box. Recommended if your
E       corpus includes source code. Default: disabled.
E     Index code files (installs tree-sitter + graph enrichment, enables graph)? [y/N]: 
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
E       Pre-loads the embedding model at server startup instead of on the first query.
E       Eliminates first-query latency (~5-15s on first search without this).
E       Default: disabled.
E     Pre-load embedding models at startup (eliminates first-query latency)? [y/N]: 
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
E     Enable LLM-backed graph enrichment? [y/N]: Which provider for graph enrichment? (anthropic/openai/ollama/llama_cpp) [anthropic]: Model name for graph enrichment (required for non-Anthropic providers): WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: Aborted.
E     
E   assert 1 == 0
E    +  where 1 = CompletedProcess(args=['archon-search', 'wizard', '--force', '--delete-db', '--skip-preload'], returncode=1, stdout="=...r non-Anthropic providers): WARNING: This will permanently delete all indexed data. Type 'yes' to confirm: Aborted.
").returncode
```

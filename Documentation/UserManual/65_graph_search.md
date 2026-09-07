**Purpose**: Enable and use the GraphRAG subsystem — entity graph, the four `graph_mode` search paths, synonym resolution, and the browser graph viewer.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-08-20 / **Next review**: 2027-07-29

# Graph search (GraphRAG)

Plain hybrid search (dense vector + FTS + reranker, see [60_searching.md](60_searching.md)) ranks each chunk on its own. The graph subsystem adds a second signal: it extracts named entities from your documents at ingest, links them into a per-collection graph, and lets a search *walk* those links to pull in related content that a single-chunk match would miss. This is what answers multi-hop questions like "which services call the auth module?" or "what connects Kubernetes to our deploy pipeline?".

This guide is task-oriented. For the operator side — community rebuild internals, GC, inspection, LLM enrichment — see [../OperatorGuide/60_graph_operations.md](../OperatorGuide/60_graph_operations.md). For the code def/ref graph and blast-radius impact analysis, see [70_code_graph_and_impact.md](70_code_graph_and_impact.md).

## Enabling the graph

Graph support ships as an optional extra and is **off by default**.

1. Install the extra (pulls in `gliner` — and with it torch and transformers — plus `networkx`, `leidenalg`, `igraph`):

   ```bash
   pip install 'archon-search[graph]'
   ```

2. Optionally pre-warm the prose extraction checkpoint — the extra installs the `gliner` library, not its weights:

   ```bash
   archon-search wizard
   ```

   This is a convenience, not a prerequisite: the server fetches the pinned checkpoint from Hugging Face on first graph ingest if it is not already cached. Pre-warming just moves the ~1218 MB download out of your first ingest.

3. Turn it on in `~/.archon-search/archon-search.toml`:

   ```toml
   [graph]
   enabled = true
   ```

4. Restart the server.

**The `gliner` library is a hard startup requirement; its checkpoint is not.** With `[graph] enabled = true` but `gliner` not installed, the server refuses to boot with a `ConfigError` (`_check_graph_deps` in `archon_search/server/app.py`, delegating to `ensure_graph_engine_importable` in `archon_search/graph_extractor.py`). A checkpoint that cannot be fetched or loaded is a softer failure: the server boots, ingest keeps working, and only prose extraction is skipped — see [What happens at ingest](#what-happens-at-ingest) and [../OperatorGuide/60_graph_operations.md](../OperatorGuide/60_graph_operations.md). Leiden clustering deps (`leidenalg`/`igraph`) are checked lazily — a missing install surfaces only when a community rebuild runs, not at boot. All the `[graph]` knobs live in the `[graph]` config section; see [30_configuration.md](30_configuration.md).

### What happens at ingest

Once the graph is enabled, every ingest runs an extra step. Chunks are split two ways. Chunks carrying a code-symbol type go down the AST path and yield `code_symbol` entities; they never reach the prose engine. Every other chunk goes to the GLiNER checkpoint, prompted with the graph's own labels — `person`, `concept`, `system`, `event` — so each entity's type comes straight from the engine, with no intermediate vocabulary in between. The same forward pass also returns **directed relations**, which become typed edges (`uses`, `implements`, `depends_on`) alongside the undirected `related_to` co-occurrence edges the extractor builds for every pair sharing a chunk. Typed edges merge additively: a distinct relationship type over the same node pair gets its own stable edge id, so nothing is overwritten. All of this is local — typed edges are produced with no provider configured and no network access. Nodes and edges are written to the collection's graph tables after the chunks themselves are persisted.

Chunks are sent to the engine in batches, not one call per chunk (`GRAPH_NER_SUB_BATCH_SIZE = 8` in `archon_search/prose_extraction_backend.py`), and a chunk longer than `GRAPH_NER_TOKEN_WINDOW_WORDS` (2048 words) is truncated to a prefix before extraction, logged once per process.

**Prose extraction degrades, it does not fail the ingest**: chunks still embed and persist, code-symbol entities are unaffected, and a warning lands in `IngestResult.warnings` (see below). A graph-write failure (after persist) is likewise never fatal — it logs a WARNING and lets the already-persisted chunks stand. The one remaining hard failure is `[graph].enabled = true` with the `gliner` *library* itself missing (not the checkpoint) — that is a startup `ConfigError` on the supported server and wizard entry points, so it should never be reached at ingest time; if it is, it still aborts the ingest before persist. Existing collections do **not** retroactively gain a graph — re-ingest is the only way to backfill entities into a collection that was indexed before you enabled the graph.

**Without a loadable checkpoint**, ingest degrades rather than failing: chunks embed and persist normally, `code_symbol` entities are still extracted from code files, prose extraction is skipped, and each affected response carries a warning in `IngestResult.warnings`:

```
graph prose extraction model is unavailable; prose entity extraction is disabled
for this ingest (code-symbol extraction is unaffected).
```

A second wording means the model loaded but the extraction call itself failed for that document:

```
graph prose entity/relation extraction failed; prose entity extraction was
skipped for this document (code-symbol extraction is unaffected).
```

A third, narrower one appears only under concurrency — this document waited too long on another document's in-flight model load and gave up rather than stalling the ingest. It is never a 503:

```
graph prose extraction model is still loading for another document; prose entity
extraction is disabled for this ingest (code-symbol extraction is unaffected).
```

All three are sanitized constants in `archon_search/graph_extractor.py`; none of them carries exception text. Note that a *failed load* latches for the life of the process, and `GET /status` does not report it — `model_validation.provider_warnings` covers the missing `gliner` package, not a checkpoint that failed to load. The ingest warnings and the server log are where you see that.

**Corpus language is not a constraint.** The pinned checkpoint is multilingual, so non-English prose produces entities and typed relations like any other. Quality still varies by language, and a specific language may extract less well than English — that is a quality gap, not a supported/unsupported line. Only the *interface* — logs, CLI copy, error bodies — is English by design; see [../Architecture/220_accessibility_and_internationalization.md](../Architecture/220_accessibility_and_internationalization.md#internationalization) for the corpus-language vs interface-language distinction.

## The four `graph_mode` values

`graph_mode` is a field on `POST /search` (and the MCP `search` tool). Valid values are the literal set `"naive"`, `"local"`, `"global"`, `"ppr"`, or `null` (off) — verified in `archon_search/server/routes_search.py:SearchRequest`. `search_with_context` / the MCP `search_with_context` tool reject any non-null `graph_mode` — use `search` for graph modes.

| Mode | What it does | When to use it | Prerequisite |
|---|---|---|---|
| `naive` | First-degree neighbour entity-name query expansion (capped at `naive_max_expansion_terms`, default 20) | Cheap recall boost; broaden a query with directly-related terms | Graph enabled |
| `local` | Retrieves chunks from the query entities' own Leiden communities | Focused multi-hop within a tight topic cluster | **Communities built** |
| `global` | Feeds community-representative chunks to the reranker (capped at `max_global_candidates`, default 100) | Broad "what does this corpus say about X" synthesis questions | **Communities built** |
| `ppr` | Personalised PageRank walk seeded from query-matched entities, blended into hybrid results | Precise multi-hop — connect two facts through intermediate entities | Graph tables exist; falls back to hybrid if no entities match |

Common guards for every graph mode (all return HTTP `422`):

- `graph_mode` set while `[graph] enabled = false` → `"graph_mode requires [graph] enabled=true in server config"`.
- `scope_filter` **and** `graph_mode` together → `"scope_filter is not supported with graph_mode"` (graph paths bypass the scope SQL predicate — they are mutually exclusive).
- `graph_mode: "ppr"` with a multi-collection `collections[]` fanout → `"graph_mode is not supported with multi-collection fanout; use a single collection"`.

### `naive` — neighbour expansion

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "how do we roll out a release", "graph_mode": "naive"}'
```

The response carries `graph_expansion_applied: true` when expansion fired. Nothing needs to be pre-built beyond the graph itself.

**Precondition — the trigger is lexical, not semantic.** Expansion fires only when an N-gram (1–3 words) of your query matches an extracted **entity name** verbatim (case-insensitive), after which that entity's first-degree neighbours are appended to the query. Entity names are the spans the extraction engine returned, so the match is against *names in the graph*, **not by semantic relevance**. A query with the same meaning but no matching entity-name N-gram returns `graph_expansion_applied: false` with plain hybrid results and no error or warning — so `false` means "your phrasing missed an entity name", not "the feature is broken" or "the graph is empty". Inspect the available names with `GET /graph/{collection}` (the `nodes[].entity_name` values) and phrase queries to overlap them.

### `local` and `global` — need communities first

Both community modes require Leiden communities to be built for the collection. Run the build via the CLI (an HTTP proxy to the running server):

```bash
archon-search graph build-communities docs --wait
```

Flags (see `archon_search/cli/graph_cmd.py`): `--wait` polls the async job to completion, `--namespace/-n` (default `default`) targets a namespace, `--api-url` / `--api-key` point at and authenticate against the server. Under the hood this `POST`s to `/graph/{collection}/rebuild-communities`, which enqueues a trackable job.

If you request `local` or `global` before communities exist, the search returns `422` with body `{"detail": {"code": "graph_communities_not_built", ...}}` (`GraphCommunitiesNotBuiltError`). Build communities, then retry:

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "what links our auth and billing systems", "graph_mode": "global"}'
```

Communities are also rebuilt automatically after ingest (synonym enrichment, below) and by the maintenance loop — see [../OperatorGuide/60_graph_operations.md](../OperatorGuide/60_graph_operations.md).

### `ppr` — Personalised PageRank

`ppr` extracts n-grams from your query, matches them against entity names, seeds a PageRank walk (weighted by how often each matched entity is mentioned), and blends the best-connected chunks into the hybrid result set:

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "which services depend on the token cache", "graph_mode": "ppr"}'
```

The response includes `ppr_entities_matched` (an integer count of seed entities matched). **Silent fallback:** if no query term matches any entity, `ppr_entities_matched` is `0` and you simply get standard hybrid results — no error, no empty set. The walk traverses every edge type: co-occurrence, `synonym_of` (below), and code def/ref edges. PPR needs the graph tables to exist for the collection but does **not** require communities to be built.

## Entity resolution and synonyms

Extraction fragments the same concept across name variants: "K8s", "Kubernetes", and "kubernetes cluster" would otherwise be three disconnected nodes, so a graph search for one misses the others. The graph resolves this automatically.

- **Automatic detection.** After each ingest, a low-priority background job embeds new entity names and links pairs whose cosine similarity is at or above `[graph] synonym_threshold` (default `0.85`, recommended tuning range 0.82–0.90) with a `synonym_of` edge — restricted to entities of the **same type**. It then rebuilds communities so the new links take effect. This is best-effort and never blocks search; set `[graph] enrichment_auto = false` to disable the automatic trigger.
- **Manual aliases.** Point `[graph] alias_file` at a TOML file to pin synonym pairs the embedding check would miss (domain jargon, proprietary abbreviations):

  ```toml
  # aliases.toml
  "K8s" = "Kubernetes"
  ```

  ```toml
  # archon-search.toml
  [graph]
  alias_file = "/path/to/aliases.toml"
  ```

Once linked, **every** graph mode traverses synonym edges transparently — searching "K8s" also surfaces Kubernetes content with no query rewriting. See the config reference in [30_configuration.md](30_configuration.md) for these keys.

## Graph viewer

`GET /graph/{collection}/view` serves a self-contained, interactive HTML graph — force-directed layout, node search, and click-to-inspect side panels — with no install and no external tools. Nodes are **colored by `entity_type`** and **sized by salience**; edge thickness is proportional to co-occurrence weight. Edges are additionally **colored by relationship type** — `uses`, `implements`, `depends_on`, `synonym_of`, `calls`, `imports`, `defines`, `inherits` each get their own color, and anything else (including `related_to` co-occurrence) falls back to grey — and **directional types draw an arrowhead** from head to tail; `related_to` and `synonym_of` are symmetric and draw none. The relationship type is still shown on hover as well; the color and arrowhead augment that tooltip rather than replacing it. There is no legend and no relationship-type filter. A banner appears if the graph was truncated to the server's inspection caps.

Because browsers cannot attach an `Authorization: Bearer` header when you just open a URL, the viewer accepts the key as a `?token=` query parameter (`archon_search/server/routes_graph.py:get_graph_view`). Open this in a browser:

```
http://127.0.0.1:8765/graph/docs/view?token=YOUR_API_KEY
```

The server validates the token, injects it into the page so the embedded graph fetch (`GET /graph/{collection}`) succeeds without a second prompt, and returns `422` if `[graph] enabled = false` or `404` for an unknown collection. The token appears in the page source, so this is intended for local or private-server use, not public or multi-tenant deployments.

## Tuning knobs

All graph tuning lives in the `[graph]` section (full list and defaults in [30_configuration.md](30_configuration.md)):

| Key | Default | Affects |
|---|---|---|
| `naive_max_expansion_terms` | 20 | `naive` expansion cap |
| `max_global_candidates` | 100 | `global` candidate ceiling into the reranker |
| `ppr_damping` | 0.85 | PageRank damping factor for `ppr` |
| `ppr_top_entities` | 20 | Number of top-scored entities whose chunks `ppr` blends in |
| `synonym_threshold` | 0.85 | Cosine cutoff for auto synonym edges |
| `alias_file` | *(none)* | TOML file of manual synonym pairs |
| `enrichment_auto` | true | Auto synonym detection + rebuild after ingest |

These are config-only — there are no per-request overrides.

## Related documents

- [00_index.md](00_index.md) — UserManual table of contents
- [60_searching.md](60_searching.md) — hybrid search, filters, HyDE, RAG Fusion
- [70_code_graph_and_impact.md](70_code_graph_and_impact.md) — code def/ref graph and blast-radius impact
- [80_explain_and_debugging.md](80_explain_and_debugging.md) — `POST /explain`, provenance, and graph traversal debugging
- [30_configuration.md](30_configuration.md) — the `[graph]` config section
- [../OperatorGuide/60_graph_operations.md](../OperatorGuide/60_graph_operations.md) — community rebuild, GC, inspection, LLM enrichment
- [../Architecture/600_api_reference_or_public_interface.md](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI reference

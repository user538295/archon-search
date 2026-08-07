**Purpose**: Query the index over REST and MCP.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-08-07 / **Next review**: 2027-08-07

# Searching

## Principles

1. **Hybrid search by default.** Every `/search` query runs dense vector + FTS retrieval, then a cross-encoder reranker (`archon_search/pipeline.py:SearchPipeline.search`).
2. **The collection is part of the request.** `POST /search` requires either a `collection` or a `collections[]` list — the server does not auto-pick. Use `POST /route` to discover which collections to query.
3. **Result count is server-configured.** Per-request `top_k` is validated (`1..[database].top_k_max`) but does not set the returned count — the pipeline returns `[database].top_k_return` results. See [`/BREAKING.md`](../../BREAKING.md).
4. **REST and MCP are equivalent surfaces.** The MCP tools wrap the same pipeline calls; choose REST for HTTP clients, MCP for MCP-native clients.

For graph-aware retrieval (`graph_mode`), the `/explain` provenance trace, and the OpenAI-compatible shim, this guide gives only pointers — those topics moved to their own files (see [Related documents](#related-documents)).

## REST `POST /search`

Request body (`archon_search/server/routes_search.py:SearchRequest`):

```json
{
  "collection": "docs",
  "query": "how does the router work?",
  "top_k": 5
}
```

Key request fields (see `GET /openapi.json` for the exhaustive schema):

| Field | Type | Notes |
| --- | --- | --- |
| `collection` | `string` | Single-collection search. **XOR** with `collections`. Non-empty after strip. |
| `collections` | `string[]` | Multi-collection fan-out. **XOR** with `collection`. De-duplicated; length capped at `[search].max_fanout` (default 8). |
| `query` | `string` | Required, non-empty after strip. |
| `top_k` | `int` | `>= 1`; rejected with `422` when above `[database].top_k_max` (default 100). Does not set the returned count (see principle 3). |
| `filters` | `object \| null` | `SearchFilters` — see [Filtering results](#filtering-results-a2--c2). |
| `hyde` | `bool` | HyDE query expansion — see [HyDE](#hyde-query-expansion-c4). |
| `rag_fusion` | `bool` | RAG Fusion multi-query recall — see [RAG Fusion](#rag-fusion-multi-query-recall-c5). |
| `graph_mode` | `"naive"\|"local"\|"global"\|"ppr"\|null` | Graph-aware retrieval — documented in [`65_graph_search.md`](./65_graph_search.md). |
| `scope_filter` | `string \| null` | Restrict to matching scope tags (exact, or trailing-`*` prefix). See [`130_ttl_and_scoping.md`](./130_ttl_and_scoping.md). |
| `acl_context` | `bool` | Attach a per-result `acl_gate` — see [Permission-aware snippets](#permission-aware-snippets-acl_context--g15). |

Response (`SearchResponse`) carries `results[]` plus signal fields (`acl_filtered`, `hyde_applied`, `rag_fusion_applied`, `graph_expansion_applied`, `expansion_used`, `expansion_warning`, `applied_filters`, `excluded_collections`, `embedding_model`). Each result includes `doc_id`, `chunk_id`, `text`, `score`, `source_path`, `file_type`, `language`, and `collection`.

Status codes:

- `200` — success (empty `results` means the search ran but matched nothing).
- `404` — collection not found in the caller's namespace.
- `422` — invalid selection (both/neither of `collection`/`collections`), fan-out over `max_fanout`, `top_k` over `top_k_max`, or a config conflict (e.g. missing HyDE/RAG-Fusion provider package).
- `503` — internal metadata lookup failed.
- `500` — pipeline stage exception (embedder, store, or reranker raised).
- `504` — pipeline call timed out (~30 s).

### `curl` example

```bash
source ~/.archon-search/.search.env

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"router centroid pre-ranking"}'
```

## REST `POST /route`

Returns the collection-routing pre-context for a query — useful when you do not know which collection to hit.

Request (`routes_route.py:RouteRequest`):

```json
{ "query": "how does the router work?", "slots": 8 }
```

- `query` — required, non-empty.
- `slots` — optional. If set, must be `>= 1`; overrides `[routing].routing_shortlist_size`.

Response (`RouteResponse`):

```json
{
  "pre_context": "…",
  "pinned_names": ["pinned1"],
  "routable_names": ["docs", "code"],
  "decomposer_invoked": false
}
```

Status codes:

- `200` — success.
- `400` — `query must not be empty` or `slots must be >= 1`.
- `504` — routing timed out (30 s hard ceiling).

A typical workflow is: call `/route`, then call `/search` once per name in `pinned_names + routable_names` (or pass them all as `collections[]` for a single fan-out call).

## MCP tools

`archon_search/server/mcp.py` registers the search surface behind the same Bearer middleware as REST. The two search tools:

| Tool | Key inputs | Output |
| --- | --- | --- |
| `search` | `query`, `collection?` / `collections?`, filter params (`file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`), `hyde`, `rag_fusion`, `graph_mode`, `scope_filter` | `{"results":[…], "acl_filtered":bool, "excluded_collections":[…], "hyde_applied":bool, "expansion_used":bool, "expansion_warning":str\|null, "graph_expansion_applied":bool}` |
| `search_with_context` | `query`, `collection?`, `context_window=1`, same filter/expansion params — but **`graph_mode` is rejected unconditionally** (`code="graph_mode_not_supported"`; use `search` for graph modes) | Results with `context_before` / `context_after` neighbours, plus `hyde_applied`, `expansion_used`, `expansion_warning` |

When `collection` is omitted, the server uses the `default_collection` injected at app construction. The MCP `search` tool exposes filters as flat parameters rather than a nested `filters` object, and — unlike REST — does **not** echo `applied_filters` (track filters client-side).

For MCP client setup and worked SDK examples, see [`../DeveloperGuide/05_mcp_integration.md`](../DeveloperGuide/05_mcp_integration.md). The endpoint is `POST /mcp` (streamable HTTP transport), mounted on the REST port when `[mcp].enabled = true` (default).

## Filtering results (A2 + C2)

`POST /search` and the `search` / `search_with_context` MCP tools accept an optional `filters` object (`SearchFilters`, `archon_search/filters.py`):

```json
{
  "collection": "docs",
  "query": "how does the router work?",
  "filters": {
    "language": "fr",
    "file_type": "md"
  }
}
```

| Filter field | Type | Description |
| --- | --- | --- |
| `file_type` | `string \| null` | Exact match on file extension (`"md"`, `"pdf"`). Leading dots stripped, lowercased. |
| `source_path_prefix` | `string \| null` | SQL prefix match on the document's source path. |
| `source_path_glob` | `string \| null` | Python `fnmatch` glob on the source path (Python-side post-filter). |
| `indexed_after` | `datetime/date \| null` | Include only chunks indexed after this timestamp. Date-only strings coerce to start-of-day UTC. |
| `indexed_before` | `datetime/date \| null` | Include only chunks indexed before this timestamp. Date-only strings coerce to end-of-day UTC. |
| `language` | `string \| null` | ISO 639-1 / ISO 639-3 code, or `"unknown"`. Usable with multi-collection fan-out (REST and MCP). |
| `include_metadata` | `bool` | When `true`, return the stored metadata dict; default `false`. |

Invalid combinations (e.g. `indexed_after > indexed_before`, an empty `file_type`, or a non-ISO `language`) are rejected with `422`.

### Applied filters echo (`applied_filters`)

`SearchResponse` includes an `applied_filters` field echoing the parsed, normalised `SearchFilters` submitted with the request:

- `null` when no `filters` field was sent.
- Values are normalised — `file_type: ".md"` becomes `"md"`.
- Present on both single-collection and multi-collection responses.
- **REST only** — the MCP `search` tool does not echo `applied_filters`.

### Language filter (C2)

When `[database].multilingual = true`, ingested documents are tagged with an ISO language code by the language detector. The `language` filter is a strict equality match:

- `language=fr` — returns only `fr`-tagged chunks; excludes `""` (legacy) and `"unknown"`.
- `language=unknown` — returns only chunks whose language fell below the confidence threshold.
- No `language` filter — returns all chunks regardless of language state.

Every chunk carries one of three language states: `""` (legacy / ingested without `multilingual`), `"unknown"` (below `language_detection_confidence_threshold`), or a detected `"<code>"`. See [`55_chunk_metadata_and_enrichment.md`](./55_chunk_metadata_and_enrichment.md) for how the tag is assigned at ingest.

## Multi-collection search (B3 + E0e)

Send `collections[]` instead of `collection` to fan a single query across several collections in one call:

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collections":["docs","code"],"query":"router centroid pre-ranking"}'
```

- `collection` and `collections` are mutually exclusive — supplying both (or neither) is a `422`.
- Names are de-duplicated; the list length is capped at `[search].max_fanout` (default 8), and exceeding it returns `422`.
- Collections whose stored embedding model differs from the live embedder are dropped and reported in the response `excluded_collections[]` (reason `embedding_model_mismatch`) rather than failing the whole request — even when *every* requested collection is excluded, you get a `200` with `results: []`.
- On `/search`, a name that does not exist in your namespace fails the whole request with `404 collection not found`; a metadata-store failure is a `503`. `/explain` is deliberately more forgiving here: it reports unknown names in `excluded_collections[]` with reason `not_found` and only returns `404` when *every* requested name is unknown (see [`80_explain_and_debugging.md`](80_explain_and_debugging.md)).
- Filters — including `language` (E0e) — apply across every collection in the fan-out.
- `graph_mode` is **not** supported with multi-collection fan-out (`ppr` is rejected with `422`; single-collection only for graph modes).

## HyDE query expansion (C4)

HyDE (Hypothetical Document Embeddings) improves recall for vocabulary-mismatch queries. When enabled, the server asks an LLM to write a short hypothetical answer passage, embeds that passage, and uses the resulting vector for ANN lookup instead of the raw query embedding.

> **Privacy notice**: `hyde=true` sends the raw query to the configured LLM provider. With `provider = "anthropic"` (default), `"openai"`, or `"claude_cli"`, the query leaves the host (`claude_cli` goes to Anthropic via Claude Code's login) — do not enable in air-gapped deployments. With `provider = "ollama"`, the query stays on-premise. See `Documentation/ADRs/C4-hyde-external-llm-dependency.md`.

### Installation

HyDE needs the optional provider package:

```bash
pip install archon-search[hyde]              # Anthropic (default)
# pip install archon-search[ollama]          # Ollama (local, zero-transmission)
# pip install archon-search[openai-provider] # OpenAI
```

### Configuration

Add or edit `[hyde]` in `~/.archon-search/archon-search.toml` (`HyDEConfig`, `archon_search/config.py`):

```toml
[hyde]
enabled = true
# provider = "anthropic"  # "anthropic" (default), "ollama", "openai", or "claude_cli" — G10
model = "claude-haiku-4-5-20251001"
# ollama_base_url = "http://localhost:11434"  # only used when provider = "ollama"
timeout_seconds = 10.0
max_requests_per_minute = 60
```

### Provider matrix (G10)

| Provider | API key | `model` | Notes |
| --- | --- | --- | --- |
| `anthropic` (default) | `ANTHROPIC_API_KEY` | required (default `claude-haiku-4-5-20251001`) | External API. |
| `ollama` | none | required (e.g. `llama3.2`) | Local, zero-transmission; rate limit not enforced. |
| `openai` | `OPENAI_API_KEY` | required (e.g. `gpt-4o-mini`) | External API. |
| `claude_cli` | none | optional (alias or full ID; blank = Claude Code default) | Uses Claude Code's login; `claude` must be on PATH; rate limit not enforced. |

Set the provider's API key (if any) in the server environment before `archon-search start`. Run `archon-search wizard` for guided provider configuration.

### Usage

Pass `hyde: true` on any `/search` or `/explain` request:

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how do I remove the CLI?","hyde":true}'
```

The response sets `hyde_applied: true` when HyDE was used, or `false` on fallback. Two related fields are always present:

| Field | Meaning |
| --- | --- |
| `expansion_used` | `true` when any of `hyde_applied`, `rag_fusion_applied`, or `graph_expansion_applied` is `true`. |
| `expansion_warning` | Non-null when expansion was requested but fell back to the original query embedding (`"HyDE expansion failed"`, `"RAG Fusion timed out"`, or `"RAG Fusion expansion failed"`); `null` otherwise. |

### Fallback behaviour

HyDE never degrades availability. It falls back silently (`hyde_applied: false`) when: `[hyde] enabled = false`; the required API key is absent (WARNING logged once); the provider call times out (`timeout_seconds`) or errors; or the per-process rate limit is exhausted. The one non-silent case is `hyde=true` with the provider package uninstalled — that returns `422` (a config error, not a runtime fallback). The rate limit is per-process: with N workers the effective call rate is up to `N × max_requests_per_minute`.

## RAG Fusion multi-query recall (C5)

RAG Fusion improves recall for multi-faceted queries. When enabled, the server asks an LLM to generate N semantic variants of the query, searches with all N+1 queries in parallel, and fuses the result sets with a second-pass Reciprocal Rank Fusion (RRF).

> **Privacy notice**: `rag_fusion=true` sends the raw query to the configured LLM provider. Same provider trade-offs as HyDE above (`"ollama"` stays on-premise; all others transmit externally). See `Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md`.

### Configuration

`[rag_fusion]` mirrors `[hyde]` plus a `num_queries` knob (`RAGFusionConfig`, `archon_search/config.py`):

```toml
[rag_fusion]
enabled = true
# provider = "anthropic"  # same provider matrix as HyDE (G10)
model = "claude-haiku-4-5-20251001"
timeout_seconds = 10.0
max_requests_per_minute = 60
num_queries = 2   # LLM-generated variants (1–5); total searches = num_queries + 1
```

Installation and the provider matrix are identical to HyDE — swap the extra for `pip install archon-search[rag_fusion]`.

### Usage

Pass `rag_fusion: true` on any `/search` or `/explain` request. The response adds:

| Field | Meaning |
| --- | --- |
| `rag_fusion_applied` | `true` when at least one LLM variant was generated and fused. |
| `rag_fusion_queries_used` | Number of successful variant searches (`0..num_queries`; excludes the original query). |
| `rag_fusion_attempted` | `true` when the generator was called (even if it returned no variants). |

### Mutual exclusion with HyDE

`rag_fusion=true` and `hyde=true` cannot be combined. When both are set, RAG Fusion executes and HyDE is skipped (`hyde_applied: false`) — RAG Fusion subsumes HyDE's intent, and running both would double LLM cost for no meaningful recall gain.

### Fallback behaviour

Same silent-fallback contract as HyDE (`rag_fusion_applied: false`), plus: `rag_fusion=true` is silently ignored for FTS-only collections (no vector index). When the generator returns no variants, the original query is still searched normally. The `/explain` response additionally surfaces `rag_fusion_failure_reason` and a per-sub-query `rag_fusion_sub_queries` breakdown.

## Permission-aware snippets (`acl_context` → G15)

Set `acl_context: true` on `POST /search` to attach an `acl_gate` object to every result — useful for a client that renders "who can see this" alongside each snippet:

```bash
curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"router","acl_context":true}'
```

Each `acl_gate` (`AclGateSchema`) carries:

| Field | Meaning |
| --- | --- |
| `allowed_principals` | The chunk's resolved ACL principal list (`null` when the collection has no ACL rule). |
| `source` | `"frontmatter"`, `"sidecar"`, or `"collection_default"`. |
| `sidecar_path` | Path of the `.acl` sidecar that supplied the ACL, when `source="sidecar"`. |
| `warnings` | Structured warnings raised while resolving the ACL (malformed value, oversized sidecar, …). |

`acl_gate` is omitted (`null`) when `acl_context` is `false`. `POST /explain` always populates `acl_gate` on its results regardless of this flag. ACL enforcement itself — how principals gate visibility, sidecar format, the `acl_filtered` response flag — lives in [`../SecurityGuide/03_authorization_and_acl.md`](../SecurityGuide/03_authorization_and_acl.md).

## Related documents

- [`00_index.md`](./00_index.md) — UserManual reading order.
- [`55_chunk_metadata_and_enrichment.md`](./55_chunk_metadata_and_enrichment.md) — the metadata (language, file_type, scopes) you filter on.
- [`65_graph_search.md`](./65_graph_search.md) — graph-aware retrieval (`graph_mode`: naive / local / global / ppr).
- [`70_code_graph_and_impact.md`](./70_code_graph_and_impact.md) — code def/ref graph and impact traversal.
- [`80_explain_and_debugging.md`](./80_explain_and_debugging.md) — `POST /explain` provenance and per-stage traces.
- [`85_openai_compatible_api.md`](./85_openai_compatible_api.md) — the OpenAI-compatible `/v1` shim.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI reference (`GET /openapi.json` is authoritative for HTTP).

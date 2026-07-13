**Purpose**: Query the index over REST and MCP.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Searching

## Principles

1. **Hybrid search by default.** Every `/search` query runs dense vector + FTS retrieval, then a cross-encoder reranker. See `archon_search/pipeline.py:SearchPipeline.search`.
2. **The collection is part of the request.** `POST /search` requires a `collection` field — the server does not auto-pick. Use `POST /route` to discover which collections to query.
3. **Result count is server-configured.** Per-request `top_k` on `/search` is accepted but ignored at the route level; the pipeline uses `[database].top_k_return` (see [`/BREAKING.md`](../../BREAKING.md)).
4. **REST and MCP are equivalent surfaces.** The MCP tools wrap the same pipeline calls; choose REST for HTTP clients, MCP for MCP-native clients.

## REST `POST /search`

Request body (`archon_search/server/routes_search.py:SearchRequest`):

```json
{
  "collection": "docs",
  "query": "how does the router work?",
  "top_k": 5
}
```

- `collection` — required, non-empty after strip.
- `query` — required, non-empty after strip.
- `top_k` — `1..100`, default `5`. **Accepted but ignored** at the route level; configure `[database].top_k_return` instead. Recorded in `BREAKING.md`.

Response (`SearchResponse`):

```json
{
  "results": [
    {
      "doc_id": "/path/to/file.md",
      "chunk_id": "<uuid>",
      "text": "…",
      "score": 0.81,
      "source_path": "/path/to/file.md"
    }
  ],
  "acl_filtered": false
}
```

Status codes:

- `200` — success.
- `404` — collection not found in the caller's namespace.
- `503` — internal metadata lookup failed.
- `200` with empty `results` — search executed successfully but found no matching documents.
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
  "routable_names": ["docs","code"],
  "decomposer_invoked": false
}
```

Status codes:

- `200` — success.
- `400` — `query must not be empty` or `slots must be >= 1`.
- `504` — routing timed out (30s hard ceiling).

A typical workflow is: call `/route`, then call `/search` once per name in `pinned_names + routable_names`.

## MCP tools

`archon_search/server/mcp.py` registers seventeen tools, all auth-protected via the same Bearer middleware. The surface covers search + ingestion + collection inspection + the `explain` debug/trace tool + export/import + key management.

| Tool | Inputs | Output |
| --- | --- | --- |
| `search` | `query`, `collection?`, `graph_mode: str\|null = null` (**E1a**) | `{"results":[…], "acl_filtered":bool, "excluded_collections":[…], "hyde_applied":bool, "expansion_used":bool, "expansion_warning":str\|null, "graph_expansion_applied":bool}` (**E0b**: gains `expansion_used`, `expansion_warning`; **E1a**: gains `graph_expansion_applied`) |
| `search_with_context` | `query`, `collection?`, `context_window=1`, `graph_mode: str\|null = null` (**E1a** — returns `{"error":…,"code":"graph_mode_not_supported"}` when non-null) | `{"results":[{result, context_before, context_after}, …], "hyde_applied":bool, "expansion_used":bool, "expansion_warning":str\|null}` (**E0b**: gains `expansion_used` and `expansion_warning`) |
| `explain` | `query`, `collection?`, `top_k=5`, `rerank=true`, `graph_mode: str\|null = null` (**E1c**) | Per-stage retrieval/reranking trace plus routing decision (mirrors `POST /explain`). **E1c**: result dict gains `graph_mode_applied` and per-result `graph_provenance: {steps:[…]}|null` |
| `ingest_file` | `path`, `collection?` | Per-file ingest result dict (gains `warnings: list[str]` in **E0b** for ACL sidecar issues) |
| `ingest_directory` | `path`, `glob_pattern="**/*"`, `collection?` | List of ingest results; reports MCP progress |
| `list_collections` | — | List of collection summaries (centroid omitted) |
| `get_collections_meta` | `include_description_embedding?` | List of `CollectionMeta` dicts (centroid and internal fields stripped; `description_embedding` included only if flag is `true`) |
| `get_collection_meta` | `name` | `CollectionMeta` dict (internal fields stripped), or `not_found` error dict |
| `list_documents` | `collection?`, `limit=100` | List of document records |
| `delete_document` | `doc_id`, `collection?` | `{"deleted": <chunk_count>}` |
| `update_collection` | `collection_name` (required), `embedding_model` (required) | Updated collection metadata dict; triggers reindex when model changes |
| `export_collection` | `collection`, `output_path?` | QUEUED job dict with `job_id` |
| `import_collection` | `collection`, `path`, `force_overwrite?`, `ignore_schema_version?`, `on_error?` | QUEUED job dict with `job_id` |
| `create_key` | `namespace` (required), `label?`, `expires_at?` (ISO-8601 datetime with tz) | `{"id":…, "token":…, …}` (token shown once only) |
| `list_keys` | `namespace?`, `status?` | List of key record dicts |
| `revoke_key` | `key_id` | Key record dict (status=revoked) |
| `rotate_key` | `grace_seconds?` | `{"new_key_id":…, "token":…, "status":"active", "old_key_id":…, "old_key_expires_at":…, "old_key_status":…}` |

When `collection` is omitted, the server uses the `default_collection` injected at app construction.

### MCP client pseudocode

Most MCP clients (Claude Desktop, Cursor, custom SDK code) handle the streamable-HTTP handshake for you. Conceptually:

```python
# Pseudocode — see your MCP client's docs for the real API.
import os
from mcp_client import connect

client = connect(
    url="http://127.0.0.1:8765/mcp",
    headers={"Authorization": f"Bearer {os.environ['ARCHON_SEARCH_API_KEY']}"},
)

response = client.call_tool("search", {
    "query": "centroid pre-ranking",
    "collection": "docs",
})
for hit in response["results"]:
    print(hit["score"], hit["source_path"])
```

The MCP endpoint is `POST /mcp` (streamable HTTP transport, mounted by `mcp.create_mcp_http_app`). `/health` remains exempt from auth.

## Filtering results (A2 + C2)

`POST /search` and the `search`/`search_with_context` MCP tools accept an optional `filters` object:

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
| `file_type` | `string \| null` | Exact match on file extension (e.g. `"md"`, `"pdf"`). |
| `source_path_prefix` | `string \| null` | SQL prefix match on the document's source path. |
| `source_path_glob` | `string \| null` | Python `fnmatch` glob on the source path (Python-side post-filter). |
| `indexed_after` | `datetime \| null` | Include only chunks indexed after this timestamp. |
| `indexed_before` | `datetime \| null` | Include only chunks indexed before this timestamp. |
| `language` | `string \| null` | ISO 639-1 or ISO 639-3 language code, or `"unknown"`. **E0e**: usable with multi-collection fan-out (REST and MCP). |
| `include_metadata` | `bool` | When `true`, return the stored metadata dict; default `false`. |

### Applied filters echo (`applied_filters`)

**E0e** — `SearchResponse` includes an `applied_filters` field that echoes the parsed, normalised `SearchFilters` submitted with the request:

```json
{
  "results": [...],
  "applied_filters": {
    "file_type": "md",
    "language": null,
    "source_path_prefix": null,
    "source_path_glob": null,
    "indexed_after": null,
    "indexed_before": null,
    "include_metadata": false
  }
}
```

- `applied_filters` is `null` when no `filters` field was sent in the request.
- Values are normalised: `file_type: ".md"` in the request becomes `"md"` in `applied_filters` (leading dot stripped, lowercased).
- `include_metadata` appears in `applied_filters` since `SearchFilters` is used directly as the response type; treat it as informational (v1 trade-off — `include_metadata` is not enforced on the multi-collection path).
- Present on both single-collection and multi-collection responses.
- **REST only** — the MCP `search` tool does not include `applied_filters` in its response (MCP response echoing is deferred beyond E0e). MCP consumers must track filters client-side.

### Language filter (C2)

When `config.multilingual=True`, ingested documents are tagged with an ISO language code (e.g. `"fr"`, `"de"`) by the fasttext `LanguageDetector`. The `language` filter then lets you retrieve only documents in that language:

- `language=fr` — returns only `fr`-tagged chunks; excludes `""` (pre-C2 legacy) and `"unknown"` (below confidence threshold).
- `language=unknown` — returns only chunks whose language could not be detected above the confidence threshold.
- No `language` filter — returns all chunks regardless of language state.

**Three-state language contract**: every chunk has one of three language values:
- `""` — legacy chunk ingested before C2 / without `multilingual=True`.
- `"unknown"` — processed but confidence below `language_detection_confidence_threshold` (default `0.7`).
- `"<code>"` — detected language code (ISO 639-1 2-letter or ISO 639-3 3-letter).

The `language` filter is a strict equality match — `language=fr` does not match `""` or `"unknown"`.

## HyDE query expansion (C4)

HyDE (Hypothetical Document Embeddings) improves recall for vocabulary-mismatch queries — cases where the query phrasing is far from the document phrasing in embedding space. When enabled, the server asks Claude to write a short hypothetical answer passage, embeds that passage, and uses the resulting vector for ANN lookup instead of the original query embedding.

> **Privacy notice**: `hyde=true` sends the user's raw query to Anthropic's API servers. Do not enable HyDE in air-gapped deployments or where data residency requirements apply. See `Documentation/ADRs/C4-hyde-external-llm-dependency.md` for the full privacy trade-off.

### Installation

HyDE requires the optional `anthropic` package. Install it alongside `archon-search`:

```bash
pip install archon-search[hyde]
```

### Configuration

Add or edit the `[hyde]` section in `~/.archon-search/archon-search.toml`:

```toml
[hyde]
# WARNING: enabled = true sends query text to Anthropic's API.
enabled = true
model = "claude-haiku-4-5-20251001"
timeout_seconds = 10.0
max_requests_per_minute = 60
```

Set `ANTHROPIC_API_KEY` in the server's environment before starting:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
archon-search start
```

When `enabled = true`, the server logs an INFO message at startup:

```
HyDE is enabled — search query text will be sent to Anthropic's API (model: claude-haiku-4-5-20251001)
```

### Usage

Pass `hyde: true` on any `/search` or `/explain` request:

```bash
source ~/.archon-search/.search.env

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how do I remove the CLI?","hyde":true}'
```

The response includes `hyde_applied: true` when HyDE was used, or `hyde_applied: false` when the fallback (original query embedding) was used instead.

**E0b — expansion signal fields** are present on every `/search` response (including when HyDE was not requested):

| Field | Type | Meaning |
|---|---|---|
| `expansion_used` | `bool` | `true` when `hyde_applied`, `rag_fusion_applied`, or `graph_expansion_applied` is `true`. Convenience field; equivalent to `hyde_applied OR rag_fusion_applied OR graph_expansion_applied`. |
| `expansion_warning` | `str \| null` | Non-null when query expansion was requested but failed and fell back to the original query embedding. For HyDE: always `'HyDE expansion failed'` (all failure modes are indistinguishable at the route level). For RAG Fusion: `'RAG Fusion timed out'` (timeout) or `'RAG Fusion expansion failed'` (other errors). `null` when expansion succeeded or was not requested. |

A non-null `expansion_warning` means search results were computed from the original query embedding only — the response is valid but may have lower recall than expected.

### Fallback behaviour

HyDE is designed to **never degrade availability**. It falls back silently (returning `hyde_applied: false`) when:

- `[hyde] enabled = false` in config (the kill-switch).
- `hyde=true` in the request but the `anthropic` package is not installed — in this case a `422` is returned (configuration error, not a runtime fallback).
- `ANTHROPIC_API_KEY` is absent from the environment (WARNING logged once).
- The Anthropic API call times out (after `timeout_seconds`).
- The Anthropic API returns an error.
- The per-process rate limit (`max_requests_per_minute`) is exhausted.

### Operator notes

- **Multi-worker deployments**: the rate limit is per-process. With N workers the effective call rate can be up to `N * max_requests_per_minute`.
- **API key rotation**: use `archon-search key rotate` (or `POST /keys/rotate`) for live rotation without restart. For legacy single-key deployments (env var / key file only), restart the server after changing the file.
- **Model choice**: `claude-haiku-4-5-20251001` (the default) is fast and cheap. Larger models may produce better hypotheses at higher latency and cost.

## RAG Fusion multi-query recall (C5)

RAG Fusion improves recall for multi-faceted queries — cases where the user's query can be expressed in several ways and the best documents only surface with different phrasings. When enabled, the server asks Claude to generate N semantic variants of the original query, searches with all N+1 queries in parallel, and fuses the result sets via second-pass Reciprocal Rank Fusion (RRF).

> **Privacy notice**: `rag_fusion=true` sends the user's raw query to Anthropic's API servers to generate variants. Do not enable RAG Fusion in air-gapped deployments or where data residency requirements apply. See `Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md` for the full privacy trade-off.

### Installation

RAG Fusion requires the optional `anthropic` package:

```bash
pip install archon-search[rag_fusion]
```

### Configuration

Add or edit the `[rag_fusion]` section in `~/.archon-search/archon-search.toml`:

```toml
[rag_fusion]
# WARNING: enabled = true sends query text to Anthropic's API.
enabled = true
model = "claude-haiku-4-5-20251001"
timeout_seconds = 10.0
max_requests_per_minute = 60
num_queries = 2   # LLM-generated variants; total searches = num_queries + 1
```

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | — | Kill-switch. When `false`, `rag_fusion=true` in requests is silently ignored. |
| `model` | `str` | `"claude-haiku-4-5-20251001"` | Non-empty | Claude model used for query variant generation. |
| `timeout_seconds` | `float` | `10.0` | `> 0` | Per-request Anthropic API timeout. |
| `max_requests_per_minute` | `int` | `60` | `>= 1` | Per-process token-bucket rate limit. |
| `num_queries` | `int` | `2` | `1–5` | Number of LLM-generated variants (not counting original). `num_queries=1` logs a WARNING. |

Set `ANTHROPIC_API_KEY` in the server's environment before starting:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
archon-search start
```

When `enabled = true`, the server logs an INFO message at startup:

```
RAG Fusion is enabled — search query text will be sent to Anthropic's API (model: claude-haiku-4-5-20251001)
```

### Usage

Pass `rag_fusion: true` on any `/search` or `/explain` request:

```bash
source ~/.archon-search/.search.env

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"how do I remove the CLI on macOS?","rag_fusion":true}'
```

The response includes:

| Field | Type | Meaning |
|---|---|---|
| `rag_fusion_applied` | `bool` | `true` when at least one LLM variant was generated and fused; `false` on fallback. |
| `rag_fusion_queries_used` | `int` | Number of successful LLM-generated variant searches (`0..num_queries`; does not count the original query search). |
| `rag_fusion_attempted` | `bool` | `true` when the generator was called (even if it returned no variants). |

The `/explain` response additionally includes:

| Field | Type | Meaning |
|---|---|---|
| `rag_fusion_failure_reason` | `str \| null` | Error type string when variant generation failed (e.g. `"TimeoutError"`). |
| `rag_fusion_sub_queries` | `list \| null` | Per-sub-query result summary: `variant_index` (0 = original query), `result_count`, `top_doc_ids` (top 5). |

### Mutual exclusion with HyDE

`rag_fusion=true` and `hyde=true` cannot be combined. When both are present in a request, RAG Fusion executes and HyDE is skipped (`hyde_applied: false` in the response). RAG Fusion subsumes HyDE's intent and adding both would multiply LLM cost without meaningful recall benefit.

### Fallback behaviour

RAG Fusion is designed to **never degrade availability**. It falls back silently (`rag_fusion_applied: false`) when:

- `[rag_fusion] enabled = false` in config (the kill-switch).
- `rag_fusion=true` in the request but the `anthropic` package is not installed — in this case a `422` is returned (configuration error, not a runtime fallback).
- `ANTHROPIC_API_KEY` is absent from the environment (WARNING logged once).
- The Anthropic API call times out (after `timeout_seconds`).
- The Anthropic API returns an error.
- The per-process rate limit (`max_requests_per_minute`) is exhausted.
- The collection has no vector index (FTS-only mode).

When the generator returns no variants (all failure paths), the original query is still searched normally.

### Operator notes

- **Multi-worker deployments**: the rate limit is per-process. With N workers the effective call rate can be up to `N × max_requests_per_minute`. For deployments with both HyDE and RAG Fusion enabled, the combined limit applies: `N × (hyde.max_requests_per_minute + rag_fusion.max_requests_per_minute)` must not exceed your Anthropic account rate limit.
- **Shared API key with HyDE**: both features use `ANTHROPIC_API_KEY`. Tune both `max_requests_per_minute` values together to stay within your account limit.
- **FTS-only collections**: `rag_fusion=true` is silently ignored (`rag_fusion_applied: false`) for collections without a vector index.
- **`rag_fusion_queries_used` semantics**: counts only successful LLM-generated variant searches — does not count the original query search. The `rag_fusion_sub_queries` list (in `/explain` responses) will have `rag_fusion_queries_used + 1` entries.

## Graph-mode search — `graph_mode=naive` (E1a)

Graph-mode search expands the query with first-degree graph-neighbour entity names before the normal hybrid search pipeline runs. This improves recall on relationship-dense corpora (codebases, API docs, research papers) where the query names an entity that has known relationships to other entities.

### Prerequisites

1. Install the `archon-search[graph]` optional extras:

   ```bash
   pip install archon-search[graph]
   ```

2. Enable graph extraction in `~/.archon-search/archon-search.toml`:

   ```toml
   [graph]
   enabled = true
   # backend_threshold_edges = 10000  # warning threshold for large graphs
   ```

3. Re-ingest all collections whose graphs you want built. Graph tables are populated at ingest time — existing chunks are not automatically extracted.

   ```bash
   archon-search ingest --path /path/to/corpus --collection docs
   ```

   After ingest, `GET /status` will include a `graph` sub-object with `node_count` and `edge_count` per collection.

### Usage

Pass `graph_mode: "naive"` on any `/search` request:

```bash
source ~/.archon-search/.search.env

curl -s -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"AuthService","graph_mode":"naive"}'
```

When the query contains tokens that match entity names in the graph, the expander appends neighbour entity names to the query before embedding. The response includes `graph_expansion_applied: true` when at least one neighbour was added.

**MCP**: the `search` tool accepts `graph_mode: "naive"` with the same semantics.

### Error cases

| Condition | Result |
|---|---|
| `graph_mode="naive"` and `[graph] enabled = false` | HTTP 422 with actionable message |
| `archon-search[graph]` not installed and `[graph] enabled = true` | `ConfigError` at server startup (server does not start) |
| Edge count ≥ `backend_threshold_edges` (default 10 000) on ingest | WARNING logged + hint in `IngestResult.warnings`; ingest completes normally |
| Query contains no tokens matching graph entities | 200 with `graph_expansion_applied: false`; normal search result returned |
| `graph_mode` on `POST /explain` | Supported (E1c); returns `graph_mode_applied` and per-result `graph_provenance`. See the E1c section below. |
| MCP `search_with_context` + `graph_mode` | Error dict `code="graph_mode_not_supported"` (permanent; use the `search` tool instead) |

### Known limitations

- Graph tables are namespace-unscoped in E1a: two collections with the same name in different namespaces share graph tables. Multi-namespace operators must NOT enable graph until E1b (which adds namespace-scoped table names).
- Stale edges from deleted documents are not pruned in E1a. Re-ingesting removes old nodes/edges for a given document (upsert by stable ID), but manually deleted documents leave orphan edges.
- Query-time entity matching is exact (case-insensitive string match), not NER — entities not mentioned verbatim in the query will not trigger expansion.

## Graph-mode search — `graph_mode=local` and `graph_mode=global` (E1b)

E1b adds two community-aware graph modes. Both require communities to have been built via `archon-search graph build-communities <collection>` (see [`04_ingestion_and_collections.md`](./04_ingestion_and_collections.md)).

### graph_mode=local

Focuses the search on the community containing the query's recognised entities.

```json
{"collection": "docs", "query": "AuthService", "graph_mode": "local"}
```

The pipeline:
1. Extracts n-grams from the query and matches them against known graph entities
2. Looks up the community containing the matched entities
3. Fetches the community's `representative_chunk_ids[]` (MMR-selected at build time)
4. Merges representative chunks with standard hybrid search candidates
5. Reranks the combined pool and returns top-k

**Fallback behaviour:** If no graph entities are recognised → standard hybrid search (`graph_expansion_applied: false`). If entities recognised but are isolated (not in any community) → falls back to naive graph expansion (`graph_expansion_applied: true`). If communities not built for the collection → falls back to standard hybrid search.

### graph_mode=global

Retrieves representative chunks from every community, reranked against the query. Useful for broad synthesis questions ("What are the main architectural patterns?").

```json
{"collection": "docs", "query": "authentication patterns", "graph_mode": "global"}
```

Returns `422 {"detail": {"code": "graph_communities_not_built"}}` if communities have not been built for the collection (`archon-search graph build-communities`).

**Prerequisite:** Communities must be built via `archon-search graph build-communities <collection>`.

**MCP**: the `search` tool accepts `graph_mode: "local"` and `graph_mode: "global"` with the same semantics as REST `POST /search`.

## Graph-path provenance in `/explain` (E1c)

E1c extends `POST /explain` and the MCP `explain` tool with the same `graph_mode` values (`"naive"`, `"local"`, `"global"`). The explain response gains two new fields:

- `graph_mode_applied: "naive" | "local" | "global" | null` — the mode the pipeline attempted (set even when no graph candidates were found; `null` when `graph_mode` was not supplied in the request).
- `results[].graph_provenance: {steps: [...]} | null` — non-null only for candidates retrieved via graph traversal; standard hybrid-search results carry `null`. Near-misses structurally omit `graph_provenance`.

Each `TraversalStep` in `graph_provenance.steps` has: `entity` (str), `entity_id` (str), `relationship` (str | null), `community_id` (str | null), `chunk_id` (str | null). At least one of `relationship`, `community_id`, or `chunk_id` is guaranteed non-null.

```bash
# Explain with graph provenance (naive mode)
curl -X POST http://localhost:8765/explain \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection":"docs","query":"AuthService","graph_mode":"naive"}'
```

### Error cases specific to graph_mode on /explain

| Condition | Status | Body |
|---|---|---|
| `graph_mode` non-null and `[graph] enabled = false` | 422 | `{"detail": "graph_mode requires [graph] enabled=true in server config"}` |
| `graph_mode` non-null and `collections` (multi-collection) supplied | 422 | `{"detail": "graph_mode is not supported with multi-collection fanout; use a single collection"}` |
| `graph_mode="local"` or `"global"` and communities not built | 422 | `{"detail": {"code": "graph_communities_not_built", "message": "..."}}` |

**MCP**: the `explain` tool accepts `graph_mode: str | null = null` with the same semantics. The result dict includes `graph_mode_applied` and per-result `graph_provenance`.

## OpenAI-compatible search (G9 shim)

When `[openai_shim] enabled = true` in `archon-search.toml`, the server exposes two additional endpoints on the same port that speak the OpenAI chat API protocol:

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Returns one model entry per namespace-visible collection (`archon-search/{name}`) plus a catch-all `archon-search` entry. |
| `POST /v1/chat/completions` | Extracts the last `role="user"` message as a search query; retrieves top-k chunks; returns them as the assistant reply in OpenAI format. |

Any tool that already speaks the OpenAI chat API (Cursor, Continue.dev, LangChain, LlamaIndex) can search Archon by pointing its `base_url` at `http://{host}:{port}/v1` and using `archon-search` or `archon-search/{collection}` as the model name — no custom integration code needed.

```python
# Python example using the openai SDK
import openai

client = openai.OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="<your archon bearer token>",
)

response = client.chat.completions.create(
    model="archon-search/docs",
    messages=[{"role": "user", "content": "How does the router work?"}],
)
print(response.choices[0].message.content)
```

Pass `stream=True` to receive one SSE event per retrieved chunk. Citations (`[Source: …]`) are appended per chunk when `[openai_shim] inject_citations = true` (the default).

Auth uses the same Bearer token as all other Archon endpoints. Error responses on `/v1/*` always use the OpenAI error envelope (`{"error": {"message": ..., "type": ...}}`). See [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) for the full schema.

## Related documents

- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST/MCP reference.
- [`/BREAKING.md`](../../BREAKING.md) — `top_k` and MCP `search` shape history.
- [`04_ingestion_and_collections.md`](./04_ingestion_and_collections.md) — populating the data you are about to search.
- [`06_telemetry.md`](./06_telemetry.md) — what `/search` and `/route` log when telemetry is on.

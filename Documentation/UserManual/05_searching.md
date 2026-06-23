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

`archon_search/server/mcp.py` registers ten tools, all auth-protected via the same Bearer middleware. The surface is intentionally narrower than REST — it covers search + ingestion + collection inspection + the `explain` debug/trace tool, not async jobs or telemetry.

| Tool | Inputs | Output |
| --- | --- | --- |
| `search` | `query`, `collection?` | `{"results":[…], "acl_filtered":bool}` (see `BREAKING.md` — was previously a bare list) |
| `search_with_context` | `query`, `collection?`, `context_window=1` | `[{result, context_before, context_after}, …]` |
| `explain` | `query`, `collection?`, `top_k=5`, `rerank=true` | Per-stage retrieval/reranking trace plus routing decision (mirrors `POST /explain`) |
| `ingest_file` | `path`, `collection?` | Per-file ingest result dict |
| `ingest_directory` | `path`, `glob_pattern="**/*"`, `collection?` | List of ingest results; reports MCP progress |
| `list_collections` | — | List of collection summaries (centroid omitted) |
| `get_collections_meta` | — | Full `CollectionMeta` list (with centroid vectors) |
| `get_collection_meta` | `name` | Full `CollectionMeta`, or `not_found` error dict |
| `list_documents` | `collection?`, `limit=100` | List of document records |
| `delete_document` | `doc_id`, `collection?` | `{"deleted": <chunk_count>}` |

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
| `language` | `string \| null` | ISO 639-1 or ISO 639-3 language code, or `"unknown"`. **Single-collection only** — rejected with 422 on multi-collection fan-out. |
| `include_metadata` | `bool` | When `true`, return the stored metadata dict; default `false`. |

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
timeout_seconds = 5.0
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
timeout_seconds = 5.0
max_requests_per_minute = 60
num_queries = 2   # LLM-generated variants; total searches = num_queries + 1
```

| Key | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `enabled` | `bool` | `false` | — | Kill-switch. When `false`, `rag_fusion=true` in requests is silently ignored. |
| `model` | `str` | `"claude-haiku-4-5-20251001"` | Non-empty | Claude model used for query variant generation. |
| `timeout_seconds` | `float` | `5.0` | `> 0` | Per-request Anthropic API timeout. |
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

## Related documents

- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full REST/MCP reference.
- [`/BREAKING.md`](../../BREAKING.md) — `top_k` and MCP `search` shape history.
- [`04_ingestion_and_collections.md`](./04_ingestion_and_collections.md) — populating the data you are about to search.
- [`06_telemetry.md`](./06_telemetry.md) — what `/search` and `/route` log when telemetry is on.

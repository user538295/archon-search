# archon-search

A standalone hybrid retrieval and routing server.

- **PyPI**: https://pypi.org/project/archon-search/
- **GitHub**: https://github.com/user538295/archon-search

## Overview

`archon-search` is a self-contained search service built around:

- **LanceDB** as the local vector store
- **fastembed** for dense embeddings
- **A cross-encoder reranker** for second-stage scoring
- **A multi-collection router** that picks which collections to query for a given prompt
- **FastAPI** for the REST control plane (with an OpenAPI 3.x contract)
- **An MCP endpoint** exposing the same control-plane tools to MCP clients

It runs as its own process, persists indexes and configuration under `~/.archon-search/`, and exposes both an HTTP API and an MCP API over the same authentication layer.

## Installation

```bash
pip install archon-search
archon-search wizard
```

After `pip install`, run `archon-search wizard` to complete setup. The wizard lets you choose a profile (`minimal`, `balanced`, or `max`), downloads the matching embedding and reranker models, and registers the server as a background service. See [Documentation/UserManual/01_installation.md](Documentation/UserManual/01_installation.md) for the full profile comparison table, flag reference, and disk-space requirements.

Or, for a checkout-based development install:

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
```

## Quick start

Run the server:

```bash
archon-search
```

This invokes the `archon_search.cli.main:main` entry point declared in `pyproject.toml` and starts the FastAPI app on the configured host/port (default `http://127.0.0.1:8765`).

Once it is running:

- `GET /health` — unauthenticated liveness probe
- `GET /docs` — interactive Swagger UI
- `GET /openapi.json` — machine-readable OpenAPI schema

Hit the search endpoint:

```bash
curl -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"collection": "docs", "query": "how does the router work?"}'
```

## Authentication

All endpoints except `GET /health` require a `Bearer` token in the `Authorization` header.

On first start, the server auto-generates a key and writes it to `~/.archon-search/.search.env` with permissions `600`. To override (Docker, CI, multi-host), set the `ARCHON_SEARCH_API_KEY` environment variable — it takes priority over the file. To point the server at a different key file, set `ARCHON_SEARCH_KEY_FILE`.

## Configuration

Server-side configuration lives in `~/.archon-search/archon-search.toml`. Notable sections:

- `[database]` — `db_path`, `embedding_model`, `chunk_size`, `top_k_return`, model paths, and per-collection embedder pool keys: `embedder_cache_size` (int, default `3`) controls how many embedding model instances are kept in the LRU cache; `eager_load_embedders` (bool, default `false`) pre-warms all distinct models at startup
- `[search]` — multi-collection fan-out bounds (`max_fanout`, `fanout_timeout_seconds`)
- `[routing]` — `routing_shortlist_size`, `routing_confidence_threshold`, routing strategy
- `[collections]` — `pinned_collections`, static collection definitions, watcher settings
- `[telemetry]` — opt-in local query logging (see below)

See `examples/archon-search.toml.example` in the repo for the full annotated reference.

## REST API

The REST surface is formally contracted via OpenAPI. `GET /openapi.json` is the authoritative machine-readable schema for endpoint shapes, request/response types, and error codes; `GET /docs` serves the interactive explorer.

Breaking changes to the REST or MCP surface are recorded in [`BREAKING.md`](BREAKING.md).

## MCP tools

The MCP server registers 11 tools (see `archon_search/server/mcp.py`), sharing the REST API's auth layer:

- `search` — hybrid vector + FTS search; returns `{"results": [...], "acl_filtered": bool}`
- `search_with_context` — same as `search` with adjacent-chunk context
- `explain` — per-stage retrieval/reranking trace plus routing decision (mirrors `POST /explain`)
- `ingest_file` — index a single file into a collection
- `ingest_directory` — recursively index a directory
- `list_collections` — list collection names
- `get_collections_meta` — metadata for all collections
- `get_collection_meta` — metadata for one collection
- `list_documents` — list documents in a collection
- `delete_document` — remove a document by `doc_id`
- `update_collection` — change a collection's embedding model (mirrors `PATCH /collections/{name}`)

## Telemetry (opt-in)

Query telemetry is **opt-in and disabled by default** (`enabled = false`). When enabled, every `search`, `search_with_context`, and `POST /route` call appends one JSONL line to a daily file under `~/.archon-search/search-logs/`. **No data is transmitted externally** — all files stay on the local machine.

### Enabling

```toml
# ~/.archon-search/archon-search.toml
[telemetry]
enabled = true
retention_days = 30          # files older than this are deleted at startup and every 24h
log_dir = "~/.archon-search/search-logs"
```

### What is logged

Each entry is a JSON object containing: `query_id` (random UUID), `timestamp` (UTC), `endpoint`, `latency_ms`, `status`, and endpoint-specific fields (`collection`, `result_count`, `result_doc_ids` for retrieval; `collections`, `decomposer_invoked` for routing). Error entries add `error_kind`, a closed set: `empty_query | slot_out_of_range | timeout | internal_error | validation_error | other`.

### What is never logged

**The raw query string is never recorded.** This is a structural guarantee: the factory methods that construct telemetry entries do not accept a `query` parameter. Exception messages are not logged either — only the coarse `error_kind` string enters the JSONL line.

### Path-derived `doc_id` risk

`result_doc_ids` are derived from the source file path on disk (e.g. `/Users/<name>/Documents/<project>/<file>.md`). When telemetry is enabled, these paths appear in the log files — **doc_ids may reveal filesystem paths**, including username and directory structure. Operators accept this when they opt in. A hashed-doc-id mode is planned for a future release.

### `export_enabled` is not available

`[telemetry].export_enabled = true` is reserved for a future release and is not implemented in v1. If set to `true`, the config loader logs a warning and silently coerces the value to `false` (see `archon_search/config.py`). No external transmission occurs in v1.

### Telemetry read-back API

Both endpoints return `{"enabled": false}` when telemetry is disabled.

#### `GET /telemetry/stats`

Aggregated query statistics over an optional time window.

| Parameter | Type | Description |
|-----------|------|-------------|
| `since` | YYYY-MM-DD | Start date (inclusive, optional) |
| `until` | YYYY-MM-DD | End date (inclusive, optional) |

Response shape summary:

```json
{
  "schema_version": 1,
  "enabled": true,
  "total_queries": 42,
  "success_rate": 0.95,
  "latency_ms": {"p50": 120, "p95": 380},
  "by_endpoint": {"search": 30, "route": 12},
  "by_collection": {"docs": 25, "code": 17},
  "error_breakdown": {"timeout": 2, "internal_error": 0}
}
```

`success_rate` is `null` when no queries exist in the window.

#### `GET /telemetry/entries`

Paginated raw log entries.

| Parameter | Type | Description |
|-----------|------|-------------|
| `since` | YYYY-MM-DD | Start date (optional) |
| `until` | YYYY-MM-DD | End date (optional) |
| `collection` | string | Filter by collection name (optional) |
| `endpoint` | string | Filter by endpoint (optional) |
| `status` | string | Filter by status (optional) |
| `error_kind` | string | Filter by error kind (optional) |
| `offset` | int | Pagination offset, default 0 |
| `limit` | int | Page size, 1–200, default 50 |

Response includes `entries`, `next_offset`, and `total_in_window`. Clients should continue calling with the returned `next_offset` until `entries` is empty (equivalently, until `next_offset >= total_in_window`).

## Evaluation harness

`tests/eval/` hosts an offline evaluation harness: a synthetic retrieval corpus, query/label fixtures, deterministic eval backends, committed thresholds, and a measured baseline. It is the sanctioned regression gate for retrieval, reranking, routing, and latency changes.

The authoritative maintenance guide — fixture schemas, threshold-lowering rationale policy, waiver workflow, and document-level metric semantics — lives at [`tests/eval/README.md`](tests/eval/README.md).

The PR and release eval command is:

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
```

The harness uses **deterministic eval backends** that are corpus-aware but label-blind so retrieval and reranking metrics are stable across runs without pulling real model weights. Latency p50/p95 is captured as a **regression guard only** — the measured values reflect the deterministic backends and are not production SLAs.

Current measured baseline values (recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles) are recorded in [`tests/eval/baselines/baseline.md`](tests/eval/baselines/baseline.md) with the machine-readable companion in `tests/eval/baselines/baseline.json`.

## Development

```bash
git clone https://github.com/user538295/archon-search.git
cd archon-search
uv sync --dev
uv run pytest
```

## License

See [LICENSE](LICENSE).

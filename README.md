# archon-search

Archon Search — standalone hybrid retrieval and routing server extracted from Archon (FEAT-038).

The package provides a FastAPI-based REST/MCP control plane over a LanceDB vector store, fastembed embeddings, a cross-encoder reranker, and a multi-collection router. It runs as its own process and is consumed by Archon via HTTP through `archon/ai/search_client.py`.

## Authentication

All endpoints except `GET /health` require a `Bearer` token via the `Authorization` header. On first start, the server auto-generates a key and writes it to `~/.archon/.search.env` (permissions 600). `SearchClient` picks this up automatically — no manual configuration needed for local use. To override (Docker/CI), set the `ARCHON_SEARCH_API_KEY` environment variable; it takes priority over the file.

## Quick start

Install package dependencies (including dev/eval extras):

```bash
cd packages/archon-search
uv sync --dev
```

Run the server:

```bash
uv run archon-search
```

This invokes the entry point declared in `pyproject.toml` (`archon_search.cli.main:main`).

For end-user installation and operation in the Archon monorepo, see [`Documentation/UserManual/search_guide.md`](../../Documentation/UserManual/search_guide.md).

## API

The REST API surface is formally contracted via OpenAPI. Once the server is running:

- `GET /docs` — interactive Swagger UI explorer
- `GET /openapi.json` — machine-readable OpenAPI 3.x schema (authoritative contract for all endpoint shapes, request/response types, and error codes)

The MCP control-plane tools (`search_status`, `search_start`, `search_stop`, `search_ingest`, `search_collection_list`, `search_collection_add`, `search_collection_remove`, `search_collection_info`, `search_collection_reindex`) are accessible via the MCP endpoint and follow the same auth requirements as the REST API.

Breaking changes to either surface are recorded in [`BREAKING.md`](BREAKING.md).

## Privacy & Telemetry

Query telemetry is **opt-in and disabled by default**. When enabled, every `search`, `search_with_context`, and `POST /route` call appends one JSONL line to a daily file under `~/.archon/search-logs/`. No data is transmitted externally; all files remain on the local machine.

### Enabling

Telemetry is `enabled = false` by default. To opt in, set the flag in `~/.archon/archon-search.toml`:

```toml
# ~/.archon/archon-search.toml
[telemetry]
enabled = true
retention_days = 30          # files older than this are deleted at startup and every 24 h
log_dir = "~/.archon/search-logs"
```

### What is logged

Each entry is a JSON object containing: `query_id` (random UUID), `timestamp` (UTC), `endpoint`, `latency_ms`, `status`, and endpoint-specific fields (`collection`, `result_count`, `result_doc_ids` for retrieval; `collections`, `decomposer_invoked` for routing). Error entries add `error_kind` (a closed set: `empty_query | slot_out_of_range | timeout | internal_error | validation_error | other`).

### What is never logged

**The raw query string is never recorded.** This is a structural guarantee: the factory methods that construct telemetry entries do not accept a `query` parameter. Exception messages are not logged either — only the coarse `error_kind` string enters the JSONL line.

### Path-derived `doc_id` risk

`result_doc_ids` are derived from the source file path on disk (e.g., `/Users/<name>/Documents/<project>/<file>.md`). When telemetry is enabled, these paths appear in the log files — **doc_ids may reveal filesystem paths**, including username and directory structure. Operators accept this when they opt in. A hashed-doc-id mode is planned for a future release (FEAT-039c). See [ADR 10](../../Documentation/ADRs/10_search_query_telemetry.md) for the full privacy contract and trade-off rationale.

### `export_enabled` is not available

`[telemetry].export_enabled = true` is rejected at config load time — external transmission of telemetry is reserved for FEAT-039c and is not implemented in v1.

---

## Telemetry Read-Back API

Both endpoints return `{"enabled": false}` when telemetry is disabled.

### `GET /telemetry/stats`

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

### `GET /telemetry/entries`

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

---

## Evaluation

`packages/archon-search/tests/eval/` hosts the FEAT-039 offline evaluation harness: a synthetic retrieval corpus, query/label fixtures, deterministic eval backends, committed thresholds, and a measured baseline. The harness is the sanctioned regression gate for retrieval, reranking, routing, and latency changes.

The authoritative maintenance guide — fixture schemas, threshold-lowering rationale policy, waiver workflow, and document-level metric semantics — lives at [`tests/eval/README.md`](tests/eval/README.md).

The maintained PR and release eval command is:

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
```

PR CI runs this command behind a path filter scoped to retrieval, reranking, routing, and the eval package (Task 4.5). The release-cut workflow runs the same gated slice before any package release mutation.

The harness uses **deterministic eval backends** that are corpus-aware but label-blind so retrieval and reranking metrics are stable across runs without pulling real model weights. Latency p50/p95 is captured as a **regression guard only** — the measured values reflect the deterministic backends and are not production SLAs.

Current measured baseline values (recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles) are recorded in [`tests/eval/baselines/baseline.md`](tests/eval/baselines/baseline.md) with the machine-readable companion in `tests/eval/baselines/baseline.json`.

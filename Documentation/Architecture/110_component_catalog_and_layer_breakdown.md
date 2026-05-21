**Purpose**: Enumerate every `archon_search` module, its role, and the layer it belongs to.
**Audience**: Engineers locating where a change belongs; reviewers checking layering.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Component Catalog and Layer Breakdown

This is the module-level map of `archon_search/`. One row per module, grouped by layer. Use it to find where a change belongs before reading code. Cross-cutting flows are in [100_system_architecture_overview.md](100_system_architecture_overview.md); the wire surface is in [600_api_reference_or_public_interface.md](600_api_reference_or_public_interface.md).

## Principles

1. **One module, one layer.** A module that touches two layers (e.g. parser code in a route handler) is a smell — escalate before merging.
2. **Layers depend downward only.** Server depends on Query/Ingest; Query/Ingest never imports Server.
3. **Names mirror responsibilities.** `routes_*.py` are HTTP edges; `*_meta.py` are metadata models; `_types.py` is the dataclass spine.
4. **Underscored modules are internal.** `_types.py`, `_diagnostics.py`, `_helpers.py` are not part of the public import surface.
5. **No reaching across siblings.** Pipeline stages talk to each other only through `SearchPipeline`; route modules talk to each other only through `app.state`. (#Unverified — `routes_collections.py` currently imports `IngestRequest` and `_default_ingest_task` from `routes_jobs.py` at module level; this is a type/helper import rather than runtime state sharing, but it bends the rule.)

## Layer summary

| Layer | What it owns |
|---|---|
| Ingest | Parsing, chunking, ACL resolution from disk |
| Index | Embeddings, vector + FTS store, collection metadata |
| Query | Hybrid search, reranking, search-with-context, ACL filtering |
| Routing | Multi-collection selection via centroid pre-ranking |
| Metadata | Per-collection descriptions and centroids |
| Server | FastAPI app, MCP app, middleware, route modules, schemas |
| CLI | Click entry points and subcommands |
| Platform | OS service install/uninstall (launchd, systemd, Windows) |
| Telemetry | Opt-in JSONL writer/reader/pruner |
| Jobs | Async job lifecycle for long-running ingest/reindex |
| Eval | Deterministic backends + harness for the regression gate |
| Cross-cutting | Config, constants, types, progress, key manager |

## Ingest

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/parser.py` | Convert files (text, PDF, images via OCR) to Markdown. Raises `ParseError` on unrecoverable failure. | `DocumentParser`, `ParseError` |
| `archon_search/chunker.py` | Split parsed Markdown into chunk records of a target size. | `DocumentChunker` |
| `archon_search/acl.py` | Parse `_acl` front matter and `.acl` sidecars; resolve effective ACL per document; filter search results by request namespace. | `resolve_acl`, `apply_acl_filter`, `is_acl_allowed`, `read_acl_sidecar`, `parse_acl_value`, `is_acl_namespace_valid` |

## Index

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/embedder.py` | Dense-embedding façade with a Protocol backend. Default backend is fastembed. | `Embedder`, `EmbedderBackend`, `ModelEmbedder`, `make_embedder` |
| `archon_search/store.py` | LanceDB-backed store: vector ANN + FTS, RRF fusion in `hybrid_search`, document/chunk lifecycle, namespace + ACL migrations, collection metadata persistence. | `SearchStore`, `validate_metadata`, `parse_metadata` |
| `archon_search/reranker.py` | Cross-encoder second-stage rerank façade with a Protocol backend. | `Reranker`, `RerankerBackend`, `ModelReranker`, `make_reranker` |

## Query

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/pipeline.py` | Orchestrates ingest (`ingest_file`, `ingest_directory`) and query (`search`, `search_with_context`). Computes per-collection centroid on directory ingest and may trigger description regeneration. | `SearchPipeline`, `SearchPipelineResult`, `create_pipeline` |

## Routing

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/router.py` | Fetch collection metadata over JSON-RPC, score each centroid against the query embedding, apply a confidence gate, build a three-tier shortlist for the decomposer. | `MultiCollectionRouter` |

## Metadata

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/collection_meta.py` | Dataclass for per-collection metadata: name, centroid, description, doc/chunk counts, embedding model, timestamps, namespace. | `CollectionMeta` |
| `archon_search/description_generator.py` | Decide whether to regenerate a collection description and call the model (Haiku) to generate one. | `generate_description`, `_should_regenerate` (semi-public; gating heuristic) |

## Server

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/server/app.py` | FastAPI app factory. Wires `SearchPipeline`, `JobStore`, `SearchStore`, telemetry, CORS, BearerAuth. Lifespan runs store migrations on startup, drains telemetry on shutdown. | `create_app`, `run_server` |
| `archon_search/server/mcp.py` | FastMCP app factory exposing pipeline-shaped tools over MCP HTTP, sharing `APIKeyMiddleware`. | `create_app`, `create_mcp_http_app` |
| `archon_search/server/middleware_auth.py` | Bearer-token middleware; resolves namespace per request; constant-time key comparison; exempts `/health`, `/docs`, `/openapi.json`, `/redoc`. | `APIKeyMiddleware`, `_EXEMPT_PATHS` |
| `archon_search/server/routes_health.py` | `GET /health`. | `router` |
| `archon_search/server/routes_state.py` | `GET /indexing-state`. | `router` |
| `archon_search/server/routes_status.py` | `GET /status`. | `router` |
| `archon_search/server/routes_search.py` | `POST /search`. | `router`, `SearchRequest`, `SearchResponse`, `SearchResultSchema` |
| `archon_search/server/routes_route.py` | `POST /route` — driver for `MultiCollectionRouter`. Defines a route-local Pydantic `RouteResponse` distinct from the dataclass `RouteResponse` in `archon_search/types.py` (two public objects share the name). | `router`, `RouteRequest`, `RouteResponse` (Pydantic) |
| `archon_search/server/routes_collections.py` | `GET/POST /collections/`, `GET/DELETE /collections/{name}`, `POST /collections/{name}/reindex`. Issues jobs for add, reindex, and delete (non-empty collections). | `router`, `AddCollectionRequest` |
| `archon_search/server/routes_jobs.py` | `POST /ingest`, `GET /jobs/{id}`, `DELETE /jobs/{id}`. Spawns the ingest task against `SearchPipeline`. | `router`, `IngestRequest` |
| `archon_search/server/routes_telemetry.py` | `GET /telemetry/stats`, `GET /telemetry/entries`. | `router` |
| `archon_search/server/schemas.py` | REST response models: `HealthResponse`, `StatusCollectionEntry`, `StatusResponse`, `IndexingStateCollectionEntry`, `IndexingStateResponse`, `CollectionSummary`, `CollectionDetail`, `JobResponse`, `DeleteResponse`, `ErrorDetail`. | (Pydantic models) |
| `archon_search/server/schemas_telemetry.py` | Pydantic models for telemetry routes. | see source: `archon_search/server/schemas_telemetry.py` |

## CLI

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/cli/main.py` | `archon-search` Click group; registers subcommands: `start`, `stop`, `status`, `install`, `uninstall`, `ingest`, `sync`, `collection`, `config`. | `main` |
| `archon_search/cli/start.py` | `start` subcommand; foreground/service start. | see source |
| `archon_search/cli/stop.py` | `stop` subcommand. | see source |
| `archon_search/cli/status.py` | `status` subcommand; hits the HTTP control plane. | see source |
| `archon_search/cli/install_cmd.py` | `install` / `uninstall` subcommands; wraps `archon_search/install.py` and the `platform/` lifecycle. | see source |
| `archon_search/cli/ingest.py` | `ingest` subcommand; submits an ingest job. | see source |
| `archon_search/cli/sync.py` | `sync` subcommand; drives `SearchCollectionSync`. | see source |
| `archon_search/cli/collection.py` | `collection` subgroup; list/add/remove/info/reindex. | see source |
| `archon_search/cli/config_cmd.py` | `config` subgroup; show/edit `~/.archon-search/archon-search.toml`. | see source |
| `archon_search/cli/_helpers.py` | Shared CLI infrastructure (auth header, base URL resolution, error printing). | internal |
| `archon_search/install.py` | High-level installer: dep check, GPU detect, provider config, service file, bootstrap. | `SearchInstaller` |

## Platform

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/platform/runtime.py` | Resolve binary paths and runtime context. | `SearchRuntime`, `get_runtime`, `find_binary` |
| `archon_search/platform/service.py` | Abstract service lifecycle. | `SearchServiceLifecycle`, `ServiceStatus` |
| `archon_search/platform/macos.py` | launchd implementation. | `LaunchdSearchService` |
| `archon_search/platform/linux.py` | systemd implementation. | `SystemdSearchService` |
| `archon_search/platform/windows.py` | Windows service implementation. | `WindowsSearchService` |
| `archon_search/platform/types.py` | Platform-shared types (currently the `GpuType` enum). | `GpuType` |

## Telemetry

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/telemetry/entry.py` | Structural model + factories. **Factories take no `query` parameter — raw queries are never logged.** | `TelemetryEntry`, `EndpointKind`, `Status`, `ErrorKind` |
| `archon_search/telemetry/writer.py` | Background JSONL writer; one line per call into `~/.archon-search/search-logs/`. | `TelemetryWriter` |
| `archon_search/telemetry/reader.py` | Reads logs for `/telemetry/stats` and `/telemetry/entries`. | `TelemetryReader` |
| `archon_search/telemetry/pruner.py` | Enforces `retention_days` on the log directory. | `Pruner` |

## Jobs

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/jobs/store.py` | Persistent JSON-backed job store with atomic writes, 7-day eviction, RUNNING/CANCELLING crash-to-FAILED recovery, and `transition()` for atomic state changes. | `JobStore` |
| `archon_search/jobs/model.py` | Re-exports `IngestJob`/`JobStatus`, defines `JOBS_FILE`, provides `job_to_dict` for response shaping. | `IngestJob`, `JobStatus`, `JOBS_FILE`, `job_to_dict` |

## Eval

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/eval/backends.py` | Deterministic, corpus-aware but label-blind embedder + reranker so eval metrics are stable without real model weights. | `EvalEmbedderBackend`, `EvalRerankerBackend` |
| `archon_search/eval/runner.py` | Loads thresholds, runtime config, baselines; emits the eval report. | `EvalThresholds`, `EvalQualityFloors`, `EvalLatencyCeilings`, `EvalRuntimeConfig`, `EvalBaseline`, `EvalReport`, `load_thresholds`, `load_runtime_config`, `load_baseline`, `validate_routing_contract` |
| `archon_search/eval/metrics.py` | Recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles, chunk-to-doc dedup. | `compute_recall_at_k`, `compute_mrr`, `compute_ndcg_at_k`, `compute_reranker_lift`, `compute_routing_accuracy`, `compute_latency_percentiles`, `deduplicate_to_doc_rankings` |
| `archon_search/eval/fixtures.py` | Load `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`. | `EvalDocument`, `EvalQuery`, `RelevanceLabel`, `EvalCorpus`, `load_eval_corpus`, `build_doc_collection_map` |
| `archon_search/eval/types.py`, `_hashing.py`, `_tracing.py` | Shared eval types, hashing helpers, trace capture. | see source |

## Cross-cutting

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/config.py` | Load/save `~/.archon-search/archon-search.toml`; coerce `telemetry.export_enabled = true` to `false` with a warning; coerce types. | `SearchConfig`, `TelemetryConfig`, `ConfigError`, `load_config`, `save_config`, `get_default_config_path` |
| `archon_search/constants.py` | Default namespace and namespace validation. | `DEFAULT_NAMESPACE`, `_validate_namespace` |
| `archon_search/_types.py` | Internal dataclass spine: `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult`. | (internal) |
| `archon_search/types.py` | Public job/collection types: `JobStatus`, `IngestJob`, `ReindexJob`, `DeleteJob`, `Query`, `RouteResponse`, `Collection`, `CollectionDetail`, `Chunk`. | (public surface) |
| `archon_search/progress.py` | Indexing progress state: `IndexingStatus`, `CollectionProgress`, `IndexingState`, `IndexingStateStore`, ETA helpers. | `IndexingStatus`, `CollectionProgress`, `IndexingState`, `IndexingStateStore`, `compute_eta_seconds` |
| `archon_search/key_manager.py` | Resolve the API key from env (`ARCHON_SEARCH_API_KEY`), file (`ARCHON_SEARCH_KEY_FILE` or `~/.archon-search/.search.env`), or generate one (chmod 600). | `load_or_generate_key` |
| `archon_search/watcher.py` | Watchdog-driven `CollectionWatcher` + `WatcherManager` with a debounced handler. | `CollectionWatcher`, `WatcherManager` |
| `archon_search/sync.py` | `SearchCollectionSync`: full reconcile between on-disk corpora and the index; manifest-based collection naming. | `SearchCollectionSync`, `SyncResult`, `path_to_collection_name` |
| `archon_search/install.py` | High-level installer driving `platform/*` and bootstrap. | `SearchInstaller` |
| `archon_search/_diagnostics.py` | Internal diagnostics helpers. | internal |

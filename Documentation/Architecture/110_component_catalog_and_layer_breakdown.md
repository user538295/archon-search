**Purpose**: Enumerate every `archon_search` module, its role, and the layer it belongs to.
**Audience**: Engineers locating where a change belongs; reviewers checking layering.
**Status**: Draft
**Last reviewed**: 2026-05-24
**Next review**: 2026-08-20

# Component Catalog and Layer Breakdown

This is the module-level map of `archon_search/`. One row per module, grouped by layer. Use it to find where a change belongs before reading code. Cross-cutting flows are in [100_system_architecture_overview.md](100_system_architecture_overview.md); the wire surface is in [600_api_reference_or_public_interface.md](600_api_reference_or_public_interface.md).

## Principles

1. **One module, one layer.** A module that touches two layers (e.g. parser code in a route handler) is a smell — escalate before merging.
2. **Layers depend downward only.** Server depends on Query/Ingest; Query/Ingest never imports Server.
3. **Names mirror responsibilities.** `routes_*.py` are HTTP edges; `*_meta.py` are metadata models; `_types.py` is the dataclass spine.
4. **Underscored modules are internal.** `_types.py`, `_diagnostics.py`, `_helpers.py`, `_durable_io.py` are not part of the public import surface.
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
| `archon_search/chunker.py` | Split parsed Markdown into chunk records of a target size. Assigns `start_offset`/`end_offset` (character offsets into the post-front-matter text) to each `ChunkRecord`; these transient fields are used by the enricher and are not persisted to LanceDB. | `DocumentChunker` |
| `archon_search/enricher.py` | **C3a** — Scan a text document for ATX/setext/RST headings (fenced-code-block exclusion applied) and populate `_heading` / `_section_path` in each `ChunkRecord.metadata`. `prepare(text)` builds an immutable `HeadingTable` (sorted by offset); `enrich_chunk(chunk, *, heading_table=None, page_table=None)` bisects the table to resolve the nearest preceding heading and its ancestor chain. Empty strings are returned for chunks preceding all headings and for non-text (binary) formats. **C3b** — `preprocess(text)` is a sibling method for docling-parsed sources (PDF/image): it excises `PAGE_BREAK_MARKER` strings, builds a post-removal `(offset, page)` coordinate table, and returns `(cleaned_text, page_table)`. The `enrich_chunk` page branch uses the table to populate `_page_start` (always present for docling sources) and `_page_end` (only when the chunk spans a page boundary). `is_docling_source(subtype)` and `source_subtype_for(suffix)` are module-level helpers that gate the pipeline's page-break path. `PAGE_BREAK_MARKER` and `PAGE_BREAK_MARKER_LEN` are module-level constants; the marker is an internal implementation detail and never reaches `ChunkRecord.text` or the FTS index. | `MarkdownEnricher`, `HeadingEntry`, `HeadingTable`, `PAGE_BREAK_MARKER`, `PAGE_BREAK_MARKER_LEN`, `source_subtype_for`, `is_docling_source` |
| `archon_search/code_enricher.py` | **C3c** — AST-based code symbol context enrichment for source-code files. `CodeEnricher` follows the same `prepare()` / `enrich_chunk()` two-pass protocol as `MarkdownEnricher`. `prepare(text, ext, file_path, collection_root)` invokes tree-sitter (optional `[code]` dep) to parse the source and build a `ScopeTable` of `ScopeEntry` records (each covering a function, method, or class scope); missing grammars and parse failures degrade gracefully. `enrich_chunk(chunk, scope_table)` resolves the innermost scope for each chunk's `start_offset` via bisect and returns a 5-field metadata dict. `CODE_EXTENSIONS` is the frozenset of dispatched extensions; `_module_path()` derives a dotted module path from the file path and optional collection root. Tree-sitter is never imported at module level, so the module is importable without `[code]` installed. | `CodeEnricher`, `ScopeEntry`, `ScopeTable`, `CODE_EXTENSIONS` |
| `archon_search/_path_safety.py` | Validate caller-supplied ingest paths at the request boundary (HTTP `POST /collections`, `POST /ingest`; MCP `ingest_file`, `ingest_directory`). Rejects empty/whitespace-only/NUL/non-absolute/`..`-traversal inputs. Symlink resolution and absolute-path scope are intentionally **not** validated (deferred to a future `allowed_dirs` feature). | `validate_ingest_path`, `PathUnsafeError` |
| `archon_search/acl.py` | Parse `_acl` front matter and `.acl` sidecars; resolve effective ACL per document; filter search results by request namespace. | `resolve_acl`, `apply_acl_filter`, `is_acl_allowed`, `read_acl_sidecar`, `parse_acl_value`, `is_acl_namespace_valid` |
| `archon_search/language_detector.py` | **C2** — Per-document language detection using the fasttext `lid.176.ftz` model. Lazy-loads the model; detects language on the first 2000 chars of Markdown output; strips `__label__` prefix; normalizes to ISO 639-1 (2-letter) or ISO 639-3 (3-letter) via `_FASTTEXT_ISO_MAP`; returns `"unknown"` when confidence is below `SearchConfig.language_detection_confidence_threshold` or text is empty. Runs in a thread pool to avoid blocking the event loop. Only active when `config.multilingual=True`. | `LanguageDetector`, `FASTTEXT_MODEL_FILENAME`, `FASTTEXT_MODELS_DIR` |

## Index

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/embedder.py` | Dense-embedding façade with a Protocol backend. Default backend is fastembed. | `Embedder`, `EmbedderBackend`, `ModelEmbedder`, `make_embedder` |
| `archon_search/embedder_cache.py` | LRU cache for `Embedder` instances keyed by model name. Avoids reloading the same model weights when multiple collections use different models in the same process. Capacity is controlled by `[database].embedder_cache_size` (default 3); `[database].eager_load_embedders = true` pre-warms all known collection models at startup. Evicts the least-recently-used model when the cache is full. | `EmbedderCache` |
| `archon_search/store.py` | LanceDB-backed store: vector ANN + FTS, RRF fusion in `hybrid_search`, document/chunk lifecycle, namespace + ACL migrations, collection metadata persistence. `hybrid_search_with_trace` is a thin instance-method delegate to the module-level `_hybrid_search_with_trace`, which returns `list[ScoredSearchCandidate]` with full score provenance — used by `SearchPipeline.explain`. Predicates are built via `_where_eq`/`_where_in` (defense-in-depth quoting), and `StoreBusyError` is raised when the per-collection ingest lock cannot be acquired. | `SearchStore`, `SearchStore.hybrid_search_with_trace`, `StoreBusyError`, `validate_metadata`, `parse_metadata` |
| `archon_search/store_filters.py` | Single source of truth for SQL predicate building and literal quoting for LanceDB (DataFusion) `where`/`delete`/`count_rows` predicates; `build_where` compiles `SearchFilters` → SQL, `_sql_quote_str` is used by `store.py`'s `_where_eq`/`_where_in` as the defense-in-depth boundary behind upstream identifier regex gates. | `_sql_quote_str`, `build_where`, `_compute_fetch`, `escape_like` |
| `archon_search/reranker.py` | Cross-encoder second-stage rerank façade with a Protocol backend. | `Reranker`, `RerankerBackend`, `ModelReranker`, `make_reranker` |

## Query

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/pipeline.py` | Orchestrates ingest (`ingest_file`, `ingest_directory`) and query (`search`, `search_with_context`, `explain`). Computes per-collection centroid on directory ingest and may trigger description regeneration. `explain` fetches an amplified candidate pool via `store.hybrid_search_with_trace`, ACL-filters, optionally reranks the entire pool via `_rerank_with_trace`, then splits top-k results from up to 20 near-misses. **C4**: `search`, `search_many`, and `search_with_context` accept `query_vector: list[float] \| None = None`; when provided, the ANN lookup uses this vector instead of embedding the query. **C5**: `search`, `search_many`, `search_with_context`, and `explain` gain `rag_fusion: bool`, `rag_fusion_generator`, and `rag_fusion_config` parameters. `_fuse_rag_fusion_results()` implements second-pass cross-variant RRF. `SearchPipelineResult` gains `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`. `ExplainPipelineResult` gains those plus `rag_fusion_failure_reason` and `rag_fusion_sub_query_results`. | `SearchPipeline`, `SearchPipelineResult`, `ExplainPipelineResult`, `ExplainStageError`, `RagFusionSubQueryInfo`, `SearchWithContextResult`, `create_pipeline` |
| `archon_search/hyde.py` | **C4** — HyDE (Hypothetical Document Embeddings) query expansion. `HyDEGenerator` lazy-imports `anthropic`, applies a per-process token-bucket rate limiter, generates a short hypothetical answer passage via the Anthropic API, and embeds it with the local fastembed model. Raw query text is never logged — log messages use `_query_fingerprint` (SHA-256, 16-char hex) from `archon_search._privacy` for correlation. Falls back (`None` return) on timeout, API error, rate limit, missing key, or missing package. `resolve_hyde_vector` is the route-layer helper that applies the operator kill-switch (`config.hyde.enabled`) and translates the `(query, hyde, generator, config)` quadruple to `(vector \| None, hyde_applied: bool)`. | `HyDEGenerator`, `resolve_hyde_vector` |
| `archon_search/rag_fusion.py` | **C5** — RAG Fusion multi-query decomposition. `RAGFusionGenerator` lazy-imports `anthropic`, applies a per-process token-bucket rate limiter, calls the Anthropic API to generate N semantic query variants, and validates each variant (≤500 chars, no control sequences). Returns `[]` on timeout, API error, rate limit, missing key, or other failure; raises `RAGFusionDependencyError` if package not installed (→ route returns 422). Raw query text is never logged — log messages use `_query_fingerprint` from `archon_search._privacy`. | `RAGFusionGenerator`, `RAGFusionDependencyError` |
| `archon_search/_diagnostics.py` | Internal diagnostics types used by the explain path. `ScoredSearchCandidate` carries per-candidate score provenance plus ACL token list (`acl: list[str] \| None`) and A1/A2 metadata fields (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, `metadata`). Not part of the public import surface. | `ScoredSearchCandidate`, `SearchScoreBreakdown` |

## Routing

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/router.py` | Fetch collection metadata over JSON-RPC, score each centroid against the query embedding, apply a confidence gate, build a three-tier shortlist for the decomposer. **B4**: accepts `strategy: Literal["centroid", "hybrid"]` and `description_weight: float` constructor parameters. Under `strategy="hybrid"`, blends centroid cosine with `description_embedding` cosine: `score = (1 - w) * centroid_score + w * description_score`; falls back to pure centroid for collections with missing or dimensionally-inconsistent `description_embedding`. `_score_collections` is the shared scoring helper (extracted for A4); `rank_with_scores` returns every supplied collection paired with its centroid similarity, bypassing the confidence-threshold gate — used exclusively by `/explain`. Accepts `initial_metadata` for constructor-time injection; exposes `invalidate()` to clear the cached metadata for long-lived router instances. | `MultiCollectionRouter`, `MultiCollectionRouter.rank`, `MultiCollectionRouter.rank_with_scores`, `MultiCollectionRouter._score_collections`, `MultiCollectionRouter.invalidate` |

## Metadata

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/collection_meta.py` | Dataclass for per-collection metadata: name, centroid, description, doc/chunk counts, embedding model, timestamps, namespace. **B4**: adds `description_embedding: list[float] \| None = None`. **C1**: adds `active_embedding_model: str`, `pending_embedding_model: str \| None`, `needs_reindex: bool`, `reindex_job_id: str \| None` — the four fields that power the per-collection model state machine. | `CollectionMeta` |
| `archon_search/description_generator.py` | Decide whether to regenerate a collection description and call the model (Haiku) to generate one. | `generate_description`, `_should_regenerate` (semi-public; gating heuristic) |

## Server

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/server/app.py` | FastAPI app factory. Wires `SearchPipeline`, `JobStore`, `SearchStore`, telemetry, CORS, BearerAuth. Lifespan runs store migrations on startup, drains telemetry on shutdown. | `create_app`, `run_server` |
| `archon_search/server/mcp.py` | FastMCP app factory exposing pipeline-shaped tools over MCP HTTP, sharing `APIKeyMiddleware`. Every tool validates its return value through a Pydantic schema from `mcp_schemas.py` before serialization; schema drift returns `{"error": "...", "code": "schema_validation_error"}`. | `create_app`, `create_mcp_http_app`, `_ERR_SCHEMA` |
| `archon_search/server/mcp_schemas.py` | MCP-specific Pydantic schemas for all 11 tool return shapes (`extra='forbid'`). `from_result()` classmethods map domain dataclasses to public contract types, excluding transient/internal fields (`vector`, `start_offset`, `end_offset`, `custom_score`, `centroid`, `centroid_sum`, `needs_recompute`, `needs_reindex`, `reindex_job_id`, `namespace`, `mutations_since_recompute`, `described_at_doc_count`). `active_embedding_model` is exposed as `embedding_model` in all collection schemas. | `McpSearchResultSchema`, `McpSearchResponse`, `ExcludedCollectionMcpSchema`, `ContextChunkSchema`, `SearchWithContextItemSchema`, `SearchWithContextResponse`, `CollectionListItemSchema`, `CollectionDetailSchema`, `CollectionMetaMcpSchema`, `IngestResultSchema`, `DocumentInfoSchema`, `DeleteDocumentSchema` |
| `archon_search/server/middleware_auth.py` | Bearer-token middleware; resolves namespace per request; constant-time key comparison; exempts `/health`, `/docs`, `/openapi.json`, `/redoc`. | `APIKeyMiddleware`, `_EXEMPT_PATHS` |
| `archon_search/server/routes_health.py` | `GET /health`. | `router` |
| `archon_search/server/routes_state.py` | `GET /indexing-state`. | `router` |
| `archon_search/server/routes_status.py` | `GET /status`. | `router` |
| `archon_search/server/routes_search.py` | `POST /search`. | `router`, `SearchRequest`, `SearchResponse`, `SearchResultSchema` |
| `archon_search/server/routes_route.py` | `POST /route` — driver for `MultiCollectionRouter`. Defines a route-local Pydantic `RouteResponse` distinct from the dataclass `RouteResponse` in `archon_search/types.py` (two public objects share the name). | `router`, `RouteRequest`, `RouteResponse` (Pydantic) |
| `archon_search/server/routes_collections.py` | `GET/POST /collections/`, `GET/DELETE/PATCH /collections/{name}`, `POST /collections/{name}/reindex`. Issues jobs for add, reindex, and delete (non-empty collections). **C1**: `PATCH /collections/{name}` implements the per-collection embedding model state machine; `POST /collections/` gains optional `embedding_model` field. | `router`, `AddCollectionRequest`, `PatchCollectionBody` |
| `archon_search/server/routes_jobs.py` | `POST /ingest`, `GET /jobs/{id}`, `DELETE /jobs/{id}`. Spawns the ingest task against `SearchPipeline`. | `router`, `IngestRequest` |
| `archon_search/server/routes_explain.py` | `POST /explain` (A4). All schemas use `extra="forbid"`. Public Pydantic models: `ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss` (no `text` field — structurally absent), `ExplainScoreBreakdown`, `RoutingExplain`, `RoutingCandidate`. `ExplainResponse.from_pipeline_result` is the seam between private `ScoredSearchCandidate` and the public wire schema. | `router`, `ExplainRequest`, `ExplainResponse`, `ExplainResult`, `ExplainNearMiss`, `ExplainScoreBreakdown`, `RoutingExplain`, `RoutingCandidate` |
| `archon_search/server/_ingest_lock.py` | Shared helper for `POST /ingest` and `POST /collections/` to pre-acquire the per-collection ingest lock and return a `503` + `Retry-After` (`{"error": "store_busy", ...}`) when a reindex holds it. | `acquire_collection_lock_or_503` |
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
| `archon_search/telemetry/entry.py` | Structural model + factories. **Factories take no `query` parameter — raw queries are never logged.** `EndpointKind` includes `explain` (A4); `from_explain_result` records `collection` and `result_count` (scalar) with no query text or result doc IDs. | `TelemetryEntry`, `EndpointKind`, `Status`, `ErrorKind` |
| `archon_search/telemetry/writer.py` | Background JSONL writer; one line per call into `~/.archon-search/search-logs/` via a persistent per-date fd with rotate-only fsync (not per-line fsync; see [130 durability contract](130_data_architecture_and_persistence.md#telemetry-durability-rotate-only-fsync)). | `TelemetryWriter` |
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
| `archon_search/eval/live_report.py` | Builds a pass/fail/report-only verdict report from a live eval run; writes JSON + Markdown artifacts consumed by the `archon-search-eval-live.yml` CI workflow. Never raises — silently absorbs missing thresholds or baselines and falls back to `report_only`. | `MetricVerdict`, `LiveEvalReport`, `build_live_report`, `write_live_report_json`, `write_live_report_markdown`, `load_live_thresholds` |
| `archon_search/eval/metrics.py` | Recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles, chunk-to-doc dedup. | `compute_recall_at_k`, `compute_mrr`, `compute_ndcg_at_k`, `compute_reranker_lift`, `compute_routing_accuracy`, `compute_latency_percentiles`, `deduplicate_to_doc_rankings` |
| `archon_search/eval/fixtures.py` | Load `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `corpus/`. | `EvalDocument`, `EvalQuery`, `RelevanceLabel`, `EvalCorpus`, `load_eval_corpus`, `build_doc_collection_map` |
| `archon_search/eval/types.py`, `_hashing.py`, `_tracing.py` | Shared eval types, hashing helpers, trace capture. | see source |

## Cross-cutting

| Module | Purpose | Key public symbols |
|---|---|---|
| `archon_search/config.py` | Load/save `~/.archon-search/archon-search.toml`; coerce `telemetry.export_enabled = true` to `false` with a warning; coerce types. **C4**: adds `HyDEConfig` dataclass (`enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`) and a `[hyde]` TOML section loader; `SearchConfig` gains `hyde: HyDEConfig`. **C5**: adds `RAGFusionConfig` dataclass (`enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`, `num_queries`) and a `[rag_fusion]` TOML section loader; `SearchConfig` gains `rag_fusion: RAGFusionConfig`. | `SearchConfig`, `TelemetryConfig`, `HyDEConfig`, `RAGFusionConfig`, `ConfigError`, `load_config`, `save_config`, `get_default_config_path` |
| `archon_search/constants.py` | Default namespace and namespace validation. | `DEFAULT_NAMESPACE`, `_validate_namespace` |
| `archon_search/_types.py` | Internal dataclass spine: `ChunkRecord`, `SearchResult`, `DocumentInfo`, `CollectionInfo`, `IngestResult`. **C3a**: `ChunkRecord` carries two transient fields `start_offset: int = -1` and `end_offset: int = -1` (character offsets into the post-front-matter text); these are not persisted to LanceDB (absent from `_schema()` and `_do_ingest()`). | (internal) |
| `archon_search/types.py` | Public job/collection types: `JobStatus`, `IngestJob`, `ReindexJob`, `DeleteJob`, `Query`, `RouteResponse`, `Collection`, `CollectionDetail`, `Chunk`. | (public surface) |
| `archon_search/progress.py` | Indexing progress state: `IndexingStatus`, `CollectionProgress`, `IndexingState`, `IndexingStateStore`, ETA helpers. `IndexingStateStore` is thread-safe — all mutating methods (`write`, `update_collection`, `remove_collection`, `set_trigger`, `reset_in_progress`) are serialized by an internal `threading.RLock`; `read()` is an unlocked snapshot. `write()` delegates to `_durable_io.atomic_write_json` for fsync-backed atomic persistence. | `IndexingStatus`, `CollectionProgress`, `IndexingState`, `IndexingStateStore` (incl. `reset_in_progress`), `compute_eta_seconds` |
| `archon_search/key_manager.py` | Resolve the API key from env (`ARCHON_SEARCH_API_KEY`), file (`ARCHON_SEARCH_KEY_FILE` or `~/.archon-search/.search.env`), or generate one (durable write with mode `0600` set at creation via `_durable_io.atomic_write_bytes`). | `load_or_generate_key` |
| `archon_search/watcher.py` | Watchdog-driven `CollectionWatcher` + `WatcherManager` with a debounced handler. | `CollectionWatcher`, `WatcherManager` |
| `archon_search/sync.py` | `SearchCollectionSync`: full reconcile between on-disk corpora and the index; manifest-based collection naming. | `SearchCollectionSync`, `SyncResult`, `path_to_collection_name` |
| `archon_search/install.py` | High-level installer: disk-space check, lock acquisition, Jina/fasttext license gates, profile-aware config write, model pre-warm, service register, health poll, rollback on failure. `NeedsForceDeleteError` signals a mismatched-profile reinstall that requires `--force --delete-db`. Key module-level helpers: `_detect_config_hand_edits` (warns before overwriting hand-edited config on re-run), `_print_next_steps` (post-install guidance), `_render_summary` (install summary with db path, server URL, API key hint, download size), `_prompt_optional_features` (7 optional-feature prompts each preceded by a plain-text explanation). Called by `cli/install_cmd.py`. | `SearchInstaller`, `NeedsForceDeleteError`, `_detect_config_hand_edits`, `_print_next_steps`, `_render_summary` |
| `archon_search/profiles.py` | Defines the three tiered install profiles (`minimal`, `balanced`, `max`) for both English and multilingual stacks. `ENGLISH_PROFILES` and `MULTILINGUAL_PROFILES` are `dict[str, InstallProfile]`; `get_profile(name, multilingual)` is the single lookup entry point. The Jina reranker constant (`JINA_RERANKER_MODEL`) lives here so the license-gate check in `install.py` has a single reference. | `InstallProfile`, `ENGLISH_PROFILES`, `MULTILINGUAL_PROFILES`, `VALID_PROFILE_NAMES`, `JINA_RERANKER_MODEL`, `get_profile` |
| `archon_search/_diagnostics.py` | Internal diagnostics helpers. | internal |
| `archon_search/_durable_io.py` | Durable fsync-backed atomic file writes (fsync file → `os.replace` → fsync parent dir). All durable JSON/bytes state writes route through here; see [130_data_architecture_and_persistence.md](130_data_architecture_and_persistence.md#durability-contract). | `atomic_write_json`, `atomic_write_bytes` |
| `archon_search/_privacy.py` | **C5** — Shared privacy utility. `_query_fingerprint(query: str) -> str` returns `sha256(query)[:16]` as a non-reversible 16-char hex log-correlation token. Used by both `hyde.py` and `rag_fusion.py` to avoid duplicate implementations. Never logs raw query text. | `_query_fingerprint` |

**Purpose**: Document the practical capacity envelope of a single-process `archon-search` deployment and the cost surfaces that bend that envelope.
**Audience**: SREs and sysadmins sizing or scaling `archon-search` on a single host.
**Status**: Draft
**Last reviewed**: 2026-07-29
**Next review**: 2027-07-29

# Capacity and Performance

`archon-search` is single-process by design ([`ADRs/01_lancedb_as_local_vector_store.md`](../ADRs/01_lancedb_as_local_vector_store.md)). There is no horizontal scale story — when you outgrow one host, you redesign, you do not add a replica. This doc lays out the cost surfaces an operator needs to reason about and the heuristics that work today. The numbers below are derived from the code paths, not from a production benchmark harness; treat latency detail as a regression guard, not an SLA (see [`../Architecture/210_performance_and_scalability.md`](../Architecture/210_performance_and_scalability.md)).

## Principles

1. **No production SLA.** Latency p50/p95 from telemetry and `tests/eval/` are regression guards. Plan capacity in absolute terms, not against an SLO. To validate a tuning change, use the eval harness — see [`tests/eval/README.md`](../../tests/eval/README.md).
2. **Ingest dominates write cost; the search path is bounded by the reranker.** The centroid is now maintained incrementally (B5, shipped): `store.ingest_chunks()` updates a running `(centroid_sum, count)` on every batch, so a normal ingest never re-reads the whole collection. The full-collection re-read (`recompute_collection_meta`, `pipeline.py`) only fires from sync reconciliation (`sync.py`), reindex, and the eval runner.
3. **FTS is maintained incrementally.** Ingest calls `store.optimize_fts()` (O(delta), not O(collection-size)); `rebuild_fts_index` is reserved for operator-initiated full repair (`archon-search collection reindex`).
4. **Router cache never drifts on the server path (A6).** `POST /route` builds a fresh `MultiCollectionRouter` per request, so cached centroids never outlive a single call — see [Router cache](#router-cache-a6). The earlier stale-centroid concern (CON-2) is addressed; do not plan around it.
5. **GPU helps embedding throughput, not LanceDB I/O.** ONNX providers are written to config at install time by `install.py::configure_providers`; `platform/runtime.py` only detects GPU type.

## Single-process limits

- **One LanceDB writer.** Two `archon-search` processes against the same `db_path` will corrupt the database. Use a single supervised instance.
- **One Uvicorn worker.** The server runs with the default worker count (1). Concurrency is asyncio inside one process.
- **Background tasks are unbounded by count, bounded by work.** Ingest is spawned via `asyncio.create_task` and tracked in `app.state._background_tasks` for graceful shutdown; there is no global semaphore. `[jobs] max_concurrent_bulk` (default 1) bounds concurrent bulk jobs.
- **Telemetry queue is bounded at 1024 entries.** Excess drops the oldest, never the newest, with a rate-limited warning.

Practical implication: a single host comfortably serves one to a few interactive clients (a developer, an editor plug-in, an LLM agent) plus background ingest. It is not a multi-tenant search service. For multi-instance layouts see [`10_deployment_topologies.md`](10_deployment_topologies.md).

## Cost surfaces by operation

### Search (`POST /search`, `POST /route`)

Path: namespace check → router (per-request metadata) → vector search + FTS → RRF fuse → cross-encoder rerank → optional context expansion.

| Stage | Cost driver | Order of magnitude |
| --- | --- | --- |
| Router centroid pre-rank | `O(collections)` cosine vs. centroids. | Trivial up to hundreds of collections. #Unverified |
| LanceDB vector search | LanceDB defaults; only the FTS column has an explicit index (`store.py`) — no IVF index is built. | #Unverified — no benchmark in repo. |
| FTS search | LanceDB-managed BM25 over the same table. | #Unverified |
| RRF fuse | `O(top_k_retrieve)`. | Trivial. |
| Cross-encoder rerank | `O(top_k_retrieve)` forward passes through the reranker. | Dominant on CPU. GPU speedup not benchmarked in repo. #Unverified |
| Context-window expansion | One extra LanceDB fetch per result for neighbouring chunks. | Overhead percentage not benchmarked. #Unverified |

`[database] top_k_retrieve` (default 15) and `top_k_return` (default 5) are the main knobs — increasing `top_k_retrieve` raises reranker cost linearly.

**RAG Fusion / multi-collection fan-out.** When `rag_fusion` or multi-collection search is used, the pipeline runs per-collection, per-variant fan-out legs, each trimmed to `[search] fanout_leg_trim` (default 40) candidates, capped at `[search] max_fanout` (default 8) legs, and bounded by `[search] fanout_timeout_seconds` (default 30.0). More variants and more collections multiply reranker input proportionally.

### LLM-augmented search (HyDE / RAG Fusion)

HyDE and RAG Fusion add a synchronous LLM call to the query path before retrieval (disabled by default). Each is rate-limited and time-boxed:

| Knob (`[hyde]` / `[rag_fusion]`) | Default | Effect |
| --- | --- | --- |
| `max_requests_per_minute` | 60 | Client-side rate limit on LLM calls; excess requests fall back to plain search. |
| `timeout_seconds` | 10.0 | Per-call deadline; on timeout the query proceeds without the LLM augmentation. |
| `[rag_fusion] num_queries` | 2 | Query variants generated (1–5); each variant adds a fan-out leg. |

Cost is one LLM round-trip per search (plus provider latency), gated by the rate limit. Provider is `anthropic` by default (also `ollama` / `openai` / `claude_cli`). See [`../UserManual/60_searching.md`](../UserManual/60_searching.md) for usage and provider setup.

### Ingest (`POST /ingest`, `POST /collections`, `POST /collections/{name}/reindex`)

Per document: parse → chunk → embed (batched) → upsert into LanceDB → incremental centroid update (B5) → incremental FTS optimize → update `.indexing-state.json`.

- **Streaming / incremental chunking (D4, shipped).** Chunks are sliced into fixed 512-chunk batches (`_INGEST_CHUNK_BATCH_SIZE`, an internal constant — not a config key) and each batch is embedded and written before the next is produced. Directory ingest no longer accumulates every vector in memory. A large single file or a large corpus completes on a memory-constrained host (e.g. a 1 GB container) because peak RAM is bounded by one batch (~2 MB) plus parse-time memory. Parse-time RAM (docling for PDFs) is still owned by the parser and is not batched — bound pathological inputs with `[ingest] max_file_mb` (0 = unlimited; REST returns 413).
- **Centroid (B5).** Maintained incrementally per batch; only `reindex`, sync reconciliation, and the eval runner trigger a full-collection re-read.
- **FTS (incremental).** `store.optimize_fts()` is O(delta); a single-file update into a 50,000-chunk collection completes in milliseconds. `delete_document` also calls `optimize_fts` after removal to prevent phantom hits.

Concrete chunk-count thresholds for "imperceptible" vs. "practical ceiling" are not benchmarked in this repo; treat sizing figures below as estimates. #Unverified

### Graph subsystem (`[graph] enabled = true`)

Enabling the graph subsystem adds cost surfaces at several points. This section is a capacity summary only — operations detail lives in [`60_graph_operations.md`](60_graph_operations.md).

| Surface | When it runs | Cost driver |
| --- | --- | --- |
| Entity / def-ref extraction | After each ingest (post-persist, non-blocking) | spaCy NER in a worker thread; AST parse for code files (`tree-sitter`). Proportional to chunk count. |
| Community rebuild (Leiden) | `POST /graph/{collection}/rebuild-communities`, or `MaintenanceLoop` GC | Leiden clustering over the whole collection graph; serialised per collection. O(nodes + edges). |
| PPR (`graph_mode="ppr"`) | Query time | Personalised PageRank via `networkx.pagerank` in a worker thread; seeded from matched entities. Adds per-query latency on top of retrieval. |
| PageRank precompute | Debounced background pass (`MaintenanceLoop`) | Runs over code-symbol edges; `gc_rebuild_cpu_priority` (low/normal/high) bounds contention. |
| Synonym / LLM enrichment | Post-ingest callback (optional; `enrichment_auto`) | Embedding-based synonym detection; never blocks or fails an ingest (logs WARNING on error). |

Extraction and enrichment are fire-and-forget: a graph write failure never fails the ingest. Community rebuild is the heavy operation — schedule it, don't run it inline on large collections.

### Watcher

`watcher.py` debounces filesystem events and feeds them into the sync layer. There is no event-rate limiter — pathological churn on a watched directory queues ingest work indefinitely. If an external tool rewrites a watched file frequently (e.g. a Jupyter notebook on auto-save), expect proportional ingest load.

### Router cache (A6)

`MultiCollectionRouter._cached_metadata` is populated on first use within a single router instance. Current state:

- The FastAPI `/route` path builds a **fresh router per request**, so the old "stale centroids until restart" symptom does **not** occur on the server path — each request re-fetches metadata. A regression test pins this per-request lifecycle.
- For **non-FastAPI** consumers, `MultiCollectionRouter` exposes `invalidate()` to clear the cache and an `initial_metadata` ctor param for constructor-time injection (the eval runner uses the latter).
- A future migration to a shared, long-lived server-side router would need to call `invalidate()` after collection mutations; that is out of scope today. Capacity planning need not assume centroid drift.

## Sizing heuristics

Treat these as starting points, not contracts. Verify against your own data with the eval harness in [`tests/eval/README.md`](../../tests/eval/README.md).

| Resource | Cheap workstation (laptop) | Server-class host |
| --- | --- | --- |
| CPU | 4 cores | 8+ cores (reranker scales with cores when on CPU) |
| RAM | 8 GB free | 16 GB+ free; embedder and reranker each hold model weights |
| Disk | NVMe; headroom proportional to raw corpus #Unverified | NVMe; multi-× raw corpus for LanceDB column files + FTS + telemetry #Unverified |
| GPU | Apple Silicon (CoreML) or none | CUDA-capable card for production reranker latency |
| Corpora | ≤ 5 collections, ≤ 50 000 chunks each #Unverified | ≤ 50 collections, ≤ 100 000 chunks each #Unverified |

Disk-usage multipliers (dense-vector overhead, FTS overhead, per-telemetry-entry size) are not benchmarked in this repo. What is verified: telemetry retention is capped by `[telemetry] retention_days`; the pruner runs every 24 h and never deletes today's file.

## Tuning knobs

All knobs live in `archon-search.toml`; `archon-search.toml.example` is the canonical list. Defaults below verified against `archon_search/config.py`.

**Retrieval (`[database]`, `[search]`)**

- `chunk_size` (512 tokens). Smaller chunks raise chunk count linearly; larger chunks hurt rerank precision.
- `top_k_retrieve` / `top_k_return` (15 / 5). Linear effect on reranker latency.
- `top_k_max` (100). Hard cap on a per-request `top_k`.
- `embedder_cache_size` (3). Number of embedder models held in the LRU cache; raise only for genuine multi-model workloads.
- `eager_load_embedders` (false). When true, ONNX weights are reconstructed at startup — for the embedder cache *and* the reranker cross-encoder — so boot is slower but there is no first-query latency spike.
- `centroid_recompute_threshold` (10 000). Chunk-count trigger for a full centroid re-read on the paths that still recompute.
- `[search] max_fanout` (8) / `fanout_leg_trim` (40) / `fanout_timeout_seconds` (30.0). Bound multi-collection / RAG-Fusion fan-out cost.

**Routing (`[routing]`)**

- `routing_shortlist_size` (8). Maximum collections the router considers.
- `routing_confidence_threshold` (0.30). Below this, `MultiCollectionRouter.rank` returns `[]`; any "fallback to pinned collections" happens upstream in `routes_route.py`, not in the router.
- `routing_strategy` (`centroid`; also `hybrid`) and `routing_description_weight` (0.3, used by the hybrid strategy).

**Providers & retention**

- `[database] providers`. ONNX execution providers; set at install time, not auto-tuned at runtime.
- `[telemetry] retention_days` (30). Bounds the JSONL directory size.

## Scaling out (not supported)

Horizontal scale is explicitly out of scope. When one host is no longer enough:

- **Split by namespace.** Run independent `archon-search` instances per workload on different hosts; clients pick the host. This is the only supported pattern — see [`10_deployment_topologies.md`](10_deployment_topologies.md).
- **Do not share `db_path` over NFS.** LanceDB assumes local POSIX semantics.
- **Roadmap items** for horizontal scaling and pluggable backends are gated behind earlier phases. Do not plan against them.

## Related documents

- [`00_index.md`](00_index.md) — OperatorGuide table of contents.
- [`10_deployment_topologies.md`](10_deployment_topologies.md) — single-host vs. multi-instance layouts.
- [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md) — latency-regression alert recipe and the health/status surface.
- [`60_graph_operations.md`](60_graph_operations.md) — running the graph subsystem, community rebuilds, GC.
- [`../UserManual/60_searching.md`](../UserManual/60_searching.md) — HyDE / RAG Fusion usage and provider setup.
- [`../Architecture/210_performance_and_scalability.md`](../Architecture/210_performance_and_scalability.md) — latency budget detail and eval-harness regression model.
- [`tests/eval/README.md`](../../tests/eval/README.md) — how to validate a tuning change.

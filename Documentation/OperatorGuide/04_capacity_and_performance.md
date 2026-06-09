**Purpose**: Document the practical capacity envelope of a single-process `archon-search` deployment and the cost surfaces that bend that envelope.
**Audience**: SREs and sysadmins sizing or scaling `archon-search` on a single host.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Capacity and Performance

`archon-search` is single-process by design (`ADRs/01_lancedb_as_local_vector_store.md`). There is no horizontal scale story — when you outgrow one host, you redesign, you do not add a replica. This doc lays out the cost surfaces an operator needs to reason about and the heuristics that work today. Per-stage tracing (`B1`) does not yet exist; the numbers below are derived from the code paths, not from a benchmark harness.

## Principles

1. **No production SLA.** Latency p50/p95 from telemetry and `tests/eval/` are regression guards (`EVL-1`, `Architecture/160…md`). Plan capacity in absolute terms, not against an SLO.
2. **Ingest dominates write cost; the search path is bounded by the reranker.** Batch ingest (`SearchPipeline.ingest_directory`, `pipeline.py:258-289`) computes the centroid from vectors accumulated in-memory during the batch; single-file `ingest_file` does not touch the centroid. The full-collection re-read of vectors (`recompute_collection_meta`, `pipeline.py:368-401`) only fires from sync reconciliation, reindex, and the eval runner (`CON-4`). **C6 (shipped)**: FTS is now maintained incrementally via `optimize_fts()` (O(delta), not O(collection-size)); `rebuild_fts_index` is no longer called from normal ingest or sync paths.
3. **Router cache is per-request in the FastAPI runtime.** `POST /route` builds a fresh `MultiCollectionRouter` per request (`routes_route._build_router`), so its `_cached_metadata` never outlives a single call — the runtime does not drift as collections evolve. `MultiCollectionRouter` now also exposes `invalidate()` and an `initial_metadata` ctor param for long-lived router consumers (`CON-2`, addressed by A6). Capacity planning need not assume centroid drift on the server path.
4. **GPU helps embedding throughput, not LanceDB I/O.** ONNX providers are written to config at install time by `install.py::configure_providers` (`install.py:135-165`); `platform/runtime.py` only detects GPU type.

## Single-process limits

The host envelope:

- **One LanceDB writer.** Two `archon-search` processes against the same `db_path` will corrupt the database. Use a single supervised instance.
- **One Uvicorn worker.** `archon_search/server/app.py:run_server` calls `uvicorn.run(app, host=..., port=...)` with the default worker count (1). Concurrency is asyncio inside one process.
- **Background tasks are unbounded by count, bounded by work.** Ingest is spawned via `asyncio.create_task` (`routes_jobs.py:102`) and tracked in `request.app.state._background_tasks` for graceful shutdown (`routes_jobs.py:103-104`); there is no global semaphore.
- **Telemetry queue is bounded at 1024 entries.** Excess drops the oldest, never the newest, with a rate-limited warning (`telemetry/writer.py`).

Practical implication: a single host comfortably serves one to a few interactive clients (a developer, an editor plug-in, an LLM agent) plus background ingest. It is not a multi-tenant search service.

## Cost surfaces by operation

### Search (`POST /search`, `POST /route`)

Path: namespace check → router (cached metadata) → vector search + FTS → RRF fuse → cross-encoder rerank → optional context expansion. Per-stage timings are not exposed yet (`B1`).

| Stage | Cost driver | Order of magnitude |
| --- | --- | --- |
| Router centroid pre-rank | `O(collections)` cosine vs. cached centroids. | Trivial up to hundreds of collections. #Unverified |
| LanceDB vector search | LanceDB defaults; only the FTS column has an explicit index created in `store.py:445-451` — no IVF index is built. | #Unverified — no benchmark in repo. |
| FTS search | LanceDB-managed BM25 over the same table. | #Unverified |
| RRF fuse | `O(top_k_retrieve)`. | Trivial. |
| Cross-encoder rerank | `O(top_k_retrieve)` forward passes through the reranker. | Dominant on CPU. GPU speedup not benchmarked in repo. #Unverified |
| Context-window expansion | One extra LanceDB fetch per result for neighbouring chunks (`pipeline.py:306-338`). | Overhead percentage not benchmarked. #Unverified |

The `top_k_retrieve` (default 15) and `top_k_return` (default 5) caps in `config.py` are the main knob. Increasing `top_k_retrieve` raises reranker cost linearly — see `Architecture/210_performance_and_scalability.md`.

### Ingest (`POST /ingest`, `POST /collections`, `POST /collections/{name}/reindex`)

Per document: parse → chunk → embed (batched) → upsert into LanceDB → **recompute collection centroid** → **optimize FTS index** (C6: incremental, O(delta)) → update `.indexing_state.json`.

The two highlighted steps are the most expensive at scale and are tracked as separate debt items:

- **Centroid recompute (`CON-4`)**: `SearchPipeline.recompute_collection_meta` (`pipeline.py:368-401`) reads **all** vectors in the collection and recomputes the centroid. It is invoked from sync reconciliation (`sync.py:707`), reindex, and the eval runner (`eval/runner.py:484`) — not from every ingest. Batch `ingest_directory` instead computes the centroid from vectors accumulated in-memory during the batch (`pipeline.py:258-289`); single-file `ingest_file` does not update the centroid. Roadmap fix is `B5` (incremental `(sum, count)` maintenance).
- **FTS maintenance (C6 — shipped)**: as of C6, ingest calls `store.optimize_fts(collection)` (wrapping `table.optimize()`) at batch end instead of a full `rebuild_fts_index`. Cost is O(delta-size), not O(collection-size). A single-file update into a 50,000-chunk collection completes in milliseconds. `delete_document` also calls `optimize_fts` after removing chunks, preventing phantom hits. `rebuild_fts_index` is retained for operator-initiated full repair (`archon-search collection reindex`). The C6 ingest latency p95 regression guard lives in `tests/eval/thresholds.toml` (`[ingest_latency].single_file_p95_ms`). See `Architecture/210_performance_and_scalability.md` for the full C6 performance model.

Concrete chunk-count thresholds for "imperceptible", "user-visible", and "practical ceiling" are not benchmarked in this repo and should be treated as estimates only. #Unverified — beyond a few tens of thousands of chunks per collection, the `B5`/`C6` paydown becomes increasingly relevant.

### Watcher

`archon_search/watcher.py` debounces filesystem events and feeds them into the sync layer. There is no event-rate limiter — pathological churn on a watched directory will queue ingest work indefinitely. If you watch a directory that an external tool rewrites frequently (e.g. a Jupyter notebook on auto-save), expect proportional ingest load.

### Router cache caveats (`CON-2`, addressed by A6)

`MultiCollectionRouter._cached_metadata` is populated on first use within a single router instance. Implications:

- The FastAPI `/route` path builds a **fresh router per request** (`routes_route._build_router`), so the documented "stale centroids until restart" symptom does **not** occur on the server path — each request re-fetches metadata. A regression test pins this per-request lifecycle.
- For **non-FastAPI** router consumers, `MultiCollectionRouter` now exposes `invalidate()` to clear the cache and an `initial_metadata` ctor param for constructor-time injection. The eval runner is a constructor-injection consumer: `_run_router_for_query` (`eval/runner.py`) builds a fresh router per query and passes `initial_metadata=`, so its cache is seeded up front rather than re-fetched. A residual in-flight-fetch TOCTOU is documented on `invalidate()`.
- A future migration to a shared, long-lived router (the actual long-lived case) would need to call `invalidate()` after collection mutations or the stale-centroid symptom reappears; that migration is out of scope for A6.

## Sizing heuristics

Treat these as starting points, not contracts. Verify against your own data with the eval harness in `tests/eval/`.

| Resource | Cheap workstation (laptop) | Server-class host |
| --- | --- | --- |
| CPU | 4 cores | 8+ cores (reranker scales with cores when on CPU) |
| RAM | 8 GB free | 16 GB+ free; embedder and reranker each hold model weights |
| Disk | NVMe; size headroom proportional to raw corpus #Unverified | NVMe; multi-× raw corpus for LanceDB column files + FTS + telemetry #Unverified |
| GPU | Apple Silicon (CoreML) or none | CUDA-capable card for production reranker latency |
| Corpora | ≤ 5 collections, ≤ 50 000 chunks each #Unverified | ≤ 50 collections, ≤ 100 000 chunks each #Unverified (above this, see `B5`/`C6`) |

Disk usage multipliers (LanceDB dense-vector overhead, FTS overhead, per-telemetry-entry size) are not benchmarked in this repo and the often-quoted "1.5× + 0.5×" / "~8 KiB per entry" figures are #Unverified. What is verified: telemetry retention is capped by `retention_days`; the pruner runs every 24 h (`telemetry/pruner.py:70`) and never deletes today's file (`telemetry/pruner.py:44-45`).

## Tuning knobs

All knobs live in `archon-search.toml`. See `archon-search.toml.example` for the canonical list.

- `[database].chunk_size` (default 512 tokens). Smaller chunks raise chunk count linearly and slow centroid recompute proportionally; larger chunks hurt rerank precision.
- `[database].top_k_retrieve` / `top_k_return` (15 / 5). Linear effect on reranker latency.
- `[routing].routing_shortlist_size` (8). Maximum collections the router considers; only relevant once you exceed that.
- `[routing].routing_confidence_threshold` (0.30). When `max(similarity) < threshold` and scored collections exist, `MultiCollectionRouter.rank` returns `[]` (`router.py:154-155`); `get_pre_context` then skips the decomposer and clears `_last_routable_names` (`router.py:206-210`). Any "fallback to pinned collections" actually happens upstream in `routes_route.py` where pinned collections are merged into the final list — the router itself does not perform that fallback.
- `[routing].max_parallel_collections` (3). Parsed in `config.py:180-184` but **not consumed by any runtime path** — no multi-collection parallel search exists in the codebase today. The knob is effectively dead config. #Unverified as a behavioural knob.
- `[database].providers`. ONNX execution providers; set at install time, not auto-tuned at runtime.
- `[telemetry].retention_days` (30). Bounds the JSONL directory size.

## Scaling out (not supported)

Horizontal scale is explicitly out of scope (`Architecture/530_technical_debt_refactoring_roadmap.md`, "Out of scope"). When one host is no longer enough:

- **Split by namespace.** Run independent `archon-search` instances per workload on different hosts; clients pick the host. This is the only supported pattern.
- **Do not share `db_path` over NFS.** LanceDB assumes local POSIX semantics.
- **Roadmap items** `F5` (reassess horizontal scaling) and `F6` (pluggable backends) are gated behind the rest of Phases A–D. Do not plan against them.

## Related documents

- `Architecture/210_performance_and_scalability.md` — latency budget detail, eval-harness regression model.
- `Architecture/530_technical_debt_refactoring_roadmap.md` — `CON-2`, `CON-4` and other capacity-relevant debt.
- `Backlog/03_world_class_roadmap.md` — `A6`, `B5`, `C6` paydowns.
- `OperatorGuide/02_monitoring_and_alerts.md` — latency-regression alert recipe.
- `tests/eval/README.md` — how to validate a tuning change.

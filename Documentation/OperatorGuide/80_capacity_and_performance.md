**Purpose**: Document the practical capacity envelope of a single-process `archon-search` deployment and the cost surfaces that bend that envelope.
**Audience**: SREs and sysadmins sizing or scaling `archon-search` on a single host.
**Status**: Draft
**Last reviewed**: 2026-10-25
**Next review**: 2027-07-29

# Capacity and Performance

`archon-search` is single-process by design ([`ADRs/01_lancedb_as_local_vector_store.md`](../ADRs/01_lancedb_as_local_vector_store.md)). There is no horizontal scale story — when you outgrow one host, you redesign, you do not add a replica. This doc lays out the cost surfaces an operator needs to reason about and the heuristics that work today. The numbers below are derived from the code paths, not from a production benchmark harness; treat latency detail as a regression guard, not an SLA (see [`../Architecture/210_performance_and_scalability.md`](../Architecture/210_performance_and_scalability.md)).

## Principles

1. **No production SLA.** Latency p50/p95 from telemetry and `tests/eval/` are regression guards. Plan capacity in absolute terms, not against an SLO. To validate a tuning change, use the eval harness — see [`tests/eval/README.md`](../../tests/eval/README.md).
2. **Ingest dominates write cost; the search path is bounded by the reranker.** The centroid is now maintained incrementally (B5, shipped): `store.ingest_chunks()` updates a running `(centroid_sum, count)` on every batch, so a normal ingest never re-reads the whole collection. The full-collection re-read (`recompute_collection_meta`, `pipeline.py`) only fires from sync reconciliation (`sync.py`), reindex, and the eval runner.
3. **FTS is maintained incrementally.** Ingest calls `store.optimize_fts()` (O(delta), not O(collection-size)); `rebuild_fts_index` is reserved for operator-initiated full repair (`archon-search collection reindex`).
4. **Router cache never drifts on the server path (A6).** `POST /route` builds a fresh `MultiCollectionRouter` per request, so cached centroids never outlive a single call — see [Router cache](#router-cache-a6). The earlier stale-centroid concern (CON-2) is addressed; do not plan around it.
5. **GPU helps embedding throughput, not LanceDB I/O.** The torch device for the embedder and reranker is `[database].device` (`auto` by default: CUDA, then Apple MPS, then CPU); the wizard writes it at install time and `platform/runtime.py` only detects GPU type.

## Single-process limits

- **One LanceDB writer.** Two `archon-search` processes against the same `db_path` will corrupt the database. Use a single supervised instance.
- **One Uvicorn worker.** The server runs with the default worker count (1). Concurrency is asyncio inside one process.
- **One parse worker (child process), serializing all docling parsing.** PDFs and images are converted by docling in a single-worker `ProcessPoolExecutor` (`spawn`) owned by `DocumentParser`, not in the server process. Because one `SearchPipeline` holds one `DocumentParser` holds one `max_workers=1` pool, every concurrent ingest job, watcher sync, and bulk job funnels all PDF/image parsing through that one worker, one file at a time — `[jobs] max_concurrent_bulk > 1` does not buy parallelism for docling formats. The worker is spawned lazily on the first PDF/image parse, recycles after `[ingest] max_tasks_per_child` *cumulative* files (default 25) — not on any idle-time trigger — and, once idle, stays resident (with its ~1 GB model stack) until enough further parses eventually recycle it; there is no idle shutdown. The `spawn` context also starts a long-lived `multiprocessing.resource_tracker` process alongside the worker, so budget for two extra child processes, not one.
- **Ingest concurrency is capped, everything else is bounded by work.** `POST /ingest` and `POST /collections/` admit at most `[ingest].max_concurrent_tasks` (default 4) ingest tasks at once; the next request gets `503` `ingest_busy` with a `Retry-After` header before any lock, job or config write, and is **not** queued, so clients must retry. Ingest is spawned via `asyncio.create_task` and tracked in `app.state._background_tasks`; the watcher and sync are not covered by that cap, and there is no global semaphore beyond it. `[jobs] max_concurrent_bulk` (default 1) bounds concurrent bulk jobs.
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
| `timeout_seconds` | 10.0 | Per-call deadline; on timeout the query proceeds without the LLM augmentation. Accepted unclamped, so HyDE is additionally capped at ~30 s (S582) on every surface that runs it — REST `/search` and `/explain`, MCP `search`, `search_with_context` and `explain` — so a value above it cannot park the HyDE call. That cap is HyDE's own window, spent before the search budget starts: on REST `/search` and MCP `search` a `hyde=true` request can take ~60 s (≈30 s HyDE + 30 s search) before its `504`. |
| `[rag_fusion] num_queries` | 2 | Query variants generated (1–5); each variant adds a fan-out leg. |

Cost is one LLM round-trip per search (plus provider latency), gated by the rate limit. Provider is `anthropic` by default (also `ollama` / `openai` / `claude_cli`). See [`../UserManual/60_searching.md`](../UserManual/60_searching.md) for usage and provider setup.

### Ingest (`POST /ingest`, `POST /collections`, `POST /collections/{name}/reindex`)

Per document: parse → chunk → embed (batched) → upsert into LanceDB → incremental centroid update (B5) → incremental FTS optimize → update `.indexing-state.json`.

- **Streaming / incremental chunking (D4, shipped).** Chunks are sliced into fixed 512-chunk batches (`_INGEST_CHUNK_BATCH_SIZE`, an internal constant — not a config key) and each batch is embedded and written before the next is produced. Directory ingest no longer accumulates every vector in memory. A large single file or a large corpus completes on a memory-constrained host (e.g. a 1 GB container) because peak RAM is bounded by one batch (~2 MB) plus parse-time memory. Parse-time RAM is still owned by the parser and is not batched — bound pathological inputs with `[ingest] max_file_mb` (default 100 since the PyTorch release; 0 = unlimited; REST returns 413 for a single oversized file), `max_zip_entries` (10000) and `max_zip_uncompressed_mb` (512) for docx/pptx/xlsx/epub archives, and the parse timeout `parse_timeout_seconds` (300) plus `parse_timeout_seconds_per_mb` (10) per MiB of file size: a document that exceeds it fails alone with a sanitized error, its worker is killed and the pool replaced. Docling parsing runs on CPU on macOS (`DOCLING_DEVICE=cpu` is set in the parse worker unless you set it yourself), measured at about 2.1 s per page and 3.8 s per MiB of a scanned PDF, so a 100 MiB scan needs about 380 s of the allowance (300 s + 10 s × 100 MiB).
- **Docling parse memory is contained in a recycled child process.** PDF and image (OCR) conversion runs in the parse worker described above, not in the server. docling/RapidOCR/onnxruntime retain native memory per conversion that no in-process release returns to the OS, so the worker exits every `[ingest] max_tasks_per_child` files (default 25) and a fresh one takes over. That bounds the accumulation instead of letting it grow for the life of the server — measured over 40 mixed-size images: **1,304 MB retained before, 210 MB after**. `max_file_mb` cannot substitute for this: it is a disk-size guard, and a 200 KB icon costs hundreds of MB of RAM to OCR. Lower `max_tasks_per_child` to cap memory harder at the cost of paying the ~9 s worker restart more often; it must be `>= 1`.
- **Centroid (B5).** Maintained incrementally per batch; only `reindex`, sync reconciliation, and the eval runner trigger a full-collection re-read.
- **FTS (incremental).** `store.optimize_fts()` is O(delta); a single-file update into a 50,000-chunk collection completes in milliseconds. `delete_document` also calls `optimize_fts` after removal to prevent phantom hits.

Concrete chunk-count thresholds for "imperceptible" vs. "practical ceiling" are not benchmarked in this repo; treat sizing figures below as estimates. #Unverified

### Graph subsystem (`[graph] enabled = true`)

Enabling the graph subsystem adds cost surfaces at several points. This section is a capacity summary only — operations detail lives in [`60_graph_operations.md`](60_graph_operations.md).

| Surface | When it runs | Cost driver |
| --- | --- | --- |
| Entity / def-ref extraction | After each ingest (post-persist, non-blocking) | A `gliner` transformer (`knowledgator/gliner-relex-multi-v1.0`) run off the event loop via `asyncio.to_thread`, in sub-batches of 8 chunks; AST parse for code files (`tree-sitter`). Proportional to chunk count. Roughly 100 ms/chunk on CPU (dev-machine measurement, not a CI figure). |
| Community rebuild (Leiden) | `POST /graph/{collection}/rebuild-communities`, or `MaintenanceLoop` GC | Leiden clustering over the whole collection graph; serialised per collection. O(nodes + edges). |
| PPR (`graph_mode="ppr"`) | Query time | Personalised PageRank via `networkx.pagerank` in a worker thread; seeded from matched entities. Adds per-query latency on top of retrieval. |
| PageRank precompute | Debounced background pass (`MaintenanceLoop`) | Runs over code-symbol edges; `gc_rebuild_cpu_priority` (low/normal/high) bounds contention. |
| Synonym / LLM enrichment | Post-ingest callback (optional; `enrichment_auto`) | Embedding-based synonym detection; never blocks or fails an ingest (logs WARNING on error). |

Extraction and enrichment are fire-and-forget: a graph write failure never fails the ingest. Community rebuild is the heavy operation — schedule it, don't run it inline on large collections.

**Memory: budget for one permanently-resident transformer.** The prose extraction checkpoint is loaded once per server process, on the first prose ingest, and is never evicted — unbounded and uncached, a deliberate deviation from the bounded+LRU pattern the embedder and reranker use. Plan for roughly 3 GB resident on top of the embedder/reranker footprint, and about 1.2 GB of disk under `<data-dir>/models/graph/`. Steady-state growth after warm-up is guarded at 600 MiB over 1,000 chunks (`tests/test_graph_ner_real_artifact_lane.py`); those figures are **provisional** dev-machine measurements, and the guardrail binds the **CPU** configuration only — an accelerator via `[graph].device` is permitted but unmeasured. #Unverified

### Watcher

`watcher.py` debounces filesystem events and feeds them into the sync layer. There is no event-rate limiter — pathological churn on a watched directory queues ingest work indefinitely. If an external tool rewrites a watched file frequently (e.g. a Jupyter notebook on auto-save), expect proportional ingest load.

### Router cache (A6)

`MultiCollectionRouter._cached_metadata` is populated on first use within a single router instance. Current state:

- The FastAPI `/route` path builds a **fresh router per request**, so the old "stale centroids until restart" symptom does **not** occur on the server path — each request re-fetches metadata. A regression test pins this per-request lifecycle.
- For **non-FastAPI** consumers, `MultiCollectionRouter` exposes `invalidate()` to clear the cache and an `initial_metadata` ctor param for constructor-time injection (the eval runner uses the latter).
- A future migration to a shared, long-lived server-side router would need to call `invalidate()` after collection mutations; that is out of scope today. Capacity planning need not assume centroid drift.

## Memory, threads and the accelerator

All figures below were measured on an Apple M4 Pro (48 GB, 14 cores), PyTorch 2.13, transformers 5.8.1, on 2026-10-25 with other builds running on the machine, so compare numbers only within one table. They are planning aids, not guarantees. x86_64, Windows, NVIDIA CUDA and Intel Macs have **not** been measured (see [Known limitations](#known-limitations)).

### Startup memory peak

Models load as fp32. At startup the warm-up loads the default embedder and the reranker, and the model validation probe then checks **those same instances** after warm-up finishes (it used to load throwaway copies alongside, which raised the peak). Peak memory, sampled every ~150 ms during startup (`footprint` on macOS, RSS on CPU):

| Config | Before the shared probe | Now |
|---|---|---|
| `bge-large` + `bge-reranker-base`, `mps` | 5721 MB (4549 MB GPU) | 3307 MB (2377 MB GPU) |
| same pair, `cpu` | 3200 MB RSS | 2469 MB RSS |
| `bge-small` + `ms-marco-MiniLM`, `mps` | 1697 MB | 1673 MB |
| `bge-small` + `ms-marco-MiniLM`, `cpu` | 503 MB | 496 MB |

Validation finished about 1 s sooner with the large pair (10.7 s to 9.4 s). Small models barely change because their weights are small.

**Startup memory warning.** The lifespan estimates the memory the configured models need (fp32 weights × 1.5 + 1536 MB runtime baseline; the default embedder in full, the other embedders up to `embedder_cache_size`, plus the reranker) and compares it with the tighter of the cgroup memory limit (v2, then v1) and system RAM. When it is over, the log carries one WARNING `estimated model memory … exceeds the cgroup memory limit of … MB; the process may be OOM-killed under load. Use a smaller profile …`. It never refuses to start and does not model a CUDA device's own memory. Treat it as the prompt to pick a smaller profile, or to lower `embedder_cache_size`, before an OOM kill names no cause.

### Embedder cache: pinning and release

The pipeline's default embedder is **seeded into the `EmbedderCache` and pinned**: it is never evicted, it does not count against `[database].embedder_cache_size`, and there is only one copy of its weights (two `bge-large` copies would have been 2 × 1.3 GB). The other embedders (per-collection models) are an LRU of `embedder_cache_size` (default 3) slots. When a warm embedder is evicted, the cache runs `gc.collect()` and returns the accelerator's free blocks to the driver (`release_model_memory`, off the event loop), because `transformers` models live in reference cycles and the allocator would otherwise hold the memory: with `embedder_cache_size = 2` and 4 models on `mps` the GPU stayed at 2289 MB before the release and returns to about 1057 MB with it (process total 3069 MB to 1765 MB). A model still referenced by an in-flight request is freed after that request ends, not at eviction.

### LanceDB caches

Every `SearchStore` and `GraphStore` connection opens its database on a session with capped caches: **64 MiB** metadata cache and **512 MiB** index cache (LanceDB's defaults are 1 GiB and 6 GiB). With the defaults, each meta-table write added about 12 KiB of cached manifest/transaction data that was never read again, so an ingest-heavy server's memory climbed without bound: over 40 ingest-and-delete cycles of 300 chunks, the process footprint on CPU went from 631 MB to 961 MB before the cap and from 628 MB to 669 MB after it (levelling off by about cycle 10); on `mps` the same 40 cycles went 1865 MB to 1938 MB with the GPU flat at 1097 MB. After the cap, 1000 searches added about 32 MB in the first 200 and about 12 MB over the next 800 (RSS stays around 1.0 GB with `bge-small` + MiniLM). On-disk growth is separate: see [Disk growth and version retention](50_maintenance_and_jobs.md#disk-growth-and-version-retention) (`[maintenance].version_retention_hours`, default 24, only acts during a maintenance pass).

### Threads

Python threads stay bounded (the asyncio executor has 18; 21 Python threads were seen at 40 clients). The OS thread count is dominated by native pools and grows with concurrent load: 72 idle, 82 with 1 client, 226 with 5, 504 with 20 (flat for 30+ s), 415 with 40, then decaying to about 215 within 30 s of the load stopping. At the peak a `sample` showed 182 `lancedb-tokio-worker`, 13 `lance-cpu`, 29 dispatch-queue threads and about 200 unnamed ones (not confirmed to be the torch/OpenMP teams). The count is bounded, and `lancedb` exposes no knob for its runtime, so no setting is provided; the peak seen was about 500 on a 14-core host, and keeping client concurrency down is the only lever.

### The `mps` accelerator lock

On Apple GPUs (`device = "mps"`) one process-wide `RLock` (`torch_runtime.accelerator_lock`) wraps every model load and every batch forward pass, so only one model computes on the GPU at a time. It exists because concurrent first use of several `mps` models hangs or crashes PyTorch (2.13): without the lock, a cold-start stress run (fresh process, threads released together, 40 s hang limit) failed in 3 of 5 model sets, with 52 hangs and 33 `SIGSEGV` (rc -11) in 90 runs and only 5 clean; the test set that mirrors the repo's regression test (the same model twice) failed 27 of 30 runs. With the lock, 0 failures in 140 runs. Narrowing it to "loads plus each model's first forward pass" also had 0 failures in 280 runs, and it was **not adopted** because the gain does not survive the cost: it moved 5-8% off search p50 and 5-12% off p95 *during an ingest*, cost 12% more ingest time (36-40 s vs 33-34 s for a 400-chunk ingest under 4 searching clients) and nothing measurable on steady-state search (0-4%), and throughput is unchanged. The GPU is saturated by one client, so unlocked work only re-divides the same GPU time between searches and ingests.

What that means for capacity on `mps` (`bge-base` + `ms-marco-MiniLM-L-12`, 3 collections, `top_k = 5`; the two load columns are separate runs, so compare within a column):

| Clients | Search p50 / p95, load average 4 | Search p50 / p95, load average 12-13 |
|---|---|---|
| 1 | 480 / 539 ms (2.1 rps) | 732-751 / 788-863 ms (1.4 rps) |
| 4 | 1824 / 2009 ms (2.3 rps) | 2792-2848 / 3148-3234 ms (1.5 rps) |
| 8 | 3702 / 4096 ms (2.4 rps) | 5557-5746 / 6280-6388 ms (1.6 rps) |

Throughput does not grow with clients, so latency scales linearly with concurrency. A search spends about 655 of 709 ms in the rerank stage because it reranks `max(top_k_retrieve × 3, 20)` = 45 chunks (about 6 batches of the 4096-token budget). While an ingest runs, search p50 rises about 1.6× with 4 clients (3.1-4.4 s vs 1.9-2.8 s outside) and the ingest itself takes 2.4× as long as alone. Lowering `top_k_retrieve` cuts latency more than any lock change would.

On **CPU** `accelerator_lock` is a no-op and concurrent encodes run in parallel (4 concurrent encodes beat serialized ones: `bge-small` 469 vs 341 texts/s, `bge-base` 146 vs 114 texts/s). Model builds are serialized on every device by a second lock (`TRANSFORMERS_LOCK`), because transformers 4.x swaps torch globals during a build and two concurrent builds left meta tensors behind; the cost is that model loads do not overlap each other.

### Known limitations

- Only **Apple silicon (arm64)** has been measured for speed and memory; the Linux arm64 container run is CPU-only. **x86_64 Linux, Windows, NVIDIA CUDA and Intel Macs are unmeasured.**
- The CPU speed gate against the old ONNX runtime fails on arm64 for 3 of 10 models (`bge-small` 0.77, `bge-base` 0.89, `paraphrase-multilingual-MiniLM` 0.73 of the old throughput; the other 7 pass, e.g. `bge-reranker-base` 1.42, `jina-reranker` 1.61), measured under host load 17-19. The cause is that the old ONNX graphs stored float16 weights that ONNX Runtime runs as native fp16 matrix multiplies on arm64 NEON; torch runs fp32. Whether this holds on x86_64 is unmeasured. ADR 18 records the decision to keep PyTorch and to judge the speed rule per platform.
- Docling document parsing is CPU-only on macOS.

## Sizing heuristics

Treat these as starting points, not contracts. Verify against your own data with the eval harness in [`tests/eval/README.md`](../../tests/eval/README.md).

| Resource | Cheap workstation (laptop) | Server-class host |
| --- | --- | --- |
| CPU | 4 cores | 8+ cores (reranker scales with cores when on CPU) |
| RAM | 8 GB free | 16 GB+ free; embedder and reranker each hold fp32 model weights (see [Memory, threads and the accelerator](#memory-threads-and-the-accelerator)) |
| Disk | NVMe; headroom proportional to raw corpus #Unverified | NVMe; multi-× raw corpus for LanceDB column files + FTS + telemetry #Unverified |
| GPU | Apple Silicon (MPS) or none | CUDA-capable card for production reranker latency |
| Corpora | ≤ 5 collections, ≤ 50 000 chunks each #Unverified | ≤ 50 collections, ≤ 100 000 chunks each #Unverified |

Disk-usage multipliers (dense-vector overhead, FTS overhead, per-telemetry-entry size) are not benchmarked in this repo. What is verified: telemetry retention is capped by `[telemetry] retention_days`; the pruner runs every 24 h and never deletes today's file.

## Tuning knobs

All knobs live in `archon-search.toml`; `archon-search.toml.example` is the canonical list. Defaults below verified against `archon_search/config.py`.

**Retrieval (`[database]`, `[search]`)**

- `chunk_size` (512 tokens). Smaller chunks raise chunk count linearly; larger chunks hurt rerank precision.
- `top_k_retrieve` / `top_k_return` (15 / 5). Linear effect on reranker latency.
- `top_k_max` (100). Hard cap on a per-request `top_k`.
- `context_window_max` (10). Cap on MCP `search_with_context`'s `context_window`; each hit fetches up to `2 * context_window` neighbour chunks (no upper limit; raise with care).
- `embedder_cache_size` (3). Number of **non-default** embedder models held in the LRU cache; the default `embedding_model` is pinned and sits outside the slots. Raise only for genuine multi-model workloads. With `eager_load_embedders = true` keep it at least the number of distinct non-default models warm-up covers: the LRU evicts the rest right after they warm, so their first search pays the load again (startup logs `eager_load_embedders: N distinct models exceed embedder_cache_size`, where `N` counts every distinct model warm-up covers, the pinned default included, and the line appears only when the non-default models alone outnumber the slots). Eviction alone does not fail warm-up (S632); only a timed-out warm-up's `still cold:` list — a state probe — can name a model evicted from a full cache.
- `eager_load_embedders` (false). Model weights are loaded at startup on **every** deployment — the default `embedding_model` and the reranker cross-encoder always; setting this true additionally warms every distinct per-collection model — removing the first-query latency spike. Warm-up runs as a background task, so it does not delay the port binding; queries that arrive before it finishes wait only a short bounded time for the build, then run degraded (FTS-only while the embedder is cold, unreranked while the cross-encoder is). Because startup no longer waits for it, poll `GET /ready` for completion. It answers `503` with `checks.models: "pending"` until the task finishes or hits the terminal ceiling of 600 s, which sets the warm-up result to failed. A failed warm-up reports `checks.models: "fail"` at once; when it left the default embedder or a configured reranker unloaded `/ready` stays `503` while a background retry (5 s doubling to 300 s) reloads them and clears the gate on its own, and when only a per-collection model is cold `/ready` is `200` and search degrades as above. The log carries the same signal: `model warm-up complete (N embedding models + reranker)` at INFO only when every covered model loaded, otherwise `model warm-up failed: <models>` at WARNING (`model warm-up failed: timed out after 600.0s`, suffixed `; still cold: <models>`), and `model warm-up recovered: the default models are warm` when the retry succeeds. After a successful warm-up, `checks.models` stays `"pending"` until the background model validation also finishes (bounded as listed under [`validation_timeout_seconds`](../UserManual/30_configuration.md#database)). Within the 600 s budget the default embedder and then the reranker are warmed **first**, the preloaded embedders after them (S279), so a timeout during that preload leaves only embedders — not the reranker — to be built lazily on the first search. See [`20_monitoring_and_alerts.md`](20_monitoring_and_alerts.md) for the full `/ready` rule.
- `centroid_recompute_threshold` (10 000). Chunk-count trigger for a full centroid re-read on the paths that still recompute.
- `[search] max_fanout` (8) / `fanout_leg_trim` (40) / `fanout_timeout_seconds` (30.0). Bound multi-collection / RAG-Fusion fan-out cost.

**Routing (`[routing]`)**

- `routing_shortlist_size` (8). Maximum collections the router considers.
- `routing_confidence_threshold` (0.30). Below this, `MultiCollectionRouter.rank` returns `[]`; any "fallback to pinned collections" happens upstream in `routes_route.py`, not in the router.
- `routing_strategy` (`centroid`; also `hybrid`) and `routing_description_weight` (0.3, used by the hybrid strategy).

**Ingest (`[ingest]`)**

- `max_file_mb` (100; 0 = unlimited). Disk-size guard applied before parsing; REST returns 413 for a single oversized file, directory ingest skips and continues. The default was 0 before the PyTorch release. Raise it, or the parse timeout, if you ingest very large scanned PDFs.
- `max_tasks_per_child` (25). Files a docling parse worker handles before it is recycled. This is the memory knob for PDF/image OCR — lower it to cap the worker's growth harder, raise it to pay the ~9 s restart less often. Must be `>= 1`.

**Device & retention**

- `[database] device` / `[graph] device`. Torch device (`auto|cpu|mps|cuda`) for the embedder/reranker and the graph engine respectively; set at install time, not auto-tuned at runtime. `[graph].device` defaults to `cpu`.
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

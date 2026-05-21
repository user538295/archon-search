# Review: OperatorGuide/04_capacity_and_performance.md

## Summary

Doc is mostly correct on architectural framing and config defaults (chunk_size, top_k_retrieve/return, routing knobs, retention_days). The biggest concrete error is the claim that `[routing].max_parallel_collections` caps multi-collection search concurrency — that setting is parsed but **not consumed by any runtime code path** (single-collection search is what the codebase ships). The "IVF index" and "Cross-encoder rerank … 5–10× speedup on CUDA/CoreML" lines also overstate what the code does / what is measurable. Every performance number (millisecond range, 10–30% expansion overhead, "imperceptible at thousands / user-visible at 50 000 / ceiling at 100 000 chunks", reranker GPU multiplier, disk-size rules of thumb, telemetry "~entries/day × 8 KiB") is unverifiable from this repo — there is no benchmark harness for any of them; the only benchmark is `tests/benchmark_routing_latency.py` (routing only).

## Inaccuracies (numbered)

1. **"Each ingest re-reads the entire collection's vectors to recompute the centroid (`CON-4`)."** (Principles §2, repeated in "Ingest" §). The hot ingest paths do not re-read.
   - `SearchPipeline.ingest_directory` (`archon_search/pipeline.py:258-289`) computes the centroid from `all_vectors` accumulated **during the batch**, not from a full re-read.
   - `SearchPipeline.ingest_file` writes chunks but does not update the centroid at all when called directly.
   - `recompute_collection_meta` (`pipeline.py:368-401`) is the function that re-reads all vectors, but it is invoked only by `sync.py:707` (sync reconciliation), `eval/runner.py:484`, and (transitively) reindex. So "every ingest" is wrong — batch ingests recompute from in-memory vectors; single-file ingest does not touch the centroid; only sync/reindex does the full re-read.

2. **"Background tasks are unbounded by count, bounded by work. Ingest is spawned via `asyncio.create_task` (`routes_jobs.py:102`). There is no global semaphore."** Partially accurate but missing context: the task is also tracked in `request.app.state._background_tasks` (`routes_jobs.py:103-104`). The phrasing "unbounded by count" is technically true (no semaphore), but the doc omits the tracking set used for graceful shutdown.

3. **"LanceDB vector search `O(top_k_retrieve · log n)` with the IVF index (`store.py`)."** No IVF index is created in `store.py`. The only `create_index` call is `await table.create_index("text", config=FTS(), replace=True)` on the FTS column (`store.py:445-451`). Vector search uses LanceDB defaults — likely a flat scan unless LanceDB auto-builds one, which is not what the code does explicitly. The complexity claim is unsupported.

4. **"Cross-encoder rerank … CUDA/CoreML cuts this 5–10×."** Unverifiable. No benchmark exists for reranker GPU speedup; no source in `reranker.py` or `tests/` references this multiplier.

5. **"Context-window expansion … Adds 10–30% on top of base search latency."** Unverifiable. No benchmark measures this; `search_with_context` (`pipeline.py:306-338`) does one extra fetch per result for neighbour chunks, but the overhead percentage is made up.

6. **"Treat ~100 000 chunks per collection as the practical ceiling … By ~50 000 chunks they become user-visible."** Both numbers unverifiable — no benchmark or scaling test in the repo establishes either threshold.

7. **"`[routing].max_parallel_collections` (3). Concurrency cap on multi-collection search."** The setting is parsed in `config.py:180-184` and persisted by `cli/config_cmd.py:33`, but **no production code path reads it**. `grep` shows zero references outside config plumbing and tests of config plumbing. There is no multi-collection parallel search in the codebase to cap.

8. **"`[routing].routing_confidence_threshold` (0.30). Below this, queries fall back to the pinned set."** Loose. In `router.rank` (`router.py:154-155`), if `max_sim < threshold` and there are scored collections, `rank` returns `[]`. In `get_pre_context` (`router.py:206-210`), the consequence is the decomposer is **not** invoked and `_last_routable_names` is cleared — there is no automatic fallback to "the pinned set" inside the router. Pinned collections are independently passed through `routes_route.py`; the doc conflates two separate mechanisms.

9. **"FTS rebuild: every change set triggers `replace=True` on the FTS index. Cost scales with collection size, not delta size."** Mostly accurate (verified in `store.py:451` and `pipeline.py:189-190`, `pipeline.py:255`), but for `ingest_directory` it is rebuilt **once at the end of the batch** (`pipeline.py:253-255`), not once per file. The doc's "every change set" phrasing is fine for single-file ingest but does not reflect the batch optimization.

10. **"Disk usage rule of thumb: LanceDB tables run ~1.5× the raw text size for dense vectors plus ~0.5× for FTS. Telemetry adds `~entries/day × 8 KiB`."** All three multipliers (1.5×, 0.5×, 8 KiB/entry) are unverifiable — no source in repo, no test asserts these.

11. **"5× the raw corpus size" / "5–10× raw corpus" for NVMe sizing.** Same as above — unverifiable.

12. **"ONNX providers are picked once at install time (`platform/runtime.py`)."** Mis-attribution. `platform/runtime.py:39-43` only detects GPU type; the provider write happens in `install.py:135-165` (`configure_providers`). The doc's principle is broadly correct (install-time, not runtime) but the file reference is wrong.

13. **"`B1`" is referenced as "per-stage tracing does not yet exist" (Status section).** Verified against `Backlog/03_world_class_roadmap.md:61` — B1 is "Observability and stage-level latency", correctly described.

## Verified claims

- **One LanceDB writer / corruption on concurrent processes.** Aligns with `ADRs/01` (LanceDB single-writer assumption); no concurrent-writer guard in `store.py`.
- **One Uvicorn worker.** `server/app.py:152-156`: `uvicorn.run(app, host=config.host, port=config.port)` with no `workers=`. Default is 1.
- **Telemetry queue bounded at 1024; drops oldest, keeps newest with rate-limited warning.** Confirmed in `telemetry/writer.py:46` (`queue_size: int = 1024`), `:75-87` (full-queue drop sequence keeps the newest and rate-limits the warning).
- **`MultiCollectionRouter._cached_metadata` is populated once and never invalidated.** Confirmed `router.py:50, 69-70, 124`. No cache-bust path; only re-populated on a fresh instance.
- **Default config values.** Verified in `config.py`:
  - `chunk_size=512`, `top_k_retrieve=15`, `top_k_return=5`, `routing_shortlist_size=8`, `routing_confidence_threshold=0.30`, `max_parallel_collections=3` (defined but unused — see Inaccuracy 7), `telemetry.retention_days=30`.
- **Pruner runs every 24 h, never deletes today's file.** Confirmed `telemetry/pruner.py:44-45` (`if file_date == now: continue`) and `:70` (`await asyncio.sleep(86400)`).
- **Watcher debounces; no event-rate limiter.** Confirmed `watcher.py:40, 47, 72` — `threading.Timer(self._debounce_seconds, …)`. No global rate limiter.
- **Telemetry config note**: `export_enabled = true` is silently coerced to `false` with a warning (`config.py:209-215`) — matches CLAUDE.md invariant; the OperatorGuide doc doesn't restate it but the section it lives in is consistent.
- **CON-2 / CON-4 / B5 / C6 / A6 / F5 / F6 / B1 IDs all exist** in `Documentation/Backlog/03_world_class_roadmap.md`.
- **Roadmap items F5, F6 gated behind earlier phases.** Confirmed in `Backlog/03_world_class_roadmap.md:113-114`.

## Unverifiable / ambiguous

- All quantitative latency / throughput / size statements (see inaccuracies 4, 5, 6, 10, 11). No production-side benchmark covers them; only `tests/benchmark_routing_latency.py` exists and it targets routing only (`p50 ≤ 30 ms, p95 ≤ 150 ms over localhost`, line 7).
- "**Trivial up to hundreds of collections**" for centroid pre-rank — plausible (in-process cosine over float lists) but not measured in repo.
- "**Millisecond range up to ~10⁶ chunks per collection on commodity SSD**" for LanceDB vector search — unverifiable.
- "**Comparable to vector**" for FTS — unverifiable.
- "Pinned set fallback" wording (Inaccuracy 8) is ambiguous rather than outright wrong; could be rephrased to "below threshold, the decomposer is skipped and only pinned collections are queried" (which is closer to what `routes_route.py:113` does when constructing the final collection list).
- "`max_parallel_collections` only relevant once you exceed [shortlist size]" — also ambiguous because the knob is dead code (Inaccuracy 7), so "relevant" is never true today.

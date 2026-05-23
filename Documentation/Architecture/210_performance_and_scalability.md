**Purpose**: State the performance targets `archon-search` does and does not promise, the scalability envelope of the single-process design, and the knobs and tools available when latency or throughput need attention.
**Audience**: Operators sizing a deployment, maintainers tuning the router, and reviewers evaluating a performance-sensitive change.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2026-08-20

# Performance and Scalability

`archon-search` is a single-process local service. Performance is bounded by what one host can do; scalability beyond that host is **not built in**. This document pins down what the latency numbers in the repo actually mean, where the boundaries are, and which configuration values move the needle.

## Principles

1. **No production SLA.** The latency p50/p95 figures captured by the eval harness and the routing benchmark are **regression guards**, not service-level agreements. They exist so a refactor that doubles routing latency fails review — they are not a promise to callers.
2. **Single-process, single-host.** One `archon-search` process owns one LanceDB directory. There is no built-in horizontal scaling, no replication, no sharding across hosts.
3. **Local I/O is the scaling unit.** Throughput is driven by disk, CPU, and (when configured) GPU on the host running the daemon. Scaling up means picking a bigger host; scaling out is not a v1 affordance.
4. **Tune the router before you tune the model.** The cheapest latency wins come from `routing_shortlist_size` and `routing_confidence_threshold`. (`max_parallel_collections` is parsed from config but **not yet wired into the runtime** — see the knob table below.) Model swaps are expensive and have to clear the eval harness.
5. **Measure with the committed tools, not with new ones.** `tests/benchmark_routing_latency.py` and the eval harness are the agreed measurement surfaces. New benchmarks need to live alongside them, not replace them.

## Performance targets

| Surface                              | Number                                                                                                                          | Source                                              | Status                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- | ----------------------------------- |
| Eval-harness retrieval latency       | p50 / p95 captured per run                                                                                                      | `tests/eval/baselines/baseline.json`                | **Report-only** — latency ceilings are intentionally unset in `tests/eval/thresholds.toml` for v1 (see header note). |
| Routing latency over HTTP            | p50 ≤ 30 ms, p95 ≤ 150 ms over localhost, 100 iterations, `POST /route`                                                          | `tests/benchmark_routing_latency.py`                | **Regression guard** for the benchmark itself. Not a production SLA. |
| Routing latency in-process           | Reported alongside HTTP latency; difference is the HTTP transport overhead.                                                      | `tests/benchmark_routing_latency.py`                | Same — regression guard.            |

Both numbers are measured against **deterministic eval backends and localhost transport**. Do not quote them to callers and do not compare them to live-server measurements taken in a different environment.

If the benchmark reports `p95 > 150 ms`, the recorded mitigation is the **co-located embedder** pattern: the host application embeds the query locally and posts the vector to `/route`, removing the embedding round-trip inside the server. The decision must be recorded in `Documentation/ADRs/` (see the printed message in `benchmark_routing_latency.py`).

## Scalability boundaries

```mermaid
flowchart LR
  C[Client] -->|HTTP / MCP| P[archon-search process<br/>single host]
  P --> R[Router<br/>centroid pre-rank]
  R --> S[LanceDB<br/>~/.archon-search/&lt;db&gt;/]
  R --> E[Embedder<br/>fastembed / fastembed-gpu]
  R --> X[Reranker<br/>cross-encoder]
  P -.->|not built in| H[(Second host?)]
```

What is *not* in the box:

- **No horizontal scale-out.** Two `archon-search` processes cannot share a LanceDB directory safely. There is no replication, no sharding, no leader election.
- **No external orchestration.** The supervisor is `launchd` or `systemd --user` (see `Architecture/160_operational_readiness_monitoring_and_reliability.md`). Restart-on-crash is the OS's job; that is the entire HA story.
- **No backpressure for ingestion.** Ingestion runs on the same process as serving; large reindexes will compete with query latency. Use `archon_search status` (`processed_files`, `eta_seconds`) to monitor.

Scaling *up* a single deployment is supported by GPU detection at install time (`platform/runtime.py::SearchRuntime.detect_gpu_type` → `GpuType.CUDA` on Linux with `nvidia-smi`, `GpuType.METAL` on ARM macOS; `install.py` later maps `GpuType.METAL` to `CoreMLExecutionProvider`) and by the systemd unit's `CPUQuota=50%` / `Nice=10` defaults, which can be raised in `~/.config/systemd/user/archon-search.service` for dedicated hosts.

## Profiling and load tools

### `tests/benchmark_routing_latency.py` — `uv run pytest -m benchmark`

In-process `MultiCollectionRouter` vs `POST /route` over 100 iterations with 3 warmups against `http://127.0.0.1:8765`. Prints p50, p95, min, max, and the HTTP transport overhead (`http_p95 − inprocess_p95`).

- Auto-skips when `/health` does not answer — safe to leave in CI.
- Asserts that at least 90% of HTTP iterations returned 200; below that the benchmark fails rather than reporting bogus percentiles.
- When `p95 > 150 ms` it prints the co-located embedder recommendation. Recording the decision in an ADR is part of the mitigation, not optional.

### Eval harness latency capture

`tests/eval/` records p50 / p95 alongside quality metrics for every run. These are stored in `baselines/baseline.json` and currently *not* gated (latency ceilings unset). They are useful for trending across PRs even though they do not fail a build.

## Routing knobs

All values live in `~/.archon-search/archon-search.toml` (see `archon-search.toml.example`). The router is `archon_search/router.py::MultiCollectionRouter`.

| Knob                            | Effect                                                                                                                                                       | When to tune                                                                                                |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `routing_shortlist_size`        | How many collections survive centroid pre-ranking. Smaller → faster `/route` (fewer per-collection queries downstream); larger → higher recall when the router is unsure. | Raise when routing accuracy is low and the eval suite shows lost positives. Lower when `/route` latency dominates and the router is confident. |
| `routing_confidence_threshold`  | All-or-nothing gate on the top centroid similarity. If `max(similarity)` across scored collections is below the threshold, `MultiCollectionRouter.rank` returns an empty shortlist; otherwise the ranked list is truncated to `routing_shortlist_size` and individual sub-threshold collections are **not** pruned independently (see `archon_search/router.py::MultiCollectionRouter.rank`). | Raise to be stricter (router falls back to empty shortlist more often, forcing callers to handle "no confident route"). Lower to keep returning a shortlist even when the router is unsure. |
| `max_parallel_collections`      | **Reserved / not yet implemented.** Parsed and validated in `archon_search/config.py` (default `3`) and surfaced by `archon-search config`, but **no runtime code in `router.py`, `pipeline.py`, or `server/` consumes it** — there is no `asyncio.Semaphore` or equivalent fan-out gate today. Setting it has no effect on latency or concurrency in the current release. #Unverified | Not actionable until the knob is wired into the pipeline. #Unverified |

Before changing any of these in a release, re-run `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` to confirm `routing_accuracy` does not drop below the floor. Knob changes are eval-gated, not just benchmark-gated.

## Router lifecycle and cache invalidation

`POST /route` constructs a fresh `MultiCollectionRouter` per request via `_build_router()` in `routes_route.py`. No router is cached on `app.state`; stale centroids cannot accumulate across requests. A regression test (`test_build_router_called_once_per_request`) pins this invariant — any refactor that caches the router on `app.state` will break CI.

`MultiCollectionRouter.invalidate()` (added in A6) clears `_cached_metadata` and is idempotent. It is intended for future long-lived router consumers (e.g. a planned shared-router migration); the current per-request lifecycle makes it unnecessary in the FastAPI path. The eval harness uses `initial_metadata=` constructor injection instead of direct `_cached_metadata` assignment (CON-2 closed).

## See also

- `Architecture/100_system_architecture_overview.md` — the pipeline these knobs affect.
- `Architecture/140_error_handling_strategy.md` — how degraded performance is surfaced as `error_count` and `eta_seconds`.
- `Architecture/160_operational_readiness_monitoring_and_reliability.md` — observability surface used to monitor latency in production.
- `Architecture/510_release_and_environment_strategy.md` — how performance regressions interact with the release process.
- `tests/eval/README.md` — eval harness maintenance, including the latency-caveat section.

# Review: Architecture/210_performance_and_scalability.md

## Summary

The doc is largely accurate as a high-level narrative and the routing-knob defaults / latency-guard numbers match the source. There is one material inaccuracy: `max_parallel_collections` is described as the runtime concurrency cap for fan-out, but the value is parsed in `config.py` and never consumed anywhere in `router.py`, `pipeline.py`, or `server/`. It is currently dead config. A couple of smaller items (GPU detection naming, `routing_confidence_threshold` semantics) are imprecise but not outright wrong.

## Inaccuracies (numbered)

1. **`max_parallel_collections` is not wired in.** The doc (line 73, knob table) describes it as "Concurrency cap when the pipeline fans out to multiple collections after routing." Verified `grep -rn "max_parallel_collections|\.max_parallel"` across `archon_search/` and `tests/`:
   - Declared at `archon_search/config.py:44` (default `3`).
   - Parsed/validated at `archon_search/config.py:180-184`.
   - Echoed by `archon_search/cli/config_cmd.py:33`.
   - **Zero references in `archon_search/router.py`, `archon_search/pipeline.py`, or `archon_search/server/`.**
   No `asyncio.Semaphore` or equivalent gating exists in the pipeline. As written, the knob does nothing at runtime; the doc should either flag it as "reserved / not yet implemented" or the code should consume it.

2. **`routing_confidence_threshold` semantics are described loosely.** The doc (line 72) says "Minimum centroid score for a collection to be kept. Pairs with `routing_shortlist_size` — below threshold, candidates are pruned even if the shortlist has room." Per `router.py::MultiCollectionRouter.rank` (lines 152-158), the threshold is **not** a per-collection prune — it is an all-or-nothing gate on `max(similarity)`: if the top score is below the threshold, the entire ranked list is replaced with `[]`. Individual sub-threshold collections below the top one are not pruned independently; they remain in the shortlist (truncated to `shortlist_size`) as long as the top score clears the gate. This is materially different from "candidates are pruned".

3. **GPU detection naming.** The doc (line 49) says `SearchRuntime.detect_gpu_type` returns "CoreML on ARM macOS". Per `archon_search/platform/runtime.py:38-50`, it returns `GpuType.METAL` on ARM macOS; the mapping to `CoreMLExecutionProvider` happens later in `install.py` (`GpuType.METAL` → `"CoreMLExecutionProvider"`). Minor, but the function itself does not produce "CoreML".

## Verified claims

- **Routing latency targets `p50 ≤ 30 ms, p95 ≤ 150 ms` over 100 iterations + 3 warmups against `http://127.0.0.1:8765`** — matches `tests/benchmark_routing_latency.py` lines 7, 20, 22-23.
- **Auto-skips when `/health` is unreachable** — `_is_server_running()` + `pytest.skip` at lines 31-36, 113-117.
- **Asserts ≥ 90 % HTTP successes** — line 143: `assert len(http_latencies) >= int(_ITERATIONS * 0.9)`.
- **Prints co-located embedder recommendation when `p95 > 150 ms`** — lines 132-139.
- **Benchmark prints p50, p95, min, max, and `http_p95 − inprocess_p95` overhead** — `_print_stats` (lines 39-48) and lines 127-130.
- **Marker is `@pytest.mark.benchmark`** — line 102.
- **Router defaults: `routing_shortlist_size = 8`, `routing_confidence_threshold = 0.30`, `max_parallel_collections = 3`** — `config.py:42-44` and `archon-search.toml.example:38-42`.
- **systemd unit defaults `Nice=10`, `CPUQuota=50%`** — `archon_search/platform/linux.py:29-30`.
- **GPU detection uses `nvidia-smi` on Linux** — `platform/runtime.py:41`.
- **Latency ceilings intentionally unset in `tests/eval/thresholds.toml`** — file header explicitly says "Latency ceilings are intentionally omitted — latency is report-only until production-comparable backends are wired in." Only `[quality_floors]` and `[policy]` sections exist.
- **Baseline file present at `tests/eval/baselines/baseline.json`** — verified via `ls`.
- **The eval gate command `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`** matches the test layout (`tests/eval/test_eval_suite.py` exists; `thresholds.toml` exists).

## Unverifiable / ambiguous

- **"Two `archon-search` processes cannot share a LanceDB directory safely"** (line 45) — plausible given LanceDB's single-writer model, but not asserted or guarded anywhere in the code; this is policy, not enforcement. Not contradicted by source.
- **"Restart-on-crash is the OS's job; that is the entire HA story."** — consistent with `linux.py` (`Restart=always`); macOS launchd plist not re-checked here.
- **Co-located embedder pattern as a mitigation** — the benchmark prints the recommendation, but there is no code path in `routes_route.py` reviewed here that accepts a pre-computed vector. Doc presents it as a documented mitigation, not a feature — defensible, but the implication that `/route` accepts a vector should be verified separately.
- **`Architecture/160_…` cross-reference for supervisor details** — not verified in this pass.
- **Claim that knob changes "are eval-gated, not just benchmark-gated"** — process/policy statement, not verifiable from source.

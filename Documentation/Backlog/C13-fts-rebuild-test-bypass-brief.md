# Feature Brief: C13 — Bypass FTS-index rebuild in tests that don't query FTS

## Problem
After C12, full-suite wall time is **median 127s** (range 106–140s across 5 runs). The remaining floor is a cluster of ~30 ingest-directory tests that each take ~35s under `-n auto` parallel execution despite being ~13s in serial. Investigation shows the dominant cost is `SearchPipeline.ingest_directory()` calling `store.rebuild_fts_index()` (which invokes `table.create_index("text", config=FTS(...))` in LanceDB) — a Tantivy index build that is disk-I/O intensive and contends across all 14 xdist workers building separate indexes simultaneously.

Concrete numbers (verified):
- 3 sample ingest tests in serial: 38.85s → **~13s each**
- Same 3 tests in `-n auto` parallel: **~35s each**
- 2.7× slowdown is disk-I/O contention from concurrent FTS index builds, not CPU contention (already addressed in C12 via thread caps)

## Goal
`uv run pytest --no-cov` median wall time **≤90s** on a ≥4-core machine (the original C12 target, now reachable). No production code behavior change. Default-run coverage stays ≥85%.

## Users & Context
Developers iterating locally. The 5-min hard target is already met (worst run 140s), but the 30s+ gap to the 90s target makes the inner loop feel slow for trivial edits. CI is unaffected (uses `-n0` where contention does not exist).

## Core Flow
1. Add a `rebuild_fts: bool = True` parameter to `SearchPipeline.ingest_directory()` — mirroring the existing parameter on `ingest_file` (`pipeline.py:285`).
2. When `rebuild_fts=False`, skip the `optimize_fts` / `rebuild_fts_index` block at `pipeline.py:513-528`. Behavior matches `ingest_file(rebuild_fts=False)` today.
3. Tests that ingest but never call `pipeline.search()` / `store.hybrid_search()` pass `rebuild_fts=False`.
4. Production callers (CLI `ingest`, `sync`, HTTP `/ingest`, MCP `ingest_directory`) keep the default `True` — zero behavior change.

## In Scope
- Add `rebuild_fts: bool = True` parameter to `archon_search/pipeline.py:ingest_directory`
- Pass it through to skip the FTS optimize/rebuild block when `False`
- Audit the 6 test files that call `ingest_directory`:
  - `tests/test_sync_e2e.py` (16 tests, none query FTS → all eligible)
  - `tests/test_pipeline_code_enricher.py` (none query FTS → all eligible)
  - `tests/pipeline/test_pipeline_ingest.py` (some tests don't query FTS — per-test audit needed)
  - `tests/pipeline/test_pipeline_search.py` (queries FTS → keep `rebuild_fts=True`)
  - `tests/test_pipeline_acl.py` (queries FTS → keep `rebuild_fts=True`)
  - `tests/test_pipeline_ingest_directory_fts.py` (FTS-specific → keep `rebuild_fts=True`)
- Update tests that don't query FTS to pass `rebuild_fts=False`
- Run the C12 5-run stability gate
- Record new wall-time median

## Out of Scope
- Any change to `ingest_file` (already has the parameter)
- Production callers (HTTP/MCP/CLI/sync) — they all need FTS for search to work
- LanceDB version bump / FTS engine swap
- Test ordering / `pytest-randomly`
- `asyncio_default_fixture_loop_scope` (see Future Iterations)
- Moving tests to RAM-disk / tmpfs (see Future Iterations)

## Key Decisions
- **Parameter on `ingest_directory` over a test-only monkeypatch**: the parameter pattern already exists on `ingest_file`. Symmetry beats invasion. Monkeypatching `store.rebuild_fts_index` would risk hiding real bugs in the FTS creation path.
- **Default `True` (current behavior)**: zero risk to production callers. Only tests explicitly opt out.
- **Audit at file-level granularity is safe but not maximal**: `test_pipeline_ingest.py` has both FTS-querying and non-FTS-querying tests; per-test classification yields more wins. The brief recommends per-test audit but acknowledges file-level as an acceptable fallback.
- **Don't touch `ingest_file` callers**: they already have the opt-out. Test usage is opportunistic.

## Edge Cases & Constraints
- **`test_pipeline_ingest.py` mixed-purpose tests**: some tests assert on `IngestResult.status`, others on document presence in the store, others on search behavior. Only the third group needs FTS. Audit via `grep -B2 -A 40 'def test_' | grep -E 'search\\(|hybrid|keyword'` per test, not per file.
- **`rebuild_fts=False` + later `pipeline.search` in the same test**: would raise `FTSIndexNotFoundError` (already documented at `store.py:67-70`). This is the correct failure mode — tests that need search must keep `rebuild_fts=True`. The 5-run gate will catch any misclassification.
- **Centroid recomputation is independent**: `ingest_directory` at `pipeline.py:530-` computes the centroid from `all_vectors` regardless of FTS rebuild. No coupling.
- **`optimize_fts` already wraps `rebuild_fts_index` in a try/except** (line 516-525): the new `if not rebuild_fts: skip` branch must precede this entire block.
- **`sync_e2e` "second sync" tests**: they ingest twice, modify, ingest again. Skipping FTS rebuild on all calls is safe because the sync's correctness is verified via `IndexingStateStore.read()` and result dicts, not search output.

## Open Questions
- Should the parameter be `rebuild_fts: bool = True` (matches `ingest_file`) or `_test_skip_fts: bool = False` (signals test-only intent)? Recommend the former — it is symmetric and not test-coupled.
- Worth investigating: does LanceDB's FTS `create_index` honor any concurrency-limiting env var? If yes, that may be a simpler global fix. Quick check via `lancedb` source / docs would resolve before deciding.

## Rollback
1. Revert the `rebuild_fts: bool = True` parameter on `ingest_directory` (`archon_search/pipeline.py`).
2. Revert each test that now passes `rebuild_fts=False`.
3. Re-run the 5-run gate.
The change is additive (default preserves behavior), so partial rollback is also safe.

## Future Iterations
- **`asyncio_default_fixture_loop_scope = "module"`** — deferred from C10 and C12. Estimated 30-60s additional saving but blocked on resolving dangling-coroutine warnings (visible in current runs: `test_parser_image_empty_ocr_returns_empty_string`, `test_watcher.py::TestDebounceHandler`). Worth a separate brief.
- **RAM-disk for `tmp_path_factory`** — set `pytest_tmpdir` to a tmpfs/RAM-disk mount. On macOS: `hdiutil attach -nomount ram://`. Eliminates remaining disk-I/O contention across all tests, not just FTS. Estimated 20-30s saving but adds platform-specific setup; would need a per-OS fixture.
- **`--dist=worksteal`** — already listed in C12 brief's future section. Would re-balance the heaviest-worker load dynamically. Not yet benchmarked.
- **Shared session-scoped collection** — most slow ingest tests create a unique collection via `col_name`. A shared, FTS-indexed fixture collection would amortize the index build. Requires careful isolation (no cross-test write conflicts) and is a bigger refactor than C13.

## Expected Impact (estimate, must be verified)
- ~30 tests currently at ~35s wall time → expected ~5–10s without FTS rebuild
- Worst-case worker time drops by an estimated 25–35s
- Wall-time median estimate: **127s → 90–100s** (29–37% additional reduction)
- The new floor would likely be a different I/O-heavy operation (e.g. embedder model load on real-model tests) or the `~17s` test_ingest_centroid_replaced_on_reingest

## Recommendation
This is the cleanest next step: a 1-parameter API change mirroring an existing pattern, plus a mechanical audit of 6 test files. The parameter default preserves production behavior with zero risk. The 5-run C12 gate is the correct verification; no new infrastructure required. Ship after audit + 5-run pass + coverage check.

If after C13 the gap to 90s is still material, the next brief should target `asyncio_default_fixture_loop_scope = "module"` — but only after the dangling-coroutine warnings are fixed.

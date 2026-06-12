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
- Audit the 6 test files that call `ingest_directory` directly:
  - `tests/server/test_mcp_error_responses.py` — NOT eligible: mocks `pipeline.ingest_directory = AsyncMock(...)` entirely. No real FTS rebuild happens.
  - `tests/test_pipeline_code_enricher.py` — 2 tests call `ingest_directory` (lines 218, 248); both eligible (neither queries FTS). The remaining 5 tests in the file call `ingest_file` and are out of scope.
  - `tests/pipeline/test_pipeline_ingest.py` (some tests don't query FTS — per-test audit needed)
  - `tests/pipeline/test_pipeline_search.py` (queries FTS → keep `rebuild_fts=True`)
  - `tests/test_pipeline_acl.py` (queries FTS → keep `rebuild_fts=True`)
  - `tests/test_pipeline_ingest_directory_fts.py` (FTS-specific → keep `rebuild_fts=True`)
- Update tests that don't query FTS to pass `rebuild_fts=False`
- For every test switched to `rebuild_fts=False`, verify via grep that the test body contains no call to `pipeline.search`, `store.hybrid_search`, or `store.full_text_search` (required because FTS misclassification is silent — see Edge Cases)
- Add a unit test verifying `optimize_fts` and `rebuild_fts_index` are NOT called when `rebuild_fts=False` is passed to `ingest_directory` (mirroring the pattern in `test_pipeline_ingest_fts.py`)
- No external API documentation update needed — `rebuild_fts` is not exposed via MCP, HTTP, or CLI. If the Python API surface is separately documented, update it there.
- Run the C12 5-run stability gate
- Run coverage before and after; confirm lines 513-528 in `pipeline.py` remain covered by the retained FTS tests
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
- **`rebuild_fts=False` + later `pipeline.search` in the same test**: `hybrid_search` at `store.py:1505-1506` silently degrades to vector-only results with a warning log — it does NOT raise `FTSIndexNotFoundError`. A misclassified test will pass, but with weakened assertion coverage (FTS recall is untested). The 5-run gate catches test failures; it does NOT catch FTS misclassification. The explicit audit grep step (see In Scope) is the required mitigation.
- **Centroid recomputation is independent**: `ingest_directory` at `pipeline.py:530-` computes the centroid from `all_vectors` regardless of FTS rebuild. No coupling.
- **`optimize_fts` already wraps `rebuild_fts_index` in a try/except** (line 516-525): the new `if not rebuild_fts: skip` branch must precede this entire block.

## Open Questions

All resolved.

- **Parameter name**: Use `rebuild_fts: bool = True` — symmetric with `ingest_file`, has legitimate production semantics, and matches the internal `rebuild_fts=False` already passed at `pipeline.py:497`.
- **LanceDB FTS concurrency env var**: No such knob exists in `lancedb==0.30.2`. The contention is inter-process (14 xdist workers building separate Tantivy indexes on the same disk); `RAYON_NUM_THREADS` would only cap intra-process thread parallelism and would not reduce the number of concurrent index builds. The `rebuild_fts` parameter approach stands.

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
- The eligible pool is ~25–40 tests (pending the per-test audit): ~25–38 from the qualifying subset of `test_pipeline_ingest.py` (95 `ingest_directory` calls in the file, only 6 lines reference FTS search) plus 2 from `test_pipeline_code_enricher.py`
- Eligible tests currently at ~35s wall time → expected ~5–10s without FTS rebuild
- Worst-case worker time drops by an estimated 25–35s
- Wall-time median estimate: **127s → 90–105s** (24–37% additional reduction); the 90s target may be reachable with C13 alone pending the per-test audit results
- Actual savings depend on the per-test audit results; run the 5-run gate to confirm.
- The new floor would likely be a different I/O-heavy operation (e.g. embedder model load on real-model tests) or the `~17s` test_ingest_centroid_replaced_on_reingest

## Recommendation
This is the cleanest next step: a 1-parameter API change mirroring an existing pattern, plus a mechanical audit of 6 test files. The parameter default preserves production behavior with zero risk. The 5-run C12 gate is the correct verification; no new infrastructure required. Ship after audit + 5-run pass + coverage check.

If after C13 the gap to 90s is still material, the next brief should target `asyncio_default_fixture_loop_scope = "module"` — but only after the dangling-coroutine warnings are fixed.

# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-20] (×54) pytest OOM/parallelism**: rules now live in `tests/CLAUDE.md`. Residual: a DIFFERENT timing test failing per full run = contention, not regression. Raise a failure-path-only budget 5s→30s — free on green, kills the flake.
- **[2026-08-20] (×4) OOM/memory RCA**: native leaks need a guarded repro (RSS cap+alarm); log-census rebuilds the timeline. Global `app.state.embedder` duplicates the cache's default-model copy — count both. Job-marker guards miss the lifespan sync.
- **[2026-08-20] (×1) mutation checks lie**: a passing revert-test isn't proof. `JobStore.__init__`'s `_write_atomic()` re-runs a good `_evict_old()` and masked a reordered `_load()`. No-op the repair path to isolate; confirm the INTENDED oracle failed.
- **[2026-08-20] (×1) sticky guard state**: durable suppression gets its OWN data-dir file — `.exists()` is parse-free, so corruption can't latch it. Clear it UNCONDITIONALLY: gating the clear on what gates the guard strands the marker.

## What Has Worked
- **[2026-08-20] (×64) bug briefs**: failing repro FIRST; probe config permutations; diff green tests against the DOC contract; fix at the guard layer, not a call-site proxy. Briefs undercount blast radius (sync.py mirrors pipeline's FTS/meta passes).
- **[2026-08-20] (×55) brief triage**: a re-filed brief whose name is already in `Completed/` = regression or 2nd failure mode; close it as `<name>-reopened.md`. A doc-only fix is never behavioral — read the code after closing a DOC report.
- **[2026-08-20] (×28) new field/pin**: dataclass + `_apply_toml` + coerce + snapshot tests; regenerate the OpenAPI snapshot on 3.12. `path_home_allowlist.txt` pins (file,lineno,sha) with `_EXPECTED_CONFIG_LINE_NO` — any edit ABOVE it breaks BOTH.
- **[2026-08-14] (×17) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` constants to 0.1 rather than shrinking an outer `wait_for` — the outer budget wins.
- **[2026-08-20] (×21) lifespan tasks**: `app.state.<x>` set in a conditional branch needs a `= None` default. TestClient portal-loop tasks: poll `.done()`, never await. Pre-seed `tmp_path/jobs.json` before `make_real_app` to drive boot-recovery paths.
- **[2026-08-20] (×21) test vacuity**: split `or` asserts; overrides start at the OPPOSITE value. A stub fabricating a 3rd-party contract shape CERTIFIES the bug — pin fixtures to the vendored lib. An assertion a broad `except` swallows can never fail.
- **[2026-08-11] (×17) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, plus `_migrate_<field>()` and a catalog entry. Then grep `tests/` for `def fake_<method>` and widen those.
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe OK but non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-20] (×12) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-08-18] (×22) install/wizard**: doc advertising an option the code never prints → fix the CODE. A prompt pinned by a locator AND a phrase admits ONE wording; on a reflip rewrite the GUARD DOCSTRINGS — a stale one argues for flip #4.
- **[2026-08-19] (×3) parse/3rd-party + wiring**: verify lib params per INPUT FORMAT (docling `OcrOptions.scale`: bitmap for IMAGE, 72-DPI multiplier for PDF). Contain leaks: `ProcessPoolExecutor(max_workers=1, spawn, max_tasks_per_child)`.

## Plan-Making & Agent Process
- **[2026-08-20] (×156) verify claims/state**: grep-verify every cite; prove repros vs unmodified HEAD (`git worktree add --detach`/`git stash push`). Diffing WIP vs HEAD reads your own uncommitted work as "pre-existing drift" — 4 such errors this run.
- **[2026-08-20] (×41) subagents**: demand a scratchpad drop as the PRIMARY channel — final text is DISCARDED. A grandchild's completion routes to the GRANDPARENT, not its spawner — relay it, or the loop owner never sees its own reviewers' findings.
- **[2026-08-20] (×32) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-04] (×9) smoke/subprocess + TypeSpec**: `-o addopts=` not `-p no:xdist`; session fixtures use `tmp_path_factory`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`; seed real text. TypeSpec: `field?: T | null`; `namespace`/`model`/`op` reserved.

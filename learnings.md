# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-15] (×51) pytest OOM/parallelism**: rules now live in `tests/CLAUDE.md`. Residual: a DIFFERENT timing test failing per full run = contention, not regression. Raise a failure-path-only budget 5s→30s — free on green, kills the flake.
- **[2026-08-19] (×3) OOM/memory RCA**: native leaks need a guarded repro (RSS cap+alarm); log-census rebuilds the timeline. Global `app.state.embedder` duplicates the cache's default-model copy — count both.

## What Has Worked
- **[2026-08-19] (×61) bug briefs**: failing repro FIRST; probe config permutations; diff green tests against the DOC contract; fix at the guard layer, not a call-site proxy. Briefs undercount blast radius (sync.py mirrors pipeline's FTS/meta passes).
- **[2026-08-18] (×54) brief triage**: a re-filed brief whose name is already in `Completed/` = regression or 2nd failure mode; close it as `<name>-reopened.md`. A doc-only fix is never behavioral — read the code after closing a DOC report.
- **[2026-08-19] (×26) new field/pin**: dataclass + `_apply_toml` + coerce + snapshot tests; regenerate the OpenAPI snapshot on 3.12. `path_home_allowlist.txt` pins (file,lineno,sha) with `_EXPECTED_CONFIG_LINE_NO` — any edit ABOVE that line breaks BOTH.
- **[2026-08-14] (×17) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` constants to 0.1 rather than shrinking an outer `wait_for` — the outer budget wins.
- **[2026-08-19] (×18) lifespan tasks**: an `app.state.<x>` set inside an `if config.<flag>:` branch needs a `= None` default. A `create_task`'d warm-up runs on TestClient's portal loop — poll `.done()`, never await it from the test loop.
- **[2026-08-18] (×19) test vacuity**: split `assert a in x or b in x`; guard loops with `assert results`; override tests start at the OPPOSITE value. A `return_value` mock cannot reproduce a malformed-input bug — make `side_effect` faithful.
- **[2026-08-11] (×17) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, plus `_migrate_<field>()` and a catalog entry. Then grep `tests/` for `def fake_<method>` and widen those.
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe succeeds non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-09] (×11) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-08-18] (×21) install/wizard**: doc advertising an option the code never prints → fix the CODE. A prompt pinned by a locator AND a phrase admits ONE wording; on a reflip rewrite the GUARD DOCSTRINGS — a stale one argues for flip #4.
- **[2026-08-19] (×1) 3rd-party param semantics**: verify a lib param per INPUT FORMAT before sharing one options object — docling `OcrOptions.scale` scales the native bitmap for IMAGE but is a 72-DPI render multiplier for PDF (1.0 = 72 DPI, not native).
- **[2026-08-19] (×1) native-memory containment**: `ProcessPoolExecutor(max_workers=1, spawn, max_tasks_per_child)`; return `(ok,payload)`, exceptions won't unpickle; a SIGKILLed parent orphans the worker unless it polls `is_alive()`.
- **[2026-08-19] (×1) assert the WIRING**: an unasserted ctor kwarg silently disconnects its feature, suite still green — `initializer=` (orphan watchdog), `max_tasks_per_child=` (operator knob). Assert the kwarg reaches the ctor, from a NON-default value.

## Plan-Making & Agent Process
- **[2026-08-19] (×151) verify claims/state**: grep-verify every label & file:line cite; `git log -- <file>` for later reverts; prove repros vs unmodified HEAD (`git worktree add --detach` / `git stash push -- <file>`); read CURRENT source before acting.
- **[2026-08-18] (×39) subagents**: demand a scratchpad file drop as the PRIMARY channel — final text is DISCARDED. A whole review batch can hang and NEVER return: bound the wait, then self-review. Reviewers still ACT — re-`git status` each round.
- **[2026-08-19] (×29) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-04] (×9) smoke/subprocess + TypeSpec**: `-o addopts=` not `-p no:xdist`; session fixtures use `tmp_path_factory`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`; seed real text. TypeSpec: `field?: T | null`; `namespace`/`model`/`op` reserved.

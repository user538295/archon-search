# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-20] (×58) pytest OOM/memory RCA**: rules: `tests/CLAUDE.md`. A DIFFERENT timing test failing per run = contention; raise a failure-path-only budget 5s→30s. Native leaks need a guarded repro (RSS cap+alarm); count BOTH embedder copies.
- **[2026-08-27] (×1) CI regex guards**: scan width = its kwarg list — positional `.where(f"` alone shipped `where=f"…"`. 7 patterns: +`filter=`/`updates_sql=`/`.add_columns(` (RAW SQL, unlike `updates=`) +`rf`/`F`. Guard only files with callsites.
## What Has Worked
- **[2026-08-27] (×1) TOCTOU lock fixes**: `asyncio.Lock` is non-reentrant — wrapping a method's body in its own lock deadlocks any caller that already holds it. Add `_locked_by_caller` (matches `ingest_chunks`), don't just wrap.
- **[2026-08-22] (×122) briefs**: failing repro FIRST; fix at the guard layer. Verify a brief's CANDIDATE FIX, not its symptom. Briefs undercount blast radius (sync.py mirrors pipeline). Re-filed name in `Completed/` → `<name>-reopened.md`.
- **[2026-08-21] (×2) brief refinement**: grep every "reuse X from brief Y" claim — 030 shipped a spaCy-WHEEL downloader, no checksum. Deleting a feature strands wire fields added only for it (`provider_notes`); a sibling method may keep its protocol.
- **[2026-08-20] (×28) new field/pin**: dataclass + `_apply_toml` + coerce + snapshot tests; regenerate the OpenAPI snapshot on 3.12. `path_home_allowlist.txt` pins (file,lineno,sha) with `_EXPECTED_CONFIG_LINE_NO` — any edit ABOVE it breaks BOTH.
- **[2026-08-14] (×17) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` constants to 0.1 rather than shrinking an outer `wait_for` — the outer budget wins.
- **[2026-08-20] (×21) lifespan tasks**: `app.state.<x>` set in a conditional branch needs a `= None` default. TestClient portal-loop tasks: poll `.done()`, never await. Pre-seed `tmp_path/jobs.json` before `make_real_app` to drive boot-recovery paths.
- **[2026-08-20] (×22) test vacuity**: split `or` asserts; overrides start at the OPPOSITE value. A stub fabricating a 3rd-party shape CERTIFIES the bug. An assert a broad `except` swallows never fails. A passing revert-test isn't proof.
- **[2026-08-11] (×17) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, plus `_migrate_<field>()` and a catalog entry. Then grep `tests/` for `def fake_<method>` and widen those.
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe OK but non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-20] (×12) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-08-18] (×22) install/wizard**: doc advertising an option the code never prints → fix the CODE. A prompt pinned by a locator AND a phrase admits ONE wording; on a reflip rewrite the GUARD DOCSTRINGS — a stale one argues for flip #4.
- **[2026-08-21] (×4) 3rd-party wiring**: verify lib params per INPUT FORMAT (docling `OcrOptions.scale`) AND per CHECKPOINT — GLiNER `relations` no-ops off-RelEx; assert it at load. Leaks: `ProcessPoolExecutor(1, spawn, max_tasks_per_child)`.

## Plan-Making & Agent Process
- **[2026-08-27] (×161) verify claims/state**: grep-verify every cite; prove repros vs unmodified HEAD (`git worktree add --detach`). Diffing WIP vs HEAD reads your own work as drift. NEVER trust a fix agent's "done" — 2 of 3 false once.
- **[2026-08-23] (×44) subagents**: PRIMARY channel is a scratchpad drop — final text is DISCARDED and a mid-run death (API error) leaves NOTHING. `mkdir -p` that dir BEFORE spawning. A grandchild's completion routes to the GRANDPARENT — relay it.
- **[2026-08-27] (×35) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-24] (×3) plan/task authoring**: a fix in the body but not the Decisions table / mermaid / doc-checklist is the top defect — sweep all 8 surfaces + the `.tsp`. `#team` gate → agent-runnable findings; never flip the box.
- **[2026-08-04] (×9) smoke/subprocess + TypeSpec**: `-o addopts=` not `-p no:xdist`; session fixtures use `tmp_path_factory`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`; seed real text. TypeSpec: `field?: T | null`; `namespace`/`model`/`op` reserved.

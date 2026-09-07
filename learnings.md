# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-20] (×58) pytest OOM/memory RCA**: rules: `tests/CLAUDE.md`. A DIFFERENT timing test failing per run = contention; raise a failure-path-only budget 5s→30s. Native leaks need a guarded repro (RSS cap+alarm); count BOTH embedder copies.
- **[2026-09-06] (×1) release-path CI**: GH caches are ref-scoped — a tag run reads its own tag or `main` only, and nothing runs on `main` here, so a release cache step never hits, only evicts the PR gate's. Autouse function fixtures beat module env.
## What Has Worked
- **[2026-08-27] (×1) TOCTOU lock fixes**: `asyncio.Lock` is non-reentrant — wrapping a method's body in its own lock deadlocks any caller that already holds it. Add `_locked_by_caller` (matches `ingest_chunks`), don't just wrap.
- **[2026-08-22] (×122) briefs**: failing repro FIRST; fix at the guard layer. Verify a brief's CANDIDATE FIX, not its symptom. Briefs undercount blast radius (sync.py mirrors pipeline). Re-filed name in `Completed/` → `<name>-reopened.md`.
- **[2026-09-05] (×5) brief refinement**: grep every "reuse X from Y" claim. Deleting a feature strands wire fields added only for it (`provider_notes`); a sibling method survives; narrowing a protocol ripples to its probe + any "N sites" test.
- **[2026-09-05] (×30) new field/pin**: dataclass + `_apply_toml` + coerce + snapshot tests; regenerate the OpenAPI snapshot on 3.12. `path_home_allowlist.txt` pins (file,lineno,sha) with `_EXPECTED_CONFIG_LINE_NO` — any edit ABOVE it breaks BOTH.
- **[2026-08-14] (×17) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` constants to 0.1 rather than shrinking an outer `wait_for` — the outer budget wins.
- **[2026-09-05] (×22) lifespan tasks**: `app.state.<x>` in a conditional branch needs `= None`. Poll portal-loop `.done()`, never await; SEEDING `app.state.model_validation` races the probe overwriting it. Pre-seed `jobs.json` pre-`make_real_app`.
- **[2026-09-07] (×34) test vacuity**: split `or` asserts; overrides start OPPOSITE. Every ABSENCE assert needs a PRESENCE anchor + the deleted wording. A scenario saying "persisted" = read back via GraphStore, not `result.*`. A tester e2e adds a LAYER.
- **[2026-08-11] (×18) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, plus `_migrate_<field>()` and a catalog entry. Then grep `tests/` for `def fake_<method>` and widen those.
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe OK but non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-20] (×12) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-09-06] (×31) install/wizard**: unprinted doc option → fix the CODE. A pinned prompt admits ONE wording. `is_multilingual=False` re-resolves `prof`. CliRunner echoes input() PROMPTS, not answers. Wizard WARNINGs reach stderr via `lastResort`.
- **[2026-09-07] (×8) 3rd-party wiring**: verify params per INPUT FORMAT AND per CHECKPOINT, on the REAL model: GLiNER `relations` no-op off-RelEx; bare `"other"` ate all spans; `label<>desc`→0 relations; but descriptive `related_to` LIFTS typed yield.

## Plan-Making & Agent Process
- **[2026-09-07] (×179) verify claims/state**: grep-verify every cite AND a reviewer's cited PRECEDENT. Grep the tasks file for a prior spike's Notes first. Repros vs unmodified HEAD (`git worktree add --detach`) — diffing WIP reads own work as drift.
- **[2026-09-07] (×55) subagents**: final text DISCARDED; API death mid-run leaves NOTHING; nested leak to GRANDPARENT. Non-general-purpose lacks SendMessage: idle_notification carries no content — pull result from subagents/agent-<name>-*.jsonl.
- **[2026-09-07] (×14) guards**: allowlist entries must STILL match else empty; new tests IMPORT `_ENGINE_PATTERN`. Deleted allowlisted file → `git rm` + strike BOTH. A cited SCRIPT FILENAME trips the name guard. Unspecified `xfail` cannot fail.
- **[2026-09-07] (×39) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-24] (×3) plan/task authoring**: a fix in the body but not the Decisions table / mermaid / doc-checklist is the top defect — sweep all 8 surfaces + the `.tsp`. `#team` gate → agent-runnable findings; never flip the box.
- **[2026-09-07] (×13) smoke/container**: `-o addopts=`; `tmp_path_factory`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`. `ARCHON_SEARCH_API_KEY` must be lowercase HEX else 401. `HF_HUB_OFFLINE=1` blocks fastembed too → ingest FAILS, not degrades.

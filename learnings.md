# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-20] (×58) pytest OOM/memory RCA**: rules: `tests/CLAUDE.md`. A DIFFERENT timing test failing per run = contention; raise a failure-path-only budget 5s→30s. Native leaks need a guarded repro (RSS cap+alarm); count BOTH embedder copies.
- **[2026-09-06] (×1) release-path CI**: GH caches are ref-scoped — a tag run reads its own tag or `main` only, and nothing runs on `main` here, so a release cache step never hits, only evicts the PR gate's. Autouse function fixtures beat module env.
## What Has Worked
- **[2026-08-27] (×1) TOCTOU lock fixes**: `asyncio.Lock` is non-reentrant — wrapping a method's body in its own lock deadlocks any caller that already holds it. Add `_locked_by_caller` (matches `ingest_chunks`), don't just wrap.
- **[2026-09-16] (×134) briefs**: failing repro FIRST; fix at the guard layer. Re-filed → `<name>-reopened.md`. `hasattr(own-method)` = DONE no-op. A non-disk `source_path` writer needs a `_run_orphan_cleanup` exemption (deletes on `exists()` false).
- **[2026-09-05] (×5) brief refinement**: grep every "reuse X from Y" claim. Deleting a feature strands wire fields added only for it (`provider_notes`); a sibling method survives; narrowing a protocol ripples to its probe + any "N sites" test.
**[2026-09-16] (×33) new field/pin**: dataclass + `_apply_toml` + coerce + snapshots. `body.filters`-only flag drops top-level — add top-level OR nested (S274). "or warning" branch can test UNBUILT code; build it, `_GRAMMAR_LOGGED` global restore.
- **[2026-08-14] (×18) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` to 0.1, don't shrink an outer `wait_for`. Poll `warmup_result` before asserting `/ready`.
- **[2026-09-15] (×36) warm-up/lifespan**: readiness gates it; x-encoder BEFORE preload; bound BOTH legs EVERY branch + pre-warmup `get_or_load` (S299); cold→`fts_only`; HyDE embeds first, LLM leg needs a budget wrap (S582). `shield`+`*WarmupTimeout`.
- **[2026-09-16] (×36) test vacuity**: split `or` asserts; overrides start OPPOSITE. Every ABSENCE assert needs a PRESENCE anchor + the deleted wording. A scenario saying "persisted" = read back via GraphStore, not `result.*`. A tester e2e adds a LAYER.
- **[2026-09-14] (×19) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, `_migrate_<field>()`, catalog, tests' `fake_<method>`. Fastembed ctors+probes: `cache_dir=get_models_dir()`.
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe OK but non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-20] (×12) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-09-29] (×32) install/wizard**: unprinted doc option→fix CODE; `is_multilingual=False` re-resolves `prof`. Interactive guard: optional-features prompts eat input()—assert prompt TEXT not count; force-reinstall rmtree needs `db_path.exists()`.
- **[2026-09-15] (×10) 3rd-party wiring**: verify params AND DICT KEYS on the REAL lib; a stub inventing keys masks the bug. fastembed `list_supported_models()` keys the name `"model"`, not `"name"`. GLiNER `relations` no-op off-RelEx (0.75 → 0 edges).

## Plan-Making & Agent Process
- **[2026-09-29] (×191) verify claims/state**: grep-verify every cite AND cited PRECEDENTs. Grep the tasks file for a spike's Notes. Repro vs the ticket's `Version:` tag, not only HEAD (`git worktree`). Re-grep after bulk `replace` — hits twins.
- **[2026-09-07] (×55) subagents**: final text DISCARDED; API death mid-run leaves NOTHING; nested leak to GRANDPARENT. Non-general-purpose lacks SendMessage: idle_notification carries no content — pull result from subagents/agent-<name>-*.jsonl.
- **[2026-09-15] (×17) guards**: allowlist entries STILL match; tests IMPORT `_ENGINE_PATTERN`; deleted file → `git rm`+strike BOTH; bare `xfail` can't fail. Doc gate: -Backlog/Completed, +README, PERIOD split, +red test; `warm.?up` ≠ "warms up".
- **[2026-09-29] (×48) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-24] (×3) plan/task authoring**: a fix in the body but not the Decisions table / mermaid / doc-checklist is the top defect — sweep all 8 surfaces + the `.tsp`. `#team` gate → agent-runnable findings; never flip the box.
- **[2026-09-14] (×16) smoke/container**: `-o addopts=`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`; throwaway `DATA_DIR` → symlink `models/`. REST `/import` allowlists `get_data_dir()`+`backup.output_dir` (S291); MCP's still only the former.

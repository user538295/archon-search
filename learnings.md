# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-10-12] (×62) pytest OOM/memory RCA**: rules: `tests/CLAUDE.md`. Never pytest a `tests/eval/corpus` path (.pyc breaks the corpus loader: 54 eval fails). Rotating timing fail = contention; failure-path budget 5s→30s. Leaks: archive.
- **[2026-10-12] (×2) release-path CI**: GH caches are ref-scoped — a tag run reads its own tag or `main` only, and nothing runs on `main` here, so a release cache step never hits, only evicts the PR gate's. Autouse function fixtures beat module env.
## What Has Worked
- **[2026-10-21] (×142) briefs**: failing repro FIRST; fix at guard layer. Re-filed → `<name>-reopened.md`. Same symptom as a sibling S-report (S40=S27)? Check its fix first. Non-disk `source_path` writer needs a `_run_orphan_cleanup` exemption.
- **[2026-09-05] (×5) brief refinement**: grep every "reuse X from Y" claim. Deleting a feature strands wire fields added only for it (`provider_notes`); a sibling method survives; narrowing a protocol ripples to its probe + any "N sites" test.
- **[2026-10-21] (×34) new field/pin**: dataclass + `_apply_toml` + coerce + snapshots. `body.filters`-only flag drops top-level — add top-level OR nested (S274). "or warning" branch can test UNBUILT code; build it, `_GRAMMAR_LOGGED` global restore.
- **[2026-10-21] (×25) xdist/asyncio**: `AsyncMock`; `asyncio.run()`; no MCP xdist group. Fake FastMCP only via `tests/_fastmcp_stub.py` `stubbed_fastmcp()` (restores; guard test). `/ready` 200 needs settled warm-up AND validation (S631).
- **[2026-10-12] (×42) warm-up/lifespan**: readiness gates it; x-encoder BEFORE preload; bound BOTH legs+pre-warmup `get_or_load` (S299); cold→`fts_only`; HyDE LLM: budget wrap (S582). Import warms pre-DONE; shutdown cancel→RUNNING (S288).
- **[2026-10-21] (×50) test vacuity**: split `or` asserts; overrides start OPPOSITE. ABSENCE assert needs PRESENCE anchor + deleted wording. "persisted" = read back via GraphStore. Tester e2e adds a LAYER. null→object breaks `is not None` pollers.
- **[2026-10-12] (×21) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, `_migrate_<field>()`, catalog, `fake_<method>`. fastembed `cache_dir=get_fastembed_cache_dir()` (S493).
- **[2026-08-18] (×16) CLI HTTP-proxy**: custom `--api-url` probe fail → NOT_RUNNING (S530). Default URL probe fail + service running → STARTING_MSG (c7829cbd). Probe OK but non-usable → NOT_RUNNING (C1-I-16). Distinguish via `_LOCAL_DEFAULT_URL`.
- **[2026-08-20] (×12) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-09-29] (×32) install/wizard**: unprinted doc option→fix CODE; `is_multilingual=False` re-resolves `prof`. Interactive guard: optional prompts eat input()—assert prompt TEXT not count; force-reinstall rmtree needs `db_path.exists()`.
- **[2026-10-13] (×16) 3rd-party wiring**: verify params+DICT KEYS on REAL lib. fastembed: models key `"model"`; no `cache_dir` → `<tmp>/fastembed_cache`. ORT>=1.31 F_GETPATH+APFS hardlinks flake ~5%: macOS-only retry. Real-model conftest: pop stub
## Plan-Making & Agent Process
- **[2026-10-21] (×215) verify claims/state**: grep-verify every cite AND cited PRECEDENTs. Grep the tasks file for a spike's Notes. Repro vs the ticket's `Version:` tag, not only HEAD (`git worktree`). Re-grep after bulk `replace` — hits twins.
- **[2026-10-21] (×56) subagents**: final text DISCARDED; API death mid-run leaves NOTHING; nested leak to GRANDPARENT. Non-general-purpose lacks SendMessage: idle_notification carries no content — pull result from subagents/agent-<name>-*.jsonl.
- **[2026-10-13] (×21) guards**: allowlist entries STILL match; tests IMPORT `_ENGINE_PATTERN`; deleted file → `git rm`+strike BOTH; bare `xfail` can't fail. Live-skip probe checks response SHAPE. Doc gate: -Backlog/Completed, +README, PERIOD split.
- **[2026-10-21] (×59) doc close-out**: grep WHOLE tree (+`README.md`, `*.toml.example`) for the old string. Error docs cite the FIRST rejecting layer. No `>` amid table rows. Compose override wins. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-24] (×3) plan/task authoring**: a fix in the body but not the Decisions table / mermaid / doc-checklist is the top defect — sweep all 8 surfaces + the `.tsp`. `#team` gate → agent-runnable findings; never flip the box.
- **[2026-10-14] (×21) smoke/container**: Dockerfile ENV pins `PIP_DEFAULT_TIMEOUT`/`PIP_RETRIES`; `-o addopts=`; pair `*_CONFIG`+`*_DATA_DIR`; tmp `DATA_DIR`: symlink `models/`. Run a lane cmd ONCE before gating it (multi-instance e2e: dev-box only).
- **[2026-10-21] (×1) multi-agent sweep**: pytest wait loop `ps|grep` counts its own `/bin/zsh` shells → deadlock; add `| grep -v '^/bin/zsh'` and bound the wait. Cap review at 3 cycles + a diff-size gate: each fix round widens scope (3.5 h, one bug).
- **[2026-10-21] (×1) branch integration**: parallel fix branches each add `BREAKING.md` + NR-index rows with the same NR-n — renumber once at merge (cross-refs, count prose); regen OpenAPI snapshots, never hand-merge; merged imports can vanish.

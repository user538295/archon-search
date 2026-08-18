# Learnings

Hard cap: under 30 lines, under 256 chars per line. Long-form detail: `learnings-archive.md` (grep it, never read whole).

## What Has Failed
- **[2026-08-15] (×50) pytest OOM/parallelism**: rules now live in `tests/CLAUDE.md`. Residual: a DIFFERENT timing test failing per full run = contention, not regression. Raise a failure-path-only budget 5s→30s — free on green, kills the flake.
- **[2026-07-05] (×3) implement-all**: no-bundle (temp defaults in task 1, thread callers later); fix-agent prompts ≤5 targeted fixes; violations emit a What/Why/Fix/Prevention block; `git status --porcelain` at 100%.

## What Has Worked
- **[2026-08-18] (×56) bug briefs**: write the failing repro FIRST; probe every config permutation. A green test proves only code+test agree — diff it against the DOC contract. Fix at the guard layer, never a call-site proxy.
- **[2026-08-18] (×54) brief triage**: a re-filed brief whose name is already in `Completed/` = regression or 2nd failure mode; close it as `<name>-reopened.md`. A doc-only fix is never behavioral — read the code after closing a DOC report.
- **[2026-08-14] (×25) regression-guard files**: `path_home_allowlist.txt` pins (file,lineno,sha) and moves with `_EXPECTED_CONFIG_LINE_NO`. A new `ReadinessChecks` field breaks SIX pinning tests — widen the bounded key set by one, never loosen.
- **[2026-08-14] (×25) new `SearchConfig` field**: dataclass + `_apply_toml` + coerce + snapshot tests. Regenerate the OpenAPI snapshot with `--update-openapi-snapshot` on 3.12. `response_model=None` trips `test_no_empty_schemas_remain`.
- **[2026-08-14] (×17) xdist/asyncio**: `async def`→`AsyncMock`; `asyncio.run()` not `get_event_loop()`; MCP tests need `xdist_group("mcp")`. Rebind `*TIMEOUT*` constants to 0.1 rather than shrinking an outer `wait_for` — the outer budget wins.
- **[2026-08-14] (×17) lifespan tasks**: an `app.state.<x>` set inside an `if config.<flag>:` branch needs a `= None` default. A `create_task`'d warm-up runs on TestClient's portal loop — poll `.done()`, never await it from the test loop.
- **[2026-08-14] (×17) test vacuity**: split `assert a in x or b in x`; guard loops with `assert results`; override tests start at the OPPOSITE value. A `return_value` mock cannot reproduce a malformed-input bug — make `side_effect` faithful.
- **[2026-08-11] (×17) new column/kwarg**: mirror ALL sites — dataclass, `_row_to_meta`, BOTH meta-write fns, every ctor, `_ROUTING_FIELDS`, plus `_migrate_<field>()` and a catalog entry. Then grep `tests/` for `def fake_<method>` and widen those.
- **[2026-08-18] (×15) CLI HTTP-proxy**: when `base_url` is given and connection refused, return NOT_RUNNING immediately — never consult the service manager (that reports the LOCAL instance, not the target). grep `tests/` first; mock where USED.
- **[2026-08-09] (×11) job-spawning route**: guard→404→create→`transition({QUEUED},RUNNING)` BEFORE `create_task`→track + `add_done_callback`. 409 via persisted `meta.<job>_job_id`, cleared BEFORE `job_store.update(DONE/FAILED)`.
- **[2026-08-03] (×9) LanceDB quirks**: `.limit()` is a scan limit, not sort-then-limit. `merge_insert(["c1","c2"])` for composite keys; `when_matched_update_all()` replaces the whole row. Missing table → `ValueError`. `db.close()` is sync.
- **[2026-08-18] (×20) install/wizard**: doc advertising an option the code never prints → fix the CODE. Base-URL probes prefix `http://`, splitting authority from path before the port. A prompt pinned by a locator AND a phrase admits ONE wording.
- **[2026-08-18] (×1) instruction-file bloat**: before adding to `CLAUDE.md`, grep `Documentation/` and `pyproject.toml` for the fact — its Architecture section was 57% of the file and fully duplicated. Keep only what is not greppable.

## Plan-Making & Agent Process
- **[2026-08-18] (×74) verify before acting**: `git ls-files Documentation/Completed/ | grep <ID>` + `git log --oneline -- <file the fix touched>` for LATER commits, then read CURRENT source — a "fix pre-existing failures" commit can revert a fix.
- **[2026-08-14] (×72) verify claims**: reviewer "Critical/Major" labels and plan file:line citations need grep-verification. Prove a repro fails against unmodified HEAD via `git worktree add --detach`, or `git stash push -- <file>` to exclude one file.
- **[2026-08-14] (×37) subagents**: a teammate's final text is DISCARDED — require a scratchpad file drop as the PRIMARY channel; poll `.jsonl` mtimes, not the inbox. Unreachable reviewers still ACT — re-run `git status` after every review round.
- **[2026-08-18] (×28) doc close-out**: grep the WHOLE tree (incl. `README.md`, `*.toml.example`) for the old invariant string — per-file scope orphans siblings. Never put a `>` block between table rows. Order: api-ref→catalog→CLAUDE.md→manual.
- **[2026-08-04] (×9) smoke/subprocess + TypeSpec**: `-o addopts=` not `-p no:xdist`; session fixtures use `tmp_path_factory`; pair `ARCHON_SEARCH_CONFIG` with `DATA_DIR`; seed real text. TypeSpec: `field?: T | null`; `namespace`/`model`/`op` reserved.

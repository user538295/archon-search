# Feature Brief: Install-Lock Parallel-Test Isolation + `Path.home()` Ratchet Guard

## Problem

`tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run` (and any future test that runs `installer.run()` without patching the lock) intermittently fails under parallel pytest with:

```
archon_search.install.InstallLockError: Install is already running (PID 5203).
Wait for it to finish or remove ~/.archon-search/.install.lock if stale.
```

Two structural defects compose:

1. `archon_search/install.py:48` — `_install_lock_path()` returns hardcoded `Path.home() / ".archon-search" / ".install.lock"`. Every xdist worker resolves to the same global path → real cross-process lock contention between workers.
2. There is no CI guard preventing the next hardcoded `Path.home()` callsite from re-introducing the same class of bug. The fix that closes one path stays leaky against any new contributor adding another.

The flake was confirmed in the agent session log `eed675f2-…jsonl:445` on 2026-06-12T13:35:31Z; a targeted re-run of the same test in isolation passed in 2.31s.

## Goal

Two observable outcomes:

1. **Zero install-lock collisions** across any number of xdist workers running `uv run pytest` (the default `-n auto --dist=loadgroup` config in `pyproject.toml:81`).
2. **Any new `Path.home()` callsite in `archon_search/`** added outside `archon_search/paths.py` fails CI before merge, with a clear error message pointing at the violation's file:line.

## Users & Context

Contributors running the test suite locally and in `archon-search-pr.yml` / `archon-search-release.yml` CI. State: running `uv run pytest` (or CI runs with `-n0` explicitly). The parallel-by-default boost was landed in commits `3e31cdf` (C10-2.1), `59c9397` (C12-1.2), `feb0950` (benchmark stabilisation); this brief closes the test-infrastructure regression those commits exposed.

## Core Flow

Engineering flow — what changes and in what order:

1. **`archon_search/install.py:_install_lock_path()`** is rewritten to `return get_data_dir() / ".install.lock"`. `get_data_dir()` is imported from `archon_search.paths`.
2. **Two adjacent `Path.home()` callsites in `install.py`** are migrated in the same change so the file is consistent: line 377 (`base_path = Path.home() / ".archon-search"`) and line 1508 (`log_dir = Path.home() / ".archon-search" / "logs"`). Both become `get_data_dir() / …`.
3. **`tests/conftest.py:_clear_archon_env_vars`** is replaced with `_archon_isolated_data_dir`. The new autouse fixture sets `ARCHON_SEARCH_DATA_DIR` to a per-worker isolated temp directory created from `tmp_path_factory.mktemp("archon-data")` at session scope (matching the `connected_store` and `three_page_pdf` patterns already in conftest). Other env vars previously cleared (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`) continue to be deleted as today.
4. **New pytest marker `archon_default_data_dir`** is registered in `pyproject.toml[tool.pytest.ini_options].markers`. Tests marked with it receive the autouse fixture in a "skip" branch that does `monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)` so the default-fallback codepath (`Path.home() / ".archon-search"`) is exercised.
5. **Eight existing tests** that assert default-fallback behavior get the new marker: `test_paths.py::test_default_returns_home_archon`, `test_key_manager.py::test_get_key_file_default`, `test_jobs_paths.py::test_get_jobs_file_default`, `test_language_detector_paths.py::test_get_fasttext_models_dir_default`, `test_language_detector.py::test_language_detector_default`, `test_config.py::test_config_defaults`, `test_service_linux.py::test_get_service_config_defaults`, `test_service_macos.py::test_get_service_config_defaults`.
6. **`tests/test_no_hardcoded_path_home.py`** is added, mirroring the regex-and-line-count pattern of `tests/test_no_fstring_sql.py`. It walks every `*.py` under `archon_search/`, compiles a regex `r"\bPath\.home\s*\("`, and asserts that violations match a checked-in **allowlist** file `tests/path_home_allowlist.txt`. Each line of the allowlist is `<relative_path>:<line>:<sha256-of-stripped-line>` — line numbers alone would drift on edits, so a content hash pins the exact callsite. The guard also includes meta-tests (the pattern from `test_no_fstring_sql.py:31-80`) that validate its own regex against fixtures.
7. **The allowlist is seeded** with the 14 grandfathered callsites that remain after step 2: install.py 1214, 1215, 1358, 1547; config.py 144; language_detector.py 10; cli/ingest.py 25; platform/linux.py 42, 100, 101; platform/macos.py 58, 71, 72, 73. `paths.py:81` is excluded by file-allowlist (the legitimate caller).
8. **A new behavioral test `tests/test_install_lock_per_worker_isolation.py`** asserts that two pytest workers can hold their own `_install_lock_path()` simultaneously without raising. Implemented as a single test that spawns two `multiprocessing.Process` instances and verifies both acquire successfully.
9. **`Documentation/Architecture/200_testing_strategy.md`** gets a paragraph describing the `archon_default_data_dir` marker and pointing at the ratchet allowlist as the source of truth for known `Path.home()` callsites.

## In Scope

- `archon_search/install.py:48` migration to `get_data_dir()`.
- `archon_search/install.py:377` and `1508` migration to `get_data_dir()` (DATA_DIR-shaped paths in the same file; coherent unit of change).
- `tests/conftest.py` autouse fixture refactor (clear → set-then-clear).
- `pyproject.toml` marker registration for `archon_default_data_dir`.
- 8 default-fallback tests get the new marker.
- `tests/test_no_hardcoded_path_home.py` + `tests/path_home_allowlist.txt` (ratchet guard).
- `tests/test_install_lock_per_worker_isolation.py` (behavioural confirmation).
- Documentation update in `200_testing_strategy.md`.

## Out of Scope

- `install.py:1214`, `1215` (LaunchAgents plist + systemd service file) — these are *system service paths*, not `$ARCHON_SEARCH_DATA_DIR` paths; correctly hardcoded today, stay grandfathered in the allowlist.
- `install.py:1358` (config TOML path) — handled by the separate `ARCHON_SEARCH_CONFIG` env var, not by `ARCHON_SEARCH_DATA_DIR`; out of scope for this brief.
- `install.py:1547` (fasttext model dir) — slated for migration under C9 Task 2.5; grandfathered until then.
- `language_detector.py:10`, `server/app.py`, `pipeline.py` — C9 Task 2.5; grandfathered.
- `cli/ingest.py:25` — C9 Task 2.6; grandfathered.
- `platform/linux.py`, `platform/macos.py` callsites — legitimately OS-specific service paths; will be permanently allowlisted (no future migration).
- `config.py:144` — by C9 design (line 50-51 of the plan), the config TOML path is not under DATA_DIR; permanently allowlisted.
- All other classes of parallel-test flake (LanceDB connection pool, telemetry log dir, etc.) — separate investigations if and when they surface.

## Key Decisions

- **Migrate install.py:48 + 377 + 1508 in a single change, not just 48.** Reason: "no intermediate solutions" per the requester. Doing only line 48 leaves the file half-migrated and the ratchet stale on the next touch. The three lines are all `~/.archon-search/...`-shaped, so they belong together.
- **Ratchet allowlist (Option B), not strict-zero (Option A).** Reason: strict-zero would force this brief to also close C9 Tasks 2.5 + 2.6 *and* migrate four platform service-path callsites that intentionally remain hardcoded. Ratchet pins today's baseline and forces every PR to either migrate-on-touch or justify a new allowlist entry. Allowlist shrinks naturally as C9 lands.
- **Hash-pinned allowlist entries (`file:line:sha256`), not bare `file:line`.** Reason: bare line numbers go stale on any unrelated edit above the callsite, producing spurious CI failures. The hash binds the entry to the literal source line content.
- **Opt-out marker (`archon_default_data_dir`), not bulk `delenv` in each fallback test.** Reason: the marker reads as a positive assertion ("this test validates default-data-dir behavior") and is greppable. Bulk `delenv` buries intent.
- **Session-scoped `tmp_path_factory` for the autouse, not function-scoped.** Reason: every test on a worker would otherwise spin up a new isolated dir; with session scope, each worker uses one dir for its whole session (matches the `connected_store` and `three_page_pdf` patterns). Function-scoped isolation isn't needed for the lock file because the lock is acquired and released within a single `installer.run()` call.
- **Standalone brief, not a C9 sub-task.** Reason: this is test-infrastructure work that *enables* C9, not part of the container product surface. Cross-link from `C9-container-support-plan.md` but don't nest.

## Edge Cases & Constraints

- **A pytest worker crashes mid-lock.** Existing stale-PID detection in `_acquire_install_lock` (`install.py:96-120`) handles this: next worker reads PID, calls `os.kill(pid, 0)`, sees `ProcessLookupError`, removes the stale lock, retries `O_EXCL`. Per-worker isolation now also means the next *test run* doesn't even see the stale lock — it's under a tmp dir that's about to be GC'd.
- **Tests that set `ARCHON_SEARCH_DATA_DIR` themselves via `monkeypatch.setenv` in the test body.** These continue to work — function-scoped monkeypatch overrides the autouse session fixture. Verified: 60+ test cases across `test_key_manager.py`, `test_jobs_paths.py`, `test_paths.py`, etc. need no changes.
- **Tests that already use the install-group xdist marker** (`test_install.py`, `test_install_run.py`, `test_install_run_c15.py`, `test_install_dry_run.py`, `test_install_lock.py`, `test_e2e_wizard_optional_features.py`). With per-worker DATA_DIR isolation, the xdist_group marker on these files becomes *unnecessary* for the lock collision (each worker has its own lock file). Decision: leave the markers in place — they may still be useful for other shared-state reasons (LanceDB connection pool, telemetry log dir) and removing them is a separate cleanup brief.
- **`tests/test_install_lock.py` patches `_install_lock_path` directly to `tmp_path`.** Continues to work — `patch(...)` overrides the function regardless of what env var the function would otherwise read. Verified all 11 tests.
- **`conftest.py`'s `connected_store` fixture creates LanceDB in `tmp_path_factory.mktemp("rag_db")`.** Unrelated to DATA_DIR; not affected.
- **`pyproject.toml:81` ships `--strict-markers`.** New marker registration in `[tool.pytest.ini_options].markers` is mandatory or every test using it errors at collection. Tests added with the marker before registration will fail CI immediately — good forcing function.
- **CI environment variables.** If `archon-search-pr.yml` or `archon-search-release.yml` already set `ARCHON_SEARCH_DATA_DIR`, the autouse fixture's `monkeypatch.setenv` overrides it — expected. If they ever set `ARCHON_SEARCH_KEY_FILE` and rely on it surviving into tests, the existing delete-in-autouse already breaks that today; no new breakage.

## Open Questions

- None blocking implementation. The allowlist's exact format (`file:line:sha256` vs. `file:hash_of_full_callsite`) is a small implementation detail to settle in the plan phase, not a brief-level decision.

## Future Iterations

- **C9 Task 2.7 (proposed):** migrate the remaining `Path.home()` callsites in `install.py` (lines 1547 in particular) and shrink the allowlist accordingly. Currently filed under C9 Task 2.5 for the fasttext model dir; install.py needs its own task because it has the heaviest concentration of unmigrated callsites.
- **Allowlist shrink ratchet:** every C9 Phase 2 task that lands also removes its callsite from `path_home_allowlist.txt`. The allowlist file becomes the live progress tracker for the migration.
- **Equivalent guard for hardcoded `~/.archon-search` string literals.** A second AST/regex guard could catch string-form references (`"~/.archon-search/..."`) that don't go through `Path.home()` — there are still ~5 in `config.py:46-96` and `install.py:685, 995`. Out of scope for this brief; useful follow-up if a similar flake surfaces.
- **Promotion of the marker pattern to other env-var-controlled defaults.** If `ARCHON_SEARCH_CONFIG`, `ARCHON_SEARCH_CONTAINER`, or `ARCHON_SEARCH_HOST` ever need autouse defaults for test isolation, the `archon_default_X` marker idiom generalises.

## Recommendation

Build this now. The C9 work upstream of it is already paying the cost of half-migrated paths — every `Path.home()` callsite outside `paths.py` is a latent parallel-test trap and a container-readiness gap. The hardest part of this brief is *not* the install.py migration (three trivial line edits) — it's the autouse fixture interacting cleanly with the 8 existing default-fallback tests; the marker design is what keeps that boundary auditable instead of a regrettable bulk edit. Do not compromise the ratchet allowlist: a stricter "zero `Path.home()` outside `paths.py`" guard would blow up scope, and a looser "warning-only" guard would silently rot the way the original install.lock callsite did.

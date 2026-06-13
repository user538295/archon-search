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

Reproducible via `uv run pytest tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run -n auto --count=20` (requires `pytest-repeat`; not a project dependency — install temporarily via `uv pip install pytest-repeat` if reproducing locally).

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

   The autouse fixture is **function-scoped** (not session-scoped), because the function-scoped `monkeypatch` cannot be requested from a session-scoped fixture (`ScopeMismatch`). The per-worker temp dir is created **once per session** via a **separate session-scoped fixture** that the function-scoped autouse depends on:

   ```python
   @pytest.fixture(scope="session")
   def _archon_worker_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
       return tmp_path_factory.mktemp("archon-data")

   @pytest.fixture(autouse=True)
   def _archon_isolated_data_dir(request, monkeypatch, _archon_worker_data_dir) -> None:
       for var in ("ARCHON_SEARCH_HOST", "ARCHON_SEARCH_PORT", "ARCHON_SEARCH_CONTAINER", "ARCHON_SEARCH_KEY_FILE", "ARCHON_SEARCH_CONFIG"):
           monkeypatch.delenv(var, raising=False)
       if "archon_unset_data_dir" in request.keywords:
           monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
       else:
           monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(_archon_worker_data_dir))
   ```

   Function-scoped autouse keeps `monkeypatch` working and lets per-test markers opt out. Session-scoped sub-fixture means one tmp dir per worker for the whole session, matching the `connected_store` pattern (no per-test mkdir cost).
4. **New pytest marker `archon_unset_data_dir`** is registered in `pyproject.toml[tool.pytest.ini_options].markers`. Tests marked with it receive the autouse fixture in a "skip" branch that does `monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)` so the default-fallback codepath (`Path.home() / ".archon-search"`) is exercised.
5. **Five existing tests** that assert default-fallback behavior get the new marker:
   - `test_paths.py::test_default_returns_home_archon`
   - `test_key_manager.py::TestGetKeyFile::test_get_key_file_default`
   - `test_jobs_paths.py::test_get_jobs_file_default`
   - `test_language_detector_paths.py::test_get_fasttext_models_dir_default`
   - `test_language_detector.py::test_module_constants` (asserts `get_fasttext_models_dir() == Path.home() / ".archon-search" / "models"`)

   Note: `test_config.py` tests of `get_default_config_path()` are governed by `ARCHON_SEARCH_CONFIG`, not `ARCHON_SEARCH_DATA_DIR`, and stay unaffected by the autouse. `test_service_linux.py` / `test_service_macos.py` tests use `tmp_path` for service file outputs and do not exercise the DATA_DIR fallback path.
6. **`tests/test_no_hardcoded_path_home.py`** is added. It **adopts the meta-test pattern from `tests/test_no_fstring_sql.py:31-80` and extends it with a hash-pinned allowlist not present in the reference**. It walks every `*.py` under `archon_search/`, compiles a regex `r"\bPath\.home\s*\("`, and asserts that violations match a checked-in **allowlist** file `tests/path_home_allowlist.txt`. Each line of the allowlist is `<relative_path>:<line>:<sha256-of-stripped-line>` — line numbers alone would drift on edits, so a content hash pins the exact callsite. Hash = `sha256(line.rstrip('\n').encode('utf-8'))`. Stripping is right-side only; leading whitespace is preserved so indented vs unindented callsites produce different hashes. The guard also includes meta-tests (the pattern from `test_no_fstring_sql.py:31-80`) that validate its own regex against fixtures.

   **Meta-test fixtures** the ratchet must include (minimum 4):
   - **positive match**: `x = Path.home() / "foo"` — must be flagged.
   - **no-parens negative**: `Path.home` without `()` — must NOT be flagged.
   - **lowercase negative**: `path.home()` — must NOT be flagged.
   - **string-literal positive (accepted false-positive)**: `"Path.home()"` inside a string IS matched (the regex has no string/comment awareness). This is by design — false positives are absorbed by the allowlist; the brief does not scope AST-level detection. The same applies to comments. Today's `archon_search/` has zero `Path.home()` references inside strings or comments outside the allowlisted callsites; any future false positive must be either migrated or added to the allowlist with rationale.
   - **comment behavior (decided)**: `# Path.home()` in a comment IS matched, for the same reason as strings (no AST-level detection scope). Per the same allowlist-absorption rule. The decision is final at the brief level.

   **Regex strategy (decided): per-line scanning, no `re.DOTALL`.** Each physical line in each `.py` file under `archon_search/` is matched against `r"\bPath\.home\s*\("`. Multiline callsites (`Path` on one line, `.home()` on the next) are out of scope — they are vanishingly rare in real-world Python and adding `re.DOTALL` would require redesigning the hash-pinned allowlist around logical statements, not lines. The multiline fixture is documented as a known gap rather than a required meta-test. This matches the simplicity of `tests/test_no_fstring_sql.py`.

   The ratchet asserts **both directions**:
   - every violation must appear in the allowlist (forward: no new callsites slip in), AND
   - every allowlist entry must match an actual violation in the source (reverse: dead entries are detected and reported).

   This keeps the allowlist a live source of truth — when a migration removes a grandfathered callsite, the corresponding allowlist line must be removed in the same PR or the test fails.

   The ratchet file additionally asserts that **the `archon_unset_data_dir` marker appears on exactly the N tests listed in step 5** (no more, no less). Mechanism: collect all tests with that marker via `pytest --collect-only -m archon_unset_data_dir -q`, parse the output, and diff against a pinned `MARKER_ALLOWLIST: frozenset[str]` constant. Prevents drive-by use of the marker to silence unrelated test flakes. The plan may prefer AST-based marker detection (grep for `@pytest.mark.archon_unset_data_dir` in source files) over invoking `pytest --collect-only` from inside a test — equally valid and avoids coupling to pytest's collection output format.

   **Test structure note for the plan:** `test_no_hardcoded_path_home.py` now carries three distinct responsibilities — (1) regex scan + hash-pinned allowlist, (2) self-validating meta-tests against the regex, (3) marker scope enforcement via `MARKER_ALLOWLIST`. The plan should structure these as three separate test functions (or a class with three methods) so a CI failure message identifies which responsibility broke. A failure in the marker-scope check shouldn't masquerade as a scan-regex failure.
7. **The allowlist is seeded** with the 12 grandfathered callsites that remain after step 2: install.py 1214, 1215, 1358, 1547; config.py 144; platform/linux.py 42, 100, 101; platform/macos.py 58, 71, 72, 73. `paths.py:81` is excluded by file-allowlist (the legitimate caller).
8. **A new behavioral test `tests/test_install_lock_per_worker_isolation.py`** asserts that two pytest workers can hold their own `_install_lock_path()` simultaneously without raising. Implementation: two `multiprocessing.Process` instances, each receiving a **distinct** `tmp_path` subdirectory as a constructor argument. Each child sets `os.environ["ARCHON_SEARCH_DATA_DIR"]` to its own path on entry (before importing `install.py` or calling `_install_lock_path()`) and verifies it acquires the lock. The parent's autouse-set `ARCHON_SEARCH_DATA_DIR` is intentionally overridden per-child so both workers exercise independent locks.

   The same file also includes a **regression test** with *both* workers sharing one `ARCHON_SEARCH_DATA_DIR`: the parent creates a `multiprocessing.Event` for coordination. **Process A**: sets `os.environ["ARCHON_SEARCH_DATA_DIR"]` to a shared dir, acquires the install lock, signals the event, then sleeps for 1 second to hold the lock. **Process B**: sets the same `ARCHON_SEARCH_DATA_DIR`, waits for the event, then attempts to acquire the lock and is expected to raise `InstallLockError`. The parent joins both processes and asserts Process B's exit code reflects the raise (e.g., via a `Queue` carrying the exception class name back). Without explicit lock-hold via the event, the test would be race-flaky — both processes might serialize naturally and both succeed, false-passing. This explicit hold is required. The regression test confirms `InstallLockError` is raised under shared-DATA_DIR contention, validating the error-handling path independently of the isolation mechanism.
9. **`Documentation/Architecture/200_testing_strategy.md`** gets a paragraph describing the `archon_unset_data_dir` marker and pointing at the ratchet allowlist as the source of truth for known `Path.home()` callsites.

**PR ordering for cleaner rollback:** the brief encompasses three logically separable changes; the plan should land them as three PRs. PR 1 stands alone (scan-only, no dependencies). PR 2 carries the highest risk because it changes the autouse fixture behavior across every test. PR 3 is trivial source-line edits guarded by the prior two PRs.

1. **PR 1 — Ratchet scan seeded with current state** (`tests/test_no_hardcoded_path_home.py` carries the regex scan + hash-pinned allowlist + meta-test fixtures only; `path_home_allowlist.txt` contains the 15 current callsites). Zero risk to other tests; no marker dependencies; no source changes.
2. **PR 2 — `conftest.py` autouse refactor** (`_archon_isolated_data_dir` + `_archon_worker_data_dir`) + marker registration (`archon_unset_data_dir`) + marker applied to the 5 verified default-fallback tests + the marker-scope meta-test in `test_no_hardcoded_path_home.py` (i.e., the `MARKER_ALLOWLIST: frozenset[str]` enforcement is added in this PR, not PR 1). Behavior change to every test; highest risk of unexpected breakage.
3. **PR 3 — `install.py` migration** of lines 48, 377, 1508 + shrinking the allowlist by 3 entries (15 → 12). Trivial code change; both the PR 1 scan and the PR 2 marker-scope guard already in place keep it honest.

## In Scope

- `archon_search/install.py:48` migration to `get_data_dir()`.
- `archon_search/install.py:377` and `1508` migration to `get_data_dir()` (DATA_DIR-shaped paths in the same file; coherent unit of change).
- `tests/conftest.py` autouse fixture refactor (clear → set-then-clear).
- `pyproject.toml` marker registration for `archon_unset_data_dir`.
- 5 default-fallback tests get the new marker.
- `tests/test_no_hardcoded_path_home.py` + `tests/path_home_allowlist.txt` (ratchet guard).
- `tests/test_install_lock_per_worker_isolation.py` (behavioural confirmation).
- Documentation update in `200_testing_strategy.md`.

## Out of Scope

- `install.py:1214`, `1215` (LaunchAgents plist + systemd service file) — these are *system service paths*, not `$ARCHON_SEARCH_DATA_DIR` paths; correctly hardcoded today, stay grandfathered in the allowlist.
- `install.py:1358` (config TOML path) — handled by the separate `ARCHON_SEARCH_CONFIG` env var, not by `ARCHON_SEARCH_DATA_DIR`; out of scope for this brief.
- `install.py:1547` (fasttext model dir) — slated for migration under a follow-up brief (see Future Iterations); grandfathered until then.
- `language_detector.py`, `cli/ingest.py`, `server/app.py`, `pipeline.py` — already migrated by C9 Phase 2 (commits 643bd29, e446a7d, etc.); zero remaining callsites.
- `platform/linux.py`, `platform/macos.py` callsites — legitimately OS-specific service paths; will be permanently allowlisted (no future migration).
- `config.py:144` — config TOML path is governed by `ARCHON_SEARCH_CONFIG`, not `ARCHON_SEARCH_DATA_DIR`; permanently allowlisted.
- All other classes of parallel-test flake (LanceDB connection pool, telemetry log dir, etc.) — separate investigations if and when they surface.

## Key Decisions

- **Migrate install.py:48 + 377 + 1508 in a single change, not just 48.** Reason: "no intermediate solutions" per the requester. Doing only line 48 leaves the file half-migrated and the ratchet stale on the next touch. The three lines are all `~/.archon-search/...`-shaped, so they belong together.
- **Ratchet allowlist (Option B), not strict-zero (Option A).** Reason: with C9 Phase 2 complete, the 12 remaining callsites are all in 4 files that are permanently grandfathered (`install.py` lines 1214, 1215, 1358, 1547; `config.py:144`; `platform/linux.py` 42/100/101; `platform/macos.py` 58/71/72/73). A *file-level* strict-zero allowlist (allow `Path.home()` only inside those 4 files + `paths.py`) is a viable simpler alternative and worth considering in the plan phase. The brief picks the line-level ratchet because (a) `install.py:1547` is slated for a follow-up migration and the ratchet shrinks naturally when it lands, and (b) line-level granularity surfaces every individual edit for review even within grandfathered files. **Decision deferred to plan: line-level ratchet (this brief) vs file-level strict-zero.**
- **Hash-pinned allowlist entries (`file:line:sha256`), not bare `file:line`.** Reason: bare line numbers go stale on any unrelated edit above the callsite, producing spurious CI failures. The hash binds the entry to the literal source line content.
- **Opt-out marker (`archon_unset_data_dir`), not bulk `delenv` in each fallback test.** Reason: the name describes the mechanism (unsets the env var) and is greppable. `archon_default_data_dir` (the original brief draft name) was deferred to the plan phase; the plan chose `archon_unset_data_dir` as more self-descriptive — a reader can infer the autouse-skip behavior from the name without consulting `conftest.py`.
- **Session-scoped sub-fixture for the per-worker temp dir; function-scoped autouse that depends on it.** Reason: every test on a worker would otherwise spin up a new isolated dir; with session scope on the sub-fixture, each worker uses one dir for its whole session (matches the `connected_store` and `three_page_pdf` patterns). The autouse itself stays function-scoped because the function-scoped `monkeypatch` cannot be requested from a session-scoped fixture (`ScopeMismatch`), and per-test markers must be able to opt out via `request.keywords`. Function-scoped isolation isn't needed for the lock file because the lock is acquired and released within a single `installer.run()` call.
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

- **C18 follow-up brief (proposed):** migrate the remaining `Path.home()` callsites in `install.py` (line 1547 — fasttext model dir specifically), shrinking the allowlist accordingly. C9 is now closed; any further `install.py` path migration needs its own brief ID.
- **Allowlist shrink ratchet:** when a follow-up brief (e.g., the proposed C18) migrates one of the grandfathered callsites, the same PR removes the corresponding allowlist entry. The allowlist file becomes the live progress tracker for the remaining migration; the bidirectional ratchet assertion (forward + reverse) keeps it honest.
- **Equivalent guard for hardcoded `~/.archon-search` string literals.** A second AST/regex guard could catch string-form references (`"~/.archon-search/..."`) that don't go through `Path.home()` — there are still ~5 in `config.py:46-96` and `install.py:685, 995`. Out of scope for this brief; useful follow-up if a similar flake surfaces.
- **Promotion of the marker pattern to other env-var-controlled defaults.** If `ARCHON_SEARCH_CONFIG`, `ARCHON_SEARCH_CONTAINER`, or `ARCHON_SEARCH_HOST` ever need autouse defaults for test isolation, the `archon_unset_X` marker idiom generalises.

## Recommendation

Build this now. The C9 work upstream of it is already paying the cost of half-migrated paths — every `Path.home()` callsite outside `paths.py` is a latent parallel-test trap and a container-readiness gap. The hardest part of this brief is *not* the install.py migration (three trivial line edits) — it's the autouse fixture interacting cleanly with the 5 existing default-fallback tests; the marker design is what keeps that boundary auditable instead of a regrettable bulk edit. Whether to keep the line-level ratchet or switch to a file-level strict-zero guard (allowing `Path.home()` only inside the 4 grandfathered files + `paths.py`) is deferred to the plan phase per Key Decisions; this brief provides the line-level ratchet as the default and lowest-risk path. Do not compromise the ratchet allowlist: a looser "warning-only" guard would silently rot the way the original install.lock callsite did.

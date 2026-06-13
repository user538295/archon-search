# C17 — Install-Lock Parallel-Test Isolation + `Path.home()` Ratchet Guard
**Purpose**: Eliminate xdist install-lock collisions and add a CI ratchet that fails any new hardcoded `Path.home()` callsite added to `archon_search/` outside `paths.py`.
**Audience**: archon-search contributors implementing C17; reviewers of the resulting PRs.
**Status**: To Do

---

## Background

`tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run` (and any future test running `installer.run()` without patching the lock) intermittently fails under parallel pytest with `InstallLockError: Install is already running` because `archon_search/install.py:_install_lock_path()` returns hardcoded `Path.home() / ".archon-search" / ".install.lock"` — every xdist worker resolves to the same global lock file. There is also no CI guard preventing the next hardcoded `Path.home()` callsite from re-introducing the same class of bug.

Full design rationale, key decisions, and edge cases are in `Documentation/Backlog/C17-install-lock-parallel-isolation-brief.md` (iterated through 3 cycles of `/iterative-review`; converged with no critical, major, or moderate open issues).

Reproducer for the flake: `uv run pytest tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run -n auto --count=20` (requires `pytest-repeat`; install locally with `uv pip install pytest-repeat`).

---

## Goal

Every `uv run pytest` run with `-n auto` produces zero `InstallLockError` from xdist worker contention. Any new `Path.home()` callsite added under `archon_search/` outside `paths.py` fails CI with a clear error pointing at the violation's file:line and a pinned content hash. The `archon_unset_data_dir` opt-out marker lets the 5 default-fallback tests still exercise the `Path.home() / ".archon-search"` codepath cleanly.

---

## Scope

### In Scope

- `archon_search/install.py` lines 48, 377, 1508 migration from `Path.home()` to `get_data_dir()`.
- `tests/conftest.py` autouse-fixture refactor: replace `_clear_archon_env_vars` with `_archon_isolated_data_dir` (function-scoped) + new `_archon_worker_data_dir` (session-scoped sub-fixture).
- `pyproject.toml` marker registration for `archon_unset_data_dir`.
- 5 default-fallback tests get the new marker.
- `tests/test_no_hardcoded_path_home.py` + `tests/path_home_allowlist.txt` (line-level hash-pinned ratchet with bidirectional assertion + AST-grep-based marker-scope enforcement).
- `tests/test_install_lock_per_worker_isolation.py` (behavioral confirmation + regression test under shared DATA_DIR).
- `Documentation/Architecture/200_testing_strategy.md` documentation update.

### Out of Scope

- `install.py:1214`, `1215`, `1358`, `1547`, `config.py:144`, `platform/linux.py:42/100/101`, `platform/macos.py:58/71/72/73` — permanently or temporarily grandfathered; explicitly seeded in `path_home_allowlist.txt`. `install.py:1547` (fasttext) is the explicit migration target of the proposed C18 follow-up brief.
- `language_detector.py`, `cli/ingest.py`, `server/app.py`, `pipeline.py` — already migrated by C9 Phase 2 (zero remaining callsites).
- LanceDB connection-pool, telemetry log-dir, or other classes of parallel-test flake — separate investigations.
- Hardcoded `~/.archon-search` *string-literal* references (not via `Path.home()`); useful follow-up but out of scope here.
- Tightening the existing C9-installed `xdist_group("install")` markers — separate cleanup brief.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.1 — Final verification & documentation update].

---

## What does NOT change

- `archon_search/install.py:_acquire_install_lock` stale-PID detection (lines 96–120) — unchanged; existing semantics preserved.
- `archon_search/paths.py:get_data_dir()` (lines 43–) — unchanged; reads `$ARCHON_SEARCH_DATA_DIR` on every call as today.
- `archon_search/config.py:144`, `archon_search/platform/{linux,macos}.py` callsites — unchanged; grandfathered in allowlist.
- `archon_search/install.py` lines 1214, 1215, 1358, 1547 — unchanged in this plan; allowlisted.
- `tests/test_no_fstring_sql.py` reference ratchet — unchanged; this plan adds a sibling, does not modify the existing one.
- `tests/conftest.py:connected_store` and `:three_page_pdf` fixtures — unchanged; the new sub-fixture mirrors their session-scoped `tmp_path_factory.mktemp` pattern.
- Existing `xdist_group("install")` markers on `test_install*.py` files — left in place (decision: see brief §Edge Cases).
- CI workflows (`archon-search-pr.yml`, `archon-search-release.yml`) — unchanged.

---

## Known limitations / accepted trade-offs

- **Multiline `Path.home()` callsites are out of scope for the ratchet.** Per-line regex scanning without `re.DOTALL` is the chosen strategy (matches `test_no_fstring_sql.py` simplicity); a callsite split across two physical lines would slip past. Vanishingly rare in real Python; not worth the allowlist redesign.
- **The ratchet regex matches inside strings and comments.** No AST-level detection. Today's `archon_search/` has zero `Path.home()` references inside strings or comments outside allowlisted callsites; future false positives must be migrated or explicitly allowlisted with rationale.
- **Hash-pinned entries (`file:line:sha256`) require updating the allowlist on any cosmetic edit to a grandfathered line.** Accepted: surfaces every individual line edit for review and makes the allowlist a live source of truth.
- **`archon_unset_data_dir` marker is opt-in by file/test author.** A drive-by use to silence an unrelated flake would be caught by the marker-scope meta-test (asserts marker appears on exactly the 5 pinned tests).
- **PR 3 (install.py migration + allowlist shrink) lands as one atomic commit.** The ratchet test fails if either the code change or the allowlist shrink lands without the other; they cannot be split.

---

## Architecture

### New modules / files

- `tests/test_no_hardcoded_path_home.py` — three test functions (scan + meta-tests + marker scope), structured per the brief's responsibility split.
- `tests/path_home_allowlist.txt` — checked-in plaintext allowlist; one entry per line `<relative_path>:<line>:<sha256-of-stripped-line>`. Seeded with 15 entries in PR 1; shrunk to 12 in PR 3.
- `tests/test_install_lock_per_worker_isolation.py` — two `multiprocessing.Process`-based tests (isolation + regression).

### Modified files

- `archon_search/install.py` lines 48, 377, 1508 — `Path.home() / ".archon-search" / ...` → `get_data_dir() / ...`. Adds `from archon_search.paths import get_data_dir` at top of file.
- `tests/conftest.py` lines 74–88 (around the existing `_clear_archon_env_vars` autouse) — replaced with the two-fixture pattern (`_archon_worker_data_dir` session-scoped + `_archon_isolated_data_dir` function-scoped autouse).
- `pyproject.toml` `[tool.pytest.ini_options].markers` — append `archon_unset_data_dir` marker registration.
- `tests/test_paths.py`, `tests/test_key_manager.py`, `tests/test_jobs_paths.py`, `tests/test_language_detector_paths.py`, `tests/test_language_detector.py` — `@pytest.mark.archon_unset_data_dir` decorator added to one test each.
- `Documentation/Architecture/200_testing_strategy.md` — paragraph added describing the marker and the ratchet.

### Key signatures

```python
# tests/conftest.py
@pytest.fixture(scope="session")
def _archon_worker_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Per-worker isolated DATA_DIR. One per pytest session per xdist worker."""
    return tmp_path_factory.mktemp("archon-data")

@pytest.fixture(autouse=True)
def _archon_isolated_data_dir(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _archon_worker_data_dir: Path,
) -> None:
    """Replaces _clear_archon_env_vars. Sets ARCHON_SEARCH_DATA_DIR per-worker
    unless the test is marked @pytest.mark.archon_unset_data_dir, in which case
    the env var is unset so the Path.home() default-fallback is exercised."""
    for var in (
        "ARCHON_SEARCH_HOST",
        "ARCHON_SEARCH_PORT",
        "ARCHON_SEARCH_CONTAINER",
        "ARCHON_SEARCH_KEY_FILE",
        "ARCHON_SEARCH_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    if "archon_unset_data_dir" in request.keywords:
        monkeypatch.delenv("ARCHON_SEARCH_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(_archon_worker_data_dir))
```

```python
# tests/test_no_hardcoded_path_home.py
import hashlib, re
from pathlib import Path

PATTERN = re.compile(r"\bPath\.home\s*\(")
ROOT = Path(__file__).resolve().parent.parent / "archon_search"
ALLOWLIST_FILE = Path(__file__).resolve().parent / "path_home_allowlist.txt"
FILE_ALLOWLIST = {"paths.py"}  # paths.py is the legitimate caller
MARKER_NAME = "archon_unset_data_dir"
MARKER_ALLOWLIST: frozenset[str] = frozenset({
    "tests/test_paths.py::test_default_returns_home_archon",
    "tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default",
    "tests/test_jobs_paths.py::test_get_jobs_file_default",
    "tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default",
    "tests/test_language_detector.py::test_module_constants",
})

def _hash_line(line: str) -> str:
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()

def _scan_violations() -> set[tuple[str, int, str]]:
    """Returns {(relative_path, line_no, sha256)} for every Path.home(
    callsite under archon_search/, excluding FILE_ALLOWLIST files."""
    ...

def _load_allowlist() -> set[tuple[str, int, str]]:
    """Parses path_home_allowlist.txt into the same tuple format."""
    ...

def test_path_home_ratchet() -> None: ...  # scan + bidirectional assertion
def test_meta_positive_match() -> None: ...
def test_meta_no_parens_negative() -> None: ...
def test_meta_lowercase_negative() -> None: ...
def test_meta_string_literal_positive() -> None: ...  # accepted false-positive
def test_archon_unset_data_dir_marker_scope() -> None: ...  # AST grep (added in Task 2.4)
def test_meta_ast_finds_pytest_mark_decorator() -> None: ...  # AST meta-test (added in Task 2.4)
```

```python
# tests/test_install_lock_per_worker_isolation.py
def _child_acquire(data_dir: str, queue: multiprocessing.Queue) -> None:
    """Run in child process: set env var, import install, acquire lock,
    push outcome onto queue (success or exception class name)."""
    ...

def test_two_workers_with_distinct_data_dirs_both_acquire(tmp_path: Path) -> None: ...
def test_two_workers_sharing_data_dir_contend(tmp_path: Path) -> None: ...
```

### Constants & env vars

- No new env vars.
- New pytest marker: `archon_unset_data_dir`.
- New constant: `MARKER_ALLOWLIST` (frozenset of 5 test node IDs) in `tests/test_no_hardcoded_path_home.py`.

---

## Task breakdown

### Phase 1 — PR 1: Ratchet scan seeded with current state

> **Releasable**: after Task 1.3. Phase 1 is a self-contained PR — zero risk to other tests; no marker dependencies; no source-code changes. Lands the scan + allowlist + meta-tests against the current 15-callsite state.

#### Task 1.1 — Seed `tests/path_home_allowlist.txt`

- [x] **File**: `tests/path_home_allowlist.txt`
- **Depends on**: nothing
- **Description**:
  - One-per-line entries `<relative_path>:<line_no>:<sha256-of-stripped-line>` where the path is relative to repo root and starts with `archon_search/`.
  - Seed with the 15 current callsites (verified by `grep -n 'Path\.home' archon_search/install.py archon_search/config.py archon_search/platform/linux.py archon_search/platform/macos.py`):
    - `archon_search/install.py:48:<sha>` — `_install_lock_path()`
    - `archon_search/install.py:377:<sha>` — `base_path`
    - `archon_search/install.py:1214:<sha>` — LaunchAgents plist
    - `archon_search/install.py:1215:<sha>` — systemd unit
    - `archon_search/install.py:1358:<sha>` — config TOML path
    - `archon_search/install.py:1508:<sha>` — `log_dir`
    - `archon_search/install.py:1547:<sha>` — fasttext model dir
    - `archon_search/config.py:144:<sha>` — config TOML path
    - `archon_search/platform/linux.py:42:<sha>` — systemd unit path
    - `archon_search/platform/linux.py:100:<sha>` — cwd
    - `archon_search/platform/linux.py:101:<sha>` — config path
    - `archon_search/platform/macos.py:58:<sha>` — LaunchAgents plist path
    - `archon_search/platform/macos.py:71:<sha>` — cwd
    - `archon_search/platform/macos.py:72:<sha>` — config path
    - `archon_search/platform/macos.py:73:<sha>` — log path
  - Each `<sha>` is `sha256(line.rstrip('\n').encode('utf-8'))` of the literal source line as it exists in the file (whitespace-preserved on the left, stripped on the right).
  - File has a one-line header comment (`# Hash-pinned allowlist for tests/test_no_hardcoded_path_home.py — see C17 plan`) and no other formatting.
- **Releasable**: after this task, the seed file exists at the repo root.
- **Tests (TDD)**: N/A — this task creates a static data file; its correctness is verified by Task 1.2's bidirectional assertion (which fails if any line:hash is wrong).
- **Checkpoint**: `wc -l tests/path_home_allowlist.txt` reports 16 (15 entries + 1 header comment); `grep -c '^archon_search/' tests/path_home_allowlist.txt` reports 15.

#### Task 1.2 — Add `tests/test_no_hardcoded_path_home.py::test_path_home_ratchet` scan + bidirectional assertion

- [ ] **File**: `tests/test_no_hardcoded_path_home.py`
- **Depends on**: Task 1.1
- **Description**:
  - Module-level constants: `PATTERN = re.compile(r"\bPath\.home\s*\(")`, `ROOT = Path(__file__).resolve().parent.parent / "archon_search"`, `ALLOWLIST_FILE = Path(__file__).resolve().parent / "path_home_allowlist.txt"`, `FILE_ALLOWLIST = {"paths.py"}`.
  - Helpers:
    - `_hash_line(line: str) -> str` — `sha256(line.rstrip('\n').encode('utf-8')).hexdigest()`.
    - `_scan_violations() -> set[tuple[str, int, str]]` — walks `ROOT.rglob("*.py")`, skips files whose `name` is in `FILE_ALLOWLIST`, scans each physical line with `PATTERN`, yields `(relative_path_str, line_no, _hash_line(raw_line))`. Per-line scanning, no `re.DOTALL`.
    - `_load_allowlist() -> set[tuple[str, int, str]]` — reads `ALLOWLIST_FILE`, skips blank lines and lines starting with `#`, parses `path:line:sha` into tuples. Raises `AssertionError` with a clear message if any line is malformed.
  - `test_path_home_ratchet()`:
    - Computes `violations = _scan_violations()` and `allowed = _load_allowlist()`.
    - Forward assertion: `assert violations <= allowed, f"New unallowlisted Path.home() callsites: {sorted(violations - allowed)}"`.
    - Reverse assertion: `assert allowed <= violations, f"Dead allowlist entries (remove them): {sorted(allowed - violations)}"`.
    - Hash-mismatch detection is implicit: an entry with the right `(path, line)` but a different `sha` appears in `allowed - violations`, so the reverse direction reports it.
  - The test must NOT use `re.DOTALL`. The multiline gap is an accepted limitation per the brief.
- **Releasable**: after this task, the ratchet test passes against the 15-entry seeded allowlist. Any new unallowlisted `Path.home()` callsite or any unrelated edit shifting a hash fails the test with a specific diff message.
- **Tests (TDD)** — `tests/test_no_hardcoded_path_home.py`:
  - The test function `test_path_home_ratchet` IS its own TDD subject. Red→green flow:
    1. Write the test against an empty `path_home_allowlist.txt` → expect 15 forward-direction failures.
    2. Add the 15 seed entries from Task 1.1 → all pass.
  - Checkpoint: `uv run pytest tests/test_no_hardcoded_path_home.py::test_path_home_ratchet -v`

#### Task 1.3 — Add meta-tests in `tests/test_no_hardcoded_path_home.py`

- [ ] **File**: `tests/test_no_hardcoded_path_home.py`
- **Depends on**: Task 1.2
- **Description**:
  - Adds 4 meta-tests that exercise `PATTERN` directly against in-memory fixtures (no codebase scan). Mirrors `tests/test_no_fstring_sql.py` lines 31–80.
  - `test_meta_positive_match()`: assert `PATTERN.search('x = Path.home() / "foo"')` is not None.
  - `test_meta_no_parens_negative()`: assert `PATTERN.search('x = Path.home + 1')` is None (no parens after `home`).
  - `test_meta_lowercase_negative()`: assert `PATTERN.search('x = path.home()')` is None.
  - `test_meta_string_literal_positive()`: assert `PATTERN.search('x = "Path.home()"')` is not None — this fixture documents the accepted false-positive (regex has no string-awareness).
  - Each meta-test's docstring states the fixture's purpose and includes the assertion-failure message that names the regex weakening that would cause the test to fail (e.g., dropping `\b` or `\s*`).
- **Releasable**: after this task, the regex is self-validating; a regex weakening would fail at least one meta-test.
- **Tests (TDD)** — `tests/test_no_hardcoded_path_home.py`:
  - The meta-tests ARE the tests. Red→green: write each meta-test, then verify the existing `PATTERN` makes it pass without modification.
  - Checkpoint: `uv run pytest tests/test_no_hardcoded_path_home.py -v` (now runs `test_path_home_ratchet` + 4 meta-tests = 5 passing).

---

### Phase 2 — PR 2: Conftest refactor + marker + behavioral test

> **Releasable**: after Task 2.5. Phase 2 is a single PR — highest-risk because the autouse fixture changes affect every test in the suite. PR 2 stands on PR 1 (Phase 1) being already merged so the ratchet catches any accidental `Path.home()` regression introduced while refactoring.

#### Task 2.1 — Register `archon_unset_data_dir` marker in `pyproject.toml`

- [ ] **File**: `pyproject.toml`
- **Depends on**: nothing (independent of Phase 1)
- **Description**:
  - Append to the `[tool.pytest.ini_options].markers` array:
    ```
    "archon_unset_data_dir: opt out of the autouse ARCHON_SEARCH_DATA_DIR isolation; the test exercises the Path.home() default-fallback path. Apply to the pinned MARKER_ALLOWLIST in tests/test_no_hardcoded_path_home.py only.",
    ```
  - Must land BEFORE any test is decorated with the marker (Task 2.3), or `--strict-markers` (already in `addopts`) errors at collection.
- **Releasable**: after this task, `@pytest.mark.archon_unset_data_dir` is a recognized marker; no test uses it yet.
- **Tests (TDD)**:
  - Unit: `uv run pytest --markers | grep archon_unset_data_dir` confirms registration.
  - No new test file; verified by Task 2.4's marker-scope meta-test once that's in place.
  - Checkpoint: `uv run pytest --markers 2>&1 | grep -F '@pytest.mark.archon_unset_data_dir'`

#### Task 2.2 — `tests/conftest.py`: replace `_clear_archon_env_vars` with the two-fixture pattern

- [ ] **File**: `tests/conftest.py`
- **Depends on**: Task 2.1 (marker must be registered before the autouse references it by string)
- **Description**:
  - Remove the existing `_clear_archon_env_vars(monkeypatch)` autouse (currently at lines 74–88 — verify exact range before editing).
  - Add the two replacement fixtures with the signatures from the Architecture section:
    - `_archon_worker_data_dir(tmp_path_factory)` — session-scoped, returns `tmp_path_factory.mktemp("archon-data")`.
    - `_archon_isolated_data_dir(request, monkeypatch, _archon_worker_data_dir)` — function-scoped autouse. Always deletes `ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`. Branches on `"archon_unset_data_dir" in request.keywords`: if marked, delete `ARCHON_SEARCH_DATA_DIR`; otherwise set it to `str(_archon_worker_data_dir)`.
  - Preserve the docstring summary and any existing comments in the rest of `conftest.py`.
- **Releasable**: after this task, every test runs with an isolated per-worker `ARCHON_SEARCH_DATA_DIR` by default. Tests that previously asserted default-fallback behavior (the 5 in Task 2.3) FAIL at this point — they're fixed by Task 2.3.
- **Tests (TDD)** — `tests/test_conftest_data_dir_isolation.py` (new, minimal):
  - Unit: `test_autouse_sets_data_dir(monkeypatch_unused: None) -> None` — no explicit fixture; asserts `os.environ["ARCHON_SEARCH_DATA_DIR"]` is set and is a `Path` that exists.
  - Unit: `test_autouse_unsets_other_vars() -> None` — pre-sets all 5 other env vars via `monkeypatch.setenv` in the test body (function-scoped monkeypatch overrides autouse), asserts they're cleared on entry. Actually verified by reading `os.environ` at the start of the test before any in-test setenv.
  - Unit: `test_two_workers_get_distinct_data_dirs(...)` — relies on running under xdist; if `PYTEST_XDIST_WORKER` env var is set, asserts the data dir path contains a worker-distinct segment. Skip otherwise.
  - Checkpoint: `uv run pytest tests/test_conftest_data_dir_isolation.py -v`

#### Task 2.3 — Apply `@pytest.mark.archon_unset_data_dir` to the 5 default-fallback tests

- [ ] **Files**:
  - `tests/test_paths.py::test_default_returns_home_archon`
  - `tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default`
  - `tests/test_jobs_paths.py::test_get_jobs_file_default`
  - `tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default`
  - `tests/test_language_detector.py::test_module_constants`
- **Depends on**: Task 2.2 (autouse fixture must respect the marker before applying it)
- **Description**:
  - Add `@pytest.mark.archon_unset_data_dir` above each of the 5 named test functions/methods.
  - Add `import pytest` to any file that doesn't already import it (`test_jobs_paths.py`, `test_language_detector_paths.py` may need it — verify before editing).
  - For the class-nested `TestGetKeyFile::test_get_key_file_default` in `test_key_manager.py`: decorator goes on the method, not the class.
  - No other changes to test bodies. The marker activates the "skip" branch in the autouse, restoring the pre-C17 default-fallback behavior for these 5 tests.
- **Releasable**: after this task, all 5 tests pass again (they failed at the end of Task 2.2). The 5-test set is now opt-out documentation: each test that exercises `Path.home() / ".archon-search"` defaults is greppable by `git grep '@pytest.mark.archon_unset_data_dir'`.
- **Tests (TDD)**:
  - No new tests. The 5 existing tests fail at end-of-Task-2.2 and pass at end-of-Task-2.3 — that IS the red→green cycle for this task.
  - Checkpoint: `uv run pytest tests/test_paths.py::test_default_returns_home_archon tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default tests/test_jobs_paths.py::test_get_jobs_file_default tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default tests/test_language_detector.py::test_module_constants -v`

#### Task 2.4 — Add marker-scope enforcement to `tests/test_no_hardcoded_path_home.py`

- [ ] **File**: `tests/test_no_hardcoded_path_home.py`
- **Depends on**: Task 2.3 (the 5 tests must already carry the marker), Task 1.3 (file exists)
- **Description**:
  - Add module-level constants:
    - `MARKER_NAME = "archon_unset_data_dir"`.
    - `MARKER_ALLOWLIST: frozenset[str]` — the 5 node IDs in `<relative_test_path>::[Class::]test_name` form. Use the exact set from the Architecture section.
    - `TESTS_ROOT = Path(__file__).resolve().parent`.
  - Add helper `_ast_scan_marker_users() -> set[str]`:
    - Walks `TESTS_ROOT.rglob("test_*.py")`.
    - Uses `ast.parse` to find every `FunctionDef` or `AsyncFunctionDef` whose `decorator_list` includes a node matching `pytest.mark.archon_unset_data_dir` (handle both `ast.Attribute` chains and `ast.Name` referenced directly).
    - For nested methods inside `ClassDef`, returns `<relative_test_path>::<ClassName>::<method_name>`; for top-level, returns `<relative_test_path>::<function_name>`.
    - Paths are relative to repo root (`Path(__file__).resolve().parent.parent`) so node IDs are stable across machines.
  - Add `test_archon_unset_data_dir_marker_scope()`:
    - Computes `actual = _ast_scan_marker_users()`.
    - `assert actual == MARKER_ALLOWLIST, f"Marker scope mismatch: extra={sorted(actual - MARKER_ALLOWLIST)} missing={sorted(MARKER_ALLOWLIST - actual)}"`.
  - Add one meta-test `test_meta_ast_finds_pytest_mark_decorator()`:
    - Writes a temp `.py` file with a sample decorated test function.
    - Calls `_ast_scan_marker_users()`-like helper on a temp dir and asserts the decorated function is found.
    - This validates the AST walker — prevents a syntax-change in pytest's marker style from silently disabling the scope check.
- **Releasable**: after this task, the ratchet file enforces all three brief-defined responsibilities; the marker cannot be drive-by-applied to silence an unrelated flake.
- **Tests (TDD)** — `tests/test_no_hardcoded_path_home.py`:
  - Self-validating per the file's pattern. Red→green: write `test_archon_unset_data_dir_marker_scope` with `MARKER_ALLOWLIST = frozenset()` → expect 5 missing-direction failures → fill in the 5 node IDs → passes.
  - Checkpoint: `uv run pytest tests/test_no_hardcoded_path_home.py -v` (now: ratchet + 4 regex meta-tests + marker scope + 1 AST meta-test = 7 passing).

#### Task 2.5 — Add `tests/test_install_lock_per_worker_isolation.py` (isolation + regression)

- [ ] **File**: `tests/test_install_lock_per_worker_isolation.py`
- **Depends on**: Task 2.2 (autouse fixture in place so the parent's DATA_DIR is well-defined; children explicitly override it)
- **Description**:
  - Module imports: `multiprocessing`, `os`, `pytest`, `pathlib.Path`.
  - Helper `_child_acquire(data_dir: str, hold_event: multiprocessing.Event | None, wait_event: multiprocessing.Event | None, hold_seconds: float, result_queue: multiprocessing.Queue) -> None`:
    - Sets `os.environ["ARCHON_SEARCH_DATA_DIR"] = data_dir` BEFORE importing `archon_search.install`.
    - Inside try/except: calls `archon_search.install._acquire_install_lock()`; on success pushes `("ok", None)`. On `InstallLockError` (or the subclass `archon_search.install.InstallLockError`) pushes `("err", "InstallLockError")`. On any other Exception pushes `("err", type(e).__name__)`.
    - If `wait_event` is not None, blocks on `wait_event.wait()` before attempting acquisition.
    - If `hold_event` is not None and acquisition succeeded, sets `hold_event`, sleeps `hold_seconds`, releases the lock (via the lock manager's `__exit__` or explicit `.release()` — use whichever the install module exposes; if only context-manager, use `with _acquire_install_lock(): hold_event.set(); time.sleep(hold_seconds)`).
  - `test_two_workers_with_distinct_data_dirs_both_acquire(tmp_path: Path) -> None`:
    - Creates `tmp_path / "worker_a"` and `tmp_path / "worker_b"` (both `mkdir`-ed).
    - Spawns two `multiprocessing.Process(target=_child_acquire, args=(str(dir_a), None, None, 0.0, queue))` and same for `dir_b`.
    - Joins both with a timeout (e.g., 10s).
    - Asserts both processes pushed `("ok", None)` to the queue.
  - `test_two_workers_sharing_data_dir_contend(tmp_path: Path) -> None`:
    - Creates `shared = tmp_path / "shared"` (mkdir-ed).
    - `hold_event = multiprocessing.Event()`.
    - Process A: `_child_acquire(str(shared), hold_event=hold_event, wait_event=None, hold_seconds=1.0, result_queue=queue)`.
    - Process B: `_child_acquire(str(shared), hold_event=None, wait_event=hold_event, hold_seconds=0.0, result_queue=queue)`.
    - Starts both, joins with 10s timeout.
    - Drains queue, finds exactly one `("ok", None)` (from A) and exactly one `("err", "InstallLockError")` (from B).
    - Asserts both outcomes are present.
  - Marker: `@pytest.mark.xdist_group("install")` on both tests so they don't interleave with other `install`-group tests; mirrors existing project pattern.
  - Add the test file's own node IDs to `MARKER_ALLOWLIST` only if these tests use `archon_unset_data_dir` (they DON'T — they use per-child env-var overrides explicitly).
- **Releasable**: after this task, the install-lock isolation fix is behaviorally guaranteed and the regression direction is locked in.
- **Tests (TDD)** — `tests/test_install_lock_per_worker_isolation.py`:
  - The two test functions ARE the deliverable.
  - Red→green: write `test_two_workers_with_distinct_data_dirs_both_acquire` against the unmigrated `install.py` (Path.home() hardcoded) → expect failure under shared `HOME`. Migrate (Phase 3) → passes. For Phase 2 verification, the test runs against pre-Phase-3 `install.py` but children write to distinct DATA_DIRs and skip the env var, so `_install_lock_path()` still resolves to `~/.archon-search/.install.lock` and the test would fail in Phase 2 alone.
  - **Workaround for Phase 2 standalone verification**: temporarily skip this test file with `pytest.skip` or `xfail` until Phase 3 lands. Add a `pytestmark = pytest.mark.xfail(reason="requires C17 Phase 3 install.py migration", strict=True)` at module level. Remove the `xfail` in Phase 3 Task 3.1.
  - Checkpoint (post-Phase-3): `uv run pytest tests/test_install_lock_per_worker_isolation.py -v`

---

### Phase 3 — PR 3: install.py migration + allowlist shrink

> **Releasable**: after Task 3.1. Phase 3 is one atomic task because the code change and allowlist shrink must land together or the ratchet test fails. Trivial source-line edits guarded by the PR 1 ratchet and PR 2 marker-scope guard already in place.

#### Task 3.1 — Migrate `install.py:48/377/1508` to `get_data_dir()` and shrink `path_home_allowlist.txt`

- [ ] **Files**:
  - `archon_search/install.py` (lines 48, 377, 1508 + imports)
  - `tests/path_home_allowlist.txt` (drop 3 entries)
  - `tests/test_install_lock_per_worker_isolation.py` (remove `xfail` from Task 2.5)
- **Depends on**: Task 2.5 (the behavioral test must exist so its un-xfailing demonstrates the fix)
- **Description**:
  - `archon_search/install.py`: add `from archon_search.paths import get_data_dir` to the import block.
  - Line 48: replace `Path.home() / ".archon-search" / ".install.lock"` with `get_data_dir() / ".install.lock"`.
  - Line 377: replace `Path.home() / ".archon-search"` with `get_data_dir()`.
  - Line 1508: replace `Path.home() / ".archon-search" / "logs"` with `get_data_dir() / "logs"`.
  - Verify post-edit line numbers for the surviving 4 allowlisted callsites in `install.py` (1214, 1215, 1358, 1547) — the 3 migrations are in-place rewrites of single lines, so the line numbers of subsequent callsites do NOT shift. Hashes for 1214, 1215, 1358, 1547 also remain valid (those source lines are unchanged).
  - `tests/path_home_allowlist.txt`: remove the 3 lines that pinned `archon_search/install.py:48:<sha>`, `:377:<sha>`, `:1508:<sha>`. File now has 12 entries + header = 13 lines.
  - `tests/test_install_lock_per_worker_isolation.py`: remove the module-level `pytestmark = pytest.mark.xfail(...)` from Task 2.5.
- **Releasable**: after this task, install-lock collisions are eliminated under `uv run pytest -n auto`. The behavioral test goes from xfail → pass.
- **Tests (TDD)**:
  - The plan's behavioral tests cover this: both `test_two_workers_with_distinct_data_dirs_both_acquire` and `test_two_workers_sharing_data_dir_contend` MUST pass at end-of-task. The ratchet `test_path_home_ratchet` MUST pass against the shrunk 12-entry allowlist. The 5 marked default-fallback tests MUST still pass (no change in their behavior).
  - Reproducer: `uv run pytest tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run -n auto --count=20` (requires local `pytest-repeat`) MUST pass with zero `InstallLockError`s — pre-edit this was the flake reproducer.
  - Checkpoint: `uv run pytest tests/test_install_lock_per_worker_isolation.py tests/test_no_hardcoded_path_home.py tests/test_paths.py::test_default_returns_home_archon tests/test_key_manager.py::TestGetKeyFile::test_get_key_file_default tests/test_jobs_paths.py::test_get_jobs_file_default tests/test_language_detector_paths.py::test_get_fasttext_models_dir_default tests/test_language_detector.py::test_module_constants -v`

---

### Final Phase — Verification & Documentation

#### Task 4.1 — Final verification & documentation update

- [ ] **File**: N/A (agent task)
- **Depends on**: Task 3.1
- **Description**:
  - Spawn an agent to discover all documentation in the project (READMEs, ADRs, API docs, architecture docs, user guides, `CLAUDE.md`, `CHANGELOG`) and update every file whose content is affected by C17:
    - `Documentation/Architecture/200_testing_strategy.md` — add a paragraph describing the `archon_unset_data_dir` marker, the `_archon_isolated_data_dir` autouse, and `tests/test_no_hardcoded_path_home.py` as the ratchet source-of-truth.
    - `Documentation/Architecture/990_documentation_index_and_contribution_guide.md` — if the index lists test-strategy entries, append the marker/ratchet mention.
    - `Documentation/Backlog/C17-install-lock-parallel-isolation-brief.md` — append a `**Status: Complete**` note at the top (or leave the brief untouched if the project convention is plan-tracks-status not brief).
    - `CLAUDE.md` — only if it references the conftest autouse pattern or test-marker conventions; otherwise no edit.
  - Do NOT touch unrelated docs.
  - Verify all acceptance criteria below pass before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - **AC1**: `uv run pytest -n auto` passes the full suite (matches the project's `--cov-fail-under=85` baseline; no new failures introduced).
  - **AC2**: `uv run pytest tests/test_install_lock_per_worker_isolation.py -v` reports 2 passed, 0 failed, 0 xfailed.
  - **AC3**: `uv run pytest tests/test_no_hardcoded_path_home.py -v` reports 7 passed (ratchet + 4 regex meta + marker-scope + AST meta), 0 failed.
  - **AC4**: `tests/path_home_allowlist.txt` has exactly 12 entries (one header + 12 callsites = 13 lines); `grep -c '^archon_search/' tests/path_home_allowlist.txt` reports 12.
  - **AC5**: `grep -c 'Path\.home' archon_search/install.py` reports 4 (lines 1214, 1215, 1358, 1547; lines 48, 377, 1508 are gone). `grep -c 'get_data_dir' archon_search/install.py` reports ≥3 (the 3 new callsites).
  - **AC6**: `uv run pytest --markers | grep archon_unset_data_dir` finds exactly one registered marker.
  - **AC7**: `git grep '@pytest.mark.archon_unset_data_dir' tests/` lists exactly 5 hits (one per pinned test).
  - **AC8**: `uv run pytest tests/test_install_ui.py::test_next_steps_not_printed_in_dry_run -n auto --count=20` (with `pytest-repeat` installed) passes 20/20 with zero `InstallLockError` (the original flake reproducer; manual verification by the implementer or reviewer — not auto-gated in CI).
  - **AC9**: `Documentation/Architecture/200_testing_strategy.md` contains paragraphs describing both the `archon_unset_data_dir` marker AND the `test_no_hardcoded_path_home.py` ratchet.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every AC1–AC9 above is checked.

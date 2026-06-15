# C18 — Test Suite Speed Fix: Clear `ANTHROPIC_API_KEY` in conftest autouse
**Purpose**: Eliminate the 30 s SDK-timeout floor that adds ~30–60 s of wall-clock to local `uv run pytest` whenever developers have `ANTHROPIC_API_KEY` exported in their shell.
**Audience**: Developers iterating locally on archon-search who already have `ANTHROPIC_API_KEY` exported for other Claude / Anthropic SDK work. CI is unaffected (uses `-n0` and never sets the key).
**Status**: Draft

---

## Background

On a developer machine with `ANTHROPIC_API_KEY` exported, every `ingest_directory` call against a new collection reaches `description_generator.generate_description` (`archon_search/description_generator.py:61`). That function wraps `_call_haiku` in `asyncio.wait_for(..., timeout=_TIMEOUT_SECONDS)` with `_TIMEOUT_SECONDS = 30` (`description_generator.py:26`, line 87). The SDK can hang in this test environment, raising per-affected-test wall-clock from a few seconds to ~35–36 s.

Two other call sites have the identical env-var gate and the same potential for a 30 s wait: `archon_search/hyde.py:101` and `archon_search/rag_fusion.py:138`.

This is a developer-machine-only problem. CI (`.github/workflows/archon-search-pr.yml`, `.github/workflows/archon-search-release.yml`) invokes pytest with `-n0` and never sets `ANTHROPIC_API_KEY`, so the timeout never fires there and none of the changes in this plan affect CI wall-clock.

The full reasoning, including alternative hypotheses (`INGEST_LOCK_TIMEOUT_S`, `model_validation.timeout_seconds`), measurement protocol, and the SDK fast-fail vs hang nuance for fake keys, lives in `Documentation/Backlog/C18-test-suite-speed-fixes-brief.md`. Read the brief before executing this plan.

## Goal

`uv run pytest` (no extra flags, using the configured `addopts` with `-n auto --dist=loadgroup`) completes measurably faster on a developer machine with `ANTHROPIC_API_KEY` set. The honest target is a measurable reduction attributable to Fix 1 alone — the post-fix p50 (5 runs) must be lower than the pre-fix p50 (5 runs) by more than the observed pre-fix run-to-run variance. No absolute number is claimed.

---

## Scope

### In Scope
- Add a separate `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` block in `tests/conftest.py:_archon_isolated_data_dir`, structurally distinct from the existing archon-namespace clearing loop, carrying an inline comment naming the SDK-timeout motivation.
- Execute the Pre-implementation Verification commands (Phase 1) before making the code change. Capture the baseline numbers as artifacts referenced by the final acceptance criteria.
- Execute the Post-implementation Verification commands (final task) after the change. The wall-clock delta becomes the honest improvement claim.
- Update any documentation that describes the test-runtime env-var contract (likely `CLAUDE.md`, `Documentation/Architecture/200_testing_strategy.md`, `Documentation/Architecture/500_development_workflows_and_conventions.md`, `Documentation/quick_start.md`).

### Out of Scope
- **Fix 2 (xdist-group `tests/pipeline/test_pipeline_ingest.py`)** — conditional; only justified if profiling under `--dist=loadgroup` shows a specific pileup after Fix 1.
- **Fix 3 (xdist-group `tests/test_sync_e2e.py`)** — same conditional gate.
- No changes to `pyproject.toml` `addopts`, markers, or `norecursedirs`.
- No production code changes (`description_generator.py`, `hyde.py`, `rag_fusion.py` untouched).
- Adding `tests/eval/live/` to `norecursedirs` — future iteration (see brief).
- Opt-out marker (`@pytest.mark.archon_keep_anthropic_key` analogue) — explicitly rejected in the brief's Design Decision #1.
- CI workflow changes — CI is unaffected.
- Conftest-level mock of `archon_search.description_generator.generate_description` — rejected (brief Option B); the env-var clear is the structurally honest fix.
- Pipeline-scoped conftest, session-scoped fixture, `pytest_runtest_setup` hook — all rejected in the brief (Options C / D / E).

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 3.1 — Final verification & documentation update].

---

## What does NOT change
- `ARCHON_SEARCH_API_KEY` — set globally at module import (`tests/conftest.py:35`); must remain set. The comment at `tests/conftest.py:94-95` explicitly documents this.
- The existing 5-tuple of cleared archon-namespace env vars (`ARCHON_SEARCH_HOST`, `ARCHON_SEARCH_PORT`, `ARCHON_SEARCH_CONTAINER`, `ARCHON_SEARCH_KEY_FILE`, `ARCHON_SEARCH_CONFIG`) — neither extended nor reordered.
- Tests that manage `ANTHROPIC_API_KEY` themselves via `monkeypatch.setenv` (`tests/test_description_generator.py`, `tests/test_hyde.py`, `tests/test_rag_fusion.py`, `tests/integration/test_wizard_e2e.py`, `tests/test_e2e_wizard_optional_features.py`, `tests/test_install_wizard_features.py`) — compose correctly because per-test `setenv` runs AFTER the autouse `delenv`, so the per-test override wins.
- Tests that already clear `ANTHROPIC_API_KEY` themselves (e.g. `test_no_api_key_returns_none` at `tests/test_description_generator.py:21-28`, `test_live_rag_fusion_fallback_on_missing_key` at `tests/eval/live/test_live_rag_fusion.py:319`) — the autouse `delenv` is idempotent with their own `delenv`.
- Production behavior — Fix 1 is a test-only change.
- CI workflows — `-n0` with no `ANTHROPIC_API_KEY` set means the timeout never fired in CI to begin with.

---

## Known limitations / accepted trade-offs
- **Live RAG-Fusion tests always skip**: after Fix 1, the 5 tests in `tests/eval/live/test_live_rag_fusion.py` that call `_skip_if_no_api_key()` (`tests/eval/live/test_live_rag_fusion.py:137, 181, 241, 376, 441`) ALWAYS skip on default runs, because the autouse clears the key in the worker process even when the developer has it in their shell. Brief Design Decision #1 resolved this as "accept the skip behavior" — no opt-out marker is introduced. The structural follow-up (adding `tests/eval/live/` to `norecursedirs`) is a future iteration.
- **No clean shell-level workaround**: there is no in-process env var the developer can set from the shell to bypass the autouse `delenv` for the 5 affected tests. Re-running those tests requires either a future opt-out marker or temporarily reverting the autouse `delenv` for the live invocation.
- **Coverage of `description_generator.py:80-97`** (SDK-call body + exception handlers) is preserved by `tests/test_description_generator.py`'s explicit `ClaudeSDKClient` mocks, which set the env var via `monkeypatch.setenv` and mock the SDK independently. If post-fix coverage of `description_generator.py` drops in any meaningful way, Task 3.1 requires adding a dedicated test that uses `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` and asserts the SDK-call path executes.
- **Wall-clock saving estimate**: under `-n auto --dist=loadgroup` with W=14 workers and N affected tests, the saving is approximately `ceil(N / W) × 30 s` in the best case (≈30–60 s if N≈17). Fix 1 alone may not hit any specific absolute target. If insufficient, the brief's conditional gate for Fixes 2/3 must be evaluated separately — they are out of scope for this plan.
- **Pre-implementation Verification cost**: Task 1.2's `pytest --durations=0 -n0 --no-cov` runs the entire suite serially, which is expected to take ~10–15 minutes per invocation (twice: once with the key set, once with it cleared). This is the honest cost of an enumerated affected-test list; estimating from memory would silently propagate the unverified "~17 tests" figure.
- **Pre-fix variance defines the noise floor**: if pre-fix max−min across 5 runs is X s, any improvement < X is treated as noise. This is intentional — the brief's measurement protocol prefers an honest "noisy, no signal" outcome over a single-anecdote claim.

---

## Architecture

Single change to `tests/conftest.py` in the existing `_archon_isolated_data_dir` autouse fixture (currently lines 84–108).

### Current state (`tests/conftest.py:84-108`)
```python
@pytest.fixture(autouse=True)
def _archon_isolated_data_dir(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _archon_worker_data_dir: Path,
) -> None:
    """..."""
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

### Target state
A new dedicated block is inserted AFTER the 5-tuple loop and BEFORE the `archon_unset_data_dir` branch:

```python
    for var in (
        "ARCHON_SEARCH_HOST",
        ...
        "ARCHON_SEARCH_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    # ANTHROPIC_API_KEY is a third-party vendor key (not archon-namespace).
    # When developers have it exported in their shell, every test that calls
    # `ingest_directory` on a new collection triggers `generate_description`,
    # which then sits in a 30 s asyncio.wait_for around the Claude SDK.
    # Clearing it here lets description_generator.py:76, hyde.py:101, and
    # rag_fusion.py:138 short-circuit on their early-exit guards. Tests that
    # need the key set use `monkeypatch.setenv` themselves — setenv runs
    # after this delenv, so the per-test override wins.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    if "archon_unset_data_dir" in request.keywords:
        ...
```

### Why a separate block, not an extension of the 5-tuple
`ANTHROPIC_API_KEY` is a third-party vendor key; the existing tuple is the archon-namespace clearing set. Conflating them would hide the SDK-timeout motivation behind a generic "env vars we clear" loop. Brief Option A is chosen explicitly to preserve the boundary.

### Why this exact fixture, not Options B–E
- **Option B** (autouse `AsyncMock(return_value=None)` for `generate_description` in `tests/pipeline/conftest.py`): rejected — silently suppresses a call site that 3 existing tests already mock deliberately; adds a second patching layer that confuses future readers.
- **Option C** (pipeline-scoped conftest): rejected — affected tests are NOT all under `tests/pipeline/` (`tests/test_sync_e2e.py`, `tests/test_pipeline_acl.py`, `tests/test_pipeline_code_enricher.py`, `tests/test_pipeline_ingest_directory_fts.py`, `tests/integration/test_fts_delete_no_phantom.py` are all outside).
- **Option D** (session-scoped fixture): rejected — would surrender the per-test override semantics that `monkeypatch.setenv` relies on in `test_hyde.py`, `test_rag_fusion.py`, `test_description_generator.py`.
- **Option E** (`pytest_runtest_setup` hook): rejected — equivalent in effect but adds a second mechanism (hook + autouse) that future readers must locate.

### Affected call sites (all production, unchanged here)
| File | Line | Guard | Return on missing key | Side effect |
|---|---|---|---|---|
| `archon_search/description_generator.py` | 76 | `if not os.environ.get("ANTHROPIC_API_KEY"): return None` | `None` | none |
| `archon_search/hyde.py` | 101 | same | `None` | one-time `_warned_no_key` log warning |
| `archon_search/rag_fusion.py` | 138 | same | `[]` | one-time `_warned_no_key` log warning |

The three guards differ in return type (`None` vs `[]`) and warning state, so a DRY-shared helper is NOT introduced (brief Future Iterations: "Affected-call-site audit" — explicitly defers this).

No new modules, classes, functions, config keys, env vars, or API surface changes.

---

## Task breakdown

### Phase 1 — Pre-implementation verification & baseline capture
> **Releasable**: nothing user-visible — this phase is a measurement gate. After this phase the team has (a) confirmed the 30 s floor is the SDK timeout, (b) enumerated the actual affected tests, and (c) captured a defensible pre-fix baseline. **No code changes yet.** Per the brief's "Pre-implementation Verification" section, all four tasks below MUST complete before Phase 2.

#### Task 1.1 — Confirm the 30 s floor IS `ANTHROPIC_API_KEY` → `generate_description` (not an alternative)
- [ ] **File**: N/A (verification command; output captured as plan artifact)
- **Depends on**: nothing
- **Description**:
  - Run two known-symptomatic tests with `ANTHROPIC_API_KEY` explicitly cleared:
    ```
    ANTHROPIC_API_KEY= uv run pytest \
      tests/test_pipeline_code_enricher.py::test_ingest_directory_forwards_collection_root \
      'tests/test_sync_e2e.py::TestS15_3_FileModifiedIncrementalUpdate::test_sync_reindexes_on_file_change' \
      -v --no-cov -n0
    ```
  - **Expected**: both drop from ~36 s to a few seconds.
  - **If either stays at ~36 s**: the root cause is NOT `generate_description` for that test. Flag as an alternative-hypothesis candidate (see brief's "Alternative Hypotheses" section: `archon_search/constants.py:19` `INGEST_LOCK_TIMEOUT_S = 30.0`, or `archon_search/model_validation.py:24` `timeout_seconds = 30.0`). Record the test name and its persistent 30 s symptom for follow-up — Fix 1 will not help that test.
  - Record per-test wall times in the plan artifact (commit message body of Task 2.1, or a verification note appended to the brief).
- **Releasable**: hypothesis confirmation captured for the tested subset; alternative-hypothesis candidates (if any) flagged for follow-up outside this plan.
- **Tests (TDD)**: N/A — this is a hypothesis-validation step.
- **Checkpoint**: above command run; per-test wall time recorded.

#### Task 1.2 — Enumerate affected tests (real list, with and without key)
- [ ] **File**: N/A (verification command; output captured as plan artifact)
- **Depends on**: nothing
- **Description**:
  - A real `ANTHROPIC_API_KEY` is REQUIRED for the with-key invocation. Fake keys may fail-fast at the SDK level and produce no enumeration (brief: "SDK fast-fail vs hang for fake/malformed keys").
  - **With-key list**:
    ```
    test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; }
    ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest --durations=0 -n0 --no-cov 2>&1 | awk '$1+0 > 25' | head -40
    ```
  - **If output is exactly 40 lines**: re-run WITHOUT `| head -40` to capture the full list. Silent truncation at the hypothesis-validation step would propagate an undercount into the implementation plan.
  - **Without-key list** (counterpart; precondition guard skipped here intentionally):
    ```
    ANTHROPIC_API_KEY= uv run pytest --durations=0 -n0 --no-cov 2>&1 | awk '$1+0 > 25' | head -40
    ```
  - Diff the two lists. Tests still >25 s with the key cleared are NOT `generate_description` cases — they are alternative-hypothesis candidates (consistent with Task 1.1).
  - Runtime ~10–15 minutes serial each. If the SDK hangs on many tests, the with-key run will be longer; allow it to complete.
- **Releasable**: an honest inventory of affected tests, replacing the brief's unverified "~17 tests" estimate. The diff between the two lists is the actual Fix 1 candidate set.
- **Tests (TDD)**: N/A — verification task.
- **Checkpoint**: both outputs captured (in full, not truncated) and saved into the plan artifact referenced by Task 3.1.

#### Task 1.3 — Capture baseline wall-clock (1 warm-up + 5 measured runs, p50)
- [ ] **File**: N/A (verification command; output captured as plan artifact)
- **Depends on**: nothing
- **Description**:
  - Per the brief's Measurement Protocol:
    - 1 warm-up run, discarded
    - 5 measured runs, p50 recorded
    - No other heavy processes running; `~/.archon-search/` clear NOT required (autouse fixture isolates per-worker data dirs)
  - Command:
    ```
    test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; }
    ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest --no-cov 2>&1 | tail -3
    ```
  - Record: each of the 5 wall-clock totals, the p50, AND the run-to-run variance (max − min). The variance defines the noise floor for the post-fix delta in Task 3.1.
  - Machine specs (brief author: M-series Apple Silicon, 14 logical CPUs). Other machines will produce different absolute numbers — what matters is the within-machine delta.
- **Releasable**: defensible pre-fix baseline captured; post-fix delta in Task 3.1 must exceed the observed variance to count as signal.
- **Tests (TDD)**: N/A — measurement task.
- **Checkpoint**: 5 wall-clock totals + p50 + variance recorded.

#### Task 1.4 — Capture pre-change collected count and key file pass/skip counts
- [ ] **File**: N/A (verification command; output captured as plan artifact)
- **Depends on**: nothing
- **Description**:
  - **Total collected count** (replaces the brief's unverified "4700+" figure):
    ```
    uv run pytest --collect-only -q 2>&1 | tail -3
    ```
  - **Targeted pass/skip count for the 3 most env-var-entangled files** (these are the files Task 3.1 will use for the regression check):
    ```
    uv run pytest tests/test_hyde.py tests/test_rag_fusion.py tests/test_description_generator.py --no-cov -n0 2>&1 | tail -1
    ```
  - Record both numbers. Task 3.1's acceptance criteria assert exact equality (collected count unchanged; key file pass/skip unchanged after the autouse change).
- **Releasable**: pre-change pass/skip baseline captured for the regression check.
- **Tests (TDD)**: N/A — verification task.
- **Checkpoint**: collected count and `N passed, M skipped` summary both recorded.

---

### Phase 2 — Apply Fix 1
> **Releasable**: after Task 2.1, developer wall-clock benefits from Fix 1 when `ANTHROPIC_API_KEY` is exported in the shell. The 30 s SDK floor is removed by the autouse `delenv` on every test; the three production guards in `description_generator.py:76`, `hyde.py:101`, `rag_fusion.py:138` short-circuit immediately.

#### Task 2.1 — Add `ANTHROPIC_API_KEY` delenv block to `_archon_isolated_data_dir`
- [ ] **File**: `tests/conftest.py`
- **Depends on**: Task 1.1, Task 1.2, Task 1.3, Task 1.4 (per the brief: "Steps 1-4 MUST complete BEFORE making any code change")
- **Description**:
  - In the existing autouse fixture body (currently lines 84–108), add a dedicated block AFTER the 5-tuple `for var in (...)` loop (line 97–104) and BEFORE the `if "archon_unset_data_dir" in request.keywords` branch (line 105).
  - The block must contain:
    1. A multi-line `#`-comment naming the motivation (third-party vendor key; 30 s SDK timeout; the three call-site guards; composition rule with per-test `setenv`).
    2. Exactly one line of code: `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)`.
  - **Do NOT extend the existing 5-tuple loop.** Keeping the block structurally separate preserves the archon-namespace vs vendor-key boundary (brief Option A is explicit on this).
  - **Do NOT modify** any other line: fixture signature, scope, docstring, the 5-tuple itself, or the `archon_unset_data_dir` branch.
  - **Use `raising=False`** so the delenv is a no-op when the key is already absent (e.g. CI, fresh shells, tests that already cleared it themselves like `test_no_api_key_returns_none` at `tests/test_description_generator.py:23`).
  - The fixture remains function-scoped autouse (it already is). This composes correctly with per-test `monkeypatch.setenv("ANTHROPIC_API_KEY", ...)` calls because pytest's setup ordering runs the autouse fixture body BEFORE the per-test body executes its own `monkeypatch.setenv` — the per-test setenv wins (brief Edge Cases: verified for `test_description_generator.py`, `test_hyde.py`, `test_rag_fusion.py`, `test_wizard_e2e.py`, `test_e2e_wizard_optional_features.py`, `test_install_wizard_features.py`).
- **Releasable**: when developer runs `uv run pytest` with `ANTHROPIC_API_KEY` exported, every test sees the env var cleared at fixture setup; `description_generator.py:76-78`, `hyde.py:101`, and `rag_fusion.py:138` short-circuit immediately; the 30 s SDK floor is eliminated for every test that does not re-set the key.
- **Tests (TDD)** — verified by the existing suite + targeted smoke commands; no new test file:
  - **Composition smoke** (the 3 files most architecturally entangled with the env var must show identical pass/skip counts to Task 1.4):
    - Checkpoint: `uv run pytest tests/test_hyde.py tests/test_rag_fusion.py tests/test_description_generator.py --no-cov -n0 2>&1 | tail -1`
    - Expected: same `N passed, M skipped` as Task 1.4 baseline. A pass→skip or pass→fail shift IS a regression and must be investigated before Phase 3.
  - **30 s floor elimination smoke** (the two known-slow tests from Task 1.1, with key set in the shell — this time WITHOUT the manual `ANTHROPIC_API_KEY=` clear, because the autouse now does it):
    - Checkpoint:
      ```
      test -n "$ANTHROPIC_API_KEY" || { echo "ANTHROPIC_API_KEY must be set; aborting"; exit 1; }
      ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY uv run pytest \
        tests/test_pipeline_code_enricher.py::test_ingest_directory_forwards_collection_root \
        'tests/test_sync_e2e.py::TestS15_3_FileModifiedIncrementalUpdate::test_sync_reindexes_on_file_change' \
        -v --no-cov -n0
      ```
    - Expected: both drop from ~36 s to a few seconds. If either stays at ~36 s after Fix 1, either the alternative-hypothesis case from Task 1.1 also covers this test, or the autouse change did not apply — diagnose before proceeding to Phase 3.
  - **Diff inspection**:
    - Checkpoint: `git diff tests/conftest.py` shows ONLY the new block (a comment + one `monkeypatch.delenv` line), inserted in the correct location, with no other lines changed.

---

### Phase 3 — Final verification & documentation update

#### Task 3.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task; verification commands + doc updates)
- **Depends on**: Task 2.1
- **Description**:
  - Spawn an agent to discover and update any documentation file describing:
    - The test-runtime env vars cleared by the autouse fixture, and/or
    - Expected behavior when `ANTHROPIC_API_KEY` is set during local pytest runs.
  - Likely candidates (the agent must verify each by reading, not assume): `CLAUDE.md`, `Documentation/Architecture/200_testing_strategy.md`, `Documentation/Architecture/500_development_workflows_and_conventions.md`, `Documentation/quick_start.md`.
  - Discovery command (excludes `Documentation/Completed/` — historical records, never rewritten): `grep -rn 'ANTHROPIC_API_KEY' Documentation/ CLAUDE.md --include='*.md' | grep -v 'Documentation/Completed/'`.
  - The agent updates ONLY files whose content is now inconsistent with the new autouse contract. Unrelated occurrences (e.g. user-facing docs about setting the key for live RAG-Fusion eval) stay untouched.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, Fix 1 is verified end-to-end, the developer-DX impact is documented (with numbers), and the docs reflect the new test-env contract.
- **Acceptance criteria** (must all pass):
  - [ ] **Diff scope**: `git diff tests/conftest.py` shows ONLY the new `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` block (comment + one delenv line) inside `_archon_isolated_data_dir`, structurally distinct from the existing 5-tuple loop.
  - [ ] **Production code unchanged**: `git diff archon_search/` produces no output.
  - [ ] **Pyproject / config unchanged**: `git diff pyproject.toml` and `git diff .github/` both produce no output.
  - [ ] **Existing 5-tuple unchanged**: the `ARCHON_SEARCH_HOST/PORT/CONTAINER/KEY_FILE/CONFIG` clearing loop is unmodified (no entries added or removed). `ARCHON_SEARCH_API_KEY` remains set globally at `tests/conftest.py:35`.
  - [ ] **Wall-clock improvement**: 5-run p50 with Fix 1 applied (per Measurement Protocol, key present in shell, same machine) is LOWER than the Task 1.3 p50 by a margin LARGER than the pre-fix variance from Task 1.3. Document the absolute p50 before, p50 after, delta, and pre-fix variance. Do NOT claim improvement on noise. If the delta is within the noise band, record the outcome honestly and proceed to "Fix 1 alone may not hit any specific absolute target" — see the brief's conditional gate for Fixes 2/3 (out of scope for C18).
  - [ ] **Coverage**: `uv run pytest 2>&1 | grep description_generator` per-module coverage is documented before and after Fix 1. The overall `--cov-fail-under=85` gate still passes. If `description_generator.py` coverage drops in any meaningful way (>5 percentage points or below the 85% gate when projected onto the module), add a dedicated test in `tests/test_description_generator.py` that does `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")`, mocks `ClaudeSDKClient`, and asserts the SDK-call path at `description_generator.py:80-97` executes — matches the existing pattern at `tests/test_description_generator.py:30-78`.
  - [ ] **Collected count**: `uv run pytest --collect-only -q 2>&1 | tail -3` total is EQUAL to the value captured in Task 1.4. Any change is a regression — investigate before merging.
  - [ ] **Pass-count regression**: full-suite `passed` count is within `[Task 1.4 passed − 5, Task 1.4 passed]`. The 5 tests in `tests/eval/live/test_live_rag_fusion.py` at lines 137, 181, 241, 376, 441 that gate on `ANTHROPIC_API_KEY` now always skip (brief Edge Cases). The 6 tests in `tests/eval/live/test_live_acceptance.py` and `tests/eval/live/test_live_eval_suite.py` are unaffected — they don't check the env var. Any OTHER pass→skip or pass→fail shift IS a regression.
  - [ ] **Targeted HyDE / RAG-Fusion / description_generator regression check**: `uv run pytest tests/test_hyde.py tests/test_rag_fusion.py tests/test_description_generator.py --no-cov -n0 2>&1 | tail -1` shows the SAME `N passed, M skipped` as Task 1.4. A pass→skip shift in any of these 3 files IS a regression (these are the files most architecturally entangled with the env var).
  - [ ] **Serial mode still passes**: `uv run pytest -n0` passes (matches the C12 acceptance pattern).
  - [ ] **Docs updated**: every active doc that previously described `ANTHROPIC_API_KEY` behavior in tests now reflects the new autouse contract. Verify via `grep -rn 'ANTHROPIC_API_KEY' Documentation/ CLAUDE.md --include='*.md' | grep -v 'Documentation/Completed/'`; every match either describes the new behavior accurately or is unrelated to the test env (e.g. user-facing notes about setting the key for live eval).
  - [ ] **Brief artifacts captured**: the artifacts produced by Tasks 1.1–1.4 (hypothesis confirmation, affected-test inventory, baseline p50, pre-change pass/skip counts) and Task 3.1 (post-fix p50, delta, variance, post-fix pass/skip counts) are recorded somewhere durable — either appended to `Documentation/Backlog/C18-test-suite-speed-fixes-brief.md` under a "Measured results" section (mirrors the C12 pattern at `Documentation/Completed/C12-dist-load-session-store-brief.md:88`), or in the commit message body for Task 2.1.
- **Tests (TDD)**: N/A — verification + documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

# A7 — fsync Hardening for Durable State Writes

**Purpose**: Implement the durability contract from `Documentation/Backlog/a7-fsync-hardening-brief.md` — every durable state write in archon-search survives a kernel crash or power loss.
**Audience**: archon-search contributors implementing A7; reviewers of the resulting PRs.
**Status**: Draft

---

## Background

`os.replace()` is atomic at the rename layer but offers no durability guarantee: the file's data, the rename, and the parent directory's dirent can all sit in dirty kernel pages when the call returns. On power loss or kernel crash, archon-search can therefore lose or corrupt indexing progress (`progress.py`), the sync manifest (`sync.py`), the job store (`jobs/store.py`), and the API key (`key_manager.py`) even though the writes appeared to succeed. Two sites are worse than that: `sync.manifest_remove_entry` and `telemetry/writer._append` skip the temp+rename pattern entirely.

The brief at `Documentation/Backlog/a7-fsync-hardening-brief.md` resolved three review cycles of design questions (telemetry restructure, error-propagation per call-site, ADR scope, CI lint gate, tmpfs detection, PR sequencing). This plan implements that decided design as a 4-PR sequence.

## Goal

After A7 ships: every named durable write site (`progress.py`, `sync.py` × 2, `jobs/store.py`, `key_manager.py`, `telemetry/writer.py`) routes through a shared `_durable_io.py` helper (or, for telemetry, a per-date persistent-fd restructure with fsync on rotation/close). A CI lint gate prevents new raw writes. Crash-injection integration tests prove the call sequence on a disk-backed filesystem. A new ADR-06 records the durability invariant.

---

## Scope

### In Scope
- Shared helper module `archon_search/_durable_io.py` with `atomic_write_json` and `atomic_write_bytes`.
- Migration of all six durable-write sites listed in the brief.
- Narrow `except OSError → {"detail": "internal error"}` in routes that drive `JobStore` writes (`routes_jobs.py`, `routes_collections.py`).
- Telemetry `_append` restructure (persistent per-date fd; fsync+close on date change and on `drain_and_stop`).
- CI lint gate `tests/test_no_raw_durable_writes.py`.
- Crash-injection integration tests (atomic JSON, atomic bytes, telemetry rotation).
- CI workflow update: integration job with `--basetemp=/var/tmp/archon-search-it`, runs on every PR.
- ADR-06 "Durable state writes via fsync".
- Updates to `Documentation/Architecture/130_data_architecture_and_persistence.md` and `140_error_handling_strategy.md`.

### Out of Scope
- `config.py::save_config` (follow-up brief — both atomicity and fsync bugs, needs tomlkit round-trip handling).
- Transient install/CLI writes (`install.py`, `cli/install_cmd.py`, `cli/config_cmd.py`, `cli/collection.py`, `platform/linux.py`, `platform/macos.py`) — one-shot, user-initiated, user notices and retries on crash.
- LanceDB durability (delegated to LanceDB).
- Per-append telemetry fsync (explicitly rejected).
- Windows behavior (not a supported runtime; `platform/windows.py` is a `NotImplementedError` stub).
- Backup / replication / multi-machine durability.

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 4.4 — Final verification & documentation update].

---

## What does NOT change
- Public REST/MCP API contract (no new endpoints, no schema changes).
- Telemetry's best-effort failure semantics — `OSError` is still swallowed in `TelemetryWriter._run`.
- `_safe_state_update`'s broad `except Exception` swallow in `sync.py` — preserved on purpose.
- `manifest_remove_entry`'s local `except (json.JSONDecodeError, OSError): pass` — preserved on migration.
- Project-wide `--cov-fail-under=85` threshold.
- `BREAKING.md` — no breaking changes.

---

## Known limitations / accepted trade-offs
- Crash-injection tests use `os._exit()`, which kills the process but not the kernel; tests verify the **call sequence** on disk-backed FS, not power-loss durability. The kernel page cache is left intact by `os._exit()`, so these tests prove call-sequence correctness (fsync was invoked in the right order) but NOT actual durability against device power loss.
- **macOS `F_FULLFSYNC` is NOT used.** `os.fsync()` on Darwin only flushes the kernel buffer cache; the SSD's internal write cache may not be drained on power loss. The "survives power loss" claim is therefore conditional on the device having power-loss protection (most modern consumer SSDs do via on-board capacitors, but this is not guaranteed). True barrier semantics on macOS would require `fcntl(fd, fcntl.F_FULLFSYNC)`, at a meaningful latency cost (commonly 10× a plain `fsync`). Deferred to a future hardening pass if real-world data loss is observed on the macOS primary target.
- Telemetry loses up to a kernel writeback window of lines on crash (Linux: ~5–30s of unwritten data). Accepted: telemetry is best-effort.
- CI lint gate is a textual tripwire (regex over patterns like `os.replace(`, `\.write_text(`, `shutil.move(`); a sufficiently creative bypass slips past it. Reviewers must inspect any `# noqa: durable-write` line.
- Windows is not exercised. The helper may degrade silently there; we don't pretend Windows runs at all today.
- Per-ingest cost is ~200 fsyncs / ~10s wall in the 10k-file worst case. Acceptable; revisit on user-reported regression.

### Lock interaction with A6's `IndexingStateStore._lock`
`IndexingStateStore.write()` is called under A6's `threading.RLock`. Each fsync call inside it (~5–50ms on SSD, longer on HDD; longer still if A7 is later upgraded to `F_FULLFSYNC` on Darwin) blocks any concurrent reader holding or waiting on the same lock. This is accepted because:
- (a) State writes are low-frequency — every ~50 ingested files per `_safe_state_update`, not per file.
- (b) The lock is fine-grained (per-store), so it does not contend with unrelated subsystems.
- (c) Readers are not on the latency-critical search hot path — search reads the LanceDB index, not `IndexingStateStore`.

If profiling later shows reader starvation under sustained ingest, options in increasing order of complexity are:
- Split read/write into separate locks (RWLock) so readers don't queue behind a writer that is fsync-blocked.
- Batch writes — raise the `_safe_state_update` cadence above every 50 files.
- Move the fsync outside the lock with a sequence-number reconciliation pass on read.

---

## Architecture

### New module: `archon_search/_durable_io.py`

Leading underscore matches existing project convention (`_diagnostics.py`, `_types.py`). Public surface within the package; not part of any user-facing API.

```python
def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write `data` as JSON to `path` with durability.

    Sequence: write to path.tmp → flush → os.fsync(file_fd) → os.replace(tmp, path)
    → os.fsync(parent_dir_fd).

    Raises OSError on any underlying I/O failure. On fsync/replace failure the
    temp file is unlinked before re-raising (POSIX fsyncgate: do NOT retry fsync).

    Concurrency precondition: callers must serialize writes to the same path.
    The helper is not internally synchronized.
    """

def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write `data` to `path` with durability and a creation-time mode.

    Sequence: os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, mode) → os.write → os.fsync(fd)
    → os.close → os.replace → os.fsync(parent_dir_fd).

    Mode is set at file creation (no chmod-after window). On EEXIST the helper
    raises FileExistsError without retry — the O_EXCL collision is signal, not noise.

    Raises OSError on any underlying I/O failure. Temp unlinked on failure path.
    """
```

### Telemetry restructure

`TelemetryWriter` gains a per-instance persistent file handle keyed by current UTC date. `_append` opens the fd lazily; on the next call observing a different date it calls `os.fsync(old_fd) + os.close(old_fd)` then opens the new fd. `drain_and_stop` calls `os.fsync(fd) + os.close(fd)` after `queue.join`. Per-line fsync remains rejected.

### Error mapping

- `_safe_state_update`-routed writes (`progress`, sync `_write_manifest`): swallowed by existing `except Exception` in `sync.py:_safe_state_update`.
- `manifest_remove_entry`: swallowed by its own local `except (json.JSONDecodeError, OSError): pass`.
- `key_manager._generate_and_write` (startup): propagates; crashes startup — operator must intervene.
- `jobs/store._write_atomic` (route-driven): propagates. New narrow `except OSError` in route handlers returns `JSONResponse({"detail": "internal error"}, status_code=500)` (matching the existing rollback envelope from `routes_collections.py:155`).
- `TelemetryWriter._run`: existing `except (OSError, ValueError)` swallow preserved.

### CI workflow change

`.github/workflows/archon-search-pr.yml` gains an integration STEP (not a separate job — coverage `--cov-append` only merges within one process/runner) inside the existing `eval-gate` job:
```
uv run pytest --basetemp=/var/tmp/archon-search-it -o addopts= --strict-markers --strict-config --cov=archon_search --cov-append -m integration tests/
```
Runs on every PR. The `--basetemp` flag is non-negotiable — GitHub Actions' default `/tmp` is tmpfs and the crash-injection tests would silently skip. The step is positioned after the eval step and before the `coverage report --fail-under=85` step (line 46) so coverage from all three pytest runs merges into one `.coverage` file.

### New config keys / env vars
None.

---

## Task breakdown

### Phase 1 — Helper module & ADR (PR 1)
> **Releasable**: after Task 1.5. PR 1 ships the helper module, the ADR, and the CI integration job — all callable from tests but with no production migration yet. Independently revertable.

#### Task 1.1 — CI: add integration job with disk-backed basetemp
- [x] **File**: `.github/workflows/archon-search-pr.yml`
- **Depends on**: nothing
- **Description**:
  - Add a new STEP (not a separate job — coverage `--cov-append` only merges within one runner) in the existing `eval-gate` job, positioned AFTER the eval step (`archon-search-pr.yml:39`) and BEFORE the `coverage report --fail-under=85` step (`archon-search-pr.yml:46`).
  - Command: `uv run pytest --basetemp=/var/tmp/archon-search-it -o addopts= --strict-markers --strict-config --cov=archon_search --cov-append -m integration tests/`.
  - Create the basetemp directory in a prior step (same job): `mkdir -p /var/tmp/archon-search-it`.
  - Must run on every pull-request event (no `paths:` filter that could skip it).
  - Mandate: must be a step, not a job. Do not split into a separate `integration` job — the merged `.coverage` SQLite file is per-runner.
- **Releasable**: after this task, CI runs the `integration` marker on every PR with a disk-backed temp; no integration tests exist yet so the step is a no-op until later tasks.
- **Tests (TDD)** — N/A for CI YAML; verified by the workflow running on the PR that lands it.
- **Checkpoint**: `gh workflow view archon-search-pr.yml` after push; confirm the integration step appears.

#### Task 1.2 — `_durable_io.atomic_write_json`
- [x] **File**: `archon_search/_durable_io.py`
- **Depends on**: nothing
- **Description**:
  - Implement `atomic_write_json(path: Path, data: Any) -> None` exactly as the brief specifies.
  - Sequence: open `path.with_suffix(path.suffix + ".tmp")` for write, `json.dump(data, fh)`, `fh.flush()`, `os.fsync(fh.fileno())`, close, `os.replace(tmp, path)`, then `dir_fd = os.open(path.parent, os.O_RDONLY)` / `os.fsync(dir_fd)` / `os.close(dir_fd)`.
  - Wrap the body in `try/except OSError` only to unlink `tmp` on failure, then re-raise unchanged. Do **not** retry fsync.
  - On Linux only: detect the parent dir fd open path; on other POSIX it's the same call. Do not branch on `sys.platform`.
  - Symlinked parent: use `path.resolve().parent` for the directory fd; documented behavior — the test covers the common (non-symlinked) case.
  - Add module docstring naming the concurrency precondition: "Callers must serialize writes to the same path. The helper is not internally synchronized."
- **Releasable**: after this task, `atomic_write_json` is importable and unit-tested in isolation. No production caller yet.
- **Tests (TDD)** — `tests/test_durable_io.py`:
  - Unit: `test_atomic_write_json_writes_data` — round-trip JSON survives.
  - Unit: `test_atomic_write_json_fsync_call_sequence` — patch `os.fsync` and `os.replace`; assert call order is fsync(file_fd) → replace → fsync(dir_fd).
  - Unit: `test_atomic_write_json_unlinks_tmp_on_fsync_failure` — patch `os.fsync` to raise `OSError(errno.EIO, "io")` on the file fd; assert tmp is unlinked and `OSError` propagates; assert `os.replace` was NOT called.
  - Unit: `test_atomic_write_json_unlinks_tmp_on_replace_failure` — patch `os.replace` to raise; assert tmp unlinked, `OSError` propagates.
  - Unit: `test_atomic_write_json_does_not_retry_fsync` — patch `os.fsync` to raise; assert it is called exactly once.
  - Unit: `test_atomic_write_json_overwrites_existing` — pre-create target file with old content; verify new content present after call.
  - Checkpoint: `uv run pytest tests/test_durable_io.py::TestAtomicWriteJson -v`

#### Task 1.3 — `_durable_io.atomic_write_bytes`
- [x] **File**: `archon_search/_durable_io.py`
- **Depends on**: Task 1.2 (same module)
- **Description**:
  - Implement `atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None`.
  - Use `os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)` to create the temp file with mode-at-creation (no chmod-after window).
  - On `FileExistsError` from `os.open`: do NOT retry inside the helper. Raise. (`key_manager._generate_and_write` already has its own retry/load-existing semantics around this; preserve that behavior at the call site, not in the helper.)
  - Use `os.write(fd, data)`, then `os.fsync(fd)`, then `os.close(fd)`. Then `os.replace` + parent dir fsync, identical to `atomic_write_json`.
  - On any failure after `os.open` succeeds: close fd if still open, unlink tmp, re-raise.
- **Releasable**: after this task, `atomic_write_bytes` is importable and unit-tested.
- **Tests (TDD)** — `tests/test_durable_io.py`:
  - Unit: `test_atomic_write_bytes_writes_data` — round-trip.
  - Unit: `test_atomic_write_bytes_mode_is_0600` — assert `stat.S_IMODE(path.stat().st_mode) == 0o600` after write (skip on Windows via `sys.platform`).
  - Unit: `test_atomic_write_bytes_mode_is_set_at_creation` — patch `os.chmod`; assert it is NOT called.
  - Unit: `test_atomic_write_bytes_raises_on_existing_tmp` — pre-create tmp; assert `FileExistsError`.
  - Unit: `test_atomic_write_bytes_fsync_call_sequence` — same shape as the JSON test.
  - Unit: `test_atomic_write_bytes_unlinks_tmp_on_fsync_failure`.
  - Unit: `test_atomic_write_bytes_custom_mode` — pass `mode=0o644`, assert resulting mode (POSIX only).
  - Checkpoint: `uv run pytest tests/test_durable_io.py::TestAtomicWriteBytes -v`

#### Task 1.4 — Helper 100% coverage CI gate
- [x] **File**: `.github/workflows/archon-search-pr.yml`
- **Depends on**: Task 1.2, Task 1.3
- **Description**:
  - Add a step: `uv run coverage report --fail-under=100 --include=archon_search/_durable_io.py`.
  - Position: AFTER the project-wide `coverage report --fail-under=85` step (so both run on the same merged `.coverage` file written by the default + eval + integration pytest steps).
  - Step name: "Verify 100% coverage of _durable_io.py". Fails the job if any line/branch is missing.
  - The project-wide `--cov-fail-under=85` in `pyproject.toml` is unchanged.
- **Releasable**: after this task, `_durable_io.py` is gated at 100% coverage.
- **Tests (TDD)** — N/A (CI gate).
- **Checkpoint**: PR CI run; confirm the new step appears and passes.

#### Task 1.5 — ADR-06 "Durable state writes via fsync"
- [x] **File**: `Documentation/ADRs/06_durable_state_writes_via_fsync.md`
- **Depends on**: nothing (ADR records the design decision and can land before or alongside implementation)
- **Description**:
  - Follow the existing ADR template (read ADR-05 for the shape: Status / Context / Decision / Consequences).
  - Status: Accepted.
  - Context: the durability gap across the four atomic-rename sites and two raw-write sites; the kernel-page-cache reality behind `os.replace`.
  - Decision: shared `_durable_io.py` helper with fsync(file) + replace + fsync(parent dir); always-fsync, no opt-out kwarg; per-call-site error propagation as specified in the brief; rotate-only fsync for telemetry via persistent per-date fd.
  - **Rejected alternatives** (must be enumerated in the ADR): SQLite for state (rejected: pulls a dependency, overkill for small JSON blobs, durability semantics still require PRAGMA tuning); WAL-style append-only log with periodic checkpoint (rejected: complicates recovery, no caller needs replay semantics); per-write fsync on telemetry hot path (rejected: explicit cost vs best-effort contract).
  - Consequences: bounded per-ingest cost (~200 fsyncs / ~10s wall); telemetry loses up to one kernel writeback window on crash; Windows not exercised; CI integration step required.
  - Cross-link to the brief and to the architecture docs touched in Phase 4.
- **Releasable**: after this task, the durability invariant is on the record.
- **Tests (TDD)** — N/A (documentation).
- **Checkpoint**: `ls Documentation/ADRs/06_durable_state_writes_via_fsync.md`.

---

### Phase 2 — Migrate write sites + lint gate + crash tests for helper (PR 2)
> **Releasable**: after Task 2.10. PR 2 migrates the five non-telemetry write sites, adds the route-level OSError handler, lands the lint gate (with telemetry carve-out), and ships crash-injection tests for atomic JSON and atomic bytes modes. Independently revertable.

#### Task 2.1 — Migrate `progress.py::IndexingStateStore.write`
- [x] **File**: `archon_search/progress.py`
- **Depends on**: Task 1.2
- **Description**:
  - Replace the current tmp+`os.replace` block in `IndexingStateStore.write` with a single call to `archon_search._durable_io.atomic_write_json(self._path, payload)`.
  - Remove the now-unused local tmp/replace code.
  - Behavior change: OSError now propagates from the helper instead of being raised by `os.replace` directly. Caller `_safe_state_update` still swallows; verify by inspection.
- **Releasable**: progress writes are now durable.
- **Tests (TDD)** — `tests/test_progress.py`:
  - Update `test_write_atomic_uses_tmp` (line ~282): patch `archon_search.progress.atomic_write_json` and assert it is called with `self._path` and the expected payload dict.
  - Update `test_write_os_replace_raises_unlinks_tmp_and_reraises` (line ~912): change to `test_write_helper_raises_propagates` — patch helper to raise `OSError`; assert it propagates from `IndexingStateStore.write` (caller's swallow is tested separately).
  - Add: `test_safe_state_update_swallows_oserror_from_helper` — exercise the `_safe_state_update` wrapper with a helper-raising mock; assert no exception escapes.
  - Checkpoint: `uv run pytest tests/test_progress.py -v`

#### Task 2.2 — Migrate `sync.py::SearchCollectionSync._write_manifest`
- [x] **File**: `archon_search/sync.py`
- **Depends on**: Task 1.2
- **Description**:
  - Replace the tmp+`os.replace` block in `_write_manifest` with `atomic_write_json(self._manifest_path, manifest_dict)`.
  - No behavior change for callers: `_safe_state_update` continues to swallow `OSError`.
- **Releasable**: sync manifest writes via `_write_manifest` are now durable.
- **Tests (TDD)** — `tests/test_sync.py`:
  - Add or update test for `_write_manifest`: patch `archon_search.sync.atomic_write_json`, assert it is called with the manifest path and the expected dict.
  - Add: `test_write_manifest_oserror_swallowed_by_safe_state_update` — assert helper-raised `OSError` is swallowed by the wrapping path.
  - Checkpoint: `uv run pytest tests/test_sync.py -k manifest -v`

#### Task 2.3 — Migrate `sync.py::manifest_remove_entry`
- [x] **File**: `archon_search/sync.py`
- **Depends on**: Task 1.2
- **Description**:
  - Rewrite `manifest_remove_entry` (currently lines 73–82): keep the up-front `if not manifest_path.exists(): return`; replace the bare `manifest_path.write_text(json.dumps(data, indent=2))` with `atomic_write_json(manifest_path, data)`.
  - Preserve the local `except (json.JSONDecodeError, OSError): pass` — `OSError` from the helper must remain swallowed. Do NOT change to `pass` only on `JSONDecodeError`.
  - Note: function has zero production callers today; test contract anchors the API. Do not delete.
- **Releasable**: manifest removals are now durable + atomic for the (currently test-only) callers.
- **Tests (TDD)** — `tests/test_sync.py`:
  - Update `test_manifest_remove_entry_removes_key` (~L835): patch `atomic_write_json`, assert it is called with the path and the post-removal dict.
  - Update `test_manifest_remove_entry_noop_if_missing` (~L847): no behavior change expected.
  - Add: `test_manifest_remove_entry_swallows_oserror` — patch helper to raise `OSError`; assert function returns normally, no exception escapes.
  - Checkpoint: `uv run pytest tests/test_sync.py -k manifest_remove -v`

#### Task 2.4 — Migrate `jobs/store.py::_write_atomic`
- [x] **File**: `archon_search/jobs/store.py`
- **Depends on**: Task 1.2
- **Description**:
  - Replace `tmp.write_text(...) + tmp.rename(...)` (lines ~120–121) with `atomic_write_json(self._path, data)`. Helper uses `os.replace` (atomic-overwrite); on POSIX equivalent to `Path.rename` for this case, fixes the missing fsync.
  - Keep the `self._evict_old()` call and the JobStatus-enum-to-string conversion that precede the write — those run before serialization.
  - Remove the now-unused local `tmp` variable.
- **Releasable**: job store writes are durable and atomic across all platforms.
- **Tests (TDD)** — `tests/test_job_store.py`:
  - Update `test_atomic_write` (line ~42): patch `atomic_write_json`, assert it is called with the store path and the list-of-dicts payload.
  - Update `test_write_atomic_failure_leaves_tmp_file` (line ~326): semantics shift — helper now unlinks the tmp on failure. Rename to `test_write_atomic_failure_unlinks_tmp_and_reraises`. Patch helper to raise; assert tmp does not exist after the call, and `OSError` propagates from `_write_atomic`.
  - Checkpoint: `uv run pytest tests/test_job_store.py -v`

#### Task 2.5 — Migrate `key_manager.py::_generate_and_write`
- [x] **File**: `archon_search/key_manager.py`
- **Depends on**: Task 1.3
- **Description**:
  - Replace the `os.open` + `os.write` + `os.replace` block (lines ~87–129) with `atomic_write_bytes(KEY_FILE, f"{ENV_VAR}={key}\n".encode(), mode=0o600)`.
  - **Preserve** the existing concurrent-bootstrap retry: the surrounding `for attempt in range(2)` loop and the `_load_from_file()` fallback on `FileExistsError` must remain — they handle two workers racing on key bootstrap. Translate the helper's `FileExistsError` (raised when tmp pre-exists) into the same retry path.
  - Remove the trailing `_chmod_600(KEY_FILE)` call — mode-at-creation in `atomic_write_bytes` (via `os.open(..., 0o600)`) plus `os.replace`'s mode-preservation make it redundant. The current `_chmod_600` only enforces file mode (no directory mode, no ownership), so dropping it removes one syscall with no functional change. Verify in the corresponding test that the resulting file mode is `0o600` after migration.
- **Releasable**: API key file writes are durable, mode-correct, and crash-safe.
- **Tests (TDD)** — `tests/test_key_manager.py`:
  - Update `test_atomic_write` (line ~179): patch `atomic_write_bytes`, assert it is called with `KEY_FILE`, the expected encoded payload, and `mode=0o600`.
  - Preserve `test_orphaned_tmp` (line ~221) and `test_os_replace_failure_cleans_up` (line ~261): retarget assertions onto the helper boundary (helper unlinks tmp; key_manager retry loop still recovers from pre-existing tmp via `_load_from_file()`).
  - Add: `test_concurrent_bootstrap_retry_still_works` — simulate FileExistsError from helper on first attempt, returning a loaded key on second; assert no RuntimeError.
  - Checkpoint: `uv run pytest tests/test_key_manager.py -v`

#### Task 2.6 — Narrow `except OSError` in JobStore-driving route handlers
- [x] **File**: `archon_search/server/routes_jobs.py`, `archon_search/server/routes_collections.py`
- **Depends on**: Task 2.4
- **Description**:
  - Wrap each of the following request-handler call sites with a narrow `try/except OSError: return JSONResponse({"detail": "internal error"}, status_code=500)`:
    - `routes_jobs.py:101` — `store.create(namespace=ns)` in `POST /ingest`.
    - `routes_jobs.py:140` — `store.transition(...)` in `DELETE /jobs/{job_id}`.
    - `routes_collections.py:158` — `store.create(namespace=ns)` in `add_collection`.
    - `routes_collections.py:318` — `store.create(namespace=ns)` in `reindex_collection`.
  - Do NOT add a global FastAPI `@app.exception_handler(OSError)` — scope is narrow per architecture decision.
  - **Explicitly excluded** (background-task path, runs after the response is sent): `routes_jobs.py:66, 71, 73, 76, 83` inside `_default_ingest_task`. These run outside the request lifecycle and cannot return a 500.
  - **Background-task hardening**: wrap the `_default_ingest_task` body's `store.update(...)` calls in `try/except OSError as exc: logger.error("background ingest write failed: %s", exc); store.update(job_id, status=JobStatus.FAILED, error=str(exc))` so background failures are at least logged and reflected in job status. If the secondary `store.update` ALSO raises, suppress and log — no other path forward.
- **Releasable**: durable-write failures from JobStore writes surface as a clean `{"detail": "internal error"}` 500 to clients.
- **Tests (TDD)** — `tests/server/test_routes_jobs.py`, `tests/server/test_routes_collections.py` (create if absent; otherwise extend existing files):
  - Patch target: install a mock store via `app.state.job_store = mock_store_raising_oserror` in a fixture that overrides the dependency. Do NOT patch `archon_search.jobs.store.JobStore.create` at the module level — the route's instance is held on `app.state` and may not pick up class-level patches consistently.
  - Integration: `test_job_create_oserror_returns_500_envelope` — patch the store on `app.state` to raise `OSError` on `create`; assert response is 500 with body `{"detail": "internal error"}`.
  - Integration: `test_collection_ingest_oserror_returns_500_envelope` — same for the `POST /collections/{name}/ingest` route.
  - Unit: `test_background_ingest_oserror_does_not_500_client` — verify the background-task path does not affect the synchronous response.
  - Unit: `test_background_ingest_oserror_logs_and_marks_failed` — patch `store.update` to raise `OSError` on first call inside `_default_ingest_task`; assert the error is logged and the job ends in `JobStatus.FAILED` with an `error` field set.
  - Unit: `test_background_ingest_double_oserror_suppressed` — patch `store.update` to raise `OSError` on every call (so both the success-path update AND the recovery `store.update(JobStatus.FAILED, ...)` raise); assert no exception escapes `_default_ingest_task`, both failures are logged, and the test completes without unhandled exception in the background task.
  - Checkpoint: `uv run pytest tests/server/test_routes_jobs.py tests/server/test_routes_collections.py -v`

#### Task 2.7 — CI lint gate `tests/test_no_raw_durable_writes.py`
- [x] **File**: `tests/test_no_raw_durable_writes.py`
- **Depends on**: Tasks 2.1–2.5 (so the gate passes on the migrated codebase)
- **Description**:
  - Pattern mirrors existing `tests/test_no_archon_imports.py` and `tests/test_no_shim_file.py` — a single pytest test function that walks files and asserts.
  - Walk `archon_search/**/*.py` excluding `archon_search/_durable_io.py`.
  - **Regex set** (single-line patterns; Black keeps these single-line in practice): `\bos\.replace\(`, `\bos\.rename\(`, `\.rename\(`, `\.write_text\(`, `\.write_bytes\(`, `\bshutil\.move\(`.
  - **AST-based detector for `os.open(..., O_CREAT, ...)`**: Black/Ruff routinely wrap long `os.open(...)` calls across lines, so regex over a single line misses them. Parse each file with `ast.parse`, walk `ast.Call` nodes whose `.func` resolves to `os.open` (an `ast.Attribute` with `value=Name("os")` and `attr="open"`), and inspect each call's args/keywords for any node referencing `O_CREAT` (an `ast.Attribute` with `attr="O_CREAT"`, typically `os.O_CREAT`, possibly OR'd inside a `BitOr`). Report the call's `lineno`.
  - Allow-list: skip any line whose trailing comment contains `# noqa: durable-write`. For AST-detected violations, look up the source line at `lineno` (or the line containing the `O_CREAT` token) and apply the same noqa check.
  - On any violation, fail with a message listing `{file}:{line}: {snippet}` for each match, plus a one-line explainer pointing to `archon_search/_durable_io.py`.
  - Add a docstring at the top of the test file: "Tripwire over textual + AST patterns; reviewers must inspect every `# noqa: durable-write` line. Known bypasses NOT detected by current rules: `open(path, 'w')` + `f.write()`, `pathlib.Path.open('w')`, `shutil.copy*`, `tempfile.NamedTemporaryFile(delete=False)` + `os.link`. Reviewers must spot these in PR review; patch the detector when an instance is discovered in the wild."
  - **Telemetry carve-out**: add `# noqa: durable-write` with a `TODO(A7 PR3): remove after _append restructure` comment on the line in `telemetry/writer.py::_append` that matches `path.open("ab")` (`.open(` is not flagged today, but if it is flagged add the noqa). Actually flag-shape check: `path.open("ab")` will NOT match the current regex set. **Re-check**: only add a noqa if a pattern actually matches `telemetry/writer.py` after Phase 2 migrations. If nothing matches, no carve-out needed and Task 3.2's "remove noqa" becomes a no-op — note this in the test's docstring.
- **Releasable**: future raw-write regressions fail CI by default.
- **Tests (TDD)** — `tests/test_no_raw_durable_writes.py` is itself the test. Sanity:
  - Self-test: include an `archon_search/_durable_io.py`-internal `os.replace(` call (legitimate) and assert it is skipped (helper exclusion works).
  - Self-test: temporarily monkeypatch the walk to a fixture directory with one violating file; assert the test fails.
  - Self-test: same fixture, but with `# noqa: durable-write`; assert the test passes.
  - Place these self-tests in a separate `tests/test_no_raw_durable_writes_self.py` to avoid recursion.
  - Checkpoint: `uv run pytest tests/test_no_raw_durable_writes.py tests/test_no_raw_durable_writes_self.py -v`

#### Task 2.8 — Crash-injection integration test: atomic JSON
- [x] **File**: `tests/integration/test_durable_io_crash.py`
- **Depends on**: Task 1.2, Task 1.1
- **Description**:
  - Marker: `@pytest.mark.integration`.
  - Helper extracted to `tests/integration/_helpers.py`: `def _tmp_is_tmpfs(path: Path) -> bool:` — on Linux, parse `/proc/self/mountinfo`, find the longest mount-point prefix matching `path.resolve()`, return `True` if fstype is `tmpfs`. On non-Linux return `False`.
  - Per-test: `pytest.skip` if `_tmp_is_tmpfs(tmp_path)` returns `True`, with reason "crash-injection requires disk-backed FS; rerun with `pytest --basetemp=/var/tmp/...`".
  - **Fault-injection mechanism**: subprocess script monkeypatches a target syscall to call `os._exit(137)` BEFORE the real syscall runs. The helper is synchronous; the only way to sit between its internal syscalls is to interpose on them. Pattern:
    ```python
    # subprocess script body
    import os, json, sys
    from pathlib import Path
    from archon_search._durable_io import atomic_write_json
    path = Path(sys.argv[1])
    # Inject the kill point BEFORE os.replace runs:
    _real_replace = os.replace
    def _kill_before_replace(*a, **kw):
        os._exit(137)
    os.replace = _kill_before_replace
    atomic_write_json(path, {"new": True})
    ```
  - Per sub-test, spell out: which syscall is monkeypatched (`os.replace` for "before replace", `os.fsync` for "before fsync"). What the parent asserts after `subprocess.run(...)` returns a nonzero exit: the target file equals the prior committed content (no replace happened); the `.tmp` file may exist and is acceptable (`os._exit` bypasses Python `finally` — do NOT assert tmp absence). For the "after replace" sub-test: do NOT monkeypatch; let the helper return, then call `os._exit(137)`. Target file must equal new content.
  - Use a sentinel "prior" JSON pre-created in the test, write a new JSON in the subprocess.
  - **Tmp lingering note**: tmp files may linger after `os._exit` mid-helper (Python's `finally` does not run). Tests must not assert tmp absence in crash sub-cases. A one-time cleanup in `IndexingStateStore.read()` / similar callers is out of scope here; tracked as a follow-up note in `Documentation/Backlog/` if needed.
  - **Unit tests for `_tmp_is_tmpfs`** in `tests/integration/test_tmpfs_detection.py` (runs inside the integration suite): `test_tmp_is_tmpfs_detects_tmpfs_mount` with a fixture mountinfo string containing a tmpfs mount → True; `test_tmp_is_tmpfs_detects_ext4_mount` with an ext4 mount → False; `test_tmp_is_tmpfs_non_linux_returns_false` for the non-Linux short-circuit.
- **Releasable**: regression test exists for the atomic-JSON call sequence on real disks.
- **Tests (TDD)** — this file IS the test.
  - Integration: `test_atomic_write_json_crash_before_replace_preserves_prior` — subprocess monkeypatches `os.replace` to `os._exit(137)`; parent asserts target file equals prior content; does NOT assert tmp absence.
  - Integration: `test_atomic_write_json_crash_before_fsync_preserves_prior` — subprocess monkeypatches `os.fsync` to `os._exit(137)` (kills before file fsync runs); parent asserts target file equals prior content.
  - Integration: `test_atomic_write_json_crash_after_replace_has_new` — subprocess does NOT monkeypatch; lets `atomic_write_json` return, then calls `os._exit(137)`; parent file equals new content.
  - Unit tests for `_tmp_is_tmpfs` per the description above (in `tests/integration/test_tmpfs_detection.py`).
  - Checkpoint: `uv run pytest --basetemp=/var/tmp/archon-search-it -m integration tests/integration/test_durable_io_crash.py -v`

#### Task 2.9 — Crash-injection integration test: atomic bytes
- [x] **File**: `tests/integration/test_durable_io_crash.py`
- **Depends on**: Task 2.8 (same file, shared tmpfs-detect helper)
- **Description**:
  - Mirror Task 2.8 for `atomic_write_bytes`, using the same monkeypatch-then-`os._exit(137)` fault-injection pattern. Use a 256-byte payload representing a fake key.
  - Additional assertion: when the new file exists after the crash, its mode is `0o600`.
  - Tmp may linger after mid-helper crash; do not assert tmp absence.
- **Releasable**: regression test for the bytes path.
- **Tests (TDD)**:
  - Integration: `test_atomic_write_bytes_crash_before_replace_preserves_prior` — monkeypatches `os.replace` → `os._exit(137)`.
  - Integration: `test_atomic_write_bytes_crash_before_fsync_preserves_prior` — monkeypatches `os.fsync` → `os._exit(137)`.
  - Integration: `test_atomic_write_bytes_crash_after_replace_has_new_with_mode_0600` — no monkeypatch; helper returns; then `os._exit(137)`.
  - Checkpoint: `uv run pytest --basetemp=/var/tmp/archon-search-it -m integration tests/integration/test_durable_io_crash.py -v`

#### Task 2.10 — Verify Phase 2 end-to-end
- [x] **File**: N/A (verification task)
- **Depends on**: Tasks 2.1–2.9
- **Description**:
  - Run the full default suite + the integration suite; both must pass with no warnings (project rule: warning-free).
  - Run the lint-gate test directly; confirm it passes against the migrated codebase.
  - Run `uv run coverage report --fail-under=100 --include=archon_search/_durable_io.py` locally; confirm 100%.
  - Confirm no regression in REST/MCP integration tests.
- **Releasable**: Phase 2 PR is ready for review.
- **Tests (TDD)**: N/A (verification).
- **Checkpoint**: `uv run pytest && uv run pytest --basetemp=/var/tmp/archon-search-it -m integration && uv run coverage report --fail-under=100 --include=archon_search/_durable_io.py`

---

### Phase 3 — Telemetry restructure (PR 3)
> **Releasable**: after Task 3.4. PR 3 restructures `TelemetryWriter._append` to use a persistent per-date fd with rotate-only fsync, removes any PR-2 telemetry carve-out, and adds the rotation crash-injection test.

#### Task 3.1 — `TelemetryWriter._append` persistent per-date fd
- [x] **File**: `archon_search/telemetry/writer.py`
- **Depends on**: nothing (independent of helper migrations)
- **Description**:
  - Add two instance attributes initialized in `__init__`: `self._fd: int | None = None`, `self._fd_date: str | None = None` (ISO date string).
  - Rewrite `_append(self, when: datetime, payload: bytes) -> None`:
    - Ensure `_log_dir` exists once (preserve `_dir_ensured`).
    - `current_date = when.date().isoformat()`.
    - If `self._fd_date != current_date`: if `self._fd is not None`: `os.fsync(self._fd); os.close(self._fd)`. Then open the new file: `self._fd = os.open(str(self._file_for(when)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)`; `self._fd_date = current_date`.
    - `os.write(self._fd, payload)`. Do NOT fsync per write.
  - Modify `drain_and_stop`: after `queue.join()` completes (or timeout), and after task cancellation, if `self._fd is not None`: `os.fsync(self._fd); os.close(self._fd); self._fd = None`.
  - Concurrency note: `_append` is only called from the single `_run` coroutine (verified in writer.py:127), so no lock needed.
  - Tag the `os.open` line with `# noqa: durable-write` (lint gate flags `os.open(... O_CREAT ...)`). Comment explains: telemetry uses rotate-only fsync per ADR-06; the persistent fd pattern is the durability contract here.
  - **Failed rotation fd lifecycle**: if `os.fsync(self._fd)` raises during rotation, in a `try/except OSError` inside `_append` do a best-effort `os.close(self._fd)` (suppressing secondary errors), set `self._fd = None`, `self._fd_date = None`, and re-raise so `_run`'s `except (OSError, ValueError)` swallows. The next `_append` call lazily reopens on the current date. The old fd is not leaked.
  - **Restart after drain**: explicitly OUT OF SCOPE — pre-existing constraint, `drain_and_stop` sets `self._stopped = True` and the current `start()` does not reset it. The new `self._fd = None` reset in `drain_and_stop` is consistent with this: if a future change ever supports restart, fd state is already correct.
- **Releasable**: telemetry writes are durable across date rotations and at shutdown.
- **Tests (TDD)** — `tests/telemetry/test_writer.py`:
  - Unit: `test_append_opens_persistent_fd_once_per_date` — append three entries on the same date; assert `os.open` called once.
  - Unit: `test_append_rotates_fd_on_date_change` — append on date A, then date B (via mocked clock); assert `os.fsync(old_fd) + os.close(old_fd)` happened before the new `os.open`.
  - Unit: `test_append_does_not_fsync_per_write` — patch `os.fsync`; append 100 entries on one date; assert `os.fsync` not called.
  - Unit: `test_drain_and_stop_fsyncs_and_closes_fd` — start writer, append, `drain_and_stop`; assert `os.fsync(fd)` + `os.close(fd)` called; assert `self._fd is None` after.
  - Unit: `test_drain_and_stop_idempotent_with_no_fd` — call `drain_and_stop` without any appends; assert no fsync, no error.
  - Unit: `test_oserror_during_rotation_swallowed` — patch `os.fsync` on rotation to raise `OSError`; assert `_run`'s `except OSError` swallows (telemetry stays best-effort).
  - Unit: `test_rotation_failure_clears_fd_state_and_recovers` — patch `os.fsync` to raise once on rotation; assert (i) the exception is swallowed by `_run`, (ii) `self._fd is None` and `self._fd_date is None` after, (iii) the next `_append` call opens a fresh fd successfully and writes its payload.
  - Unit: `test_rotation_fsyncs_before_closing_old_fd` (deterministic ordering, no subprocess) — record call order via a shared list `calls = []`; monkeypatch `os.fsync` and `os.close` to append `("fsync", fd)` / `("close", fd)` to `calls`; trigger a date rollover via the mocked clock; assert `calls.index(("fsync", old_fd)) < calls.index(("close", old_fd))`.
  - Checkpoint: `uv run pytest tests/telemetry/test_writer.py -v`

#### Task 3.2 — Remove PR-2 telemetry lint-gate carve-out (if any)
- [x] **File**: `archon_search/telemetry/writer.py`
- **Depends on**: Task 3.1
- **Description**:
  - If Task 2.7 added any `# noqa: durable-write` to telemetry code (re-check after Phase 2 ships), remove only those lines that no longer match a flagged pattern post-restructure.
  - The `os.open(... O_CREAT ...)` in the restructured `_append` still matches the heuristic and KEEPS its `# noqa: durable-write` comment (legitimately documented via ADR-06).
  - If no PR-2 carve-out existed, this task is a no-op — record that fact in the PR description and check the task off.
- **Releasable**: lint gate is in its steady state.
- **Tests (TDD)** — re-run lint gate; must still pass.
- **Checkpoint**: `uv run pytest tests/test_no_raw_durable_writes.py -v`

#### Task 3.3 — Crash-injection integration test: telemetry rotation
- [x] **File**: `tests/integration/test_telemetry_rotation_crash.py`
- **Depends on**: Task 3.1, Task 2.8 (reuses `_tmp_is_tmpfs` helper — import from `tests/integration/test_durable_io_crash.py` or extract to `tests/integration/_helpers.py` if used in 2 files)
- **Description**:
  - Marker: `@pytest.mark.integration`. tmpfs skip via the shared helper in `tests/integration/_helpers.py`.
  - **Sub-case A — rollover boundary (integration, real disk-backed FS via subprocess)**: pre-populate `<date1>.jsonl` with one entry. Subprocess script (run via `subprocess.run([sys.executable, "-c", script], env={...})`, tmp_path passed via env var): instantiate writer; append entry on date1; advance clock to date2; monkeypatch `os.close` to call `os._exit(137)` before any close runs; trigger an append on date2 (which forces the rotation path). The kill fires inside rotation AFTER the fsync of the old fd but BEFORE the close. Parent asserts: `<date1>.jsonl` contains BOTH the pre-populated entry AND the date1 entry (the rotation's fsync survived). `<date2>.jsonl` may or may not exist / may be empty (kernel write-back timing).
  - **Sub-case B — ordering regression detector (moved to UNIT test in `tests/telemetry/test_writer.py` as `test_rotation_fsyncs_before_closing_old_fd`, see Task 3.1)**: a subprocess is unnecessary for this check — the ordering of `os.fsync` vs `os.close` in the rotation path is a deterministic property verifiable by recording call order via monkeypatched syscalls. The unit test lives in Task 3.1's test list; this integration file does not need to re-cover it.
- **Releasable**: regression test for the telemetry rotation contract.
- **Tests (TDD)**:
  - Integration: `test_rotation_fsyncs_old_file_before_opening_new` (Sub-case A above).
  - (Ordering test lives in `tests/telemetry/test_writer.py::test_rotation_fsyncs_before_closing_old_fd` — see Task 3.1.)
  - Checkpoint: `uv run pytest --basetemp=/var/tmp/archon-search-it -m integration tests/integration/test_telemetry_rotation_crash.py -v`

#### Task 3.4 — Verify Phase 3 end-to-end
- [x] **File**: N/A (verification task)
- **Depends on**: Tasks 3.1–3.3
- **Description**:
  - Full default + integration suite passes warning-free.
  - Coverage gate on `_durable_io.py` still 100%.
  - Telemetry writer test coverage did not regress.
- **Releasable**: Phase 3 PR is ready for review.
- **Tests (TDD)**: N/A.
- **Checkpoint**: `uv run pytest && uv run pytest --basetemp=/var/tmp/archon-search-it -m integration`

---

### Phase 4 — Documentation (PR 4)
> **Releasable**: after Task 4.4. PR 4 documents the durability contract in the architecture docs, runs the project-wide doc sweep, and verifies gated suites locally before merge.

#### Task 4.1 — Update `130_data_architecture_and_persistence.md`
- [x] **File**: `Documentation/Architecture/130_data_architecture_and_persistence.md`
- **Depends on**: Phases 1–3 complete
- **Description**:
  - Add a new section "Durability contract" describing: every durable JSON/bytes write goes through `_durable_io.py`; fsync(file) + replace + fsync(parent dir); telemetry uses rotate-only fsync via a persistent per-date fd.
  - Cross-link to ADR-06.
  - Document the call-site error-propagation matrix (swallow for sync/progress/manifest_remove/telemetry; propagate for key_manager startup and route-driven JobStore writes).
  - Document the known limits (tmpfs caveat, kernel writeback window for telemetry, Windows unsupported).
  - Do not change unrelated sections.
- **Releasable**: architecture doc reflects the new contract.
- **Tests (TDD)**: N/A (doc).
- **Checkpoint**: visual review of the diff; cross-link to ADR-06 renders correctly.

#### Task 4.2 — Update `140_error_handling_strategy.md` with OSError→500 mapping
- [x] **File**: `Documentation/Architecture/140_error_handling_strategy.md`
- **Depends on**: Task 2.6 (the mapping must exist in code first)
- **Description**:
  - Add a row to the error-mapping table: `OSError from durable write (JobStore-driving routes)` → `JSONResponse({"detail": "internal error"}, status_code=500)`, referencing `routes_jobs.py` / `routes_collections.py` and the helper at `archon_search/_durable_io.py`.
  - Add a paragraph: "OSError from other call sites (`_safe_state_update`, `manifest_remove_entry`, telemetry) is intentionally swallowed and logged. OSError at startup from `key_manager._generate_and_write` is intentionally fatal; the operator must intervene."
  - Update the existing line 95 statement ("unmapped exceptions become a plain 500") to reference this row as a deliberate exception.
- **Releasable**: error doc reflects the new envelope mapping.
- **Tests (TDD)**: N/A.
- **Checkpoint**: visual diff review.

#### Task 4.3 — Run gated suites locally before merging
- [x] **File**: N/A (verification task)
- **Depends on**: Tasks 4.1, 4.2
- **Description**:
  - Run `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/`. Document the pass/fail and any threshold deltas in the PR description.
  - Run `uv run pytest -m benchmark` (auto-skips if no running server; record the skip in the PR description).
  - Run `uv run pytest -m live` (skip-if-no-credentials; record the skip).
  - REQUIRED before merging PR 4: paste the three outputs into the PR description.
- **Releasable**: gated suites have a documented baseline post-A7.
- **Tests (TDD)**: N/A (verification).
- **Checkpoint**: PR description contains the three command outputs.

#### Task 4.4 — Final verification & documentation update
- [x] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn a documentation-sweep agent: discover every project doc (READMEs, ADRs, API docs, architecture docs in `Documentation/**`, `CHANGELOG.md` if present, `CLAUDE.md`) and update any whose content is affected by A7 (durability invariant, helper module, ADR-06, error-mapping change, telemetry restructure). Do NOT update unrelated docs.
  - Particular attention: `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` should list `_durable_io.py`. `Documentation/Architecture/990_documentation_index_and_contribution_guide.md` should reference ADR-06.
  - Verify each acceptance criterion below.
- **Releasable**: A7 is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - `archon_search/_durable_io.py` exists, exports `atomic_write_json` and `atomic_write_bytes`, and has 100% line+branch coverage enforced in CI.
  - All six durable-write sites (`progress.IndexingStateStore.write`, `sync._write_manifest`, `sync.manifest_remove_entry`, `jobs.store._write_atomic`, `key_manager._generate_and_write`, `telemetry.writer._append`) route through `_durable_io.py` or the persistent-fd restructure; no raw `os.replace` / `os.rename` / `.write_text` / `.write_bytes` / `shutil.move` / `os.open(...O_CREAT...)` remains in `archon_search/**` outside `_durable_io.py` (except `# noqa: durable-write` lines, which must be reviewed and justified).
  - `tests/test_no_raw_durable_writes.py` passes against the codebase.
  - Crash-injection integration tests (atomic JSON, atomic bytes, telemetry rotation Sub-case A) pass on a disk-backed FS via `pytest --basetemp=/var/tmp/archon-search-it -m integration`. Sub-case B (rotation ordering) is verified as a unit test in `tests/telemetry/test_writer.py::test_rotation_fsyncs_before_closing_old_fd` and runs in the default suite.
  - CI runs the `integration` job on every PR.
  - `routes_jobs.py` and `routes_collections.py` return `JSONResponse({"detail": "internal error"}, status_code=500)` on OSError from JobStore writes; verified by integration test.
  - `key_manager._generate_and_write` still recovers from concurrent-bootstrap races (FileExistsError → load existing → retry).
  - `_safe_state_update` and `manifest_remove_entry` still swallow OSError; verified by tests.
  - `TelemetryWriter.drain_and_stop` fsyncs and closes the persistent fd; verified by tests.
  - ADR-06 exists at `Documentation/ADRs/06_durable_state_writes_via_fsync.md` with Status: Accepted.
  - `130_data_architecture_and_persistence.md` documents the durability contract and cross-links ADR-06.
  - `140_error_handling_strategy.md` documents the OSError→500 mapping.
  - Default `uv run pytest` passes with `--cov-fail-under=85` and is warning-free.
  - Gated suites (eval, benchmark, live) were run per Task 4.3 and their outputs are pasted in the PR description; any deltas are explained.
  - `_tmp_is_tmpfs` returns True for a tmpfs mount and False for ext4/apfs — verified by unit tests in `tests/integration/test_tmpfs_detection.py` (added in Task 2.8).
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.

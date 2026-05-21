# Feature Brief: A7 — fsync Hardening for Durable State Writes

## Problem
On power loss or kernel crash, archon-search can lose or corrupt critical on-disk state — indexing progress, sync manifest, job store, and the API key file — because every "atomic" write site uses `os.replace()` (or in two places, plain `write_text`) without first calling `fsync()` on the file (or its parent directory). Returned-success writes are not actually durable.

## Goal
Every durable state write in archon-search survives a kernel crash or power loss. After an unclean shutdown, the next start finds either the previous committed state or the new committed state — never a truncated file, a missing rename, or a zero-byte temp file. Proven by per-site call-sequence unit tests and a crash-injection integration test on a real disk-backed filesystem.

## Users & Context
Operators running `archon-search` as a long-lived service on a workstation or server, who experience an unclean shutdown (kernel panic, power loss, OOM-kill, forced VM stop) mid-ingest or mid-search. Today, recovery can mean re-indexing a corpus because the indexing-state JSON or the sync manifest is lost or stale. The user is in a "did I just lose my work?" state and expects the index to come back consistent.

## Core Flow
1. A subsystem (indexing progress, sync, jobs, key manager) needs to persist JSON state to disk.
2. It calls a shared `atomic_write_json(path, data)` helper.
3. The helper writes to `path.tmp`, calls `flush()` + `os.fsync(file_fd)`, then `os.replace(tmp, path)`, then `os.fsync(parent_dir_fd)`.
4. On the next process start after a crash, the file on disk is guaranteed to be either the prior committed JSON or the new committed JSON — fully written, never partial.
5. Telemetry uses a separate code path: rotate-only fsync via a persistent per-date fd held by `TelemetryWriter`, fsync+close on date change and on `drain_and_stop()`.

## In Scope
- A shared helper module `archon_search/_durable_io.py` exposing:
  - `atomic_write_json(path, data)` — temp + `fsync(file)` + `os.replace` + `fsync(parent_dir)`.
  - `atomic_write_bytes(path, data, mode=0o600)` — same shape, using `os.open(..., O_WRONLY|O_CREAT|O_EXCL, mode)` so the mode is set at file creation (no chmod-after window). Preserves `key_manager`'s existing `O_EXCL` semantics on the temp file.
- Migrating all atomic-rename sites to the helper:
  - `progress.py` — `IndexingStateStore.write` (PROG-1).
  - `sync.py` — `SearchCollectionSync._write_manifest` (SYN-1).
  - `sync.py` — `manifest_remove_entry` (bare `write_text`, no temp+rename today; full migration required, not just an fsync addition). Note: this function has zero production callers today (only test callers); migrate-in-place anyway — the test contract anchors the API and the function is part of the public-ish sync surface. Do not delete.
  - `jobs/store.py::_write_atomic` — currently uses `Path.rename` (raises on Windows if target exists) AND lacks fsync; helper fixes both.
  - `key_manager.py::_generate_and_write` — currently raw `os.write` + `os.replace`, no fsync.
- Telemetry writer (`telemetry/writer.py`, TEL-2) — **design change, not a one-line addition**: `_append` currently opens and closes the file handle per call (lines 151-157), so there is no fd to fsync on rotation. Restructure `_append` to hold a persistent fd keyed by current UTC date; on date change, `fsync(old_fd) + close(old_fd) + open(new_fd)`; on `drain_and_stop()`, `fsync + close`. Per-line fsync remains rejected.
- ADR-06 "Durable state writes via fsync" — added in this brief's PR. A cross-cutting durability invariant deserves an ADR; the existing ADRs cover library choices, not invariants.
- Unit tests per write site asserting the helper is called (calling-convention layer).
- A CI lint gate as a Python test under `tests/test_no_raw_durable_writes.py` (mirroring existing precedent `tests/test_no_archon_imports.py`, `tests/test_no_shim_file.py`) — runs in the default pytest suite, no new tool. Scans `archon_search/**/*.py` excluding `archon_search/_durable_io.py`. Flags: `os.replace(`, `os.rename(`, `\.rename(` on Path-like, `\.write_text(`, `\.write_bytes(`, `shutil.move(`, and any `os.open(` containing `O_CREAT` in non-helper code (heuristic). Allow-list mechanism: trailing `# noqa: durable-write` on the offending line. Known gap: this is a tripwire over textual patterns, not a proof — it catches the common bypass shapes and lets reviewers see deliberate exceptions in diff.
- One crash-injection integration test per durability mode (atomic JSON, atomic bytes, telemetry rotation) using `os._exit()` in a subprocess mid-write. Verifies the **call sequence** (flush → fsync(file) → replace → fsync(dir)) survives an abrupt process kill on a real filesystem. Marked `integration`. CI must run the `integration` job on every PR. Telemetry rotation mode has two sub-cases under one test: (a) crash *between* `fsync(old_fd)` and the first write to the new fd (rollover boundary — committed lines must be on disk); (b) crash *between* `close(old_fd)` and `fsync(old_fd)` to detect ordering regressions (this case must fail if a future refactor reorders close-before-fsync — that is the bug the restructure prevents). tmpfs detection: on Linux, parse `/proc/self/mountinfo` for the mount covering `tmp_path` and skip with a recorded reason if fstype is `tmpfs`; on macOS skip-check is unnecessary (APFS-backed). CI must invoke pytest with `--basetemp=/var/tmp/archon-search-it` (or any disk-backed workspace path) for the `integration` job — GitHub Actions' default `/tmp` is tmpfs and would silently skip every run.
- Migration of existing tests:
  - `tests/test_progress.py::test_write_atomic_uses_tmp` — update to assert helper call.
  - `tests/test_progress.py::test_write_os_replace_raises_unlinks_tmp_and_reraises` — extend to fsync failures.
  - `tests/test_job_store.py::test_atomic_write` — update to assert helper call.
  - `tests/test_job_store.py::test_write_atomic_failure_leaves_tmp_file` — semantics shift: helper deletes temp on failure (POSIX fsyncgate; do not retry fsync). Update the test to assert temp is removed.
  - `tests/test_key_manager.py::test_atomic_write`, `test_orphaned_tmp`, `test_os_replace_failure_cleans_up` — update / preserve as appropriate.
  - `tests/test_sync.py::test_manifest_remove_entry_removes_key` (~L835) and `test_manifest_remove_entry_noop_if_missing` (~L847) — update to assert helper call. `manifest_remove_entry` has its own local `except (json.JSONDecodeError, OSError): pass`; preserve that swallow on migration (do NOT let OSError from the helper escape — that would be a behavior change for the only test caller). Add coverage for `_write_manifest` if absent — assert helper call and that `_safe_state_update`-wrapped callers continue to swallow OSError.
- 100% line+branch coverage for `_durable_io.py` enforced in CI via a dedicated `coverage report --fail-under=100 --include=archon_search/_durable_io.py` step (project-wide 85% gate is unchanged).
- Update `Documentation/Architecture/130_data_architecture_and_persistence.md` with the new durability contract. Update `140_error_handling_strategy.md` to **add** an explicit OSError → HTTP 500 mapping for durable-write failures from route-handler code paths (currently no such mapping exists — line 95 documents that unmapped exceptions become a plain 500 via FastAPI's default; the `{"detail": "internal error"}` envelope today only appears for the specific rollback case in `routes_collections.py`). Implementation: add a narrow `except OSError` in the affected route(s) returning the same `{"detail": "internal error"}` envelope. Doc update describes the new mapping; it does not merely cite an existing one.

## Out of Scope
- `config.py::save_config` — has both bugs (non-atomic `path.write_text`, no fsync), excluded because writes are user-initiated (CLI), low-frequency, and the fix requires tomlkit round-trip handling. Tracked as a follow-up brief.
- Transient install/CLI writes (`install.py:167`, `cli/install_cmd.py:90`, `cli/config_cmd.py:132`, `cli/collection.py:77,163`, `platform/linux.py:103`, `platform/macos.py:86`) — one-shot, user-initiated, and the user will notice and retry on crash. Not state in the durability-critical sense. Document the decision; do not migrate.
- LanceDB durability — delegated to LanceDB; we do not write its files directly.
- Per-append telemetry fsync — explicitly rejected; telemetry is best-effort by design.
- Backup, replication, or multi-machine durability — out of project scope per `130_data_architecture`.

## Key Decisions
- **One shared helper, not per-site fsync sprinkles**: durability becomes a property of the codebase, tested once, used everywhere. Eliminates copy-paste drift across five write sites.
- **Fix all atomic-rename sites at once**: PROG-1, TEL-2, SYN-1 are the named roadmap items (`Documentation/Backlog/03_world_class_roadmap.md`); `jobs/store.py`, `key_manager.py`, and `manifest_remove_entry` carry the same bug class. Fixing them in one PR avoids a follow-up ticket each.
- **Telemetry rotation requires structural change**: today `_append` opens-and-closes per write. The brief commits to restructuring `TelemetryWriter` to hold a persistent per-date fd. This is non-trivial; estimate it accordingly.
- **Helper raises `OSError` on fsync failure; does not retry**: POSIX fsyncgate — on EIO the kernel may have already marked the page clean, so a retry is unsafe. Helper unlinks the temp file and re-raises. Propagation differs per call site and is deliberate:
  - **`_safe_state_update`-routed writes** (sync `_write_manifest`, progress writes invoked through sync): `sync.py::_safe_state_update` (lines ~302-309) catches `Exception` and logs. OSError stays swallowed — preserved on purpose, because partial state is recoverable on the next sync pass.
  - **`manifest_remove_entry`**: has its own local `except (json.JSONDecodeError, OSError): pass`; behavior matches `_safe_state_update` paths (swallow + log-or-pass). Preserved on migration.
  - **Startup writes** (`key_manager._generate_and_write`): OSError propagates and crashes startup. Desired — the operator must intervene before the server can serve requests with an unwritten key file.
  - **Job-store writes from route handlers** (`jobs/store.py`): OSError propagates out of the route. There is no global OSError handler today (`140_error_handling_strategy.md` line 95: unmapped exceptions become a plain 500 via FastAPI default). The doc-update task adds an explicit `except OSError → {"detail": "internal error"}` mapping in the affected route(s) so durable-write failures surface as the same envelope used elsewhere for internal errors.
  - **Telemetry**: existing `swallows_oserror` behavior preserved (best-effort by design).
- **Crash-injection test verifies call sequence on a real filesystem, not power-loss durability**: `os._exit()` only kills the process; kernel page cache and writeback survive. The test proves the helper performs the right syscalls in the right order on a disk-backed FS. Tests must skip if `tmp_path` resolves under tmpfs.
- **ADR-06 lands with the work, not later**: a cross-cutting invariant ("every durable state write survives kernel crash") is architectural in nature.
- **Always-fsync, no opt-out kwarg**: simpler contract. Revisit only if measured test-suite slowdown exceeds 5%.
- **Windows is not a supported runtime**: `platform/windows.py` is a stub raising `NotImplementedError` everywhere. The helper's behavior on Windows is best-effort and not exercised by tests. Drop the "we don't pretend Windows is durable" framing — we don't pretend Windows runs at all.
- **Expected delivery: 3–4 sequenced PRs, each independently revertable**:
  1. Helper module (`_durable_io.py`) + ADR-06 + helper unit tests with 100% coverage gate.
  2. Migrate `progress.py`, `sync.py` (`_write_manifest` and `manifest_remove_entry`), `jobs/store.py`, `key_manager.py` to the helper; update existing tests in `test_progress.py` / `test_sync.py` / `test_job_store.py` / `test_key_manager.py`; land the lint gate. **PR-2 lint-gate carve-out**: telemetry's still-raw `_append` (the `os.open(...O_CREAT...)` heuristic flags it) gets a single `# noqa: durable-write` line with a TODO referencing PR 3. PR 3 removes the carve-out as part of the restructure — landing the lint gate without it would either fail PR 2's CI or silently exempt telemetry forever.
  3. Telemetry `_append` restructure (persistent per-date fd) + rotation crash-injection test; remove the PR-2 `# noqa: durable-write` carve-out.
  4. Doc updates (`130_data_architecture_and_persistence.md`, `140_error_handling_strategy.md` with the new OSError → 500 mapping).

## Edge Cases & Constraints
- **EIO / ENOSPC during fsync**: helper unlinks the temp file and re-raises `OSError`. Propagation is call-site-specific (see Key Decisions): swallowed by `_safe_state_update` for sync/progress writes; crashes startup for `key_manager`; reaches the route layer for job-store writes, where the doc-update task adds a narrow `except OSError → {"detail": "internal error"}` mapping (no such mapping exists today). Telemetry swallows the error per existing best-effort policy.
- **Caller concurrency**: helper is **not** internally synchronized. Two concurrent calls against the same path race in the temp→replace→dir-fsync window. Callers must serialize (e.g., `progress.py`'s reliance on `SearchCollectionSync._collection_locks`, `key_manager`'s `O_EXCL` retry). This precondition is documented in the helper's docstring.
- **Telemetry data-loss window**: without per-line fsync, ALL telemetry lines within the kernel writeback window (Linux default ~5s via `dirty_writeback_centisecs`, up to ~30s under load) are at risk on crash. Accepted: telemetry is best-effort by design.
- **Performance — per-ingest cost is bounded**: `_safe_state_update` is called every 50 files during ingest. For a 10k-file corpus, that is ~200 fsyncs (~5–50ms each on a disk-backed FS, ~10s wall in the worst case). Acceptable; revisit only on user-reported regression.
- **Test-suite cost on macOS**: `os.fsync` is notably slower on macOS than Linux. If always-fsync inflates suite wall time by >5%, add a `fsync: bool = True` kwarg. Decision deferred to first measurement.
- **Crash-injection tests must run on disk-backed FS**: detection on Linux parses `/proc/self/mountinfo` for the mount covering `tmp_path` and skips with a recorded reason if fstype is `tmpfs`; macOS needs no check (APFS-backed). CI must pass `--basetemp=/var/tmp/archon-search-it` (or another disk-backed workspace path) for the `integration` job — GitHub Actions' default `/tmp` is tmpfs and would silently skip every run, turning the guarantee decorative.
- **Mode-at-creation for the key file**: helper uses `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` (matching today's `_generate_and_write`). No `write_bytes` + `chmod` — that opens a world-readable window between create and chmod.
- **Symlinked parent path**: if the target's parent is a symlink, fsync the resolved directory fd, not the link. Document; tests cover the common case only.
- **Coverage**: 100% line+branch coverage for `_durable_io.py`. Project-wide 85% threshold otherwise unchanged.

## Open Questions
- (None currently — lint-gate location decided: pytest-suite Python test per existing project precedent.)

## Future Iterations
- `config.py` atomic-write fix (separate brief).
- Optional `telemetry.fsync_mode` config knob if a user ever asks for it — not now.
- WAL-style durability for indexing progress if re-sync becomes too expensive at scale.
- Re-evaluate Windows support holistically; only then revisit Windows fsync semantics.

## Recommendation
Build it. The fsync helper itself is small; the work is in the telemetry restructure, the per-site migration, the lint gate, and the call-sequence + crash-injection tests. The hardest discipline is resisting the urge to fsync telemetry per line. Hold the line on the parent-directory fsync, the ADR, the lint gate, and the integration test — without those, this is decorative.

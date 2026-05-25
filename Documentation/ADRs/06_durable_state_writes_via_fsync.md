# 06. Durable State Writes via fsync

**Status**: Accepted
**Date**: 2026-05-24
**Deciders**: archon-search maintainers

## Context

`archon-search` persists critical runtime state as small JSON and bytes files
under `~/.archon-search/`: indexing progress (`progress.py`), the sync manifest
(`sync.py`), the job store (`jobs/store.py`), and the API key
(`key_manager.py`). Every "atomic" write site relied on the temp-file +
`os.replace()` pattern, and two sites — `sync.manifest_remove_entry` and
`telemetry/writer._append` — skipped even that and wrote in place.

`os.replace()` is atomic at the *rename* layer, but it offers **no durability
guarantee**. When the call returns, the file's data, the rename itself, and the
parent directory's updated dirent can all still sit in dirty kernel page-cache
pages. The kernel reports success the moment the change is visible in the page
cache; it has not necessarily reached stable storage. On power loss or a kernel
crash before writeback flushes those pages, a write that *appeared* to succeed
can come back as the prior content, a missing rename, or a zero-byte temp file.

For a long-lived service that experiences an unclean shutdown (kernel panic,
power loss, OOM-kill, forced VM stop) mid-ingest, this means recovery can
require re-indexing a corpus because the indexing-state JSON or the sync
manifest was lost or left stale even though the process logged a successful
write. The durability gap is the same bug class across all six sites; fixing it
once, as a property of the codebase, is the goal.

## Decision

All durable state writes route through a single shared helper module,
`archon_search/_durable_io.py`, exposing two functions:

- `atomic_write_json(path, data)` — write to `path.tmp`, `flush()`,
  `os.fsync(file_fd)`, `os.replace(tmp, path)`, then `os.fsync(parent_dir_fd)`.
- `atomic_write_bytes(path, data, mode=0o600)` — same sequence, but the temp
  file is created with `os.open(..., O_WRONLY | O_CREAT | O_EXCL, mode)` so the
  mode is set at creation time (no chmod-after window). This preserves
  `key_manager`'s existing `O_EXCL` semantics.

Key properties of the decision:

- **fsync the file *and* the parent directory.** The parent-directory fsync is
  what makes the rename itself durable; without it, a fully-fsynced file can
  still be lost because its dirent never reached disk.
- **Always-fsync, no opt-out kwarg.** A simpler contract. Revisited only if a
  measured test-suite slowdown exceeds 5%.
- **Helper raises `OSError` on fsync failure and does not retry.** On EIO the
  kernel may have already marked the page clean (POSIX "fsyncgate"), so a retry
  is unsafe. On a file-fsync or `os.replace` failure the helper unlinks the temp
  file and re-raises unchanged. A failure of the *parent-directory* fsync happens
  after `os.replace` has already committed the data, so there is no temp file to
  unlink; the `OSError` is re-raised to signal that the data is written but the
  rename's durability is unconfirmed.
- **The helper is not internally synchronized.** Callers must serialize writes
  to the same path. This precondition is stated in the helper's docstring.
- **Per-call-site error propagation is deliberate**, not uniform:
  - `_safe_state_update`-routed writes (`sync._write_manifest`, progress writes
    invoked through sync) — `OSError` stays swallowed by the existing
    `except Exception` in `sync.py::_safe_state_update`; partial state is
    recoverable on the next sync pass.
  - `manifest_remove_entry` — preserves its own local
    `except (json.JSONDecodeError, OSError): pass`.
  - `key_manager._generate_and_write` (startup) — `OSError` propagates and
    crashes startup; the operator must intervene before the server can serve
    requests without a key file.
  - Route-driven `jobs/store` writes — `OSError` propagates to a narrow
    `except OSError` in the JobStore-driving route handlers
    (`routes_jobs.py`, `routes_collections.py`) returning the existing
    `JSONResponse({"detail": "internal error"}, status_code=500)` envelope.
  - Telemetry — existing best-effort swallow preserved.
- **Telemetry uses rotate-only fsync, not per-line fsync.** `TelemetryWriter`
  holds a persistent file descriptor keyed by the current UTC date. `_append`
  writes without fsync; on a date change it does `fsync(old_fd) + close(old_fd)`
  before opening the new file, and `drain_and_stop()` does `fsync + close`. This
  keeps the telemetry hot path cheap while still flushing on rotation and
  shutdown.
- **A CI lint gate prevents regressions.** `tests/test_no_raw_durable_writes.py`
  scans `archon_search/**/*.py` (excluding the helper) for raw write patterns.
  Single-line patterns (`os.replace(`, `os.rename(`, `.rename(`, `.write_text(`,
  `.write_bytes(`, `shutil.move(`) are matched textually by regex; `os.open(...)`
  calls with `O_CREAT` are detected by an AST walker instead, because formatters
  routinely wrap long `os.open(...)` calls across lines where a single-line regex
  would miss them. A `# noqa: durable-write` allow-list covers documented
  exceptions.
- **Crash-injection integration tests verify the call sequence** (flush →
  fsync(file) → replace → fsync(dir)) on a disk-backed filesystem, using
  `os._exit()` in a subprocess mid-write.

## Consequences

### Positive

- Durability is a tested property of the codebase, used everywhere, rather than
  per-site fsync sprinkles that drift over time.
- After an unclean shutdown, each durable file is either the prior committed
  state or the new committed state — never truncated, never a missing rename.
- The lint gate makes new raw writes fail CI by default, so the invariant does
  not depend on reviewer vigilance.
- Mode-at-creation for the key file closes the world-readable window that a
  `write_bytes` + `chmod` sequence would open.

### Negative

- **Bounded per-ingest cost.** `_safe_state_update` runs every ~50 files; a
  10k-file corpus is ~200 fsyncs (~5-50ms each on disk), roughly ~10s of wall
  time in the worst case. Acceptable; revisited only on user-reported
  regression.
- **Telemetry can lose up to one kernel writeback window of lines on crash**
  (Linux default ~5s via `dirty_writeback_centisecs`, up to ~30s under load),
  because per-line fsync is deliberately rejected. Accepted: telemetry is
  best-effort by design.
- **macOS `F_FULLFSYNC` is NOT used.** `os.fsync()` on Darwin only flushes the
  kernel buffer cache; the SSD's internal write cache may not be drained on
  power loss. The "survives power loss" claim is therefore conditional on the
  device having power-loss protection. True barrier semantics would require
  `fcntl(fd, fcntl.F_FULLFSYNC)` at a meaningful latency cost; deferred to a
  future hardening pass if real-world data loss is observed on macOS.
- **Windows is not exercised.** `platform/windows.py` is a `NotImplementedError`
  stub; the helper's behavior there is best-effort and untested.
- **A CI integration step with a disk-backed `--basetemp` is required.** The
  crash-injection tests skip on tmpfs, and GitHub Actions' default `/tmp` is
  tmpfs, so CI must pass `--basetemp=/var/tmp/archon-search-it` (or another
  disk-backed path) or the guarantee becomes decorative. The crash tests verify
  the *call sequence* on a real filesystem, not power-loss durability — `os._exit()`
  kills the process but leaves the kernel page cache intact.

## Alternatives Considered

- **SQLite for state**: Rejected — pulls in a dependency, is overkill for small
  JSON blobs, and its durability semantics still require explicit PRAGMA tuning
  (`synchronous`, `journal_mode`) to actually fsync, so it would not remove the
  durability reasoning anyway.
- **WAL-style append-only log with periodic checkpoint**: Rejected —
  complicates recovery and no caller needs replay semantics. The state files
  are small last-writer-wins blobs, not event streams.
- **Per-write fsync on the telemetry hot path**: Rejected — the explicit
  per-line fsync cost is not justified against telemetry's best-effort contract.
  Rotate-only fsync via the persistent per-date fd is the durability boundary
  that matches the contract.

## Links

- Brief: [`Documentation/Backlog/A7-fsync-hardening-brief.md`](../Backlog/A7-fsync-hardening-brief.md)
- Plan: [`Documentation/Backlog/A7-fsync-hardening-plan.md`](../Backlog/A7-fsync-hardening-plan.md)
- The durability contract and the OSError-to-500 mapping are documented in
  [`Architecture / 130 — Data Architecture and Persistence`](../Architecture/130_data_architecture_and_persistence.md)
  and [`Architecture / 140 — Error Handling Strategy`](../Architecture/140_error_handling_strategy.md)
  (updated in A7 Phase 4).

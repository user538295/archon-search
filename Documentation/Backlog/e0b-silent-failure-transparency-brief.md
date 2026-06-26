# Feature Brief: E0b — Silent Failure Transparency

## Problem
archon-search silently degrades across six distinct failure modes with no signal to the user: HyDE/RAG Fusion times out and falls back to plain search; managed service doesn't inherit `ANTHROPIC_API_KEY` so expansion stops working after install; failed ingest jobs age out after 72 hours with no trace; oversized telemetry entries are dropped without counting; ACL sidecars over 64 KB are silently ignored; `--wait` exits after 2 minutes with no indication whether the job finished or timed out. Users see worse results, missing data, and confusing CLI output — and have no way to diagnose why.

## Goal
Every silent failure surface emits an observable signal: a response field, a CLI message, a job state, or a warning in `GET /status`. A user who hits any of these failure modes can diagnose and resolve it without reading source code.

## Users & Context
- **Operators** who enabled HyDE/RAG Fusion and notice lower search quality after running `archon-search start` (managed service).
- **Developers** running bulk ingest jobs who check `archon-search status` days later and find no record of failures.
- **End users** who ingest documents with ACL sidecars and silently lose their access control rules.
- **Any user** who runs `archon-search maintenance run --wait` or `archon-search export --wait` on a large collection and gets a timeout with no recovery path.

## Core Flow

### HyDE / RAG Fusion silent fallback (L7)
1. User sends `POST /search` with `hyde=true`.
2. Anthropic API call times out after 10 s (raised from 5 s).
3. Search falls back to non-expanded retrieval.
4. Response includes `expansion_warning: "HyDE timed out after 10s — results may be less relevant"` and `expansion_used: false`.

### ANTHROPIC_API_KEY not forwarded by managed service (L6)
1. Operator runs `archon-search start` on macOS/Linux.
2. Server starts; `ANTHROPIC_API_KEY` is not in the service environment.
3. `GET /status` returns `hyde.key_available: false` and `rag_fusion.key_available: false`.
4. CLI `archon-search status` prints a warning: "HyDE/RAG Fusion enabled in config but ANTHROPIC_API_KEY is not set. Add it to the service environment — see docs."
5. Service templates gain an `EnvironmentFile=~/.archon-search/.secrets.env` entry; the wizard creates the empty file (mode 600) when HyDE or RAG Fusion is enabled.

### Failed ingest jobs silently abandoned (L10)
1. An `IngestJob` fails 3 times and ages past 72 hours.
2. Maintenance loop transitions it to `FAILED_EXPIRED` (new terminal state) instead of silently dropping it from retry.
3. `GET /jobs?status=FAILED_EXPIRED` returns the aged-out jobs.
4. `archon-search status` includes a count: "2 ingest jobs expired without completing — re-ingest with `archon-search ingest`."

### Telemetry entries silently dropped (L11)
1. A search result set produces a serialised telemetry entry > 8 KB.
2. Instead of dropping, the entry is truncated: `result_doc_ids` list is shortened to fit.
3. Entry is written with `truncated: true` field.
4. `GET /telemetry/stats` includes `truncated_count` alongside existing stats.

### ACL sidecar silently ignored (L14)
1. User ingests a file with an `.archon-acl` sidecar > 64 KB.
2. Ingest completes but `IngestResult` includes a `warnings: ["ACL sidecar exceeds 64 KB limit — access control not applied. Reduce the sidecar file size."]` field.
3. `archon-search ingest` CLI prints the warning to stderr.

### `--wait` timeout with no recovery path (L8)
1. User runs `archon-search maintenance run --wait` on a large collection.
2. Default 120-second timeout can be overridden: `--wait --timeout 600`.
3. On expiry, CLI prints: "Still running after 600s — job ID is `<id>`. Poll with `archon-search maintenance status`." and exits 0.
4. Exit code 2 when the job is confirmed FAILED; exit 0 for timeout (still running).

## In Scope
- **L7**: Raise `HyDE.timeout_seconds` default from 5.0 → 10.0; same for `RAGFusionConfig.timeout_seconds`. Add `expansion_used: bool` and `expansion_warning: str | null` to `SearchResponse` in `schemas.py`. Populate in `pipeline.py` search path.
- **L6**: Add `EnvironmentFile=~/.archon-search/.secrets.env` to Linux (`platform/linux.py`) service template. For macOS (`platform/macos.py`), use a wrapper script that sources `.secrets.env` before exec (launchd does not support `EnvironmentFile` natively). Wizard (`install.py`) creates the empty `.secrets.env` file (mode 600) when HyDE or RAG Fusion is selected. Add `hyde.key_available: bool` and `rag_fusion.key_available: bool` to `GET /status` response. `archon-search status` CLI warns when enabled but key unavailable.
- **L10**: Add `FAILED_EXPIRED` to `JobStatus` enum in `_types.py`. Maintenance loop transitions aged-out jobs to `FAILED_EXPIRED` instead of ignoring them. `GET /jobs` supports `?status=FAILED_EXPIRED`. `archon-search status` surfaces a count.
- **L11**: Truncate `result_doc_ids` in telemetry writer when entry exceeds `MAX_ENTRY_BYTES`; set `truncated: true` on the entry. Add `truncated_count` to `GET /telemetry/stats` response.
- **L14**: Add `warnings: list[str]` to `IngestResult` (or reuse an existing result field). Populate with ACL sidecar size warning when skipped. Surface in CLI ingest output.
- **L8**: Add `--timeout SECONDS` option to `--wait` in `maintenance_cmd.py`, `export_cmd.py`, `backup_cmd.py`. On timeout, print job ID and recovery instructions; exit 0. Exit 2 only on confirmed FAILED.

## Out of Scope
- Changing the 64 KB ACL sidecar limit itself — raising the limit is a separate decision (the limit exists for memory safety; L14 only fixes the silent-ignore behaviour).
- Email/Slack notifications for job failures — future iteration.
- Structured warning taxonomy (warning codes) — plain strings are sufficient for v1.

## Key Decisions
- **`expansion_warning` is a string, not a code**: Codes require a stable enum and docs. A plain English string is actionable immediately and doesn't create a contract surface.
- **`.secrets.env` EnvironmentFile pattern over writing key to service plist**: Secrets never land in a template file tracked by launchctl. The `EnvironmentFile` approach is already the recommended pattern in the notes doc and is idiomatic for systemd.
- **`FAILED_EXPIRED` is a terminal state, not a deletion**: Preserves auditability. Operators can query and re-ingest. Keeps the job store honest.
- **Truncate telemetry rather than split**: Splitting a single search event across multiple JSONL lines breaks the reader and stats aggregation. Truncation preserves the event's identity.

## Edge Cases & Constraints
- **`expansion_warning` when both HyDE and RAG Fusion fail**: Only one can be active (they are mutually exclusive); single warning field is sufficient.
- **`FAILED_EXPIRED` transition race**: The maintenance loop already holds a lock per collection during retry evaluation; the state transition is atomic within that lock.
- **`.secrets.env` file absent after upgrade**: systemd must use `EnvironmentFile=-~/.archon-search/.secrets.env` (leading `-` = optional). The macOS wrapper script must guard with `[ -f .secrets.env ] && source .secrets.env` so a missing file is a no-op, not a startup failure.
- **Telemetry `truncated_count` backfill**: Only entries written after this change carry `truncated: true`; historical entries don't. Stats accumulate from deploy time — documented behaviour.
- **`--wait` exit codes**: Current behaviour undocumented; making exit 2 = FAILED and exit 0 = success-or-timeout is a new contract. Document in CLI help text and `BREAKING.md` if the exit code was previously relied upon.

## Open Questions

None — all resolved.

## Future Iterations
- Push notifications (webhook / email) when `FAILED_EXPIRED` jobs accumulate — needs a notification channel abstraction.
- Structured warning codes on `IngestResult` for programmatic handling by SDK users.
- `GET /status` health signal that aggregates all silent-failure states into a single `warnings: list[str]` top-level field.

## Recommendation
This is the most important UX brief in the E0 cluster. Silent failures erode user trust faster than hard errors — at least errors are diagnosable. The `ANTHROPIC_API_KEY` issue is particularly insidious: users enable HyDE at install time, run `archon-search start`, and never know it stopped working. The launchd `EnvironmentFile` question in Open Questions must be resolved before planning starts — that decision shapes the platform code for L6.

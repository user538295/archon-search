**Purpose**: Understand the async job model and manage jobs from the CLI and REST.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-07-29 / **Next review**: 2027-07-29

# Jobs and async operations

## Principles

1. **Write operations are asynchronous.** Every command that mutates the index submits a job to the running server and returns a job ID immediately — the CLI does not do the work itself (`archon_search/cli/`; the write commands are HTTP proxies).
2. **The server must be running.** Because write commands proxy to REST, they require `archon-search serve` (or `start`). On connection refused they print `archon-search serve is not running. Start it first with: archon-search serve` (`cli/_helpers.py:_SERVER_NOT_RUNNING_MSG`).
3. **Poll for completion.** The server tracks each job through a lifecycle; you check progress with `archon-search jobs` or by polling `GET /jobs/{id}`. Commands that support `--wait` do this polling for you.
4. **Jobs are namespaced.** `GET /jobs` and `GET /jobs/{id}` only ever return jobs in the caller's token namespace; a job in another namespace reads as `404` (`server/routes_jobs.py`).

## Why writes are jobs

Ingesting, reindexing, exporting, or rebuilding communities can take seconds to minutes. Rather than block the CLI (or an HTTP client) for the whole duration, the server accepts the request, creates a job, starts the work in the background, and returns `202 Accepted` with the job record. This keeps the CLI responsive, survives client disconnects, and lets several clients observe the same job.

Operations that run as jobs:

| Operation | CLI | REST route |
|---|---|---|
| Ingest a file or directory | `archon-search ingest` | `POST /ingest` |
| Add a collection | `archon-search collection add` | `POST /collections/` |
| Reindex (re-embed) a collection | `archon-search collection reindex` | `POST /collections/{name}/reindex` |
| Reindex metadata only | `archon-search collection reindex-metadata` | `POST /collections/{name}/reindex-metadata` |
| Apply a schema migration | `archon-search collection migrate` | `POST /collections/{name}/migrate` |
| Sync pinned collections | `archon-search sync` | `POST /sync` |
| Export a collection | `archon-search export` | `POST /collections/{name}/export` |
| Import a collection | `archon-search import` | `POST /collections/{name}/import` |
| Rebuild graph communities | `archon-search graph build-communities` | `POST /graph/{collection}/rebuild-communities` |
| Scheduled backups | (automatic) | `POST /backup/trigger` |

See [Ingestion and collections](50_ingestion_and_collections.md) and [Export / import](90_export_import.md) for the operation-specific flags.

## Job lifecycle

Every job carries a `status` drawn from the `JobStatus` enum (`archon_search/types.py`):

```
PENDING / QUEUED ──▶ RUNNING ──▶ DONE
                        │
                        ├─▶ FAILED ──▶ FAILED_EXPIRED
                        │
                        └─▶ CANCELLING ──▶ CANCELLED
```

- **`PENDING`** — created, work not yet started (ingest, reindex, delete jobs start here).
- **`QUEUED`** — a bulk job (export, import, migration, community rebuild, sync, metadata reindex) is waiting for a scheduler slot.
- **`RUNNING`** — the work is in progress.
- **`DONE`** — completed successfully. The `result` field holds any output (e.g. ingest `warnings`).
- **`FAILED`** — the job errored; the `error` field explains why. Some bulk jobs can be resumed (see below).
- **`FAILED_EXPIRED`** — a terminal state for `FAILED` ingest jobs that aged out (`[maintenance] retry_max_age_hours`, default 72) or exhausted retries (`retry_max_attempts`, default 3). Never re-enqueued. Surfaced in `GET /jobs?status=FAILED_EXPIRED` and in `GET /status`. See the operator [maintenance & jobs guide](../OperatorGuide/50_maintenance_and_jobs.md).
- **`CANCELLING` / `CANCELLED`** — a `DELETE /jobs/{id}` on an active job sets `CANCELLING`; the job settles to `CANCELLED` when it stops.

`DONE`, `FAILED`, `FAILED_EXPIRED`, and `CANCELLED` are terminal — the job will not change status again.

## The `jobs` CLI group

`archon-search jobs` (`cli/jobs_cmd.py`) has three subcommands. All accept `--api-url` (default `http://127.0.0.1:8765`) and `--api-key` (falls back to `ARCHON_SEARCH_API_KEY` or the key file).

### `jobs list`

Tabular listing, newest first.

```bash
archon-search jobs list
archon-search jobs list --status running --status queued   # repeatable filter
archon-search jobs list --limit 100                         # 1..200, default 50
```

Output columns: `ID` (first 8 chars), `TYPE`, `COLLECTION`, `STATUS`, `STARTED`, `ELAPSED`. When more jobs exist than were returned (`N < M`), it prints `Showing N of M jobs — use --limit to see more (max: 200).` below the table, where `N` is the number of rows above it and `M` the total across all pages.

### `jobs show <id>`

Full detail for one job — `job_id`, `job_type`, `status`, `collection`, `source`, `source_path`, timestamps, and (when present) `result`, `progress`, and `error`.

```bash
archon-search jobs show 3f2a9c1e
archon-search jobs show 3f2a9c1e --wait                     # poll until terminal
archon-search jobs show 3f2a9c1e --wait --timeout 900       # cap the wait (default 600s)
```

Without `--wait`, `show` exits `1` if the job is in `FAILED`, `FAILED_EXPIRED`, or `CANCELLED` — handy in scripts.

### `jobs status <id>`

One-shot status check (no polling); prints `job_id`, `status`, `collection`, `created_at`, and `progress`/`error` when present.

```bash
archon-search jobs status 3f2a9c1e
```

Exit codes: `0` for `DONE` and any in-progress state (`PENDING`/`QUEUED`/`RUNNING`/`CANCELLING`); `1` for `FAILED`, `FAILED_EXPIRED`, `CANCELLED`, or a `404` (job not found).

## The `--wait` flag

The write commands that support `--wait` (`ingest`, `sync`, `collection add`, `collection reindex`) poll `GET /jobs/{id}` on a fixed interval via the shared `_poll_job` helper (`cli/_helpers.py`) until the job reaches a terminal status, printing `phase: processed/total` progress lines along the way. On `DONE` the command exits `0`; on `FAILED`/`FAILED_EXPIRED`/`CANCELLED` it prints `Job <STATUS>: <error>` and exits `1`. Ctrl-C prints `Polling stopped — job continues on server` and detaches — the job keeps running on the server.

```bash
# Submit and block until ingest finishes (or fails)
archon-search ingest --path ./docs --collection handbook --wait
```

## REST surface

All routes require a `Bearer` token (`server/routes_jobs.py`). `GET /openapi.json` is the authoritative schema.

| Route | Purpose |
|---|---|
| `GET /jobs` | List jobs (namespace-filtered). Query params: `status` (repeatable), `kind`, `source`, `limit` (1..200, default 50), `cursor`. Response envelope below. |
| `GET /jobs/{id}` | Fetch one job; `404` if missing or in another namespace. |
| `POST /jobs/{id}/resume` | Transition a `FAILED` **export / import / migration** job back to `QUEUED` for retry. `409` if the job type is not resumable or is not `FAILED`; `422` if the backing file is gone. |
| `DELETE /jobs/{id}` | Cancel an active job (sets `CANCELLING`, returns `202`); idempotent `200` on already-terminal jobs. |

`GET /jobs` returns a cursor-paginated envelope object (never a bare array):

| Field | Type | Meaning |
|---|---|---|
| `items` | array | The page of jobs, newest first. |
| `next_cursor` | string \| null | Continuation token — pass it back as the `cursor` query param to fetch the next page; `null` on the last page. |
| `total` | integer | Total jobs matching the filters, across all pages. |

```bash
# List running + queued jobs
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/jobs?status=RUNNING&status=PENDING"

# Retry a failed export
curl -s -X POST -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/jobs/$JOB_ID/resume"

# Cancel a running job
curl -s -X DELETE -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8765/jobs/$JOB_ID"
```

Note: only `ExportJob`, `ImportJob`, and `MigrationJob` support `resume`. Ingest and reindex jobs are not resumable — re-submit them instead.

## Seeing active jobs in `status`

`archon-search status` adds a job-queue summary line **only when work is in progress** (`cli/status.py`). It queries `GET /jobs?status=RUNNING` and `?status=PENDING` and, if either is non-zero, prints:

```
Jobs: 1 running, 3 queued — run `archon-search jobs list` for details
```

When nothing is active the line is omitted, so an idle server stays quiet. Counts above 50 display as `50+`. `GET /status` itself does not expose a running/queued count — it only reports the `FAILED_EXPIRED` ingest count — so this line comes from the extra `GET /jobs` calls, not from the status payload.

## Worked example

```bash
# 1. Submit an ingest — returns a job ID immediately (202)
$ archon-search ingest --path ./handbook --collection handbook
job 3f2a9c1e-... QUEUED

# 2. Check the queue at a glance
$ archon-search status
...
Jobs: 1 running, 0 queued — run `archon-search jobs list` for details

# 3. Watch it to completion
$ archon-search jobs show 3f2a9c1e --wait
indexing: 42/128
indexing: 128/128
job_id     3f2a9c1e-...
job_type   ingest
status     DONE
collection handbook
...
```

## Related documents

- [Index](00_index.md) — UserManual table of contents.
- [Ingestion and collections](50_ingestion_and_collections.md) — the operations that create jobs.
- [Export / import](90_export_import.md) — resumable bulk jobs.
- [Running the server](40_running_the_server.md) — the server the CLI proxies to.
- [Operator: maintenance and jobs](../OperatorGuide/50_maintenance_and_jobs.md) — retry policy, `FAILED_EXPIRED`, and the maintenance loop.
- [API reference](../Architecture/600_api_reference_or_public_interface.md) — full REST + MCP + CLI surface.

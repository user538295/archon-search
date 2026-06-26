**Purpose**: Enable, inspect, and reason about local query telemetry.
**Audience**: End users / operators
**Status**: Stable
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# Telemetry

## Principles

1. **Opt-in, disabled by default.** `[telemetry].enabled = false` out of the box; nothing is written until you flip it.
2. **Local only.** Every entry is appended to a JSONL file under `~/.archon-search/search-logs/`. No external transmission exists in v1.
3. **Raw queries are never logged — structural invariant.** The factory methods on `TelemetryEntry` do not accept a `query` parameter (`archon_search/telemetry/entry.py`); there is no code path that can write the raw query.
4. **`doc_id`s are path-derived.** When telemetry is enabled, result `doc_id`s — which equal source file paths — appear in the logs. Treat this as an accepted leak risk; see "Path-derived `doc_id` risk" below.
5. **Old files are pruned.** Files older than `retention_days` are removed at startup and every 24h.

## Enabling

Edit `~/.archon-search/archon-search.toml`:

```toml
[telemetry]
enabled = true
retention_days = 30
log_dir = "~/.archon-search/search-logs"
hash_doc_ids = false         # set true to HMAC-SHA256 hash result_doc_ids before logging
# export_enabled = false   # see note below — true is silently coerced to false
```

Restart the server. Instrumented call sites append one JSON line per call to `<log_dir>/<YYYY-MM-DD>.jsonl` (UTC date):

- `search` and `search_with_context` — instrumented in the **MCP tool wrappers** (`archon_search/server/mcp.py`). The REST `/search` route is **not** instrumented.
- `route` — instrumented in the **REST** endpoint (`archon_search/server/routes_route.py`).

### `export_enabled` coercion

`export_enabled` is reserved for a future remote-export feature. The current behaviour in `archon_search/config.py:209-217` is:

- `false` (default) — stored as-is.
- `true` — logged as a warning (`telemetry: export_enabled is reserved for a future release and will be ignored`) and silently coerced to `false`.

`README.md` and `archon-search.toml.example` both describe this behavior as silent coercion to `false` with a logged warning, matching the implementation. No external transmission occurs.

## What is logged

Every entry contains:

| Field | Notes |
| --- | --- |
| `query_id` | Random UUID per call. |
| `timestamp` | UTC ISO-8601. |
| `endpoint` | `search`, `search_with_context`, or `route`. |
| `latency_ms` | Wall-clock latency. |
| `status` | Exactly one of `ok`, `validation_error`, `timeout`, `internal_error` (the four values of `Status` in `entry.py`). |
| `collection` / `collections` | Endpoint-specific. `collection` is set for `search` / `search_with_context`; `collections` is set for `route` and contains whatever list of collection names the route handler passes in (#Unverified — the precise semantics, e.g. "pinned + routable", are not enforced by the telemetry module; see `routes_route.py` for the call-site logic). |
| `result_count`, `result_doc_ids` | For retrieval endpoints. |
| `truncated` | `true` when `result_doc_ids` were dropped to fit the per-entry size cap (`MAX_ENTRY_BYTES = 8192`); otherwise absent / `null`. See `writer.py`. |
| `decomposer_invoked` | For `route`. |
| `doc_ids_hashed` | `true` when HMAC-SHA256 hashing was active for this entry (i.e., `hash_doc_ids = true` and the salt was successfully loaded); `false` otherwise. |
| `error_kind` | One of `empty_query \| slot_out_of_range \| timeout \| internal_error \| validation_error \| other` on errors. |

## What is never logged

- The **raw query string**. There is no constructor argument that accepts it; this is a structural guarantee, not a runtime filter.
- Exception messages. Only the coarse `error_kind` literal enters the JSONL line.

## Path-derived `doc_id` risk

`result_doc_ids` are derived from the source file path on disk (e.g. `/Users/<name>/Documents/<project>/<file>.md`). When telemetry is enabled, these paths appear in the log files. Concretely, this can reveal:

- Your operating-system username.
- The directory layout of indexed corpora.
- The filenames of matching documents.

To mitigate this, set `hash_doc_ids = true` in the `[telemetry]` config section. When enabled, every `doc_id` is replaced by its HMAC-SHA256 hex digest (64 chars, lowercase) before the JSONL line is written. The salt is generated once at `~/.archon-search/.telemetry-salt` (mode 0600) and reused across restarts; if the salt file is unreadable, hashing is skipped (ERROR logged) — the fallback is plaintext, not a crash. Note that the salt lives alongside LanceDB which stores raw `source_path` in plaintext: HMAC hashing protects telemetry logs **shared or exported separately** from the data directory but does not protect against an attacker with read access to the whole `~/.archon-search/` directory. If this is unacceptable for your environment, leave telemetry disabled.

## Read-back API

Both endpoints return `{"enabled": false}` when telemetry is disabled. When enabled, dates are inclusive and use `YYYY-MM-DD` format.

**Date range resolution.** `since` and `until` are optional query parameters. Server-side, `TelemetryReader.resolve_dates` (`archon_search/telemetry/reader.py`) applies the following defaults and clamping to **both** `/telemetry/stats` and `/telemetry/entries`:

- If `until` is omitted, it defaults to **today (UTC)**.
- If `since` is omitted, it defaults to `until - retention_days`.
- If `since` is earlier than `until - retention_days`, it is clamped to that floor.

In other words, an "unset" range is not unbounded: it always resolves to a window of at most `retention_days` days ending today UTC.

### `GET /telemetry/stats`

Verified against `archon_search/server/routes_telemetry.py` and `schemas_telemetry.py`:

| Query parameter | Type | Notes |
| --- | --- | --- |
| `since` | `date` | Inclusive start date. Defaults to `until - retention_days` if omitted (see "Date range resolution" above). |
| `until` | `date` | Inclusive end date. Defaults to today UTC if omitted. |

Response (`StatsResponse`):

```json
{
  "schema_version": 1,
  "enabled": true,
  "since": "2026-05-01",
  "until": "2026-05-20",
  "total_queries": 42,
  "success_rate": 0.95,
  "skipped_lines": 0,
  "truncated_count": 0,
  "latency_ms": {"p50": 120.0, "p95": 380.0},
  "by_endpoint": {"search": {"total": 30, "ok": 29, "error": 1}},
  "by_collection": {"docs": {"total": 25, "ok": 25}},
  "error_breakdown": {
    "empty_query": 0, "slot_out_of_range": 0, "timeout": 1,
    "internal_error": 0, "validation_error": 0, "other": 0
  }
}
```

`success_rate` is `null` when the window has zero queries. `by_collection.total` can exceed `total_queries` because routing entries fan out across multiple collections (see the comment in `schemas_telemetry.py:CollectionStats`).

### `GET /telemetry/entries`

| Query parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `since` | `date` | unset → `until - retention_days` | Inclusive start date; clamped to `until - retention_days` if earlier. |
| `until` | `date` | unset → today UTC | Inclusive end date. |
| `collection` | string | unset | Filter by collection name. |
| `endpoint` | `EndpointKind` literal | unset | One of `search`, `search_with_context`, `route`. |
| `status` | `Status` literal | unset | Filter by entry status. |
| `error_kind` | `ErrorKind` literal | unset | Filter by error kind. |
| `offset` | int `>= 0` | `0` | Pagination offset. |
| `limit` | int `1..200` | `50` | Page size. |

Response (`EntriesResponse`):

```json
{
  "schema_version": 1,
  "enabled": true,
  "entries": [ /* raw entry dicts */ ],
  "next_offset": 50,
  "total_in_window": 234,
  "skipped_lines": 0
}
```

Iterate by re-sending the request with `offset = next_offset` until `entries` is empty (equivalently `next_offset >= total_in_window`).

### Curl example

```bash
source ~/.archon-search/.search.env

curl -s "http://127.0.0.1:8765/telemetry/stats?since=2026-05-01" \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"

curl -s "http://127.0.0.1:8765/telemetry/entries?endpoint=search&limit=20" \
  -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY"
```

## Retention

`TelemetryReader` (and the writer pruner) deletes files older than `retention_days` at startup and again every 24h. Reducing `retention_days` causes files to be pruned on the next pass — which may be up to 24h later, since the pruner loop sleeps for 86400s between passes (`archon_search/telemetry/pruner.py`).

## Related documents

- [`02_configuration.md`](./02_configuration.md) — `[telemetry]` keys.
- [`/BREAKING.md`](../../BREAKING.md) — telemetry-surface changes (none currently logged).
- [`../ADRs/05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md) — design rationale.
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — privacy invariants.

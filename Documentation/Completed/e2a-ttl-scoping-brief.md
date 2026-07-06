# Feature Brief: E2a — TTL and Scoping

> **Status**: AAA-reviewed. See [AAA findings](#aaa-review-findings) for the 8 issues incorporated into this brief.
> This brief covers the TTL and scoping sub-features of roadmap item E2 only.
> Entity graph (`GET /graph/`) is split to E2b, gated on E1 (GraphRAG) landing first.
> This brief is the input for `/plan-maker`.

## Problem

Archon-search has no concept of time or logical ownership on individual chunks. Every ingested chunk lives forever and is visible to every search in its collection. This makes two common agent patterns impossible: (1) ephemeral session data that should expire after a task ends; (2) multi-tenant or multi-agent corpora where different callers share a collection but must not see each other's chunks.

## Goal

After E2a, every chunk can carry an optional expiry timestamp and an optional list of scope tags. The D5 maintenance loop prunes expired chunks automatically. Search accepts a `scope_filter` that restricts results to chunks matching the specified scope. Neither feature is on by default — existing behaviour is unchanged for callers that omit both fields.

## Users & Context

- **Agent developers** using Archon via MCP as a session memory store: they ingest tool-call context at task start, want it pruned automatically after the session ends without manual cleanup.
- **Operators** running multi-agent or multi-user corpora in a single collection: they assign scope tags at ingest time and filter at search time to isolate per-user or per-agent views.
- Both groups are technical; the surface is REST + MCP, not a UI.

## Core Flow

### TTL

1. Caller ingests a file or chunk with `ttl_seconds: int` in chunk metadata, or the collection has `default_ttl_seconds` set via `PATCH /collections/{name}`.
2. `pipeline.ingest_file` / `ingest_chunks` computes `expires_at = now_utc + ttl_seconds` and writes it to the chunk row. If neither chunk nor collection specifies a TTL, `expires_at` is `null` — chunk never expires.
3. Chunk-level `ttl_seconds` takes precedence over collection default; `null` on both = no expiry.
4. D5 maintenance loop gains a new policy `prune_expired_chunks` (default `true`): queries `expires_at < now_utc`, deletes matching rows, logs `WARNING: pruned {n} expired chunks from {collection}` with doc_ids to stderr and the maintenance job result.
5. `GET /collections/{name}/expiring?within_hours={n}` returns chunks whose `expires_at` falls within the next `n` hours (paginated, same cursor pattern as `GET /collections/{name}/documents`).
6. `GET /status` maintenance detail gains `expired_chunk_count` and `last_expired_pruned_at`.

### Scoping

1. Caller ingests with `scopes: list[str]` in chunk metadata (e.g. `["user:alice", "run:task-123"]`). Empty list or omitted = no scope restriction.
2. `POST /search` and `POST /explain` accept optional `scope_filter: str` (e.g. `"user:alice"` or `"user:alice*"`).
3. Filter semantics: exact string match OR wildcard suffix match (`*`). `"user:alice"` matches only `"user:alice"`. `"user:alice*"` matches `"user:alice"`, `"user:alice:thread-1"`, etc. No other glob syntax supported.
4. The filter is applied as a pre-retrieval LanceDB predicate — only chunks where `scopes` contains a value satisfying the filter are candidates.
5. MCP `search` and `search_with_context` tools gain a `scope_filter` parameter.
6. `GET /collections/{name}/documents` response includes `scopes` per document.

## In Scope

- `expires_at: utf8 | null` column added to the chunk table schema (triggers `STORE_SCHEMA_VERSION` bump + `migrate_expires_at` migration).
- `scopes: list<utf8>` column added to the chunk table schema (same migration, same version bump — both columns land in one migration).
- `CollectionMeta` gains `default_ttl_seconds: int | null` (null = disabled).
- `_meta_schema()` gains `default_ttl_seconds` column + `migrate_default_ttl` migration.
- `SearchConfig` gains `[maintenance].prune_expired_chunks: bool = true`.
- D5 `MaintenanceLoop._run_expired_chunk_pruning()` policy — runs after orphan cleanup; writes maintenance job result with prune count and doc_ids.
- `GET /collections/{name}/expiring` endpoint (cursor-paginated, `within_hours: int = Query(ge=1, le=8760)`).
- `PATCH /collections/{name}` gains `default_ttl_seconds` field.
- `POST /search` and `POST /explain` gain `scope_filter: str | None`.
- `POST /ingest` (single-file) and `POST /ingest/directory` gain `chunk_ttl_seconds: int | None` and `chunk_scopes: list[str] | None` at the request level (applied to all chunks in the ingest as defaults; per-chunk `ttl_seconds` in chunk metadata dict overrides the request-level value for that chunk only).
- MCP `ingest_file`, `ingest_directory`, `search`, `search_with_context`, `explain` gain the corresponding parameters.
- `GET /status` maintenance sub-object gains `expired_chunk_count` and `last_expired_pruned_at`.
- `schemas.py` updates: `SearchRequest`, `ExplainRequest`, `IngestRequest`, `CollectionUpdateRequest`, `ExpiringChunksResponse`.
- `BREAKING.md`: two new additive chunk-table columns, one new meta column; no removal.
- Doc updates: `130_data_architecture_and_persistence.md` (new columns), `160_operational_readiness_monitoring_and_reliability.md` (TTL pruning runbook), `600_api_reference_or_public_interface.md` (new endpoints + params), `archon-search.toml.example` (`prune_expired_chunks`).

## Out of Scope

- Entity graph endpoints (`GET /graph/`) — split to E2b, blocked on E1.
- Mutation history log entries for TTL deletions — deferred to G2; maintenance job result covers v1 observability.
- Scope hierarchy enforcement or scope registry — scopes are free-form strings; no validation of the namespace prefix beyond character constraints.
- TTL on collection-level resources (collections themselves) — out of scope entirely.
- UI for TTL/scope management — E8.
- `include_expired=true` search flag — no use case identified; expired chunks are gone after pruning.

## Key Decisions

**Chunk-level TTL wins over collection default**: collection `default_ttl_seconds` fills in only when chunk metadata omits `ttl_seconds`. Callers can always override. Operator enforcement via TTL is not the use case — ACL/namespace isolation handles access control; TTL handles lifecycle.

**Exact + wildcard suffix semantics for `scope_filter`**: bare string = exact match; `*`-suffix = prefix match on the scope value. No regex, no mid-string glob. LanceDB predicate: exact via `list_has(scopes, filter_value)` (empirically verified against LanceDB 0.30.2 — `array_has_any` and `array_contains` also work; `ANY(...)` does not); wildcard via Python-side post-filter on the candidate set (LanceDB lacks native prefix-on-array-element; acceptable given top-k candidate counts). Use `list_has` in `build_where` — it requires no array literal and integrates cleanly with `_sql_quote_str`.

**Both schema columns in one `STORE_SCHEMA_VERSION` bump**: `expires_at` and `scopes` arrive together. One migration, one version increment. Avoids two sequential bumps for related columns.

**Pruning logs warnings, not mutation records**: D5 maintenance job result carries `{pruned_count, doc_ids, collection}` per collection. G2 will add durable mutation records when it lands; it can backfill from job history. No speculative G2 schema in E2a.

**`within_hours` ceiling of 8760 (1 year)**: prevents accidental full-table scans from `within_hours=999999`. Operators needing longer windows use `GET /collections/{name}/documents` with client-side filtering.

**`/status` `expired_chunk_count` is a point-in-time snapshot**: at `GET /status` call time, count chunks with `expires_at < now_utc` across all collections (aggregated). Not the count from the last prune run — that's `pruned_count` in the maintenance job result. `last_expired_pruned_at` is the timestamp of the most recent successful `prune_expired_chunks` completion. Both fields are null when `prune_expired_chunks` has never run.

**Input validation constraints**: `ttl_seconds` — integer in `[1, 2^31-1]`; reject `<= 0`. `scopes` — list of 0–100 strings, each 1–255 UTF-8 chars, no null bytes, no empty strings. `scope_filter` — 1–255 chars; allowed: alphanumeric, `_`, `-`, `:`, `.`, `/`, and at most one trailing `*`; multiple `*`, leading `*`, or `*` mid-string → 400 with human-readable message. `default_ttl_seconds` — same range as `ttl_seconds`, or `null` to disable.

**`PATCH /collections/{name}` `default_ttl_seconds` is forward-only**: changing the collection default does not retroactively update `expires_at` on existing chunks. Operators who need to update existing chunks must re-ingest them. This must be documented in the API reference and the runbook.

## Edge Cases & Constraints

- **Pre-E2a collection, first post-E2a ingest**: `expires_at` and `scopes` are null/empty for existing chunks — treated as "no TTL, no scope restriction." `migrate_expires_at` and `migrate_scopes` are idempotent `add_columns` migrations; existing rows gain null defaults. No data loss, no reindex required.
- **Pruning during active ingest**: D5 maintenance loop holds no lock during the prune query. A chunk being actively written could theoretically expire between insertion and the ingest commit. Accepted: the window is tiny and the next ingest of the same document re-creates the chunk with a fresh `expires_at`.
- **`scope_filter` wildcard post-filter cost**: LanceDB returns top-k candidates matching the vector/FTS predicate; wildcard filtering happens in Python on that candidate set. At `top_k=100`, this is negligible. At `top_k_max` (operator ceiling, E0c), still negligible. No latency concern.
- **`expires_at` precision**: stored as ISO 8601 UTC string (consistent with existing timestamp columns). Pruning compares `expires_at < now_utc` at maintenance loop invocation time — not sub-second precision; acceptable for session/task TTL use cases.
- **Collection with `default_ttl_seconds` set, existing chunks**: existing chunks keep `expires_at=null` — they do not retroactively inherit the new default. Only new ingests pick it up.
- **`scope_filter` on a collection with no scoped chunks**: returns all candidates (no chunks are excluded); filter is a no-op. No error.
- **`within_hours=0`**: rejected by `ge=1` validation; returns 422.
- **Pruning + concurrent re-ingest**: pruning deletes a chunk, then ingest re-creates it before the ingest commit completes — the chunk briefly disappears and reappears. No lock coordination between the maintenance loop and ingest. Acceptable for session TTL and multi-tenant use cases, which tolerate eventual consistency. Documented in the runbook.
- **`scope_filter` with invalid syntax**: multiple `*`, leading `*`, or `*` mid-string → 400 with error `"invalid scope_filter syntax"`. A bare `*` (match-all) is explicitly rejected — callers who want unfiltered results must omit the parameter.
- **`scope_filter` wildcard post-filter latency**: Python-side filtering on top-k candidate sets is negligible at typical `top_k` values. Acceptance criterion: p99 overhead &lt;10ms at `top_k=1000` (verified in implementation, documented in test).

## Test Acceptance Criteria

- **TTL pruning**: chunk with `expires_at < now_utc` is deleted by the maintenance loop; chunk with `expires_at >= now_utc` is not.
- **TTL precedence**: per-chunk `ttl_seconds` in chunk metadata overrides request-level `chunk_ttl_seconds`; both override `default_ttl_seconds`; `null` on all three = no expiry.
- **Scope exact match**: `scope_filter="user:alice"` returns only chunks with `"user:alice"` in `scopes`; excludes `"user:alice:thread-1"` and `"user:bob"`.
- **Scope wildcard match**: `scope_filter="user:alice*"` matches `"user:alice"` and `"user:alice:thread-1"`; excludes `"user:bob"`.
- **Scope no-op**: collection with no scoped chunks + any `scope_filter` returns all top-k candidates (no error, no empty result from filter).
- **Scope filter invalid syntax**: `"*"`, `"user:*alice"`, `"user:**"` → 400.
- **Expiring endpoint**: `GET /expiring?within_hours=24` returns only chunks with `expires_at` in `[now_utc, now_utc + 24h)`; excludes already-expired and never-expiring chunks.
- **`/status` fields**: after a prune run, `last_expired_pruned_at` is set; `expired_chunk_count` reflects the live point-in-time count (not the prune-run delta).
- **Migration idempotency**: running `migrate_expires_at` + `migrate_scopes` twice on an existing table produces no error and no data change.
- **Input validation**: `ttl_seconds=0`, `ttl_seconds=-1`, scope string of 256 chars, scope list of 101 items → 422.
- **Wildcard latency**: `scope_filter="user:*"` at `top_k=1000` adds &lt;10ms p99 overhead (measured and logged in test output).

## Open Questions

None. All decisions resolved.

## Future Iterations

- Per-chunk `ttl_seconds` override via raw chunk metadata dict at ingest (v2).
- `include_expired=true` flag to search already-pruned chunks via job history (requires G2).
- Scope registry / validation (list of allowed scope prefixes per collection) — useful for strict multi-tenant deployments.
- TTL-triggered webhooks: notify an external endpoint when chunks expire (G-series).
- `GET /collections/{name}/expiring` in the E8 admin UI.

## Recommendation

Ship E2a now. TTL and scoping are the minimum viable memory-lifecycle story for agent use cases — without them, any agent using Archon for session memory must implement its own cleanup, which defeats the purpose. The implementation is straightforward: two new LanceDB columns, one maintenance policy, a handful of request/response fields. The hardest part is the LanceDB wildcard predicate workaround (Python-side post-filter), which is minor. Don't wait for E1 — entity graph is a separate concern and a much heavier lift. The split to E2a/E2b is the right call.

---

## AAA Review Findings

**Classification**: Solid, approaching Strong. Design is technically complete; findings below are clarifications and hardening — no redesigns required.

### ❌ Blocking
None.

### ⚠️ Critical (resolved in this brief)

**1. Per-chunk TTL override scope was ambiguous** — In Scope now explicitly states: per-chunk `ttl_seconds` in chunk metadata dict overrides request-level `chunk_ttl_seconds` for that chunk. Out of Scope no longer mentions this as deferred.

**2. `/status` `expired_chunk_count` semantics were undefined** — Key Decisions now specifies: point-in-time count at call time (not last-prune delta); aggregated across all collections; null before first prune run.

**3. Input validation constraints were missing** — Key Decisions now specifies ranges and character constraints for `ttl_seconds`, `scopes`, `scope_filter`, and `default_ttl_seconds`.

### ⚠️ Major (resolved in this brief)

**4. `scope_filter` invalid syntax behaviour was unspecified** — Edge Cases now specifies: multiple `*`, leading `*`, `*` mid-string, or bare `*` → 400 with human-readable error. Resolved.

**5. `PATCH /collections/{name}` forward-only semantics not called out** — Key Decisions now documents that `default_ttl_seconds` changes are not retroactive; operators must re-ingest to update existing chunks.

### ⚠️ Minor (resolved in this brief)

**6. Wildcard post-filter latency had no acceptance criterion** — Edge Cases and Test Acceptance Criteria now specify: p99 overhead &lt;10ms at `top_k=1000`.

**7. Pruning + concurrent re-ingest atomicity gap not documented** — Edge Cases now documents the eventual-consistency window and the runbook requirement.

**8. Test acceptance criteria were absent** — Test Acceptance Criteria section added covering all 11 test cases.

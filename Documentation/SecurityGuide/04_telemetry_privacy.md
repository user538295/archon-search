**Purpose**: Describe the privacy guarantees, residual risks, and controls of `archon-search` telemetry.
**Audience**: Security engineers, privacy reviewers, operators evaluating whether to enable telemetry.
**Status**: Draft
**Last reviewed**: 2026-05-20
**Next review**: 2027-05-20

# Telemetry Privacy

Telemetry in `archon-search` is **opt-in, default off, local-only, and structurally incapable of recording raw query text**. This document describes what that means in practice, what risks remain, and how to verify the controls.

For the architectural commitment, see ADR [`../ADRs/05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md) and [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md).

## Principles

1. **Privacy is structural, not procedural.** The schema has no field for the query, and no factory accepts one.
2. **Local-only.** No transport ships in v1; nothing leaves the host.
3. **Off by default.** A fresh install produces no telemetry until the operator opts in.
4. **Path-derived identifiers are mitigated by HMAC hashing.** Operators sharing logs can enable `hash_doc_ids = true`; see below.

## Default posture

| Setting | Default | Source |
| --- | --- | --- |
| `[telemetry].enabled` | `false` | `archon_search/config.py` `TelemetryConfig.enabled = False` |
| `[telemetry].retention_days` | `30` | `archon_search/config.py` `TelemetryConfig.retention_days = 30` |
| `[telemetry].export_enabled` | `false` (always — coerced from `true` with a warning) | `archon_search/config.py:213–215` |
| `[telemetry].log_dir` | `~/.archon-search/search-logs` | `archon_search/config.py` `TelemetryConfig.log_dir` |
| `[telemetry].hash_doc_ids` | `false` | `archon_search/config.py` `TelemetryConfig.hash_doc_ids = False` |

When `enabled = false`, the `TelemetryWriter` is never started and `app.state.telemetry_writer` is `None` (`archon_search/server/app.py:104–105`).

Note: `retention_days` is validated at config load — values `< 1` raise `ConfigError` (`archon_search/config.py:206–207`), so the minimum reachable value is `1`.

## Structural no-raw-query invariant

The Pydantic model in `archon_search/telemetry/entry.py` enforces the privacy contract by construction:

- `TelemetryEntry.model_config = ConfigDict(extra="forbid", frozen=True)` — extra fields cannot be added at runtime, and instances are immutable.
- The documented field set (`DOCUMENTED_SCHEMA_FIELDS` at `entry.py:39–54`) is exactly: `query_id`, `timestamp`, `endpoint`, `latency_ms`, `status`, `collection`, `result_count`, `result_doc_ids`, `truncated`, `collections`, `decomposer_invoked`, `error_kind`, `doc_ids_hashed`. **There is no `query` field.**
- The factory classmethods are keyword-only and none accepts a `query` parameter:
  - `from_search_tool_result(*, endpoint, collection, result_doc_ids, latency_ms, doc_id_hasher=None)`
  - `from_route_response(*, collections, decomposer_invoked, latency_ms)`
  - `from_error(*, endpoint, status, error_kind, latency_ms)`

Adding raw query logging therefore requires editing the model itself — a deliberate, reviewable code change. This invariant is also called out in the project `CLAUDE.md` ("Structural invariant: factory methods in `entry.py` do not accept a `query` parameter").

The invariant is enforced structurally at the factory level AND by a dedicated test (`tests/telemetry/test_entry_factories.py::test_factory_signatures_reject_raw_query_argument`) that introspects every factory and forbids `{'query', 'query_text', 'body', 'request'}` kwargs. Tracked as `SEC-3` in [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md).

## What goes into telemetry vs. what does not

| Logged | Not logged |
| --- | --- |
| `query_id` (UUID4 hex, generated per call) | The query string |
| ISO-8601 UTC timestamp | Client IP, headers, bearer token |
| Endpoint kind (`search`, `search_with_context`, `route`) | Request body |
| `latency_ms` | Stack traces or exception messages (only `ErrorKind` enum) |
| `status` (`ok`, `validation_error`, `timeout`, `internal_error`) | User identity beyond the resolved namespace (and even that is not logged today) |
| `collection` (search) or `collections` list (route) | Chunk text |
| `result_count`, `result_doc_ids`, `truncated` | Embedding vectors |
| `decomposer_invoked` (route) | Per-result scores |
| `error_kind` from a closed enum | Anything from `request.state.namespace` — namespace is not in the schema |
| `doc_ids_hashed` (bool flag) | Raw query text in any form |

The error path uses a closed `ErrorKind` enum (`empty_query`, `slot_out_of_range`, `timeout`, `internal_error`, `validation_error`, `other`); free-form messages cannot enter telemetry.

## doc_id path-leak risk — mitigated by HMAC hashing mode (D8)

`doc_id` values are derived from source file paths. The risk:

- `result_doc_ids` is logged in every successful search telemetry entry.
- The `source_path` column in the LanceDB chunk table stores the path in clear (`archon_search/store.py::_schema`).
- Anyone with read access to `~/.archon-search/search/` can correlate `result_doc_ids` from `~/.archon-search/search-logs/<date>.jsonl` back to filesystem paths. On a single-user host that is the operator by definition; on a shared host with a relaxed home directory, it is anyone with the right Unix permissions.

**Mitigation — HMAC hashing mode (D8, implemented 2026-06-25).** Set `[telemetry] hash_doc_ids = true` in `archon-search.toml` to apply a second-stage HMAC-SHA256 transform to every `result_doc_ids` value before it is written to JSONL. This severs the mapping from log values to LanceDB source paths.

### How it works

- `archon_search/telemetry/hasher.py` provides `hash_doc_id(salt, doc_id) -> str` (deterministic, 64-char lowercase hex HMAC-SHA256).
- On first startup with `hash_doc_ids = true`, a 32-byte salt is generated atomically and stored at `get_data_dir()/.telemetry-salt` with mode `0600`. On subsequent startups the salt is reloaded; values are stable across restarts.
- If the salt file is unreadable at startup, an ERROR is logged and hashing falls back to disabled for the session — the server never crashes.
- Every `TelemetryEntry` carries `doc_ids_hashed: bool = False`. The field is `True` only for entries where the hasher was active, so log consumers can detect boundary transitions when the flag is toggled.
- `GET /status` exposes `telemetry.hash_doc_ids_enabled: bool` — `true` only when both the config flag is on **and** a valid salt was loaded at startup.

### Threat-model scope (salt co-location)

The salt at `get_data_dir()/.telemetry-salt` sits alongside LanceDB, which stores raw `source_path` in plaintext. HMAC hashing protects telemetry logs **shared or exported separately** from the data directory (the stated threat model). It does **not** protect against an attacker with read access to the whole `~/.archon-search/` directory — they hold both the salt and the plaintext paths. Operators who need protection against a full-directory compromise require filesystem-level encryption or separate storage for the salt, which is out of scope for v1.

See `SEC-2` (now closed) in [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) and the Amendment section in ADR-05.

**Fallback mitigation:** if HMAC hashing cannot be enabled, do not enable telemetry on a host where filesystem path names themselves carry sensitive context (e.g., paths named after client engagements or projects).

## Storage and retention

- **Location.** One file per UTC date, `~/.archon-search/search-logs/<YYYY-MM-DD>.jsonl`. Lines are appended; one JSON object per call.
- **Retention.** `archon_search/telemetry/pruner.py::Pruner.prune_once` deletes `*.jsonl` files whose stem date is older than `today - retention_days`. **Today's file is never deleted** regardless of cutoff (`pruner.py:44–45` — `if file_date == now: continue`). Note that `retention_days` is validated at config load to be `>= 1`, so `retention_days = 0` is unreachable through configuration; the smallest reachable value (`retention_days = 1`) still preserves today's file. The pruner runs once at startup and then on a 24-hour interval.
- **No per-entry redaction.** Old entries are deleted by *file age*, not by per-line policy.
- **Directory permissions.** The directory is created with `mkdir(..., parents=True, exist_ok=True)` (`server/app.py:97`) and inherits the user's umask. The operator is responsible for ensuring the parent home directory is not world-readable on shared hosts.

## No external transmission

`[telemetry].export_enabled = true` is **silently coerced** to `false` and a warning is logged (`archon_search/config.py:213–215`):

> `telemetry: export_enabled is reserved for a future release and will be ignored`

There is no corresponding transport code in `archon_search/telemetry/`. The writer's only sink is the daily JSONL file. Enabling external export would require both a config-loader change and a new transport implementation; neither is shipped in v1.

Note: the project debt register flags `TEL-1` because the documented intent in `CLAUDE.md` says "rejected at config load" while the code performs silent coercion. The effective security behavior is the same — nothing leaves the host — but the diagnostic is weaker than the docs suggest. #Unverified

## How to disable

Telemetry is off by default. To explicitly disable in a config file:

```toml
[telemetry]
enabled = false
```

To disable after enabling:

1. Set `[telemetry].enabled = false` in `~/.archon-search/archon-search.toml`.
2. Restart the server.
3. Optionally delete `~/.archon-search/search-logs/*.jsonl` if you also want to remove the historical record.

## How to verify it is disabled

Three independent checks:

1. **No writer object.** On a process started with telemetry off, no JSONL files are created. After issuing a few `/search` calls, `ls ~/.archon-search/search-logs/` should still be empty or unchanged.
2. **Telemetry endpoints behave consistently.** `GET /telemetry/stats` (`routes_telemetry.py`) reads from the log directory; with telemetry off it has no data to summarize. A non-empty response on a quiet system is a signal something is writing.
3. **Startup log.** When telemetry is enabled, the lifespan creates `log_dir`, runs an initial prune, and starts the writer (`server/app.py:95–103`). The application log shows no such activity when disabled.

Note: the `grep` recipe below uses a regex anchored on the JSON key boundary to avoid false positives from substrings such as `query_id`.

To verify that telemetry, when enabled, does not contain raw queries:

```bash
# After enabling and issuing a /search call:
grep -E '"query"[[:space:]]*:' ~/.archon-search/search-logs/*.jsonl
# Expected: no matches. If you see any, file a security issue.
# (The boundary on ":" excludes legitimate keys such as "query_id".)
```

## Related documents

- [`01_threat_model.md`](./01_threat_model.md) — telemetry as an asset.
- [`../ADRs/05_opt_in_local_telemetry_no_raw_query.md`](../ADRs/05_opt_in_local_telemetry_no_raw_query.md) — original decision + D8 Amendment (HMAC hashing mode).
- [`../Architecture/150_security_and_privacy_architecture.md`](../Architecture/150_security_and_privacy_architecture.md) — broader privacy architecture.
- [`../Architecture/530_technical_debt_refactoring_roadmap.md`](../Architecture/530_technical_debt_refactoring_roadmap.md) — ~~`SEC-2`~~ (resolved by D8), `SEC-3`, `TEL-1`.
- [`../UserManual/120_telemetry.md`](../UserManual/120_telemetry.md) — operator-facing how-to.

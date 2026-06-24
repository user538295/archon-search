# Feature Brief: D8 — Hardening: Hashed `doc_id` Mode for Telemetry (SEC-2)

## Problem

When telemetry is enabled, `result_doc_ids` are written to JSONL logs as SHA-256 hashes of resolved filesystem paths. Because the same mapping is stored unencrypted in LanceDB, anyone with read access to `~/.archon-search/` can join telemetry logs back to exact file paths — making the "hash" effectively reversible. Operators who share logs, run archon-search on multi-user hosts, or bundle logs into support archives inadvertently expose their filesystem layout.

## Goal

An operator can enable a second-stage hash on `result_doc_ids` so that telemetry JSONL files are opaque to anyone without access to the server-derived salt. `doc_id` uniqueness is preserved (counts and per-document metrics remain valid), but the mapping to LanceDB source paths is severed.

## Users & Context

Operators running archon-search on shared infrastructure (VMs, containers, CI environments) or who forward telemetry logs to external observability systems. They have telemetry enabled and need confidence that log files do not leak filesystem path structure — even to readers with LanceDB access.

## Core Flow

1. Operator opens `archon-search.toml` and sets `[telemetry] hash_doc_ids = true`.
2. Server starts; config loader reads the flag and generates (or loads from disk) a server-bound HMAC salt stored at `get_data_dir() / ".telemetry-salt"` with mode 600.
3. A search request arrives; the pipeline resolves results with their raw `doc_id` hashes.
4. Before the telemetry entry is written, each `doc_id` in `result_doc_ids` is transformed: `HMAC-SHA256(salt, doc_id_hex)` → truncated to 32 hex characters (128-bit, collision-safe for realistic collection sizes).
5. The JSONL entry is written with the hashed `result_doc_ids`; the raw `doc_id` values never touch the log file.
6. `/telemetry/stats` and `/telemetry/entries` responses reflect the hashed values unchanged — the API surface is unmodified.
7. If the operator disables `hash_doc_ids` later, new entries contain raw `doc_id` hashes; existing log entries are not retroactively transformed.

## In Scope

- New `TelemetryConfig` field: `hash_doc_ids: bool = False` (default off; backward-compatible).
- Salt management: on first start with `hash_doc_ids = true`, generate a 32-byte cryptographic random salt, write it to `get_data_dir() / ".telemetry-salt"` with mode 600. On subsequent starts, load the existing salt. Salt file path follows the same `get_data_dir()` convention as `key_manager.py`.
- Transform function applied in `TelemetryEntry` factory methods (`from_search_tool_result`, `from_search_multi_result`) — the two factories that populate `result_doc_ids`. The transform is a pure function `hash_doc_id(salt: bytes, doc_id: str) -> str` in `archon_search/telemetry/entry.py` (or a sibling module `archon_search/telemetry/hasher.py`).
- The transform is passed into the factory method as an optional callable argument `doc_id_hasher: Callable[[str], str] | None = None`; when `None`, raw values are used. This keeps the factory signature clean and the behaviour testable without a real salt.
- `routes_search.py` constructs and passes the hasher when the config flag is on.
- `archon-search.toml.example` updated with the new `hash_doc_ids` key and a comment explaining what it does and when to use it.
- Documentation updated: `Documentation/Architecture/150_security_and_privacy_architecture.md` (remove "accepted risk" caveat), `CLAUDE.md` telemetry section, Security Guide `04_telemetry_privacy.md`, and debt register `530_technical_debt_refactoring_roadmap.md` (close SEC-2).
- ADR-05 updated to record that the hashed-doc-id mode is now implemented (append a "Amendment" section — ADRs are append-only).
- Tests: unit test for `hash_doc_id` (determinism, distinct inputs → distinct outputs, raw vs hashed entry comparison); config test for the new field; integration test asserting that with `hash_doc_ids = true`, no value in `result_doc_ids` matches any raw SHA-256 of the ingested file paths.

## Out of Scope

- Retroactive re-hashing of existing JSONL log entries — log files are append-only; old entries are not modified.
- Hashing `doc_id` in the LanceDB store itself — this feature is telemetry-only; the store schema is unchanged.
- Hashing the `collection` field or any other telemetry field — only `result_doc_ids` is the identified leak vector.
- Key rotation for the HMAC salt — deferred; rotating the salt would break metric continuity across log files. A rotation mechanism (with a log-format version bump) is a separate concern.
- Telemetry export (`export_enabled`) — still coerced to `false`; this feature does not unblock it.
- CLI or API to inspect or rotate the salt file — operator must manage it directly (consistent with the existing key file pattern).

## Key Decisions

- **HMAC-SHA256 over a second SHA-256 pass**: A bare double-hash (`sha256(sha256(path))`) can still be brute-forced over the known path space. HMAC with a server-side secret breaks the correlation completely without knowing the salt. This is the minimum viable fix for the stated threat model.
- **Salt stored on disk, not in TOML**: Puts the secret outside config files that operators are more likely to share, grep, or commit. Follows the same pattern as `key_manager.py`.
- **Default off**: Changing the log format of existing deployments silently is a breaking operational change. Operators who enabled telemetry and built dashboards around existing `doc_id` values would see metric discontinuity. Opt-in preserves their experience.
- **Callable injected into factory, not a module-global**: Keeps the factory methods pure and testable; avoids a singleton that makes unit tests order-dependent.
- **Keep full 64-char HMAC output**: Preserves the existing field length, avoiding silent breakage in any consumer that validates a 64-char `doc_id`. 128-bit truncation is off the table until a grep of `reader.py`, `routes_telemetry.py`, and tests confirms no fixed-length check exists.
- **Include `doc_ids_hashed: bool` in every JSONL entry**: Added as an optional field (default `False`); set to `true` when `hash_doc_ids = true`. This lets consumers distinguish log segments written before and after the flag is toggled — prevents silent metric mixing. `DOCUMENTED_SCHEMA_FIELDS` in `entry.py` and `schemas_telemetry.py` must be updated accordingly.
- **Surface `hash_doc_ids_enabled` in `GET /status`**: One boolean under `telemetry.hash_doc_ids_enabled`; mirrors the pattern of other config-derived flags already in the status response. Avoids requiring operators to open the config file to verify the active state.

## Edge Cases & Constraints

- **Salt file missing at startup with `hash_doc_ids = true`**: generate a fresh salt and write it. Log a WARNING so operators know a new salt was created (metric continuity is broken from this point).
- **Salt file unreadable (permissions issue)**: log an ERROR and fall back to `hash_doc_ids = false` for this session. Do not crash the server. The WARNING must be prominent enough for operators to act on.
- **`hash_doc_ids` toggled off then on between restarts**: the same salt file is reused if it still exists; metric continuity is preserved within the salt's lifetime.
- **`result_doc_ids` is `None`**: factory methods already handle `None`; the hasher is not called. No change needed.
- **Empty `result_doc_ids` list**: hasher is applied to an empty list; result is an empty list. No special case needed.
- **`from_explain_result` factory**: this factory does not populate `result_doc_ids` (it is `None`). No hasher parameter needed for this factory.
- **`from_error` and `from_route_response` factories**: neither populates `result_doc_ids`. No hasher parameter needed.
- **Concurrent search requests**: the hasher callable is stateless (pure function over salt + input); no locking required.
- **`ARCHON_SEARCH_DATA_DIR` override**: salt file path derives from `get_data_dir()`, so it follows the data directory correctly in all deployment modes including Docker.

## Open Questions

_Resolved — see Key Decisions._

## Future Iterations

- Salt rotation with JSONL log-format version bump (`doc_ids_salt_id` or `log_version` header) — allows rolling the salt without losing metric continuity.
- Automatic rotation on a configurable interval (e.g., monthly) for operators who forward logs to external systems.
- `archon-search telemetry re-hash --old-salt OLD --new-salt NEW` CLI tool to retroactively transform existing log files when rotation occurs.
- Hashed-doc-id support for the (currently deferred) `export_enabled` telemetry export path — prerequisite when remote export ships.

## Recommendation

Build this now. The threat model is real: telemetry enables path-leak correlation to any reader with LanceDB access, and "don't enable telemetry on sensitive hosts" is not a workable operator stance once telemetry becomes more useful. The implementation is narrow — one config field, one salt file, one pure transform function injected into two factory methods. The hardest part is getting the salt lifecycle right (generate-on-first-use, fail-safe on unreadable). All open questions are resolved; planning can start.

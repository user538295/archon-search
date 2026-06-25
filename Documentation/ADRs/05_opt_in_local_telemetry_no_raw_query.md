# 05. Opt-In Local Telemetry with No Raw Query Logging

**Status**: Accepted
**Date**: 2026-05-20
**Deciders**: archon-search maintainers

## Context

Operators need visibility into query volume, latency, error mix, and
collection usage to tune routing and capacity. At the same time,
`archon-search` is a local, single-user-or-small-team tool that indexes
potentially sensitive content. Logging raw query strings — even locally —
creates a durable record of user intent that may outlive the session and be
exfiltrated by accident (backups, support bundles, screen shares).

The README's "Telemetry (opt-in)" section commits to: disabled by default,
local-only, no raw query text, and no remote export in v1.

## Decision

Telemetry is **opt-in, local-only, and structurally cannot record raw query
strings**.

- **Opt-in**: `TelemetryConfig.enabled` defaults to `False`
  (`archon_search/config.py`).
- **Local-only**: One JSONL line per call appended to a daily file under
  `[telemetry].log_dir` (default `~/.archon-search/search-logs`) by
  `archon_search/telemetry/writer.py`. Retention is enforced by
  `[telemetry].retention_days`.
- **No raw query**: `TelemetryEntry` (`archon_search/telemetry/entry.py`) has
  no `query` field, and its factory methods (`from_search_tool_result`,
  `from_route_response`, `from_error`) do not accept a `query` parameter.
  This is a structural privacy guarantee, not a runtime check.
- **No remote export in v1**: `[telemetry].export_enabled = true` is not
  honored — `load_config` logs a warning and coerces the value back to
  `False` (`archon_search/config.py`).
- **`doc_id` path-leak risk accepted**: `result_doc_ids` are derived from source
  file paths and may reveal filesystem structure when telemetry is enabled. This
  is documented in the README and accepted; a hashed-doc-id mode is deferred.

## Consequences

### Positive
- Default-off, local-only design means the privacy posture does not depend
  on operator vigilance.
- The no-raw-query guarantee is enforced structurally — `TelemetryEntry` has
  `model_config = ConfigDict(extra="forbid", frozen=True)` (Pydantic runtime
  validation) and the factory methods use keyword-only signatures that omit
  any `query` parameter — not by review.
- Telemetry data is inspectable on disk; no opaque service in the loop.
- Aggregated stats are still useful — `error_kind` is a closed enum
  sufficient for trend analysis without exposing exception messages.

### Negative
- Without raw queries, debugging a specific bad result requires the user to
  reproduce it interactively — telemetry alone cannot explain *why* a query
  failed.
- `doc_id`s in logs may still embed PII via filesystem paths until a hashed mode lands.
- Opt-in defaults mean most installs produce no telemetry, so fleet-level
  insights are unavailable by design.

## Alternatives Considered

- **Telemetry on by default**: Rejected — violates the privacy default for a
  local tool that indexes potentially sensitive content.
- **Redacted / hashed query logging**: Rejected for v1 — hashing is not yet
  implemented and "redacted" leaves residual leak risk via length, n-gram,
  or repeated-prefix patterns. Tracked as future work.
- **Remote export of telemetry**: Deferred — the `export_enabled` key is
  reserved in the config schema but actively rejected at load time, so the
  surface is preserved without shipping the capability.

---

## Amendment — D8: Hashed doc_id Mode (2026-06-25)

**Status**: Implemented
**Deciders**: archon-search maintainers

### Context

Operators who share or export telemetry logs separately from the data directory
(e.g., off-host log forwarding, support bundles) needed a way to prevent
filesystem path reconstruction from `result_doc_ids`. The original decision
accepted this as a residual risk; D8 delivers the deferred mitigation.

### Decision

A second-stage HMAC-SHA256 transform is added behind an opt-in config flag
`[telemetry] hash_doc_ids = true` (default `false`). When enabled:

- `archon_search/telemetry/hasher.py` provides two primitives:
  - `hash_doc_id(salt: bytes, doc_id: str) -> str` — pure, deterministic
    HMAC-SHA256 producing a 64-char lowercase hex string.
  - `load_or_create_salt(hash_doc_ids_enabled: bool) -> bytes | None` — reads
    or atomically generates a 32-byte salt at `get_data_dir()/.telemetry-salt`
    (mode 0600); returns `None` if disabled or if the file is unreadable
    (fallback path, ERROR logged, no crash).
- `TelemetryEntry` gains `doc_ids_hashed: bool = False`. `from_search_tool_result`
  accepts `doc_id_hasher: Callable[[str], str] | None = None`; when provided,
  applies it to every `result_doc_ids` element and sets `doc_ids_hashed=True`.
- The lifespan in `app.py` calls `load_or_create_salt()` once at startup,
  stores `app.state.salt_bytes` and constructs `app.state.doc_id_hasher` via
  `functools.partial`. The hasher is injected into the REST `/search` route
  handler and forwarded to `create_mcp_http_app()` so MCP `search` and
  `search_with_context` tools hash consistently.
- `GET /status` exposes `telemetry.hash_doc_ids_enabled: bool` — `true` only
  when both the config flag is on **and** a valid salt was loaded (so the
  fallback is observable).

### Threat-model scope

The salt at `get_data_dir()/.telemetry-salt` sits alongside LanceDB, which
stores raw `source_path` in plaintext. HMAC hashing protects telemetry logs
**shared or exported separately** from the data directory. It does **not**
protect against an attacker with read access to the whole `~/.archon-search/`
directory. This scope limitation is documented in
[`Documentation/SecurityGuide/04_telemetry_privacy.md`](../SecurityGuide/04_telemetry_privacy.md)
and in `Documentation/Architecture/150_security_and_privacy_architecture.md`.

### Consequences

- Operators can now share exported telemetry logs without exposing filesystem
  paths, provided the data directory is not also shared.
- The `doc_ids_hashed` boolean field lets log consumers detect log-segment
  boundaries when the flag is toggled.
- Default is `false`; existing deployments are unaffected.
- `SEC-2` in the tech-debt register is closed.

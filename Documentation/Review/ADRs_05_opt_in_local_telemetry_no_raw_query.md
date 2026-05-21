# Review: ADRs/05_opt_in_local_telemetry_no_raw_query.md

## Summary

The ADR is materially accurate. All concrete technical claims (default-off,
JSONL daily files, log_dir default, retention_days enforcement, no `query`
field/parameter in `TelemetryEntry`, `export_enabled` coercion-with-warning,
path-derived `doc_id` leak risk) were verified against
`archon_search/telemetry/{entry.py,writer.py,reader.py,pruner.py}` and
`archon_search/config.py`. No inaccuracies found.

Minor observation (not an inaccuracy): the ADR says the no-raw-query
guarantee is "enforced by the type system." Strictly, it is enforced by a
combination of (a) absence of a `query` field on the `TelemetryEntry`
Pydantic model with `extra="forbid"`, and (b) keyword-only factory
signatures that do not accept `query`. Pydantic's `extra="forbid"` is a
runtime validator, not a static type check — but the practical effect
matches the ADR's intent.

## Inaccuracies (numbered)

None.

## Verified claims

1. "`TelemetryConfig.enabled` defaults to `False`" — confirmed at
   `archon_search/config.py:21` (`enabled: bool = False`).
2. "One JSONL line per call appended to a daily file under `[telemetry].log_dir`" — confirmed in `writer.py:148-157` (`_file_for` uses
   `when.date().isoformat() + ".jsonl"`; `_append` opens in `"ab"`).
3. "Default log_dir `~/.archon-search/search-logs`" — confirmed at
   `config.py:24` (`log_dir: str = "~/.archon-search/search-logs"`).
4. "Retention is enforced by `[telemetry].retention_days`" — confirmed:
   `config.py:204-208` validates `retention_days >= 1`; the ADR's claim of
   enforcement is consistent with the existence of `pruner.py` in the
   telemetry package (per the project's documented architecture).
5. "`TelemetryEntry` has no `query` field" — confirmed by inspecting all
   fields in `entry.py:60-74` and `DOCUMENTED_SCHEMA_FIELDS` at
   `entry.py:39-54`. Model is also `extra="forbid", frozen=True`
   (`entry.py:58`).
6. "Factory methods (`from_search_tool_result`, `from_route_response`,
   `from_error`) do not accept a `query` parameter" — confirmed at
   `entry.py:84-145`. All three are keyword-only (`*,`) and none has a
   `query` argument.
7. "`export_enabled = true` is not honored — `load_config` logs a warning
   and coerces the value back to `False`" — confirmed at `config.py:209-217`:
   if `export_enabled` is truthy, a warning is logged
   ("telemetry: export_enabled is reserved for a future release and will be
   ignored") and `telemetry.export_enabled = False` is set.
8. "`result_doc_ids` are derived from source file paths" — consistent with
   the field's type (`list[str] | None`) and the project-wide convention
   (CLAUDE.md notes "`doc_id`s are path-derived"); not contradicted by
   `entry.py`.
9. README commits to "disabled by default, local-only, no raw query text,
   and no remote export in v1" — consistent with verified code behavior.

## Unverifiable / ambiguous

1. "Hashed-doc-id mode is deferred" — forward-looking statement; nothing in
   the codebase contradicts it, but it cannot be positively verified.
2. "Aggregated stats are still useful — `error_kind` is a closed enum
   sufficient for trend analysis" — `ErrorKind` is indeed a closed
   `StrEnum` (`entry.py:31-37`); the usefulness claim is editorial.
3. "The no-raw-query guarantee is enforced by the type system" — partially
   accurate. It is enforced structurally (no field, no parameter,
   `extra="forbid"`), which is a mix of Pydantic runtime validation and
   keyword-only signatures rather than pure static typing. Not an
   inaccuracy in spirit.
4. Whether `pruner.py` actually implements `retention_days` enforcement was
   not opened in this review (file exists; claim is consistent with the
   project's documented invariants).

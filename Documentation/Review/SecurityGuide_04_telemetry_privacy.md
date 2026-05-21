# Review: SecurityGuide/04_telemetry_privacy.md

## Summary

The doc is largely accurate against `archon_search/telemetry/` and `archon_search/config.py`. The structural no-raw-query invariant, the closed `ErrorKind` enum, the silent `export_enabled` coercion, the daily-file retention semantics, and the "today's file is never deleted" rule all match code. One material inaccuracy: the doc's claim that "today's file is never deleted, even if `retention_days` is set to `0`" is misleading because the config loader rejects `retention_days < 1` outright (`ConfigError` at `config.py:206-207`), so `0` is unreachable from configuration. Several line-number citations are slightly off but close. The reachable values of `Status` versus `ErrorKind` are stated correctly.

## Inaccuracies (numbered)

1. **`retention_days = 0` scenario is unreachable.** Doc line 77: "Today's file is never deleted, even if `retention_days` is set to `0`". `config.py:206-207` raises `ConfigError` if `retention_days < 1`, so the user cannot set `0` via config. The `Pruner` code path does honor "today is never deleted" regardless of cutoff (verified in `pruner.py:44-45`), but the example scenario the doc uses to illustrate the invariant cannot occur in normal operation. Either change the example to `retention_days = 1` or note the validation floor.

2. **`config.py:209-217` citation imprecise.** Doc lines 26 and 83 cite `config.py:209-217` for the `export_enabled` coercion. The actual block spans `config.py:209-217` for the `if "export_enabled" in telemetry_cfg:` body — this matches; however line 217 is the trailing `telemetry.export_enabled = export_enabled` (the False branch). The cited range is correct; flagged here only because the doc claims the coercion happens in that range, and in fact the True-branch warning+coercion is `config.py:213-215`. Minor — recommend narrowing to `213-215`.

3. **`server/app.py:103-105` citation off-by-a-few.** Doc line 29 cites `server/app.py:103-105` for `app.state.telemetry_writer = None`. In the read source, that assignment is around line 104-105 inside the `else:` branch beginning at the preceding line. Likely off by 1-2 lines depending on file revision; verify in current file.

4. **`server/app.py:95-103` citation for the enabled branch** (doc line 112) — same caveat as #3; the block exists, lines may have shifted. Worth re-verifying.

5. **`server/app.py:97` citation for `mkdir(..., parents=True, exist_ok=True)`** (doc line 79) — the actual `log_dir.mkdir(parents=True, exist_ok=True)` is inside the `if config.telemetry.enabled:` branch (verified). Line number likely shifted by 1.

6. **`pruner.py:21-27` citation** (doc line 77) — the cited range is the `prune_once` docstring (lines 21-27 in the read source), but the actual "today is never deleted" logic is at `pruner.py:44-45` (`if file_date == now: continue`). Cite the implementation, not the docstring, for verifiability.

## Verified claims

- `TelemetryConfig.enabled = False` default — `config.py:21`.
- `TelemetryConfig.retention_days = 30` default — `config.py:22`.
- `TelemetryConfig.export_enabled = False` default — `config.py:23`.
- `TelemetryConfig.log_dir = "~/.archon-search/search-logs"` default — `config.py:24`.
- `TelemetryEntry.model_config = ConfigDict(extra="forbid", frozen=True)` — `entry.py:58`.
- `DOCUMENTED_SCHEMA_FIELDS` exact set as listed — `entry.py:39-54`. No `query` field anywhere in the model (`entry.py:60-74`).
- All three factory classmethods are keyword-only (`*` marker) and none accepts `query` — `entry.py:84-145`. Signatures match doc verbatim.
- `ErrorKind` enum members exactly: `empty_query`, `slot_out_of_range`, `timeout`, `internal_error`, `validation_error`, `other` — `entry.py:31-37`.
- `Status` enum members: `ok`, `validation_error`, `timeout`, `internal_error` — `entry.py:24-28`. Matches doc table.
- `EndpointKind`: `search`, `search_with_context`, `route` — `entry.py:18-21`. Matches.
- Daily filename pattern `<YYYY-MM-DD>.jsonl` from `writer.py:148-149` (`self._log_dir / f"{when.date().isoformat()}.jsonl"`). Matches doc.
- Pruner deletes by filename stem date and skips today — `pruner.py:37-50`. Matches doc.
- Pruner runs once at startup, then 24h loop — `pruner.py:63-70` and `app.py` lifespan calls `pruner.prune_once` then `pruner.start()`. Matches.
- `export_enabled = true` triggers `logger.warning("telemetry: export_enabled is reserved for a future release and will be ignored")` and silent coercion to `False` — `config.py:213-215`. Matches the warning quote in doc.
- No transport code present in `archon_search/telemetry/` (writer's only sink is the daily JSONL file) — verified by directory listing (`__init__.py, entry.py, pruner.py, reader.py, writer.py`) and writer module's local-file-only design.
- `namespace` is not referenced anywhere in `archon_search/telemetry/` — verified via grep; supports the doc's claim that namespace is not in the schema.
- When telemetry disabled, `app.state.telemetry_writer = None` — verified in `app.py` lifespan `else:` branch.

## Unverifiable / ambiguous

- **`SEC-2`, `SEC-3`, `TEL-1` references** to `Architecture/530_technical_debt_refactoring_roadmap.md` — not opened during this review; trust per scope (only telemetry+config were authoritative sources).
- **`D8` reference** in `Backlog/03_world_class_roadmap.md` — not verified.
- **ADR-05 cross-references** — not verified.
- **`source_path` column in LanceDB schema** (doc line 67, "`archon_search/store.py::_schema`") — not opened in this review; out of scope (the rule said verify against `telemetry/` and `config.py`).
- **Doc claim that "the invariant is enforced by convention at the factory level, not by a dedicated unit test"** — not verified (would require reading `tests/`).
- **Doc's verification recipe** `grep -i '"query"' ~/.archon-search/search-logs/*.jsonl` — semantically correct given the schema has no `query` field, but the recipe would also match a `query_id` JSON key (since `grep -i '"query"'` matches as a substring inside `"query_id"`). The recipe is therefore prone to false positives. Worth noting or refining (e.g., `grep -E '"query"[[:space:]]*:'`).

# Review: UserManual/06_telemetry.md

## Summary

The document is largely accurate but contains one clearly inaccurate claim about which endpoints write telemetry, several incomplete enumerations, and a few minor ambiguities. The structural privacy guarantees (no raw query, opt-in, local-only, JSONL daily files) are all verified true. Field list omits `truncated`; status enum example uses an ellipsis where the source defines exactly four values; the `since`/`until` "unset" default labels conceal server-side resolution semantics.

Source files consulted:
- `/Users/manczg/Documents/development/archon-search/archon_search/telemetry/entry.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/telemetry/writer.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/telemetry/reader.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/telemetry/pruner.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/app.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/routes_telemetry.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/routes_route.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/routes_search.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/mcp.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/server/schemas_telemetry.py`
- `/Users/manczg/Documents/development/archon-search/archon_search/config.py`

## Inaccuracies (numbered)

1. **Line 28** — "New `/search`, `search_with_context` (MCP), and `/route` calls now append one JSON line per call". Misleading on two counts:
   - The leading slash on `/search` implies the REST endpoint, but `routes_search.py` contains zero telemetry hooks (`grep telemetry routes_search.py` returns nothing). Telemetry for `search` is enqueued only by the MCP tool wrapper in `server/mcp.py` lines 50, 64.
   - The correct grouping is: `search` and `search_with_context` are both **MCP tools** (instrumented in `mcp.py`); `/route` is the **REST** endpoint (instrumented in `routes_route.py:72,112,126,141,156`). REST `/search` is not instrumented.

2. **Line 37** — "The README and `archon-search.toml.example` describe this as 'rejected'". Not supported by either source:
   - `README.md:121-123` says "silently coerces the value to `false`".
   - `archon-search.toml.example:67-71` says "the config loader logs a warning and silently coerces this to false".
   The word "rejected" does not appear in either file. The contrast the doc draws is fabricated.

3. **Line 49** — `status` value list `ok, internal_error, timeout, validation_error, …` uses an ellipsis suggesting more values exist. `entry.py` `Status(StrEnum)` defines exactly those four — no more. The ellipsis is inaccurate.

4. **Lines 41-54 (field table)** — Omits the `truncated: bool | None` field. Writer (`writer.py:159-198`) sets `truncated=True` when result_doc_ids are dropped to fit `MAX_ENTRY_BYTES` (8192). It is in `DOCUMENTED_SCHEMA_FIELDS` (entry.py:39-54). Operators reading entries will see it and find no documentation for it here.

5. **Line 50** — "for `/route`, the union of pinned + routable names". `TelemetryEntry.from_route_response` (entry.py:109-125) accepts a `collections: list[str]` argument and writes it verbatim. The actual content passed at the call site (`routes_route.py:112`) needs verification against route-handler logic to confirm the "union of pinned + routable" claim; nothing in `entry.py` or the writer enforces or describes that semantics. Treat as unverified, possibly correct but not grounded in telemetry code.

6. **Lines 110-115 (entries query parameters)** — Default column is "unset" for `since`/`until`. While the FastAPI parameter default is `None`, the server then calls `reader.resolve_dates(...)` which fills `until = today UTC` and `since = until - retention_days`, then clamps `since` to that floor (reader.py:33-56). Saying "unset" hides that an unbounded query still returns a bounded window of at most `retention_days` days. The same resolution applies to `/telemetry/stats` (mentioned only obliquely at line 72: "dates are inclusive").

## Verified claims

- Line 10: `[telemetry].enabled` defaults to `false`. Confirmed: `config.py:21`.
- Line 11: JSONL files under `~/.archon-search/search-logs/`; no external transmission. Confirmed: `config.py:24`, `writer.py:148-157`.
- Line 12: `TelemetryEntry` factories accept no `query` parameter. Confirmed: `entry.py:84-145` — `from_search_tool_result`, `from_route_response`, `from_error` all use keyword-only safe args; no `query` field on the model.
- Line 14: Files older than `retention_days` removed at startup and every 24h. Confirmed: `app.py:99` runs `prune_once` at startup; `pruner.py:63-70` loops `sleep(86400)`.
- Lines 21-25: Config keys `enabled`, `retention_days`, `log_dir`, `export_enabled`. Confirmed: `TelemetryConfig` dataclass at `config.py:19-24`. Defaults match (`retention_days=30`, `log_dir="~/.archon-search/search-logs"`).
- Lines 30-37 (`export_enabled` coercion semantics modulo the inaccuracy in #2): `false` stored as-is; `true` logged as warning and coerced to `false`; warning string matches. Confirmed: `config.py:209-217`. Doc's pinpoint reference `archon_search/config.py:209-217` is exact.
- Line 28 daily filename `<YYYY-MM-DD>.jsonl`: confirmed `writer.py:148` `f"{when.date().isoformat()}.jsonl"`; UTC clock via `datetime.now(UTC)` (writer.py:48).
- Line 44 `query_id`: random UUID per call — `uuid.uuid4().hex` (entry.py:78).
- Line 45 `timestamp`: UTC ISO-8601 — `datetime.now(UTC).isoformat().replace("+00:00", "Z")` (entry.py:81-82).
- Line 53 `error_kind` set — six literals match `ErrorKind` enum (entry.py:31-37).
- Line 58 "Exception messages … only coarse `error_kind`": `from_error` accepts only status + error_kind + latency (entry.py:128-145); no message field anywhere on the model.
- Lines 62-66 (path-derived doc_id risk): consistent with the model — `result_doc_ids: list[str] | None` carries unmodified strings (entry.py:68).
- StatsResponse schema (lines 85-102): all fields verified against `schemas_telemetry.py:39-52` and `reader.compute_stats` (reader.py:116-189), including `schema_version=1`, `success_rate=None` on empty window (reader.py:127-131), nearest-rank percentile computation, error_breakdown pre-populated with all six kinds at zero (reader.py:172-175).
- Line 104 `by_collection.total` can exceed `total_queries`: matches `schemas_telemetry.py:23-27` comment and `reader.py:158-169` fan-out logic. The doc's pointer to `CollectionStats` comment is correct.
- EntriesResponse (lines 121-130): fields and types match `schemas_telemetry.py:55-61` and `routes_telemetry.py:71-78`.
- Line 117 `offset >= 0`, `limit 1..200`, default 50: confirmed `routes_telemetry.py:50-51`.
- Line 132 pagination terminator (`next_offset >= total_in_window`): consistent with `routes_telemetry.py:75` (`next_offset = offset + len(page)`).
- Line 72 `DisabledResponse` shape `{"enabled": false}` when disabled: confirmed `schemas_telemetry.py:64-65` and `routes_telemetry.py:29-30, 53-55`.

## Unverifiable / ambiguous

- Line 50 collection union semantics for `/route` — see inaccuracy #5; needs cross-check against `routes_route.py` route-handler logic, not just telemetry module.
- Line 13 "Treat this as an accepted leak risk; see 'Path-derived `doc_id` risk' below" — the framing as "accepted risk for v1" is editorial; CLAUDE.md confirms this posture but it is policy, not code.
- Line 68 "A hashed-doc-id mode is on the roadmap" — not verified against `Documentation/roadmap.md` or `Backlog/` (out of scope: docs are not authoritative per review rules).
- Line 148 "Reducing `retention_days` causes files to be pruned on the next pass" — true in principle (`prune_once` recomputes `cutoff = now - timedelta(days=retention_days)` each call), but "next pass" is at most 24h later because the loop sleeps after pruning (pruner.py:67-70). The doc does not state this latency.
- Lines 137-143 curl examples — syntactic only; depend on `.search.env` shape, not in scope.

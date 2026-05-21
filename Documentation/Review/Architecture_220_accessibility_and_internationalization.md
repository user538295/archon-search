# Review: Architecture/220_accessibility_and_internationalization.md

## Summary

The document is largely accurate. The CLI is genuinely unstyled, errors do go to stderr via `click.echo(..., err=True)`, telemetry `error_kind` values match the source, and there is no locale/`Accept-Language` handling anywhere in the codebase. Two factual errors stand out:

1. The endpoint name for indexing-state polling is wrong (`/state` does not exist — it is `/indexing-state`).
2. The claim that error bodies from `server/middleware_auth.py` are JSON objects with English `detail` strings is incorrect — that middleware returns bare-body 401 responses (no body, no `detail`).
3. The description of how `export_enabled = true` is handled overstates the rejection ("refused", "rejected at config load") — it is logged as a warning and silently coerced to `False`, which matches the project's own CLAUDE.md text.

The rest of the claims (no styling, no `tqdm`, ISO 8601 UTC timestamps, English-only, closed set of `error_kind` values, no GUI) are verified.

## Inaccuracies (numbered)

1. **Line 30 — wrong endpoint name.** The doc says "clients … can poll … the HTTP `/state` and `/jobs` endpoints." There is no `/state` endpoint. `archon_search/server/routes_state.py:14` registers `@router.get("/indexing-state", …)`. `/jobs/{job_id}` does exist (`routes_jobs.py:108`).

2. **Line 36 — middleware error bodies are not JSON with `detail`.** The doc states: "Error responses from `server/middleware_auth.py` and the `routes_*.py` modules are JSON objects with English `detail` strings." `archon_search/server/middleware_auth.py:32-35` and `:50-53` construct `Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})` with no body at all. The `routes_*.py` modules do raise `HTTPException` with English `detail` strings (FastAPI then serialises those as JSON), so the claim is correct for routes but wrong for `middleware_auth.py`.

3. **Line 47 — overstated rejection of `export_enabled = true`.** The doc says it is "currently rejected at config load — `export_enabled = true` is refused". `archon_search/config.py:209-217` actually logs a warning ("telemetry: export_enabled is reserved for a future release and will be ignored") and silently sets `telemetry.export_enabled = False`. The config load does not raise or reject; it accepts the file and coerces the value. The project's own CLAUDE.md describes this as "logs a warning and silently coerces it to false", which contradicts the 220 doc's framing.

## Verified claims

- Line 9: three surfaces (Click CLI, FastAPI HTTP, MCP) — matches `archon_search/cli/main.py`, `server/app.py`, `server/mcp.py`.
- Line 14: English-only; no locale negotiation, no `Accept-Language` handling. `grep -rn "Accept-Language|accept_language|locale|gettext" archon_search/` returns no hits.
- Line 27: "zero uses of `click.style`, `click.secho`, `fg=`, `bold=`, or `tqdm`". Verified — `grep` across `archon_search/` returns no matches for any of these tokens.
- Line 28: errors routed to stderr via `click.echo(..., err=True)` in `cli/ingest.py`, `cli/config_cmd.py`, `cli/sync.py`, `cli/install_cmd.py` (and additionally `start.py`, `stop.py`, `status.py`, `collection.py`).
- Line 30 (partial): `IndexingStateStore` lives in `archon_search/progress.py:77`, writes `~/.archon-search/.indexing_state.json` (line 86). The class name and file path are correct.
- Line 41: telemetry `error_kind` closed set is exactly `empty_query`, `slot_out_of_range`, `timeout`, `internal_error`, `validation_error`, `other` — verified at `archon_search/telemetry/entry.py:31-37`.
- Line 42: ISO 8601 UTC timestamps — verified in `archon_search/telemetry/entry.py:82` (`datetime.now(UTC).isoformat().replace("+00:00", "Z")`) and `archon_search/progress.py:48,126,133,142` (`datetime.now(UTC).isoformat()`).
- Line 14 / Line 40: no message catalog, no gettext — verified (no `gettext`/`locale` imports anywhere in `archon_search/`).
- Line 9: CLI is Click-based — verified in `archon_search/cli/main.py` and subcommand modules.

## Unverifiable / ambiguous

- Line 29: "output is identical on a TTY and when redirected — there is no TTY-only rendering path to disable." The absence of styling makes this true in practice, but Click itself may perform line-buffering differences between TTY and pipe — this is a Click behaviour, not an archon-search behaviour, and the doc's claim is fair under a charitable reading.
- Line 32: "any screen reader … can consume the CLI without special handling." This is an inference, not a testable code claim; it is consistent with the plain-text output but cannot be "verified" against source.
- Line 42: "Timestamps are ISO 8601 in UTC (see `progress.py` and telemetry entries)." Verified for `progress.py` and `telemetry/entry.py`. Other timestamp sites in the codebase (e.g. job records, log messages) were not exhaustively audited; the doc's wording ("Timestamps are ISO 8601 in UTC") is a global claim that this review only partially confirms.
- Line 21: "The frontend a11y series (300–320) is not applicable to this project and is intentionally absent." The absence is verifiable (no such files exist) but "intentionally" is editorial.

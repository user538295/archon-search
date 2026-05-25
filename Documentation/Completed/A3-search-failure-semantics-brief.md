# Feature Brief: Search-Failure Semantics (A3 / CON-5)

> Ships BEFORE A4 (explain endpoint). A3 establishes the canonical pipeline-failure taxonomy (HTTP 500 for pipeline-stage exceptions, 503 reserved for meta-lookup); A4 will inherit it.

> **⚠️ Post-implementation deviation (2026-05-24):** This brief originally stated that bare re-raise yields FastAPI's default `{"detail": "Internal Server Error"}` JSON envelope. During implementation it was discovered that bare `raise` in a FastAPI handler is caught by Starlette's `ServerErrorMiddleware` (not FastAPI's exception handler), which returns a **plain-text** body `Internal Server Error` with `Content-Type: text/plain` — not a JSON envelope. The bare re-raise is still the correct implementation; only the description of the resulting HTTP body was wrong. All operational docs (`BREAKING.md`, `140_error_handling_strategy.md`, `600_api_reference_or_public_interface.md`, both DeveloperGuides) were updated to describe the actual plain-text body. The inline claims in this brief and the plan that mention `{"detail": "Internal Server Error"}` are stale planning text and should not be relied upon — see `BREAKING.md` for the authoritative contract.

## Problem
When `/search` pipeline stages (embedder, store, reranker) raise an exception, the route handler at `archon_search/server/routes_search.py:76-84` swallows the error, logs at WARNING, and returns HTTP 200 with `results=[]`. Clients and operators cannot distinguish "no hits" from "search is broken," which masks outages and corrupts downstream telemetry.

## Goal
A failing search pipeline returns HTTP 500 with the standard error envelope, emits an error telemetry entry, and is covered by tests that lock in the new behavior. Operators see real failures in logs and telemetry; clients can react to 5xx as a real outage.

## Users & Context
- **REST clients** (other services, IDE plugins, the CLI) calling `/search` during normal operation — they currently silently get empty results on outages and have no way to know.
- **MCP clients** — already receive a distinguishable `McpErrorResponse({"error": ..., "code": "internal_error"})` from `mcp.py` search / search_with_context tools (see `archon_search/server/mcp.py:60-74, 108-122`). Out of scope for this fix; REST-only. **Caveat**: the distinction is by payload shape only, not by MCP protocol-level error signaling — FastMCP returns the dict as a normal tool result, so clients must check the `error` key explicitly. The decision still stands, but the basis is weaker than a true protocol-level error envelope.
- **Operators** running archon-search who rely on telemetry (`/telemetry/stats`, `/telemetry/entries`) and logs to detect regressions — today, REST search outages disappear from their observability surface.
- **Blast radius**: archon-search is a single-user local server (per `Documentation/Architecture/100_*`). The contract change affects whoever wraps `/search`; expected impact is low. CalVer (`YY.M.<rev-count>`) means no semver-pinned consumers exist.

## Core Flow
1. Client sends `POST /search` with a valid query and collection.
2. Pipeline stage (embedder, LanceDB hybrid search, ACL filter, or reranker) raises an exception.
3. Route handler logs the failure at ERROR (not WARNING) with a structured `event_type="search_pipeline_failure"` field so operators can alert precisely, then enqueues a `TelemetryEntry.from_error(...)` with `endpoint="search"`, `status="internal_error"` (the `Status` StrEnum value — there is no integer status member), `error_kind="other"` (matching `routes_route.py:159` for symmetry), and the measured latency.
4. Route handler bare-re-raises the exception (matching `routes_route.py:166`); Starlette's `ServerErrorMiddleware` intercepts the unhandled exception and returns HTTP 500 with a **plain-text** body `Internal Server Error` (Content-Type: `text/plain`) — not a JSON envelope. *(The brief originally claimed a JSON envelope; see implementation note above.)*
5. Client observes 5xx and applies its own retry/escalation policy.
6. Operator sees the failure in `/telemetry/entries` and in the structured log.

## In Scope
- Replace the silent `except: return empty` block in `routes_search.py:76-84` with a bare re-raise (symmetric with `routes_route.py:166`) that propagates as HTTP 500.
- Add a telemetry enqueue on the failure path mirroring `routes_route.py:152-164`'s pattern (`status="internal_error"`, `error_kind="other"`).
- Bump log level for pipeline failures from WARNING to ERROR, with a structured `event_type="search_pipeline_failure"` field.
- Flip the four existing regression tests in `tests/server/test_routes_search.py` that assert the old 200+[] behavior — line 140 (`test_search_pipeline_error_returns_empty`), 262 (`test_search_store_exception_returns_empty`), 299 (`test_search_embedder_failure_returns_empty`), 315 (`test_search_reranker_failure_returns_empty`) — to expect HTTP 500 and the FastAPI default `{"detail": ...}` envelope. **Do not touch** line 386 (`test_search_store_exception_returns_503`) — that is the meta-lookup failure path (`routes_search.py:68-71`) and stays 503.
- Add a `BREAKING.md` entry describing the contract change.
- Update `Documentation/Architecture/140_error_handling_strategy.md` to remove the "silent failure masking" caveat (line 16, 82-84) once the fix lands.
- Update `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` CON-5 entry to mark resolved.
- Update `Documentation/OperatorGuide/05_incident_runbook.md` "Search returns empty (silent regression — CON-5)" runbook entry — failure mode no longer presents that way.

## Out of Scope
- **MCP `search` / `search_with_context` parity changes.** Reason: both tools already return `McpErrorResponse({"error": ..., "code": "internal_error"})` on failure (`mcp.py:60-74, 108-122`) — distinguishable by payload shape, not silent 200+[]. (Caveat: this is not MCP protocol-level error signaling; FastMCP returns the dict as a normal tool result and clients must check the `error` key.) No fix required; tracked only if a future audit shows consumers misreading it as success.
- **Granular HTTP status per failure type (503 for transient embedder/store, 500 for unexpected).** Reason: requires typed exceptions in `pipeline.py`; bigger refactor, deferred to a future ADR.
- **Partial/degraded responses (e.g., return un-reranked candidates if only reranker fails).** Reason: spec asks for hard 5xx, and partial success would require new schema fields.
- **A new `search_failure` value on the telemetry `ErrorKind` enum.** Reason: `internal_error` is sufficient; enum extension expands API surface for marginal observability gain.
- **Fixing the stale-router-cache race (CON-2).** Reason: separate concern, separate roadmap item.
- **Changing `SearchResponse` schema.** Reason: failure is signalled by HTTP status alone; no field changes needed.

## Key Decisions
- **HTTP 500 via bare re-raise, not explicit `HTTPException(500, detail=...)`**: Matches `routes_route.py:166` exactly. Produces plain-text `Internal Server Error` from Starlette's `ServerErrorMiddleware` (not a JSON envelope — see implementation note above). Symmetric pattern across both endpoints; no custom envelope to maintain.
- **`status="internal_error"`, `error_kind="other"`**: Matches `routes_route.py:159` (the `error_kind` reference) for symmetry. Telemetry consumers filtering on `status="internal_error"` already get both endpoints. (`status` is the `Status` StrEnum — `ok`/`validation_error`/`timeout`/`internal_error`; there is no integer member.)
- **Avoid 503 for pipeline errors**: Reserved for the existing meta-lookup failure path at `routes_search.py:68-71`. Keeping 500 vs 503 distinct preserves the "metadata unreachable" vs "pipeline broke" distinction for operators.
- **Three-stage test coverage (store + embedder + reranker)**: Spec asks for a store-failure test; we extend by symmetry to all three pipeline stages currently unguarded. Timeout and ACL paths deferred (no current evidence they're load-bearing failure modes).
- **BREAKING.md + release-notes announcement**: Project convention per `CLAUDE.md`; no feature flag (would leave dead config behind).

## Edge Cases & Constraints
- **Meta lookup failure still returns 503** (existing behavior at `routes_search.py:86-90`) — unchanged. Clients see 503 for "collection metadata unreachable" and 500 for "pipeline broke." Note: that 503 path currently still lacks a telemetry enqueue; tracked separately, out of scope here. **Telemetry asymmetry post-fix**: after this change the 500 (pipeline) path emits telemetry but the 503 (meta-lookup) path still does not — operators filtering search-endpoint errors via `/telemetry/entries` will see pipeline failures but not meta-lookup failures until the 503 path is also instrumented.
- **Empty result with a healthy pipeline** still returns HTTP 200 with `results=[]`. The fix only affects the *exception* path.
- **ACL filter failure → 500 (fail-closed)**: if ACL filtering raises, returning 500 rather than potentially leaking unfiltered results is the conservative, correct choice.
- **`asyncio.CancelledError` is a `BaseException`**, not `Exception`. The new `except Exception` block does not catch it — cancellation behavior is unchanged.
- **Telemetry must not log the raw query** — `TelemetryEntry.from_error(...)` already honors this structural invariant; no new risk.
- **Log level bump (WARNING → ERROR)** may surface failures that were previously tolerated as noise. Acceptable: an operator should *want* to see these.
- **No retry logic added** in the route — clients own retry policy.

## Open Questions
- None blocking. `TelemetryEntry.from_error(...)` already accepts `endpoint="search"` and `error_kind="other"`/`internal_error` (verified in `archon_search/telemetry/entry.py` — `EndpointKind.search`, `ErrorKind.other`, `Status.internal_error` all defined).

## Test Scope (concrete assertions)

**File split**: the existing `tests/server/test_routes_search.py` uses `_make_app` via `create_app()` and has no telemetry-writer plumbing, while `tests/server/test_routes_route_telemetry.py` builds a separate minimal app with `app.state.telemetry_writer = writer_mock`. Mirror that pattern: the four flipped tests in `tests/server/test_routes_search.py` assert **status code + envelope only** (no telemetry/log assertions), and all telemetry / log-record assertions live in a **new file** `tests/server/test_routes_search_telemetry.py` patterned after `test_routes_route_telemetry.py`.

Flipped tests in `tests/server/test_routes_search.py` must assert:
- `response.status_code == 500` only. **Do not call `response.json()`** — the body is plain-text `Internal Server Error` from Starlette's `ServerErrorMiddleware`, not a JSON envelope. *(The brief originally required a JSON assertion; the implementation delivers plain text — see implementation note above.)*

New `tests/server/test_routes_search_telemetry.py` must assert (approximately 10 telemetry tests in total, covering per-stage failures plus serialization error, writer=None, and privacy-sentinel cases — see the A3 plan for the full enumerated list). Per failure stage — store, embedder, reranker:
- The telemetry writer was called once with `endpoint="search"`, `status="internal_error"`, `error_kind="other"`, and `latency_ms > 0`.
- Log record at `ERROR` level (logger `archon.search`) carries `event_type="search_pipeline_failure"`. **Assertion mechanism**: the implementer uses `logger.error(msg, extra={"event_type": "search_pipeline_failure"})` and tests assert via `record.event_type` on the captured `LogRecord` — not a substring match on the formatted message.
- **Telemetry-enqueue-failure resilience**: one test where the writer's enqueue raises — the route must still return HTTP 500 (mirrors `tests/server/test_routes_route_telemetry.py:296/318/338`).

Additional shared test (placement: `test_routes_search_telemetry.py`):
- Sequential failure-then-success on the same app instance — request N fails (500), request N+1 succeeds (200) — to confirm no state leaks between requests.

After all edits, `uv run pytest` must pass with `--cov-fail-under=85` (default `addopts`) still satisfied.

## Future Iterations
- **Typed pipeline exceptions** (`EmbedderError`, `StoreError`, `RerankerError`) with granular HTTP status mapping (503 for transient, 500 for unexpected). Enables smarter client retry.
- **`search_failure` ErrorKind** if telemetry consumers grow and need a clean filter.
- **Graceful degradation mode** (e.g., return un-reranked candidates with a `degraded: true` flag) as an opt-in client feature.
- **Resolution of CON-2** (stale router cache) — separate hardening item.

## Recommendation
Ship this exactly as scoped. It's a small code change in `routes_search.py` (bare re-raise + telemetry enqueue + log-level bump, ~10 lines mirroring `routes_route.py:152-166`), four test flips plus one new sequential test, a `BREAKING.md` line, and three doc edits — small surface, high observability payoff. The hardest part is *not* expanding scope: resist the urge to add typed exceptions, MCP changes, or degraded-response modes in the same PR. The one thing that must not be compromised is the **structural no-raw-query invariant** in telemetry on the new error path; verify in code review that the error enqueue passes through `TelemetryEntry.from_error(...)` rather than constructing entries by hand.

*Minor follow-up*: `tests/eval/corpus/docs/troubleshooting.md:13` documents the old "search returns empty on failure" symptom. Out of scope here (eval corpus is fixture data, not user-facing docs), but flag for a future sweep.

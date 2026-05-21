# Feature Brief: Metadata Filters at Search (A2, minimum slice)

## Problem
Users can search a collection by query text but cannot narrow results to a subset (e.g. "only Markdown files", "only files indexed this week", "only `/docs/api/**`"). With collections growing past a few thousand chunks, top-k results get diluted by irrelevant file types or stale documents, and there is no escape hatch short of building a new collection.

## Goal
A `/search` (REST) and `search` / `search_with_context` (MCP) request can specify metadata filters that are pushed down to LanceDB via string-built `.where()` predicates assembled through centralized escape helpers (`_sql_quote_str` + `escape_like`), with a Python `fnmatch` fallback for glob. Hits are constrained server-side, every returned hit carries the metadata fields it was filtered on, and invalid bounds are rejected at the API boundary.

## Users & Context
- **Power users / agents** running multi-collection routed search and needing to scope to a file-type or path subtree.
- **Operators** debugging ingest freshness ("did the new docs make it in?") via `indexed_after`.
- **MCP clients** (LLM tool calls) that want to bias retrieval toward a known doc family without spawning a new collection.

All three call the same code path; success is measured by filter correctness, not UI polish.

## A1 Prerequisite — IMPORTANT
A1 widened `SearchResult` with `file_type, indexed_at, updated_at, ingested_by, metadata` and typed `ingested_by` as the `IngestedBy` Literal. A2 adds the `language: str | None = None` field to `SearchResult` (deferred from A1) and owns all filter-related additions. A2 does NOT re-widen the response schema.

## Core Flow
1. Caller issues `POST /search` (or MCP `search`) with `query`, `collection`, `top_k` plus any of: `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, `language`, `include_metadata`.
2. Pydantic validation: each filter checked at API boundary (see Validation section); `language` non-empty rejected with HTTP 422 referencing C2.
3. Server passes a `SearchFilters` **Pydantic model** to `SearchPipeline.search`, which forwards to `LanceStore.hybrid_search`.
4. Store builds a string predicate (see Predicate section) via Python-side escaping helpers (`_sql_quote_str` for value quoting, `escape_like` for LIKE pattern metachars) and calls `.where(pred)` on **both** the vector and FTS branches. LanceDB's async path prefilters by default — there is no `prefilter` kwarg to pass — so pushdown is automatic; the opt-out would be `.postfilter()`, which A2 does not use.
5. RRF fuses the filtered candidate sets.
6. **If `source_path_glob` is set**: store applies `fnmatch.fnmatchcase` post-RRF, then re-truncates to `top_k_retrieve`. The store over-fetches once at the LanceDB layer when glob is active (see Over-fetch section); this **replaces** the existing 3× factor, it does not compound with it.
7. **ACL filter** (`pipeline.py:302`) runs on the already-filtered candidates.
8. **Reranker** runs on ACL-filtered candidates.
9. Response is `SearchResponse` with each hit carrying `file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`, plus `metadata` (custom dict) only when `include_metadata=true`.

## In Scope
- **Response widening (A1 follow-on)**: extend `SearchResult`, `hybrid_search` row projection, `SearchResultSchema`, MCP return shape.
- **Shared `SearchFilters` Pydantic model** in `archon_search/filters.py` (NEW; revised during planning — keeping Pydantic out of `_types.py` preserves the dataclass-only import graph for core modules and 40+ test files) — **NOT in `archon_search/server/`**. Core modules (`store.py`, `pipeline.py`) must not import from the server layer; placing the model in core preserves the existing inward-only dependency arrow. Imported by both `routes_search.py` and `mcp.py`. Single source of truth for validation; no inline param duplication in MCP.
- REST `SearchRequest` extended with embedded `filters: SearchFilters | None`.
- MCP `search` and `search_with_context` tools accept the same filter fields (typed kwargs that hydrate `SearchFilters`).
- `SearchPipeline.search` and `LanceStore.hybrid_search` accept an optional `filters: SearchFilters | None`.
- **Predicate builder** (`archon_search/store_filters.py`): pure helper that compiles `SearchFilters` to a complete LanceDB `where` string. **Note (revised during planning):** LanceDB's async `.where()` accepts a string predicate or an `Expr` tree — there is NO `?`-placeholder parameterization API and NO `prefilter` kwarg in the async path (prefiltering is the default; `.postfilter()` is the opt-out). The plan therefore returns a single SQL string built via centralized escape helpers (`_sql_quote_str` for string literals — wraps in `'…'` and doubles internal `'`; `escape_like` for `%`, `_`, `\`). No f-string SQL anywhere outside `build_where`. See `A2-metadata-filters-at-search-plan.md` for the canonical design.
- **DataFusion `LIKE` escape**: predicate builder emits `source_path LIKE '<escaped>%' ESCAPE '\'` as a string-built SQL fragment, with literal `%`, `_`, `\` in the user-supplied prefix escaped via `escape_like`, then the whole literal quoted via `_sql_quote_str`. No `?` placeholders — LanceDB's async `.where()` does not accept them. Verified against the actual installed LanceDB Python API surface, not assumed.
- **Datetime safety** (see Datetime Strategy section).
- **`fnmatch` post-filter** for `source_path_glob` with a defined over-fetch policy and a warn-log on under-delivery.
- **Filter-aware over-fetch policy** (see Over-fetch section) — single source of truth, not a magic constant scattered around the code.
- **Test coverage** (see Testing section): unit, integration (real LanceDB), property test for `LIKE` escape, contract test for REST↔MCP filter parity, no-result 200 test, ACL × filter test matrix, benchmark-marker test for glob over-fetch latency.
- **Telemetry**: extend `TelemetryEntry` (`archon_search/telemetry/entry.py`) with `filter_flags: FilterFlags` where `FilterFlags` is a typed Pydantic submodel (matches plan; the plan defines its fields). Booleans only, not values; `extra="forbid"`. No raw filter values logged (`source_path_prefix` may reveal filesystem layout).
- **Doc updates**: `Architecture/600_api_reference_or_public_interface.md`, `Architecture/130_data_architecture_and_persistence.md`, `Architecture/520_api_design_and_contracts.md`, OpenAPI regeneration, `BREAKING.md` entry **scoped to MCP only** (see Backwards-Compatibility section).

## Out of Scope
- **`language` filter actually matching** — A1 stores `""`. A2 reserves the parameter and rejects non-empty values with HTTP 422. Real behavior arrives in C2.
- **A4 explain endpoint** — separate task; A2 returns the metadata fields raw without an explain wrapper.
- **Custom `metadata.*` filtering** (e.g. `metadata.author=...`) — out of scope.
- **OR / NOT / nested boolean predicates** — A2 is implicit AND across filters.
- **Cross-collection / router-aware filters** — see Routing Limitation section; explicitly deferred.
- **Scalar indexes on `file_type` / `indexed_at`** — at current scale (few-thousand chunks per collection) full-scan `.where()` is acceptable; revisit if benchmark trips. Documented as accepted risk.
- **`include_metadata` as field-projection list** — boolean is sufficient for v1.
- **CLI surface for filtered search** — not part of A2.

## Key Decisions

### `language` is reserved with HTTP 422
A1 stores empty strings; full detection is C2. A2 rejects non-empty `language` loudly. One-line removal when C2 lands. OpenAPI description **must say "reserved; rejected with 422 until C2"** so spec readers are not misled.

### Two source-path params (`prefix` + `glob`)
`source_path_prefix` uses fast `LIKE` pushdown; `source_path_glob` uses Python `fnmatch.fnmatchcase` with over-fetch. Decision held from refinement: speed/power tradeoff is explicit; users opt into the slow path knowingly. Glob discrepancy with SQL `LIKE` (especially `**` semantics) is the reason both exist.

### `include_metadata` defaults to `false`
Custom metadata dict can reach ~200KB per chunk (50 × 4096 chars). Always-on is pathological at high `top_k`. System metadata (`file_type`, `indexed_at`, `updated_at`, `ingested_by`, `language`) is always present — these are tiny and necessary to verify filter behavior.

### `SearchFilters` is a Pydantic model, not a dataclass
FastAPI needs `@field_validator` for cross-field rules (`indexed_after <= indexed_before`, ISO-8601 parsing, empty-string rejection, leading-dot normalization on `file_type`). A plain dataclass cannot carry these. A shared Pydantic model is also the natural seam for REST↔MCP unification and prevents schema drift.

### Predicates built via string-built `.where()` with Python-side quoting + explicit LIKE `ESCAPE`
No f-string SQL, even for `source_path_prefix`. All value interpolation goes through `_sql_quote_str` (wraps in `'…'`, doubles internal `'`); LIKE patterns additionally go through `escape_like` for `%`, `_`, `\`, and the SQL form is `LIKE '<escaped>%' ESCAPE '\'` (DataFusion). LanceDB's async `.where()` has no `?`-placeholder API, so defense-in-depth against SQL injection lives entirely in these helpers. Verified in plan; not assumed.

### Datetime Strategy: dual-storage normalization + post-filter fallback
`indexed_at` and `updated_at` are stored as `pa.utf8()` ISO strings. Variable-precision strings (`...:00Z` vs `...:00.123456Z`) sort incorrectly because `Z` > `.` in ASCII. A2 fixes this:
- **Storage normalization**: all newly written `indexed_at` / `updated_at` use a fixed-width ISO form: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (6-digit microseconds, always `Z`). A small helper centralizes formatting in `_types.py` / store ingestion path.
- **Backfill consideration**: pre-A2 rows may have variable-width values. A2 documents this and provides a one-off `reindex --normalize-timestamps` CLI flag (or piggybacks on the existing `reindex-metadata` command added in A1). Until backfilled, datetime filters may have silent boundary errors on legacy rows — documented in `BREAKING.md`.
- **API-layer normalization**: user-supplied `indexed_after` / `indexed_before` are parsed as `datetime` by Pydantic, then re-emitted in the same fixed-width form before being quoted via `_sql_quote_str` and embedded in the `.where()` predicate string. Apples-to-apples string comparison is then safe.

### Over-fetch policy
`hybrid_search` already uses `fetch = max(top_k * 3, 20)` (`store.py:471`). When `source_path_glob` is active, store **replaces** the multiplier: `fetch = max(top_k * GLOB_OVERFETCH_FACTOR, 60)` with `GLOB_OVERFETCH_FACTOR = 5`. The two multipliers never compound. The constant lives in `store.py` with a comment explaining it; rationale is testable via a benchmark-marker test, not by intuition.

**Attrition stack acknowledgement**: the over-fetch factor must absorb glob attrition AND downstream ACL attrition (ACL filter runs after glob in `pipeline.py:302`). In ACL-heavy deployments (user sees a small fraction of chunks), the under-delivery WARNING log is the operator's signal that `GLOB_OVERFETCH_FACTOR` is too low for their corpus. Tuning is operational, not hardcoded; the constant is sized for typical (low-ACL-attrition) deployments.

### Backwards-Compatibility scope
- **REST**: adding new optional response fields (`file_type`, `indexed_at`, etc.) is **additive and non-breaking** for any conforming JSON consumer. No `BREAKING.md` entry for REST.
- **MCP**: tool schemas change (new optional input params + new output fields). For strict MCP clients this is a schema change → **`BREAKING.md` entry scoped to MCP only**, with a note that all new fields are optional.
- **Datetime normalization**: writes change format; reads compatible with old format only when explicit reindex is run. Documented in `BREAKING.md` as an operational note (not a hard break, but an awareness item).

### Routing Limitation (explicit)
Routed multi-collection search (`POST /route` → per-collection `POST /search`) does NOT benefit from filter-aware collection elimination in A2. A query with `file_type=md` will still be sent to all router-selected collections, including those with zero `.md` chunks, wasting latency. This is an accepted limitation; a future task (router filter-awareness) is captured in Future Iterations.

## Validation
At the Pydantic layer (`SearchFilters`):
- `file_type`: strip leading dot if present (`".md"` → `"md"`); reject empty string (distinct from omitted) with 422; lowercase.
- `source_path_prefix`: reject empty string with 422; otherwise free-form (escaped at predicate-build time).
- `source_path_glob`: reject empty string with 422; otherwise free-form (validated as compilable by `fnmatch.translate` in the validator).
- `indexed_after` / `indexed_before`: ISO-8601 parse; naive treated as UTC; cross-field `indexed_after <= indexed_before` with 422 otherwise. **Inclusive bounds** on both ends (`>=` and `<=`). **Date-only inputs** (`2026-05-21`) are coerced: `indexed_after=DATE` → start-of-day `DATEt00:00:00.000000Z`; `indexed_before=DATE` → end-of-day `DATET23:59:59.999999Z`. Without this coercion `indexed_before=2026-05-21` would exclude everything indexed on that day — a UX trap. OpenAPI description states the inclusive + date-coercion behavior.
- `language`: any non-empty value rejected with 422 (`"language filtering not yet supported (see C2)"`).
- `include_metadata`: standard bool.

## Edge Cases & Constraints
- **`indexed_after > indexed_before`**: HTTP 422 at validation, before the store.
- **Naive vs aware datetimes**: API accepts ISO-8601; naive input assumed UTC, documented. Stored values fixed-width per Datetime Strategy.
- **Empty-string filters**: rejected with 422 (distinct from omitted).
- **Special chars in `source_path_prefix`**: `%`, `_`, `\` escaped via `escape_like` then the whole literal quoted via `_sql_quote_str` before being concatenated into the predicate string; SQL form is `LIKE '<escaped>%' ESCAPE '\'`.
- **`file_type` leading dot**: normalized away by Pydantic validator; `".md"` and `"md"` are equivalent inputs.
- **Glob over-fetch under-delivery**: if `fnmatch` shrinks the post-RRF pool below `top_k`, store emits a `WARNING` log (`"glob post-filter shrank pool below top_k: <N>/<top_k>"`); test asserts this via `caplog`.
- **Glob position in pipeline**: post-RRF, **before** ACL filter, **before** reranker. Documented in the flow. Rationale: glob is a hard exclusion (like ACL); deferring it past reranker wastes reranker work.
- **MCP schema drift**: REST and MCP share `SearchFilters`; **contract test** asserts the MCP tool input schema names are a superset of `SearchFilters` model fields. Without this test, the claim of shared schemas is aspirational.
- **ACL × filter ordering**: filters apply before ACL (LanceDB pushdown), ACL applied after (Python). `acl_filtered` flag remains accurate. Tested explicitly.
- **No-result responses**: `200 + results=[]` when filters exclude everything (collection exists). Distinct from 422 (invalid filter) and 404 (unknown collection). Explicit test required.
- **Telemetry**: extend `TelemetryEntry` (model has `extra="forbid"`) with `filter_flags: FilterFlags` (typed Pydantic submodel — see plan for fields) — booleans only, not values. `source_path_prefix` values may leak filesystem layout and are NOT logged. The no-raw-query invariant on `query` remains; a negative test asserts the factory still rejects a `query` kwarg.
- **`metadata` field always present in `ChunkRecord` but only in response when `include_metadata=true`**.
- **Scalar indexes**: not created in A2. Risk: full-scan `.where()` at scale. Mitigation: benchmark-marker test exercises filtered search at the upper end of expected collection size (3K–10K chunks) and asserts latency stays within the existing routing latency budget. If it doesn't, A2 reverts to documenting "add scalar index" as a follow-up.

## Testing
Project mandates TDD (CLAUDE.md). Implementation plan must enforce test-first ordering for each task below.

### Unit (default suite)
- `SearchFilters` Pydantic validation: every rejection path returns 422; every normalization (file_type lowercasing, leading-dot strip, datetime UTC coercion).
- Predicate builder: correctly-escaped string-built `.where()` predicate, escape correctness (hand-picked cases).
- LIKE-escape function: **property test** (Hypothesis) — `forall s, escape_like(s)` produces a pattern that, when quoted via `_sql_quote_str` and used as `LIKE '<escaped>' ESCAPE '\'`, matches only `s` under DataFusion `LIKE` semantics.
- `fnmatch` glob behavior: `*`, `?`, character classes. **Note**: Python's `fnmatch.fnmatchcase` does NOT treat `**` as recursive-directory glob — it behaves the same as `*` and does not cross `/` boundaries any differently. A2 uses `fnmatchcase` as documented; users wanting true recursive `**` matching are documented to use `source_path_prefix` for the parent and `source_path_glob` for the suffix, or wait for a future iteration that swaps in `pathlib.PurePosixPath.match` semantics. Tests cover this distinction explicitly so the limitation is enforced, not accidental.
- Glob under-delivery warn-log: `caplog` assertion.
- Datetime normalization helper: round-trip equality for `datetime` ↔ fixed-width string.

### Integration (`-m integration`)
- Real LanceDB `.where()` round-trip: insert chunks with known metadata, query with each filter individually and in combination, assert correct rows returned.
- Prefilter correctness (LanceDB async `.where()` prefilters by default): a filter that matches a small fraction of rows still returns full `top_k` from the matching subset (not zero).
- Empty-result 200: collection exists, filters exclude everything → `status=200`, `results=[]`.
- ACL × filter matrix: **all 12 cases** `{no_filter, prefix, glob, date_range} × {no_acl, acl_match, acl_deny}` — assert correct combination of filtered + ACL-filtered results and that `acl_filtered` flag is correct. (The prior "minimum 8" was undercounted; the matrix has 12 cells and `glob+acl_deny` / `date_range+acl_deny` are the highest-risk ordering bugs.)
- **Datetime backfill correctness**: seed a collection with mixed-format rows (`...:00Z` and `...:00.123456Z`), assert `indexed_after` returns the expected superset under raw string compare (i.e. reproduces the silent-drop bug), then run the `reindex --normalize-timestamps` backfill and assert the same query returns the correct set. This is the "hardest part" of A2 per the Recommendation section and must have an explicit failing-then-passing test.
- **Mixed-storage transition window**: query a collection with mixed-format rows BEFORE backfill runs, assert the query returns correct results for new-format rows and emits a WARNING log naming the legacy-format row count. Closes the silent-failure gap during the deploy/backfill window.
- **`include_metadata=false` suppression** (negative test): assert response has no `metadata` key when the flag is false/omitted, and has the dict when true.

### Contract
- REST↔MCP parity: assert MCP `search` and `search_with_context` accept all field names declared in `SearchFilters`. Test the MCP tool's published schema, not the function signature.

### Benchmark (`-m benchmark`)
- Glob over-fetch latency: synthetic 10K-chunk collection, glob filter that matches ~20%. Filtered `/search` is a different code path from `/route`, so the routing budget in `Architecture/210_performance_and_scalability.md` (p95 ≤ 150ms for `/route`) is **not directly reusable**. A2 establishes its own ceiling: **p95 ≤ 250ms for filtered `/search` over HTTP at top_k=10 against the synthetic 10K-chunk corpus** (100 iterations, localhost, in-process LanceDB). The ceiling is recorded in `tests/eval/thresholds.toml` under a new `[search_filtered]` section so it can be tuned without code changes. If the first benchmark run shows this is wrong, the value is amended in the same PR — but it must be a concrete number, not a hand-wave.

- **Baseline comparison**: a second benchmark run with `source_path_prefix` only (no glob) asserts p95 has not regressed by more than 10% vs the existing unfiltered hybrid_search benchmark. This catches default-prefilter overhead and `LIKE '<escaped>%' ESCAPE '\'` overhead.

### Eval (`-m eval`)
- Baseline run **with no filters** must produce metrics identical to current baseline (filters are additive; default code path must not regress).
- Add a comment in `tests/eval/README.md` noting `language` filter is reserved/rejected in A2 so future eval fixtures don't accidentally use it.

## Open Questions
- `GLOB_OVERFETCH_FACTOR = 5` — confirm via benchmark; if recall is poor for highly selective globs, raise; if latency trips, lower and accept under-delivery warning.
- Backfill UX: extend the existing `reindex-metadata` CLI command (A1) to also normalize timestamps, or add a separate flag? Lean toward extending the existing command (one operational primitive).
- Should the no-collection-found case for routed multi-collection search expose filter-aware hints in the response (e.g. "0/3 routed collections contained `file_type=md`")? Likely a Future Iteration alongside router filter-awareness.

## Future Iterations
- A4 explain output: echoes matched filters; shows pushdown vs post-filter breakdown; reports glob under-delivery counts.
- Custom `metadata.<key>` filtering (typed predicates against the JSON column).
- OR / NOT / nested boolean predicates.
- Router filter-awareness: pre-eliminate collections at `/route` based on `file_type` / `source_path_prefix` against collection-level summaries (requires new collection metadata).
- CLI flags mirroring the REST params.
- Scalar indexes on `file_type` and `indexed_at` once benchmark indicates need.
- `include_metadata` as field-projection list.
- `language` filter actually working — C2 (multilingual).

## Recommendation
This is the right next thing to build, and the fixes above turn it from a "minimum slice" with three latent bugs (silent date sort errors, missing FTS prefilter, ungrown `SearchResult`) into a hardened minimum slice. The hardest part is the **datetime safety strategy**: storage normalization + API-layer normalization + a one-off backfill is more work than the rest of the feature combined, but the alternative — string-comparing variable-precision ISO timestamps — silently drops valid results in production. Do not compromise on three things: (1) string-built `.where()` predicates assembled exclusively through `_sql_quote_str` + `escape_like` with an explicit `ESCAPE '\'` in every `LIKE`, (2) relying on LanceDB async `.where()`'s default prefilter behavior on both vector and FTS branches (no `.postfilter()` opt-out), (3) the REST↔MCP shared `SearchFilters` Pydantic model. The first two are correctness; the third prevents this feature from accreting drift on every future addition.

# C5 — RAG Fusion / Multi-Query Decomposition

**Purpose**: Improve recall on multi-faceted queries by decomposing them into N semantic variants via LLM, searching with each variant in parallel, and fusing the ranked results via a second-pass RRF — measured by the eval harness before merge.
**Audience**: archon-search contributors implementing C5; operators who opt in via `rag_fusion=true`.
**Status**: To Do

---

## Background

Single-query search fails when a user's query is multi-faceted or expressible in several ways — the best result may only surface with a different phrasing. RAG Fusion decomposes the query into semantic variants, runs parallel searches, and fuses the ranked result sets via Reciprocal Rank Fusion. C4 HyDE provides the blueprint: optional Anthropic API dependency, per-process rate limiter, kill-switch config, silent fallback, no-raw-query logging invariant, and telemetry fingerprinting.

The full design, key decisions, edge cases, and privacy analysis are in `Documentation/Backlog/C5-rag-fusion-brief.md`. This plan operationalizes it.

**Privacy note**: RAG Fusion sends the user's raw query to Anthropic's API to generate variants. Operators who cannot allow this must keep `[rag_fusion] enabled = false`. An ADR documenting the privacy and architectural decisions is required before Phase 2 implementation; it is written as Task 7.1 but must be completed before merging any code that calls the Anthropic API. A placeholder stub may be committed in Task 1.2 and finalized in Task 7.1.

---

## Goal

After C5 ships: a caller who sends `rag_fusion=true` on any search or explain request gets a response where the original query plus N LLM-generated semantic variants are searched in parallel and fused via second-pass RRF. If the LLM call fails for any reason, the server falls back silently to original single-query search — `rag_fusion_applied: false` in the response. Callers who omit `rag_fusion` pay zero overhead. `rag_fusion=true` with `hyde=true` returns `rag_fusion_applied: true, hyde_applied: false` (RAG Fusion wins mutual exclusion).

---

## Scope

### In Scope
- `RAGFusionConfig` dataclass (`enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`, `num_queries`) + `[rag_fusion]` TOML section loader in `config.py`
- `archon_search/rag_fusion.py` — `RAGFusionGenerator` class: async Anthropic client, per-process token-bucket rate limiter, `generate_variants(query) -> list[str]` with validation (≤500 chars each, plain text, no control sequences), silent fallback on all error paths
- `_fuse_rag_fusion_results(variant_results: list[list[ScoredSearchCandidate]], k: int = 60) -> list[ScoredSearchCandidate]` in `pipeline.py` — cross-variant RRF, deduplication by chunk_id, multi-contribution boosting
- `pipeline.search()`, `pipeline.search_many()`, `pipeline.explain()`, and `pipeline.search_with_context()` gain `rag_fusion: bool = False`, `rag_fusion_generator: RAGFusionGenerator | None = None`, `rag_fusion_config: RAGFusionConfig | None = None` parameters; orchestration lives inside these methods (`search_with_context` delegates to `search()`)
- `SearchPipelineResult` gains `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, `rag_fusion_attempted: bool = False`
- `ExplainPipelineResult` gains `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, `rag_fusion_attempted: bool = False`, `rag_fusion_failure_reason: str | None = None`, `rag_fusion_sub_query_results: list[RagFusionSubQueryResult] | None = None`
- Mutual exclusion with HyDE at route/MCP handler level: if `rag_fusion=true`, skip `resolve_hyde_vector()` call and pass `hyde_vector=None, hyde_applied=False`
- Request/response schema fields: `rag_fusion: bool` on `SearchRequest` / `ExplainRequest`; `rag_fusion_applied: bool`, `rag_fusion_queries_used: int` on `SearchResponse`; five new fields + `RagFusionSubQueryResult` model on `ExplainResponse`
- Telemetry: `rag_fusion_applied: bool | None` and `rag_fusion_queries_used: int | None` on `TelemetryEntry`; factory method optional kwargs — no query or variant text
- CI static-analysis guard (`tests/test_no_query_log_in_rag_fusion.py`) — extends the C4 guard pattern to cover `archon_search/rag_fusion.py`
- `RAGFusionGenerator` initialization in `app.py:create_app()`, stored on `app.state.rag_fusion_generator`; passed to `create_mcp_app()`
- REST wiring: `routes_search.py`, `routes_explain.py`
- MCP wiring: `search`, `search_with_context`, `explain` tools in `mcp.py`
- `rag_fusion = ["anthropic>=0.40"]` in `[project.optional-dependencies]` (separate from `hyde` for clarity, same package)
- `archon-search.toml.example` update for `[rag_fusion]` section with shared-rate-limit operational note
- `BREAKING.md` entries for all new response/MCP-return fields
- Eval harness: `[search_rag_fusion_disabled]` and `[search_rag_fusion_enabled]` latency scenarios in `thresholds.toml`; RAG Fusion recall regression scenario in `test_eval_suite.py`
- Integration tests: HyDE+RAG Fusion mutual exclusion on REST and MCP; partial fusion (0 variants from generator); FTS-only collection guard
- ADR: external LLM dependency, privacy, HyDE mutual exclusion, shared rate-limit operational risk
- `Documentation/UserManual/` operator guide update: RAG Fusion section

### Out of Scope
- Heuristic/rule-based decomposition
- Additive HyDE + RAG Fusion combination
- Distributed rate limiting
- UI/dashboard for sub-query inspection
- Per-request `num_queries` override
- Per-sub-query explain summary view only (full result sets shipped from the start)

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- `SearchPipeline.search()` call sites that do NOT pass `rag_fusion` — all default to `False` (no behaviour change)
- HyDE code paths — `hyde.py` receives a minor import change in Task 1.2 (replacing its local `_query_fingerprint` definition with `from archon_search._privacy import _query_fingerprint`); all HyDE logic and behavior is otherwise untouched
- FTS leg — always receives the original `query` string regardless of `rag_fusion`
- Reranker — always receives the original `query` string; runs on the final fused result set, not per-variant
- `TelemetryEntry` factory methods — no `query` or variant-text parameter added (structural invariant preserved)
- `key_manager.py` and `ARCHON_SEARCH_API_KEY` — unrelated to `ANTHROPIC_API_KEY`
- `description_generator.py` — uses claude-agent-sdk, untouched
- LanceDB schema
- All existing tests
- `query_vector` parameter: when `rag_fusion=True`, any caller-supplied `query_vector` is ignored with a WARNING (pipeline manages embedding).

---

## Known limitations / accepted trade-offs
- **Rate limit is per-process, in-memory**: a multi-worker deployment multiplies effective RPM by worker count. Documented in operator guide.
- **Silent fallback on API failure**: callers know via `rag_fusion_applied: false` but receive no error detail in search responses. Explain responses include `rag_fusion_attempted: true, rag_fusion_failure_reason: "..."` when fallback occurs.
- **Shared Anthropic API key with HyDE**: combined steady-state RPM can exceed account limits. Both `[hyde].max_requests_per_minute` and `[rag_fusion].max_requests_per_minute` must be tuned. Documented in `archon-search.toml.example` and ADR.
- **Prompt injection**: user query forwarded verbatim to Anthropic API. Mitigation: validate LLM output — variants must be ≤500 chars, plain text, no control sequences; malformed variants are dropped.
- **FTS-only collections**: `rag_fusion=true` silently ignored (`rag_fusion_applied: false`) when collection has no vector index.
- **`num_queries=1` edge case**: produces 2 total searches; config validation logs WARNING that the LLM overhead rarely justifies a single variant.
- **`rag_fusion_queries_used` semantics**: counts the number of successful LLM-generated variant searches (0..`num_queries`), NOT including the original query search. The `RagFusionSubQueryResult` list will have `rag_fusion_queries_used + 1` entries (entries where `variant_index=0` represent the original query). Callers should not assume `len(rag_fusion_sub_queries) == rag_fusion_queries_used`.
- **Query text leaves the machine**: sending the original query to Anthropic API is disclosed in the ADR and operator documentation.

---

## Architecture

### New dataclass — `archon_search/config.py`

```python
@dataclass
class RAGFusionConfig:
    enabled: bool = False
    model: str = "claude-haiku-4-5-20251001"    # DEFAULT_FAST_MODEL
    timeout_seconds: float = 5.0
    max_requests_per_minute: int = 60
    num_queries: int = 2    # LLM-generated variants (not counting original); total = num_queries + 1
```

`SearchConfig` gains `rag_fusion: RAGFusionConfig = field(default_factory=RAGFusionConfig)`.

Validation: `timeout_seconds > 0`, `max_requests_per_minute >= 1`, `1 <= num_queries <= 5` (ConfigError if outside; WARNING if `== 1`), `model` non-empty.

### New module — `archon_search/rag_fusion.py`

```python
class RAGFusionDependencyError(RuntimeError):
    pass  # Raised when anthropic package is not installed.
          # Defined in archon_search/rag_fusion.py.
          # Route handlers and MCP tools import it as:
          #   from archon_search.rag_fusion import RAGFusionDependencyError

def _query_fingerprint(query: str) -> str:
    """sha256(query)[:16] — non-reversible log correlation token."""

class RAGFusionGenerator:
    def __init__(self, config: RAGFusionConfig) -> None:
        # Lazy-imports anthropic; raises RAGFusionDependencyError on missing package.
        # AsyncAnthropic client, token bucket, _warned_no_key flag.

    def _validate_variant(self, text: str) -> str | None:
        # Returns stripped text if ≤500 chars and no control sequences, else None.

    async def generate_variants(self, query: str) -> list[str]:
        # Returns up to config.num_queries validated variant strings.
        # Returns [] on rate limit, missing key, timeout, APIError, or other failure.
        # Raises RAGFusionDependencyError if anthropic package not installed.
        # Never logs query or variant text verbatim — fingerprint only.
```

System prompt structure (exact wording TBD empirically at implementation time):
```
You are a search query decomposer. Given a user query, generate {num_queries} alternative
search queries that capture different facets of the same information need.
Rules: each query on its own line, plain text, under 500 characters.
Output exactly {num_queries} queries, one per line.

---
{query[:2000]}
---
```

### Second-pass RRF — `archon_search/pipeline.py`

**Resolved type decision**: The `search()` RAG Fusion path MUST call `store.hybrid_search_with_trace()` (not `hybrid_search()`) for all variant searches, to obtain `ScoredSearchCandidate` objects. This makes the return type consistent with the `explain()` path and allows a single `_fuse_rag_fusion_results` function. Update all task descriptions and the pipeline signature note in Task 2.2 accordingly.

> **`hybrid_search_with_trace()` lacks a `filters` parameter** (verified in `store.py` — the current signature is `hybrid_search_with_trace(collection, query_vector, query_text, candidate_depth)`). Implementing the RAG Fusion `search()` path requires extending this method as part of Task 2.2. Add a sub-step: "Add filter support to `_hybrid_search_with_trace()` (new code — this method currently has no filter logic): apply `build_where(filters)` and `.where(pred)` to both the vector search and FTS search legs, following the same pattern as `hybrid_search()`. This is a new feature addition, not a pass-through."

> **Implementation cascade note**: When using `hybrid_search_with_trace()` for the RAG Fusion path, the result type changes from `SearchResult` (returned by `hybrid_search()`) to `ScoredSearchCandidate`. Implementors must handle this type through the remaining pipeline steps:
> - The fused `list[ScoredSearchCandidate]` from `_fuse_rag_fusion_results()` is passed to ACL filter (`apply_acl_filter(fused, lambda c: c.acl, namespace)`) in the same way the existing `explain()` path handles it (see `pipeline.py` line ~657).
> - The fused set is then passed to `self._reranker.rerank_candidates(query, fused, top_k=...)` (not `rerank()` which consumes `SearchResult`). This matches the existing trace path pattern (see `pipeline.py` line ~631).
> - Final result construction: convert `ScoredSearchCandidate` objects to `SearchResult` via `self._candidate_to_search_result(c)` — the same method used in `search_many()` (see `pipeline.py` line ~736). This is the required final conversion step for the `search()` path, since `search()` must return `SearchPipelineResult` containing `list[SearchResult]`.

```python
def _fuse_rag_fusion_results(
    variant_results: list[list[ScoredSearchCandidate]],
    k: int = 60,
) -> list[ScoredSearchCandidate]:
    # Per-rank RRF: score = 1 / (k + rank) for each variant list.
    # Deduplicate by chunk_id — same chunk in multiple variants accumulates scores.
    # RRF formula: score = 1.0 / (k + index + 1) for 0-indexed iteration (i.e.,
    # for index, candidate in enumerate(variant_list): score = 1.0 / (k + index + 1)).
    # Do NOT import _rrf_score from store.py — it is a private implementation detail;
    # coupling pipeline to store internals would make future refactors fragile.
    # The formula is trivial enough to reimplement inline.
    # Returns candidates sorted descending by fused RRF score.

    # NOTE on score_breakdown staleness: The fused ScoredSearchCandidate retains the
    # score_breakdown from the variant where it ranked highest (as returned by
    # hybrid_search_with_trace()). This score_breakdown reflects only that variant's
    # search — it does NOT reflect the accumulated RRF fusion score. Downstream consumers
    # (reranker, explain output) should not interpret score_breakdown.rrf_score as the
    # final fused score. The fused score exists only as the ordering criterion for the list.
```

The `k=60` constant matches the first-pass per-variant RRF constant in `store.py` (`_RRF_K = 60`).

> **Note on `score_breakdown` staleness**: The fused `ScoredSearchCandidate` retains the `score_breakdown` from the variant where it ranked highest (as returned by `hybrid_search_with_trace()`). This `score_breakdown` reflects only that variant's search — it does NOT reflect the accumulated RRF fusion score. Downstream consumers (reranker, explain output) should not interpret `score_breakdown.rrf_score` as the final fused score. The fused score exists only as the ordering criterion for the returned list.

### Pipeline method signature additions

```python
async def search(
    self, query: str, collection: str, namespace: str = DEFAULT_NAMESPACE,
    *, embedder: Embedder, filters: SearchFilters | None = None,
    query_vector: list[float] | None = None,              # existing (C4)
    rag_fusion: bool = False,                             # NEW
    rag_fusion_generator: "RAGFusionGenerator | None" = None,  # NEW
    rag_fusion_config: "RAGFusionConfig | None" = None,   # NEW
) -> SearchPipelineResult: ...

async def search_many(
    self, query: str, collections: list[str], namespace: str = DEFAULT_NAMESPACE,
    query_vector: list[float] | None = None,              # existing (C4)
    rag_fusion: bool = False,                             # NEW
    rag_fusion_generator: "RAGFusionGenerator | None" = None,  # NEW
    rag_fusion_config: "RAGFusionConfig | None" = None,   # NEW
) -> SearchPipelineResult: ...

async def explain(
    self, query: str, collection: str, namespace: str = DEFAULT_NAMESPACE,
    *, query_vector: list[float] | None = None,           # existing (C4)
    rag_fusion: bool = False,                             # NEW
    rag_fusion_generator: "RAGFusionGenerator | None" = None,  # NEW
    rag_fusion_config: "RAGFusionConfig | None" = None,   # NEW
    ...
) -> ExplainPipelineResult: ...
```

Orchestration inside `search()` when `rag_fusion=True` and generator is not None and `config.enabled`:
0. **HyDE/RAG Fusion vector conflict guard**: if `rag_fusion=True` and `query_vector is not None`, log WARNING with fingerprint (`"rag_fusion=True received with pre-computed query_vector; ignoring query_vector"`) and set `query_vector = None`. When RAG Fusion is active, the pipeline manages all embedding internally.
1. FTS-only guard: if collection has no vector index, return standard search with `rag_fusion_applied=False`.
2. `variants = await generator.generate_variants(query)` — may return `[]` on failure.
3. If `RAGFusionDependencyError` (package not installed): re-raise (route handler returns 422).
4. Embed `[query] + variants` in parallel via `asyncio.gather`.
5. Call `store.hybrid_search_with_trace()` for each `(query_text=query, vector=vi)` in parallel via `asyncio.gather`.
6. `fused = _fuse_rag_fusion_results(list(variant_results))`.
7. Apply ACL filter and reranker on `fused`.
8. Return with `rag_fusion_applied=True`, `rag_fusion_queries_used=<count of successful variant searches (0..num_queries)>`.

On exception in steps 4–7: log WARNING with `_query_fingerprint(query)`, fall back to standard single-query search.

### Route-level mutual exclusion (routes_search.py, routes_explain.py, mcp.py)

> Note: The brief (`C5-rag-fusion-brief.md`) originally specified centralizing mutual exclusion in the pipeline layer. This plan deliberately places it at the route/MCP handler level (same as `resolve_hyde_vector`), which avoids pipeline coupling to HyDE internals and matches the pattern already established in the codebase. This is an intentional deviation from the brief; the brief will be updated in Task 7.1.

```python
rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)
if body.rag_fusion:
    hyde_vector, hyde_applied = None, False   # skip HyDE entirely
else:
    generator = getattr(request.app.state, "hyde_generator", None)
    hyde_vector, hyde_applied = await resolve_hyde_vector(body.query, body.hyde, generator, config.hyde)
```

### Schema additions

`SearchRequest`: `rag_fusion: bool = False`
`SearchResponse`: `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, `rag_fusion_attempted: bool = False`

`ExplainRequest`: `rag_fusion: bool = False`
`ExplainResponse`: `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`,
`rag_fusion_attempted: bool = False`, `rag_fusion_failure_reason: str | None = None`,
`rag_fusion_sub_queries: list[RagFusionSubQueryResult] | None = None`

```python
class RagFusionSubQueryResult(BaseModel):
    variant_index: int          # 0 = original query, 1..N = LLM-generated variants
    result_count: int
    top_doc_ids: list[str]      # top 5 doc IDs for operator inspection
```

> **`rag_fusion_queries_used` semantics**: counts the number of successful LLM-generated variant searches (0..`num_queries`), NOT including the original query search. The `RagFusionSubQueryResult` list will have `rag_fusion_queries_used + 1` entries (entries where `variant_index=0` represent the original query). Callers should not assume `len(rag_fusion_sub_queries) == rag_fusion_queries_used`.

### `SearchWithContextResult` — `archon_search/pipeline.py`

```python
# In archon_search/pipeline.py

@dataclass
class SearchWithContextResult:
    results: list[dict[str, Any]]
    pipeline_result: SearchPipelineResult
```

`search_with_context()` returns `SearchWithContextResult` (not a bare tuple). The MCP `search_with_context` handler (Task 5.1) unpacks this to include `rag_fusion_applied`, `rag_fusion_queries_used`, and `rag_fusion_attempted` from `pipeline_result` in its return dict.

### Telemetry additions — `archon_search/telemetry/entry.py`

`TelemetryEntry` gains `rag_fusion_applied: bool | None = None`, `rag_fusion_queries_used: int | None = None`.
Factory methods `from_search_tool_result()`, `from_search_multi_result()`, `from_explain_result()` each gain matching optional kwargs. No `query` or variant text anywhere.

---

## Task breakdown

### Phase 1 — Config + RAGFusionGenerator
> **Releasable**: after Task 1.2 — `RAGFusionGenerator.generate_variants()` is callable with a mocked Anthropic client and all unit tests pass.

#### Task 1.1 — `RAGFusionConfig` dataclass + `[rag_fusion]` TOML loader
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add `RAGFusionConfig` dataclass with five fields: `enabled: bool = False`, `model: str = DEFAULT_FAST_MODEL`, `timeout_seconds: float = 5.0`, `max_requests_per_minute: int = 60`, `num_queries: int = 2`. Import `DEFAULT_FAST_MODEL` from `archon_search.constants` (already imported for `HyDEConfig`).
  - Add `rag_fusion: RAGFusionConfig = field(default_factory=RAGFusionConfig)` to `SearchConfig`.
  - Add `[rag_fusion]` TOML section loader in `load_config()` using the exact pattern from the `[hyde]` loader (lines 375–394): `doc.get("rag_fusion", {})`, field-by-field with `_coerce_bool` / `_coerce_str` / `_coerce_float` / `_coerce_int`.
  - Validate: `timeout_seconds > 0` (raise `ConfigError`); `max_requests_per_minute >= 1` (raise `ConfigError`); `1 <= num_queries <= 5` (raise `ConfigError` outside range, log `WARNING` if `== 1` about LLM overhead rarely justifying a single variant); `model` non-empty (raise `ConfigError`).
- **Releasable**: `load_config()` returns `SearchConfig` with `config.rag_fusion.num_queries == 2` and `config.rag_fusion.enabled == False` when `[rag_fusion]` is absent.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_rag_fusion_config_defaults` — load_config with no TOML returns `RAGFusionConfig` with `enabled=False, num_queries=2`.
  - Unit: `test_rag_fusion_toml_all_keys` — TOML with all five keys parses correctly.
  - Unit: `test_rag_fusion_toml_partial_keys` — TOML with only `num_queries=3` applies that; other fields remain default.
  - Unit: `test_rag_fusion_config_invalid_timeout` — `timeout_seconds = 0` raises `ConfigError`.
  - Unit: `test_rag_fusion_config_invalid_rpm` — `max_requests_per_minute = 0` raises `ConfigError`.
  - Unit: `test_rag_fusion_config_empty_model` — `model = ""` raises `ConfigError`.
  - Unit: `test_rag_fusion_config_num_queries_zero` — `num_queries = 0` raises `ConfigError`.
  - Unit: `test_rag_fusion_config_num_queries_six` — `num_queries = 6` raises `ConfigError`.
  - Unit: `test_rag_fusion_config_num_queries_one_warns` — `num_queries = 1` does NOT raise; `caplog` contains WARNING about overhead.
  - Checkpoint: `uv run pytest tests/test_config.py -k "rag_fusion" -x`

#### Task 1.2 — `RAGFusionGenerator` class + variant generation + validation
- [x] **File**: `archon_search/rag_fusion.py` (new)
- **Depends on**: Task 1.1
- **Description**:
  - Create a stub `Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md` with title, context, and a 'Status: Draft — to be finalized in Task 7.1' note. This satisfies the ADR-required-before-merge gate.
  - Extract `_query_fingerprint(query: str) -> str` to `archon_search/_privacy.py` (new tiny module, ≤10 lines) instead of defining it in `rag_fusion.py`. Import it in both `hyde.py` (replace the local definition) and `rag_fusion.py`. This avoids duplicate implementations that could diverge. Add this as a prerequisite sub-step: create `archon_search/_privacy.py` with `_query_fingerprint` before implementing `rag_fusion.py`.
  > `hyde.py` must re-export `_query_fingerprint` via `from archon_search._privacy import _query_fingerprint` so that existing `tests/test_hyde.py` import paths and the CI guard continue to work without modification. Add `uv run pytest tests/test_hyde.py -x` to the Task 1.2 checkpoint (immediately after creating `_privacy.py` and updating the import in `hyde.py`).
  - `RAGFusionGenerator.__init__(self, config: RAGFusionConfig) -> None`:
    - Import `time` at the module top level (standard library; not lazy-imported).
    - Lazy-imports `anthropic` inside try/except `ImportError`; stores `_anthropic_available: bool`. Does NOT raise at init time.
    - Creates `AsyncAnthropic()` client (reads `ANTHROPIC_API_KEY` from env automatically) if available.
    - Token bucket: `_rpm_tokens: int = config.max_requests_per_minute`, `_rpm_refill_at: float = time.monotonic() + 60.0` (matching HyDE's initialization — starts the rate limit window at construction time rather than at first call), `asyncio.Lock`.
    - `_warned_no_key: bool = False`, `_rate_limit_warned_at: float = 0.0`.
  - `_validate_variant(self, text: str) -> str | None`:
    - Strips whitespace. Returns `None` if: empty after strip, `len(text) > 500`, or text contains any Unicode control sequences (`\x00–\x1F` excluding `\t\n\r`, or `\x7F–\x9F`). Returns stripped text otherwise.
  - `generate_variants(self, query: str) -> list[str]`:
    - If `_anthropic_available` is `False`: raise `RAGFusionDependencyError("Install archon-search[rag_fusion] to use RAG Fusion")`.
    - Token bucket pre-flight under `_lock`: refill tokens if wall-clock minute has passed; if tokens == 0: log WARNING with fingerprint (rate-limited to once/minute via `_rate_limit_warned_at`), return `[]`.
    - Check `ANTHROPIC_API_KEY` in `os.environ`: if absent and not `_warned_no_key`: log WARNING once, set flag, return `[]`.
    - Truncate `query` to 2000 characters. Build prompt requesting `config.num_queries` variants, using `---` delimiters around truncated query.
    - `asyncio.wait_for(self._client.messages.create(model=config.model, max_tokens=150 * config.num_queries, messages=[{"role": "user", "content": prompt}]), timeout=config.timeout_seconds)`. (minimum 150 tokens per variant; ensures the response has budget for all requested variants without truncation — 300 tokens for `num_queries=2` but 750 for `num_queries=5`).
    - Parse response: split by `\n`, strip each line, filter empty, apply `_validate_variant()`, collect valid. Truncate to `config.num_queries` if LLM returns more. Log WARNING with fingerprint if fewer valid variants than requested.
    - Return validated variant list (may be empty; never raises on LLM path).
    - Catch `asyncio.TimeoutError`, `anthropic.APIError`, `Exception`: log WARNING with fingerprint (never log `query` or variant text verbatim), return `[]`.
    - Module has no top-level `import anthropic` — all anthropic imports inside `__init__` under try/except guard.
- **Releasable**: `RAGFusionGenerator.generate_variants()` is callable; all fallback paths tested with mocked `AsyncAnthropic`.
- **Tests (TDD)** — `tests/test_rag_fusion.py` (new):
  - Unit: `test_generate_variants_success` — mock returns 2 valid variant lines; result is list of 2 strings, each ≤500 chars.
  - Unit: `test_generate_variants_timeout_fallback` — mock raises `asyncio.TimeoutError`; result is `[]`, WARNING logged with 16-char hex fingerprint.
  - Unit: `test_generate_variants_api_error_fallback` — mock raises `anthropic.APIError`; result is `[]`.
  - Unit: `test_generate_variants_empty_response` — mock returns only whitespace lines; result is `[]`.
  - Unit: `test_generate_variants_no_api_key` — `ANTHROPIC_API_KEY` absent; result is `[]`; WARNING logged exactly once across two calls.
  - Unit: `test_generate_variants_package_not_installed` — simulate `ImportError`; `generate_variants()` raises `RAGFusionDependencyError` with "Install archon-search[rag_fusion]" message.
  - Unit: `test_rate_limit_fallback` — exhaust token bucket; next call returns `[]` with WARNING.
  - Unit: `test_fingerprint_no_raw_query_in_log` — `caplog` does NOT contain raw query string; fingerprint is exactly 16 hex chars.
  - Unit: `test_validate_variant_too_long` — variant of 501 chars returns `None`.
  - Unit: `test_validate_variant_control_sequences` — variant containing `\x00` returns `None`.
  - Unit: `test_validate_variant_valid` — normal text ≤500 chars returns stripped text.
  - Unit: `test_generate_variants_malformed_dropped` — mock returns 3 lines: 1 valid, 1 too-long, 1 with control seq; result is `[valid_one]`.
  - Unit: `test_generate_variants_more_than_requested_truncated` — mock returns 5 lines for `num_queries=2`; result has ≤2 items.
  - Unit: `test_query_truncated_to_2000_chars` — query of 3000 chars; verify prompt passed to LLM contains at most 2000 chars of query content.
  - Unit: `test_validate_variant_del_char_rejected` — variant containing `\x7F` returns `None`.
  - Unit: `test_validate_variant_c1_control_rejected` — variant containing `\x80` returns `None`.
  - Unit: `test_validate_variant_tab_allowed` — variant containing `\t` returns stripped text (not rejected).
  - Unit: `test_validate_variant_newline_in_content` — variant body containing `\n` is not rejected (it's split before validation; single line with `\n` at end strips to valid text).
  - Unit: `test_generate_variants_prompt_contains_query_and_num_queries` — after calling `generate_variants("my query")` with a mocked client, inspect the `messages` kwarg passed to `self._client.messages.create()`; assert the prompt contains the string `"my query"` (truncated form) AND the string representation of `config.num_queries`; assert `max_tokens` equals `150 * config.num_queries`.
  - Unit: `test_concurrent_generate_variants_respects_token_limit` — initialize token bucket with capacity 2; launch 5 concurrent `asyncio.gather` calls to `generate_variants()`; assert exactly 2 calls return valid results and 3 return `[]` (rate-limited); no `AttributeError` or deadlock.
  - Checkpoint: `uv run pytest tests/test_rag_fusion.py -x`

---

### Phase 2 — Second-pass RRF + Pipeline Orchestration
> **Releasable**: after Task 2.4 — all three pipeline methods support `rag_fusion=True` with full orchestration; unit tests pass with deterministic inputs.
>
> **Note**: The `search_with_context()` return type change to `SearchWithContextResult` (Task 2.2) MUST be deployed atomically with the MCP handler update (Task 5.1). The Phase 2 intermediate state where `search_with_context()` returns `SearchWithContextResult` but the MCP handler still expects `list[dict]` will cause a runtime crash. Do NOT merge Phase 2 independently if Phase 5 is not also merged. The safest approach is to move the `search_with_context()` return type change to Task 5.1, keeping Task 2.2's `search_with_context()` signature unchanged while adding the RAG Fusion parameter forwarding.

#### Task 2.1 — `_fuse_rag_fusion_results()` in `pipeline.py`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: nothing
- **Description**:
  - Add module-level function `_fuse_rag_fusion_results(variant_results: list[list[ScoredSearchCandidate]], k: int = 60) -> list[ScoredSearchCandidate]`:

  **Resolved type decision**: Both `search()` and `explain()` paths MUST call `store.hybrid_search_with_trace()` (not `hybrid_search()`), which returns `ScoredSearchCandidate` objects. A single `_fuse_rag_fusion_results` function operates on `ScoredSearchCandidate` for both paths.

    - For each variant result list: compute per-rank RRF score using `score = 1.0 / (k + index + 1)` for 0-indexed iteration (i.e., `for index, candidate in enumerate(variant_list): score = 1.0 / (k + index + 1)`). **RRF formula**: Implement inline — do NOT import `_rrf_score` from `store.py` (it is a private implementation detail; coupling pipeline to store internals would make future refactors fragile). The formula is trivial enough to reimplement.
    - Deduplicate by `chunk_id`: when the same `chunk_id` appears in multiple variant lists, accumulate RRF scores. Keep the `ScoredSearchCandidate` instance from the variant where it ranked highest (lowest rank).
    - Return candidates sorted descending by accumulated fused RRF score.
    - Empty input or all-empty variant lists: return `[]`.
    - Single non-empty variant list: return ranked as per that list's RRF scores.
  - The `k=60` constant matches the first-pass per-variant RRF already used in `store.py`.
- **Releasable**: `_fuse_rag_fusion_results()` is callable and deterministically fuses any list of candidate lists.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_fuse_rag_fusion_results_two_variants` — two variant lists with one overlapping chunk_id; overlapping chunk has higher fused score than non-overlapping; order is deterministic.
  - Unit: `test_fuse_rag_fusion_results_no_overlap` — two variant lists with no shared chunk_ids; all chunks present in output at their individual RRF scores.
  - Unit: `test_fuse_rag_fusion_results_empty_inputs` — `_fuse_rag_fusion_results([[], []])` returns `[]`.
  - Unit: `test_fuse_rag_fusion_results_single_variant` — single list returns all chunks ranked by RRF score.
  - Unit: `test_fuse_rag_fusion_results_multi_contribution_boost` — chunk in variant 0 at rank 1 AND variant 1 at rank 1 scores higher than a chunk only at rank 1 in a single variant.
  - Unit: `test_fuse_rag_fusion_results_deterministic` — same inputs always produce same output.
  - Unit: `test_fuse_rag_fusion_results_same_doc_different_chunks` — variant A returns chunk-001 of doc X, variant B returns chunk-002 of doc X; assert BOTH chunks survive in the fused output (dedup is by chunk_id, not doc_id).
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "fuse_rag_fusion" -x`

#### Task 2.2 — `SearchPipelineResult` RAG Fusion fields + `pipeline.search()` orchestration
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, and `rag_fusion_attempted: bool = False` to `SearchPipelineResult` dataclass (after `fanout_timings`).
    - Semantics: `rag_fusion_attempted=True` when the generator was called (regardless of outcome); `rag_fusion_applied=True` only when at least one variant was generated and the fusion path executed. When generator returns `[]`: set `rag_fusion_attempted=True, rag_fusion_applied=False, rag_fusion_queries_used=0`.
  - Add `rag_fusion: bool = False`, `rag_fusion_generator: "RAGFusionGenerator | None" = None`, `rag_fusion_config: "RAGFusionConfig | None" = None` as keyword-only parameters to `search()` (after `query_vector`).
  - Extend `pipeline.search_with_context()` to accept `rag_fusion: bool = False`, `rag_fusion_generator: "RAGFusionGenerator | None" = None`, `rag_fusion_config: "RAGFusionConfig | None" = None` and forward them to `search()`. Without this, the MCP `search_with_context` tool cannot pass RAG Fusion params through the pipeline. (`search_with_context()` delegates to `search()` and must have its signature extended in this same task.)
  > **Decision**: Change `search_with_context()` to return `SearchWithContextResult` (the `@dataclass` defined in the Architecture section above). Do NOT use a bare tuple (fragile to reordering). The MCP `search_with_context` handler update (Task 5.1) depends on this change and must be treated as a co-dependent change with Task 2.2. Add `search_with_context()` return type update as a step in Task 2.2's description. The MCP `search_with_context` handler (Task 5.1) must unpack this to include `rag_fusion_applied`, `rag_fusion_queries_used`, and `rag_fusion_attempted` from `pipeline_result` in its return dict.
  - Add `TYPE_CHECKING` imports for `RAGFusionGenerator` from `archon_search.rag_fusion` and `RAGFusionConfig` from `archon_search.config`.
  - Extend `store.hybrid_search_with_trace()` to accept `filters: SearchFilters | None = None` and implement filter logic (new code — see Architecture section). Add a test: `test_hybrid_search_with_trace_filters_applied` — call with filters; assert filtered results exclude disallowed docs. Both the public `SearchStore.hybrid_search_with_trace()` method and the private `_hybrid_search_with_trace()` function must be updated: the public method passes `filters` through; the private function applies `build_where(filters)` and `.where(pred)` to both the vector and FTS search legs (same pattern as `hybrid_search()`).
  - Update the `_hybrid_search_with_trace()` docstring in `store.py` to remove the "eval/debug-only" qualifier and note its dual role: it is also the production search backend for the RAG Fusion path. Failure to update this will mislead future contributors into treating it as removable debug code.
  - Add `SearchStore.has_vector_index(collection: str) -> bool` to `store.py` as part of this task. **Semantics**: `has_vector_index(collection) -> bool` should return `True` if the collection has a vector column in its LanceDB schema (meaning it was ingested with embeddings), and `False` if the collection was created with FTS-only mode. If all current collections always have vector columns (verify in `store.py`'s `ensure_collection` schema), document this guard as a forward-compatibility measure for future FTS-only collections. The method should use the table schema inspection (via LanceDB `table.schema` or equivalent), NOT require a live IVF/PQ index to be present — having a vector column is the relevant condition. If `has_vector_index` does not already exist, it must be added in this task.
  - Orchestration inside `search()` when `rag_fusion=True` and generator is not `None` and `rag_fusion_config is not None` and `rag_fusion_config.enabled`:
    0. **Vector conflict guard**: if `query_vector is not None`, log WARNING via `_logger.warning("rag_fusion=True received with pre-computed query_vector=%s; ignoring", _query_fingerprint(query))` and set `query_vector = None`.
    1. FTS-only guard: check whether the collection has a vector index via `self.store.has_vector_index(collection)`. If `False`, return standard single-query search with `rag_fusion_applied=False`.
    2. `variants: list[str] = await rag_fusion_generator.generate_variants(query)` — may return `[]`; `RAGFusionDependencyError` re-raised. Set `rag_fusion_attempted=True` after this call regardless of result.
    3. All queries = `[query] + variants`. Embed each in parallel: `vectors = await asyncio.gather(*[embedder.embed_one(q) for q in all_queries])`. `asyncio` is already imported.
    4. Call `self.store.hybrid_search_with_trace()` for each `(query_text=query, query_vector=v)` in parallel via `asyncio.gather`. Pass the same `filters` argument that the caller provided to every per-variant `store.hybrid_search_with_trace()` call. Dropping filters on variant searches would include documents that ACL or field filters should have excluded.
    5. Use `asyncio.gather(*search_calls, return_exceptions=True)` to collect per-variant results. Filter out exceptions using `isinstance(r, BaseException)` (not `Exception`) — `asyncio.CancelledError` inherits from `BaseException` in Python 3.8+ and would not be filtered by an `Exception` check. If ALL searches failed, fall back to standard single-query search with `rag_fusion_applied=False`. If SOME searches succeeded, proceed with partial fusion using only the successful result sets — this is the "partial fusion" behavior.
    6. `fused = _fuse_rag_fusion_results(list(variant_results))`.
    7. Apply ACL filter and reranker on `fused` (same as existing single-query path).
    8. Return `SearchPipelineResult(..., rag_fusion_applied=True, rag_fusion_queries_used=len(successful_variant_results), rag_fusion_attempted=True)`.

    > `rag_fusion_queries_used` counts only the LLM-generated variant searches that succeeded (not exceptions). It does not count the original query's search. Range: 0..`num_queries`.
    - On exception in steps 3–4 (embedding stage): log WARNING with `_query_fingerprint(query)` (import from `archon_search._privacy`), fall back to standard single-query search with `rag_fusion_applied=False, rag_fusion_queries_used=0`.
  - When `rag_fusion=False` (or disabled): standard single-query path; `rag_fusion_applied=False, rag_fusion_queries_used=0, rag_fusion_attempted=False` on result.
- **Releasable**: `pipeline.search(..., rag_fusion=True, rag_fusion_generator=mock)` fuses N+1 variants; `rag_fusion=False` is identical to pre-C5 behavior.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_pipeline_result_has_rag_fusion_fields` — `SearchPipelineResult(results=[], acl_filtered=False)` has `rag_fusion_applied=False, rag_fusion_queries_used=0, rag_fusion_attempted=False`.
  - Unit: `test_search_rag_fusion_calls_generate_variants` — mock generator returns `["v1", "v2"]`; verify `store.hybrid_search_with_trace` called 3 times (original + 2 variants); result `rag_fusion_applied=True, rag_fusion_queries_used=2`.
  - Unit: `test_search_rag_fusion_empty_variants_still_searches` — mock generator returns `[]`; `store.hybrid_search_with_trace` called 1 time (original only); `rag_fusion_attempted=True, rag_fusion_applied=False, rag_fusion_queries_used=0`.
  - Unit: `test_search_rag_fusion_disabled_config_skips` — `rag_fusion_config.enabled=False`; generator NOT called; `rag_fusion_applied=False`, `rag_fusion_attempted=False` (generator was never invoked, NOT the same as returning []).
  - Unit: `test_search_rag_fusion_no_generator_skips` — `rag_fusion_generator=None`; standard search; `rag_fusion_applied=False`.
  - Unit: `test_search_rag_fusion_fts_only_guard` — `store.has_vector_index` returns `False`; generator NOT called; `rag_fusion_applied=False`.
  - Unit: `test_search_rag_fusion_false_no_overhead` — `rag_fusion=False`; `generate_variants` NOT called; result identical to pre-C5 search (no extra store calls).
  - Unit: `test_search_with_context_rag_fusion_forwarded` — `pipeline.search_with_context(..., rag_fusion=True, rag_fusion_generator=mock)` calls `search()` with those params; result carries `rag_fusion_applied=True`.
  - Unit: `test_search_rag_fusion_reranker_uses_original_query` — mock reranker; `pipeline.search(..., rag_fusion=True, rag_fusion_generator=mock_returning_2_variants)`; assert reranker was called with `query=<original_query_string>`, not any variant text; assert `reranker.rerank.call_count == 1` (reranker runs once on fused set, not per-variant).
  - Unit: `test_search_rag_fusion_config_none_skips` — call `pipeline.search(..., rag_fusion=True, rag_fusion_generator=mock, rag_fusion_config=None)`; verify no `AttributeError`; standard single-query search proceeds; `rag_fusion_applied=False`.
  - Unit: `test_search_rag_fusion_acl_filter_applied_to_fused_results` — fused results include a candidate with an ACL that does not match the namespace; verify the candidate is excluded from the final result (confirms ACL filter runs on the merged set, not per-variant only).
  - Unit: `test_search_rag_fusion_partial_search_failure` — mock `hybrid_search_with_trace` so call #1 (original) succeeds, call #2 (variant 1) raises `LanceDBError`, call #3 (variant 2) succeeds; use `asyncio.gather(return_exceptions=True)`; assert final fused result contains only docs from the 2 successful searches; assert no `LanceDBError` object appears in the fused result; `rag_fusion_applied=True`, `rag_fusion_queries_used=1` (one successful variant).
  - Unit: `test_search_rag_fusion_all_searches_fail` — all variant searches raise; assert standard single-query fallback; `rag_fusion_applied=False`.
  - Unit: `test_search_rag_fusion_ignores_caller_query_vector` — call `pipeline.search(..., rag_fusion=True, rag_fusion_generator=mock, query_vector=[0.1]*384)`; assert the embedder was called for the original query (confirming the pipeline re-embedded the query rather than using the caller-provided vector); assert `store.hybrid_search_with_trace` was called at least once (confirming search proceeded); assert `rag_fusion_applied=True`.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "rag_fusion" -x`

#### Task 2.3 — `pipeline.search_many()` gains RAG Fusion orchestration
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `rag_fusion: bool = False`, `rag_fusion_generator: "RAGFusionGenerator | None" = None`, `rag_fusion_config: "RAGFusionConfig | None" = None` to `search_many()`.
  - When `rag_fusion=True` and enabled: generate variants once (single LLM call, not per-collection), embed all queries in parallel, fan out `hybrid_search_with_trace()` calls across all collections × all variants in parallel, fuse per-collection via `_fuse_rag_fusion_results`, then merge across collections using the existing cross-collection merge logic. `rag_fusion_applied` and `rag_fusion_queries_used` propagated onto `SearchPipelineResult`.

  > **Integration with `_fanout_merge_acl()`**: The RAG Fusion path does NOT call `_fanout_merge_acl()` (which handles a single query vector per collection). Instead, for each collection that has a vector index: call `hybrid_search_with_trace()` N+1 times (once per query vector) in parallel via `asyncio.gather`, collect N+1 result lists, pass them to `_fuse_rag_fusion_results()` to get a single per-collection fused result. For FTS-only collections: call `hybrid_search_with_trace()` once with the original query only. After per-collection fusion, the cross-collection merge uses the same logic as the existing `_fanout_merge_acl()` path: (a) trim each per-collection fused result to `max(self._fanout_leg_trim, 1)` candidates, sorted by `(-rrf_score, chunk_id)`; (b) concatenate the trimmed per-collection lists in alphabetical collection-name order; (c) apply a single `apply_acl_filter()` to the merged list. **The ACL filter step that exists inside `_fanout_merge_acl()` must NOT be skipped** — apply it to the merged per-collection fused results before returning. There is no score normalization between collections in the existing logic; do not add any. The `_fanout_merge_acl()` helper is bypassed for the RAG Fusion path; its ACL logic (step c) must be replicated in the RAG Fusion branch or extracted into a shared helper.

  > Detailed orchestration: (a) Call `generate_variants(query)` once to get `variants`; set `rag_fusion_attempted=True`. All queries = `[query] + variants`. (b) Embed all queries using `self._global_embedder.embed_one()` in parallel via `asyncio.gather`. (c) For each collection: if `self.store.has_vector_index(collection)`, call `hybrid_search_with_trace()` for each of the N+1 `(query_text=query, query_vector=v)` pairs in parallel; for FTS-only collections, call `hybrid_search_with_trace()` once with the original query only. (d) Fuse per-collection result sets via `_fuse_rag_fusion_results`. (e) Cross-collection merge using existing logic. This produces at most `num_queries+1` `hybrid_search_with_trace()` calls per vector-capable collection.

  > **`RAGFusionDependencyError` handling**: if `generate_variants()` raises `RAGFusionDependencyError` (package not installed), re-raise it immediately — same as Task 2.2. Do not swallow it in a generic `except`.

  > **Implementation note**: To avoid duplicating the per-collection fan-out logic between `search()` and `search_many()`, extract a shared private method `SearchPipeline._rag_fusion_per_collection_searches(self, all_queries: list[str], all_vectors: list[list[float]], collection: str, filters: SearchFilters | None) -> list[list[ScoredSearchCandidate]]` in `pipeline.py`. This method calls `hybrid_search_with_trace()` for each `(query_text=all_queries[0], query_vector=v)` pair in parallel and returns the list of per-variant result lists. Both `search()` (Task 2.2) and `search_many()` (Task 2.3) call this method. Task 2.2 implementors should write it inline first, then Task 2.3 extracts it into the shared method.
  - When `rag_fusion=False`: identical to pre-C5 behavior (zero overhead).
- **Releasable**: multi-collection search with `rag_fusion=True` fuses results per-collection; `rag_fusion=False` unchanged.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_many_rag_fusion_generates_once` — 2 collections, mock generator returns 2 variants; verify `generate_variants` called exactly once; `store.hybrid_search_with_trace` called 2 collections × 3 queries = 6 times; assert final result contains docs from BOTH collections (per-collection fusion happened before cross-collection merge, not a single merged fusion of all 6 result sets).
  - Unit: `test_search_many_rag_fusion_false_unchanged` — `rag_fusion=False`; behavior identical to pre-C5 (`generate_variants` not called).
  - Unit: `test_search_many_rag_fusion_mixed_collection_types` — 3 collections: 2 with vector index, 1 FTS-only; mock returns 2 variants (so N+1=3 total queries per vector collection); assert `hybrid_search_with_trace` called 2 vector-index collections × 3 queries = 6 calls for vector collections, plus 1 call for the FTS-only collection = 7 total `hybrid_search_with_trace` calls; result contains docs from all 3 collections.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "search_many.*rag" -x`

#### Task 2.4 — `ExplainPipelineResult` RAG Fusion fields + `pipeline.explain()` orchestration
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add to `ExplainPipelineResult` dataclass:
    - `rag_fusion_applied: bool = False`
    - `rag_fusion_queries_used: int = 0`
    - `rag_fusion_attempted: bool = False`
    - `rag_fusion_failure_reason: str | None = None`
    - Add a `@dataclass class RagFusionSubQueryInfo: variant_index: int; result_count: int; top_doc_ids: list[str]` in `pipeline.py` (pipeline-internal, NOT the Pydantic schema `RagFusionSubQueryResult`). Use `rag_fusion_sub_query_results: list[RagFusionSubQueryInfo] | None = None` on `ExplainPipelineResult`. The route handler maps `RagFusionSubQueryInfo` to the Pydantic `RagFusionSubQueryResult` by field name (not positional unpacking), eliminating the fragile tuple-unpacking risk.
  - Add `rag_fusion: bool = False`, `rag_fusion_generator: "RAGFusionGenerator | None" = None`, `rag_fusion_config: "RAGFusionConfig | None" = None` to `explain()` (after `query_vector`).
  - When `rag_fusion=True` and enabled:
    - FTS-only guard (same as `search()`).
    - Try to `await rag_fusion_generator.generate_variants(query)` → `variants`. On `RAGFusionDependencyError`: re-raise. On exception (timeout, API error): set `rag_fusion_attempted=True, rag_fusion_failure_reason="<error type string>"` on result; fall back to standard explain.
    - If variants returned: embed all queries, call `store.hybrid_search_with_trace()` for each in parallel, fuse via `_fuse_rag_fusion_results()`, apply ACL filter + reranker on fused set.
    - Use `asyncio.gather(*search_calls, return_exceptions=True)` for the variant `hybrid_search_with_trace()` calls (same as `search()`). Filter out `BaseException` instances. `rag_fusion_queries_used` counts only successful variant searches (not the original). Failed variant entries are omitted from `rag_fusion_sub_query_results`.
    - Build `rag_fusion_sub_query_results` from the original search + successful variant searches only: `[RagFusionSubQueryInfo(variant_index=0, result_count=len(original_results), top_doc_ids=top_5_doc_ids_original), RagFusionSubQueryInfo(variant_index=1, result_count=len(v1_results), top_doc_ids=top_5_doc_ids_v1), ...]`. Failed variants are omitted (no entry for their variant_index).
    - Set `rag_fusion_applied=True, rag_fusion_queries_used=len(successful_variant_searches)` on result (counting only successful variant searches, not the original).
  - When `rag_fusion=False`: standard explain; all new fields remain at defaults.
- **Releasable**: `pipeline.explain(..., rag_fusion=True)` exposes per-sub-query result sets; `rag_fusion=False` unchanged.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_explain_pipeline_result_has_rag_fusion_fields` — `ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)` has all five new fields at defaults.
  - Unit: `test_explain_rag_fusion_sub_query_results_populated` — mock generator returns 2 variants (both successful); result has `rag_fusion_sub_query_results` with 3 `RagFusionSubQueryInfo` entries (variant_index 0=original, 1, 2); `rag_fusion_applied=True, rag_fusion_queries_used=2` (`rag_fusion_queries_used + 1` total entries: original + successful variants only).
  - Unit: `test_explain_rag_fusion_failure_sets_attempted_and_reason` — generator raises `asyncio.TimeoutError` internally during variants call; result has `rag_fusion_attempted=True, rag_fusion_failure_reason` non-empty; explain still completes (standard fallback).
  - Unit: `test_explain_rag_fusion_false_unchanged` — `rag_fusion=False`; behavior identical to pre-C5.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "explain.*rag" -x`

---

### Phase 3 — Schema Changes
> **Releasable**: after Task 3.3 — all request/response and telemetry Pydantic models carry the new fields; OpenAPI snapshot updated.

#### Task 3.1 — `SearchRequest` + `SearchResponse` RAG Fusion fields
- [x] **File**: `archon_search/server/routes_search.py`
- **Depends on**: nothing
- **Description**:
  - Add `rag_fusion: bool = False` to `SearchRequest` (after `hyde`).
  - Add `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, and `rag_fusion_attempted: bool = False` to `SearchResponse`.
  - Add `BREAKING.md` entry: "C5: `SearchResponse` gains `rag_fusion_applied: bool` (default `false`), `rag_fusion_queries_used: int` (default `0`), and `rag_fusion_attempted: bool` (default `false`). Backward-compatible for clients ignoring unknown fields; breaking for strict-schema validators."
  - Update OpenAPI snapshot: `uv run --python 3.12 python -c "..."` (follow existing snapshot script).
- **Releasable**: `POST /search` with `{"rag_fusion": true}` deserialises; response includes both new fields.
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - [x] Unit: `test_search_request_rag_fusion_default_false` — `SearchRequest(query="q", collection="c")` has `rag_fusion == False`.
  - [x] Unit: `test_search_request_accepts_rag_fusion_true` — `SearchRequest(..., rag_fusion=True)` validates without error.
  - [x] Unit: `test_search_response_has_rag_fusion_fields` — `SearchResponse(results=[], acl_filtered=False)` has `rag_fusion_applied=False, rag_fusion_queries_used=0, rag_fusion_attempted=False`.
  - Checkpoint: `uv run pytest tests/test_routes_search.py -k "rag_fusion" -x`

#### Task 3.2 — `ExplainRequest` + `ExplainResponse` + `RagFusionSubQueryResult` schema
- [x] **File**: `archon_search/server/routes_explain.py`
- **Depends on**: nothing
- **Description**:
  - Add `RagFusionSubQueryResult(BaseModel)` before `ExplainRequest`:
    ```python
    class RagFusionSubQueryResult(BaseModel):
        variant_index: int
        result_count: int
        top_doc_ids: list[str]
    ```
  - Add `rag_fusion: bool = False` to `ExplainRequest` (after `hyde`).
  - Add to `ExplainResponse`: `rag_fusion_applied: bool = False`, `rag_fusion_queries_used: int = 0`, `rag_fusion_attempted: bool = False`, `rag_fusion_failure_reason: str | None = None`, `rag_fusion_sub_queries: list[RagFusionSubQueryResult] | None = None`.
  - Update `ExplainResponse.from_pipeline_result()` classmethod signature to accept `rag_fusion_sub_query_results: list[RagFusionSubQueryInfo] | None = None` (the pipeline-internal type from `pipeline.py`) and map it to `list[RagFusionSubQueryResult] | None` using: `[RagFusionSubQueryResult(variant_index=r.variant_index, result_count=r.result_count, top_doc_ids=r.top_doc_ids) for r in rag_fusion_sub_query_results] if rag_fusion_sub_query_results else None`. Thread through all five new fields as optional kwargs (defaults match field defaults). The route handler (Task 4.3) passes the pipeline result's `rag_fusion_sub_query_results` directly to `from_pipeline_result()` — the mapping happens inside the classmethod, not in the route handler.
  - Add `BREAKING.md` entry for `ExplainResponse` new fields.
  - Update OpenAPI snapshot.
- **Releasable**: `POST /explain` with `{"rag_fusion": true}` deserialises; response schema includes all new fields.
- **Tests (TDD)** — `tests/test_routes_explain.py`:
  - [x] Unit: `test_explain_request_rag_fusion_default_false` — `ExplainRequest(query="q", collection="c")` has `rag_fusion == False`.
  - [x] Unit: `test_explain_request_accepts_rag_fusion_true`
  - [x] Unit: `test_explain_response_has_rag_fusion_fields` — all five new fields present with defaults; `rag_fusion_sub_queries=None`.
  - [x] Unit: `test_rag_fusion_sub_query_result_schema` — `RagFusionSubQueryResult(variant_index=0, result_count=3, top_doc_ids=["a","b","c"])` validates correctly.
  - [x] Unit: `test_explain_from_pipeline_result_threads_rag_fusion` — `from_pipeline_result(..., rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_sub_query_results=[...])` sets those fields on the response.
  - Checkpoint: `uv run pytest tests/test_routes_explain.py -k "rag_fusion" -x`

#### Task 3.3 — `TelemetryEntry` RAG Fusion fields
- [x] **File**: `archon_search/telemetry/entry.py`
- **Depends on**: nothing
- **Description**:
  - Add `rag_fusion_applied: bool | None = None` and `rag_fusion_queries_used: int | None = None` to `TelemetryEntry` (after existing fields).
  - Update `from_search_tool_result()` to accept optional `rag_fusion_applied: bool | None = None` and `rag_fusion_queries_used: int | None = None` kwargs and pass them to the constructor.
  - Update `from_search_multi_result()` with same optional kwargs.
  - Update `from_explain_result()` with same optional kwargs.
  - No `query` or variant text parameter anywhere — structural invariant preserved.
- **Releasable**: telemetry entries can carry RAG Fusion metadata; all existing call sites remain unmodified (new kwargs are optional).
- **Tests (TDD)** — `tests/test_telemetry.py` (extend):
  - [x] Unit: `test_telemetry_entry_rag_fusion_fields_default_none` — `TelemetryEntry` created without new kwargs has `rag_fusion_applied=None, rag_fusion_queries_used=None`.
  - [x] Unit: `test_telemetry_entry_from_search_tool_result_with_rag_fusion` — factory method sets both fields when provided.
  - [x] Unit: `test_telemetry_entry_from_search_multi_result_with_rag_fusion` — same for multi-result factory.
  - [x] Unit: `test_telemetry_entry_from_explain_result_with_rag_fusion` — same for explain factory.
  - [x] Unit: `test_telemetry_entry_no_query_param` — static inspection of all three updated factory method signatures confirms no `query` parameter exists.
  - Checkpoint: `uv run pytest tests/test_telemetry.py -k "rag_fusion" -x`

---

### Phase 4 — App Wiring + REST Route Handlers
> **Releasable**: after Task 4.3 — `POST /search` and `POST /explain` respect `rag_fusion=true`; all new response fields are correct.

#### Task 4.1 — `RAGFusionGenerator` initialization in `app.py` + optional dep + example config
- [x] **Files**: `archon_search/server/app.py`, `pyproject.toml`, `archon-search.toml.example`
- **Depends on**: Task 1.2
- **Description**:
  - Add `rag_fusion = ["anthropic>=0.40"]` to `[project.optional-dependencies]` in `pyproject.toml`.
  - In `app.py:create_app()`: `from archon_search.rag_fusion import RAGFusionGenerator`; instantiate `app.state.rag_fusion_generator = RAGFusionGenerator(config=config.rag_fusion)`. Startup always succeeds regardless of `anthropic` install status (lazy import).
  - If `config.rag_fusion.enabled`: log INFO `"RAG Fusion is enabled — search query text will be sent to Anthropic's API (model: %s)"`.
  - `create_mcp_app()` signature gains `rag_fusion_generator: "RAGFusionGenerator | None" = None` parameter.
  - In `app.py`, pass `rag_fusion_generator=app.state.rag_fusion_generator` when calling `create_mcp_app()`.
  - Update `archon-search.toml.example` with `[rag_fusion]` section including all five keys, privacy warning comment, and explicit rate-limit note: "WARNING: [hyde].max_requests_per_minute + [rag_fusion].max_requests_per_minute must not exceed your Anthropic account rate limit — both features share the same API key."
- **Releasable**: server starts with `app.state.rag_fusion_generator` set; `POST /search` with `rag_fusion=false` works end-to-end with no regression.
- **Tests (TDD)** — `tests/test_app.py`:
  - Unit: `test_app_state_has_rag_fusion_generator` — `create_app()` sets `app.state.rag_fusion_generator` to a `RAGFusionGenerator` instance.
  - Unit: `test_rag_fusion_optional_dep_in_pyproject` — read `pyproject.toml`; assert `rag_fusion` in `[project.optional-dependencies]` contains `anthropic`.
  - Unit: `test_app_startup_logs_info_when_rag_fusion_enabled` — `config.rag_fusion.enabled=True`; `caplog` contains INFO with "RAG Fusion" and "Anthropic's API".
  - Unit: `test_app_startup_no_log_when_rag_fusion_disabled` — `config.rag_fusion.enabled=False`; no RAG Fusion INFO message.
  - Checkpoint: `uv run pytest tests/test_app.py -k "rag_fusion" -x`

#### Task 4.2 — Wire `routes_search.py` REST handler
- [x] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Tasks 2.2, 2.3, 3.1, 4.1
- **Description**:
  - In the `search` handler, before the `if body.collections is not None` branch:
    ```python
    rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)
    # Mutual exclusion: rag_fusion=True suppresses HyDE entirely
    if body.rag_fusion:
        hyde_vector, hyde_applied = None, False
    else:
        generator = getattr(request.app.state, "hyde_generator", None)
        hyde_vector, hyde_applied = await resolve_hyde_vector(
            body.query, body.hyde, generator, config.hyde
        )
    ```
  - Single-collection path: pass `rag_fusion=body.rag_fusion, rag_fusion_generator=rag_fusion_gen, rag_fusion_config=config.rag_fusion` to `pipeline.search(...)`.
  - Multi-collection path: same to `pipeline.search_many(...)`.
  - Both response constructions: include `rag_fusion_applied=result.rag_fusion_applied, rag_fusion_queries_used=result.rag_fusion_queries_used, rag_fusion_attempted=result.rag_fusion_attempted` in `SearchResponse(...)`.
  - Add import: `from archon_search.rag_fusion import RAGFusionDependencyError` at the top of `routes_search.py`.
  - Catch `RAGFusionDependencyError` from pipeline (package not installed): return `JSONResponse({"detail": str(e)}, status_code=422)`.
  - Update telemetry calls to pass `rag_fusion_applied=result.rag_fusion_applied, rag_fusion_queries_used=result.rag_fusion_queries_used`.
- **Releasable**: `POST /search` with `rag_fusion=true` triggers orchestration in pipeline; `rag_fusion_applied` and `rag_fusion_queries_used` correct in response.
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - Unit: `test_search_rag_fusion_true_skips_hyde` — `rag_fusion=True, hyde=True`; verify `resolve_hyde_vector` NOT called; response has `hyde_applied=False`.
  - Unit: `test_search_rag_fusion_true_passes_to_pipeline` — mock pipeline result has `rag_fusion_applied=True, rag_fusion_queries_used=2`; response carries both values.
  - Unit: `test_search_rag_fusion_false_hyde_still_works` — `rag_fusion=False, hyde=True`; verify `resolve_hyde_vector` IS called.
  - Unit: `test_search_rag_fusion_package_not_installed_returns_422` — pipeline raises `RAGFusionDependencyError`; response is 422.
  - Unit: `test_search_many_rag_fusion_true` — multi-collection path passes rag_fusion params to `search_many`.
  - Checkpoint: `uv run pytest tests/test_routes_search.py -k "rag_fusion" -x`

#### Task 4.3 — Wire `routes_explain.py` REST handler
- [x] **File**: `archon_search/server/routes_explain.py`
- **Depends on**: Tasks 2.4, 3.2, 4.1
- **Description**:
  - In `explain_endpoint`, resolve mutual exclusion at the top of the handler body:
    ```python
    rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)
    if body.rag_fusion:
        hyde_vector, hyde_applied = None, False
    else:
        generator = getattr(request.app.state, "hyde_generator", None)
        hyde_vector, hyde_applied = await resolve_hyde_vector(
            body.query, body.hyde, generator, config.hyde
        )
    ```
  - Pass `rag_fusion=body.rag_fusion, rag_fusion_generator=rag_fusion_gen, rag_fusion_config=config.rag_fusion` to all `pipeline.explain(...)` call sites in the handler.
  - `ExplainResponse.from_pipeline_result(...)`: thread through `rag_fusion_applied=result.rag_fusion_applied`, `rag_fusion_queries_used=result.rag_fusion_queries_used`, `rag_fusion_attempted=result.rag_fusion_attempted`, `rag_fusion_failure_reason=result.rag_fusion_failure_reason`, and `rag_fusion_sub_queries` (map `result.rag_fusion_sub_query_results` `RagFusionSubQueryInfo` entries to `RagFusionSubQueryResult` by field name, not positional unpacking).
  - Update telemetry calls with RAG Fusion fields.
  - Add import: `from archon_search.rag_fusion import RAGFusionDependencyError` at the top of `routes_explain.py`.
  - Catch `RAGFusionDependencyError` (package not installed): return 422.
- **Releasable**: `POST /explain` with `rag_fusion=true` returns per-sub-query result sets; mutual exclusion with HyDE correct.
- **Tests (TDD)** — `tests/test_routes_explain.py`:
  - Unit: `test_explain_rag_fusion_true_skips_hyde` — `rag_fusion=True, hyde=True`; `resolve_hyde_vector` NOT called; `hyde_applied=False` in response.
  - Unit: `test_explain_rag_fusion_true_passes_to_pipeline` — mock pipeline result carries all five new fields; response includes them all.
  - Unit: `test_explain_rag_fusion_failure_reason_in_response` — result has `rag_fusion_attempted=True, rag_fusion_failure_reason="timeout"`; response maps both through.
  - Unit: `test_explain_rag_fusion_sub_queries_mapped_to_schema` — `rag_fusion_sub_query_results=[RagFusionSubQueryInfo(variant_index=0, result_count=3, top_doc_ids=["a",...]), RagFusionSubQueryInfo(variant_index=1, result_count=2, top_doc_ids=["b",...])]` on result → `rag_fusion_sub_queries=[RagFusionSubQueryResult(...), ...]` in response (mapped by field name).
  - Unit: `test_explain_rag_fusion_package_not_installed_returns_422` — pipeline raises `RAGFusionDependencyError`; response is 422.
  - Checkpoint: `uv run pytest tests/test_routes_explain.py -k "rag_fusion" -x`

---

### Phase 5 — MCP Wiring
> **Releasable**: after Task 5.1 — MCP `search`, `search_with_context`, and `explain` tools support `rag_fusion: bool`.

#### Task 5.1 — Wire MCP tools: `search`, `search_with_context`, `explain`
- [x] **File**: `archon_search/server/mcp.py`
- **Depends on**: Tasks 2.2, 2.3, 2.4, 4.1
- **Description**:
  - `create_mcp_app()` closure now has access to `rag_fusion_generator` via its parameter.
  - MCP `search` tool: add `rag_fusion: bool = False` parameter. Mutual exclusion at top of handler:
    ```python
    _rf_config = getattr(config, "rag_fusion", None) or RAGFusionConfig()
    _hyde_config = getattr(config, "hyde", None) or HyDEConfig()
    if rag_fusion:
        _hyde_vector, _hyde_applied = None, False
    else:
        _hyde_vector, _hyde_applied = await resolve_hyde_vector(query, hyde, hyde_generator, _hyde_config)
    ```
    Pass `rag_fusion=rag_fusion, rag_fusion_generator=rag_fusion_generator, rag_fusion_config=_rf_config` to pipeline call. Return dict includes `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted`, `hyde_applied=_hyde_applied`.
  - MCP `search_with_context` tool: same mutual exclusion + forwarding. Return dict: `{"results": [...], "hyde_applied": bool, "rag_fusion_applied": bool, "rag_fusion_queries_used": int, "rag_fusion_attempted": bool}`.
    > Note: The MCP handler must access `rag_fusion_applied`, `rag_fusion_queries_used`, and `rag_fusion_attempted` from the pipeline result; see Task 2.2 for the updated `search_with_context()` return type.
  - MCP `search_with_context` telemetry (pre-existing gap fixed here): the `TelemetryEntry.from_search_tool_result()` call at `mcp.py:420` currently omits all feature-flag metadata because `search_with_context()` previously returned `list[dict]` with no access to `SearchPipelineResult`. Now that the return type exposes the pipeline result, pass `hyde_applied=_swc_hyde_applied`, `rag_fusion_applied=pipeline_result.rag_fusion_applied`, `rag_fusion_queries_used=pipeline_result.rag_fusion_queries_used` to the telemetry call. This closes the gap for both HyDE and RAG Fusion in one go.
  - MCP `explain` tool: same mutual exclusion. Pass RAG Fusion params to `pipeline.explain()`. Return dict includes all five new ExplainResponse RAG Fusion fields (map sub_query_results tuples to dicts).
  - Add import: `from archon_search.rag_fusion import RAGFusionDependencyError` at the top of `mcp.py` (or within the closure if dynamically constructed — follow the pattern used for other error imports in that file).
  - Catch `RAGFusionDependencyError` (package not installed): return error dict with clear message.
  - Add `BREAKING.md` entries: "C5: MCP `search`, `search_with_context`, and `explain` tool return dicts gain `rag_fusion_applied: bool`, `rag_fusion_queries_used: int`, and `rag_fusion_attempted: bool` fields."
- **Releasable**: all three MCP tools accept `rag_fusion: bool` and propagate the new fields correctly.
- **Tests (TDD)** — `tests/test_mcp.py`:
  - Unit: `test_mcp_search_tool_rag_fusion_parameter_accepted` — `search(query="q", collection="c", rag_fusion=True)` dispatches without error (generator mocked).
  - Unit: `test_mcp_search_tool_rag_fusion_applied_in_result` — mock pipeline result has `rag_fusion_applied=True, rag_fusion_queries_used=2`; result dict has both values.
  - Unit: `test_mcp_search_tool_rag_fusion_true_skips_hyde` — `rag_fusion=True, hyde=True`; `resolve_hyde_vector` NOT called; `hyde_applied=False` in result dict.
  - Unit: `test_mcp_search_with_context_rag_fusion` — same pattern; result dict includes `rag_fusion_applied`, `rag_fusion_queries_used`, and `rag_fusion_attempted`.
  - Unit: `test_mcp_search_with_context_telemetry_includes_feature_flags` — mock writer; call `search_with_context` with `hyde=True`; assert telemetry entry has `hyde_applied=True` (pre-existing gap now fixed). Also assert `rag_fusion_applied` and `rag_fusion_queries_used` are present in the telemetry entry.
  - Unit: `test_mcp_explain_rag_fusion` — same pattern; result dict includes `rag_fusion_sub_queries`.
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "rag_fusion" -x`

---

### Phase 6 — Cross-Cutting Tests & CI Guard
> **Releasable**: after Task 6.3 — full test suite passes, CI guard active, integration tests green, eval harness updated. Task 6.4 (live E2E) is not a CI gate but must pass manually before merge.

#### Task 6.1 — Telemetry invariant CI guard for `rag_fusion.py`
- [x] **File**: `tests/test_no_query_log_in_rag_fusion.py` (new)
- **Depends on**: Task 1.2
- **Description**:
  - Analogous to `tests/test_no_query_log_in_hyde.py`. Copy the full guard implementation (both regex patterns, `_extract_call_args`, `_bare_query_in_log_violations`, all meta-tests). Change the final integration test to read `archon_search/rag_fusion.py` instead of `hyde.py`.
  - The guard checks that no `logging.`/`_logger.`/`logger.` call in `rag_fusion.py` receives the raw `query` variable (or variant text variables) directly — only via `_query_fingerprint(query)`.
  - **Decision**: The guard will explicitly check for these variable names in logging calls (in addition to `query`): `variants`, `variant`, `all_queries`, `truncated_query`. The names `text` and `line` are too generic for reliable static analysis and will NOT be checked by the guard. Instead, the `rag_fusion.py` implementation MUST NOT use bare local variable names containing user-derived text as logging arguments — use `_query_fingerprint()` for query-derived correlation tokens only. The CI guard meta-tests must verify each banned variable name in both positive (fires) and negative (clean) cases.
  - Note: the fingerprint function is now in `archon_search/_privacy.py` (extracted in Task 1.2). Both `test_no_query_log_in_rag_fusion.py` and the analogous `test_no_query_log_in_hyde.py` guard tests should verify that the same fingerprint function from `_privacy.py` is used in each module (rather than local duplicates).
  - Does NOT import the module — purely static text analysis.
  - Update `test_no_raw_query_in_rag_fusion_logging` (the final integration test) to scan `rag_fusion.py`.
- **Releasable**: CI fails if the no-raw-query invariant is broken in `rag_fusion.py`.
- **Tests**: the test IS the guard.
  - Checkpoint: `uv run pytest tests/test_no_query_log_in_rag_fusion.py -x`

#### Task 6.2 — E2E integration tests: real store, real HTTP, mock generator only
- [x] **File**: `tests/test_integration_rag_fusion.py` (new)
- **Depends on**: Tasks 4.2, 4.3, 5.1
- **Description**:
  Follow the pattern established by `tests/test_integration_hyde.py` exactly: real LanceDB store, real data ingested before each test, real HTTP calls through `TestClient`, mock only `RAGFusionGenerator.generate_variants` to avoid real Anthropic API calls. Do NOT use `AsyncClient` or mock the store.

  **Shared fixtures** (module-level, mirrors `test_integration_hyde.py`):
  - `_VECTOR_DIM = 384` — must match the stub fastembed dimension used in CI.
  - `_FIXED_VARIANTS = ["documentation alternative query", "hello world related search"]` — deterministic variants returned by the mocked generator.
  - `_COLLECTION = "ragfusioncol"`.
  - `async def _ingest_chunk(tmp_path: Path) -> None` — creates a `SearchStore`, calls `ensure_collection`, ingests one `ChunkRecord` with `text="hello world documentation"` and a zero vector, writes `CollectionMeta`, disconnects.
  - `def _make_app(tmp_path: Path, *, rag_fusion_enabled: bool = True)` — returns `create_app(SearchConfig(..., rag_fusion=RAGFusionConfig(enabled=rag_fusion_enabled)), JobStore(...))`.

  Mark all tests `@pytest.mark.integration` and `@pytest.mark.asyncio`.

  **Test cases (each calls `_ingest_chunk`, wraps in `patch("archon_search.rag_fusion.RAGFusionGenerator.generate_variants", new=AsyncMock(return_value=_FIXED_VARIANTS))` unless stated otherwise)**:

  - `test_search_rag_fusion_true_returns_200_applied_true` — `POST /search {"query":"hello world","collection":_COLLECTION,"rag_fusion":true}`; assert 200, `rag_fusion_applied=true`, `rag_fusion_queries_used=2`, `results` is a list (may be empty if no FTS/vector match but must be present).
  - `test_search_rag_fusion_false_returns_200_applied_false` — `POST /search {"rag_fusion":false}`; no generator mock needed; assert 200, `rag_fusion_applied=false`, `rag_fusion_attempted=false`; result shape identical to pre-C5.
  - `test_search_rag_fusion_true_hyde_true_mutual_exclusion` — `POST /search {"rag_fusion":true,"hyde":true}`; assert 200, `rag_fusion_applied=true`, `hyde_applied=false`; verify `resolve_hyde_vector` was NOT called (spy or assert no HyDEGenerator call).
  - `test_search_rag_fusion_generator_returns_empty_fallback` — mock returns `[]`; `POST /search {"rag_fusion":true}`; assert 200, `rag_fusion_applied=false`, `rag_fusion_attempted=true`, `rag_fusion_queries_used=0`; assert `results` list is present.
  - `test_search_rag_fusion_disabled_config_skips` — use `_make_app(tmp_path, rag_fusion_enabled=False)` (config kill-switch off); apply the generator mock (`patch(...generate_variants...)`) so that if the generator IS accidentally called, the mock records the call; `POST /search {"rag_fusion":true}`; assert 200, `rag_fusion_applied=false`; assert the mock's `call_count == 0` (generator was never called, confirming the kill-switch prevents the API call).
  - `test_explain_rag_fusion_true_returns_sub_queries` — `POST /explain {"query":"hello world","collection":_COLLECTION,"rag_fusion":true}`; assert 200, `rag_fusion_applied=true`, `rag_fusion_sub_queries` is a list with 3 entries (variant_index 0, 1, 2), each has `result_count` (int) and `top_doc_ids` (list).
  - `test_explain_rag_fusion_true_hyde_true_mutual_exclusion` — `POST /explain {"rag_fusion":true,"hyde":true}`; assert 200, `rag_fusion_applied=true`, `hyde_applied=false`.
  - `test_search_rag_fusion_telemetry_entry_written` — enable telemetry writer to a `tmp_path` log dir; `POST /search {"rag_fusion":true}`; read the written JSONL file; assert the entry has `rag_fusion_applied=true` and `rag_fusion_queries_used=2`. Ensures the telemetry call in `routes_search.py` propagates the RAG Fusion fields to disk.
  - `test_mcp_search_rag_fusion_true_applied_in_result` — call the MCP `search` tool directly (via the registered tool function, same as `tests/test_mcp.py`); assert result dict has `rag_fusion_applied=True`, `rag_fusion_queries_used=2`, `rag_fusion_attempted=True`.
  - `test_mcp_search_rag_fusion_true_hyde_true_mutual_exclusion` — MCP `search` with `rag_fusion=True, hyde=True`; assert `rag_fusion_applied=True`, `hyde_applied=False`.
  - `test_mcp_search_with_context_rag_fusion_result_and_telemetry` — MCP `search_with_context` with `rag_fusion=True`; assert result dict has `rag_fusion_applied`, `rag_fusion_queries_used`, `rag_fusion_attempted` keys; assert telemetry entry has `hyde_applied` and `rag_fusion_applied` fields (verifies the pre-existing HyDE telemetry gap is closed).

- **Releasable**: full-stack RAG Fusion path verified against real LanceDB without real Anthropic API calls.
- **Tests**: the file IS the integration test.
  - Checkpoint: `uv run pytest tests/test_integration_rag_fusion.py -m integration -x`

#### Task 6.3 — Eval harness: latency + recall regression scenarios
- [ ] **Files**: `tests/eval/test_eval_suite.py`, `tests/eval/thresholds.toml`, `tests/eval/README.md`
- **Depends on**: Task 2.2
- **Description**:
  - Add `[search_rag_fusion_disabled]` in `thresholds.toml`: `p95_ms = 5` (same ceiling as `[search_hyde_false]`). Guards that `rag_fusion=False` path adds zero overhead.
  - Add `[search_rag_fusion_enabled]` in `thresholds.toml`: `p95_ms = 15` (≤3× the 5 ms baseline p95, matching the brief's requirement). Guards against severe regressions on the enabled path.
  - Add eval test `test_eval_rag_fusion_regression_scenario` (`@pytest.mark.eval`): ingest committed corpus, run committed retrieval queries with mocked `RAGFusionGenerator.generate_variants` returning deterministic `[query + "_variant1", query + "_variant2"]`, assert `recall@5 >= thresholds.quality_floors.recall_at_5`. The deterministic backend cannot measure semantic improvement; this only verifies RAG Fusion does not break recall.
  - Add note in `tests/eval/README.md`: measuring recall *improvement* from RAG Fusion requires `@pytest.mark.live` with real fastembed + real Claude API.
  - Add latency benchmark tests `test_bench_search_rag_fusion_disabled_latency` and `test_bench_search_rag_fusion_enabled_latency` (both `@pytest.mark.benchmark`) to `tests/eval/test_eval_suite.py`; read ceilings from the new `[search_rag_fusion_disabled]` and `[search_rag_fusion_enabled]` sections.
- **Releasable**: `uv run pytest -m eval tests/eval/test_eval_suite.py` passes with RAG Fusion scenario; latency regression is guarded.
- **Tests**: the eval scenarios ARE the tests.
  - Checkpoint: `uv run pytest -m eval tests/eval/test_eval_suite.py -x`

#### Task 6.4 — Live E2E tests: real fastembed + real Anthropic API
- [ ] **File**: `tests/eval/live/test_live_rag_fusion.py` (new)
- **Depends on**: Task 6.2
- **Description**:
  Follow the pattern in `tests/eval/live/test_live_acceptance.py`. Requires `ANTHROPIC_API_KEY` set and real model weights. Mark all tests `@pytest.mark.live_eval`. Excluded from default CI; run manually or in a dedicated live-eval CI job.

  **Shared setup** (module-level):
  - Use `_build_pipeline_with_eval_backends(tmp_path, backend="live")` to get a pipeline with real fastembed embeddings.
  - Ingest the committed eval corpus (`tests/eval/corpus/`) into the pipeline's store.
  - Use a real `RAGFusionGenerator` (no mock) — `ANTHROPIC_API_KEY` must be present or the test is skipped via `pytest.importorskip` / `pytest.skip`.

  **Test cases**:

  - `test_live_rag_fusion_returns_applied_true` — call `pipeline.search(query="...", collection=..., rag_fusion=True, rag_fusion_generator=generator, rag_fusion_config=config.rag_fusion)`; assert `result.rag_fusion_applied is True`; assert `result.rag_fusion_queries_used >= 1` (at least one real LLM-generated variant was returned and searched).

  - `test_live_rag_fusion_variants_are_semantically_different` — after a search with `rag_fusion=True`, access `result.rag_fusion_sub_query_results` (via explain path); assert that the `top_doc_ids` for at least two different variant indices differ — i.e., the variants actually surface different documents, confirming semantic diversity. If all variants return identical top docs, emit a warning (not a failure — determinism is acceptable for some corpora).

  - `test_live_rag_fusion_recall_at_5_meets_floor` — run the committed eval query set with `rag_fusion=True` and real generator; compute `recall@5` against committed labels; assert `recall@5 >= thresholds.quality_floors.recall_at_5`. This is the only test that can measure whether RAG Fusion actually *improves* recall over single-query baseline — the deterministic eval backend in Task 6.3 cannot.

  - `test_live_rag_fusion_fallback_on_missing_key` — unset `ANTHROPIC_API_KEY` for this test; call `pipeline.search(..., rag_fusion=True, ...)`; assert `result.rag_fusion_applied is False` and `result.rag_fusion_attempted is True` (silent fallback confirmed end-to-end).

  - `test_live_search_with_context_rag_fusion` — exercise the full HTTP stack against a running server (or `TestClient`) with `rag_fusion=True` and real generator; assert response is 200, `rag_fusion_applied=true`, and `results` is non-empty.

  - `test_live_mcp_search_rag_fusion` — invoke the MCP `search` tool with `rag_fusion=True` and real generator; assert `rag_fusion_applied=True`, `rag_fusion_queries_used >= 1` in the result dict.

- **Releasable**: live E2E tests confirm RAG Fusion works end-to-end with real models and real LLM; recall improvement is measurable.
- **Tests**: the file IS the live test suite. Not run in default CI.
  - Run manually: `uv run pytest -m live_eval tests/eval/live/test_live_rag_fusion.py -v --no-cov`
  - Checkpoint before merge: at least `test_live_rag_fusion_returns_applied_true` and `test_live_rag_fusion_recall_at_5_meets_floor` must pass with a real API key.

---

### Phase 7 — Verification & Documentation

#### Task 7.1 — Final verification & documentation update + C5-ADR
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Write `Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md` — ADR documenting: (a) why LLM-based decomposition vs. heuristic (semantic richness is the point); (b) privacy trade-off (query text leaves the machine); (c) HyDE mutual exclusion design decision and rationale; (d) shared Anthropic API key operational risk (`[hyde].max_requests_per_minute + [rag_fusion].max_requests_per_minute` ≤ account limit); (e) evaluated alternatives; (f) the decision and rationale. ADRs are append-only — new file.
  - Verify `archon-search.toml.example` contains `[rag_fusion]` section with all five keys, privacy warning, and combined-rate-limit note (added in Task 4.1).
  - Update `Documentation/UserManual/` (operator guide) with RAG Fusion section: installation (`pip install archon-search[rag_fusion]`), `ANTHROPIC_API_KEY` setup, `[rag_fusion]` config keys and validation rules, `rag_fusion=true` request usage, privacy implications, mutual exclusion with HyDE, combined rate limit warning, and `rag_fusion_applied`/`rag_fusion_queries_used` response fields.
  - Update `Documentation/Architecture/600_api_reference_or_public_interface.md` with new request/response fields for `POST /search` and `POST /explain`.
  - Update `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` with `archon_search/rag_fusion.py` and `RAGFusionGenerator`.
  - Update `Documentation/Architecture/150_security_and_privacy_architecture.md` with RAG Fusion data-transmission privacy note and shared rate-limit operational risk.
  - Spawn an agent to scan all other documentation files and update any that describe the search request/response schema, the pipeline architecture, or the external API dependency list.
  - Run `uv run pytest` (full suite, no-cov override not allowed) and confirm all pass.
  - Regenerate the OpenAPI snapshot with `uv run --python 3.12 ...` and commit.
- **Releasable**: after this task, C5 is fully implemented, tested, documented, and ADR-accepted.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` exits 0 with coverage ≥ 85%.
  - `uv run pytest -m integration` exits 0 for RAG Fusion integration tests.
  - `uv run pytest -m eval tests/eval/test_eval_suite.py` exits 0.
  - `POST /search` with `rag_fusion=true` and `config.rag_fusion.enabled=true` returns 200 with `rag_fusion_applied: true` (mocked generator, 2 variants).
  - `POST /search` with `rag_fusion=true` and `config.rag_fusion.enabled=false` returns 200 with `rag_fusion_applied: false` (kill switch respected).
  - `POST /search` with `rag_fusion=true, hyde=true` returns 200 with `rag_fusion_applied: true, hyde_applied: false`.
  - `POST /search` with `rag_fusion=true` and no `ANTHROPIC_API_KEY` returns 200 with `rag_fusion_applied: false, rag_fusion_attempted: true, rag_fusion_queries_used: 0` (variant generation silent fallback; original query still searched).
  - `POST /search` with `rag_fusion=true` and `anthropic` package not installed returns 422 (`RAGFusionDependencyError` caught by route handler).
  - `POST /explain` with `rag_fusion=true` and mocked generator returns 200 with `rag_fusion_applied: true`, `rag_fusion_sub_queries` populated.
  - `POST /explain` with `rag_fusion=true` and LLM failure returns 200 with `rag_fusion_attempted: true`, `rag_fusion_failure_reason` non-empty.
  - MCP `search` tool with `rag_fusion=True` returns result dict with `rag_fusion_applied` and `rag_fusion_queries_used`.
  - MCP `search` tool with `rag_fusion=True, hyde=True` returns `rag_fusion_applied=True, hyde_applied=False`.
  - `tests/test_no_query_log_in_rag_fusion.py` passes.
  - `Documentation/ADRs/C5-rag-fusion-external-llm-dependency.md` exists and is non-empty.
  - `archon-search.toml.example` contains `[rag_fusion]` section with all five keys and rate-limit note.
  - `BREAKING.md` contains entries for `SearchResponse`, `ExplainResponse`, and MCP tool return changes introduced by C5.
  - OpenAPI snapshot is current (CI snapshot test passes).
  - `uv run pytest -m integration tests/test_integration_rag_fusion.py` exits 0 (real LanceDB E2E, all 11 test cases green).
  - Live E2E (manual, requires `ANTHROPIC_API_KEY`): `uv run pytest -m live_eval tests/eval/live/test_live_rag_fusion.py -v --no-cov` exits 0 for at minimum `test_live_rag_fusion_returns_applied_true` and `test_live_rag_fusion_recall_at_5_meets_floor`.
- **Tests (TDD)**: N/A — verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `bash ~/.claude/scripts/audit-plan-run.sh Documentation/Backlog/C5-rag-fusion-plan.md <sha_before_c5>`.

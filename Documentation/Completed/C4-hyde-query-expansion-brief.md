# Feature Brief: C4 — HyDE Query Expansion

## Problem
Dense retrieval misses semantically relevant documents when the user's query is short, keyword-heavy, or uses different vocabulary than the indexed content — the query embedding lands far from the answer embedding in vector space.

## Goal
Callers who opt in get measurably higher retrieval recall on semantic queries, with no latency regression for callers who do not opt in, and no availability risk from LLM-API outages.

## Users & Context
Developers and operators querying archon-search via REST or MCP on corpora where vocabulary mismatch is common: technical documentation, codebases, or domain-specific content. They are willing to pay extra latency for better results and pass `hyde=true` explicitly to signal that.

## Privacy & Data Transmission

**HyDE sends the user's raw query text to Anthropic's API servers.** This is a deliberate opt-in design decision that breaks the local-first runtime model for callers who enable it. Operators who have strict data residency requirements, air-gapped deployments, or confidentiality constraints on query content must not enable HyDE.

- Operators must be informed of this in the user manual and in `archon-search.toml.example` under the `[hyde]` section.
- An ADR (C4-ADR) is required before implementation documenting this architectural decision and why a local-model alternative was evaluated and rejected.
- The no-raw-query telemetry invariant applies to logging only; it does not and cannot prevent the query from being sent to the LLM for hypothesis generation. These are separate concerns.

## Core Flow
1. Caller sends a search request with `hyde=true`.
2. Server sends the original query to a Claude model (default: `claude-haiku-4-5-20251001`) asking it to write a short hypothetical answer passage.
3. If the LLM call succeeds within the configured timeout: embed the hypothesis text and use the resulting vector for ANN lookup instead of the original query vector.
4. If the LLM call times out or fails: silently fall back to embedding the original query; the search proceeds normally.
5. FTS leg (keyword search) always uses the original query text regardless of `hyde`.
6. The reranker ALWAYS receives the original user query string — never the hypothesis text. This is critical: the reranker must score candidates against the user's actual intent, not a generated passage.
7. RRF fusion runs as normal on the retrieved candidates.
8. Response includes `hyde_applied: bool` indicating whether the HyDE vector was actually used (true) or fallback occurred (false).

## Pipeline Integration Point

Two pipeline methods are modified, following the pattern already established by `explain()` which accepts `query_vector: list[float] | None`:

**`SearchPipeline.search()`** gains `query_vector: list[float] | None = None`:
- When provided, the `embedder.embed_one(query)` call is skipped. The skip pattern mirrors `explain()` at line 621: `vector = query_vector if query_vector is not None else await embedder.embed_one(query)`. The `embedder` keyword argument remains required in the signature.
- `query_vector` is added as a keyword-only parameter (after `*`); callers pass it as `query_vector=...` only.
- The `embedder` parameter remains required even when `query_vector` is provided — it is not called for embedding, but the signature is unchanged to avoid a breaking API change.
- The original `query` string passes through unchanged to `hybrid_search()` (FTS leg) and to the reranker.
- **Model-mismatch guard**: Before passing `query_vector` to `pipeline.search()`, the route handler must verify that the target collection's active embedding model matches `pipeline._global_embedder.model_name`. If it does not match, pass `query_vector=None` to force re-embedding with the collection's own embedder; set `hyde_applied: false` in the response. **Wiring locations #1, #3, #6, and #8** (all single-collection search and single-collection explain paths) require this explicit check. For locations #2, #4, #7, and #9 (all multi-collection paths), model mismatch is handled automatically by the existing per-collection exclusion logic inside the pipeline — no route-handler guard is needed. For location #5 (`search_with_context()`), it delegates to `search()` which already has the guard.

**`SearchPipeline.search_many()`** gains `query_vector: list[float] | None = None`:
- When provided, the `self._global_embedder.embed_one(query)` call at line 662 is skipped and `query_vector` is used as the shared vector for all per-collection `hybrid_search` calls in the fan-out.
- Unlike `search()`, `search_many()` uses `self._global_embedder` directly (no `embedder` parameter); only the `query_vector` override is needed.
- `query_vector` is added as a keyword-only parameter (after `*`); a `*` separator must be inserted before `query_vector` in `search_many()`'s signature since it currently has no `*` separator. Callers pass it as `query_vector=...` only.
- The HyDE vector is computed using the global embedder's model and is only valid for collections that match `_global_embedder.model_name`. Per-collection model mismatch handling in `search_many()` remains unchanged — mismatched collections are skipped as they currently are. `hyde_applied: true` applies only when the HyDE vector was actually used in at least one collection's ANN lookup. If all collections are skipped due to model mismatch, `hyde_applied: false` (the vector was generated but not used in any retrieval).

**`SearchPipeline.search_with_context()`** gains `query_vector: list[float] | None = None`:
- This method delegates to `self.search()` at line 799. The new `query_vector` parameter must be forwarded to that inner call.
- `query_vector` is added as a keyword-only parameter (after `*`); callers pass it as `query_vector=...` only.
- The `embedder` that `search_with_context()` passes to `search()` is likewise ignored when `query_vector` is provided.

A new module `archon_search/hyde.py` holds `HyDEGenerator`:
- `HyDEGenerator.generate(query: str) -> list[float] | None` — returns the hypothesis embedding or `None` on any failure.
- `HyDEGenerator` holds a reference to the global embedder (`pipeline._global_embedder` or equivalent). `generate()` calls LLM → hypothesis text → `global_embedder.embed_one(hypothesis_text)` → returns the float vector. The HyDE vector is ALWAYS in the global embedder's model space. Per-collection model-mismatch guards at wiring locations are responsible for discarding this vector when it is incompatible with the target collection.
- The `AsyncAnthropic` client is initialized once in `app.py:create_app()` and stored on `app.state.hyde_generator`. The same instance is passed to `mcp.py:create_mcp_app()` as a parameter, so REST and MCP share one client. Concretely: `HyDEGenerator` is constructed in `app.py:create_app()` and added to `app.state.hyde_generator`. It is passed explicitly as an additional parameter to `create_mcp_http_app()` (the function in `mcp.py` that creates the MCP FastAPI sub-application). MCP tool closures receive it via closure capture, mirroring how `pipeline`, `config`, and `embedder_cache` are currently passed.

A shared helper `resolve_hyde_vector(query, hyde_flag, generator) -> list[float] | None` is called explicitly in each of the 9 wiring locations — each route handler and MCP tool closure calls `resolve_hyde_vector()` directly before dispatching to the pipeline. The helper centralizes the HyDE logic (LLM call, fallback, fingerprint logging) but the call site exists once per wiring location, not in a shared middleware.

`resolve_hyde_vector()` is a fire-and-forget wrapper that never raises for transient failures. All error handling — including `TimeoutError`, `APIError`, rate-limit exhaustion, missing API key, and empty hypothesis — is handled internally; callers receive `list[float] | None`. One exception: if the `anthropic` package is not installed, `resolve_hyde_vector()` re-raises the `RuntimeError` from `HyDEGenerator` rather than returning `None`. This is a permanent configuration error (not a transient failure), and silent degradation would hide it indefinitely. Route handlers catch this specific `RuntimeError` and return a 422 with a clear message. All other failure modes are swallowed silently with a WARNING log.

**Wiring locations** (all 9 must receive the resolved vector):

Search paths (5):
1. `routes_search.py` — single-collection REST search (calls `pipeline.search()`)
2. `routes_search.py` — multi-collection REST search (calls `pipeline.search_many()`)
3. `mcp.py:search` tool — single-collection path (calls `pipeline.search()`)
4. `mcp.py:search` tool — multi-collection path (calls `pipeline.search_many()`)
5. `mcp.py:search_with_context` tool (calls `pipeline.search_with_context()` → delegates to `pipeline.search()`)

Explain paths (4):
6. `routes_explain.py` — REST single-collection explain (calls `pipeline.explain()`)
7. `routes_explain.py` — REST multi-collection explain (calls `pipeline.explain()`)
8. `mcp.py:explain` tool — single-collection path (calls `pipeline.explain()`)
9. `mcp.py:explain` tool — multi-collection path (calls `pipeline.explain()`)

**`explain` endpoint** (`routes_explain.py` + `mcp.py:explain` tool): `explain()` already accepts `query_vector: list[float] | None`. Supporting `hyde=true` for `explain` is nearly free — pass the resolved HyDE vector through the same parameter. This is explicitly in scope because `explain` is the primary debugging tool for search behavior, and operators need it to diagnose HyDE's effect.

**HyDE vector scope for explain**: The HyDE vector is used for the final ANN retrieval step only. The collection routing step (centroid scoring) always uses the original query embedding. The `resolve_hyde_vector()` call runs after routing has selected the target collection. Concretely: the existing `query_vector` computed by `pipeline._global_embedder.embed_one()` for routing is passed to the router unchanged; the HyDE vector replaces the `query_vector` argument to `pipeline.explain()` only after routing completes.

The existing model-mismatch nullification applies to the **MCP single-collection explain path only** (`mcp.py:553-554`). **Wiring location #6** (REST single-collection explain, `routes_explain.py`) requires a **NEW explicit model-mismatch guard** added by this feature: after the caller specifies a collection directly, check whether that collection's active embedding model matches `pipeline._global_embedder.model_name`. If it does not match, set the HyDE vector to `None` before calling `pipeline.explain()`. Without this guard, the HyDE vector (computed with the global embedder) will be passed to `pipeline.explain()` in the wrong embedding space, producing silently incorrect retrieval. Wiring location #7 (REST multi-collection explain) does NOT require this guard — the multi-collection pipeline branch already applies per-collection model-mismatch exclusion internally, consistent with `search_many()` (locations #2 and #4).

**Implementation note — variable naming**: The explain routing paths use `query_vector` for two distinct purposes. To avoid confusion, the implementation should use distinct variable names: `routing_vector` for the embedding used for centroid-based collection routing, and `retrieval_vector` (or `hyde_vector`) for the HyDE-augmented embedding passed to `pipeline.explain()`. The routing step uses `routing_vector = await pipeline._global_embedder.embed_one(body.query)`. After routing, `retrieval_vector = hyde_vector if (hyde_vector is not None and models_match) else None`.

**`search_with_context` clarification**: this is a MCP-only tool (no REST route). Internally it delegates to `pipeline.search_with_context()` which calls `pipeline.search()`. The HyDE vector is passed to the inner `search()` ANN lookup only. The adjacent-chunk context window fetch is index-based (by chunk ID), not vector-based — HyDE does not affect it.

## In Scope
- `hyde: bool` field on the search request schema (`REST SearchRequest`, `REST ExplainRequest`, MCP `search`, `search_with_context`, and `explain` tool parameters)
- `hyde_applied: bool = False` field on the search, search_with_context, and explain response schemas — default `False` preserves backward compatibility for clients that validate response schemas strictly. A `BREAKING.md` entry is required to document the schema surface change (adding the field) even though it has a default. (For `search_with_context`, which currently returns `list[dict[str, Any]]`, the return type changes to `dict` with shape `{"results": list[dict[str, Any]], "hyde_applied": bool}` — this is a **breaking change** to the MCP tool's return contract; a `BREAKING.md` entry is required. The default for `hyde_applied` in this dict is `False`.)
- `GET /openapi.json` snapshot must be regenerated after schema changes (`hyde: bool` on request, `hyde_applied: bool` on response); use `uv run --python 3.12` per project convention
- HyDE hypothesis generation via the `anthropic` Python SDK (`AsyncAnthropic` client) — a new **optional** dependency (`archon-search[hyde]`)
- `ANTHROPIC_API_KEY` environment variable (user-provisioned; not managed by `key_manager.py`)
- Configurable model and timeout in `archon-search.toml` under a `[hyde]` section (see Key Decisions for sub-keys)
- Graceful degradation: timeout or API error → silent fallback to original query, logged at WARNING with query fingerprint
- Dense search only: FTS always uses the original query
- MCP `search`, `search_with_context`, and `explain` tools receive a `hyde: bool` parameter (default false)
- Eval harness coverage: new scenario in `tests/eval/` (see Testing Requirements)
- Unit tests for all fallback paths; integration tests for the full request path; latency regression guard for non-HyDE path
- C4-ADR documenting the external LLM dependency decision

## Out of Scope
- `hyde_count` schema field and multi-hypothesis averaging (generating N>1 hypotheses and averaging their embeddings) — deferred; the eval harness will determine if N=1 is a ceiling before this is reconsidered
- HyDE applied to FTS / keyword leg — architecturally unsound; hypothetical text degrades keyword precision
- Streaming the hypothesis generation or surfacing the generated text to the caller
- Per-collection HyDE config — per-request flag covers the use case without added complexity
- RAG Fusion / multi-query decomposition (C5) — a separate, more complex feature
- Local-model alternative for hypothesis generation — deferred; the ADR must document this evaluation explicitly

## Key Decisions
- **Per-request opt-in (`hyde=true`) over always-on**: Zero overhead for the common case; callers who don't need it pay nothing; the eval harness can isolate the effect cleanly.
- **Graceful degradation over hard failure**: The retrieval server must not fail because the Claude API is slow or unavailable. Search quality degrades silently; availability does not.
- **Dense only**: HyDE was designed for embedding-space retrieval. Feeding generated text to BM25/FTS would likely hurt precision. The hybrid pipeline retains its keyword leg unmodified.
- **`anthropic` SDK over `claude-agent-sdk`**: HyDE is a hot-path, latency-sensitive operation. The `claude-agent-sdk` subprocess model (with its `_ENV_LOCK` serialization) adds unacceptable latency variance and serializes concurrent requests. The `anthropic` SDK makes direct HTTP calls and supports true async concurrency.
- **`anthropic` as optional dependency**: Following the `multilingual = ["fasttext-wheel>=0.9.2"]` pattern, HyDE is `[project.optional-dependencies] hyde = ["anthropic>=0.40,<2.0"]`. Upper bound `<2.0` guards against a major version breaking change; update the bound explicitly when migrating to a new major version. Lazy import implementation: `hyde.py` MUST NOT import `anthropic` at module level. Type hints for `anthropic` types use `TYPE_CHECKING` guards (`if TYPE_CHECKING: from anthropic import AsyncAnthropic`). At runtime, the `try/except ImportError` occurs inside `HyDEGenerator.__init__()` (or `generate()`), not at module load. This means `import archon_search.hyde` succeeds even when `anthropic` is absent; the `RuntimeError` is deferred to the first instantiation or first `hyde=true` request. `app.py:create_app()` constructs `HyDEGenerator` unconditionally at startup — if the package is absent, `HyDEGenerator.__init__()` catches the `ImportError` and stores `_available = False`; the first `hyde=true` request raises `RuntimeError`. Startup does NOT fail if `anthropic` is absent.
- **`AsyncAnthropic` client initialized once at startup**: Held in `HyDEGenerator`, stored on `app.state`, shared between REST and MCP servers. Per-request client creation is prohibited — it wastes TLS handshake time against the timeout budget.
- **`ANTHROPIC_API_KEY` via environment variable**: Consistent with `description_generator.py`. The `[hyde]` TOML section does NOT accept an inline API key — storing billing credentials in config files is a security anti-pattern.
- **`[hyde]` TOML sub-keys**: `enabled` (bool, default `false`), `model` (string, default `"claude-haiku-4-5-20251001"`), `timeout_seconds` (float, default `5.0`), `max_requests_per_minute` (int, default `60`). When `enabled = false` (the default), `resolve_hyde_vector()` returns `None` immediately regardless of `hyde=true` in the request, the `anthropic` package, or the API key — this is the operator-level kill switch for environments with data residency requirements. Per-request `hyde=true` is a necessary but not sufficient condition; the operator must also set `enabled = true`. The `HyDEConfig` dataclass follows the nested config pattern of `TelemetryConfig`. Implementation note: A `HydeConfig` dataclass (analogous to `TelemetryConfig`) must be added to `config.py`. The `load_config()` function requires a new parsing block for the `[hyde]` section using the existing `_coerce_float`, `_coerce_int` helpers, with validation that `timeout_seconds > 0` and `max_requests_per_minute > 0`. Validation: `enabled` must be explicitly set to `true` in `archon-search.toml` for HyDE to be available. The server logs an INFO message at startup when `enabled = true` to inform operators that query data will be sent to Anthropic.
- **Default timeout 5 seconds**: The existing `description_generator.py` uses `_TIMEOUT_SECONDS = 30` for the same model class. A 2-second timeout would cause frequent fallbacks under real-world network conditions (DNS + TLS + TTFT for Haiku). The 5-second default is a practical starting point; the actual p95 latency must be measured during implementation and the default tuned accordingly (tracked as an open question).
- **Rate limiting**: 60 HyDE calls/minute, per-process, in-memory token bucket. This is a pre-flight check before the API call, not a post-hoc counter. Each server process has its own counter — a multi-worker deployment (uvicorn `--workers N`) multiplies the effective limit by N. This is accepted behavior for v1; per-process rate limiting is documented in the user manual. Requests that exceed the limit fall back to the non-HyDE path with a WARNING log (at most once per minute to avoid log spam). Token bucket specification: refill rate is 1 token per second (60/minute continuous refill, not a fixed minute-window reset). Bucket capacity (burst depth) equals `max_requests_per_minute` — a fresh server can immediately absorb up to 60 concurrent `hyde=true` requests before the first refill. Callers should expect burst-60 depletes the bucket for ~60 seconds.
- **`hyde_applied: bool` in response**: Callers who explicitly pass `hyde=true` must be able to determine whether HyDE was actually applied or fell back. Without this signal, quality debugging is impossible.
- **`explain` supports `hyde`**: `explain()` already accepts `query_vector`. Extending HyDE to `explain` is essentially free and critical for the debugging workflow. An operator investigating why HyDE changed results must be able to use `explain` with the same HyDE vector.

## Prompt Safety

The user's raw query is embedded verbatim into an LLM prompt. To mitigate prompt injection:
- The prompt template MUST embed the user query as a delimited/quoted block (e.g., surrounded by `---` markers or XML-like delimiters), not as a bare instruction continuation.
- The hypothesis output is treated as untrusted data: it MUST NOT be logged verbatim (fingerprint only), returned in any response field, used in string interpolation, or stored anywhere.
- Hypothesis text is consumed solely by `embedder.embed_one()`. An adversarial hypothesis (e.g., one that contains markup, code, or unusual Unicode) cannot cause harm in the embedding pipeline — it becomes a float vector.
- The prompt template is finalized during implementation; the constraint above is non-negotiable regardless of template choice.

## Edge Cases & Constraints
- **LLM timeout (default 5s)**: Fall back to original query vector; log WARNING with query fingerprint (see below). `hyde_applied: false` in response. **HyDE fallback `except` blocks MUST use `type(exc).__name__` only, never `str(exc)`, `repr(exc)`, or `exc_info=True`**. The `anthropic` SDK's exception classes may include the request body (which contains the user query) in their string representation. Logging `str(exc)` or enabling `exc_info=True` on a fallback path would violate the no-raw-query invariant even though the format string only contains the fingerprint.
- **Claude API rate-limit or network error**: Same fallback path as timeout — no exception surfaces to the caller. `hyde_applied: false` in response.
- **Rate limit exceeded (local pre-flight)**: Requests beyond `max_requests_per_minute` fall back silently; WARNING logged at most once per minute.
- **Empty hypothesis response**: If the model returns an empty or whitespace-only string, fall back to original query. `hyde_applied: false`.
- **Very long queries**: Truncate the query to 2000 characters **before inserting it into the LLM prompt only**. The original (un-truncated) query string is always passed to FTS (`hybrid_search()`) and to the reranker unchanged — consistent with Core Flow steps 5 and 6. Character-based truncation avoids tokenizer dependency (both Claude's and fastembed's) and is simple to implement.
- **Hypothesis text length**: The LLM call MUST specify `max_tokens=256` (or a similar short bound) in the `messages.create()` call. Fastembed embedding models have a fixed token limit (e.g., 512 tokens for `bge-small-en-v1.5`). Without a `max_tokens` cap, the LLM can return a multi-thousand-token hypothesis that silently gets truncated by fastembed, wasting the HyDE call. A 256-token cap is consistent with the 'short hypothetical answer passage' instruction and keeps the hypothesis well within the embedding model's limit.
- **No Anthropic API key configured**: `HyDEGenerator.generate()` returns `None` immediately. A WARNING is logged **on the first `hyde=true` request** when `ANTHROPIC_API_KEY` is absent (lazy, not at startup — the server cannot know at startup whether HyDE will be requested). Subsequent requests with a missing key fall back silently.
- **`anthropic` package not installed**: `HyDEGenerator.generate()` raises `RuntimeError("Install archon-search[hyde] to use HyDE")`. Unlike transient failures, this `RuntimeError` is re-raised by `resolve_hyde_vector()` (not swallowed), and the route handler catches it and returns a 422 with a clear message. This behavior is intentional: a missing package is a permanent deployment error that operators must fix, not a transient condition to degrade silently.
- **Telemetry invariant**: The raw query string must not appear in any log entry from the HyDE code path. The **query fingerprint** used in WARNING logs is `sha256(query.encode()).hexdigest()[:16]` — a 16-character hex prefix that is non-reversible, contains no query substrings, and provides enough entropy to correlate log lines.
- **Multi-collection fan-out (`search_many`)**: The `resolve_hyde_vector()` helper generates the HyDE vector once before `search_many()` is called. The same `query_vector` is passed to `search_many()`, which uses it as the shared ANN vector across all per-collection `hybrid_search` calls in the fan-out. The FTS query string is the original query in all legs.
- **`hyde=false` or absent**: HyDE code path is entirely skipped; the `resolve_hyde_vector()` helper returns `None` immediately with no overhead beyond the conditional check.
- **Client cancellation**: If a client disconnects mid-request while the HyDE LLM call is in-flight, `asyncio.CancelledError` is propagated to the `AsyncAnthropic` call, cancelling the in-flight HTTP request and freeing the API quota budget.

## Testing Requirements

### Unit Tests (mock `anthropic.AsyncAnthropic` directly — NOT `ClaudeSDKClient` from `claude_agent_sdk`, which is a different SDK with a different mock pattern)
1. **Successful HyDE**: mock `messages.create` returns hypothesis text → verify hypothesis vector passed as `query_vector`, original `query` passed to FTS and reranker, `hyde_applied: true` in response.
2. **Timeout fallback**: mock raises `asyncio.TimeoutError` → original query vector used, WARNING logged with fingerprint (not raw query), `hyde_applied: false`, no exception raised.
3. **API error fallback**: mock raises `anthropic.APIError` → same fallback as timeout.
4. **No API key**: `ANTHROPIC_API_KEY` absent → fallback on first `hyde=true` request, WARNING logged exactly once, `hyde_applied: false`. (Not a startup WARNING — triggered by request.)
5. **Package not installed**: `anthropic` import guard raises `RuntimeError` → route handler catches and returns 422 with clear message.
6. **Empty hypothesis**: mock returns `""` → fallback to original query, `hyde_applied: false`.
7. **Rate limit exceeded**: after `max_requests_per_minute` pre-flight tokens exhausted, additional `hyde=true` requests fall back silently.
8. **Fingerprint safety**: assert the WARNING log message does not contain the raw query string; assert the fingerprint is exactly 16 hex chars.
9. **Explain with HyDE**: `explain` with `hyde=true` passes resolved `query_vector` to `pipeline.explain()`, same fallback semantics.
10. **`search_many()` HyDE forwarding**: mock `resolve_hyde_vector()` returns a vector; verify `pipeline.search_many()` receives it as `query_vector` and skips `self._global_embedder.embed_one()`; `hyde_applied: true` in response. Separate from test #1 which covers the single-collection path.
11. **`search_with_context()` HyDE forwarding**: mock returns a vector; verify the vector is forwarded to the inner `pipeline.search()` call; context window fetch is unaffected.
12. **MCP `search` tool HyDE**: same as test #1 but exercising the MCP code path (not the REST route handler).
13. **Concurrent rate limiter**: N concurrent `hyde=true` requests with a bucket of M tokens; assert exactly M requests return `hyde_applied: true` and the rest return `hyde_applied: false`.
14. **No-reranker path**: `hyde=true` with `config.reranker_model = ''`; verify HyDE vector is used for ANN and the reranker step is skipped without error.
15. **Explain routing + HyDE**: `explain` with `hyde=true`; verify the routing (centroid scoring) uses the original query vector, not the HyDE vector; the HyDE vector is passed to `pipeline.explain()` after routing completes.

### Integration Test (`@pytest.mark.integration`, mocked `AsyncAnthropic` client)
1. Full request path: `POST /search` with `{"query": "...", "hyde": true}` → valid response with `hyde_applied` field present, no exception, correct HTTP 200.

### Telemetry Invariant CI Guard
Two layers of invariant enforcement are required:
- **Structural guard (grep, analogous to `test_no_fstring_sql.py`)**: Scans `archon_search/hyde.py` for `logger.*` or `logging.*` calls passing a variable named `query` directly (not via the fingerprint function). Defense-in-depth only — catches the obvious case.
- **Runtime log-capture test (primary enforcement)**: Injects a sentinel query string through each fallback path (timeout, API error, rate limit, empty response, missing key), captures all log output at WARNING and above, and asserts the sentinel string does not appear in any log record. This verifies the invariant even when the query is aliased, interpolated into an exception message, or echoed via Anthropic SDK error bodies.

### Eval Harness
The default eval lane (`uv run pytest -m eval`) tests that HyDE does NOT break recall — a regression guard only:
- `hyde=True` results must have recall@K ≥ `baseline − allowed_regression` (threshold in `thresholds.toml`).
- The deterministic hash-based embedder cannot measure semantic improvement; this is expected.

Measuring whether HyDE *improves* recall requires `@pytest.mark.live` with real fastembed weights and real Claude API calls. This is calibrated once during implementation to establish the baseline improvement metric, which is then documented in `tests/eval/baselines/`.

### Latency Regression Guard
`thresholds.toml` must include a `[search_hyde_false]` scenario asserting p95 latency ≤ existing `[search_filtered]` baseline. This verifies that importing `archon_search/hyde.py` and the conditional `hyde=false` code path add no measurable overhead to non-HyDE requests.

## Open Questions
- What prompt template produces the best hypothesis quality across the eval corpus? Needs empirical testing during implementation. The safety constraint (delimited user query, untrusted output) is non-negotiable regardless of template choice.
- What is the empirically measured p95 latency for Claude Haiku hypothesis generation under real conditions? The 5s default must be validated and may need tuning.
- Should the `max_requests_per_minute` rate limit be per-key or global? Global per-process is simpler; per-key is more equitable under multi-tenant use.

## Future Iterations
- **Multi-hypothesis vector averaging** (N>1 embeddings averaged): implement once eval data shows N=1 is a ceiling. Will require a `hyde_count` schema field.
- **Local-model alternative**: generate hypotheses with a local instruction-tuned model (e.g., via `llama.cpp`) to preserve the local-first architecture. Requires ADR evaluation of quality vs. convenience tradeoff.
- **Caller-visible hypothesis** (`hyde_hypothesis` in response): useful for debugging but adds response schema surface; defer until there's demand.
- **Per-collection default**: if certain collections benefit reliably from HyDE, a collection-level `hyde_default: true` in collection metadata is a natural follow-on.
- **C5 — RAG Fusion / multi-query decomposition**: parallel sub-query execution with fused ranking; a separate, higher-complexity quality feature.

## Recommendation
This is the right feature to build after C3 ships — it targets the vocabulary-mismatch problem that no amount of structural enrichment can fix. The hardest parts are: (1) the graceful degradation path — getting the timeout, fallback, `hyde_applied` signaling, and query fingerprinting exactly right so the server never degrades in availability; (2) the C4-ADR — the external LLM dependency and query-data-leaving-the-machine are a significant architectural departure that must be documented and accepted explicitly; (3) the 5 wiring locations — use the `resolve_hyde_vector()` helper to centralize the logic and avoid per-handler duplication. Do not compromise on the graceful degradation or prompt safety constraints. The per-request opt-in, dense-only constraint, and single-hypothesis v1 scope keep implementation tight and the eval story clean. Note: the actual implementation scope is 9 wiring locations (5 search + 4 explain) — budget accordingly.

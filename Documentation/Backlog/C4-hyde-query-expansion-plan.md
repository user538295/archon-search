# C4 — HyDE Query Expansion

**Purpose**: Improve dense retrieval recall on vocabulary-mismatch queries by generating a short hypothetical answer passage via Claude Haiku and using its embedding as the ANN lookup vector in place of the original query embedding — while preserving full availability under LLM API failure.
**Audience**: archon-search contributors implementing C4; reviewers; operators who will opt in via `hyde=true`.
**Status**: To Do

---

## Background

Dense retrieval fails when the user's query vocabulary is far from the document vocabulary in embedding space. "How do I uninstall the CLI?" fails to retrieve "Remove the binary from your PATH" because the query vector lands nowhere near the answer vector. HyDE (Hypothetical Document Embeddings, Gao et al. 2022) solves this by asking an LLM to write a short hypothetical answer passage, embedding that passage, and using the resulting vector for ANN lookup. The hypothesis is closer to the answer in embedding space than the original query.

The full design, key decisions, edge cases, and privacy analysis are in `Documentation/Backlog/C4-hyde-query-expansion-brief.md`. That document is the authoritative source; this plan operationalises it.

**Privacy note**: HyDE sends the user's raw query to Anthropic's API servers. This is an explicit opt-in (per-request `hyde=true`) and must be documented for operators. An ADR is required before implementation (Task 7.1).

---

## Goal

After C4 ships: a caller who sends `hyde=true` on any search or explain request gets a response where the ANN lookup was driven by a Claude-generated hypothesis embedding. If the LLM call times out or fails for any reason, the server falls back silently to the original query embedding — `hyde_applied: false` in the response — without surfacing an error. Callers who send no `hyde` parameter pay zero overhead. The Anthropic SDK is an optional dependency (`archon-search[hyde]`); installations without it fall back with a clear 422 error.

---

## Scope

### In Scope
- `HyDEConfig` dataclass (`enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`) + `[hyde]` TOML section loader
- `archon_search/hyde.py` — `HyDEGenerator` class with optional-import guard, per-process token-bucket rate limiter, and `generate(query) -> list[float] | None`
- `query_vector: list[float] | None = None` parameter on `SearchPipeline.search()`, `search_many()`, and `search_with_context()`
- `hyde: bool = False` on `SearchRequest` and `ExplainRequest`
- `hyde_applied: bool` on `SearchResponse` and `ExplainResponse`
- `resolve_hyde_vector(query, hyde_flag, generator, config) -> tuple[list[float] | None, bool]` helper with `config.enabled` kill-switch check (9 wiring sites: 5 search + 4 explain)
- `BREAKING.md` entries for `SearchResponse.hyde_applied`, `ExplainResponse.hyde_applied`, and MCP `search_with_context` return type change
- `HyDEGenerator` initialized in `app.py:create_app()`, stored on `app.state.hyde_generator`, passed to `create_mcp_app()`
- REST wiring: `routes_search.py` (single + multi collection), `routes_explain.py`
- MCP wiring: `search`, `search_with_context`, `explain` tools in `mcp.py`
- `anthropic>=0.40` as optional dependency under `[project.optional-dependencies] hyde`
- `ANTHROPIC_API_KEY` environment variable (user-provisioned)
- `archon-search.toml.example` update for `[hyde]` section
- Telemetry invariant CI guard for `archon_search/hyde.py`
- Integration test (`@pytest.mark.integration`)
- Eval harness HyDE regression scenario + latency regression threshold in `thresholds.toml`
- C4-ADR documenting the external LLM dependency decision

### Out of Scope
- `hyde_count` and multi-hypothesis averaging — deferred
- HyDE on the FTS / keyword leg
- Streaming hypothesis generation or surfacing hypothesis text
- Per-collection HyDE config
- Local-model alternative for hypothesis generation (covered in ADR)
- RAG Fusion / C5

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- `SearchPipeline.search()` call sites that do NOT pass `query_vector` — all default to `None` (no behaviour change)
- FTS leg — always receives the original `query` string regardless of `hyde`
- Reranker — always receives the original `query` string
- Telemetry `TelemetryEntry` factories — no `query` parameter added (structural invariant preserved)
- `key_manager.py` and `ARCHON_SEARCH_API_KEY` — unrelated to `ANTHROPIC_API_KEY`
- `description_generator.py` — uses `claude-agent-sdk`, untouched
- All existing tests
- LanceDB schema

---

## Known limitations / accepted trade-offs
- **Rate limit is per-process, in-memory**: a multi-worker deployment multiplies effective limit by worker count. Documented in operator guide.
- **Silent fallback on API failure**: callers know via `hyde_applied: false` but receive no error detail. Quality degrades silently; availability never does.
- **Hypothesis embedder dimension must match collection embedder**: HyDE generates the hypothesis with the global fastembed model. Per-collection embedding models (C1) are unaffected — the hypothesis vector is generated using whichever embedder is passed to `HyDEGenerator`.
- **Multi-collection fan-out**: one hypothesis generated before fan-out; same vector used across all collection legs. Slightly suboptimal if collections use different embedding models.
- **`anthropic` package is optional**: `hyde=true` without the package installed returns 422 (not a fallback). This is intentional — it is a deployment configuration error, not a runtime transient failure.
- **Startup has no visibility into whether HyDE will be requested**: missing `ANTHROPIC_API_KEY` warning fires on the first `hyde=true` request, not at startup.
- **Prompt injection**: the user query is embedded in the LLM prompt. Mitigation: query is quoted/delimited in the template; hypothesis output is consumed only by the embedder and never logged or returned.

---

## Architecture

### New dataclass — `archon_search/config.py`

```python
@dataclass
class HyDEConfig:
    enabled: bool = False                        # operator kill switch — must be true for HyDE to run
    model: str = "claude-haiku-4-5-20251001"   # DEFAULT_FAST_MODEL
    timeout_seconds: float = 5.0
    max_requests_per_minute: int = 60
```

`SearchConfig` gains `hyde: HyDEConfig = field(default_factory=HyDEConfig)`.

### New TOML section — `~/.archon-search/archon-search.toml`

```toml
[hyde]
# WARNING: setting enabled = true causes query text to be sent to Anthropic's API servers.
# Do not enable in air-gapped deployments or where data residency requirements apply.
enabled = false
model = "claude-haiku-4-5-20251001"
timeout_seconds = 5.0
max_requests_per_minute = 60
```

### New module — `archon_search/hyde.py`

```python
def _query_fingerprint(query: str) -> str:
    """sha256(query)[:16] — non-reversible log correlation token."""
    ...

class HyDEGenerator:
    def __init__(self, embedder: Embedder, config: HyDEConfig) -> None:
        # Lazy-imports anthropic; raises RuntimeError on missing package.
        # Initialises AsyncAnthropic client once (reused across requests).
        # Initialises in-memory token bucket for max_requests_per_minute.
        ...

    async def generate(self, query: str) -> list[float] | None:
        # Pre-flight rate check (token bucket) → fallback + WARNING if exhausted
        # asyncio.wait_for(..., timeout=config.timeout_seconds) around messages.create
        # Truncate query to 2000 chars before inserting into prompt
        # Query quoted with --- delimiters in prompt template
        # On success: embed hypothesis text → return list[float]
        # On AsyncioTimeoutError / anthropic.APIError / empty response: log WARNING
        #   with _query_fingerprint(query), return None
        # Hypothesis text is untrusted: never logged verbatim
        ...
```

**Prompt template** (structure only — exact wording TBD empirically):
```
Write a short passage that would directly answer the following question.
Output only the passage — no preamble, no explanation.

---
{query[:2000]}
---
```

### Helper function — `archon_search/server/_hyde_helper.py` (or inline in routes)

```python
async def resolve_hyde_vector(
    query: str,
    hyde: bool,
    generator: HyDEGenerator | None,
    config: HyDEConfig,
) -> tuple[list[float] | None, bool]:
    """Returns (hyde_vector, hyde_applied).

    hyde_vector is None when hyde=False, config.enabled=False, generator is None,
    or generation fails. hyde_applied is True only when a non-None vector is returned.
    """
    ...
```

### Pipeline method signature changes — `archon_search/pipeline.py`

```python
async def search(
    self,
    query: str,
    collection: str,
    namespace: str = DEFAULT_NAMESPACE,
    *,
    embedder: Embedder,
    filters: SearchFilters | None = None,
    query_vector: list[float] | None = None,   # NEW
) -> SearchPipelineResult:
    # vector = query_vector if query_vector is not None else await embedder.embed_one(query)
    # (mirrors explain() at line 621)
    ...

async def search_many(
    self,
    query: str,
    collections: list[str],
    namespace: str = DEFAULT_NAMESPACE,
    query_vector: list[float] | None = None,   # NEW
) -> SearchPipelineResult:
    # vector = query_vector if query_vector is not None else await self._global_embedder.embed_one(query)
    ...

async def search_with_context(
    self,
    ...,
    query_vector: list[float] | None = None,   # NEW — forwarded to self.search()
) -> ...:
    ...
```

### Schema changes — `archon_search/server/routes_search.py`

```python
class SearchRequest(BaseModel):
    ...
    hyde: bool = False                          # NEW

class SearchResponse(BaseModel):
    ...
    hyde_applied: bool = False                  # NEW
```

### Schema changes — `archon_search/server/routes_explain.py`

```python
class ExplainRequest(BaseModel):
    ...
    hyde: bool = False                          # NEW

class ExplainResponse(BaseModel):
    ...
    hyde_applied: bool = False                  # NEW
```

### App factory changes — `archon_search/server/app.py`

```python
# In create_app() lifespan or synchronous setup:
from archon_search.hyde import HyDEGenerator
app.state.hyde_generator = HyDEGenerator(
    embedder=app.state.global_embedder,
    config=config.hyde,
)
```

`create_mcp_app()` in `mcp.py` gains `hyde_generator: HyDEGenerator | None = None` parameter.

### Optional dependency — `pyproject.toml`

```toml
[project.optional-dependencies]
multilingual = ["fasttext-wheel>=0.9.2"]
hyde = ["anthropic>=0.40"]                     # NEW
```

---

## Task breakdown

### Phase 1 — Config + HyDEGenerator
> **Releasable**: after Task 1.2 — `HyDEGenerator.generate()` is callable with a mocked Anthropic client and all unit tests pass.

#### Task 1.1 — `HyDEConfig` dataclass + `[hyde]` TOML loader
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing
- **Description**:
  - Add `HyDEConfig` dataclass with four fields: `enabled: bool = False`, `model: str = DEFAULT_FAST_MODEL`, `timeout_seconds: float = 5.0`, `max_requests_per_minute: int = 60`. Import `DEFAULT_FAST_MODEL` from `archon_search.constants`.
  - Add `hyde: HyDEConfig = field(default_factory=HyDEConfig)` to `SearchConfig`.
  - Add `[hyde]` TOML section loader in `load_config()` following the exact pattern used for `telemetry_cfg` / `TelemetryConfig` (lines 310-333): extract section with `doc.get("hyde", {})`, apply field-by-field with `_coerce_bool` / `_coerce_str` / `_coerce_float` / `_coerce_int`, validate `timeout_seconds > 0` and `max_requests_per_minute >= 1`, raise `ConfigError` on invalid values.
  - `_coerce_str` helper (analogous to `_coerce_int`) if not already present.
  - `model` must be a non-empty string; raise `ConfigError` if empty.
- **Releasable**: `load_config()` returns a `SearchConfig` with `config.hyde.model == "claude-haiku-4-5-20251001"` when `[hyde]` section is absent from TOML.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_hyde_config_defaults` — load_config with no TOML file returns `HyDEConfig` with `enabled=False` and all other defaults.
  - Unit: `test_hyde_toml_all_keys` — TOML with `[hyde] enabled=true model="gpt-test" timeout_seconds=10.0 max_requests_per_minute=30` parses correctly.
  - Unit: `test_hyde_toml_partial_keys` — TOML with only `[hyde] timeout_seconds=3.0` applies that value; other fields remain default; `enabled` defaults to `False`.
  - Unit: `test_hyde_config_invalid_timeout` — `timeout_seconds = 0` raises `ConfigError`.
  - Unit: `test_hyde_config_invalid_rpm` — `max_requests_per_minute = 0` raises `ConfigError`.
  - Unit: `test_hyde_config_empty_model` — `model = ""` raises `ConfigError`.
  - Checkpoint: `uv run pytest tests/test_config.py -k "hyde" -x`

#### Task 1.2 — `HyDEGenerator` class + optional import guard + rate limiter
- [x] **File**: `archon_search/hyde.py` (new)
- **Depends on**: Task 1.1
- **Description**:
  - `_query_fingerprint(query: str) -> str`: returns `hashlib.sha256(query.encode()).hexdigest()[:16]`. Pure function, no logging.
  - `HyDEGenerator.__init__(self, embedder: Embedder, config: HyDEConfig) -> None`:
    - Lazy-imports `anthropic` inside try/except `ImportError`; stores `_anthropic_available: bool`. Does NOT raise at init time.
    - Creates `AsyncAnthropic()` client (reads `ANTHROPIC_API_KEY` from env automatically) if available.
    - Creates `asyncio.Lock` and `_rpm_tokens: int = config.max_requests_per_minute`, `_rpm_refill_at: float = 0.0` for the token bucket. The token bucket is checked and decremented under the lock.
    - `_warned_no_key: bool = False` — flag for one-time missing-key warning.
  - `HyDEGenerator.generate(self, query: str) -> list[float] | None`:
    - If `_anthropic_available` is `False`: raise `RuntimeError("Install archon-search[hyde] to use HyDE")`.
    - Token bucket pre-flight (under `_lock`): refill tokens if the wall-clock minute has passed; if tokens == 0: log `WARNING` with fingerprint (rate limited to once/minute via `_rate_limit_warned_at`), return `None`.
    - Check `ANTHROPIC_API_KEY` in `os.environ`: if absent and not `_warned_no_key`: log `WARNING` once, set `_warned_no_key = True`, return `None`.
    - Truncate `query` to 2000 characters. Build prompt with `---` delimiters around the truncated query.
    - `asyncio.wait_for(self._client.messages.create(model=config.model, max_tokens=200, messages=[{"role": "user", "content": prompt}]), timeout=config.timeout_seconds)`.
    - Extract `response.content[0].text` (strip whitespace). If empty: log WARNING with fingerprint, return `None`.
    - `await asyncio.to_thread(self._embedder.embed_one_sync, hypothesis_text)` — or use the async `embed_one` if available. Return the vector as `list[float]`.
    - Catch `asyncio.TimeoutError`, `anthropic.APIError` (and subclasses), `Exception`: log WARNING with fingerprint (never log `query` or hypothesis text verbatim), return `None`.
  - `resolve_hyde_vector(query: str, hyde: bool, generator: "HyDEGenerator | None", config: HyDEConfig) -> tuple[list[float] | None, bool]`:
    - Standalone async function (not a method). If `not hyde` or `generator is None` or `not config.enabled`: return `(None, False)`.
    - `config.enabled` is the operator-level kill switch: when `False`, HyDE never runs regardless of `hyde=true` in the request, installed package, or API key.
    - Call `await generator.generate(query)`. If result is not `None`: return `(result, True)`. Else return `(None, False)`.
  - Module has no top-level `import anthropic` — all anthropic imports are inside `__init__` under the try/except guard.
- **Releasable**: `HyDEGenerator.generate()` is callable; all fallback paths tested with mocked `AsyncAnthropic`.
- **Tests (TDD)** — `tests/test_hyde.py` (new):
  - Unit: `test_generate_success` — mock `AsyncAnthropic.messages.create` returns a non-empty text; verify result is a list of floats, length matches embedder dimension.
  - Unit: `test_generate_timeout_fallback` — mock raises `asyncio.TimeoutError`; result is `None`, WARNING logged with 16-char hex fingerprint.
  - Unit: `test_generate_api_error_fallback` — mock raises `anthropic.APIError`; result is `None`.
  - Unit: `test_generate_empty_response_fallback` — mock returns `""` after strip; result is `None`.
  - Unit: `test_generate_no_api_key` — `ANTHROPIC_API_KEY` absent from env; result is `None`, WARNING logged exactly once across two calls.
  - Unit: `test_generate_package_not_installed` — simulate `ImportError` from lazy import; `generate()` raises `RuntimeError` with "Install archon-search[hyde]" message.
  - Unit: `test_rate_limit_fallback` — exhaust token bucket; next call returns `None` with WARNING.
  - Unit: `test_fingerprint_no_raw_query` — assert the WARNING log message (captured via `caplog`) does NOT contain the raw query string; assert fingerprint is exactly 16 hex chars.
  - Unit: `test_resolve_hyde_vector_hyde_false` — `resolve_hyde_vector(query, False, generator, config)` returns `(None, False)` without calling `generate`.
  - Unit: `test_resolve_hyde_vector_no_generator` — `resolve_hyde_vector(query, True, None, config)` returns `(None, False)`.
  - Unit: `test_resolve_hyde_vector_enabled_false_kill_switch` — `config.enabled=False` with `hyde=True` and a valid generator returns `(None, False)` without calling `generate`.
  - Unit: `test_resolve_hyde_vector_success` — `config.enabled=True`, generator returns a vector; result is `(vector, True)`.
  - Unit: `test_query_truncated_to_2000_chars` — query of 3000 chars; verify the prompt passed to the LLM contains at most 2000 chars of query content.
  - Checkpoint: `uv run pytest tests/test_hyde.py -x`

---

### Phase 2 — Pipeline Signature Extensions
> **Releasable**: after Task 2.3 — all three pipeline methods accept `query_vector` with no behaviour change when `None`.

#### Task 2.1 — `SearchPipeline.search()` gains `query_vector`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: nothing (pure signature extension; no callers change yet)
- **Description**:
  - Add `query_vector: list[float] | None = None` as a keyword-only parameter to `search()` (after `filters`).
  - Change line 504 from `vector = await embedder.embed_one(query)` to:
    ```python
    vector = list(query_vector) if query_vector is not None else await embedder.embed_one(query)
    ```
    Mirror the pattern from `explain()` at line 621. `list()` coerces in case caller passes a numpy array.
  - No other changes.
- **Releasable**: `search(..., query_vector=[...])` uses the provided vector for ANN lookup; `search(..., query_vector=None)` behaves identically to pre-C4.
- **Tests (TDD)** — `tests/test_pipeline.py` (extend existing):
  - Unit: `test_search_uses_provided_query_vector` — pass a known `query_vector`; verify `embedder.embed_one` is NOT called; verify the vector is passed to `store.hybrid_search`.
  - Unit: `test_search_embeds_when_no_query_vector` — `query_vector=None`; verify `embedder.embed_one` IS called.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "query_vector" -x`

#### Task 2.2 — `SearchPipeline.search_many()` gains `query_vector`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: nothing
- **Description**:
  - Add `query_vector: list[float] | None = None` as a parameter to `search_many()` (after `namespace`).
  - Change line 662 from `vector = await self._global_embedder.embed_one(query)` to:
    ```python
    vector = list(query_vector) if query_vector is not None else await self._global_embedder.embed_one(query)
    ```
  - No other changes.
- **Releasable**: `search_many(..., query_vector=[...])` uses the provided vector across all collection legs; `query_vector=None` behaves identically to pre-C4.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_many_uses_provided_query_vector` — pass `query_vector`; verify `_global_embedder.embed_one` NOT called.
  - Unit: `test_search_many_embeds_when_no_query_vector` — `query_vector=None`; verify `_global_embedder.embed_one` IS called.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "search_many" -x`

#### Task 2.3 — `SearchPipeline.search_with_context()` gains `query_vector`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1 (inner `search()` call must accept `query_vector`)
- **Description**:
  - Add `query_vector: list[float] | None = None` parameter to `search_with_context()`.
  - Forward it to the inner `self.search(...)` call (line 799).
  - The adjacent-chunk context window fetch is index-based and does NOT use `query_vector`.
- **Releasable**: `search_with_context(..., query_vector=[...])` passes the vector through to the ANN leg.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_search_with_context_forwards_query_vector` — pass `query_vector`; verify it reaches `search()` and `embed_one` is NOT called.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -k "search_with_context" -x`

---

### Phase 3 — Schema Changes
> **Releasable**: after Task 3.2 — request/response Pydantic models carry `hyde` and `hyde_applied` fields; OpenAPI snapshot updated.

#### Task 3.1 — `SearchRequest` gains `hyde: bool`; `SearchResponse` gains `hyde_applied: bool`
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: nothing
- **Description**:
  - Add `hyde: bool = False` to `SearchRequest` (after `filters`).
  - Add `hyde_applied: bool = False` to `SearchResponse`.
  - No route handler changes in this task — the field is wired in Phase 4.
  - Add an entry to `BREAKING.md` documenting the schema surface change: adding `hyde_applied: bool` to `SearchResponse` (default `False` preserves backward compatibility for clients that ignore unknown fields; required for clients that validate schema strictly).
  - Update the OpenAPI snapshot: `uv run --python 3.12 python -c "..."` (follow existing snapshot script).
- **Releasable**: `POST /search` with `{"hyde": true}` deserialises without error; response schema includes `hyde_applied`.
- **Tests (TDD)** — `tests/test_routes_search.py` (extend):
  - Unit: `test_search_request_hyde_default_false` — `SearchRequest(query="q", collection="c")` has `hyde == False`.
  - Unit: `test_search_request_accepts_hyde_true` — `SearchRequest(..., hyde=True)` validates without error.
  - Unit: `test_search_response_has_hyde_applied` — `SearchResponse(results=[], acl_filtered=False)` has `hyde_applied == False`.
  - Checkpoint: `uv run pytest tests/test_routes_search.py -k "hyde" -x`

#### Task 3.2 — `ExplainRequest` gains `hyde: bool`; `ExplainResponse` gains `hyde_applied: bool`
- [ ] **File**: `archon_search/server/routes_explain.py`
- **Depends on**: nothing
- **Description**:
  - Add `hyde: bool = False` to `ExplainRequest`.
  - Add `hyde_applied: bool = False` to `ExplainResponse`.
  - Update the `ExplainResponse.from_pipeline_result()` classmethod to thread `hyde_applied` through (value passed in by the route handler in Phase 4, not by the pipeline result object itself — pass as an explicit argument).
  - Add an entry to `BREAKING.md` documenting the schema surface change: adding `hyde_applied: bool` to `ExplainResponse`.
  - Update OpenAPI snapshot.
- **Releasable**: `POST /explain` with `{"hyde": true}` deserialises; response includes `hyde_applied`.
- **Tests (TDD)** — `tests/test_routes_explain.py` (extend):
  - Unit: `test_explain_request_hyde_default_false`
  - Unit: `test_explain_request_accepts_hyde_true`
  - Unit: `test_explain_response_has_hyde_applied`
  - Checkpoint: `uv run pytest tests/test_routes_explain.py -k "hyde" -x`

---

### Phase 4 — App Wiring + REST Route Handlers
> **Releasable**: after Task 4.3 — `POST /search` and `POST /explain` respect `hyde=true`; `hyde_applied` is correct in all responses.

#### Task 4.1 — `HyDEGenerator` initialization in `app.py` + `pyproject.toml` optional dep
- [ ] **Files**: `archon_search/server/app.py`, `pyproject.toml`
- **Depends on**: Task 1.2
- **Description**:
  - Add `hyde = ["anthropic>=0.40"]` to `[project.optional-dependencies]` in `pyproject.toml`.
  - In `app.py:create_app()` (in the lifespan or synchronous setup, after `SearchPipeline` is constructed): import `HyDEGenerator` from `archon_search.hyde`, instantiate `app.state.hyde_generator = HyDEGenerator(embedder=app.state.global_embedder, config=config.hyde)`.
  - The `RuntimeError` from `HyDEGenerator.__init__` should NOT be raised at startup even if `anthropic` is absent — the lazy-import check defers to `generate()`. Startup always succeeds.
  - If `config.hyde.enabled` is `True`: log an INFO message at startup: `"HyDE is enabled — search query text will be sent to Anthropic's API (model: %s)"`. This is the operator visibility signal.
  - `create_mcp_app()` in `mcp.py` signature gains `hyde_generator: "HyDEGenerator | None" = None` parameter (string annotation to avoid circular import).
  - All call sites of `resolve_hyde_vector()` must pass `config.hyde` as the fourth argument.
- **Releasable**: server starts with `app.state.hyde_generator` set; `POST /search` with `hyde=false` works end-to-end with no regression.
- **Tests (TDD)** — `tests/test_app.py` (extend):
  - Unit: `test_app_state_has_hyde_generator` — `create_app()` sets `app.state.hyde_generator` to a `HyDEGenerator` instance.
  - Unit: `test_hyde_optional_dep_in_pyproject` — read `pyproject.toml` and assert `[project.optional-dependencies].hyde` contains `anthropic`.
  - Unit: `test_app_startup_logs_info_when_hyde_enabled` — `config.hyde.enabled=True`; assert `caplog` contains the startup INFO message with model name and "Anthropic's API".
  - Unit: `test_app_startup_no_log_when_hyde_disabled` — `config.hyde.enabled=False`; assert no HyDE INFO message is logged.
  - Checkpoint: `uv run pytest tests/test_app.py -k "hyde" -x`

#### Task 4.2 — Wire `routes_search.py` REST handler
- [ ] **File**: `archon_search/server/routes_search.py`
- **Depends on**: Tasks 2.1, 2.2, 3.1, 4.1
- **Description**:
  - Import `resolve_hyde_vector` from `archon_search.hyde`.
  - In the `search` handler (line 131), before the `if body.collections is not None` branch:
    ```python
    generator = getattr(request.app.state, "hyde_generator", None)
    hyde_vector, hyde_applied = await resolve_hyde_vector(body.query, body.hyde, generator)
    ```
  - Catch `RuntimeError` from `resolve_hyde_vector` (package not installed): return `JSONResponse({"detail": str(e)}, status_code=422)`.
  - Single-collection path: pass `query_vector=hyde_vector` to `pipeline.search(...)`.
  - Multi-collection path: pass `query_vector=hyde_vector` to `pipeline.search_many(...)`.
  - Both response constructions: include `hyde_applied=hyde_applied` in `SearchResponse(...)`.
- **Releasable**: `POST /search` with `hyde=true` uses HyDE vector; `hyde_applied` is `true` or `false` correctly.
- **Tests (TDD)** — `tests/test_routes_search.py`:
  - Unit: `test_search_hyde_true_passes_vector_to_pipeline` — mock `resolve_hyde_vector` returns `([0.1, ...], True)`; verify `pipeline.search` called with `query_vector=[0.1, ...]`; response has `hyde_applied=True`.
  - Unit: `test_search_hyde_fallback_passes_none` — mock `resolve_hyde_vector` returns `(None, False)`; verify `pipeline.search` called with `query_vector=None`; response has `hyde_applied=False`.
  - Unit: `test_search_hyde_package_not_installed_returns_422` — mock `resolve_hyde_vector` raises `RuntimeError`; response is 422.
  - Unit: `test_search_many_hyde_true` — multi-collection path passes `query_vector` to `search_many`.
  - Checkpoint: `uv run pytest tests/test_routes_search.py -x`

#### Task 4.3 — Wire `routes_explain.py` REST handler
- [ ] **File**: `archon_search/server/routes_explain.py`
- **Depends on**: Tasks 2.1 (explain uses search internally), 3.2, 4.1
- **Description**:
  - Import `resolve_hyde_vector` from `archon_search.hyde`.
  - In the `explain_endpoint` handler (line 271), resolve the HyDE vector at the top of the handler body, before any pipeline dispatch:
    ```python
    generator = getattr(request.app.state, "hyde_generator", None)
    hyde_vector, hyde_applied = await resolve_hyde_vector(body.query, body.hyde, generator)
    ```
  - Catch `RuntimeError`: return 422.
  - Pass `query_vector=hyde_vector` to all `pipeline.explain(...)` call sites in the handler.
  - `ExplainResponse.from_pipeline_result(result, hyde_applied=hyde_applied)` — thread through (update the classmethod signature in Task 3.2 to accept this kwarg).
- **Releasable**: `POST /explain` respects `hyde=true`; `hyde_applied` is correct.
- **Tests (TDD)** — `tests/test_routes_explain.py`:
  - Unit: `test_explain_hyde_true_passes_vector` — mock `resolve_hyde_vector` returns a vector; verify `pipeline.explain` called with `query_vector=...`; response `hyde_applied=True`.
  - Unit: `test_explain_hyde_false_passes_none` — `hyde=false`; `pipeline.explain` called with `query_vector=None`; response `hyde_applied=False`.
  - Checkpoint: `uv run pytest tests/test_routes_explain.py -x`

---

### Phase 5 — MCP Wiring
> **Releasable**: after Task 5.1 — MCP `search`, `search_with_context`, and `explain` tools support `hyde: bool`.

#### Task 5.1 — Wire MCP tools: `search`, `search_with_context`, `explain`
- [ ] **File**: `archon_search/server/mcp.py`
- **Depends on**: Tasks 2.1, 2.2, 2.3, 4.1
- **Description**:
  - `create_mcp_app()` signature gains `hyde_generator: "HyDEGenerator | None" = None`.
  - In `app.py`, pass `hyde_generator=app.state.hyde_generator` when calling `create_mcp_app()`.
  - `create_mcp_app()` gains `hyde_generator: "HyDEGenerator | None" = None`; all `resolve_hyde_vector()` calls pass `config.hyde` as the fourth argument.
  - MCP `search` tool (`@app.tool()` at line 167): add `hyde: bool = False` parameter. At the top of the handler, call `resolve_hyde_vector(query, hyde, hyde_generator, config.hyde)` (generator and config captured via closure over `create_mcp_app` scope). Pass `query_vector` to `pipeline.search()` or `pipeline.search_many()` as appropriate. Return dict must include `hyde_applied`.
  - MCP `search_with_context` tool (line 333): add `hyde: bool = False`. Same pattern. Pass `query_vector` to `pipeline.search_with_context()`. **Breaking change**: the return type changes from `list[dict[str, Any]]` to `{"results": list[dict[str, Any]], "hyde_applied": bool}`. Add a `BREAKING.md` entry documenting this MCP tool contract change.
  - MCP `explain` tool (line ~428): add `hyde: bool = False`. Same pattern. Pass `query_vector` to `pipeline.explain()`. Include `hyde_applied` in return dict `{"results": ..., "hyde_applied": bool}`.
  - Catch `RuntimeError` (package not installed): return error dict with clear message.
- **Releasable**: MCP `search`, `search_with_context`, and `explain` tools accept `hyde: bool` and propagate the vector correctly.
- **Tests (TDD)** — `tests/test_mcp.py` (extend):
  - Unit: `test_mcp_search_tool_hyde_parameter_accepted` — `search(query="q", collection="c", hyde=True)` dispatches without error (generator mocked).
  - Unit: `test_mcp_search_tool_hyde_applied_in_result` — mock `resolve_hyde_vector` returns `(vector, True)`; result dict has `hyde_applied=True`.
  - Unit: `test_mcp_search_with_context_hyde` — same pattern for `search_with_context`.
  - Unit: `test_mcp_explain_hyde` — same pattern for `explain`.
  - Checkpoint: `uv run pytest tests/test_mcp.py -k "hyde" -x`

---

### Phase 6 — Cross-Cutting Tests & CI Guard
> **Releasable**: after Task 6.3 — full test suite passes with HyDE, CI guard active, integration test green, eval harness updated.

#### Task 6.1 — Telemetry invariant CI guard for `hyde.py`
- [ ] **File**: `tests/test_no_query_log_in_hyde.py` (new)
- **Depends on**: Task 1.2
- **Description**:
  - Analogous to `tests/test_no_fstring_sql.py`. Read `archon_search/hyde.py` as a string. Assert that no `logging.` or `logger.` call in the file passes the raw `query` variable directly (e.g., `logger.warning("...", query)` or `f"...{query}..."`). Use regex: if `query` appears as an argument to a logging call without going through `_query_fingerprint`, fail.
  - The test does NOT need to import the module — purely static text analysis.
  - The guard should fail for `logger.warning("q=%s", query)` but pass for `logger.warning("fp=%s", _query_fingerprint(query))`.
- **Releasable**: CI fails if the telemetry invariant is broken in `hyde.py`.
- **Tests**: the test IS the guard.
  - Checkpoint: `uv run pytest tests/test_no_query_log_in_hyde.py -x`

#### Task 6.2 — Integration test: full search path with `hyde=true`
- [ ] **File**: `tests/test_integration_hyde.py` (new)
- **Depends on**: Tasks 4.2, 5.1
- **Description**:
  - Mark with `@pytest.mark.integration`.
  - Use the existing `AsyncClient` + `create_app()` test pattern.
  - Mock `HyDEGenerator.generate` to return a fixed vector (avoid real API calls).
  - `POST /search` with `{"query": "test", "collection": "...", "hyde": true}` → assert HTTP 200, `hyde_applied == True` in response, results list is present.
  - `POST /search` with `{"query": "test", "collection": "...", "hyde": false}` → assert HTTP 200, `hyde_applied == False`.
  - `POST /explain` with `{"query": "test", "collection": "...", "hyde": true}` → assert HTTP 200, `hyde_applied == True`.
- **Releasable**: full-stack HyDE request path verified without real API calls.
- **Tests**: the file IS the integration test.
  - Checkpoint: `uv run pytest tests/test_integration_hyde.py -m integration -x`

#### Task 6.3 — Eval harness HyDE regression scenario + latency threshold
- [ ] **Files**: `tests/eval/` (new scenario), `tests/eval/thresholds.toml`
- **Depends on**: Tasks 2.1, 2.2, 4.2
- **Description**:
  - Add a `[search_hyde_regression]` eval scenario: run `hyde=true` with a mocked `HyDEGenerator` (deterministic vector — e.g., zero vector or fixed float array). Assert `recall@K >= baseline - allowed_regression`. The deterministic embedder cannot measure semantic improvement; this scenario only verifies HyDE does not *break* recall.
  - Add `[search_hyde_false]` latency scenario in `thresholds.toml`: assert `p95_ms` for `hyde=false` requests ≤ the existing `[search_filtered]` p95 ceiling. This confirms the `resolve_hyde_vector(hyde=False)` fast-path adds no measurable overhead.
  - Note in `tests/eval/README.md`: measuring recall *improvement* from HyDE requires `@pytest.mark.live` with real fastembed + real Claude API — not part of the default eval gate.
- **Releasable**: `uv run pytest -m eval` passes with HyDE scenario; latency regression is guarded.
- **Tests**: the eval scenarios ARE the tests.
  - Checkpoint: `uv run pytest -m eval tests/eval/test_eval_suite.py -x`

---

### Phase 7 — Verification & Documentation

#### Task 7.1 — Final verification & documentation update + C4-ADR
- [ ] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Write `Documentation/ADRs/C4-hyde-external-llm-dependency.md` — ADR documenting: (a) why HyDE requires an external LLM API rather than a local model; (b) the privacy trade-off (query text leaves the machine); (c) the evaluated alternatives (local model, skip HyDE); (d) the decision and rationale. ADRs are append-only; this is a new ADR.
  - Update `archon-search.toml.example` with a `[hyde]` section, all four keys (`enabled`, `model`, `timeout_seconds`, `max_requests_per_minute`), correct defaults, and a comment noting that `enabled = true` sends query text to Anthropic's API.
  - Update `Documentation/UserManual/` (operator guide) with a HyDE section covering: installation (`pip install archon-search[hyde]`), `ANTHROPIC_API_KEY` setup, `[hyde]` config, `hyde=true` request usage, privacy implications, `hyde_applied` response field.
  - Update `Documentation/Architecture/600_api_reference_or_public_interface.md` with the new `hyde` request field and `hyde_applied` response field for `/search` and `/explain`.
  - Update `Documentation/Architecture/110_component_catalog_and_layer_breakdown.md` with `archon_search/hyde.py` and `HyDEGenerator`.
  - Update `Documentation/Architecture/150_security_and_privacy_architecture.md` with the HyDE data-transmission privacy note.
  - Spawn an agent to scan all other documentation files and update any that describe the search request/response schema, the pipeline architecture, or the dependency list.
  - Run `uv run pytest` (full suite, no-cov override not allowed) and confirm all pass.
  - Regenerate the OpenAPI snapshot with `uv run --python 3.12 ...` and commit.
- **Releasable**: after this task, C4 is fully implemented, tested, documented, and ADR-accepted.
- **Acceptance criteria** (must all pass):
  - `uv run pytest` exits 0 with coverage ≥ 85%.
  - `uv run pytest -m integration` exits 0 for the HyDE integration test.
  - `uv run pytest -m eval tests/eval/test_eval_suite.py` exits 0.
  - `POST /search` with `hyde=true` and `config.hyde.enabled=true` returns 200 with `hyde_applied: true` (mocked generator).
  - `POST /search` with `hyde=true` and `config.hyde.enabled=false` returns 200 with `hyde_applied: false` (kill switch respected).
  - `POST /search` with `hyde=true` and no `ANTHROPIC_API_KEY` returns 200 with `hyde_applied: false`.
  - `POST /search` with `hyde=true` and `anthropic` not installed returns 422.
  - `POST /explain` with `hyde=true` and `config.hyde.enabled=true` returns 200 with `hyde_applied: true` (mocked generator).
  - MCP `search` tool with `hyde=True` returns a result dict containing `hyde_applied`.
  - MCP `search_with_context` returns `{"results": [...], "hyde_applied": bool}` (not bare list).
  - `tests/test_no_query_log_in_hyde.py` passes.
  - `Documentation/ADRs/C4-hyde-external-llm-dependency.md` exists and is non-empty.
  - `archon-search.toml.example` contains a `[hyde]` section with all four keys.
  - `BREAKING.md` contains entries for `SearchResponse.hyde_applied`, `ExplainResponse.hyde_applied`, and MCP `search_with_context` return type change.
  - OpenAPI snapshot is current (CI snapshot test passes).
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `bash ~/.claude/scripts/audit-plan-run.sh Documentation/Backlog/C4-hyde-query-expansion-plan.md <sha_before_c4>`.

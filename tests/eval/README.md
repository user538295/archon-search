# archon-search eval harness

This directory contains the search evaluation harness: fixture corpus,
baselines, thresholds, and pytest-driven eval tests. It exists to detect
retrieval / routing / latency regressions on every change.

## Layout

```
tests/eval/
  corpus/                # actual document files referenced by documents.jsonl
  documents.jsonl        # fixture document manifest
  queries.jsonl          # eval queries
  labels.jsonl           # query → doc relevance grades
  routing/               # optional routing fixtures (e.g. collections.jsonl)
  runtime.toml           # eval runtime config (backends, k, etc.)
  thresholds.toml        # quality floors + latency ceilings + policy
  baselines/
    baseline.json        # machine-readable measured baseline metadata
    baseline.md          # human-readable baseline rationale / notes
    regenerate.py        # baseline refresh helper
  skip_xfail_allowlist.toml
```

## Schemas

### `documents.jsonl`

One JSON object per line. See
`archon_search/eval/fixtures.py::EvalDocument` for the canonical schema.

Fields:

- `doc_id` (str, required) — stable fixture ID used in labels and
  metrics. Must be unique across the manifest.
- `collection` (str, required) — collection name; must match
  `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`.
- `relative_path` (str, required) — path under `corpus/`. Must be
  relative (no leading `/`), with no `..` traversal, and unique across
  the manifest. The file must exist under `corpus/`; orphan files
  under `corpus/` not declared here cause load to fail.

### `queries.jsonl`

See `EvalQuery`. Fields:

- `query_id` (str, required, unique).
- `text` (str, required) — query text.
- `metric_scope` (str, required) — `"retrieval"` or `"routing"`.
- `collection` (str | null) — target collection. Required for
  retrieval-scope queries; may be `null` only when
  `metric_scope == "routing"`.
- `routing_bypass` (bool, optional, default `false`).

### `labels.jsonl`

See `RelevanceLabel`. Fields:

- `query_id` (str, required) — must reference a known query.
- `doc_id` (str, required) — must reference a known document.
- `grade` (int, optional, default `1`) — document-level relevance grade,
  must be `>= 0`. A grade of `0` is a negative label; positives are
  `grade > 0`.

Every query must have at least one positive label. For retrieval-scope
queries, every positive label's document must belong to the query's
target collection (loader rejects "unreachable positives").

## Thresholds vs baselines

- `baselines/baseline.json` records the **measured** state of the
  harness on a known-good run (current quality + latency + hashes).
- `thresholds.toml` declares the **floors and ceilings** the suite
  enforces:
  - Quality metrics (recall, nDCG, MRR, reranker lift, routing
    accuracy) are **minimums** — a measured value below the floor fails.
  - Latency metrics (`latency_p50_ms`, `latency_p95_ms`) are **ceilings
    and only enforced when set**. Leaving them unset (or `null` in the
    baseline) disables the latency gate.

Floors should be set **at or below the corresponding baseline value**,
never above. The relationship is:
`threshold_floor <= baseline_metric_value`.

### `baselines/baseline.json` schema

- `metrics` (object) — measured metric values from the baseline run
  (e.g. `recall_at_1`, `ndcg_at_5`, `mrr`, `reranker_lift`,
  `routing_accuracy`, `latency_p50_ms`, `latency_p95_ms`).
- `eval_hash` (str) — hash of the eval fixture corpus.
- `runtime_config_hash` (str) — hash of `runtime.toml`.
- `thresholds_hash` (str) — hash of `thresholds.toml`.
- `command` (str) — exact pytest invocation that produced the baseline.
- `waiver_ids` (object) — reviewed waivers keyed by metric name (see
  the waiver policy below).

## Refreshing thresholds from measured baselines

1. Run the report-only calibration to produce fresh measured metrics:

   ```
   uv run pytest --no-cov -m eval -k "report_only" tests/eval/test_eval_suite.py -v
   ```

2. Update `baselines/baseline.json` (and the human notes in
   `baselines/baseline.md`) with the new measured values, eval/runtime/
   thresholds hashes, and the command used. `baselines/regenerate.py`
   automates this.
3. Update `thresholds.toml` so each floor is at or below the new
   baseline value. **Never set a floor above its baseline.**
4. If you intentionally **lower** any floor below the previous
   baseline, record a written rationale in `baselines/baseline.md`
   (preferred) or in the commit message that touches `thresholds.toml`.
   Reviewers will reject a lowered floor with no rationale.

### Floor-drop waiver policy

`thresholds.toml` declares
`[policy].max_floor_drop_without_waiver = 0.05` (default). Any single
floor lowered by more than this delta from the previous threshold
requires a reviewed waiver:

- Add an entry under `waiver_ids` in `baselines/baseline.json` keyed by
  the affected metric, pointing at the review (e.g. PR / issue ID).
- Document the reason in `baselines/baseline.md`.

Floor drops at or below `max_floor_drop_without_waiver` only need
rationale, not a waiver ID.

## Document-level scoring

Retrieval metrics score at the **document level**, not the chunk level.
Runtime hits (chunks) are mapped back to their fixture `doc_id` via
`build_doc_collection_map` and then **deduplicated** to the set of
unique doc_ids that appear in the top-k before computing recall, nDCG,
and MRR. Chunk-level duplicates of the same document collapse to a
single hit.

## Latency caveat

The v1 latency numbers in baselines and thresholds are measured against
**deterministic eval backends** (stubbed embedders / rerankers) over a
local LanceDB index. They exist as **regression guards** for the eval
harness itself — they are **not production SLAs** and must not be
compared to live server latency.

## Filter constraints for fixture authors

- When writing new `queries.jsonl` entries that exercise filtered search, use only `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, `indexed_before`, or `language` as filter fields.
- **`language` filter** (C2): valid for single-collection retrieval queries. Use ISO 639-1 (e.g. `"fr"`, `"de"`) or ISO 639-3 codes. The value `"unknown"` is also valid. Multi-collection (`collection: null`) queries must **not** use `language` — the fan-out path rejects it with HTTP 422.

## Local commands

Report-only calibration (no gating, prints measured metrics):

```
uv run pytest --no-cov -m eval -k "report_only" tests/eval/test_eval_suite.py -v
```

Gated eval (enforces `thresholds.toml`):

```
uv run pytest --no-cov -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py -v
```

Default unmarked eval units (contract / fixture / metric tests, fast):

```
uv run pytest --no-cov tests/eval/ -q
```

## Live eval lane

The `live_eval` pytest marker runs the eval corpus through real fastembed and cross-encoder model weights. It is separate from the deterministic `eval` marker — use it for CI quality checks on tag push and for calibrating the live baseline.

### Directory structure

```
tests/eval/
  live/
    __init__.py
    conftest.py            # overrides autouse to isolate ARCHON_SEARCH_EVAL_BACKENDS
    test_live_eval_suite.py
    test_live_acceptance.py
  live_thresholds.toml     # live quality floors + latency ceilings (stub until calibrated)
  live_baselines/
    .gitkeep
    baseline.json          # calibrated baseline (absent until first calibration run)
    _artifacts/            # CI upload target (.gitignore keeps it clean locally)
```

### Conftest isolation

`tests/eval/live/conftest.py` no-ops the parent `conftest.py` autouse fixture that sets `ARCHON_SEARCH_EVAL_BACKENDS=1`. Live tests must never run under deterministic backends, so the no-op shadow prevents the env var from leaking in.

### Calibration procedure

1. Run the live eval suite on a clean install with model weights:

   ```
   uv run pytest -m live_eval tests/eval/live/ -v --no-cov
   ```

2. Inspect the printed report. If the metrics look reasonable, copy the measured values into `live_baselines/baseline.json`. The file schema mirrors `baselines/baseline.json` with six additional model-version fields (`embedding_model_id`, `embedding_model_version`, `reranker_model_id`, `reranker_model_version`, `archon_search_version`, `captured_at`).

3. Update `live_thresholds.toml` with initial quality floors. Recommended starting points:
   - Quality floors: measured baseline value − 0.02 pp (e.g. `recall_at_1 = 0.86` if measured is `0.88`).
   - Latency ceiling: `latency_p95_ms` = 1.5× the measured baseline (e.g. `3000` if baseline is `2000`).

4. Commit `live_baselines/baseline.json` and the updated `live_thresholds.toml` together.

### Outlier checklist

Before lowering a live threshold, check:

- Was the run on cold or warm model cache? Cold first-run latency is up to 10× warmer.
- Are the CI host specs comparable to the baseline host? (Runner CPU count, RAM)
- Did the fastembed or cross-encoder model version change? Update `embedding_model_version` / `reranker_model_version` in the baseline.
- Did the eval fixture corpus change? Regenerate the baseline.

### `live_thresholds.toml` lifecycle

`live_thresholds.toml` starts as a comment-only stub (no `[quality_floors]` section). In this state `load_live_thresholds()` returns `None` and the CI run is **report-only** — no gates fire. Once calibrated, add a `[quality_floors]` section to activate gating. The latency ceiling is always optional; leave it unset until you have a stable CI baseline.

### CI latency variance caveat

Live latency numbers are measured on ephemeral GitHub Actions runners under shared CPU, model download cache, and OS scheduling noise. Latency thresholds derived from CI baselines may not reflect local or production performance. Set latency ceilings generously (1.5×) and treat them as regression guards rather than SLAs.

## Live benchmark lane (C16)

The `live_benchmark` pytest marker runs two hard-gated latency benchmarks using **real fastembed BAAI/bge-small-en-v1.5 and Xenova/ms-marco-MiniLM-L-6-v2 ONNX models** on every PR. This is the only marker excluded from the default `uv run pytest` run — see below.

### Directory structure

```
tests/eval/
  live_benchmark/
    __init__.py
    conftest.py                         # stub removal, thread reset, model-cache skip hook
    test_real_model_search_benchmark.py # two benchmark tests
  live_thresholds.toml                  # [real_model_search] section for live_benchmark
                                        # [quality_floors] section for live_eval
```

### Conftest isolation and stub removal

`tests/eval/live_benchmark/conftest.py` performs three actions at module load time (before any test import):

1. **Stub removal** — removes `fastembed`, `fastembed.rerank`, and `fastembed.rerank.cross_encoder` from `sys.modules` so that `from fastembed import TextEmbedding` resolves to the real package, not the `_FakeTextEmbedding` installed by `tests/_search_stubs.py`.
2. **Thread reset** — sets `ORT_NUM_THREADS`, `OMP_NUM_THREADS`, and related env vars to `os.cpu_count()`, overriding the single-threaded values from the root conftest.
3. **Shadow fixture** — defines `_activate_deterministic_eval_backends` as a function-scoped autouse no-op, shadowing the parent `tests/eval/conftest.py` autouse fixture that would otherwise set `ARCHON_SEARCH_EVAL_BACKENDS=1` and inject deterministic stubs into the pipeline.

Because step 1 runs at module level (unconditionally at conftest import), the `live_benchmark` conftest cannot safely co-exist in the same process as the default test suite. This is why `not live_benchmark` is in `addopts` — it is process-isolation, not mere convenience.

### Model-cache skip hook

A session-scoped autouse fixture `_require_model_cache` skips the entire session if either `*bge-small*` or `*ms-marco-MiniLM*` blobs are absent from the fastembed cache directory (`$FASTEMBED_CACHE_PATH` or `~/.cache/fastembed`). On developer machines without a prefetched cache, the session skips rather than fails.

This is defense-in-depth. The primary protection is the `not live_benchmark` in `addopts` — a developer running `uv run pytest` never loads this conftest at all.

### Running locally

```bash
# Will skip gracefully if model cache is absent (no model download triggered)
uv run pytest -m live_benchmark tests/eval/live_benchmark/ --no-cov

# Force-run a single test (still skips if cache absent)
uv run pytest tests/eval/live_benchmark/test_real_model_search_benchmark.py::test_real_model_search_steady_state_p95 -v --no-cov
```

To prefetch models locally:

```python
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
TextEmbedding("BAAI/bge-small-en-v1.5")
TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")
```

### Calibration procedure

The thresholds in `tests/eval/live_thresholds.toml` under `[real_model_search]` must be calibrated on ubuntu-latest CI runners. Darwin/aarch64 (Apple Silicon) is 2–5× faster — do not use local measurements as the source of truth.

1. Trigger `workflow_dispatch` on `archon-search-pr.yml` 10 times on ubuntu-latest. Read the p95/p90 values printed in the `Run real-model latency benchmark` step output for each run.
2. Compute calibrated thresholds:
   - `steady_state_p95_ms = median_of_10_runs × 2`
   - `cold_load_p90_ms = median_of_10_runs × 3`
3. Update `tests/eval/live_thresholds.toml`:
   - Replace the values under `[real_model_search]`.
   - Add a provenance comment block with date, runner type, and 10 raw samples.
4. Run `uv run pytest tests/eval/test_runner.py -k "live_thresholds_toml" -v` to confirm the file parses correctly.

### Updating thresholds after a legitimate regression

If a fastembed upgrade or model change legitimately increases latency:

1. Calibrate new thresholds using the procedure above (10 CI runs, median × multiplier).
2. Update `[real_model_search]` in `live_thresholds.toml` with new values and updated provenance comment.
3. Note the change in the PR description: what changed, why the threshold increased, and which CI run was used for calibration.

### What the gate catches vs. what it does not

**Catches**:
- Regressions in fastembed version upgrades (model download path, API changes)
- ONNX session configuration regressions (thread count, execution provider)
- Accidental re-introduction of `sys.modules` stubs in the search path
- Large latency regressions (threshold = 2× / 3× CI median, so 2× the measured latency will fail)

**Does not catch**:
- Isolated embedder-only or reranker-only regressions (the steady-state test measures the full pipeline)
- Embedding batching regressions (single-query benchmark; batching is an ingest-path concern)
- GPU-specific regressions (CI uses CPU-only ONNX; GPU path requires a GPU runner)
- Memory footprint regressions
- Fresh-process startup cost (the cold-load test re-constructs backends within one process; ONNX shared libraries persist)

## Routing fixture schema (B4)

`routing/collections.jsonl` — one JSON object per line:

```json
{"name": "code", "description": "..."}
{"name": "docs", "description": "..."}
{"name": "mixed", "description": "..."}
{"name": "faq", "description": "..."}
```

**Coupling rules** (all four files must be updated together):
- `routing/collections.jsonl` — add collection `name` and `description`
- `tests/eval/corpus/<name>/` — add ≥1 corpus file so the collection has a non-zero centroid
- `documents.jsonl` — add entries with `"collection": "<name>"` pointing to corpus files
- `queries.jsonl` — add routing-scope queries (`"metric_scope": "routing"`) targeting the new collection
- `labels.jsonl` — add positive labels for every new routing query

**Precondition**: every collection in `routing/collections.jsonl` must have ≥1 corpus file that produces a non-zero centroid under the deterministic SHA-256 eval embedder. Violating this causes the ranked list to contain unscored-fallback entries in arbitrary order, silently corrupting MRR. `test_fixture_all_routing_collections_have_scorable_centroids` enforces this.

### Rank-sensitive routing metrics (added in B4 Task 1.2)

`routing_mrr_centroid` / `routing_mrr_hybrid` — Mean Reciprocal Rank over routing-scope traces. Each routing query contributes RR = 1/position of the first gold collection in the ranked list (0.0 if not found). Macro-averaged.

`routing_precision_at_1_centroid` / `routing_precision_at_1_hybrid` — fraction of routing queries where rank-1 is the gold collection.

### Hybrid floor policy

`routing_mrr_hybrid` floor is set to the measured `routing_mrr_centroid` baseline value (Δ ≥ 0 constraint): hybrid must be at least as good as the centroid baseline. A value below the floor fails the eval gate.

### Threshold hash refresh note

Adding routing queries or corpus files changes `eval_hash` and `thresholds_hash`. After B4 fixture changes, run `baselines/regenerate.py` to update both `baseline.json` and `baseline.md` before pushing.

## HyDE eval scenarios (C4)

`tests/eval/test_eval_suite.py` includes two C4-specific eval scenarios:

- **`test_eval_hyde_regression_scenario`** (`@pytest.mark.eval`) — verifies that
  supplying a custom `query_vector` to `pipeline.search()` does not crash or
  empty the result set.  Uses a deterministic (zero-vector or real-query-aligned)
  vector rather than a live LLM call.  This scenario only guards against *breakage*
  of the HyDE plumbing path — it does **not** measure recall *improvement*.

- **`test_eval_hyde_false_fast_path_no_overhead`** (`@pytest.mark.eval`) — asserts
  that `resolve_hyde_vector(hyde=False, ...)` returns `(None, False)` without
  calling the generator, confirming the fast-path adds zero overhead.

**Measuring recall *improvement* from HyDE** requires `@pytest.mark.live` with
real fastembed + real Claude API (set `ANTHROPIC_API_KEY` and use the live eval
lane). The default deterministic eval gate cannot measure semantic uplift because
the SHA-256 hash embedder is agnostic to LLM-generated hypothesis text.

### HyDE latency benchmark (`[search_hyde_false]`)

`tests/test_search_filtered_benchmark.py::test_hyde_false_search_p95_under_threshold`
(`@pytest.mark.benchmark`) measures the end-to-end latency of a `hyde=False`
request (`resolve_hyde_vector` + `hybrid_search`) and asserts p95 ≤
`[search_hyde_false].p95_ms` from `thresholds.toml` (currently **5 ms** — same
ceiling as `[search_filtered].p95_ms_glob_filtered`).  This guards against the
HyDE fast-path accidentally adding measurable overhead over an unfiltered search.

## RAG Fusion eval scenarios (C5)

`tests/eval/test_eval_suite.py` includes three C5-specific eval scenarios:

- **`test_eval_rag_fusion_regression_scenario`** (`@pytest.mark.eval`) — verifies that
  the RAG Fusion path (mocked `RAGFusionGenerator.generate_variants()` returning
  deterministic variants) does not break recall on the committed corpus.  Uses a
  mocked generator returning `query + "_variant1"` and `query + "_variant2"`.
  This scenario only guards against *breakage* of the RAG Fusion plumbing path —
  it does **not** measure recall *improvement*.

- **`test_bench_search_rag_fusion_disabled_latency`** (`@pytest.mark.benchmark`) —
  asserts that `rag_fusion=False` adds no measurable overhead over unfiltered
  hybrid search.  Threshold: `[search_rag_fusion_disabled].p95_ms` in
  `thresholds.toml` (**5 ms** — same ceiling as `[search_hyde_false].p95_ms`).

- **`test_bench_search_rag_fusion_enabled_latency`** (`@pytest.mark.benchmark`) —
  asserts that the mocked RAG Fusion path (no real LLM, no real embedding model)
  completes within ≤3× the disabled-path ceiling.  Threshold:
  `[search_rag_fusion_enabled].p95_ms` in `thresholds.toml` (**15 ms**).
  This is a regression guard against severe pipeline overhead — not a production SLA.

**Measuring recall *improvement* from RAG Fusion** requires `@pytest.mark.live_eval`
with real fastembed + real Claude API (set `ANTHROPIC_API_KEY` and use the live eval
lane in `tests/eval/live/test_live_rag_fusion.py`).  The default deterministic eval
gate cannot measure semantic uplift because the SHA-256 hash embedder is agnostic to
LLM-generated variant text.

## Graph eval gates (E2e)

The E2e feature introduces real graph eval gates with frozen public datasets and deterministic community detection (Leiden algorithm with fixed seed). This replaces three fake stub-based floors (`graph_local_mrr = 1.0`, `graph_global_mrr = 1.0`) that could not detect regressions.

### Fixture datasets

Four new evaluation datasets committed to `tests/eval/corpus/`:

- **MuSiQue-Ans** (`corpus/multihop-musique/`) — ~100 two-hop questions, supporting paragraphs, CC BY 4.0
- **2WikiMultiHopQA** (`corpus/multihop-2wiki/`) — ~100 bridge + comparison multi-hop questions, supporting paragraphs, Apache-2.0
- **HotpotQA** (`corpus/hotpotqa/`) — ~100 distractor (negative control) questions, supporting paragraphs, CC BY 4.0

Total corpus size: ~6,000 documents. Attribution required in `tests/eval/corpus/LICENSE-DATASETS`.

### Fixture layout

- `documents.jsonl` — entries with `"collection": "multihop-musique" | "multihop-2wiki" | "hotpotqa"` (separate from `"graph"` collection)
- `queries.jsonl` — entries with `"graph_mode": "naive" | "local" | "global"` and matching collection name
- `labels.jsonl` — positive relevance grades for every multi-hop query (corpus-specific coverage)
- `corpus/{dataset}/` — supporting paragraphs for each dataset (no LFS required)

### Graph eval metrics and gates

Four new recall metrics in `EvalMetrics`, all gated in `thresholds.toml`:

- **`graph_naive_recall_at_5`** — Recall@5 on MuSiQue naive-mode queries (graph entity expansion without communities)
- **`graph_local_recall_at_5`** — Recall@5 on 2WikiMultiHopQA local-mode queries (retrieval via pre-built communities)
- **`graph_global_recall_at_5`** — Recall@5 on 2WikiMultiHopQA global-mode queries (aggregation across top-N communities)
- **`graph_negative_control_recall_at_5`** — Recall@5 on HotpotQA naive-mode queries (regression guard: verifies simple queries do not regress with naive graph mode)

All four metrics are computed by `run_eval_suite` (when the graph extras are installed) and gated at CI.

### Community pre-build

`tests/eval/test_e2e_graph_eval_gate_v2.py` includes a module-scoped conftest fixture `build_communities_for_eval` that:

1. Ingests MuSiQue, 2WikiMultiHopQA, and HotpotQA corpora into a temporary eval LanceDB store
2. Runs `CommunityBuilder.build(collection, ns, seed=42)` for each multi-hop collection
3. Verifies ≥2 communities with ≥2 representative chunks each (non-trivial structure)

Deterministic Leiden seed (`42`) ensures byte-identical representative chunk lists across runs.

### Graph extras requirement

All graph eval tests require `archon-search[graph]` extras (`leidenalg`, `igraph`, `spacy`). Tests in `test_e2e_graph_eval_gate_v2.py` use `pytest.importorskip("leidenalg")` at module level; they skip gracefully when the extras are absent. The eval suite remains functional with or without the extras — graph metrics report as `None` when leidenalg is missing, no gate failure.

## Code-lane eval gate (BE-10)

BE-10 introduces two small, independent fixture corpora that each isolate one code-intelligence feature — AST-aware chunking, and def/ref (`calls`/`imports`/`defines`/`inherits`) graph edges — so a regression in either feature fails a dedicated gate, not just the general retrieval floors.

### Fixture datasets

Two new collections committed to `tests/eval/corpus/`:

- **`code-chunking`** (`corpus/code-chunking/`) — 6 Python files, chunk-boundary-sensitive. `order_pipeline.py`'s target function shares heavy vocabulary overlap with 5 distractor files; only chunking that keeps the function's docstring and code body in one chunk (the real AST chunker) reliably surfaces it.
- **`code-defref`** (`corpus/code-defref/`) — 7 Python files, connection-sensitive. `token_service.py` defines `validate_token`; `auth_gateway.py`, `audit_logger.py`, and `notification_service.py` call it (directly or via inheritance from `base_service.py`). Two distractor files (`rate_limiter.py`, `session_cache.py`) mention the word "token" without ever calling `validate_token`, so a co-occurrence-only graph would produce false positives that a directed `calls` edge does not.

Zero shared document IDs and zero shared query IDs between the two collections (`test_twoCorpora_areDisjoint`).

### Fixture layout

- `documents.jsonl` — entries with `"collection": "code-chunking" | "code-defref"`
- `queries.jsonl` — one query per collection (`q-code-chunking-001`, `q-code-defref-001`), both with `"graph_mode": "naive"`
- `labels.jsonl` — `code-defref`'s query has 3 gold docs at two grades: grade=2 for the lexically-weak target (`notification_service.py`, which only surfaces via the `calls` edge), grade=1 for the two lexically-trivial callers (both literally contain the string `"validate_token"`). Aggregate `recall_at_5` alone cannot isolate whether the grade-2 target was found — `test_code_lane_eval_gate.py` and the gated tests in `test_e2e_graph_eval_gate_v2.py` additionally assert its presence/absence directly.

### Real feature wiring on the gated path

Unlike the general eval collections (ingested through a stub/default-chunker pipeline), `code-chunking`/`code-defref` are ingested through a **separate real pipeline** when `run_eval_suite` is called with `lancedb_root` set (the gated CI path): a real `ASTChunker` at the calibrated `chunk_size=65` and a real `DefRefExtractor` + `GraphStore` + `RealGraphExpander`, sharing the same on-disk LanceDB directory as the rest of the suite. See `archon_search/eval/runner.py`'s `_build_code_lane_ingest_pipeline` and the BE-10 comment on `_build_pipeline_with_eval_backends` for why this collection-scoped routing is necessary (a single pipeline-wide `ASTChunker(chunk_size=65)` regressed the pre-existing `code` collection's retrieval quality — chunking is file-extension-gated, not collection-gated, in `pipeline.py`).

Without `lancedb_root` (report-only calibration runs, `regenerate.py`), both collections are ingested through the plain stub pipeline — the "no-feature" control measured in `baselines/baseline.json`.

### Code-lane eval metrics and gates

Two new recall metrics in `EvalMetrics`; only one is gated in `thresholds.toml` (Cycle 2 fix, C2-1/C2-7):

- **`code_chunking_recall_at_5`** — Recall@5 on the code-chunking collection's naive-mode query. **Report-only, no gated floor.** The gated-vs-no-feature comparison is structurally apples-to-oranges: the gated path uses `chunk_size=65` + real `ASTChunker`, the no-feature (default) path uses `chunk_size=256` + the stub chunker — a floor comparing the two cannot discriminate an AST-chunker regression regardless of corpus calibration, since both retrieve the target document (1.0 vs 1.0) even after recalibrating the committed corpus to be chunk-boundary-sensitive. The real AST-vs-fixed-window non-vacuity proof is `test_codeChunkingRecall_nonVacuous` in `tests/eval/test_code_lane_eval_gate.py`, which runs both arms through the identical `chunk_size=65` pipeline construction (only the chunker differs) and asserts a strict inequality.
- **`code_defref_recall_at_5`** — Recall@5 on the code-defref collection's naive-mode query. Floor (`1.0`) is set strictly above the measured no-feature baseline (`0.6667`, recorded in `baselines/baseline.json` — the DEFAULT, non-code-lane path, not the same gated path with the feature toggled off) — mirroring the `synonym_bridge_recall_at_5` non-vacuity pattern — so a regression that disables `DefRefExtractor` wiring reproduces the 0.6667 baseline and fails the gate. That floor>baseline comparison is a config-lint guard only; the primary non-vacuity proof is a targeted presence/absence assertion on the one gold doc (`code-defref-notification-service`) that can only be retrieved via the real `calls` edge — aggregate recall alone can pass (2/3) without ever finding it.

The `code_defref_recall_at_5` gate additionally asserts a non-zero edge count for `code-defref` before trusting the recall comparison (`test_eval_gate_code_defref_recall_at_5` in `test_e2e_graph_eval_gate_v2.py`) — a silently-failed graph extraction (post-persist hooks never propagate errors, per the project's error-handling invariant) would otherwise surface as a confusing recall mismatch instead of a clear "fixture no longer discriminates" failure. `test_defrefExtractorFailure_leavesZeroEdges_C16GuardWouldCatchIt` in `test_code_lane_eval_gate.py` proves this guard actually catches a real `DefRefExtractor.extract` failure (monkeypatched to raise), not just that it passes when extraction succeeds.

### Threshold-lowering notes

If `code_defref_recall_at_5`'s floor is ever lowered, it must stay strictly above the no-feature baseline (`0.6667`) or the gate becomes vacuous — update the `_NO_FEATURE_BASELINE` constant in both `test_eval_gate_code_defref_recall_at_5` (`test_e2e_graph_eval_gate_v2.py`) and the `thresholds.toml` comment together with the new baseline value.

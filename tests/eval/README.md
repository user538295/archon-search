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

## A2 filter constraints for fixture authors

- **`language` is reserved and rejected with HTTP 422** (roadmap item C2). Eval fixture queries must **not** use `language` as a filter value — doing so will always produce a validation error rather than a retrieval result, and the test will fail or produce meaningless metrics.
- When writing new `queries.jsonl` entries that exercise filtered search, use only `file_type`, `source_path_prefix`, `source_path_glob`, `indexed_after`, or `indexed_before` as filter fields.

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

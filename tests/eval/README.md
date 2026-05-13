# archon-search eval harness

This directory contains the FEAT-039 search evaluation harness: fixture
corpus, baselines, thresholds, and pytest-driven eval tests. It exists
to detect retrieval / routing / latency regressions on every change.

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
   cd packages/archon-search && uv run pytest --no-cov -m eval -k "report_only" tests/eval/test_eval_suite.py -v
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

## Local commands

Report-only calibration (no gating, prints measured metrics):

```
cd packages/archon-search && uv run pytest --no-cov -m eval -k "report_only" tests/eval/test_eval_suite.py -v
```

Gated eval (enforces `thresholds.toml`):

```
cd packages/archon-search && uv run pytest --no-cov -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py -v
```

Default unmarked eval units (contract / fixture / metric tests, fast):

```
cd packages/archon-search && uv run pytest --no-cov tests/eval/ -q
```

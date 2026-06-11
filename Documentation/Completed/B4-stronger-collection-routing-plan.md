# B4 — Stronger Collection Routing

**Purpose**: Blend a second routing signal — the embedding of each collection's generated description — with the centroid baseline under an opt-in `"hybrid"` strategy, and fix the routing eval gate to be rank-sensitive so any ranking change is observable.
**Audience**: archon-search contributors implementing B4; eval-harness maintainers.
**Status**: Done

---

## Background

`MultiCollectionRouter.rank()` (`router.py`) ranks collections by a single signal: cosine similarity between the query embedding and one mean-pooled `centroid` per collection. ADR-04 records the failure mode: a genuinely relevant but heterogeneous collection (diffuse centroid) loses to a tight off-topic centroid. Each collection already has a Haiku-generated `description` (`description_generator.py`) but it is used only to render the decomposer prompt block — it never participates in ranking. No summary embedding exists.

Worse, the current routing eval gate (`routing_accuracy = 0.9259`) cannot detect a ranking change: `_run_router_for_query` sets `shortlist_size = len(collections)` so the shortlist always includes every collection, making the intersection check invariant to ranking order. B4's load-bearing first commit is the gate fix, not the blend.

## Goal

Ship a minimum end-to-end "stronger routing" slice: (1) a `description_embedding` artifact stored on `CollectionMeta` and persisted in the meta table; (2) an opt-in `routing_strategy = "hybrid"` blend of centroid-cosine and description-embedding-cosine in `MultiCollectionRouter.rank()`; (3) a rank-sensitive eval metric (P@1 / MRR over gold position, scoped to `metric_scope == "routing"` traces) that moves when ranking order changes; (4) expanded routing fixtures that make the metric discriminating; (5) `thresholds.toml` floors encoding Δ ≥ 0 for hybrid vs centroid baseline. Centroid-only stays the default and must produce identical ordering and identical confidence-gate decisions versus pre-B4.

> **Eval-validation caveat (see Known limitations)**: under the SHA-256 bag-of-words eval embedder, the deterministic harness can only gate against regression (Δ ≥ 0); it cannot validate that hybrid adds discriminative signal. Real value-add validation belongs to B6 (production-model eval lane). This plan ships infrastructure + a regression gate, not a proof that hybrid improves quality.

---

## Scope

### In Scope
- `CollectionMeta.description_embedding: list[float] | None = None` field.
- `_meta_schema()` gains a `description_embedding_json` utf8 column; `_row_to_meta` parses it with the same malformed-JSON warning pattern as `centroid_json`; `update_collection_meta` serializes it.
- Schema-evolution read tolerance: old meta tables without the column read back as `None`.
- Embedding the generated description at ingest (pipeline.py `ingest_files` block) and in `recompute_collection_meta`; always uses the same `Embedder`/`embedding_model` as chunk vectors.
- `_ROUTING_FIELDS` gains `description_embedding` so `fetch_metadata` deserializes it.
- `MultiCollectionRouter.__init__` gains `strategy: Literal["centroid", "hybrid"]` and `description_weight: float`; `rank()` computes a blended score with per-collection centroid-only fallback when `description_embedding` is absent or model-mismatched.
- Config: `routing_strategy: Literal["centroid", "hybrid"] = "centroid"` and `routing_description_weight: float = 0.3` in `SearchConfig`; parsed and bounds-validated (`[0.0, 1.0]`) in `config.py`; unknown strategy raises `ConfigError`. Mirrored in `archon-search.toml.example`.
- `_build_router` (`routes_route.py`) threads both new knobs from config.
- MCP: `list_collections` strips `description_embedding` (same as `centroid`); `get_collections_meta` strips it BY DEFAULT and gains an opt-in `include_description_embedding: bool = False` parameter; `get_collection_meta` (single-collection) exposes `description_embedding` by default. The production router (`router.fetch_metadata`) passes `include_description_embedding=True` ONLY when `self._strategy == "hybrid"` (centroid mode does not need the data).
- Eval harness: `QueryEvalTrace` gains `ranked_collections: list[str] | None`; `EvalMetrics` gains `routing_mrr_centroid`, `routing_mrr_hybrid`, `routing_precision_at_1_centroid`, `routing_precision_at_1_hybrid`; `_run_router_for_query` is parameterized by `strategy` + `description_weight`; the suite runs both strategies per routing query in one corpus ingest; rank-sensitive metrics aggregate over `metric_scope == "routing"` traces only; `routing_accuracy` (rank-insensitive coverage check) is retained.
- Expanded routing fixture: add a fourth collection (`"faq"`) plus 4 new routing-scope queries across all four coupled fixtures (`routing/collections.jsonl` ↔ `documents.jsonl` ↔ `queries.jsonl` ↔ `labels.jsonl`).
- `thresholds.toml` floors for `routing_mrr_centroid`, `routing_mrr_hybrid`, `routing_precision_at_1_centroid` (gate on MRR, report P@1); `routing_mrr_hybrid` floor = centroid baseline value (Δ ≥ 0 constraint).
- New ADR-07 extending ADR-04 (ADR-04's centroid-default decision remains in force; B4 only adds an opt-in alternative — ADR-04's status is NOT changed); `BREAKING.md` entry; `tests/eval/README.md` updates; `03_world_class_roadmap.md` B4 checkbox; `archon-search.toml.example`.

### Out of Scope
- Making hybrid the default (deferred to B6 after prod-model eval lane).
- Multi-centroid / clustered representations (deferred to B4-followup).
- Rich routing explainability on `/explain` or `RouteResponse` (A4 / B1).
- LLM-based routing as primary signal (ADR-04 rejected; not revisited here).
- Incremental artifact maintenance for description embedding (B5 / CON-4).
- `schema_version` on `CollectionMeta` (same open question as A1 — deferred).
- `description_generator.py` prompt or sampling changes.
- Activating `max_parallel_collections` (inert config, unrelated).

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task 7.1 — Final verification & documentation update].

---

## What does NOT change
- `RouteResponse` shape (`pre_context`, `pinned_names`, `routable_names`, `decomposer_invoked`) — unchanged.
- `routing_strategy = "centroid"` (default) produces identical ordering and identical confidence-gate decisions versus today's `rank()` (regression-pinned by unit test that asserts exact numeric scores against hand-computed expected cosine values for synthetic embeddings).
- `routing_accuracy` (rank-insensitive coverage check over all non-bypassed traces) — retained; not deleted.
- `select()`, `get_pre_context()`, tiering logic (Tiers 1–3), confidence gate, and unscored-fallback ordering in `router.py` — only the Tier-3 ranking score changes.
- `description_generator.py` — untouched; B4 only embeds whatever `generate_description` already produces.
- All existing retrieval-scope metric floors in `thresholds.toml` — must still hold.

---

## Known limitations / accepted trade-offs
- The deterministic eval embedder (SHA-256 bag-of-words) may produce Δ = 0 for hybrid vs centroid; offline movement is suggestive, not the merge bar. The merge bar is no regression (Δ ≥ 0).
- Description embedding is re-embedded on every ingest even when unchanged (the embedding call is cheap and avoids stale state from an intermediate model swap). This is intentional and accepted: skipping re-embedding when the description text is unchanged is a future optimisation (B5).
- **Eval cannot validate hybrid's value; it can only gate against regression.** Under the SHA-256 bag-of-words eval embedder, `description_embedding` is structurally a sparse subset of the centroid's token-hash space (the centroid is the element-wise mean of chunk vectors over the same hash space). The eval harness therefore CANNOT validate that hybrid adds discriminative signal — it can only gate against regression (Δ ≥ 0). Real value-add validation belongs to B6 (production-model eval lane). This plan ships the infrastructure and the regression gate, not a proof that hybrid improves quality.
- Pre-B4 collections have `description_embedding = None`; under `"hybrid"` they receive centroid-only fallback until recomputed. No auto-migration.
- A collection whose `embedding_model` field is `""` (legacy default) is UNSCORED under the existing centroid guard (`col.embedding_model == self._embedding_model` fails when `self._embedding_model` is non-empty), and B4 preserves this. It does NOT receive centroid-only fallback — it is appended to the unscored tail by name. The hybrid blend only applies to collections that already pass the existing usable-centroid guard.
- The confidence gate compares `max(blended_score)` to `routing_confidence_threshold`; the threshold's meaning shifts slightly under hybrid because it is a convex combination of two cosines. The default `0.30` is re-confirmed as part of Task 6.2 (inspect the baseline run results and verify no routing queries are spuriously bypassed under hybrid).
- `get_collections_meta` MCP payload may now include `description_embedding` per collection. Vector sizes: eval uses 128 floats; production fastembed is typically 384–768 floats per collection. At 768d (~9 KB JSON per collection) this is ~900 KB at 100 collections and ~9 MB at 1000 collections — large enough to exceed typical LLM agent context windows at scale. **Decision (B4)**: strip `description_embedding` from `get_collections_meta` BY DEFAULT (same convention as `list_collections`); add an opt-in `include_description_embedding: bool = False` parameter to opt back in. The production router opts in only under `strategy="hybrid"` (see Task 4.1 / Task 5.2). `get_collection_meta` (single-collection) returns it by default since the per-call payload is bounded. See Task 5.2 for the MCP wiring.
- B4 defers `schema_version` on `CollectionMeta` and relies on per-column "absent column = None" tolerance. This is sustainable for one or two added columns; if B5 / B6 also add columns, `schema_version` becomes a prerequisite rather than a deferral. Track in the tech-debt register (`Documentation/Architecture/530_technical_debt_refactoring_roadmap.md`).
- Under `"hybrid"`, `/explain` and any other consumer of `rank_with_scores()` returns blended scores `(1-w)*cos(q,centroid) + w*cos(q,desc_emb)` per collection, not the decomposed centroid and description components. Operators inspecting `/explain` under hybrid will see different score magnitudes than under centroid with no per-signal breakdown. Rich per-signal explainability (separate centroid_score / description_score on the explain payload) is deferred to A4 / B1 — see Out of Scope.

---

## Architecture

### New / modified modules

**`archon_search/collection_meta.py`**
- `CollectionMeta` gains `description_embedding: list[float] | None = None`.

**`archon_search/store.py`**
- `_meta_schema()` gains `pa.field("description_embedding_json", pa.utf8())`.
- `_row_to_meta` parses `description_embedding_json` with malformed-JSON warning (reuses `centroid_json` pattern); returns `None` when column absent or empty.
- `update_collection_meta` serializes `meta.description_embedding` as JSON string.
- `migrate_description_embedding()` — idempotent column-add migration (same pattern as `migrate_namespace`).

**`archon_search/pipeline.py`**
- `ingest_files`: after centroid computation, if description was (re)generated or already present, embed it via `await self._embedder.embed_one(description)` and store on the `CollectionMeta`.
- `recompute_collection_meta`: re-embeds `existing_meta.description` (if not `None`) via `await self._embedder.embed_one(description)` and stores on `CollectionMeta`.

**`archon_search/constants.py`**
- New constant `DEFAULT_ROUTING_DESCRIPTION_WEIGHT: Final[float] = 0.3`. **Defined in `constants.py` (not `router.py`)** so both `router.py` and `config.py` can import it without `config → router` import direction reversal. This is the single change point for the default weight.

**`archon_search/router.py`**
- `_ROUTING_FIELDS` adds `"description_embedding"`.
- `MultiCollectionRouter.__init__` gains `strategy: Literal["centroid", "hybrid"] = "centroid"` and `description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (imported from `archon_search.constants`).
- `fetch_metadata` is extended: when `self._strategy == "hybrid"`, the JSON-RPC payload becomes `"arguments": {"include_description_embedding": True}`; under `"centroid"` it remains `"arguments": {}` (the server strips the field by default and the centroid path does not need it).
- **`_score_collections` is NEW** — extracted as a private method from the existing per-collection cosine logic inside `rank()` / `rank_with_scores()`. Today both methods compute `cos(q, centroid)` inline; B4 lifts that into `_score_collections` so the centroid and hybrid scoring paths share a single dispatch site. Under `"centroid"`, `_score_collections` returns `cos(q, centroid)` per collection — semantically identical to today's inline logic. Under `"hybrid"`, per-collection score = `(1 - w) * cos(q, centroid) + w * cos(q, desc_emb)` when description embedding is present AND `col.embedding_model == self._embedding_model` AND `col.embedding_model != ""` (non-empty tag required to verify model compatibility); falls back to `cos(q, centroid)` otherwise (absent embedding, model-mismatch, or empty tag all trigger centroid-only).
- `rank()` and `rank_with_scores()` are refactored to delegate to `_score_collections` for the per-collection scoring step; all downstream logic (confidence gate, shortlist slice, unscored fallback) unchanged.

**`archon_search/config.py`**
- `SearchConfig` gains `routing_strategy: str = "centroid"` and `routing_description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (imported from `archon_search.constants`).
- `load_config`: parses both from `[routing]` section; validates `routing_strategy in {"centroid", "hybrid"}` (raises `ConfigError`); validates `0.0 <= routing_description_weight <= 1.0` (raises `ConfigError`).

**`archon_search/server/routes_route.py`**
- `_build_router` passes `strategy=config.routing_strategy` and `description_weight=config.routing_description_weight` to `MultiCollectionRouter`.

**`archon_search/server/mcp.py`**
- `list_collections`: also pops `description_embedding` key (alongside `centroid`). Currently `list_collections` strips `centroid` via `d.pop("centroid", None)` (see `mcp.py:629`); the new strip mirrors that exactly.
- `get_collections_meta`: pops `description_embedding` BY DEFAULT; gains `include_description_embedding: bool = False` parameter — when True, keeps the field. Docstring warns about payload size at scale. Note: today `get_collections_meta` does NOT strip `centroid` (it returns `asdict(r)` unmodified, see `mcp.py:640-641`); B4 introduces stripping for `description_embedding` only, and the production router opts back in only under hybrid.
- `get_collection_meta` (single-collection): no change — `description_embedding` is included as a new field by default (single-collection payload is bounded).

**`archon_search/eval/types.py`**
- `QueryEvalTrace` gains `ranked_collections: list[str] | None = None`.
- `EvalMetrics` gains `routing_mrr_centroid: float | None`, `routing_mrr_hybrid: float | None`, `routing_precision_at_1_centroid: float | None`, `routing_precision_at_1_hybrid: float | None`.

**`archon_search/eval/metrics.py`**
- `compute_routing_mrr(traces: list[QueryEvalTrace], gold_fn: Callable[[str], set[str]]) -> float | None` — aggregates over `metric_scope == "routing"` traces only; returns `None` when no such traces exist.
- `compute_routing_precision_at_1(traces: list[QueryEvalTrace], gold_fn: Callable[[str], set[str]]) -> float | None` — same scope.

**`archon_search/eval/runner.py`**
- `_run_router_for_query` gains `strategy: str = "centroid"` and `description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT` params; passes them to `MultiCollectionRouter`; returns `list[str]` (full ranked order — all collections are scored because eval fixtures always populate centroids before routing runs; see Task 1.3 fixture precondition assertion).
- `run_eval_suite` maintains **two separate trace lists** alongside the main `traces` list: `centroid_routing_traces` and `hybrid_routing_traces`. For each routing-scope query, one `QueryEvalTrace` is created for the centroid run (added to `centroid_routing_traces`) and one for the hybrid run (added to `hybrid_routing_traces`). The main `traces` list retains only centroid traces for backward-compatible `routing_accuracy` computation. `compute_routing_mrr` / `compute_routing_precision_at_1` are called against each list independently. **Existing fetch site is already post-recompute** — the current `collection_metas = await pipeline.get_all_collections_meta()` at the top of `run_eval_suite` (currently `runner.py:566`) runs **after** `_ingest_corpus`, which calls `recompute_collection_meta` per collection (currently `runner.py:484`). Once Task 3.2 lands, this single fetch already returns metas with `description_embedding` populated; no second fetch is needed. Add an inline comment at the fetch site documenting that `description_embedding` is now load-bearing and the fetch must remain post-recompute.
- `EvalQualityFloors` (the actual class name in `runner.py`, held by `EvalThresholds.quality_floors`) gains `routing_mrr_centroid: float | None = None`, `routing_mrr_hybrid: float | None = None`, `routing_precision_at_1_centroid: float | None = None`, `routing_precision_at_1_hybrid: float | None = None` (all defaulting to `None`). `load_thresholds` (also in `runner.py`) parses all four from `[quality_floors]`.
- `_QUALITY_FLOOR_FIELDS` tuple gains the four new keys.
- `assert_thresholds` and `render_report` handle the new fields.

**`tests/eval/routing/collections.jsonl`** — adds `{"name": "faq", "description": "..."}`.
**`tests/eval/corpus/faq/`** — new corpus directory with ≥5 documents covering a distinct topic domain.
**`tests/eval/queries.jsonl`** — adds 4 new routing-scope queries (`q-route-03` … `q-route-06`).
**`tests/eval/labels.jsonl`** — adds positive labels for new queries.
**`tests/eval/documents.jsonl`** — adds entries for faq corpus documents.

### Config keys

| Key | Type | Default | Validation |
|---|---|---|---|
| `routing_strategy` | `str` | `"centroid"` | Must be `"centroid"` or `"hybrid"` |
| `routing_description_weight` | `float` | `DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (= `0.3`) | Must be in `[0.0, 1.0]` |

### Method signatures (key)

```python
# router.py
class MultiCollectionRouter:
    def __init__(
        self,
        search_url: str,
        embedder: "Embedder",
        shortlist_size: int,
        confidence_threshold: float,
        embedding_model: str,
        initial_metadata: list[CollectionMeta] | None = None,
        strategy: Literal["centroid", "hybrid"] = "centroid",
        description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT,
    ) -> None: ...

    def _score_collections(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]: ...

    def rank_with_scores(
        self,
        query_embedding: list[float],
        collections: list[CollectionMeta],
    ) -> list[tuple[CollectionMeta, float | None]]: ...
    # rank_with_scores delegates to _score_collections; blended scores flow to /explain

# metrics.py
def compute_routing_mrr(
    traces: list[QueryEvalTrace],
    gold_fn: Callable[[str], set[str]],
) -> float | None: ...

def compute_routing_precision_at_1(
    traces: list[QueryEvalTrace],
    gold_fn: Callable[[str], set[str]],
) -> float | None: ...

# runner.py
async def _run_router_for_query(
    pipeline,
    query_text: str,
    collection_metas: list[CollectionMeta],
    *,
    strategy: str = "centroid",
    description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT,
) -> list[str]: ...  # full ranked collection names
```

---

## Task breakdown

> **Phase ordering note**: Phase 1 (eval gate) is load-bearing and must land first. After Phase 1 completes, Phases 2.1 (CollectionMeta field), 3 (producer), and 4 (router) can be parallelized — Task 4.x depends only on 2.1 (the field) and not on Phase 3 (the producer that fills it), because router behavior with `description_embedding=None` is itself a tested case. Phase 5 depends on Phase 4 (router knobs). Phase 6 depends on Phases 3 + 4. Listing phases in numeric order is for narrative clarity, not a hard sequence.

### Phase 1 — Rank-sensitive eval gate (build the gate before the feature)

> **Releasable**: after Task 1.4, the eval harness measures rank-sensitive routing quality under the existing centroid strategy. No production behavior changes.

#### Task 1.1 — Extend `QueryEvalTrace` with `ranked_collections`
- [x] **File**: `archon_search/eval/types.py`
- **Depends on**: nothing
- **Description**:
  - Add `ranked_collections: list[str] | None = None` field to `QueryEvalTrace` dataclass.
  - Semantics mirror `router_correct`: `None` when routing is disabled or query bypasses routing; a list (possibly empty) when routing ran. The list contains full ranked collection names in score-descending order from the **scored** portion of `router.select()`. Because eval fixtures always populate centroids before routing runs (enforced by Task 1.3's precondition test), the unscored-fallback tail of `router.select()` is empty and the list is purely score-ordered.
  - **Design decision on centroid vs hybrid traces**: the runner uses **two separate lists** (`centroid_routing_traces` and `hybrid_routing_traces`) rather than a strategy tag on the trace. Each list holds `QueryEvalTrace` objects with `ranked_collections` populated for their respective strategy. This avoids ambiguity in `compute_routing_mrr` calls and keeps the trace schema minimal. `QueryEvalTrace` does NOT gain a `routing_strategy` field.
  - No change to `EvalMetrics` in this task (metrics land in Task 1.2).
- **Releasable**: `QueryEvalTrace` can carry a ranked collection list; downstream consumers can read it.
- **Tests (TDD)** — `tests/eval/test_types.py`:
  - Unit: `test_query_eval_trace_ranked_collections_default_none` — default-constructed `QueryEvalTrace` has `ranked_collections is None`.
  - Unit: `test_query_eval_trace_ranked_collections_list` — field accepts and stores a `list[str]`.
  - Checkpoint: `uv run pytest tests/eval/test_types.py -x`

#### Task 1.2 — Add rank-sensitive routing metrics to `EvalMetrics` and `metrics.py`
- [x] **File**: `archon_search/eval/types.py`, `archon_search/eval/metrics.py`
- **Depends on**: Task 1.1
- **Description**:
  - `EvalMetrics` gains four new optional fields: `routing_mrr_centroid: float | None = None`, `routing_mrr_hybrid: float | None = None`, `routing_precision_at_1_centroid: float | None = None`, `routing_precision_at_1_hybrid: float | None = None`.
  - `compute_routing_mrr(traces: list[QueryEvalTrace], gold_fn: Callable[[str], set[str]]) -> float | None`:
    - Filters to `metric_scope == "routing"` traces with non-None `ranked_collections`.
    - For each such trace: find the first position (1-based) where `ranked_collections[i]` is in `gold_fn(trace.query_id)`; reciprocal rank = `1 / position`; if gold not found, reciprocal rank = 0.
    - Returns mean reciprocal rank, or `None` when no eligible traces exist.
  - `compute_routing_precision_at_1(traces: list[QueryEvalTrace], gold_fn: Callable[[str], set[str]]) -> float | None`:
    - Same scope filter. P@1 = fraction of traces where `ranked_collections[0]` is in the gold set.
    - Returns `None` when no eligible traces or all have empty `ranked_collections`.
  - Both functions have the same `None`-skip convention: a trace with `ranked_collections = None` is excluded.
- **Releasable**: rank-sensitive routing metrics are computable from any trace list with `ranked_collections` populated.
- **Tests (TDD)** — `tests/eval/test_metrics.py`:
  - Unit: `test_routing_mrr_top1_gold` — gold is rank 1 → MRR = 1.0.
  - Unit: `test_routing_mrr_top2_gold` — gold is rank 2 → MRR = 0.5.
  - Unit: `test_routing_mrr_gold_not_found` — gold not in ranked list → MRR = 0.0.
  - Unit: `test_routing_mrr_none_when_no_routing_traces` — no routing-scope traces → returns None.
  - Unit: `test_routing_mrr_none_when_ranked_collections_is_none` — routing traces with `ranked_collections=None` are excluded → returns None when all are None.
  - Unit: `test_routing_mrr_changes_when_order_changes` — feed two synthetic orderings (gold at rank 1 vs rank 2), assert MRR values differ (this pins the property `routing_accuracy` lacks).
  - Unit: `test_routing_p1_gold_at_top` → 1.0; `test_routing_p1_gold_not_top` → 0.0; `test_routing_p1_none_when_empty_traces`.
  - Checkpoint: `uv run pytest tests/eval/test_metrics.py -x -k "routing_mrr or routing_p"`

#### Task 1.3 — Expand routing fixtures (four-fixture coordinated edit)
- [x] **File**: `tests/eval/routing/collections.jsonl`, `tests/eval/corpus/faq/` (new directory + ≥5 files), `tests/eval/documents.jsonl`, `tests/eval/queries.jsonl`, `tests/eval/labels.jsonl`
- **Depends on**: nothing (fixture-only change)
- **Description**:
  - Add a fourth routing collection `"faq"` with a description clearly distinct from `"code"`, `"docs"`, and `"mixed"` (e.g. `"Frequently asked questions: troubleshooting answers, how-to guides, and common error resolutions in plain language"`).
  - Create `tests/eval/corpus/faq/` with ≥5 plain-language FAQ documents (`.md`) covering a topic domain that does NOT overlap strongly with the existing three collections' centroids. Content must be distinct enough that `"code"`/`"docs"` centroids score lower on FAQ queries under the deterministic SHA-256 embedder. Use terms that appear only in the FAQ corpus files.
  - Add entries to `documents.jsonl` for each faq corpus file (schema: `{"doc_id": "faq-doc-NN", "collection": "faq", "source_path": "corpus/faq/..."}`).
  - Add 4 routing-scope queries (`q-route-03` … `q-route-06`) to `queries.jsonl` (schema: `{"query_id": "q-route-NN", "text": "...", "collection": null, "metric_scope": "routing"}`). Each query should have a clear gold collection — ideally 2 targeting `"faq"`, 1 targeting `"code"`, 1 targeting `"docs"` — so that no strategy is trivially top-1 on every query.
  - Add positive labels to `labels.jsonl` for each new routing query: at least one document from the gold collection per query (schema: `{"query_id": "q-route-NN", "doc_id": "faq-doc-NN", "relevance": 1}`).
  - Verify: existing retrieval-scope queries in `documents.jsonl`/`labels.jsonl` are not disturbed; the loader's no-orphan-corpus-file / no-unreachable-positive constraints must still hold.
  - Document the new fixture scenario in `tests/eval/README.md` (fixture schema section).
  - **Fixture precondition requirement**: every routing collection fixture must have a centroid (i.e. at least one document ingestible by the eval harness). The eval harness calls `recompute_collection_meta` on each collection before routing runs; if any collection lacks documents, its centroid will be `None` and the ranked list will contain an unscored-fallback entry in arbitrary order, silently corrupting MRR. The Task 1.3 tests must verify this property.
- **Releasable**: fixture is self-consistent and passes the corpus contract tests.
- **Tests (TDD)** — `tests/eval/test_corpus_contract.py`, `tests/eval/test_fixtures.py`:
  - The existing corpus contract tests (`test_corpus_contract.py`) must pass with the new fixtures — they enforce no-orphan and no-unreachable constraints.
  - Unit: `test_fixture_faq_collection_in_routing_collections` — `routing/collections.jsonl` contains `"faq"`.
  - Unit: `test_fixture_faq_documents_present` — `documents.jsonl` has entries with `collection == "faq"`.
  - Unit: `test_fixture_routing_queries_expanded` — `queries.jsonl` has ≥ 6 routing-scope entries.
  - Unit: `test_fixture_all_routing_collections_have_scorable_centroids` — for every collection in `routing/collections.jsonl`, run the deterministic eval embedder (`archon_search/eval/backends.py`) over the collection's corpus documents in `tests/eval/corpus/<collection>/` and compute a centroid; assert all centroids are non-None and non-zero. This is the true precondition that ensures no unscored-fallback entries contaminate the ranked list — having documents in `documents.jsonl` is necessary but not sufficient (a document that produces zero chunks would still leave the centroid None).
  - Unit: `test_fixture_faq_vocab_distinct_from_other_collections` — using the SHA-256 deterministic embedder from `archon_search/eval/backends.py`, embed the FAQ routing queries and compute centroid similarity against each collection's centroid (derived from the fixture corpus). Assert that the FAQ queries have higher similarity to the `"faq"` centroid than to the `"code"` or `"docs"` centroids. **Additionally**: embed the `"faq"` collection's description string and assert it has higher cosine similarity to the FAQ corpus centroid than to the `"code"` / `"docs"` corpus centroids (this catches the case where corpus centroids are distinct but descriptions are not, which would zero out hybrid's contribution to ranking). Note: this test uses the SHA-256 bag-of-words eval embedder and is a **necessary-but-not-sufficient** smoke test for fixture distinctness; production model validation belongs to B6.
  - Checkpoint: `uv run pytest tests/eval/test_corpus_contract.py tests/eval/test_fixtures.py -x`

#### Task 1.4 — Wire `ranked_collections` into runner and compute centroid baseline metric
- [x] **File**: `archon_search/eval/runner.py`, `archon_search/eval/metrics.py`
- **Depends on**: Task 1.1, Task 1.2, Task 1.3
- **Description**:
  - `_run_router_for_query` gains params `strategy: str = "centroid"` and `description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (both keyword-only). **Phase-ordering note**: in Task 1.4 (Phase 1) these params are accepted on `_run_router_for_query` but NOT yet passed to the `MultiCollectionRouter` constructor — Phase 1 must complete before Phase 4, and `MultiCollectionRouter.__init__` does not gain the params until Task 4.2. Only the centroid path (default values) is exercised here. The constructor passthrough is wired in Task 6.1 (which depends on Task 4.2). The signature is added in Phase 1 to keep the call sites stable as the feature lands.
  - Return type stays `list[str]` (full ranked order — since `shortlist_size = len(collections)` and `confidence_threshold = 0.0`, and all fixture collections have centroids, this is the complete score-ordered list).
  - In `run_eval_suite`, for routing-scope queries: call `_run_router_for_query(..., strategy="centroid")` and record the result as `ranked_collections` on the `QueryEvalTrace` added to `centroid_routing_traces`. Update `router_correct` to derive from `ranked_collections` (same set-intersection logic). For retrieval-scope queries: keep existing `_run_router_for_query` call (centroid) with `ranked_collections=None` (retrieval traces don't need ranked order for this metric).
  - Compute `routing_mrr_centroid` and `routing_precision_at_1_centroid` using `compute_routing_mrr` / `compute_routing_precision_at_1` over `centroid_routing_traces`; store in `EvalMetrics`.
  - `routing_mrr_hybrid` and `routing_precision_at_1_hybrid` default to `None` in this phase (hybrid runner not yet wired).
  - Extend `_QUALITY_FLOOR_FIELDS` to include `routing_mrr_centroid` (the gating field for this phase). **Interim gap (intentional)**: `routing_mrr_hybrid`, `routing_precision_at_1_centroid`, and `routing_precision_at_1_hybrid` are added to `EvalQualityFloors` (below) for forward compatibility but are NOT in `_QUALITY_FLOOR_FIELDS` until Task 6.1; floors set for them in `thresholds.toml` between Task 1.4 and Task 6.1 are silently ignored. This is an intentional interim state.
  - **`EvalQualityFloors`** (the actual dataclass in `runner.py`, held by `EvalThresholds.quality_floors`) gains `routing_mrr_centroid: float | None = None` and the three other new keys defaulting to `None`. `load_thresholds` (in `runner.py`) is updated to parse all four from `[quality_floors]` using the same optional-float pattern as `routing_accuracy`.
  - `assert_thresholds` handles the new keys with the standard `None` metric AND `None` floor → skip convention.
  - `render_report` appends routing MRR centroid line (and P@1 as secondary) after `routing_accuracy`.
  - Update the `"routing_accuracy"` comment in `_run_router_for_query` docstring to note that the divergence (`shortlist_size = len(collections)`) no longer hides ranking quality since the metric reads full ranked order.
  - **Trace-provenance comment**: the runner must include an inline code comment near the trace-list construction stating that `routing_accuracy` is computed from `traces` (centroid-only routing traces in v1, retained for backward compatibility), while `routing_mrr_centroid` / P@1 centroid is computed from `centroid_routing_traces` and `routing_mrr_hybrid` / P@1 hybrid from `hybrid_routing_traces`. Future maintainers should not assume the same trace set feeds all routing metrics.
  - **Do NOT regenerate the baseline yet** — that happens in Task 1.5.
- **Releasable**: the harness computes `routing_mrr_centroid` on the expanded fixture; `thresholds.toml` can gate on it.
- **Tests (TDD)** — `tests/eval/test_runner.py`, `tests/eval/test_metrics.py`:
  - Unit: `test_run_router_for_query_accepts_strategy_param` — calling `_run_router_for_query(..., strategy="centroid")` does not raise; result is a list of strings.
  - Unit: `test_run_router_for_query_returns_full_ranked_order` — with 3 collections and `shortlist_size=3`, `confidence_threshold=0.0`, returns all 3 names in score order.
  - Unit: `test_routing_mrr_centroid_in_eval_metrics` — `EvalMetrics` has `routing_mrr_centroid` field accessible.
  - Unit: `test_load_thresholds_parses_routing_mrr_centroid` — TOML with `[quality_floors] routing_mrr_centroid = 0.75` → `thresholds.quality_floors.routing_mrr_centroid == 0.75`; absent key → `None`.
  - Integration: a full `run_eval_suite` over the expanded routing fixture computes a non-None `routing_mrr_centroid` — value should be > 0.
  - Checkpoint: `uv run pytest tests/eval/test_runner.py tests/eval/test_metrics.py -x --no-cov`

#### Task 1.5 — Regenerate baseline for centroid routing metric + add `thresholds.toml` floor
- [x] **File**: `tests/eval/baselines/baseline.json`, `tests/eval/baselines/baseline.md`, `tests/eval/thresholds.toml`
- **Depends on**: Task 1.4
- **Description**:
  - Run the calibration command from `baselines/regenerate.py` (or the command documented in `baseline.json`) to produce a fresh baseline that includes the new `routing_mrr_centroid` and `routing_precision_at_1_centroid` keys in the flat metric dict. Also regenerates `eval_hash`, `runtime_config_hash`, `thresholds_hash` to match the expanded fixture and updated runner.
  - Commit the regenerated `baseline.json` and `baseline.md`.
  - Set `thresholds.toml` `[quality_floors]` floor for `routing_mrr_centroid` equal to the measured baseline value (strict floor per policy — any regression fails the gate or requires a documented waiver). Add `routing_precision_at_1_centroid` as a reported-only entry (no floor yet — resolved in Task 6.2 once hybrid is measured and P@1 gating decision is made).
  - All existing `[quality_floors]` values must still hold (no retrieval regression from fixture expansion).
  - **Do not add `routing_mrr_hybrid` floor here** — that lands in Task 6.2.
  - Verify: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` passes.
- **Releasable**: the eval gate is now rank-sensitive for centroid routing; the gate is live and can detect a routing regression.
- **Tests (TDD)** — `tests/eval/test_eval_baseline_unchanged.py`, `tests/eval/test_baseline_contract.py`:
  - The existing baseline-contract tests must pass with the updated baseline (including the hash checks in `test_eval_baseline_unchanged.py` — `eval_hash`, `runtime_config_hash`, and `thresholds_hash` will all change and must be regenerated correctly).
  - Checkpoint: `uv run pytest tests/eval/test_baseline_contract.py tests/eval/test_eval_baseline_unchanged.py -x`

---

### Phase 2 — Persistence: `description_embedding` field + column

> **Releasable**: after Task 2.3, `description_embedding` round-trips through write→read and old meta tables read back as `None`. No ranking change yet.

#### Task 2.1 — Add `description_embedding` field to `CollectionMeta`
- [x] **File**: `archon_search/collection_meta.py`
- **Depends on**: nothing
- **Description**:
  - Add `description_embedding: list[float] | None = None` as a dataclass field (with default `None`, so all existing `CollectionMeta(...)` call sites continue to work unchanged).
  - No validation here — the field is an opaque float list; the router's same-model guard is the semantic validator.
- **Releasable**: `CollectionMeta` can hold a description embedding; callers may pass it or leave it `None`.
- **Tests (TDD)** — `tests/test_collection_meta.py` (create if it does not exist):
  - Unit: `test_description_embedding_default_none` — `CollectionMeta(name="x")` has `description_embedding is None`.
  - Unit: `test_description_embedding_stored` — `CollectionMeta(name="x", description_embedding=[0.1, 0.2])` stores the list.
  - Checkpoint: `uv run pytest tests/test_collection_meta.py -x`

#### Task 2.2 — Add `description_embedding_json` column to `_meta_schema` and persistence layer
- [x] **File**: `archon_search/store.py`
- **Depends on**: Task 2.1
- **Description**:
  - `_meta_schema()`: add `pa.field("description_embedding_json", pa.utf8())` after `centroid_json`. The field stores a JSON-encoded `list[float]` or an empty string when `None`.
  - `_row_to_meta`: parse `description_embedding_json` using `json.loads` when non-empty; catch `json.JSONDecodeError` and log at WARNING with the collection name (same pattern as `centroid_json` — see `_row_to_meta` in `store.py`); set to `None` on error or when the key is absent from `row` (schema-evolution tolerance: use `row.get("description_embedding_json", "")` to avoid `KeyError` on old tables). Additionally validate that the parsed JSON is a `list` whose elements are all finite floats (use `isinstance(x, (int, float))` and `math.isfinite(x)`); if any element is non-numeric, NaN, or Inf, treat as malformed (log WARNING with collection name, set to `None`).
  - `update_collection_meta`: serialize `meta.description_embedding` as `json.dumps(meta.description_embedding) if meta.description_embedding is not None else ""`.
  - `migrate_description_embedding()`: idempotent column-add migration (same pattern as `migrate_namespace` — see `migrate_namespace` in `store.py`); adds `description_embedding_json` varchar column with default `""` if absent; called at startup alongside other migrations.
- **Releasable**: `description_embedding` round-trips through `update_collection_meta` → `_row_to_meta`; old tables read back as `None`.
- **Tests (TDD)** — `tests/test_store.py` (unit section) and `tests/test_store_integration.py` (integration):
  - Unit: `test_row_to_meta_with_description_embedding` — row dict with valid JSON list yields correct `description_embedding`.
  - Unit: `test_row_to_meta_missing_key_yields_none` — row dict without `description_embedding_json` key → `None` (no `KeyError`).
  - Unit: `test_row_to_meta_malformed_json_yields_none_with_warning` — invalid JSON string → `None` + warning logged.
  - Unit: `test_row_to_meta_empty_string_yields_none` — empty string → `None`.
  - Unit: `test_row_to_meta_non_float_elements_yield_none_with_warning` — parameterized over malformed-element cases. Each case yields `None` and logs a WARNING with the collection name:
    - `"[0.1, \"x\", 0.3]"` — string element
    - `"[0.1, null, 0.3]"` — JSON null (Python `None`)
    - `"[0.1, true, 0.3]"` — JSON `true` (Python `True`). Note: Python's `isinstance(True, int)` is `True`, so the validation must explicitly reject `bool` (use `type(x) in (int, float)` or check `not isinstance(x, bool)` first). Spec the validator to reject booleans; do not silently accept `True == 1.0`.
    - `"[NaN, 0.2, 0.3]"` / `"[Infinity, 0.2, 0.3]"` — non-finite values caught by `math.isfinite`.
  - Integration (`@pytest.mark.integration`): `test_description_embedding_round_trips` — write a `CollectionMeta` with `description_embedding=[0.5, -0.3]`, read back, assert values match (within float tolerance).
  - Integration: `test_old_table_without_column_reads_none` — create a meta table using the schema WITHOUT `description_embedding_json` column, call `get_collection_meta`, assert `description_embedding is None` (pins schema-evolution tolerance). Note: this test carries `@pytest.mark.integration` and is excluded from the default `uv run pytest` run; the `row.get(...)` fallback logic that it pins is also covered by the unit test `test_row_to_meta_missing_key_yields_none` (which runs in the default suite).
  - Checkpoint: `uv run pytest tests/test_store.py -x -k "description_embedding"`

#### Task 2.3 — Wire `migrate_description_embedding` into startup
- [x] **File**: `archon_search/store.py`, `archon_search/server/app.py` (or wherever migrations are invoked at startup)
- **Depends on**: Task 2.2
- **Description**:
  - Locate the startup migration sequence (where `migrate_namespace` and `migrate_acl` are called) and add `await store.migrate_description_embedding()` in the same sequence.
  - `migrate_description_embedding` must be idempotent: calling it twice in succession must not raise.
  - Log at INFO on successful column add; log at WARNING on concurrent-add race (same pattern as `migrate_namespace`).
  - **Ordering precondition (mandatory)**: `migrate_description_embedding` MUST run before any code path that writes `description_embedding_json` via `update_collection_meta`. Verify the startup sequence in `archon_search/server/app.py` (or wherever migrations are invoked) calls all `migrate_*` functions **before** the FastAPI app accepts requests on any ingest endpoint. Document the chosen ordering in a code comment at the migration call site (e.g. "All `migrate_*` calls complete before the lifespan context yields control to the request loop"). If the existing migration sequence already satisfies this (it should — `migrate_namespace` has the same constraint), inherit the pattern; if not, fix it as part of this task.
- **Releasable**: existing deployments auto-migrate the meta table column on next startup; `description_embedding` reads as `None` until recomputed.
- **Tests (TDD)** — `tests/test_store.py`:
  - Unit: `test_migrate_description_embedding_idempotent` — call `migrate_description_embedding()` twice on the same table; no exception raised.
  - Unit: `test_migrate_description_embedding_noop_when_column_present` — table already has column; call returns without error and does not log WARNING.
  - Unit: `test_migrate_description_embedding_concurrent_calls` — invoke `migrate_description_embedding` from two `asyncio.gather` tasks on the same table; assert neither raises (idempotency under concurrency). If the existing `migrate_namespace` does not have an equivalent test, document the omission inline as acceptable — `migrate_description_embedding` inherits the same concurrency behavior as `migrate_namespace`, so we are not raising the bar above the precedent.
  - Checkpoint: `uv run pytest tests/test_store.py -x -k "migrate_description_embedding"`

---

### Phase 3 — Producer: embed description at ingest and recompute

> **Releasable**: after Task 3.2, ingesting a collection that produces a description persists a `description_embedding` of the same dimensionality as chunk vectors. No ranking change yet.

#### Task 3.1 — Embed description at ingest in `pipeline.py`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 2.1, Task 2.2
- **Description**:
  - In `ingest_files`, after the existing description generation block (line ~312–330 neighborhood), add:
    - If `description` is not `None` (whether freshly generated or preserved from `existing_meta`), embed it via `await self._embedder.embed_one(description)` and store on the `CollectionMeta` being built as `description_embedding=<result>`.
    - If `description` is `None`, set `description_embedding=None` and log at DEBUG `"description_embedding: description is None for collection %r — skipping"`.
  - The embedding uses the same `Embedder` instance as chunk vectors, so `embedding_model` tag is automatically consistent.
  - The `CollectionMeta` constructor call at line ~319 gains `description_embedding=description_embedding`.
  - **Verification step (mandatory before merging this task)**: grep all call sites of `update_collection_meta` and `pipeline.ingest_files` / `pipeline.ingest_directory`. Confirm the `archon_search/jobs/` subsystem invokes ingest only through `pipeline.ingest_files` / `pipeline.ingest_directory` / `recompute_collection_meta` (which are covered by this task and Task 3.2). If any code path in `jobs/` constructs a `CollectionMeta` directly and calls `update_collection_meta` without going through the description-embedding step, surface that path as a **separate sub-task** (Task 3.1.1) to thread the description-embedding logic into it; do not let it ship as a regression hole.
- **Releasable**: after ingest, `get_collection_meta` returns a `CollectionMeta` with a populated `description_embedding` when a description exists.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_ingest_populates_description_embedding` — stub `generate_description` to return `"test desc"`, stub `Embedder.embed_one` as an `AsyncMock` returning `[0.1] * 32` (must be async since `embed_one` is `async def`); after `ingest_files`, assert `store.update_collection_meta` was called with a `CollectionMeta` having `description_embedding == [0.1] * 32`.
  - Unit: `test_ingest_description_none_sets_embedding_none` — stub `generate_description` to return `None`; assert `description_embedding is None` on the persisted meta.
  - Unit: `test_ingest_re_embeds_description_on_every_ingest` — existing meta has `description = "old desc"` and `description_embedding = [0.5] * 32` (the "prior" persisted value); `described_at_doc_count` does not trigger regeneration. Stub `Embedder.embed_one` (AsyncMock) to return `[0.9] * 32` — a vector **distinct** from the prior. Assert (a) `embed_one` is called with `"old desc"`, and (b) the persisted `CollectionMeta` passed to `update_collection_meta` has `description_embedding == [0.9] * 32` (the NEW vector, not the prior `[0.5] * 32`). This verifies re-embedding actually overwrites the persisted value rather than preserving stale state; see Known limitations.
  - Checkpoint: `uv run pytest tests/test_pipeline.py -x -k "description_embedding"`

#### Task 3.2 — Embed description in `recompute_collection_meta`
- [x] **File**: `archon_search/pipeline.py`
- **Depends on**: Task 3.1
- **Description**:
  - In `recompute_collection_meta`, after reading `description = existing_meta.description if existing_meta else None`, add:
    - If `description` is not `None`: `description_embedding = await self._embedder.embed_one(description)`.
    - Else: `description_embedding = None`.
  - Add `description_embedding=description_embedding` to the `CollectionMeta` constructor call where the new meta is assembled.
  - This enables backfill: pre-B4 collections populate `description_embedding` on the next explicit `recompute_collection_meta` / `reindex` call.
- **Releasable**: `recompute_collection_meta` is the backfill entry point; after calling it, a collection with an existing description will have a `description_embedding`.
- **Tests (TDD)** — `tests/test_pipeline.py`:
  - Unit: `test_recompute_populates_description_embedding` — existing meta has `description = "some desc"`; after `recompute_collection_meta`, the stored `CollectionMeta` has `description_embedding` equal to the stub embed result.
  - Unit: `test_recompute_no_description_embedding_when_description_none` — existing meta has `description = None`; assert `description_embedding is None`.
  - Unit: `test_recompute_no_op_when_empty` — empty collection (no vectors) → `recompute_collection_meta` returns without calling `update_collection_meta` (this pins pre-existing behavior; verify the test doesn't already exist before adding).
  - Checkpoint: `uv run pytest tests/test_pipeline.py -x -k "recompute"`

---

### Phase 4 — Router: strategy/description_weight + blended `rank()`

> **Releasable**: after Task 4.2, `MultiCollectionRouter` supports `strategy="hybrid"` and blends description-embedding cosine with centroid cosine per collection, with per-collection fallback. Default config stays `"centroid"` — no behavior change for existing deployments.

#### Task 4.1 — Add `description_embedding` to `_ROUTING_FIELDS` and `fetch_metadata`
- [x] **File**: `archon_search/router.py`
- **Depends on**: Task 2.1
- **Description**:
  - Add `"description_embedding"` to `_ROUTING_FIELDS` set (module-level constant in `router.py`). This causes `fetch_metadata` to include the field when deserializing `CollectionMeta` from the JSON-RPC response.
  - **`fetch_metadata` opt-in under hybrid**: because `get_collections_meta` (server side, Task 5.2) strips `description_embedding` BY DEFAULT, the production router must opt in to receive it under hybrid. Update `fetch_metadata`'s JSON-RPC payload as follows: when `self._strategy == "hybrid"`, set `"arguments": {"include_description_embedding": True}`; otherwise leave `"arguments": {}` (centroid mode does not need the data — cheap optimization). Note that `self._strategy` is added to `__init__` in Task 4.2; until Task 4.2 lands, `_strategy` does not exist, so the conditional must be added in the same commit as Task 4.2 OR guarded with `getattr(self, "_strategy", "centroid") == "hybrid"`. The cleaner option is to land both changes together — Task 4.1 and Task 4.2 are tightly coupled and can be combined into a single PR if convenient.
  - No other change — `description_embedding` defaults to `None` on `CollectionMeta`, so existing responses without the field silently yield `None`.
- **Releasable**: `fetch_metadata` round-trips `description_embedding` when the server includes it; under hybrid, it actively asks the server for it.
- **Tests (TDD)** — `tests/test_router.py`:
  - Unit: `test_routing_fields_includes_description_embedding` — `"description_embedding"` in `_ROUTING_FIELDS`.
  - Unit: `test_fetch_metadata_deserializes_description_embedding` — mock JSON-RPC response with `"description_embedding": [0.1, 0.2]` on one collection; `fetch_metadata` returns a `CollectionMeta` with `description_embedding == [0.1, 0.2]`.
  - Unit: `test_fetch_metadata_missing_description_embedding_yields_none` — response without the field → `description_embedding is None` (no `KeyError`).
  - Unit: `test_fetch_metadata_passes_include_flag_under_hybrid` — construct a `MultiCollectionRouter(strategy="hybrid", ...)`; mock `httpx.AsyncClient.post` and capture the JSON payload; assert `payload["params"]["arguments"] == {"include_description_embedding": True}`.
  - Unit: `test_fetch_metadata_omits_include_flag_under_centroid` — construct a router with `strategy="centroid"` (default); mock the POST and assert `payload["params"]["arguments"] == {}` (no `include_description_embedding` key). This guards against the cheap optimization being accidentally removed.
  - Checkpoint: `uv run pytest tests/test_router.py -x -k "description_embedding or routing_fields or include_flag"`

#### Task 4.2 — Implement `strategy` + `description_weight` in `MultiCollectionRouter`
- [x] **File**: `archon_search/router.py`
- **Depends on**: Task 4.1, Task 2.1
- **Description**:
  - Define a module-level constant in `archon_search/constants.py`: `DEFAULT_ROUTING_DESCRIPTION_WEIGHT: Final[float] = 0.3`. **Location rationale**: placing it in `constants.py` (rather than `router.py`) lets `config.py` import it without reversing the existing `config → router` import direction (`config.py` already imports nothing from `router.py`; pulling in `router.py` to read a default would create a new dependency edge). This is the single change point for the default weight; `MultiCollectionRouter.__init__`, the `SearchConfig` default, the `_run_router_for_query` default, and `archon-search.toml.example` all reference / mirror this constant rather than hardcoding `0.3`. `router.py` imports it as `from archon_search.constants import DEFAULT_ROUTING_DESCRIPTION_WEIGHT`.
  - `__init__` gains `strategy: Literal["centroid", "hybrid"] = "centroid"` and `description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT`; stored as `self._strategy` and `self._description_weight`.
  - `_score_collections` is extended. **Case order is critical**:
    1. **Existing usable-centroid guard applies first, unchanged**: a collection is eligible for scoring only if `col.centroid is not None and col.embedding_model == self._embedding_model`. This guard automatically excludes `col.embedding_model == ""` whenever `self._embedding_model` is non-empty (the production case), so empty-tag collections remain in the **unscored tail** under both strategies — B4 preserves this behavior exactly.
    2. **Under `"centroid"` (default)**: for each eligible collection, score = `cos(q, centroid)`. Identical to today.
    3. **Under `"hybrid"`**: for each eligible collection, attempt the blend. Apply the blend ONLY if ALL of the following hold:
       - `col.description_embedding is not None`
       - `len(col.description_embedding) == len(query_embedding)` (dimensionality match — guards against stale embeddings from a prior model with a different vector width)
       - `col.description_embedding` is not the all-zeros vector (zero-norm guard — a zero-norm `description_embedding` would have undefined cosine; blending it would penalize the score rather than act as a no-op fallback)
       - `col.embedding_model == self._embedding_model` (already implied by the usable-centroid guard, but stated for clarity)
       - `col.embedding_model != ""` (defensive — the usable-centroid guard already excludes this in production, but the check is explicit here)
       
       When all hold, score = `(1 - self._description_weight) * cos(q, col.centroid) + self._description_weight * cos(q, col.description_embedding)`. Otherwise, fall back to centroid-only within the hybrid path: score = `cos(q, col.centroid)` (NOT moved to the unscored tail — the collection is still eligible, just not blended).
  - `rank()` calls the updated `_score_collections`; all downstream logic (has_scored check, confidence gate, shortlist slice, unscored fallback, unscored ordering) is unchanged and operates on the blended score.
  - `rank_with_scores()` also calls the updated `_score_collections` (scores are now blended under hybrid, which is correct for `/explain`).
  - `w = 0.0` case: blend reduces to `cos(q, centroid)` exactly.
  - `w = 1.0` case: blend = `cos(q, description_embedding)` when present, else `cos(q, centroid)`.
- **Releasable**: `MultiCollectionRouter(strategy="hybrid", description_weight=DEFAULT_ROUTING_DESCRIPTION_WEIGHT)` blends signals correctly with per-collection fallback; `strategy="centroid"` produces identical ordering and identical confidence-gate decisions versus pre-B4.
- **Tests (TDD)** — `tests/test_router.py`:
  - Unit: `test_centroid_strategy_identical_to_pre_b4` — regression pin: construct a `MultiCollectionRouter` with `strategy="centroid"` over a fixed set of collections (some with `description_embedding` set, some without) and synthetic query/centroid embeddings whose cosine values are hand-computable (e.g. `q = [1, 0, 0]`, `c1 = [1, 0, 0]` → cosine 1.0; `c2 = [0.6, 0.8, 0]` → cosine 0.6; etc.). Assert `_score_collections` returns the **exact numeric scores** (not just ordering) matching the hand-computed expected values, demonstrating that the `strategy="centroid"` code path preserves the pre-B4 `cos(q, centroid)` formula exactly.
  - Unit: `test_hybrid_outranks_tight_off_topic_centroid` — the ADR-04 failure mode: one collection has a diffuse centroid (low cosine to query) but a description embedding that aligns well; another has a tight centroid that scores higher on centroid alone; under `strategy="hybrid"` the first collection ranks first. (Construct synthetic embeddings arithmetically — no model needed.)
  - Unit: `test_per_collection_centroid_fallback` — collection with `description_embedding=None` under `strategy="hybrid"` is scored by centroid alone and not penalized (appears in scored set, not unscored).
  - Unit: `test_hybrid_all_description_embeddings_none_degrades_to_centroid` — every collection has `description_embedding=None`; under `strategy="hybrid"`, scored values equal `strategy="centroid"` scores **exactly** for every collection, and the ranked order is identical.
  - Unit: `test_hybrid_dimensionality_mismatch_falls_back_to_centroid` — collection has `description_embedding` whose length differs from the query embedding (e.g. query is 128d, description embedding is 96d); under hybrid, the blend is skipped and score = centroid cosine; no exception raised.
  - Unit: `test_hybrid_zero_norm_description_embedding_falls_back_to_centroid` — collection has `description_embedding = [0.0] * dim`; under hybrid, the blend is skipped (zero-norm guard) and score = centroid cosine.
  - Unit: `test_model_mismatch_description_embedding_ignored` — collection with `description_embedding` set but `embedding_model` not matching `router._embedding_model`; the existing usable-centroid guard already pushes this collection to the unscored tail (pre-B4 behavior preserved).
  - Unit: `test_empty_embedding_model_remains_unscored_under_hybrid` — collection with `description_embedding` set but `embedding_model == ""` (legacy default) and `router._embedding_model` non-empty; assert the collection is appended to the **unscored tail** (NOT scored with centroid-only). This pins that B4 does not change the existing centroid guard's treatment of empty-tag collections.
  - Unit: `test_weight_zero_equals_centroid` — `description_weight=0.0` produces identical scores to `strategy="centroid"`.
  - Unit: `test_weight_one_pure_description_embedding` — `description_weight=1.0` on a collection with a description embedding; score = `cos(q, description_embedding)`.
  - Unit: `test_confidence_gate_uses_blended_score` — confidence gate fires on the blended max score; all-unscored bypass is unchanged.
  - Unit: `test_hybrid_gate_fires_when_all_description_embeddings_none_and_centroids_below_threshold` — construct a `MultiCollectionRouter(strategy="hybrid", confidence_threshold=0.5, ...)` over collections where (a) every collection has `description_embedding=None`, (b) every collection has a usable centroid (so the all-unscored bypass does NOT trigger — they are scored, just centroid-only), and (c) every centroid cosine is below `0.5`. Assert `rank()` returns `[]` (the confidence gate fires). This confirms that the all-`description_embedding=None` degradation correctly reduces to centroid-only scoring AND preserves the confidence-gate semantics — the all-None case must NOT be confused with the all-unscored-centroid bypass branch.
  - Unit: `test_hybrid_does_not_spuriously_bypass_at_default_threshold` — construct a `MultiCollectionRouter(strategy="hybrid", confidence_threshold=0.30)` with synthetic embeddings such that the centroid-only `max(score)` clears 0.30; assert the hybrid `max(blended_score)` also clears 0.30 (no spurious bypass under default threshold). If a constructed case demonstrates a bypass that would not occur under centroid, document it inline as a calibration finding for Task 6.2 instead of failing — but the test as a property check should pass for non-pathological inputs.
  - Integration (`@pytest.mark.integration`) — add in `tests/test_router_integration.py` (or `tests/test_pipeline.py`) a test that: ingests a small corpus with the mock embedder; calls `recompute_collection_meta` per collection; fetches metas from the store via `pipeline.get_all_collections_meta()`; constructs a `MultiCollectionRouter` with `strategy="hybrid"`; runs a query and asserts (a) the wiring works end-to-end (no exceptions, `description_embedding` is non-None on the fetched metas) and (b) the hybrid ranking differs from centroid ranking for a query where the description aligns with one collection but the centroid aligns with another. This verifies the full ingest → persist → fetch → route hybrid path.
  - Checkpoint: `uv run pytest tests/test_router.py -x`

---

### Phase 5 — Config + server wiring + MCP surface

> **Releasable**: after Task 5.2, operators can set `routing_strategy = "hybrid"` in `archon-search.toml` and the server passes it through to the router. MCP payload is correctly stripped/extended.

#### Task 5.1 — Config parsing for `routing_strategy` and `routing_description_weight`
- [x] **File**: `archon_search/config.py`
- **Depends on**: nothing (config is independent of router implementation)
- **Description**:
  - `SearchConfig` gains `routing_strategy: str = "centroid"` and `routing_description_weight: float = DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (imported from `archon_search.constants` — see Task 4.2 for the location rationale; `config.py` must NOT import from `router.py` to avoid reversing the existing `config → router` import direction). Do not hardcode `0.3` here.
  - In `load_config`, `[routing]` section parsing (line ~169):
    - `routing_strategy`: parse string; validate `value in {"centroid", "hybrid"}`; raise `ConfigError(f"routing_strategy must be 'centroid' or 'hybrid', got {value!r}")` otherwise.
    - `routing_description_weight`: parse float via `_coerce_float`; validate `0.0 <= value <= 1.0`; raise `ConfigError(f"routing_description_weight must be in [0.0, 1.0], got {value}")` otherwise.
  - Both fields are optional in the TOML; defaults apply when absent.
  - Update `archon-search.toml.example` to include both knobs under `[routing]` with their defaults and a brief comment.
- **Releasable**: `load_config` accepts both new routing knobs from TOML; invalid values fail fast at load time.
- **Tests (TDD)** — `tests/test_config.py`:
  - Unit: `test_routing_strategy_default` — config loaded from empty TOML has `routing_strategy == "centroid"`.
  - Unit: `test_routing_strategy_hybrid_parsed` — TOML with `routing_strategy = "hybrid"` → `config.routing_strategy == "hybrid"`.
  - Unit: `test_routing_strategy_invalid_raises` — TOML with `routing_strategy = "cosine"` → `ConfigError`.
  - Unit: `test_routing_description_weight_default` — default equals `archon_search.constants.DEFAULT_ROUTING_DESCRIPTION_WEIGHT` (currently `0.3`); assert via the constant, not a hardcoded literal, so the test follows the constant if it changes.
  - Unit: `test_routing_description_weight_boundary_zero` — `0.0` parses without error.
  - Unit: `test_routing_description_weight_boundary_one` — `1.0` parses without error.
  - Unit: `test_routing_description_weight_out_of_range_raises` — `1.1` → `ConfigError`; `-0.1` → `ConfigError`.
  - Checkpoint: `uv run pytest tests/test_config.py -x -k "routing_strategy or routing_description_weight"`

#### Task 5.2 — Wire new knobs through `_build_router` and MCP surface
- [x] **File**: `archon_search/server/routes_route.py`, `archon_search/server/mcp.py`
- **Depends on**: Task 5.1, Task 4.2
- **Description**:
  - `_build_router` (in `routes_route.py`): pass `strategy=config.routing_strategy` and `description_weight=config.routing_description_weight` to `MultiCollectionRouter(...)`.
  - `mcp.py` `list_collections` tool: add `d.pop("description_embedding", None)` immediately after `d.pop("centroid", None)`.
  - `mcp.py` `get_collections_meta` tool: **strip `description_embedding` BY DEFAULT** (same convention as `list_collections`) to keep payload bounded at scale (~9 MB at 1000 collections at 768d would exceed agent context windows). Add an optional `include_description_embedding: bool = False` parameter to the tool; when `True`, `description_embedding` is included on each collection. Update the tool docstring to warn about payload size at scale and document the flag. **Param name rationale**: `include_description_embedding` (not the earlier draft name `include_embeddings`) — explicit about which field is included; `centroid` is unaffected by this flag (it is already returned today and B4 does not change that).
  - `mcp.py` `get_collection_meta` tool (single-collection): `description_embedding` is included by default (single-collection payload is bounded). No new parameter needed.
  - **FastMCP schema verification**: confirm that the project's FastMCP version (declared in `pyproject.toml`) introspects optional boolean parameters from the tool function signature and exposes them in the MCP tool schema. Run `archon-search` locally, list MCP tools, and verify the `get_collections_meta` schema advertises `include_description_embedding` as an optional boolean input. If FastMCP requires an explicit schema decorator/annotation for tool params, add it here. (Most recent FastMCP versions handle this via signature introspection — this is a one-time smoke check, not an architectural unknown.)
- **Releasable**: the server routes `routing_strategy` and `routing_description_weight` from config to the router; `list_collections` MCP payload does not include `description_embedding`; `get_collections_meta` does.
- **Tests (TDD)** — `tests/test_routes_route.py`, `tests/test_mcp.py`:
  - Unit: `test_build_router_passes_strategy` — mock `MultiCollectionRouter`; assert constructor is called with `strategy=config.routing_strategy` and `description_weight=config.routing_description_weight`.
  - Unit: `test_build_router_default_weight_matches_constant` — config loaded from empty TOML (all defaults); assert `MultiCollectionRouter` is called with `description_weight == archon_search.constants.DEFAULT_ROUTING_DESCRIPTION_WEIGHT`. This catches hardcoded constant bugs in `_build_router` and follows the constant if calibration (Task 6.2) changes it.
  - Unit: `test_list_collections_strips_description_embedding` — mock `pipeline.get_all_collections_meta()` returning a meta with `centroid=[0.2]` AND `description_embedding=[0.1]`; assert the MCP payload contains **neither** `"centroid"` **nor** `"description_embedding"`. Asserting both absent (not just the new field) guards against a future edit accidentally removing the existing `pop("centroid", None)` call.
  - Unit: `test_get_collections_meta_strips_description_embedding_by_default` — same mock; call the tool without `include_description_embedding`; assert the payload does NOT contain `"description_embedding"`. (Note: `get_collections_meta` today returns `centroid` and B4 does NOT change that — `centroid` presence is unchanged.)
  - Unit: `test_get_collections_meta_includes_description_embedding_when_opted_in` — same mock; call the tool with `include_description_embedding=True`; assert the payload DOES contain `"description_embedding"`.
  - Unit: `test_get_collection_meta_includes_description_embedding` — single-collection variant returns `description_embedding` by default (no flag needed).
  - Checkpoint: `uv run pytest tests/test_routes_route.py tests/test_mcp.py -x -k "description_embedding or routing_strategy or build_router"`

---

### Phase 6 — Eval run under hybrid + baseline + floors

> **Releasable**: after Task 6.2, the eval suite runs both strategies in one corpus ingest, records all four new metric keys in the baseline, and the `thresholds.toml` hybrid floor (Δ ≥ 0) is live.

#### Task 6.1 — Wire hybrid router pass into `run_eval_suite`
- [x] **File**: `archon_search/eval/runner.py`
- **Depends on**: Task 1.4, Task 4.2, Task 3.2
- **Description**:
  - **Constructor passthrough wired here**: per Task 1.4's phase-ordering note, `_run_router_for_query` accepts `strategy` / `description_weight` from Phase 1 but does NOT pass them to `MultiCollectionRouter.__init__` until this task. With Task 4.2 now complete (constructor accepts the params), update `_run_router_for_query` to pass `strategy=strategy` and `description_weight=description_weight` through to the `MultiCollectionRouter(...)` call. This is the deferred wiring from Task 1.4.
  - For each routing-scope query in `run_eval_suite`, after recording the centroid `ranked_collections` trace (in `centroid_routing_traces`), run a second call: `hybrid_ranked = await _run_router_for_query(pipeline, q.text, collection_metas, strategy="hybrid", description_weight=DEFAULT_ROUTING_DESCRIPTION_WEIGHT)`.
  - **`collection_metas` sourcing**: verified against `archon_search/eval/runner.py` — the existing fetch `collection_metas = await pipeline.get_all_collections_meta()` (currently at `runner.py:566`) runs **after** `_ingest_corpus`, which calls `recompute_collection_meta` per collection (currently at `runner.py:484`). Once Task 3.2 lands (description embedding populated in `recompute_collection_meta`), this single existing fetch already returns metas with non-None `description_embedding` — **no second fetch is needed**. Add a code comment at the existing fetch site stating: "Fetched after `_ingest_corpus` → `recompute_collection_meta`; `description_embedding` is now load-bearing for hybrid routing, do not move this fetch earlier." Both the centroid and hybrid router constructions reuse this same `collection_metas` list.
  - Record `hybrid_ranked` on a separate `QueryEvalTrace` object added to `hybrid_routing_traces`. `QueryEvalTrace` does NOT gain a `routing_strategy` field — the two-list approach (established in Task 1.1) separates them unambiguously.
  - Compute `routing_mrr_hybrid = compute_routing_mrr(hybrid_routing_traces, gold_fn)` and `routing_precision_at_1_hybrid = compute_routing_precision_at_1(hybrid_routing_traces, gold_fn)`.
  - Store all four new metrics in `EvalMetrics`.
  - Add `routing_mrr_centroid`, `routing_mrr_hybrid`, `routing_precision_at_1_centroid`, `routing_precision_at_1_hybrid` to the flat baseline dict (alongside `routing_accuracy`).
  - Extend `_QUALITY_FLOOR_FIELDS` with all four new keys (floors will be set in Task 6.2).
  - `assert_thresholds` and `render_report` handle all four new fields.
- **Latency-floor impact (verified against `archon_search/eval/runner.py`)**: this task doubles the routing-pass work per routing-scope query (one centroid call + one hybrid call). The harness's `latency_p50_ms` / `latency_p95_ms` are computed from `latencies = [t.latency_ms for t in retrieval_traces]` (currently `runner.py:665`) — **routing-scope traces are excluded** from the latency percentiles, so the added second router pass does NOT affect the gated latency metrics. The routing-trace `latency_ms` field is recorded but not aggregated into the gated percentiles. No latency-floor regression is expected from this task; if the runner's latency aggregation is ever extended to include routing traces, this assumption must be re-evaluated.
- **Releasable**: a single `-m eval` run exercises both strategies per routing query and records all four metric keys.
- **Tests (TDD)** — `tests/eval/test_runner.py`:
  - Unit: `test_run_eval_suite_records_hybrid_metric` — synthetic eval run with mocked pipeline where `recompute_collection_meta` populates `description_embedding` on the returned metas; assert `report.metrics.routing_mrr_hybrid is not None`.
  - Unit: `test_hybrid_and_centroid_metrics_are_independent` — mutating one routing trace list does not affect the other.
  - Unit: `test_hybrid_metric_differs_from_centroid_when_description_changes_order` — synthetic run where the hybrid router returns a different ranking than centroid (mock `_run_router_for_query` to return different orderings per strategy); assert `routing_mrr_hybrid != routing_mrr_centroid`. This is the runner-level rank-sensitivity test (complements the metric-level `test_routing_mrr_changes_when_order_changes` from Task 1.2).
  - Unit: `test_hybrid_router_receives_metas_with_populated_description_embedding` — set up a synthetic eval run where `_ingest_corpus` + `recompute_collection_meta` populate `description_embedding` on every collection's persisted meta. Spy on the `MultiCollectionRouter` constructor for the hybrid pass and assert that the `initial_metadata` list it receives contains entries with non-None `description_embedding` (not just that some fetch was called). This is the property that matters — if the runner ever moves the fetch to before recompute (or otherwise feeds stale metas), hybrid scoring silently degrades to centroid-only for all collections, and this test fails.
  - Checkpoint: `uv run pytest tests/eval/test_runner.py -x -k "hybrid"`

#### Task 6.2 — Regenerate baseline with hybrid metrics + add `routing_mrr_hybrid` floor
- [x] **File**: `tests/eval/baselines/baseline.json`, `tests/eval/baselines/baseline.md`, `tests/eval/thresholds.toml`
- **Depends on**: Task 6.1, Task 1.5
- **Description**:
  - Run the calibration command from `baselines/regenerate.py` over the full expanded corpus. The baseline now records `routing_mrr_centroid`, `routing_mrr_hybrid`, `routing_precision_at_1_centroid`, `routing_precision_at_1_hybrid` as top-level keys in the flat metric dict alongside all existing keys.
  - Inspect the results:
    - If `routing_mrr_hybrid >= routing_mrr_centroid` (Δ ≥ 0): proceed. Set `routing_mrr_hybrid` floor = measured `routing_mrr_centroid` baseline value (encoding Δ ≥ 0: hybrid must be at least as good as centroid baseline). Set `routing_mrr_centroid` floor = its own measured value (guard the default path).
    - If `routing_mrr_hybrid < routing_mrr_centroid` (Δ < 0): this feature does NOT merge as a recommended path (per rule 2). Document this finding in a waiver entry in `baseline.json` under `waiver_ids` and leave the hybrid floor unset (or set conservatively). Evaluate whether the fixture corpus is discriminating enough before concluding hybrid is worse.
  - Add `routing_mrr_hybrid` floor to `thresholds.toml` at the value decided above.
  - **Weight calibration**: the calibration sweep may indicate that a `description_weight` other than the initial `0.3` maximizes the offline metric without overfitting. If so, update the single change point — `DEFAULT_ROUTING_DESCRIPTION_WEIGHT` in `archon_search/constants.py` — and record the rationale in the baseline commit message. The example TOML and all defaults follow the constant automatically; no other code changes are needed. If the sweep confirms `0.3`, leave the constant unchanged.
  - Optionally add `routing_precision_at_1_centroid` and `routing_precision_at_1_hybrid` floors based on P@1 gating decision (from open question — default: report-only, no floor, leave `None`).
  - Commit updated `baseline.json`, `baseline.md`, and `thresholds.toml`.
  - Verify: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` passes.
- **Releasable**: B4's merge bar is defined and live: hybrid shows Δ ≥ 0 vs centroid offline, or the feature is flagged.
- **Confidence threshold re-confirmation**: inspect the hybrid run results to confirm that the default `routing_confidence_threshold = 0.30` does not cause spurious query bypasses under hybrid scoring. Specifically, check that all routing-scope queries in the expanded fixture produce `max(blended_score) >= 0.30` (i.e. none are incorrectly bypassed). If any query falls below 0.30 under hybrid due to the convex combination shifting scores, record the finding here (in a comment in the baseline commit message or a note in `baseline.json`). ADR-07 is authored in Task 7.1 and will incorporate this finding.
- **Tests (TDD)** — `tests/eval/test_eval_suite.py`, `tests/eval/test_runner.py`:
  - Eval: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` — all floors hold; latency p50/p95 floors still hold.
  - Unit: `test_assert_thresholds_fails_when_hybrid_mrr_below_centroid_floor` — construct an `EvalReport` where `routing_mrr_hybrid = 0.5` and the `routing_mrr_hybrid` floor is `0.8`; assert `assert_thresholds` raises `AssertionError`. This pins the Δ ≥ 0 merge-gate mechanism — a future change that removes the floor would be caught by this test.
  - Checkpoint: `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`

---

### Phase 7 — Documentation

> **Releasable**: after Task 7.1, all documentation reflects the delivered implementation and all acceptance criteria are verified.

#### Task 7.1 — Final verification & documentation update
- [x] **File**: N/A (agent task)
- **Depends on**: all prior tasks
- **Description**:
  - Spawn an agent to discover all documentation in the project and update every file whose content is affected by B4:
    - `Documentation/ADRs/07_description_embedding_hybrid_routing.md` — new ADR recording the hybrid-routing decision, **extending ADR-04** (ADRs are append-only; do not edit ADR-04 — its centroid-default decision remains in force, B4 only adds an opt-in alternative; ADR-04's status header is NOT changed to "Superseded"). Cover: problem (diffuse centroid), solution (description embedding blend), decision (centroid stays default, hybrid opt-in), consequences (per-collection fallback, eval gate fix).
    - `BREAKING.md` — document: (a) MCP `get_collection_meta` (single-collection) gains `description_embedding` output key by default (additive, breaking for strict-validating clients); (b) MCP `get_collections_meta` (multi-collection) gains an additive optional INPUT parameter `include_description_embedding: bool = False` and now STRIPS `description_embedding` from the output by default (the field was never present pre-B4, so output is unchanged for callers who omit the flag; the additive input is breaking only for clients that reject unknown parameters); (c) MCP `list_collections` strips `description_embedding` (no change for existing clients — field was never present); (d) REST `/route` shape unchanged.
    - `tests/eval/README.md` — add: routing fixture schema documentation (new `"faq"` collection, fixture coupling rules), rank-sensitive metric documentation (`routing_mrr_centroid` / `routing_mrr_hybrid`, P@1), hybrid floor policy (floor = centroid baseline value), threshold refresh procedure note for B4's hash changes.
    - `Documentation/Backlog/03_world_class_roadmap.md` — annotate B4 checkbox as completed: note "one artifact + deferred multi-centroid" narrowing of roadmap item 9.
    - `archon-search.toml.example` — add `routing_strategy` and `routing_description_weight` under `[routing]` with defaults and comments.
    - `Documentation/Architecture/100_system_architecture_overview.md` and `110_component_catalog_and_layer_breakdown.md` — update router section to describe the hybrid strategy and `description_embedding` artifact.
    - `Documentation/Architecture/600_api_reference_or_public_interface.md` — document new `routing_strategy` and `routing_description_weight` config knobs; note MCP `get_collections_meta` opt-in flag and `get_collection_meta` additive output key.
    - `Documentation/Architecture/530_technical_debt_refactoring_roadmap.md` — add a `schema_version` debt entry describing the deferred per-meta-table version marker and the compounding cost as more migration columns are added (B4 adds one more "column-absent = None" tolerance after A1; B5/B6 may add more, at which point `schema_version` becomes a prerequisite rather than a deferral).
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: B4 is fully implemented, verified, and documented.
- **Acceptance criteria** (must all pass):
  - `CollectionMeta.description_embedding` field exists and defaults to `None`; all existing `CollectionMeta(...)` call sites still compile without change.
  - Ingest a collection for which `generate_description` returns a non-None string; `get_collection_meta` returns a `CollectionMeta` with `description_embedding` of the same length as `centroid`.
  - `routing_strategy = "centroid"` (default) produces identical ordering and identical confidence-gate decisions versus pre-B4, with exact numeric scores matching hand-computed cosine values on synthetic embeddings (regression-pinned unit test passes).
  - `routing_strategy = "hybrid"` with `description_weight = 0.3` blends centroid-cosine and description-embedding-cosine per collection; a collection with `description_embedding=None` under hybrid receives centroid-only fallback (unit test passes).
  - `load_config` with `routing_strategy = "multi_centroid"` raises `ConfigError`.
  - `load_config` with `routing_description_weight = 1.5` raises `ConfigError`.
  - MCP `list_collections` response payload contains neither `centroid` nor `description_embedding` (unit test passes).
  - MCP `get_collections_meta` strips `description_embedding` by default; includes it when called with `include_description_embedding=True`; the production router under `strategy="hybrid"` passes this flag (unit tests pass).
  - MCP `get_collection_meta` (single-collection) response payload contains `description_embedding` (unit test passes).
  - `uv run pytest` (default run, no markers) passes with `--cov-fail-under=85`.
  - `uv run pytest -m integration` passes (description_embedding round-trips; old table schema-evolution reads `None`).
  - `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py` passes: all quality floors hold; `routing_mrr_centroid` and `routing_mrr_hybrid` are both non-None; `routing_mrr_hybrid >= routing_mrr_centroid_floor` (Δ ≥ 0); latency p50/p95 floors hold.
  - `BREAKING.md` has an entry for MCP `get_collections_meta`/`get_collection_meta` additive key.
  - `Documentation/ADRs/07_description_embedding_hybrid_routing.md` exists and references ADR-04.
- **Tests (TDD)** — `tests/test_docs.py`:
  - Unit: `test_adr_07_exists_and_references_adr_04` — asserts `Documentation/ADRs/07_description_embedding_hybrid_routing.md` exists, is non-empty, and its contents mention `"ADR-04"`. This is the single automated check for the documentation task; manual verification covers the rest.
- **Checkpoint**: manually confirm every acceptance criterion above is checked; run `uv run pytest` and `uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py`.

# Bug Brief: Startup loads the embedding model twice concurrently (eager warmup + model validation) — ~4.6 GB each under CoreML, plus leaked CoreML contexts

**ID:** 2026-08-19-040 · **Severity:** P1 · **Status:** Open — fix decided (Option B, 2026-08-19)
**Found during:** [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

With `eager_load_embedders = true`, server startup spawns two background tasks that **each load a
full copy of the configured embedding model at the same time**:

1. the eager warmup, which loads the model into the `EmbedderCache` (`app.py:367-417`), and
2. `validate_models_async`, whose probe instantiates its **own** `TextEmbedding`
   (`model_validation.py:166`) plus a full `TextCrossEncoder` for the reranker
   (`model_validation.py:178`).

The gate that should prevent this exists but can never fire: `_run_model_validation` reads
`embedder_is_warm` from `app.state.embedder` — the *global* embedder — while eager warmup
populates a *separate instance* inside `embedder_cache`. The comment at `app.py:425-431` states
this openly: "…never this global instance, so it is typically False at startup."

Measured cost per load of `intfloat/multilingual-e5-large` under `CoreMLExecutionProvider`
(the wizard-written config): **4,629 MB RSS after load** (3.7–3.8 GB steady) — the 2.24 GB fp32
ONNX graph splits into **146 CoreML partitions** (786/1237 nodes), roughly doubling the resident
footprint vs the file size — plus **4 × "Context leak detected, CoreAnalytics returned false"**
per instance on first embed (native CoreML contexts that are never reclaimed). Two concurrent
loads also race the HuggingFace download/file locks on a cold cache (observed live: a validation
`TextEmbedding.__init__` blocked on a lock while `hf_xet` streamed the same files).

Transient startup footprint before any ingest work: ~9 GB. On a machine already under pressure
(incident day) this is the difference between surviving and not.

## Failing repro

Start the server with `eager_load_embedders = true` and CoreML providers; observe the log:

- two `UserWarning … TextEmbedding(...)` emissions from **different** files — `embedder.py:43`
  (warmup) and `model_validation.py:166` (validation) — 2026-08-19 log: 11:34:52 and 11:35:23,
  overlapping until warm-up completed at 11:35:50;
- two `CoreMLExecutionProvider::GetCapability … 146 partitions` blocks;
- ≥8 `Context leak detected` lines.

Isolated measurement (same venv):

```python
from fastembed import TextEmbedding           # RSS 86 MB after import
m = TextEmbedding("intfloat/multilingual-e5-large",
                  providers=["CoreMLExecutionProvider"])   # RSS 4,629 MB
list(m.embed(["hello world"]))                # RSS 3,692 MB steady; 4× "Context leak detected"
```

## Root cause

- `app.py:431-437`: `embedder_is_warm = app.state.embedder.is_warm` — wrong object; the cache's
  instance for `config.embedding_model` is the one warmup exercises.
- No ordering between the two tasks: `validation_task` (`app.py:451`) starts immediately, so even
  a correct warm check would race the still-loading warmup.
- The reranker probe (`model_validation.py:172-182`) always instantiates the full cross-encoder
  even when the eager warmup is about to (or already did) warm the same reranker.
- **(Review 2026-08-19)** Split-provider config doubles the reranker load *within* validation:
  the first `validate_providers_shared` call probes the reranker under `config.providers`
  (`model_validation.py:212-218`), then the split re-validation loads it again under
  `reranker_providers` (`:241-256`) and discards the first result (`:257`) — while *keeping*
  the failed CoreML probe's warning (`:258`), so healthy split installs likely report
  `checks.models: WARN` permanently (`routes_ready.py:103`; confirm live during the fix).
- **(Review 2026-08-19)** The duplication is not startup-only: the global instance is exercised
  in steady state by `POST /route` (`routes_route.py:115,126`), MCP auto-routing
  (`mcp.py:1002-1005`), HyDE when enabled (`app.py:958` → `hyde.py:131`), and graph/PPR search
  (`pipeline.py:1405`) — the first such use loads a **second resident copy** (~4.6 GB CoreML)
  beside the cache's warmed one. `GET /status` readiness detail also reads the global's warmth
  (`readiness.py:39-41`), reporting the embedder cold after a successful warmup.

## Production evidence

Incident log (session 1): both loads 11:34:52 / 11:35:23; `eager model warm-up complete
(1 embedding models + reranker)` 11:35:50; 10 × `Context leak detected`. Session 2 (post-crash):
same double load while the corpus re-sync was already running; the second load's HF fetch was
still in flight 4 minutes after boot (live thread sample).

## Approved fix — Option B (decided 2026-08-19)

One shared instance per model, plus sequencing. Constraint that shaped the decision: sequencing
and the warm-flag fix only work **as a package** — sequencing alone still peaks ~9 GB (the
serial probe loads beside the resident cache copy), and the warm flag alone is read at task
start, before warmup completes, from the wrong object.

1. **Sequence:** `_run_model_validation` awaits `warmup_task` when one exists, *then* reads warm
   state. Safe: the task is internally bounded by `_EAGER_WARMUP_TIMEOUT_SECONDS`
   (`app.py:88,388`) and never raises except cancellation (`app.py:400-406`). Eager off →
   validate immediately, as today. `/ready` is unaffected — it never gates on validation
   results (`routes_ready.py:121-126`).
2. **Seed, don't add an accessor:** `EmbedderCache` accepts a pre-seeded, non-evictable entry
   `{config.embedding_model: app.state.embedder}` (global built at `app.py:870`, cache at
   `app.py:367`); the eviction loop (`embedder_cache.py:134-135`) skips the pinned key. Eager
   preload then warms the shared instance itself, so the existing check
   `app.state.embedder.is_warm` (`app.py:432`) becomes correct **as written**, `GET /status`
   turns truthful, and the steady-state consumers (route/MCP/HyDE/PPR) reuse the warmed copy.
   **Decided:** the pinned entry does NOT count toward `embedder_cache_size` — preserves
   today's worst case (N cached + 1 global resident) exactly. Safe: the default model never
   changes at runtime (`app.py:870` is its only assignment; no config route mutates
   `embedding_model`), and seeding happens in the lifespan before any concurrent access.
3. **Reranker skip:** `validate_models_async` gains `reranker_is_warm` (fed from
   `pipeline.reranker_is_warm`, `pipeline.py:387-388`), mapped to the existing `""` skip
   (`model_validation.py:134-135,140`). No `validate_providers_shared` signature change — the
   wizard shares it (`install/installer.py:200,217`); the `""` contract is the seam.
4. **Split-config probe skip:** pass `""` for the reranker in the *first* shared call when
   `config.reranker_providers` is set — its result is discarded today (`:257`) and its CoreML
   failure warning is pure noise (also clears the permanent-WARN suspicion above).
5. **Docs in the same PR:** rewrite the `app.py:424-428` admission comment and the S9 note in
   the `validate_models_async` docstring (`model_validation.py:199-201`); update
   `Architecture/110`/`120` where they reference `embedder_is_warm`. `Completed/D6-*` stays
   untouched (archive).

**Out of scope** (own brief): graph/PPR-mode embedder resolution and removing the global
embedder entirely — [[2026-08-19-100-retire-global-embedder-brief.md]].

## Verification

Per `tests/CLAUDE.md` lifespan rules — poll `.done()`, never await lifespan tasks from the
test loop:

- Eager on: exactly **one** `fastembed.TextEmbedding` construction process-wide (patch it,
  assert call count) and validation reports `embedder_ok=True`.
- Ordering: validation completes only after the warmup task; warmup-**FAILED** path still runs
  the full probe (diagnostics preserved).
- Eager off: validation still probes (construction count 1).
- Reranker: warm → **zero** `_load_cross_encoder` calls in validation; split config without
  warmup → exactly **one** (not two).
- Pinning: under LRU eviction pressure the seeded entry survives and
  `get_or_load(config.embedding_model)` returns the *identical* object
  (`is app.state.embedder`).

## References

- [[archon_search/server/app.py]] — warmup task (:367-417), validation task + admission comment (:418-453)
- [[archon_search/model_validation.py]] — embedder probe (:161-170), reranker probe (:172-182), warm-skip contract (:134-137)
- [[archon_search/embedder_cache.py]] — the cache the shared instance is seeded into
- [[2026-08-19-000-oom-crash-incident-report.md]] — measurement D
- [[2026-08-19-100-retire-global-embedder-brief.md]] — follow-up: retire the global embedder (Option C)

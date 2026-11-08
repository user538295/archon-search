// C9 — Source of `GET /status.search.context_window_max` (Interface Adapters <-> config, internal).
// The wire shape does not change: `SearchStatusDetail` keeps max_fanout, top_k_max, context_window_max, so no field, type or path of either OpenAPI snapshot changes for it and no OpenAPI artefact is emitted for this seam. The one exception is the model's docstring: `SearchStatusDetail`'s docstring in schemas.py (line 310) is published as its `description` in both snapshots (line 3765), so the sentence rewritten below changes that one `description` string in `tests/server/openapi_snapshot.json` and `tests/contract/openapi_snapshot.json` (regenerated together).
// What changes is where the value comes from. Today routes_status.py (line 195) reads `request.app.state.pipeline.context_window_max`; the cap leaves the pipeline (C3), so it is read from the `[search]` config that `_build_search_status` already receives.
namespace StatusContextWindowSourceContract;

// `SearchConfig.context_window_max` (config.py): floor 1, default 10 (DEFAULT_CONTEXT_WINDOW_MAX). The status value and the value the normaliser (C2) enforces come from this one field, so they cannot disagree.
model SearchStatusDetail {
  max_fanout: int32;
  top_k_max: int32;
  context_window_max: int32;
}

// `_build_search_status` drops its `context_window_max` keyword and reads it from the config argument. Docstrings that call it "the bound the served SearchPipeline enforces" (routes_status.py line 252, schemas.py line 310) change.
interface StatusSearchBlock {
  build(): SearchStatusDetail;
}

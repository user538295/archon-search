# ADR-10: CoreML Split-Provider Configuration

**Status:** Accepted  
**Date:** 2026-07-20  
**Supersedes:** D6 deferral ("split providers deferred until a real device can be tested")

## Context

The D6 provider-validation brief deferred split-provider support — writing separate ONNX provider
lists for the embedder and reranker — because no device was available to confirm that CoreML
fails for the reranker but works for the embedder. Field reports confirmed this scenario is real:
some Apple Silicon configurations load the cross-encoder reranker under CoreML but fail at
inference with a shape mismatch, while the text embedder succeeds cleanly.

Without split-provider support the wizard either wrote `providers = ["CoreMLExecutionProvider"]`
for both models (reranker silently CPU-falls-back at runtime with no operator visibility) or fell
back to CPU entirely after the combined probe failed (wasting GPU for embeddings).

## Decision

Add a `reranker_providers` field to `[database]` in `archon-search.toml`:

- **Absent (null):** the reranker inherits `providers` — fully backward-compatible.
- **`[]` (empty list):** the reranker uses CPU regardless of `providers`.
- **Non-empty list:** the reranker uses that provider list.

The wizard (Step 9 CoreML gate) now runs a two-phase probe:

1. Combined probe (`validate_providers`): embedder + reranker under CoreML.
2. If combined fails: embedder-only probe (`validate_embedder_only`): embedder under CoreML
   with reranker disabled (passes `reranker_model=""`).

When the embedder-only probe passes, the wizard writes the split config and sets the install
summary to `"CoreML — text search; CPU — result ranking"`. The FE-1 post-download re-probe
is skipped in this case (`split_coreml = True`) because the result is already known.

A one-time WARNING is logged at startup when `providers` contains `CoreMLExecutionProvider`
but `reranker_providers` is absent — indicating a pre-fix install that may have a silently
degraded reranker.

## Consequences

- **Positive:** Apple Silicon users with the cross-encoder CoreML issue now get GPU-accelerated
  text search instead of full CPU fallback. The degraded state is surfaced clearly at install
  time and at startup rather than silently.
- **Positive:** Fully backward-compatible: existing configs with `providers` only are unchanged.
- **Negative:** One more TOML field to document and parse, and two new `InstallWizard` methods
  (`validate_embedder_only`, `configure_reranker_providers`).
- **Accepted:** The complexity cost is low; the operator benefit on affected hardware is high.

## Implementation

- `config.py`: `reranker_providers: list[str] | None = None` field + `_apply_toml` parse.
- `pipeline.py`: `ModelReranker` construction uses `reranker_providers if not None else providers`.
- `server/app.py`: same logic for the server-side reranker construction.
- `install.py`: `validate_embedder_only`, `configure_reranker_providers`, split branch in Step 9,
  `split_coreml` guard in FE-1.
- `model_validation.py`: stale-config advisory warning in `validate_models_async`.

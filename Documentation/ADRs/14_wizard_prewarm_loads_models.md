# 14. Wizard Pre-Warm Loads the Models It Downloads

**Status**: Accepted
**Date**: 2026-10-09
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR 10 — CoreML Split-Provider Configuration](10_coreml_split_providers.md) — its sentence "The FE-1 post-download re-probe is skipped in this case (`split_coreml = True`) because the result is already known" (`10_coreml_split_providers.md:34-35`) and its Implementation item "`split_coreml` guard in FE-1" (`10_coreml_split_providers.md:56-57`): the re-probe, and with it the `split_coreml` flag, no longer exist. Nothing else in ADR 10 is affected by this decision. [ADR 13](13_graph_device_probe_two_stage_and_memory_guardrail.md)'s "pre-warm is non-fatal by construction" and its `prewarm.py` / `installer.py` line citations describe the graph pre-warm as of ADR 13's date (2026-09-07); the graph pre-warm is still non-fatal, the embedder pre-warm no longer is.

## Context

The wizard's pre-warm built `TextEmbedding(..., lazy_load=True)` and `TextCrossEncoder(..., lazy_load=True)` (`prewarm.py:132,147` at `8b8c5404`). In fastembed 0.9.0 `lazy_load=True` only downloads; the ONNX session is built later (`fastembed/text/onnx_embedding.py:310-318`). A model whose files downloaded but could not load therefore passed pre-warm, the wizard exited `0`, and every ingest failed afterwards (S671, `Documentation/Completed/S671-both_ingests_reach_done.md`).

The download-only design was deliberate: the C0 brief chose `lazy_load=True` so the CLI process would not build a throwaway ONNX session (`Documentation/Completed/C0-tiered-install-profiles-brief.md:147`).

## Decision

**Pre-warm downloads each model, then loads it and runs one inference exactly as the service does, one model at a time.**

- **Two phases per model** (`prewarm.py:99-124`): construct the fastembed class with `lazy_load=True` (download), then run the service's own backend — `ModelEmbedder.encode` / `ModelReranker.predict` — under the service's providers: `[database].providers` for the embedder, `resolve_reranker_providers` for the reranker (`prewarm.py:173-194`). The error names the phase that failed. Each model is a local of its callable, released before the next one loads.
- **Failure policy**: an embedder failure is fatal — `InstallError`, config rollback per branch, exit `1` (`prewarm.py:173-174`, `installer.py:934-955`). A reranker failure only warns (`prewarm.py:186-196`). The graph NER pre-warm is unchanged and non-fatal (`prewarm.py:248-256`).
- **One deadline, checked between models**: after the embedder, the reranker and graph NER steps run from one ordered list; before each, a passed deadline skips it and every later step with one warning naming exactly those (`prewarm.py:199-221`). A running download or load is never interrupted.
- **FE-1 removed**: `_fe1_reprobe` (`installer.py:259-273` at `8b8c5404`) re-validated the providers after the download; pre-warm now loads both models under them itself, so it has no successor.
- **CoreML fallback writes CPU**: when both CoreML probes fail, Step 9 writes `providers = []` (`installer.py:253-258`) instead of leaving a previous run's CoreML entry for pre-warm and the service to load.
- **Config reloaded after Step 9**: Step 9 rewrites the providers after Step 8b loaded the config, so `run()` reloads it (`installer.py:816`) and pre-warm loads under what the service will read.

## Consequences

### Positive

- A model that downloads but cannot load fails the install with the model, the phase and the cause, instead of a service whose every ingest fails.
- Pre-warm loads through the service's own backends (`ModelEmbedder`, `ModelReranker`), with the same providers and cache directory (`get_models_dir()`), so the install checks the load the service will perform.

### Negative / Tradeoffs

- The install pays one ONNX session build and one inference per model — the cost the C0 brief avoided.
- On Apple Silicon with GPU acceleration on, Step 9's CoreML check already builds and runs both models (`model_validation.py:435-452`), so those hosts pay at least two session builds per model: the check's and the pre-warm's.
- Hosts whose configured GPU provider the installed onnxruntime lacks now exit `1` at install: fastembed raises for a provider it cannot find (`fastembed/common/onnx_model.py:111-117`). That includes every native NVIDIA install that accepts CUDA without `onnxruntime-gpu`, since fastembed depends on the CPU `onnxruntime`. Before, the same host installed with exit `0` and a broken service; `--disable-gpu` installs on CPU.

## Alternatives Considered

- **Keep `lazy_load=True` only.** Rejected: it is the S671 defect.
- **Eager construction only** (no `lazy_load` phase). Rejected: one constructor call both downloads and loads, so the error can no longer say which phase failed — and the remedies differ (network or registry versus file or provider).
- **Reuse `validate_providers_shared`.** Rejected: it takes one providers list for both models and never raises (`model_validation.py:385-392`), while pre-warm needs separate embedder/reranker providers and a fatal embedder failure.

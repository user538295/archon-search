# 02. fastembed for Dense Embeddings

**Status**: Accepted
**Date**: 2026-05-20
**Deciders**: archon-search maintainers

## Context

The retrieval pipeline (`archon_search/embedder.py`) needs to produce dense
embeddings for both ingestion (chunks) and query time. Constraints:

- The server must install via `pip` and run without GPU on most developer
  laptops, while still allowing GPU/CoreML acceleration when available.
- The dependency footprint should be minimal — avoid pulling a full deep
  learning training stack at runtime.
- The same backend should serve `Embedder` and the cross-encoder reranker
  (`archon_search/reranker.py`) for a consistent runtime story.

The default embedding model is `BAAI/bge-small-en-v1.5` (see
`SearchConfig.embedding_model` in `archon_search/config.py` and
`archon-search.toml.example` → `[database] embedding_model`). Execution
providers are configurable via `[database] providers` and forwarded to the
underlying ONNX Runtime session.

## Decision

Use **fastembed** as the embedding and reranker runtime. `ModelEmbedder` wraps
`fastembed.TextEmbedding`; `ModelReranker` wraps
`fastembed.rerank.cross_encoder.TextCrossEncoder`. Both are lazy-loaded and
honour the configured ONNX `providers` list (CPU default,
`CUDAExecutionProvider`, `CoreMLExecutionProvider`).

## Consequences

### Positive
- No PyTorch dependency at runtime — install is faster and lighter.
- Single switch (`providers`) selects CPU / NVIDIA / Apple Silicon backends
  without code changes.
- Same library powers embeddings and reranking, reducing surface area.
- Works fully offline once model weights are cached.

### Negative
- Model selection is bounded by what fastembed exposes as ONNX; bespoke
  fine-tuned PyTorch checkpoints cannot be plugged in directly.
- First call pays a model-download and ONNX-load cost; subsequent calls are
  fast (lazy init pattern in `ModelEmbedder.encode`).
- ONNX provider availability depends on the user's wheel and platform.

## Alternatives Considered

- **sentence-transformers**: Rejected — pulls a full PyTorch runtime, which
  bloats install size and slows cold-start; CoreML/ONNX optimisation paths
  are not first-class.
- **OpenAI / hosted embeddings**: Rejected — introduces a network dependency,
  per-call cost, and sends document text off-host; conflicts with the
  "local-first, no external transmission" stance also reflected in the
  telemetry decision (see [ADR 05](05_opt_in_local_telemetry_no_raw_query.md)).
- **Bring-your-own embedder via protocol only**: Deferred — the `Embedder`
  layer already accepts an `EmbedderBackend` protocol, so this remains
  possible without changing the default.

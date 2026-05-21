# Review: ADRs/02_fastembed_for_dense_embeddings.md

## Summary

The ADR is largely accurate. All structural claims (fastembed wrapping `TextEmbedding` / `TextCrossEncoder`, lazy loading, `providers` forwarded to ONNX, default model, dependency footprint, `EmbedderBackend` protocol existence) check out against `archon_search/embedder.py`, `archon_search/reranker.py`, `archon_search/config.py`, `pyproject.toml`, and `archon-search.toml.example`. One minor stale comment in the config example is the only non-ADR-bound nit (cosmetic). No inaccuracies in the ADR itself were found.

## Inaccuracies (numbered)

None confirmed against source.

Notes on phrasings that are technically defensible but worth flagging:

- The phrase "honour the configured ONNX `providers` list (CPU default, ...)" is accurate: `ModelEmbedder.__init__` converts an empty/falsy list to `None` (`self._providers = providers or None`) and passes it to `TextEmbedding(..., providers=...)`. Same pattern in `ModelReranker`. The ADR's wording matches behavior.
- "Same library powers embeddings and reranking" — verified: both `embedder.py` and `reranker.py` import from `fastembed` / `fastembed.rerank.cross_encoder`.
- The pointer to `[database] providers` in `archon-search.toml.example` is correct; the example file shows it commented out under `[database]` (lines 31–34).

## Verified claims

1. "The retrieval pipeline (`archon_search/embedder.py`) needs to produce dense embeddings" — file exists; `ModelEmbedder.encode` returns `list[list[float]]`. (`embedder.py:24-32`)
2. "`ModelEmbedder` wraps `fastembed.TextEmbedding`" — `from fastembed import TextEmbedding` at `embedder.py:28`; instantiated at line 30.
3. "`ModelReranker` wraps `fastembed.rerank.cross_encoder.TextCrossEncoder`" — `reranker.py:33-35`.
4. "Both are lazy-loaded" — both use `self._model is None` double-checked-locking with `threading.Lock` (`embedder.py:22,25-30`; `reranker.py` confirmed via grep at lines 19, 33-35).
5. "honour the configured ONNX `providers` list (CPU default, `CUDAExecutionProvider`, `CoreMLExecutionProvider`)" — `providers` parameter is forwarded to fastembed (`embedder.py:30`); `archon-search.toml.example:31-34` lists exactly those three options with CPU as default (empty list / commented).
6. "default embedding model is `BAAI/bge-small-en-v1.5`" — `archon_search/config.py:34` `embedding_model: str = "BAAI/bge-small-en-v1.5"`; `archon-search.toml.example:24` same.
7. "`SearchConfig.embedding_model` in `archon_search/config.py`" — verified at `config.py:34, 143-144`.
8. "fastembed" listed as a runtime dependency — `pyproject.toml:10` `fastembed>=0.8.0`.
9. "No PyTorch dependency at runtime" — `pyproject.toml` `[project].dependencies` contains no `torch` / `pytorch` / `sentence-transformers` entries.
10. "lazy init pattern in `ModelEmbedder.encode`" — `embedder.py:24-32` confirms first-call download/load semantics.
11. "the `Embedder` layer already accepts an `EmbedderBackend` protocol" — `embedder.py:9-13` defines `@runtime_checkable class EmbedderBackend(Protocol)`; `Embedder.__init__` accepts it (`embedder.py:38`).
12. ADR cross-reference to ADR 05 (telemetry) — out of scope for this review; existence of the file not verified here.

## Unverifiable / ambiguous

- "install is faster and lighter" / "bloats install size and slows cold-start" — qualitative claims; not directly checkable from source. Plausible given absence of torch in `pyproject.toml`.
- "Works fully offline once model weights are cached" — depends on fastembed runtime behavior; not contradicted by code but not directly asserted in this repo.
- "First call pays a model-download and ONNX-load cost" — consistent with the lazy-init pattern but the actual network behavior is fastembed-internal.
- "ONNX provider availability depends on the user's wheel and platform" — true in general for `onnxruntime`; not enforced by this repo's code.
- Date "2026-05-20" matches the current date in environment context; no historical record to verify against.
- Status "Accepted" — no machine-checkable ADR registry; conventionally fine.

Cosmetic (not an ADR inaccuracy, but adjacent): `archon-search.toml.example:23` comments the embedding model as "Sentence-transformers embedding model" while the runtime uses fastembed. This is a config-file comment issue, not an ADR error, but worth noting since the ADR cites that file.

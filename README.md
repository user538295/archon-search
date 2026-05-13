# archon-search

Archon Search — standalone hybrid retrieval and routing server extracted from Archon (FEAT-038).

The package provides a FastAPI-based REST/MCP control plane over a LanceDB vector store, fastembed embeddings, a cross-encoder reranker, and a multi-collection router. It runs as its own process and is consumed by Archon via HTTP through `archon/ai/search_client.py`.

## Quick start

Install package dependencies (including dev/eval extras):

```bash
cd packages/archon-search
uv sync --dev
```

Run the server:

```bash
uv run archon-search
```

This invokes the entry point declared in `pyproject.toml` (`archon_search.cli.main:main`).

For end-user installation and operation in the Archon monorepo, see [`Documentation/UserManual/search_guide.md`](../../Documentation/UserManual/search_guide.md).

## Evaluation

`packages/archon-search/tests/eval/` hosts the FEAT-039 offline evaluation harness: a synthetic retrieval corpus, query/label fixtures, deterministic eval backends, committed thresholds, and a measured baseline. The harness is the sanctioned regression gate for retrieval, reranking, routing, and latency changes.

The authoritative maintenance guide — fixture schemas, threshold-lowering rationale policy, waiver workflow, and document-level metric semantics — lives at [`tests/eval/README.md`](tests/eval/README.md).

The maintained PR and release eval command is:

```bash
uv run pytest -m eval --thresholds-path tests/eval/thresholds.toml tests/eval/test_eval_suite.py
```

PR CI runs this command behind a path filter scoped to retrieval, reranking, routing, and the eval package (Task 4.5). The release-cut workflow runs the same gated slice before any package release mutation.

The harness uses **deterministic eval backends** that are corpus-aware but label-blind so retrieval and reranking metrics are stable across runs without pulling real model weights. Latency p50/p95 is captured as a **regression guard only** — the measured values reflect the deterministic backends and are not production SLAs.

Current measured baseline values (recall@k, MRR, nDCG@k, reranker lift, routing accuracy, latency percentiles) are recorded in [`tests/eval/baselines/baseline.md`](tests/eval/baselines/baseline.md) with the machine-readable companion in `tests/eval/baselines/baseline.json`.

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

## Privacy & Telemetry

Query telemetry is **opt-in and disabled by default**. When enabled, every `search`, `search_with_context`, and `POST /route` call appends one JSONL line to a daily file under `~/.archon/search-logs/`. No data is transmitted externally; all files remain on the local machine.

### Enabling

```toml
# ~/.archon/archon-search.toml
[telemetry]
enabled = true
retention_days = 30          # files older than this are deleted at startup and every 24 h
log_dir = "~/.archon/search-logs"
```

### What is logged

Each entry is a JSON object containing: `query_id` (random UUID), `timestamp` (UTC), `endpoint`, `latency_ms`, `status`, and endpoint-specific fields (`collection`, `result_count`, `result_doc_ids` for retrieval; `collections`, `decomposer_invoked` for routing). Error entries add `error_kind` (a closed set: `empty_query | slot_out_of_range | timeout | internal_error | validation_error | other`).

### What is never logged

**The raw query string is never recorded.** This is a structural guarantee: the factory methods that construct telemetry entries do not accept a `query` parameter. Exception messages are not logged either — only the coarse `error_kind` string enters the JSONL line.

### Path-derived `doc_id` risk

`result_doc_ids` are derived from the source file path on disk (e.g., `/Users/<name>/Documents/<project>/<file>.md`). When telemetry is enabled, these paths appear in the log files. Operators accept this when they opt in. A hashed-doc-id mode is planned for a future release (FEAT-039c). See [ADR 10](../../Documentation/ADRs/10_search_query_telemetry.md) for the full privacy contract and trade-off rationale.

### `export_enabled` is not available

`[telemetry].export_enabled = true` is rejected at config load time — external transmission of telemetry is reserved for FEAT-039c and is not implemented in v1.

---

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

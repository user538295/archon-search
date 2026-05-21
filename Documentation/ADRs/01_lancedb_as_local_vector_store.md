# 01. LanceDB as the Local Vector Store

**Status**: Accepted
**Date**: 2026-05-20
**Deciders**: archon-search maintainers

## Context

`archon-search` is a single-process, self-contained retrieval server that
persists all runtime state under `~/.archon-search/` (see
`archon-search.toml.example` → `[database] db_path`). The store must support:

- Dense vector ANN search over fastembed embeddings.
- Full-text search (FTS) over chunks, fused with vector hits via Reciprocal
  Rank Fusion (`_RRF_K = 60` in `archon_search/store.py`).
- Per-collection isolation, on-disk persistence, and zero external services so
  the tool can ship as a `pip install` package and run on a developer laptop.

The server is targeted at desktop and small-team deployments — there is no
multi-tenant cluster requirement. Adding a separate database service would
contradict the "single process, file-backed state" property described in
[Architecture / 100 — System Architecture Overview](../Architecture/100_system_architecture_overview.md).

## Decision

Use **LanceDB** as the vector + FTS store. Each collection is a LanceDB table;
`SearchStore` (`archon_search/store.py`) owns table creation, upsert, hybrid
search, and RRF fusion. No external database process is required; LanceDB
operates directly on the configured `db_path`.

## Consequences

### Positive
- Zero-deployment vector store — `pip install archon-search` is sufficient.
- Hybrid retrieval (vector + FTS) lives behind one storage API, simplifying
  the pipeline in `archon_search/pipeline.py`.
- File-backed tables are trivially backed up, copied, and inspected.
- Aligns with the engineering principle of minimum operational surface.

### Negative
- Not horizontally scalable; concurrent multi-writer access is not a target.
- LanceDB upgrades may require table-level migrations. Current migrations
  (`migrate_namespace`, `migrate_acl` in `archon_search/store.py`) are
  idempotent and run automatically on connect — there is no user-facing
  accept step — but future schema evolutions may need a more involved path.
- Performance characteristics on very large corpora (>10M chunks) are not
  validated by the eval harness, which uses a synthetic fixture corpus.

## Alternatives Considered

- **pgvector**: Rejected — requires a running PostgreSQL instance and
  per-deployment provisioning, breaking the single-binary install model.
- **Qdrant / Weaviate**: Rejected — both require a separate long-running
  service, container, or hosted plan; conflicts with the zero-service goal.
- **FAISS**: Rejected — pure ANN library with no built-in FTS, no managed
  persistence schema, and no hybrid retrieval primitives. Would push
  significant storage and indexing logic into `archon-search`.

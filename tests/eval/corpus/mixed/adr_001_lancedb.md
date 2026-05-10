# ADR-001: Use LanceDB as the Vector Store

**Status**: Accepted  
**Date**: 2026-01-10

## Context

We need a vector store to hold document chunk embeddings. Candidate stores: Pinecone, Weaviate, Qdrant, LanceDB.

## Decision

Use LanceDB (embedded mode) for the initial implementation.

```python
import lancedb

db = lancedb.connect("~/.archon/search.db")
table = db.create_table("docs", schema=schema)
table.add(rows)
results = table.search(query_vector).limit(20).to_list()
```

## Rationale

1. **No separate service**: LanceDB runs in-process. Zero operational overhead for self-hosted deployments.
2. **Lance columnar format**: Efficient for high-dimensional vectors; supports versioned tables.
3. **Python-native**: First-class async API; no gRPC dependency.
4. **Open source**: Apache 2.0 license; no vendor lock-in.

## Consequences

- Cannot be shared across multiple server processes without external coordination.
- HNSW index must be rebuilt after large batch ingestions.
- Upgrading LanceDB may require re-encoding existing tables if the on-disk format changes.

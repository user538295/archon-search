# ADR-003: Default Embedding Model Selection

**Status**: Accepted  
**Date**: 2026-01-20

## Context

The embedding model determines retrieval quality. Key trade-offs: model size, inference speed, multilingual support, licence.

Candidates evaluated on BEIR benchmark (NDCG@10):

| Model | NDCG@10 | Dim | Licence |
|-------|---------|-----|---------|
| `all-MiniLM-L6-v2` | 0.412 | 384 | Apache 2.0 |
| `bge-small-en-v1.5` | 0.438 | 384 | MIT |
| `bge-large-en-v1.5` | 0.483 | 1024 | MIT |
| `e5-mistral-7b` | 0.567 | 4096 | Apache 2.0 |

## Decision

Default to `bge-small-en-v1.5` (384 dims, MIT, fast, good quality). Allow override per collection via `embedding_model` config key.

```toml
[collections.code]
embedding_model = "bge-small-en-v1.5"

[collections.docs]
embedding_model = "bge-large-en-v1.5"
```

## Consequences

- Collections with different embedding models cannot be cross-searched without re-encoding.
- Upgrading the default model requires running `archon-search reindex`.

# ADR-004: Embedding-Based Routing as Default Strategy

**Status**: Accepted  
**Date**: 2026-02-05

## Context

`POST /route` must select the best collection for a user query. Three strategies are viable: keyword, embedding, LLM. We need a default that is accurate without adding per-request LLM cost.

## Decision

Default routing strategy: **embedding-based** (cosine similarity to per-collection centroid vectors).

Collection centroids are precomputed at index time and stored in memory. At query time, routing adds ~2ms overhead (one embedding + N cosine similarities, where N = number of collections).

```python
def route(query_vector, collection_centroids):
    sims = {
        name: cosine(query_vector, centroid)
        for name, centroid in collection_centroids.items()
    }
    return max(sims, key=sims.get)
```

## Alternatives Considered

- **Keyword routing**: Fast but brittle — fails on paraphrase and ambiguous terms.
- **LLM routing**: Most accurate but adds 200–500ms and token cost per query.

## Consequences

- Centroids must be updated when documents are added to a collection (`POST /collections/{name}/reindex`).
- Accuracy depends on the quality and diversity of documents in each collection — a collection with only one document has a poor centroid.
- Users can override by passing `collection` directly to `POST /search`, bypassing routing.

# Performance Tuning

## Embedding Throughput

Batch embedding requests: send 32 texts per call instead of one at a time. For a 512-dimensional model this gives ~10x throughput improvement.

Enable ONNX Runtime when available—it is 3-5x faster than PyTorch inference for CPU deployments.

## Database Query Optimization

Use a covering index on `(collection_id, created_at DESC)` for paginated listing queries. Add partial indexes for active-only document queries:

```sql
CREATE INDEX idx_docs_active ON documents (collection_id, created_at)
WHERE deleted_at IS NULL;
```

## Vector Search

Keep `top_k` low at the ANN stage (default: 20) and re-rank with a cross-encoder to return the final 5. ANN recall degrades for very high `top_k` values on small corpora.

For collections under 10,000 chunks, disable approximate nearest-neighbour and use exact search—latency difference is negligible.

## Caching

Cache embedding vectors for repeated queries. An LRU cache of 1,024 entries typically achieves 40-60% hit rates in interactive workloads. Cache key: SHA256(text + model_name).

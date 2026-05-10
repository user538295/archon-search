# SearchResult Schema

## Public Contract

```python
@dataclass
class SearchResult:
    doc_id: str        # stable document identifier from the manifest
    chunk_id: str      # unique chunk identifier (doc_id + chunk index)
    text: str          # chunk text returned in the result
    score: float       # relevance score in [0, 1]
    source_path: str   # relative path to the source file
```

## Stability Guarantees

- `doc_id` is stable across re-indexing operations.
- `chunk_id` format: `{doc_id}::{chunk_index}` (zero-padded to 4 digits).
- `score` is always in [0.0, 1.0]; higher is more relevant.
- `source_path` is relative to the corpus root directory.

## Example

```json
{
  "doc_id": "code-001",
  "chunk_id": "code-001::0000",
  "text": "class AsyncHttpClient:\n    ...",
  "score": 0.94,
  "source_path": "code/async_http_client.py"
}
```

## Deduplication

When multiple chunks from the same document are returned, the evaluation harness deduplicates by `doc_id` before computing NDCG and Recall metrics. This means a query's recall is capped at 1.0 per document regardless of how many chunks matched.

# Troubleshooting

## Service Won't Start

**Symptom**: `uvicorn` exits immediately with code 1.

1. Check `config.toml` exists and is valid TOML: `python -c "import tomllib; tomllib.load(open('config.toml','rb'))"`
2. Verify the database is reachable: `python -m archon_search doctor`
3. Check for port conflicts: `lsof -i :8080`

## Search Returns Empty Results

**Symptom**: `/search` returns an empty `results` array even for queries that should match.

1. Confirm the collection is not empty: `GET /collections` and check `doc_count`.
2. Run `archon-search reindex` to rebuild the vector index if it was corrupted.
3. Lower the relevance threshold if one is configured.

## High Latency (>2s per Query)

1. Enable ONNX Runtime: set `embedding.backend = "onnx"` in config.
2. Reduce ANN candidate count: `search.ann_candidates = 20`.
3. Profile with `ARCHON_SEARCH_PROFILE=1` environment variable to identify bottlenecks.

## Memory Usage Keeps Growing

Likely cause: embedding cache is unbounded. Set `cache.max_entries` in config.

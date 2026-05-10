# Changelog

## [2.3.0] - 2026-03-15
### Added
- Cross-encoder reranking: results are now re-scored with a lightweight cross-encoder before returning, improving precision by ~15%.
- `GET /collections/{name}/stats` endpoint returning document count, chunk count, and index freshness.
- `routing_enabled` flag in collection config to opt in to multi-collection routing.

### Changed
- Default embedding model upgraded from `all-MiniLM-L6-v2` to `bge-small-en-v1.5`. Re-index existing collections with `archon-search reindex`.
- `top_k` now refers to final returned results after reranking, not ANN candidate count.

### Fixed
- Fixed a race condition in batch ingest where concurrent writes could corrupt the chunk index.

## [2.2.1] - 2026-02-28
### Fixed
- Resolved OOM issue when ingesting documents larger than 100 MB by switching to streaming chunking.

## [2.2.0] - 2026-02-01
### Added
- Multi-collection routing via `POST /route`. Query is dispatched to the most relevant collection automatically.
- Watchdog-based hot-reload for configuration changes without service restart.

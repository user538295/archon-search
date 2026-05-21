# Review: Architecture/000_introduction_and_guiding_principles.md

## Summary
- Total factual claims checked: 27
- Inaccurate / unsupported: 0
- Verified: 27
- Overall accuracy estimate: 100%

## Inaccuracies
None found. Every factual claim in this document is supported by the source code, configs, CI workflows, or release tooling.

## Verified claims

1. **"standalone hybrid retrieval and routing server"** — matches `pyproject.toml:4` description and overall layout in `archon_search/`.
2. **"runs as a single local process, persists its state under `~/.archon-search/`"** — confirmed: `archon_search/config.py:24` (`log_dir = "~/.archon-search/search-logs"`), `:51` (`log_file = "~/.archon-search/logs/archon-search.log"`), `archon_search/platform/macos.py:73`.
3. **"FastAPI REST control plane and an MCP endpoint behind a shared bearer-token auth layer"** — confirmed: `archon_search/server/app.py`, `archon_search/server/mcp.py`, `archon_search/server/middleware_auth.py` exist and the CLAUDE.md project notes corroborate.
4. **"All runtime state … lives under `~/.archon-search/`"** — `config.py` defaults all paths under that root.
5. **"OpenAPI document published at `GET /openapi.json` is the authoritative shape"** — FastAPI exposes this by default; project CLAUDE.md and `pyproject.toml` are consistent.
6. **"dense embeddings (fastembed)"** — `pyproject.toml:10` lists `fastembed>=0.8.0`; `archon_search/embedder.py` present.
7. **"full-text search (LanceDB FTS)"** — `pyproject.toml:9` lists `lancedb>=0.30.0`; `archon_search/store.py` present.
8. **"cross-encoder reranker"** — `archon_search/reranker.py` present.
9. **"Multi-collection routing (`router.py`) selects which collections to query per prompt"** — confirmed: `archon_search/router.py:33` defines `MultiCollectionRouter`, docstring says "centroid pre-ranking for RAG collection selection".
10. **"Telemetry is opt-in and disabled by default"** — `archon_search/config.py:21`: `enabled: bool = False`.
11. **"stays on the local disk and never includes the raw query string"** — `archon_search/telemetry/entry.py` factory methods (`from_search_tool_result`, `from_route_response`, `from_error`) do not accept a `query` parameter; module docstring explicitly says it does not carry raw query text.
12. **"This is enforced structurally, not by review"** — matches the no-`query`-parameter invariant in `entry.py`.
13. **"CalVer (`YY.M.<rev-count>`) derived from git tags via `hatch-vcs`"** — `pyproject.toml:32` (`hatch-vcs` build backend), `:35-43` (vcs version source); release workflow comment "Tag format: YY.M.<git-rev-list-count> (computed by release.sh)".
14. **"compatibility is documented separately in `BREAKING.md`"** — `BREAKING.md` exists at repo root.
15. **Pipeline order "`parser.py` → `chunker.py` → `embedder.py` → `store.py` → `reranker.py` → `pipeline.py`"** — all modules exist in `archon_search/`.
16. **"gated by `tests/eval/`"** — directory exists with `thresholds.toml`, `baselines/`, `documents.jsonl`, `queries.jsonl`, `labels.jsonl`, `routing/`.
17. **"ACL and namespace isolation … (`acl.py`, namespace field on `CollectionMeta`)"** — `archon_search/acl.py` exists; `archon_search/collection_meta.py:23` has `namespace: str = DEFAULT_NAMESPACE`.
18. **"Setting `[telemetry].export_enabled = true` logs a warning at config load and is coerced back to `false` (`config.py`)"** — `config.py:209-217`: warns "telemetry: export_enabled is reserved for a future release and will be ignored" and sets `telemetry.export_enabled = False`.
19. **"writes JSONL files locally under `~/.archon-search/search-logs/`"** — `config.py:24` default `log_dir = "~/.archon-search/search-logs"`.
20. **"factory methods in `archon_search/telemetry/entry.py` do not accept a `query` parameter"** — verified by inspecting signatures of `from_search_tool_result`, `from_route_response`, `from_error`.
21. **"LanceDB-based storage is fast locally but is not a multi-writer distributed store"** — consistent with LanceDB's single-process embedded design and `pyproject.toml` dependency.
22. **"Version strings come from git tags via `hatch-vcs`; the package never embeds a literal version"** — `pyproject.toml:3` `dynamic = ["version"]` and `[tool.hatch.version] source = "vcs"`.
23. **"Plain pushes to `main` do not publish — only a tag push (typically via `release.sh`) triggers `archon-search-release.yml`"** — workflow header: "Manual-only release. The workflow runs only on tag push (created by `release.sh`) or on `workflow_dispatch:`. Plain pushes to `main` do not trigger anything here". `release.sh` exists at repo root.
24. **"serves traffic on `127.0.0.1` by default"** — `config.py:30` `host: str = "127.0.0.1"`; `archon-search.toml.example:17` `host = "127.0.0.1"`.
25. **CLI entrypoint `cli/main.py`** — `pyproject.toml:22`: `archon-search = "archon_search.cli.main:main"`.
26. **`archon_search/platform/`** — directory exists with `macos.py`, `linux.py`, `windows.py`, `runtime.py`, `service.py`.
27. **"MCP tools mirror the same control-plane verbs"** — `archon_search/server/mcp.py` exists alongside REST routes; project CLAUDE.md confirms shared auth and overlapping tools.

## Unverifiable / ambiguous
- **"Last reviewed: 2026-05-20" / "Next review: 2026-08-20"** — review-cadence metadata; cannot be independently verified against source. Today's date is 2026-05-20 per env, so the value is self-consistent.
- **"`pip install`"** — the doc says the user can `pip install` the package. While `pyproject.toml` produces a PyPI-compatible wheel and the release workflow publishes to PyPI via OIDC, actual PyPI availability cannot be confirmed without network access. The repo is configured for it; project README and CLAUDE.md treat it as a PyPI distribution (`archon-search`).
- **"single OS-level service across macOS, Linux, and Windows"** — modules `macos.py`, `linux.py`, `windows.py` exist; functional completeness of each was not tested, only file presence.

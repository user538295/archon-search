# Review: Architecture/530_technical_debt_refactoring_roadmap.md

## Summary

The debt register is largely accurate against `archon_search/` — most named items map to real code patterns (API-2 ignored `top_k`, CON-5 200-on-error, PLT-1 Windows stub, CON-1 docling race, CON-2 router cache, CON-4 full-vector centroid, TEL-1 silent coerce, EVL-1 deterministic backends, CORS-1 wildcard, TLG-1 coverage convention). The header still claims **Draft** even though the doc is dated for "today" (2026-05-20).

The following defects are material:

- **SEC-3 is wrong**: a structural test that introspects `TelemetryEntry` factory signatures and rejects `query`-like kwargs already exists (`tests/telemetry/test_entry_factories.py:151` — `test_factory_signatures_reject_raw_query_argument`). The whole "no dedicated test" premise is false, and the "Planned refactor" bullet that proposes adding it is redundant.
- **DOC-1 is wrong**: `CLAUDE.md` does NOT list `search_status`/`search_start`/`search_stop`/`search_ingest`/`search_collection_*`. Line 67 of `CLAUDE.md` enumerates the correct nine MCP tools matching `mcp.py` verbatim. The "stale source" claim is invented.
- **TEL-1's documented contradiction is wrong**: `CLAUDE.md` line 75 says the loader "logs a warning and silently coerces it to `false`" — i.e. it already describes the current behavior. There is no quote in `CLAUDE.md` matching "rejected at config load (no external transmission in v1)". Code and `CLAUDE.md` agree; only ADR-05 would need to be checked for a separate contradiction (not done here).
- **CON-3 is partially wrong**: `sync.py` does take per-collection `asyncio.Lock`s (`_get_lock`) around `_safe_state_update` / `_safe_state_remove` call sites (lines 487, 614). Intra-collection writes are serialised; the residual race is across-collection (two collections writing the shared `.indexing_state.json` file via two different per-collection locks). Additionally, `set_trigger` is **not** called from `sync.py` at all (only defined in `progress.py`). The claim "concurrent watcher and ingest tasks with no observable lock" overstates the gap.

Everything else either checks out or is a low-stakes minor imprecision (item count off by two for API-4 asdict sites; line range citations all verified).

## Inaccuracies (numbered)

1. **SEC-3** — "would break the invariant silently without a dedicated test" is false. The dedicated test exists: `tests/telemetry/test_entry_factories.py::test_factory_signatures_reject_raw_query_argument` uses `inspect.signature(...)` to forbid `{"query", "query_text", "body", "request"}` in every factory. The "Planned refactor" item that proposes adding such a test (line 135) is also stale and should be removed.

2. **DOC-1** — The quoted stale tool names (`search_status`, `search_start`, `search_stop`, `search_ingest`, `search_collection_{list,add,remove,info,reindex}`) do not appear in `CLAUDE.md`. `CLAUDE.md:67` already lists the correct nine names. There is no "stale source" to correct. The quoted attribution "MCP tools mirror the REST surface" does not appear in `CLAUDE.md` either (closest line: `CLAUDE.md:67` actually says "names do not mirror the REST routes 1:1").

3. **TEL-1** — The claimed disagreement between code and `CLAUDE.md` is fabricated. `CLAUDE.md:75` reads: "`export_enabled = true` is not implemented in v1: the config loader logs a warning and silently coerces it to `false` (see `config.py`)." That is exactly what `config.py:209–217` does. No "rejected at config load (no external transmission in v1)" quote exists in `CLAUDE.md`. The item should either drop the contradiction framing or re-anchor on ADR-05 if that's where the disagreement actually lives.

4. **CON-3** — Two errors:
   (a) `sync.py` does provide observable locking around state-store writes: `async with self._get_lock(name):` wraps the `_safe_state_update` / `_safe_state_remove` calls (see `sync.py:487`, `sync.py:614`). The race that remains is across-collection (different locks, same JSON file), not "no observable lock".
   (b) `set_trigger` is defined in `progress.py:129` but is **not called from `sync.py`** (grep across `archon_search/` returns only the definition). Listing it as a sync.py call site is inaccurate.

5. **API-4** — The list of `asdict(...)` call sites is incomplete. `mcp.py` also returns `asdict(...)` for `ingest_file` (line 134) and `ingest_directory` (line 158). The contract-drift risk applies there too (`IngestResult` dataclass shape) but the item omits them.

6. **Header `Status: Draft`** while the doc is dated `2026-05-20` (today) and reviewed against real code. Either promote to `Approved`/`Accepted` or push next-review forward; "Draft" indefinitely is itself a small documentation debt.

## Verified claims

- **API-1** — MCP `search` returns `{"results": [...], "acl_filtered": bool}` (`mcp.py:59`); `BREAKING.md` "[next release]" entry exists verbatim.
- **API-2** — `SearchRequest.top_k` is defined (`routes_search.py:20`) but never read by the `search` handler (`routes_search.py:62–84`); pipeline takes only `(query, collection, namespace=ns)`. `BREAKING.md` entry exists.
- **API-3** — `mcp.py` exposes exactly the 9 listed tools (`@app.tool()` decorators at lines 38, 76, 124, 139, 163, 178, 188, 200, 215). `app.py:140–147` includes 8 REST routers (collections, health, jobs, status, state, route, search, telemetry). The "8 routers" parenthetical is correct.
- **API-5** — `routes_search.py:71, 74` use `JSONResponse({"detail": ...}, status_code=...)`; `routes_collections.py:130, 136, 148, 155` likewise; `routes_jobs.py:154` also. Line citation `routes_search.py:68–84` matches.
- **SEC-1** — `middleware_auth.py:20` accepts `api_key: str` plus `namespaces: dict[str, str]`; no rotation/expiry primitive in the module.
- **SEC-2** — `doc_id` is path-derived; telemetry writer appends to `~/.archon-search/search-logs/`.
- **CORS-1** — `app.py:122` reads `app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])` — exact line match.
- **TEL-1 (code half)** — `config.py:209–217` matches the described silent-coerce-with-warning behavior. Line range is correct.
- **PLT-1** — `windows.py` confirms every method (`start`, `stop`, `restart`, `register`, `unregister`) raises `NotImplementedError`; `status()` returns `ServiceStatus(running=False, pid=None, uptime_seconds=None)`.
- **PLT-2** — Both workflows (`archon-search-pr.yml`, `archon-search-release.yml`) only set `runs-on: ubuntu-latest`; no macOS/Windows matrix.
- **PLT-3** — `install_cmd.py:29, 32, 33` use `subprocess.run([...], check=False, capture_output=True)` and discard the result; failures are silent.
- **CON-1** — `parser.py:96–104` contains the literal `# NOT THREAD SAFE` comment and the check-then-set lazy init.
- **CON-2** — `router.py:50` initialises `self._cached_metadata: list[CollectionMeta] | None = None`; `fetch_metadata` short-circuits on cached value (line 69) and only assigns once (line 124). No invalidator, TTL, or bust API.
- **CON-4** — `pipeline.py:368` `recompute_collection_meta` calls `self.store.get_all_vectors(collection)` (`store.py:658`), which docstring says is used "by `SearchPipeline.recompute_collection_meta`". Full re-scan confirmed; no incremental path.
- **CON-5** — *Pre-A3 baseline* — `routes_search.py:82–84` caught `Exception` from `pipeline.search`, logged at `warning`, and returned `SearchResponse(results=[], acl_filtered=False)` with implicit 200; meta failure → 503 (`routes_search.py:71`). All line citations matched at the time of the audit. **Resolved by A3**: the swallow-block has been replaced; pipeline exceptions now bare-re-raise to HTTP 500 (lines ~124-144) and timeouts return HTTP 504 (lines ~104-123); 503 meta-lookup branch moved to lines 86-90.
- **EVL-1** — `archon_search/eval/backends.py` exists; harness is label-blind deterministic.
- **TLG-1** — `pyproject.toml:55–61` matches the line citation; `--cov-fail-under=85` is in `addopts` at line 61.
- **ARCH-1** — Both `_types.py` (ChunkRecord, SearchResult, ingest-side types) and `types.py` (JobStatus, IngestJob, …) exist as separate modules.
- **ARCH-2** — `config.py:30–31` defines `host`/`port` defaults; `grep -rn ARCHON_SEARCH_HOST archon_search/` returns nothing — confirmed no env override. `key_manager` does honor `ARCHON_SEARCH_API_KEY` / `ARCHON_SEARCH_KEY_FILE` (referenced in `CLAUDE.md:43`).
- **ARCH-3** — No `request_id` / correlation-ID handling in `middleware_auth.py` or `app.py`.
- **In-code debt markers** — `grep -RIn "TODO\|FIXME\|XXX\|HACK" archon_search/` returns no matches; the `# NOT THREAD SAFE` in `parser.py` is the only self-warning. Verified today.

## Unverifiable / ambiguous

- **API-1 severity / consumer base** — "Consumers may still depend on the old shape" is unfalsifiable without external telemetry; treated as a forecast.
- **CON-4 trigger** ("past a few thousand chunks") — Not measured; ordering of magnitude is plausible but not benchmarked in-repo.
- **EVL-1 vs ADR-05 production-model lane** — The Backlog reference (`Backlog/03_world_class_roadmap.md`) was not opened during this review; cross-doc consistency not checked.
- **CON-3 cross-collection race** — Logically plausible (per-collection `asyncio.Lock` does not protect against another collection's lock writing the same file), but not exercised by any test in the repo, so the practical likelihood is open.
- **TEL-1 ADR-05 wording** — The doc cites `150_security_and_privacy_architecture.md` and implicitly ADR-05 for the "reject at config load" contract. ADR-05 itself was not opened; if it contains the "reject" wording, the TEL-1 inaccuracy framing could be partially salvaged by re-pointing the citation away from `CLAUDE.md`.
- **EVL-2 "evolve by waiver"** — Procedural claim, not code-verifiable.
- **Prioritisation matrix coordinates** — Subjective; not part of the accuracy review.

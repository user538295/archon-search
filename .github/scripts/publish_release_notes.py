#!/usr/bin/env python3
"""Publishes high-quality release notes for all archon-search tags."""

import json
import os
import sys
import urllib.request
import urllib.error

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("REPO", "user538295/archon-search")
API = f"https://api.github.com/repos/{REPO}/releases"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/vnd.github+json",
}

RELEASES = {
    "26.5.0": {
        "name": "26.5.0",
        "body": """\
**Initial standalone release: full hybrid retrieval server extracted from the Archon monorepo**

- **Standalone extraction (FEAT-046):** migrated all runtime state from `~/.archon/` to `~/.archon-search/`; added `ARCHON_SEARCH_CONFIG` env var override for config path; switched to self-contained test stubs (removed `_search_stubs_shim.py`); configured dynamic CalVer via `hatch-vcs` (`YY.M.<rev-count>` scheme); `python -m archon_search` entry point
- **MCP auth parity (FEAT-045):** added `APIKeyMiddleware` to the FastMCP HTTP transport so MCP and REST share the same Bearer token requirement; all MCP error shapes standardized via `McpErrorResponse` TypedDict; MCP `search` tool response aligned to the `SearchResponse` envelope; OpenAPI snapshot test added with `--update` flag; `BREAKING.md` with CalVer compatibility policy
- **Document-level ACL (FEAT-044):** `acl` column added to LanceDB chunk schema; sidecar `.acl` files parsed at ingest via `read_acl_sidecar`/`resolve_acl`; `is_acl_allowed` filter applied in `SearchPipeline.search` and `search_with_context`; `acl_protected_count`/`acl_open_count` on `GET /collections/{name}`; startup `migrate_acl()` for existing tables; `deny-all` reserved as a forbidden namespace
- **Multi-namespace isolation (FEAT-043):** all routes (`/search`, `/route`, `/ingest`, `/jobs/{id}`, `/indexing-state`, `/status`, `/collections`) filter and enforce namespace scope; per-namespace collection name uniqueness; two-namespace isolation integration tests
- **Full REST API schema coverage:** shared `schemas.py` with Pydantic `response_model` on every endpoint; `SearchResponse` envelope; always-on CORS middleware; BearerAuth OpenAPI security annotation; `/docs`, `/openapi.json`, `/redoc` exempted from auth
- **Retrieval pipeline:** `SearchPipeline` + `SearchStore` (LanceDB + FTS hybrid with RRF), `Embedder`/`Reranker` with injectable backends (fastembed dense + cross-encoder second stage), `DocumentChunker` (Chonkie recursive), `DocumentParser` multi-format file parsing, `MultiCollectionRouter` with centroid pre-ranking, `CollectionMeta` + `DescriptionGenerator` (Haiku auto-description), watchdog-driven sync/watcher, async job store for long-running ingest, platform service install/uninstall (macOS launchd, Linux systemd, Windows)
- **Key management:** `key_manager.py` auto-generates `~/.archon-search/.search.env` (mode 600) on first start; `ARCHON_SEARCH_API_KEY` env override; `ARCHON_SEARCH_KEY_FILE` redirect
- **MCP tools (10):** `search`, `search_with_context`, `explain`, `ingest_file`, `ingest_directory`, `list_collections`, `get_collections_meta`, `get_collection_meta`, `list_documents`, `delete_document` — sharing the same auth layer as REST
- **Opt-in telemetry:** JSONL per-call logging to `~/.archon-search/search-logs/`; no raw query strings logged (structural invariant); `/telemetry/stats` and `/telemetry/entries` endpoints; configurable `retention_days`
- **Evaluation harness:** deterministic corpus-aware eval suite with `documents.jsonl`, `queries.jsonl`, `labels.jsonl`; latency p50/p95 regression guards; `thresholds.toml` + baseline JSON/Markdown""",
    },
    "26.5.333": {
        "name": "26.5.333",
        "body": """\
**Automated CI/CD: PyPI OIDC publish, PR gate, and eval gate**

- Replaced the manual eval-only `release.yml` with a full automated publish workflow — every push to `main` runs the eval gate (`test` job), and on success the `publish` job computes a `YY.M.<git-rev-list-count>` CalVer tag, pushes it, builds the wheel via `hatch build`, and publishes to PyPI via OIDC trusted publisher (no stored secrets needed)
- Added `archon-search-pr.yml` PR gate — runs the default test suite + coverage check on every pull request
- Release runs serialized via `concurrency: group: release, cancel-in-progress: false`; tag-check step makes publish runs idempotent — re-running on a SHA whose tag already exists is a no-op
- Fixed `coverage combine` failure in CI: both test steps wrote to a single `.coverage` file via `--cov-append`, so `coverage combine` had no shards to merge and exited 1; CI now reports directly on the single file
- Wheel version verification step added before PyPI upload to catch tag/wheel version mismatches before they reach the index
- Monorepo-only stub comparison guard in `tests/test_search_stubs_copy.py` now skips cleanly when the monorepo root stubs file is absent (standalone repo)""",
    },
    "26.5.645": {
        "name": "26.5.645",
        "body": """\
**A-C series: search hardening, observability, multi-collection routing, live eval, structured logging, tiered install, git-cliff CHANGELOG**

**Search hardening (A1-A7)**
- A1: `SearchResult` gains 5 metadata fields (`indexed_at`, `doc_id`, `language`, `ingested_by`, `custom_score`); per-collection ingest lock with `StoreBusyError` + 30s timeout; CLI `reindex-metadata` command with `--normalize-timestamps` backfill flag; `IngestedBy` Literal type and `X-Ingested-By` header normalizer; MCP `search_with_context` strips vector bytes from returned neighbor chunks
- A2: `SearchFilters` Pydantic model with validation and ISO-8601 date coercion; `hybrid_search` accepts filter kwargs; fnmatch glob post-RRF filter; `SearchRequest` embeds `SearchFilters`; MCP search tools accept filter kwargs; `FilterFlags` submodel for privacy-safe telemetry; `language` surfaced in `SearchResultSchema` and MCP response
- A3: pipeline exceptions now propagate as HTTP 500 instead of silent 200 with empty results; 504 timeout guard added to `/search`; telemetry coverage for `/search` error paths
- A4: `POST /explain` endpoint with `ExplainResponse` — per-candidate routing scores, rerank scores, stage timings, pipeline trace; `explain` MCP tool with identical functionality; router `rank_with_scores` refactor; `from_explain_result` telemetry factory
- A5: `validate_ingest_path` + `PathUnsafeError` wired into all ingest endpoints and MCP tools; `_where_eq`/`_where_in` SQL fragment helpers replace all f-string SQL in `store.py`; CI guard (`test_no_fstring_sql.py`) fails the build if f-string `.where()`/`.delete()`/`.count_rows(` reappears; `StoreBusyError` to 503 on ingest endpoints; MCP surfaces it as `code=store_busy`
- A6: `threading.RLock` added to `IndexingStateStore` (CON-3); `MultiCollectionRouter.invalidate()` + `initial_metadata` (CON-2); per-request router lifecycle pinned in FastAPI to prevent shared mutable state
- A7: durable-write helper with crash-injection tests; 5 persistent state files migrated to fsync-safe write pattern; telemetry persistent-fd with rotate-only fsync; `OSError` to 500 routing; CI lint gate blocks regression

**Observability and health (B1-B2)**
- B1: `StageRecorder`/`record_stage`/`bind_stage_recorder` + `ObservabilityConfig` / `[observability]` TOML section; pure-ASGI `RequestContextMiddleware` propagates `X-Request-ID`; `correlation_id` threaded into all telemetry entries; stage timings emitted as structured log records in `/search`, `/route`, MCP search tools, and ingest paths; `stage_timings_ms` added to `ExplainResponse`
- B2: `GET /ready` endpoint; `SearchStore.ping()` with TTL cache; `embedder_is_warm`/`reranker_is_warm` on `SearchPipeline`; `ReadinessResponse`/`ReadinessDetail`/`JobCounts` schemas; readiness sub-object added to `GET /status`

**Multi-collection fan-out (B3)**
- `SearchPipeline.search_many` fan-out with configurable concurrency; `collections` list accepted on `POST /search` and the MCP `search` tool; `collection` provenance field on every result; `excluded_collections` in response envelope; fanout count in telemetry; fan-out provenance in `/explain`

**Hybrid routing with description embedding (B4)**
- `description_embedding_json` column on `_meta_schema`; description embedded at ingest and in `recompute_collection_meta`; `routing_strategy` + `routing_description_weight` config knobs blend centroid and description similarity; `migrate_description_embedding` at startup; routing MRR/P@1 added to the eval harness

**Incremental centroid maintenance (B5)**
- `centroid_sum_json`, `mutations_since_recompute`, `needs_recompute` columns on `_meta_schema`; `_do_update_meta_on_add`/`_do_subtract_meta_on_delete` helpers maintain running sum on ingest/delete; vector-aware `delete_document` with lock; CI guard for `_do_*_unlocked` call safety; `centroid_incremental_enabled` defaults to `True`; `migrate_centroid_sum()` with per-column resumable guards runs at startup; `recompute_collection_meta` removed from watcher-sync hot path

**Live eval harness (B6)**
- `live_eval` pytest marker + `tests/live/` directory with autouse shadow fixtures; acceptance tests for scenarios 1, 3, 8, 9, 10 against a live backend; JSON + Markdown report artifacts; `MetricVerdict`/`LiveEvalReport`/`build_live_report()`; `load_live_thresholds()`; `archon-search-eval-live.yml` CI workflow

**Structured logging (B7)**
- `[logging]` TOML section: `format` (plain/json), `backup_count`, `file_path`; `configure_logging()` called first in `run_server()`; `CorrelationIdFilter` injects correlation IDs into all log records; `getLogger(__name__)` normalized across all modules with a CI guard that fails if any module uses a non-`__name__` logger name

**Tiered install profiles (C0)**
- `InstallProfile` registry with `english` (default) and `multilingual` profiles; `profile` + `multilingual` fields on `SearchConfig`; disk-space preflight (`_check_disk_space`), advisory PID-based install lock, model pre-warm with `threading.Timer` timeout (`_prewarm_models`), reinstall guard (`NeedsForceDeleteError`), 5-step rollback on `--force` reinstall; Jina CC-BY-NC-4.0 license gate shown before multilingual profile install; `install_cmd.py` consolidated to a thin Click shim

**git-cliff CHANGELOG (C1)**
- `cliff.toml` with CalVer pattern; `CHANGELOG.md` stub + `.gitattributes`; awk extraction tests; `release.sh` gains git-cliff >= 2.4 preflight check, provisional tag computation with count verification, CHANGELOG shell-prepend + commit + push in one step; `--dry-run` shows tag, cliff notes, and full `curl` preview; `github-release` CI job added to `archon-search-release.yml`""",
    },
    "26.5.647": {
        "name": "26.5.647",
        "existing_id": 332132881,
        "body": """\
**Fix: fractional seconds rejected in captured_at timestamp regex**

- Fixed a regex in the test suite that matched `captured_at` ISO-8601 timestamps — the pattern only accepted whole-second variants (e.g. `2026-05-27T12:34:56Z`) and rejected fractional-second timestamps (e.g. `2026-05-27T12:34:56.789Z`)
- Failures were intermittent, occurring only when the system clock produced sub-second precision at capture time, making the root cause hard to reproduce locally""",
    },
    "26.5.652": {
        "name": "26.5.652",
        "body": """\
**Release automation: backfill workflow + flat release note format**

- Added `workflow_dispatch` GitHub Actions workflow (`backfill-release-notes.yml`) to retroactively create or update release notes for all existing tags in one run
- Switched the per-release note format to a flat style: bold title line derived from the first `feat` commit subject, followed by plain bullet points — no section headings within the release body (matches archon-assistant style)""",
    },
    "26.5.654": {
        "name": "26.5.654",
        "existing_id": 332198797,
        "body": """\
**Fix: dynamic git-cliff download URL in CI**

- The `github-release` CI job used a static git-cliff download URL that hardcoded a specific version number — the URL silently broke whenever a new git-cliff release was published upstream
- Switched to a dynamic URL that resolves the latest git-cliff release version at download time via the GitHub releases API, so the CI job stays functional without manual URL updates after each git-cliff release""",
    },
}


def api_request(method, url, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def upsert(tag, release):
    payload = {
        "tag_name": tag,
        "name": release["name"],
        "body": release["body"],
        "draft": False,
        "prerelease": False,
    }
    existing_id = release.get("existing_id")
    if existing_id:
        print(f"  Updating {tag} (id={existing_id})...")
        result, status = api_request("PATCH", f"{API}/{existing_id}", payload)
    else:
        print(f"  Creating {tag}...")
        result, status = api_request("POST", API, payload)

    if "html_url" in result:
        print(f"    -> OK [{status}]: {result['html_url']}")
        return True
    else:
        print(f"    -> ERROR [{status}]: {result}")
        return False


def verify():
    result, _ = api_request("GET", f"{API}?per_page=50")
    if not isinstance(result, list):
        print(f"ERROR fetching releases: {result}")
        return False
    print(f"\nVerification — total releases: {len(result)}")
    all_ok = True
    for r in sorted(result, key=lambda x: x["tag_name"]):
        chars = len(r.get("body") or "")
        status = "OK" if chars > 200 else "SHORT"
        if chars <= 200:
            all_ok = False
        print(f"  [{status}] {r['tag_name']:12}  chars={chars:5}  {r['html_url']}")
    return all_ok


if __name__ == "__main__":
    print(f"Publishing release notes to {REPO}...")
    success = True
    for tag, release in RELEASES.items():
        if not upsert(tag, release):
            success = False

    ok = verify()
    if not ok or not success:
        print("\nFAILED: not all releases were published correctly")
        sys.exit(1)
    print("\nAll 6 releases published successfully.")

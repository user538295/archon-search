"""Repo-wide structural meta-guards completing S42 and S52 (BE-21, task 4.15).

Four S42 properties plus S52's BREAKING.md-to-index parity guard. These are the
repo-wide superset of BE-16's interim ``tests/``-scoped guards in
``tests/test_graph_engine_stub.py`` (deliberately left intact): this file owns the
whole-repository scan those docstrings defer to.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from tests.test_parser_ocr_memory import (
    _WORKFLOW_FILES,
    assert_marker_excluded_on_step_lines,
    find_marker_tests_missing_xdist_group,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# --- S42 property 1: engine-name absence over git-tracked files -----------------------

_ENGINE_PATTERN = re.compile(r"spacy|en_core_web_sm|_LABEL_TO_ENTITY_TYPE", re.IGNORECASE)

# The ONLY categories S42(1) permits to still name the removed engine. Historical or
# generated records that must not be rewritten to pass a lint, plus the guard files that
# must contain the search literal and the retrieval-corpus data prefix.
_HISTORICAL_PREFIXES = (
    "Documentation/Completed/",  # archived planning history
    "Documentation/Backlog/",    # planning history incl. this feature's plan + .tsp contracts
    "tests/eval/corpus/",        # Wikipedia-derived retrieval DATA, not code
)
# Permanent, defensive entries: append-only / generated records that must not be rewritten to
# pass a lint. Some may not name the engine today (e.g. ADR 12, the agent logs) — allowlisting a
# non-match is inert and harmless; the entry stands so a future append here can't trip the guard.
# This list is permanent (unlike _PENDING_CLEANUP, it is NOT expected to shrink).
_HISTORICAL_EXACT = frozenset({
    "BREAKING.md",              # append-only compatibility contract
    "CHANGELOG.md",             # generated release history
    "learnings.md",             # append-only agent log
    "learnings-archive.md",     # append-only agent log
    "Documentation/ADRs/12_local_prose_relations_enrichment_narrowed.md",  # ADR may name the engine
})
# Guard files that legitimately contain the search literal (this file + BE-16's interim
# guards + the frozen pre-removal throughput baseline).
_GUARD_FILES = frozenset({
    "tests/test_removed_engine_repo_guard.py",
    "tests/test_graph_engine_stub.py",
    "tests/eval/_graph_ner_throughput_baseline.py",
})

# Self-tightening TEMPORARY allowlist: live files a LATER task still owns. Each still
# names the engine today; the test below fails if one stops matching, forcing the owning
# task to strike it from this list. This set must shrink to empty by feature close-out.
# The count is pinned (below): it may only SHRINK — decrement the constant when striking an
# entry — so a later task cannot dodge the repo-wide guard by appending a new file here.
_EXPECTED_PENDING_CLEANUP = 18
_PENDING_CLEANUP = frozenset({
    'Documentation/Architecture/100_system_architecture_overview.md',  # T-14 (Documentation update)
    'Documentation/Architecture/110_component_catalog_and_layer_breakdown.md',  # T-14 (Documentation update)
    'Documentation/Architecture/130_data_architecture_and_persistence.md',  # T-14 (Documentation update)
    'Documentation/Architecture/530_technical_debt_refactoring_roadmap.md',  # T-14 (Documentation update)
    'Documentation/Architecture/600_api_reference_or_public_interface.md',  # T-14 (Documentation update)
    'Documentation/OperatorGuide/20_monitoring_and_alerts.md',  # T-14 (Documentation update)
    'Documentation/OperatorGuide/60_graph_operations.md',  # T-14 (Documentation update)
    'Documentation/OperatorGuide/80_capacity_and_performance.md',  # T-14 (Documentation update)
    'Documentation/OperatorGuide/90_incident_runbook.md',  # T-14 (Documentation update)
    'Documentation/UserManual/10_installation.md',  # T-14 (Documentation update)
    'Documentation/UserManual/140_running_with_docker.md',  # T-14 (Documentation update)
    'Documentation/UserManual/160_troubleshooting.md',  # T-14 (Documentation update)
    'Documentation/UserManual/20_wizard.md',  # T-14 (Documentation update)
    'Documentation/UserManual/30_configuration.md',  # T-14 (Documentation update)
    'Documentation/UserManual/65_graph_search.md',  # T-14 (Documentation update)
    'Documentation/UserManual/70_code_graph_and_impact.md',  # T-14 (Documentation update)
    'Documentation/docker-test-runner.md',  # T-14 (Documentation update)
    'archon-search.toml.example',  # T-14 (Documentation update)
})


def _tracked_files() -> list[str]:
    """Every git-tracked path (the .gitignore-aware scan S42(1) specifies; a bare
    filesystem walk would hit .venv/.../spacy on any machine that ran `uv sync --dev`).
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [p for p in out if p]


def _is_allowlisted(path: str) -> bool:
    return (
        path.startswith(_HISTORICAL_PREFIXES)
        or path in _HISTORICAL_EXACT
        or path in _GUARD_FILES
        or path in _PENDING_CLEANUP
    )


def _files_naming_engine(paths: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in paths:
        if _ENGINE_PATTERN.search(rel):
            hits.append(rel)
            continue
        path = _REPO_ROOT / rel
        # A tracked path may be absent (e.g. an owning task DELETED a _PENDING_CLEANUP file
        # rather than scrubbing the literal). Skip it: the self-tightening test then reports
        # "no longer names the engine" cleanly instead of raising FileNotFoundError.
        if not path.is_file():
            continue
        # Strip NULs then decode latin-1 (never raises): a UTF-16 file interleaves NULs
        # between ASCII bytes, so a plain UTF-8 read would let the engine name slip the scan.
        text = path.read_bytes().replace(b"\x00", b"").decode("latin-1")
        if _ENGINE_PATTERN.search(text):
            hits.append(rel)
    return hits


def test_no_untracked_reference_to_the_removed_engine_remains() -> None:
    """S42(1): no git-tracked file names ``spacy``/``en_core_web_sm``/
    ``_LABEL_TO_ENTITY_TYPE`` outside the pinned allowlist. `git ls-files`-scoped, never a
    filesystem walk (`.venv/` carries the real package on any dev machine).
    """
    # Liveness anchor: the scanner MUST detect the engine name in a file known to carry it,
    # so a silent scan regression (early exit, decode masking, path-scope shrink) cannot let
    # this guard pass vacuously. BREAKING.md is a permanent allowlisted historical record, so
    # this backstop outlives the self-tightening _PENDING_CLEANUP list once it empties.
    assert _files_naming_engine(["BREAKING.md"]) == ["BREAKING.md"], (
        "the engine-name scanner failed to detect the known reference in BREAKING.md — the "
        "scan is broken and any 'no offenders' result below is meaningless"
    )
    candidates = [p for p in _tracked_files() if not _is_allowlisted(p)]
    offenders = _files_naming_engine(candidates)
    assert not offenders, (
        "these git-tracked files still name the removed engine and are not on any "
        f"S42 allowlist category: {sorted(offenders)}"
    )


def test_pending_cleanup_allowlist_is_self_tightening() -> None:
    """The temporary ``_PENDING_CLEANUP`` list is a forcing function, not suppression:
    every entry must STILL name the engine today. When an owning task finishes its cleanup,
    its file stops matching and this test fails until the file is struck from the list —
    driving the list provably to empty by close-out.
    """
    assert len(_PENDING_CLEANUP) == _EXPECTED_PENDING_CLEANUP, (
        "_PENDING_CLEANUP changed size — it must only SHRINK as owning tasks clean up. "
        "Decrement _EXPECTED_PENDING_CLEANUP when striking an entry; never grow it to dodge "
        "the repo-wide engine-absence guard"
    )
    matched = set(_files_naming_engine(sorted(_PENDING_CLEANUP)))
    cleaned = sorted(_PENDING_CLEANUP - matched)
    assert not cleaned, (
        "these files no longer name the removed engine — their owning task must remove "
        f"them from _PENDING_CLEANUP so the allowlist keeps shrinking: {cleaned}"
    )


# --- S42 property 2: new-lane CI exclusion (mirrors the docling trio) ------------------

_GRAPH_MARKER_EXCLUSION = "not graph_real_artifact"


def test_pyproject_default_lane_excludes_graph_real_artifact() -> None:
    """pyproject.toml's own addopts `-m` filter must exclude `graph_real_artifact`."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fp:
        data = tomllib.load(fp)
    addopts: str = data["tool"]["pytest"]["ini_options"]["addopts"]
    assert _GRAPH_MARKER_EXCLUSION in addopts, (
        "pyproject.toml [tool.pytest.ini_options].addopts must contain "
        "'not graph_real_artifact' in its -m filter, or the heavyweight real-artifact "
        "lane runs in the default suite"
    )


def test_both_workflows_exclude_the_new_marker_on_the_same_filter_line() -> None:
    """S42(2): both CI workflows' unit and integration `uv run pytest` steps must carry
    `not graph_real_artifact` on the SAME `-m` line — `-o addopts=` wipes pyproject's own
    filter per invocation, so each step is the only thing excluding the lane in CI. Reuses
    the existing ``_STEP_FILTER_MARKERS`` tuple as the step LOCATOR (never extended — K11:
    appending a third exact string would only prove presence somewhere, not coexistence on
    the same line). Delegates to the shared helper that owns the locator (one implementation
    shared with the docling CI-exclusion guard).
    """
    assert_marker_excluded_on_step_lines(_REPO_ROOT, _WORKFLOW_FILES, _GRAPH_MARKER_EXCLUSION)


# --- GRAPH-4: the lane's own step must EXIST in both workflows -------------------------
#
# The three guards above all assert the marker is EXCLUDED somewhere. Every one of them
# passed while the release workflow ran the lane nowhere at all — that is precisely how
# GRAPH-4 happened. An absence assert needs a presence anchor, so this pair is the anchor.

_GRAPH_LANE_SELECTOR = "-m graph_real_artifact"
_GRAPH_CACHE_KEY_PREFIX = "key: graph-ner-"


def _lane_step_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if "uv run pytest" in line and _GRAPH_LANE_SELECTOR in line
    ]


def test_both_workflows_run_the_graph_real_artifact_lane() -> None:
    """Both CI workflows must actually RUN the lane, not merely exclude it elsewhere.

    `archon-search-release.yml` triggers on tag push, and `release.sh`'s pre-flight checks
    branch, tree cleanliness and origin sync — never CI status — so a tag can be cut from a
    commit that never met the PR gate. Without this guard, deleting the release workflow's
    lane step reopens GRAPH-4 (a publish with zero real-model coverage) and nothing fails.
    """
    for rel_path in _WORKFLOW_FILES:
        matches = _lane_step_lines((_REPO_ROOT / rel_path).read_text(encoding="utf-8"))
        assert matches, (
            f"{rel_path}: no `uv run pytest` step selects {_GRAPH_LANE_SELECTOR!r}. The lane "
            "is the only guard that drives the real GLiNER checkpoint and fails on a vacuous "
            "extraction — every workflow that gates a merge or a publish must run it."
        )


def test_graph_artifact_cache_key_is_identical_across_workflows() -> None:
    """The lane's `actions/cache` key must match in both workflows.

    The step block is duplicated per workflow (GitHub Actions has no cheaper sharing for
    four steps), so the one field that silently degrades on drift — a mismatched key means
    a guaranteed cache miss and a ~1.2 GB re-download per release — is pinned here.
    """
    keys = {
        rel_path: [
            line.strip()
            for line in (_REPO_ROOT / rel_path).read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(_GRAPH_CACHE_KEY_PREFIX)
        ]
        for rel_path in _WORKFLOW_FILES
    }
    for rel_path, found in keys.items():
        assert len(found) == 1, (
            f"{rel_path}: expected exactly one {_GRAPH_CACHE_KEY_PREFIX!r} line for the graph "
            f"NER artifact cache, found {len(found)} — this guard can no longer compare keys."
        )
    distinct = {found[0] for found in keys.values()}
    assert len(distinct) == 1, (
        "the graph NER artifact cache key differs between workflows, so one of them takes a "
        f"guaranteed cache miss and re-downloads ~1.2 GB every run: {keys}"
    )


# --- S42 property 3: no docling parse-pool reuse on the engine call path ---------------

_ENGINE_CALL_PATH = (
    "archon_search/prose_extraction_backend.py",
    "archon_search/graph_extractor.py",
)
# The docling parse-pool surface these modules must not reuse: the class itself and the
# `DocumentParser._docling_pool()` accessor that hands out the shared instance (parser.py:279).
_PARSE_POOL_TOKENS = ("ProcessPoolExecutor", "_docling_pool")


def _names_on_code_line(source: str, token: str) -> bool:
    """True iff ``token`` appears outside a ``#`` line-comment. P3/P4 are the ONLY structural
    proof of S48/S28, so they must be comment-blind-proof: a commented-out ``torch.set_num_threads``
    must not false-PASS the pin, and a comment naming ``ProcessPoolExecutor`` must not false-FAIL
    the absence check. Splitting at the first ``#`` is exact for these statement-level tokens.
    """
    return any(token in line.split("#", 1)[0] for line in source.splitlines())


def test_no_docling_parse_pool_import_on_engine_call_path() -> None:
    """S42(3)/S48: neither module on the graph engine's call path reuses docling's parse pool
    — neither the ``ProcessPoolExecutor`` class nor the ``_docling_pool()`` accessor that hands
    out the shared instance. The engine runs in-process off ``asyncio.to_thread``; this is where
    S48's otherwise-unfalsifiable absence claim is proven structurally (comment mentions of the
    parser convention do not count — the check is comment-aware).
    """
    offenders = [
        f"{rel} ({token})"
        for rel in _ENGINE_CALL_PATH
        for token in _PARSE_POOL_TOKENS
        if _names_on_code_line((_REPO_ROOT / rel).read_text(encoding="utf-8"), token)
    ]
    assert not offenders, (
        f"these engine-call-path modules reference the docling parse pool: {offenders}"
    )


# --- S42 property 4: torch thread-count pin -------------------------------------------

_PROSE_BACKEND = "archon_search/prose_extraction_backend.py"


def test_engine_pins_torch_thread_counts() -> None:
    """S42(4)/S28: the backend pins both torch thread counts to module constants rather
    than leaving them at torch's defaults.
    """
    src = (_REPO_ROOT / _PROSE_BACKEND).read_text(encoding="utf-8")
    for call in (
        "torch.set_num_threads(_TORCH_INTRA_OP_THREADS)",
        "torch.set_num_interop_threads(_TORCH_INTER_OP_THREADS)",
    ):
        assert _names_on_code_line(src, call), (
            f"{_PROSE_BACKEND} must pin torch threads via {call!r} on a live code line (not "
            "commented out), not leave them at torch defaults"
        )


# --- S52: BREAKING.md heading -> migration-guide index parity -------------------------

_BREAKING_MD = "BREAKING.md"
_INDEX_MD = "Documentation/MigrationGuide/03_breaking_changes_index.md"
_EXPECTED_PREEXISTING_UNINDEXED = 65

# The 65 `### ` headings that predate this feature and have no index row yet. S52 is a
# regression guard against FUTURE unindexed headings, not a backfill of this historical
# gap (owned separately). A heading leaving this list once indexed is enforced below.
_PREEXISTING_UNINDEXED_HEADINGS = frozenset({
    '[next release] — graph NER model unavailability no longer fails ingest; `provider_warnings` gains a graph-NER category (2026-08-19)',
    '[next release] — the startup collection sync is suppressed after an unclean, mid-ingest death; `StartupSyncResult` gains `"suppressed"` and `/ready` `checks.sync` gains `"warn"` (2026-08-19)',
    '[next release] — `GET /ready` returns 503 while eager model warm-up or the startup collection sync is pending (2026-08-14)',
    '[next release] — `EmbedderNotReadyError` now maps to a uniform, sanitized 503 across every route surface (2026-08-14)',
    '[next release] — `DELETE /collections/{name}` returns 503 while an ingest job is active (2026-08-07)',
    '[next release] — `[search].fanout_leg_trim` and `fanout_timeout_seconds` are now enforced on the server (2026-08-07)',
    '[next release] — `POST /explain` no longer 404s on a partially-unknown `collections` list (2026-08-07)',
    '[next release] — Removed inert `[routing].max_parallel_collections` config key (2026-07-29)',
    '[next release] — `ErrorDetail` gains optional `code` field; 503 meta-lookup responses now include `code` (2026-07-21)',
    '[next release] — New `reranker_providers` TOML field for CoreML split-provider support (2026-07-20)',
    '[next release] — Removed `--log-to-stderr` wizard flag (2026-07-18)',
    '[next release] — CSP120: `archon-search collection add`, `collection reindex`, and `sync` are now HTTP proxies (require server running)',
    '[next release] — DCS: `archon-search collection list` is now an HTTP proxy (requires server running)',
    '[next release] — FE-8: `archon-search collection remove` is now an HTTP proxy (requires server running)',
    '[next release] — FE-5: `archon-search ingest` is now an HTTP proxy (requires server running)',
    '[next release] — G10: `HydeStatusDetail` and `RagFusionStatusDetail` gain `provider: str` field',
    '[next release] — E2j BE-1: `GraphNodeResponse` gains required `entity_type: str` field',
    '[next release] — E2h: `graph_mode` extended to `"ppr"`; `SearchResponse` and `ExplainResponse` gain `ppr_entities_matched` (all additive)',
    '[next release] — E2h BE-8: naive graph expansion is now capped at `naive_max_expansion_terms`',
    '[next release] — E2d: graph table names are now namespace-scoped (`_archon_graph_{ns}__{col}_*`)',
    '[next release] — E2c: `GET /graph/{collection}` and `GET /graph/cross-collection` gain `salience_mode` field and namespace-scoped resolution',
    '[next release] — E2a: TTL and scoping — additive chunk columns, new endpoints, scope_filter, maintenance fields',
    '[next release] — E2a BE-4: `PATCH /collections/{name}` `embedding_model` field is now optional',
    '[next release] — E1b: `graph_mode` extended to `"local"` and `"global"`; `StatusCollectionEntry` gains community stats (all additive)',
    '[next release] — E1a: graph tables, `SearchRequest.graph_mode`, `SearchResponse.graph_expansion_applied` (all additive)',
    '[next release] — E0e: `POST /search` multi-collection + filters now supported; `SearchResponse` gains `applied_filters`',
    '[next release] — E0d: `POST /ingest` gains 413 response; `IngestResult.code` field added; MCP `IngestResultSchema` gains `code` field',
    '[next release] — E0c: `top_k` OpenAPI schema change; 422 envelope change for fanout and top_k validation; additive `search` sub-object on `GET /status`; new `GET /collections/{name}/documents` endpoint',
    '[next release] — E0b: additive fields on SearchResponse, StatusResponse, StatsResponse; new FAILED_EXPIRED job status',
    '[next release] — E0b FE-2: `export --wait` and `backup --now --wait` exit codes changed; `--timeout` option added',
    '[next release] — E0b FE-1: `maintenance run --wait` timeout exit code changed 1 → 0',
    '[next release] — D7: new `/keys` REST endpoints and `key` CLI commands (additive)',
    '[next release] — D6: `CheckStatus` gains `PENDING` and `WARN`; `GET /ready` gains `checks.models`; `GET /status` gains `model_validation`',
    '[next release] — D4: `centroid_incremental_enabled` config field removed',
    '[next release] — D2 job contract: `JobResponse` gains bulk-job subclass fields',
    '[next release] — D2 status contract: `StatusResponse` will gain a `backup` object',
    '[next release] — D1/D2 MCP tools: `export_collection` and `import_collection` added (tool count 11 → 13)',
    '[next release] — D1 job contract: `progress` field on `JobResponse` and `QUEUED` status',
    '[next release] — C7 MCP Pydantic responses: field-narrowing on collection and context tools',
    '[next release] — C5 RAG Fusion: additive fields on MCP tool return dicts',
    '[next release] — C5 RAG Fusion: additive fields on ExplainResponse',
    '[next release] — C5 RAG Fusion: additive fields on SearchResponse',
    '[next release] — C4 HyDE query expansion: additive fields on SearchResponse and ExplainResponse',
    '[next release] — C4 HyDE query expansion: MCP `search_with_context` return type change',
    '[next release] — C2 language field type change (SearchResult, ScoredSearchCandidate, ExplainResult, ExplainNearMiss)',
    '[next release] — C1 per-collection embedding model (schema changes)',
    '[next release] — B5 incremental centroid maintenance (additive internal columns)',
    '[next release] — B4 hybrid collection routing',
    '[next release] — B3 multi-collection search',
    '[next release] — B2 (additive): `GET /ready` endpoint and `readiness` field on `GET /status`',
    '[next release] — B1 observability: `stage_timings_ms` on `POST /explain` and MCP `explain`',
    '[next release] — A2 query-side filters',
    '[next release] — A4 explain endpoint (purely additive)',
    '[next release] — A1 metadata schema v1',
    '[next release] — `POST /search` pipeline-exception behavior (CON-5 / A3)',
    '[next release] — A5a ingest path safety',
    '[next release] — D3 migration tooling: new REST endpoints and `STORE_SCHEMA_VERSION` policy',
    '[next release] — D5 maintenance jobs: `IngestJob` base class gains `source`, `source_path`, `collection`, `retry_count` fields (BE-7)',
    '[next release] — E0b BE-7: ingest job `result` contract changes from `null` to `{"warnings": [...]}`',
    '[next release] — D5 maintenance jobs: `GET /status` gains `maintenance` field (BE-4, BE-8)',
    '[next release] — D5 maintenance jobs: new `POST /maintenance/trigger` endpoint (BE-4)',
    '[next release] — D3 migration tooling: new nullable fields on `JobResponse` (BE-11)',
    '[next release] — E2f synonym edges: additive `GraphCollectionStats` fields and `GraphEdgeResponse.relationship_type`',
    "[next release] — S310: namespace key collections now stored under the caller's namespace (not `default`)",
    '[next release] — brief 270: `BackupTriggerResponse.queued` changed from `list[string]` to `list[QueuedBackupJob]`',
})


def _breaking_headings() -> list[str]:
    text = (_REPO_ROOT / _BREAKING_MD).read_text(encoding="utf-8")
    return [ln[4:].strip() for ln in text.splitlines() if ln.startswith("### ")]


def _index_row_text() -> str:
    """Only the index lines that link back to ``BREAKING.md`` are real entry rows. Anchoring
    the parity check to those lines stops a heading merely quoted in surrounding prose from
    counting as 'indexed' (a bare whole-file substring test would)."""
    index_text = (_REPO_ROOT / _INDEX_MD).read_text(encoding="utf-8")
    return "\n".join(ln for ln in index_text.splitlines() if "BREAKING.md" in ln)


def test_breaking_md_headings_have_index_rows() -> None:
    """S52: every `### ` heading in BREAKING.md has a matching row in the migration-guide
    index, except the pinned 65 pre-existing unindexed headings.
    """
    index_text = _index_row_text()
    headings = _breaking_headings()
    unindexed = [h for h in headings if h not in index_text]
    offenders = [h for h in unindexed if h not in _PREEXISTING_UNINDEXED_HEADINGS]
    assert not offenders, (
        "these BREAKING.md headings have no row in the migration-guide index and are not "
        f"on S52's pre-existing allowlist — add an index row: {offenders}"
    )


def test_s52_allowlist_is_pinned_and_self_tightening() -> None:
    """The 65-heading allowlist stays exactly 65, every entry is still a current heading,
    and none has since gained an index row (which would require striking it from the list).
    """
    assert len(_PREEXISTING_UNINDEXED_HEADINGS) == _EXPECTED_PREEXISTING_UNINDEXED
    current = set(_breaking_headings())
    stale = sorted(_PREEXISTING_UNINDEXED_HEADINGS - current)
    assert not stale, f"allowlisted headings no longer exist in BREAKING.md: {stale}"
    index_text = _index_row_text()
    now_indexed = sorted(h for h in _PREEXISTING_UNINDEXED_HEADINGS if h in index_text)
    assert not now_indexed, (
        "these headings are now indexed — strike them from _PREEXISTING_UNINDEXED_HEADINGS "
        f"so the allowlist keeps shrinking: {now_indexed}"
    )


def test_every_graph_real_artifact_test_is_pinned_to_its_xdist_group() -> None:
    """S42(3) mirror: every `@pytest.mark.graph_real_artifact` test must also carry
    `@pytest.mark.xdist_group("graph_real_artifact")`.

    `addopts` mandates `-n 8 --dist=loadgroup`, so an unpinned lane test lands on an
    arbitrary second xdist worker and loads a second ~1.2 GB GLiNER artifact concurrently
    with the pinned group — two full torch model stacks at once. This guard protects only
    the local parallel run; it is inert in CI, which passes `-n0`. Delegates to the shared
    scan (one implementation, two callers — the other is the `docling` lane's).
    """
    offenders = find_marker_tests_missing_xdist_group(_REPO_ROOT, "graph_real_artifact")
    assert not offenders, (
        "these @pytest.mark.graph_real_artifact tests are missing "
        '@pytest.mark.xdist_group("graph_real_artifact"), so under -n 8 --dist=loadgroup '
        f"they can load a second real ~1.2 GB model stack concurrently: {offenders}"
    )

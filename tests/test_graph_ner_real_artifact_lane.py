"""The `graph_real_artifact` lane — real GLiNER artifact, no stubs (Task T-8, S26).

This lane exists because every other graph test in the suite runs against a stubbed
engine, which certifies the fixture rather than the engine. Its two tests load the real
multi-hundred-MB artifact, so it is excluded from the default run by the
`graph_real_artifact` marker (`pyproject.toml` `addopts`, and both CI workflows' unit and
integration `-m` filters — BE-21) and runs as its own CI step in `archon-search-pr.yml`.

Run it explicitly — `ARCHON_SEARCH_DATA_DIR` must point at a data dir whose
`models/graph/` already holds the pinned checkpoint (see `_lane_data_dir` below for why):

    ARCHON_SEARCH_DATA_DIR="$HOME/.archon-search" uv run pytest -o addopts= \\
        --strict-markers --strict-config --no-cov -n0 \\
        -m graph_real_artifact tests/test_graph_ner_real_artifact_lane.py

**The two halves are deliberately two test functions, not one gated two ways** (K9,
team plan → Tester → Budget). `xfail` applies to a whole function, so a single marked
test holding both the numeric budget comparison *and* the non-vacuity asserts would make a
non-vacuity failure an expected failure too — silently green, which is exactly the defect
the split prevents. `test_graph_ner_lane_non_vacuity` carries no `xfail` marker and blocks
from day one; only `test_graph_ner_memory_budget`'s final numeric comparison is
non-blocking, because a laptop-derived budget enforced on a GitHub runner is either a
guaranteed flake or a guaranteed no-op until a real CI measurement replaces it. Everything
else inside that same function's BODY — the warm-up load, the host-safety RSS ceiling, and
the wall-clock cap — uses `pytest.exit()`, not `pytest.fail()`/`assert`: `xfail` catches any
regular exception the test function's CALL phase raises, host-safety abort included, so a
bare `pytest.fail()` there would be silently swallowed as an expected failure. `pytest.exit()`
raises pytest's session-level `Exit`, which `xfail` does not intercept. `xfail` also covers
the SETUP phase (fixture failures), which this split does not reach — a `graph_pipeline` or
`_lane_data_dir` fixture failure during this test's own setup would still report as XFAIL.
In this file's own CI step that phase is exercised first by `test_graph_ner_lane_non_vacuity`
(no `xfail` marker, and file-definition order runs it first), so a broken artifact still
fails loudly there; running the budget test in isolation loses that guarantee.

**Never `importorskip`.** A missing artifact or a missing `[graph]` extra must fail this
lane loudly: the whole point is to detect a silent no-op, and skipping on absence is how a
silent no-op stays invisible.

fastembed stays stubbed here (`tests/conftest.py` `install_stubs()`): only the GLiNER path
must be real, and a real embedder would add its own resident footprint to an RSS budget
that is meant to bind the graph engine alone.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.graph_types import RelationshipType, ChunkInput
from archon_search.paths import get_graph_models_dir
from archon_search.pipeline import create_pipeline

# gliner/torch are NOT imported here: this module is collected on every default run
# (the lane is marker-gated, not directory-gated) and importing them at module scope
# would pull ~1 GB of ML libraries into all eight xdist workers.

# --- corpus -----------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The committed reference corpus, already in the repo (team plan → Corpus, Q21).
_CORPUS = _REPO_ROOT / "tests" / "eval" / "corpus" / "docs"

# A paragraph shorter than this is mostly headings/list bullets, not extractable prose —
# excluded so both tests draw from genuinely entity-bearing text.
_MIN_PARAGRAPH_CHARS = 80

# --- budget constants -------------------------------------------------------------
#
# PROVENANCE — provisional, and note the units differ from the spike's. The Spike gate's
# PyTorch figures in the tasks file's T-8 Notes (resident 3172.9 MiB, peak RSS @ batch 8
# 3737.0 MiB) are ABSOLUTE resident sizes; the constant below is a steady-state GROWTH
# ceiling, so the spike does not hand it over directly — it informs the host-safety ceiling
# instead. The growth figure is set with headroom over a local measurement taken by this
# very test: 137.0 MiB of growth over 1,000 measured chunks (baseline 2831.4 MiB resident,
# which corroborates the spike's 3172.9 MiB resident figure), macOS arm64 dev machine,
# 2026-09-06, CPU only. A developer machine is not a GitHub runner, so this stays
# provisional: **the first green CI run of this lane replaces it** with a CI-measured
# figure in a follow-up commit (K9) — the run records `rss_growth_mib` into the CI step's
# --junitxml precisely so that figure is recoverable. Until then only the numeric
# comparison below is non-blocking.
#
# The budget binds the **CPU** configuration only. An accelerator (CUDA/MPS) is permitted
# but unmeasured — no scenario asserts a ceiling for it.
#
# A growth ceiling, not a literal no-trend assertion: CPython's arena reclaim is
# non-deterministic, so no-trend is not supportable (S26). The transient peak inside a
# single sub-batch forward pass is bounded by `GRAPH_NER_SUB_BATCH_SIZE`, not by this test.
_RSS_GROWTH_BUDGET_MIB = 600

# Host-safety ceiling: abort before hurting the machine, in the shape of
# `test_parser_ocr_memory.py`'s `_RSS_CEILING_MB`. This one IS informed by the spike's
# absolute peak (3737.0 MiB) plus the interpreter's own footprint, so it trips only on a
# genuine leak rather than on normal steady-state residency (~2.9 GiB observed).
_RSS_CEILING_MIB = 6000

# Wall-clock cap for the measured loop, mirroring `_RUN_BUDGET_S`. At the locally measured
# ~100 ms/chunk on CPU, 1,000 chunks is ~100 s; this leaves headroom for a slower runner
# without letting a pathological run hang the CI step.
_RUN_BUDGET_S = 900

# S26: "at least a thousand prose chunks ingested in one process" — both tests ingest
# the same corpus at this scale, not just the budget test (S26's non-vacuity clause reads
# "a separate, unmarked, always-blocking test that ingests the same corpus").
_MIN_CHUNKS = 1000

# Chunks per `inference()` call inside the budget test's measured loop. Independent of
# `GRAPH_NER_SUB_BATCH_SIZE` (which bounds the gliner-boundary forward pass); this only
# controls how often the loop samples RSS and checks its caps.
_SAMPLE_BATCH = 64

# Warm-up chunks driven through the engine before the RSS baseline is taken, so one-time
# lazy allocations (kernels, tokenizer caches, arena growth) land before the baseline
# rather than inside the measured window.
_WARMUP_CHUNKS = 16

# The typed, non-co-occurrence relationship types S26 names for this assert verbatim:
# "at least one typed (uses/implements/depends_on) edge exists". The prose engine's full
# relation vocabulary is wider (calls/imports/defines/inherits/related_to,
# prose_extraction_backend.py:83-90) — this set is deliberately the S26-specified subset,
# not the full vocabulary; `related_to` is excluded regardless since it is produced by the
# co-occurrence loop, and an assertion that accepted it would pass on co-occurrence alone.
_TYPED_RELATIONSHIPS = frozenset(
    {RelationshipType.uses, RelationshipType.implements, RelationshipType.depends_on}
)


def _read_own_rss_mib() -> float:
    """Current resident set size of THIS process, in MiB.

    Own-process, not the process-tree sampling `test_parser_ocr_memory.py` uses: the
    engine runs in-thread via `asyncio.to_thread`, so there is no child worker to sum.
    Current RSS, not `resource.getrusage().ru_maxrss` — that reports a high-water PEAK
    that never falls, which cannot express a steady-state growth ceiling (and its unit is
    platform-dependent: bytes on macOS, KiB on Linux). `ps` is used rather than psutil,
    which is only a transitive dependency (same reasoning as `test_parser_ocr_memory.py`).
    """
    rss_kib_text = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(rss_kib_text) / 1024


def _build_prose_chunks(count: int) -> list[ChunkInput]:
    """`count` prose chunks drawn from the committed corpus, cycling it as needed.

    `symbol_type=None` on every chunk: that is what routes them down the prose
    extraction path rather than the C3 code-symbol path (`graph_extractor.py`).

    Uses `pytest.exit()`, not `assert`, on a missing/empty corpus: this helper is called
    from both lane tests, including the `xfail`-marked budget test, where a plain `assert`
    would be silently absorbed as an expected failure rather than surfacing the real
    problem (a broken fixture, not an over-budget measurement).
    """
    paragraphs = [
        block.strip()
        for path in sorted(_CORPUS.rglob("*.md"))
        for block in path.read_text(encoding="utf-8").split("\n\n")
        if len(block.strip()) >= _MIN_PARAGRAPH_CHARS
    ]
    if not paragraphs:
        pytest.exit(
            f"graph_real_artifact lane: no prose paragraphs found under {_CORPUS} — "
            "corpus missing?",
            returncode=1,
        )
    return [
        ChunkInput(
            chunk_id=f"lane-{index:06d}",
            text=paragraphs[index % len(paragraphs)],
            symbol_type=None,
            symbol_subtype=None,
        )
        for index in range(count)
    ]


def _check_host_safety_or_exit(
    current_mib: float, baseline_mib: float, started: float, trace: list[float]
) -> None:
    """Abort the whole pytest session via `pytest.exit()` if own-process RSS has crossed
    the host-safety ceiling or the wall-clock cap has been exhausted.

    Shared by both lane tests: a 1,000-chunk real-artifact ingest is exactly as capable of
    exhausting a host's memory or hanging regardless of which test drives it, so both call
    this after every batch — not only the `xfail`-marked budget test, whose numeric
    comparison is the only thing meant to be soft. `pytest.exit()` bypasses `xfail`
    entirely (see the module docstring), so this is safe to call from either.
    """
    if current_mib >= _RSS_CEILING_MIB:
        pytest.exit(
            f"graph_real_artifact lane: own-process RSS {current_mib:.0f} MiB crossed "
            f"the {_RSS_CEILING_MIB} MiB host-safety ceiling (baseline {baseline_mib:.0f} "
            f"MiB). RSS trace: {trace}",
            returncode=1,
        )
    if time.monotonic() - started > _RUN_BUDGET_S:
        pytest.exit(
            f"graph_real_artifact lane: run budget {_RUN_BUDGET_S}s exhausted. "
            f"RSS trace: {trace}",
            returncode=1,
        )


# Captured at IMPORT time, before `tests/conftest.py`'s function-scoped autouse fixture
# redirects `ARCHON_SEARCH_DATA_DIR` at a throwaway per-worker directory (conftest.py:198).
# That redirect is right for the stubbed suite and wrong for this lane: it moves
# `get_graph_models_dir()` off the populated cache, so `GLiNER.from_pretrained` re-downloads
# the ~1.2 GB checkpoint on every run — which is also what would make the CI artifact-cache
# step pointless. `_lane_data_dir` puts the operator's value back for these two tests only.
_LANE_DATA_DIR = os.environ.get("ARCHON_SEARCH_DATA_DIR")


@pytest.fixture(scope="module")
def _lane_data_dir():
    """Restore the operator-provided `ARCHON_SEARCH_DATA_DIR` over the suite's isolation.

    Fails loudly rather than silently falling back: this lane exists to catch silent
    no-ops, and a run that quietly re-downloads (or fails to find) the artifact is exactly
    the kind of invisible outcome it must not produce. Checks that the checkpoint directory
    actually holds files, not merely that the env var is set — a set-but-empty data dir
    would otherwise pass this check and then silently trigger the ~1.2 GB re-download it
    exists to prevent.

    Module-scoped, so it manages the env var itself rather than through the function-scoped
    `monkeypatch` fixture (a module-scoped fixture cannot depend on a narrower-scoped one).
    """
    assert _LANE_DATA_DIR, (
        "ARCHON_SEARCH_DATA_DIR is unset. The graph_real_artifact lane needs a data dir "
        "whose models/graph/ already holds the pinned checkpoint — otherwise the suite's "
        "per-worker data-dir isolation sends GLiNER.from_pretrained to an empty cache and "
        'it re-downloads ~1.2 GB. Run with ARCHON_SEARCH_DATA_DIR="$HOME/.archon-search".'
    )
    previous = os.environ.get("ARCHON_SEARCH_DATA_DIR")
    os.environ["ARCHON_SEARCH_DATA_DIR"] = _LANE_DATA_DIR
    try:
        checkpoint_dir = get_graph_models_dir()
        assert checkpoint_dir.is_dir() and any(checkpoint_dir.iterdir()), (
            f"ARCHON_SEARCH_DATA_DIR={_LANE_DATA_DIR!r} is set, but {checkpoint_dir} is "
            "missing or empty — GLiNER.from_pretrained would silently re-download ~1.2 GB "
            "instead of using the cache this lane's CI step restores."
        )
        yield
    finally:
        if previous is None:
            os.environ.pop("ARCHON_SEARCH_DATA_DIR", None)
        else:
            os.environ["ARCHON_SEARCH_DATA_DIR"] = previous


@pytest.fixture(scope="module")
def graph_pipeline(_lane_data_dir, tmp_path_factory):
    """A real pipeline with the graph enabled, on a throwaway store.

    `graph.enabled=True` is what makes `create_pipeline` construct a `GraphExtractor`
    (and therefore a real `ProseExtractionBackend`); `graph.provider=None` keeps the
    optional LLM enrichment call out of the picture, so anything this lane observes came
    from the local engine.

    Module-scoped, deliberately: one shared backend for both tests in this file, not one
    each. Each `ProseExtractionBackend` instance loads its own full GLiNER stack (spike:
    ~3.2 GiB resident, ~3.7 GiB peak) — on a standard GitHub Actions runner (7 GiB total),
    two of those resident at once in the same process would risk a genuine OOM, not just a
    tighter host-safety margin. `load()` is idempotent (`prose_extraction_backend.py`), so
    sharing costs nothing: whichever test runs first pays the one real load, `load_count`
    stays `== 1` for the rest of the module, and the second test's own "warm-up" step below
    becomes a no-op reload rather than a genuine one — which only makes its baseline more
    settled, not less.
    """
    tmp_path = tmp_path_factory.mktemp("graph_real_artifact_db")
    config = SearchConfig(
        db_path=str(tmp_path / "db"),
        graph=GraphConfig(enabled=True, provider=None),
    )
    return create_pipeline(config)


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
@pytest.mark.asyncio
async def test_graph_ner_lane_non_vacuity(graph_pipeline) -> None:
    """S26's four blocking non-vacuity asserts — no `xfail` marker, gating merges from
    day one. Ingests the same 1,000-chunk corpus scale as the budget test below (S26:
    "a separate, unmarked, always-blocking test that ingests the same corpus"), batched
    through the same host-safety ceiling and wall-clock cap: this workload is exactly as
    capable of exhausting the host as the budget test's, so it carries the same guards.

    Each assert closes a distinct way the lane could pass while proving nothing:

    1. `degraded is False` — a degraded run skips prose extraction entirely and still
       returns `status == "ok"`, so ingest success alone is not evidence.
    2. entity node count > 0 — the engine can run and return nothing (the silent no-op).
    3. at least one **typed** edge — `related_to` co-occurrence edges appear whenever two
       entities share a chunk, so `edge_count >= 1` passes without any real relation
       extraction (Q28).
    4. `load_count == 1` — proves the load path actually executed. Deliberately not the
       resolved provider list, which is computable from config with no model loaded and
       so cannot discriminate a real load from a configuration read.
    """
    extractor = graph_pipeline._graph_extractor
    assert extractor is not None, "graph.enabled=True must construct a GraphExtractor"

    chunks = _build_prose_chunks(_MIN_CHUNKS)
    baseline_mib = _read_own_rss_mib()
    started = time.monotonic()
    trace: list[float] = []

    degraded = False
    warnings: list[str] = []
    nodes: list = []
    edges: list = []
    for offset in range(0, len(chunks), _SAMPLE_BATCH):
        batch = chunks[offset : offset + _SAMPLE_BATCH]
        batch_result = await extractor.extract(
            batch, f"lane-doc-{offset}", "graph_real_artifact_non_vacuity"
        )
        degraded = degraded or batch_result.degraded
        warnings.extend(batch_result.warnings)
        nodes.extend(batch_result.nodes)
        edges.extend(batch_result.edges)

        trace.append(round(_read_own_rss_mib(), 1))
        _check_host_safety_or_exit(trace[-1], baseline_mib, started, trace)

    # (1) Not degraded. Checked first and on its own: every assert below is meaningless
    # if prose extraction never ran, so this must not be folded into a compound condition.
    assert degraded is False, (
        "prose extraction degraded — the real artifact did not load or inference raised. "
        f"warnings={warnings}"
    )

    # (4) The load path actually executed. Read before the emptiness asserts so a failure
    # here is reported as "the model never loaded" rather than "the graph is empty".
    assert extractor.load_count == 1, (
        "expected exactly one real model construction for this process, got "
        f"{extractor.load_count} — the engine's load path did not execute once"
    )

    # (2) The engine produced prose entities. `nodes` here are prose nodes by
    # construction: every chunk above carries `symbol_type=None`, so the C3 code-symbol
    # path contributed nothing.
    assert len(nodes) > 0, (
        "the real engine returned zero entity nodes over the committed prose corpus — a "
        "silent no-op: extraction ran, did not degrade, and produced nothing"
    )

    # (3) At least one typed, non-co-occurrence edge. Split out from the count assert
    # above so a graph that has nodes but no real relations still fails loudly.
    typed_edges = [edge for edge in edges if edge.relationship_type in _TYPED_RELATIONSHIPS]
    assert typed_edges, (
        "no typed (uses/implements/depends_on) edge was extracted — only co-occurrence "
        f"`related_to` edges, which prove nothing about relation extraction. "
        f"edge types seen: {sorted({e.relationship_type.value for e in edges})}"
    )


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="provisional spike-derived budget; replaced by the first green CI measurement",
)
async def test_graph_ner_memory_budget(graph_pipeline, record_property) -> None:
    """S26's growth ceiling: steady-state own-process RSS over >= 1,000 prose chunks.

    `xfail(strict=False)` softens ONLY the final numeric comparison at the bottom of this
    function. Every other check in this function's body — the warm-up's own non-vacuity,
    the host-safety RSS ceiling, and the wall-clock cap — uses `pytest.exit()`, which raises
    pytest's session-level `Exit` rather than a normal exception, so `xfail` cannot absorb
    it: a real leak or a broken artifact must never be reported as an expected failure.
    `xfail` still covers this function's SETUP phase (fixture failures) — see the module
    docstring for why that gap is covered elsewhere in this file's own CI step.
    """
    extractor = graph_pipeline._graph_extractor
    if extractor is None:
        pytest.exit("graph.enabled=True must construct a GraphExtractor", returncode=1)

    chunks = _build_prose_chunks(_MIN_CHUNKS + _WARMUP_CHUNKS)

    # Warm-up, excluded from the baseline: loads the model and lets one-time allocations
    # settle, so the growth figure below is steady state rather than cold-load cost. A
    # no-op if the non-vacuity test above already ran first and loaded the shared backend
    # (`graph_pipeline` is module-scoped) — harmless, since `load()` is idempotent and this
    # step's real job is letting allocations settle, not proving a cold load succeeds.
    warmup = await extractor.extract(chunks[:_WARMUP_CHUNKS], "warmup-doc", "warmup_col")
    if warmup.degraded:
        pytest.exit(
            f"graph_real_artifact memory lane: warm-up degraded — the artifact did not "
            f"load. warnings={warmup.warnings}",
            returncode=1,
        )

    measured = chunks[_WARMUP_CHUNKS:]
    baseline_mib = _read_own_rss_mib()
    started = time.monotonic()
    trace: list[float] = []

    for offset in range(0, len(measured), _SAMPLE_BATCH):
        batch = measured[offset : offset + _SAMPLE_BATCH]
        batch_result = await extractor.extract(batch, f"budget-doc-{offset}", "budget_col")
        if batch_result.degraded:
            pytest.exit(
                f"graph_real_artifact memory lane: extraction degraded at chunk "
                f"{offset + len(batch)}/{len(measured)} — a mid-run failure would "
                f"otherwise silently understate the growth figure. "
                f"warnings={batch_result.warnings}",
                returncode=1,
            )

        trace.append(round(_read_own_rss_mib(), 1))
        _check_host_safety_or_exit(trace[-1], baseline_mib, started, trace)

    growth_mib = _read_own_rss_mib() - baseline_mib
    # Into the --junitxml the CI step already writes, so the figure that replaces the
    # provisional constant above (K9's second stage) is recoverable from a green CI run
    # rather than only from a failing one — captured stdout is not shown when a test passes.
    # NOTE: record_property emits a PytestWarning under the default junit_family=xunit2
    # ("incompatible with junit_family"); the <properties> block is still written and the
    # figure is still recoverable, so this is an accepted, expected warning, not a bug.
    record_property("rss_growth_mib", round(growth_mib, 1))
    record_property("rss_baseline_mib", round(baseline_mib, 1))
    record_property("measured_chunks", len(measured))

    assert growth_mib < _RSS_GROWTH_BUDGET_MIB, (
        f"graph NER steady-state RSS grew {growth_mib:.0f} MiB over {len(measured)} prose "
        f"chunks (baseline {baseline_mib:.0f} MiB), exceeding the {_RSS_GROWTH_BUDGET_MIB} "
        f"MiB CPU-configuration budget. RSS trace: {trace}"
    )

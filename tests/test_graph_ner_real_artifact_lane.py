"""The `graph_real_artifact` lane — real GLiNER artifact, no stubs (Task T-8, S26).

This lane exists because every other graph test in the suite runs against a stubbed
engine, which certifies the fixture rather than the engine. Its three tests load the real
multi-hundred-MB artifact, so it is excluded from the default run by the
`graph_real_artifact` marker (`pyproject.toml` `addopts`, and both CI workflows' unit and
integration `-m` filters — BE-21) and runs as its own CI step in both `archon-search-pr.yml`
and `archon-search-release.yml` (GRAPH-4 — a tag can be pushed from a commit that never
went through the PR gate, so the publish path needs its own copy of this guard).

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

`test_graph_ner_throughput_within_budget` (T-9, S27) is NOT marked either — its comparison
BLOCKS. `xfail(strict=False)` would make it unfailable in both directions (a pass reports
XPASS, a failure reports XFAIL), so marking it would deliver zero enforcement instead of a
soft signal. `REGRESSION_MULTIPLIER` is widened to absorb a slower CI runner instead, and
every check in that function's body is therefore a plain `assert`, including S27's own
non-vacuity gate (K13, which is why that gate can live inside the throughput function
rather than needing a fourth test). Only the shared `_check_host_safety_or_exit` still
calls `pytest.exit()` there — that is host safety, not the comparison.

**Never `importorskip`.** A missing artifact or a missing `[graph]` extra must fail this
lane loudly: the whole point is to detect a silent no-op, and skipping on absence is how a
silent no-op stays invisible.

fastembed stays stubbed here (`tests/conftest.py` `install_stubs()`): only the GLiNER path
must be real, and a real embedder would add its own resident footprint to an RSS budget
that is meant to bind the graph engine alone. That stubbing also removes real embedding
cost from `test_graph_ner_throughput_within_budget`'s timed window, which the pre-change
baseline it compares against DID pay — see the COMPARABILITY CAVEAT beside
`THROUGHPUT_BASELINE_MS`.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import statistics
import subprocess
import time
from pathlib import Path

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
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

# --- throughput constants (T-9, S27) ------------------------------------------------
#
# Moved here verbatim from the standalone `tests/eval/_graph_ner_throughput_baseline.py`,
# which T-2 captured before the old engine was deleted and which T-9 deletes now that this
# lane's test file exists to hold the figure (team plan → Corpus, Q21, K10). Both constants
# are named WITHOUT the leading underscore the rest of this file uses: S27 and the Tester
# Done-when checklist name `THROUGHPUT_BASELINE_MS` and `REGRESSION_MULTIPLIER` verbatim.
#
# PROVENANCE of the baseline — figure, corpus, machine, provider, date, as captured:
#   figure    1335.3 ms — the median of three local runs (1296.9 / 1335.3 / 1345.4 ms).
#             A DURATION, not a rate: LOWER IS BETTER, which is what makes the
#             `measured <= BASELINE * MULTIPLIER` direction below correct.
#   corpus    tests/eval/corpus/docs/ — 17 files, 18380 bytes, sha256[:16] 5b5717d98f2ac6e7,
#             ingested with the `**/*` glob. Pinned below and re-checked before timing.
#   machine   Darwin 25.5.0 arm64 (macOS 26.5.2) — a local dev machine, NOT a CI runner.
#             The standalone module recorded that gap honestly and said the figure "should
#             be re-captured on CI before being trusted as the S27 regression baseline";
#             that gap is exactly what `REGRESSION_MULTIPLIER` below is sized to absorb,
#             and what K9's CI re-capture closes.
#   provider  fastembed's own default provider selection (`SearchConfig.providers == []`),
#             graph enabled with `graph.provider=None` so no LLM enrichment call ran, and
#             ANTHROPIC_API_KEY unset so no description-generation call was timed.
#   date      2026-09-01, at commit b5b8ba6ca7a45457811d2f8bff7ce8b3e7ddf4fe.
#
# COMPARABILITY CAVEAT, stated plainly rather than papered over — and deliberately NOT
# "fixed" by adding a second assertion, which is exactly the defect M13 exists to prevent.
# The baseline timed the OUTGOING NER engine with the REAL fastembed embedder inside the
# timed window. This lane keeps fastembed STUBBED (`tests/conftest.py` → `install_stubs()`,
# module docstring above) and runs the real GLiNER artifact. The two figures therefore
# differ on two axes at once, pushing in opposite directions: embedding cost is now
# ~free (biasing the measurement DOWN) while a real transformer forward pass replaces a
# lightweight statistical tagger (biasing it heavily UP). REGRESSION_MULTIPLIER absorbs a
# whole engine swap, not machine noise — it is not a tight "no regression" bound and must
# not be read as one.
THROUGHPUT_BASELINE_MS = 1335.3

# Allowed regression over that baseline. Provisional in exactly the way
# `_RSS_GROWTH_BUDGET_MIB` above is, and set the same way: headroom over a local
# measurement taken by this very test, macOS arm64 dev machine, 2026-09-06, CPU only:
# 4002.8 ms as the whole lane runs it (median of 4000.9 / 4002.8 / 4097.0), 3858.2 ms with
# this test run alone — 3.0x the baseline, taking the in-lane figure. The run-to-run spread
# is ~1%, so the headroom below is not covering measurement noise; it is covering the
# machine gap. This comparison BLOCKS — it carries no `xfail` (module docstring) — so the
# multiplier must absorb a slower GitHub Actions runner rather than flake the build: 10.0
# is that 3.0x with roughly 3x headroom over it.
#
# Stated plainly rather than overclaimed: at 10.0 this catches CATASTROPHIC regressions —
# a lost batch, a per-chunk model reload, an accidental serialization of the forward pass —
# and NOT modest ones. A 2x or 3x slowdown passes this test today. That is the price of
# enforcing a laptop-derived figure on an unmeasured runner, and the fix is to tighten the
# number once a real runner has produced one, not to set a bound the runner cannot meet:
# **the first green CI run of this lane replaces it** with a CI-derived figure (K9); the
# run records `measured_ms` into the CI step's `--junitxml` precisely so that figure is
# recoverable from a green run rather than only from a failing one.
#
# MANUAL REFERENCE CHECK — NEVER ASSERTED (team plan → Corpus, Q21; M13). The tight
# Apple-Silicon reference is ten percent, i.e. `measured <= THROUGHPUT_BASELINE_MS * 1.10`.
# The local measurement above misses it by a wide margin, which is the engine swap and the
# stubbed embedder in the caveat above showing up in the number — read it that way, by eye,
# against the `measured_ms` property the test records. That figure lives here as a comment
# and nothing else: this lane holds exactly ONE enforced pass condition, and an earlier
# draft's second, undefined "loose ceiling" is the defect that rule was written to close.
# Never turn it into a second comparison.
REGRESSION_MULTIPLIER = 10.0

# Corpus identity, carried across from the standalone module. The comparison above is a
# comparison against a hardcoded historical constant, so it is only valid while its input
# is unchanged: a mutated corpus must invalidate it loudly rather than silently shift the
# measurement. Re-verified against the tree on 2026-09-06.
_INGEST_GLOB_PATTERN = "**/*"  # `ingest_directory`'s own default (archon_search/pipeline.py)
_BASELINE_CORPUS_FILE_COUNT = 17
_BASELINE_CORPUS_TOTAL_BYTES = 18380
_BASELINE_CORPUS_SHA256_16 = "5b5717d98f2ac6e7"

# Timed runs, mirroring the baseline's mechanism exactly (median of 3, one warmed pipeline
# reused across all runs, each run ingesting into its own fresh collection so none hits a
# doc-already-exists shortcut). A single noisy run compared against a median-of-3 constant
# would not be the same measurement.
_THROUGHPUT_RUNS = 3


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


def _corpus_identity() -> tuple[int, int, str]:
    """`(file_count, total_bytes, sha256[:16])` over the files `_INGEST_GLOB_PATTERN` matches.

    Byte-for-byte the computation the deleted standalone module used to derive the pins
    above, so the two are directly comparable.
    """
    digest = hashlib.sha256()
    total_bytes = 0
    files = sorted(p for p in _CORPUS.glob(_INGEST_GLOB_PATTERN) if p.is_file())
    for path in files:
        data = path.read_bytes()
        digest.update(data)
        total_bytes += len(data)
    return len(files), total_bytes, digest.hexdigest()[:16]


def _check_host_safety_or_exit(
    current_mib: float, baseline_mib: float, started: float, trace: list[float]
) -> None:
    """Abort the whole pytest session via `pytest.exit()` if own-process RSS has crossed
    the host-safety ceiling or the wall-clock cap has been exhausted.

    Shared by all three lane tests: a real-artifact ingest at this scale is exactly as
    capable of exhausting a host's memory or hanging regardless of which test drives it, so
    all three call this after every batch — not only the `xfail`-marked budget test, whose
    numeric comparison is the only thing meant to be soft. `pytest.exit()` bypasses `xfail`
    entirely (see the module docstring), so this is safe to call from any of them. It stays
    `pytest.exit()` in the unmarked tests too: host safety is a reason to abandon the whole
    session, not to fail one test and start the next one on a host already near its limit.
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


# Captured at IMPORT time, before `tests/conftest.py`'s autouse fixture can touch
# `ARCHON_SEARCH_DATA_DIR`. That fixture's per-worker redirect is right for the stubbed
# suite and wrong for this lane — being function-scoped it lands AFTER the module-scoped
# `_lane_data_dir` below, and `GLiNER.from_pretrained` reads `get_graph_models_dir()`
# lazily on first extract, so the redirect would move the load off the populated cache and
# re-download the ~1.2 GB checkpoint on every run, making the CI cache step pointless.
# `_archon_isolated_data_dir` therefore skips tests carrying the `graph_real_artifact`
# marker; `_lane_data_dir` supplies the operator's value, and the non-vacuity test asserts
# it actually survived into the test body.
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

    # The data dir must still be the operator's INSIDE the test body, not only during this
    # module's fixture setup: `GLiNER.from_pretrained` reads `get_graph_models_dir()` lazily
    # on first extract (prose_extraction_backend.py:272), so a function-scoped override that
    # lands after `_lane_data_dir` would silently re-download ~1.2 GB past the CI cache and
    # still pass every assert below. Checked here, not in the fixture, for that exact reason.
    assert str(get_graph_models_dir()).startswith(_LANE_DATA_DIR), (
        f"the lane is reading models from {get_graph_models_dir()}, outside the operator's "
        f"ARCHON_SEARCH_DATA_DIR={_LANE_DATA_DIR!r} — something re-redirected the data dir "
        "after `_lane_data_dir` ran, so this run bypasses the artifact cache entirely"
    )

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


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
@pytest.mark.asyncio
async def test_graph_ner_throughput_within_budget(graph_pipeline, record_property) -> None:
    """S27: total ingest wall time over the committed corpus, one comparison against the
    moved baseline — `measured <= THROUGHPUT_BASELINE_MS * REGRESSION_MULTIPLIER`.

    **This test blocks.** It carries no `xfail`: with `strict=False` a pass reports XPASS
    and a failure reports XFAIL, so marking it would leave the comparison unable to fail CI
    in either direction — zero enforcement rather than a soft signal. The baseline's own
    provisional nature (a developer-laptop figure captured against the outgoing engine with
    a real embedder — see the COMPARABILITY CAVEAT beside the constants) is absorbed by
    `REGRESSION_MULTIPLIER`'s width instead, which is the knob K9's CI re-capture tightens.

    Every check in this body is therefore a plain `assert`, including S27's non-vacuity
    gating (K13, which is why that gate can live inside this function rather than needing a
    fourth test): `status == "ok"` plus non-zero chunk, entity and relation counts gate the
    timing comparison, because a comparison against a degraded, short or empty run measures
    nothing. Only `_check_host_safety_or_exit` still aborts the session — host safety is
    shared with the other two tests and is not about this comparison.

    Mirrors the deleted standalone module's measurement mechanism, which is the only thing
    that makes the comparison meaningful at all: the same `create_pipeline` /
    `ingest_directory` production entrypoint, the same corpus and glob, warm-up outside the
    timed window, `time.perf_counter()` around the ingest, and the median of three runs.
    """
    file_count, total_bytes, sha16 = _corpus_identity()
    assert (file_count, total_bytes, sha16) == (
        _BASELINE_CORPUS_FILE_COUNT,
        _BASELINE_CORPUS_TOTAL_BYTES,
        _BASELINE_CORPUS_SHA256_16,
    ), (
        f"graph_real_artifact throughput lane: {_CORPUS} no longer matches the corpus "
        f"THROUGHPUT_BASELINE_MS was measured over — got {file_count} files / "
        f"{total_bytes} bytes / sha256[:16]={sha16}, pinned "
        f"{_BASELINE_CORPUS_FILE_COUNT} / {_BASELINE_CORPUS_TOTAL_BYTES} / "
        f"{_BASELINE_CORPUS_SHA256_16}. A comparison against a frozen historical "
        "figure is only valid while its input is unchanged; re-capture the baseline "
        "rather than re-pointing this test."
    )

    assert graph_pipeline._graph_extractor is not None, (
        "graph.enabled=True must construct a GraphExtractor"
    )
    graph_store = graph_pipeline._graph_store
    assert graph_store is not None, "graph.enabled=True must construct a GraphStore"

    # The `graph_pipeline` fixture builds the pipeline but connects neither store; the
    # standalone module connected both before timing, so this does too. These are the only
    # `connect()` calls in this module — deliberately, since `GraphStore.connect` rebinds
    # `self._db` without closing the previous handle (`graph_store.py`), so calling it twice
    # would leak a connection rather than being a harmless no-op.
    await graph_pipeline.store.connect()
    await graph_store.connect()

    # Warm-up, OUTSIDE the timed window — the baseline's rule, and the reason its figure is
    # steady-state throughput rather than one-time model load. Deliberately the SAME
    # operation the timed runs perform, over the same corpus and glob, into a throwaway
    # collection: parse, chunk and LanceDB table setup are inside the timed window too, so
    # warming only the model (as an `extract()` call would) still leaves those one-time
    # costs in the first measured run whenever this test runs alone.
    warmup_results = await graph_pipeline.ingest_directory(
        _CORPUS,
        "throughput_warmup_col",
        glob_pattern=_INGEST_GLOB_PATTERN,
        embedder=graph_pipeline._global_embedder,
    )
    warmup_nodes = await graph_store.node_count("throughput_warmup_col", DEFAULT_NAMESPACE)
    assert warmup_nodes > 0 and all(r.status == "ok" for r in warmup_results), (
        "graph_real_artifact throughput lane: the warm-up ingest produced no real "
        "extraction — the artifact did not load, so any timing below would measure a "
        f"skipped extraction. entity nodes={warmup_nodes}, results="
        f"{[(r.doc_id, r.status, r.error) for r in warmup_results]}"
    )

    # ANTHROPIC_API_KEY needs no guard of its own here: `tests/conftest.py` clears it both
    # session-wide (`_block_anthropic_key_at_session`) and per test
    # (`_archon_isolated_data_dir`, which does NOT skip that step for this lane), so
    # `generate_description()` short-circuits and no live LLM call lands in the timed
    # window. The standalone module asserted it only because it ran outside pytest.
    baseline_mib = _read_own_rss_mib()
    started = time.monotonic()
    trace: list[float] = []
    elapsed_ms: list[float] = []

    for run_index in range(_THROUGHPUT_RUNS):
        collection = f"throughput_col_{run_index}"
        run_started = time.perf_counter()
        # `asyncio.timeout`, not only the post-ingest `_check_host_safety_or_exit` below:
        # that helper runs between ingests, so on its own it cannot interrupt a single hung
        # `ingest_directory`. Both CI workflows size their step budget above this cap
        # precisely so an overrun is reported here, with the RSS trace, rather than killed
        # opaquely by the step — which only holds if the cap actually binds mid-ingest.
        async with asyncio.timeout(_RUN_BUDGET_S):
            results = await graph_pipeline.ingest_directory(
                _CORPUS,
                collection,
                glob_pattern=_INGEST_GLOB_PATTERN,
                embedder=graph_pipeline._global_embedder,
            )
        elapsed_ms.append((time.perf_counter() - run_started) * 1000)

        # --- S27/K13 non-vacuity gate: a timing figure from a degraded, short or empty
        # run is meaningless, so these gate the comparison rather than sitting beside it.
        # `_BASELINE_CORPUS_FILE_COUNT` does double duty and that is deliberate: it pins the
        # corpus for the comparison above AND is the expected result count, because
        # `ingest_directory` returns one `IngestResult` per matched file — the same 17 files
        # in, 17 results out.
        # `r.warnings` is deliberately NOT part of the gate: `pipeline.py` appends a benign
        # `backend_threshold_edges` advisory to that same list once the graph grows, and ACL
        # provenance notes land there too, so a non-empty `warnings` is not evidence of a
        # degraded run. `status == "ok"` plus the chunk/node/edge counts are.
        bad = [(r.doc_id, r.status, r.error) for r in results if r.status != "ok"]
        # A chunker regression that emptied most files would make the ingest FASTER and
        # still satisfy `node_count > 0` off a single surviving file, so per-file chunk
        # counts are part of the gate rather than left to the aggregate.
        empty = [r.doc_id for r in results if r.chunks_created == 0]
        node_count = await graph_store.node_count(collection, DEFAULT_NAMESPACE)
        edge_count = await graph_store.edge_count(collection, DEFAULT_NAMESPACE)
        assert (
            len(results) == _BASELINE_CORPUS_FILE_COUNT
            and not bad
            and not empty
            and node_count > 0
            and edge_count > 0
        ), (
            f"graph_real_artifact throughput lane: run {run_index} did not measure a "
            f"real, complete extraction — ingested {len(results)} of "
            f"{_BASELINE_CORPUS_FILE_COUNT} files, non-ok={bad}, zero-chunk={empty}, "
            f"entity nodes={node_count}, relations={edge_count}. Timing such a run would "
            "compare nothing against the baseline."
        )

        trace.append(round(_read_own_rss_mib(), 1))
        _check_host_safety_or_exit(trace[-1], baseline_mib, started, trace)

    measured_ms = statistics.median(elapsed_ms)
    # Into the --junitxml the CI step already writes, so the CI-measured figure that
    # replaces the provisional multiplier (K9) is recoverable from a green run — captured
    # stdout is not shown when a test passes. Same accepted `record_property` xunit2
    # warning as the memory budget test above.
    record_property("measured_ms", round(measured_ms, 1))
    record_property("throughput_runs_ms", [round(ms, 1) for ms in elapsed_ms])

    budget_ms = THROUGHPUT_BASELINE_MS * REGRESSION_MULTIPLIER
    assert measured_ms <= budget_ms, (
        f"graph NER ingest of {_BASELINE_CORPUS_FILE_COUNT} corpus files took "
        f"{measured_ms:.1f} ms (median of {elapsed_ms}), over the {budget_ms:.1f} ms budget "
        f"({THROUGHPUT_BASELINE_MS} ms baseline x {REGRESSION_MULTIPLIER} allowed "
        f"regression). RSS trace: {trace}"
    )

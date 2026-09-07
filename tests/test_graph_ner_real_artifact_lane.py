"""The `graph_real_artifact` lane — real GLiNER artifact, no stubs (Tasks T-8/T-9/T-10/T-11;
S1, S26, S27, S28).

S1's Hungarian half is manual, so it has no test here — T-11's recorded review sits at the
END of this file, under `MANUAL REVIEW — HUNGARIAN SPAN QUALITY`, in the same
evidence-beside-the-machinery shape as `REGRESSION_MULTIPLIER`'s manual reference check.

This lane exists because every other graph test in the suite runs against a stubbed
engine, which certifies the fixture rather than the engine. Its five tests load the real
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

`test_graph_ner_french_spans_with_offsets` (T-10, S1) is not marked either, for the same
reason: it pins spans the real engine actually produces, so a failure is a real capability
regression, not a threshold that a slower host can miss.

`test_graph_ner_determinism_across_processes` (T-10, S28) is the one test here that does
NOT take the shared `graph_pipeline` fixture — it drives two child processes, each of
which loads its own copy of the engine. It is therefore defined FIRST on purpose: pytest
runs a module in definition order, and the module-scoped `graph_pipeline` loads the real
stack lazily on the first `extract()` call, so running this test before any of the others
keeps the parent process model-free while a child holds ~3.2 GiB. Placed later it would
put a parent copy and a child copy resident at once — ~6.4 GiB, a genuine OOM on a 7 GiB
GitHub runner rather than merely a tighter host-safety margin. That placement does not
weaken the setup-phase argument above: the ordering that matters there is
`test_graph_ner_lane_non_vacuity` running before `test_graph_ner_memory_budget`, and it
still does.

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
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
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
# The committed French corpus — five first-party documents (team plan → Corpus, Q21),
# the real-engine half of S1.
_FR_CORPUS = _REPO_ROOT / "tests" / "eval" / "corpus" / "fr-docs"

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

# The graph configuration every test in this lane runs under. Shared by the
# `graph_pipeline` fixture, the French test's direct backend call (which needs the same
# confidence thresholds the extractor used, and would otherwise re-derive them from a
# private attribute), and the determinism child process — so no test can silently drift
# onto different thresholds than the one it is being compared against.
# `provider=None` keeps the optional LLM enrichment call out of the picture, so anything
# this lane observes came from the local engine.
_LANE_GRAPH_CONFIG = GraphConfig(enabled=True, provider=None)

# --- cross-process determinism constants (T-10, S28) --------------------------------
#
# Prose chunks each determinism child extracts. Deliberately far below `_MIN_CHUNKS`:
# S28's claim is about the model's own decoding being reproducible across processes, not
# about scale — and each child pays its own full model load, so the corpus size here buys
# nothing but wall time. Enough chunks to yield a graph with many nodes and both edge
# kinds, which is what makes an id-set comparison discriminating.
_DETERMINISM_CHUNKS = 64

# Collection the child writes its extraction into before reading it back. No `__` and no
# leading/trailing `_` — `GraphStore._validate_collection` rejects both.
_DETERMINISM_COLLECTION = "determinism_col"

# Wall-clock cap for the two SMALL T-10 workloads — each determinism child (one model
# load, 64 chunks) and the French leg (one model load, ~30 paragraphs). Tighter than
# `_RUN_BUDGET_S`, which sizes the 1,000-chunk loops, but deliberately ABOVE
# `prose_extraction_backend._LOAD_TIMEOUT_SECONDS` (300 s): each of these windows contains
# a full model load, so a cap equal to the load's own timeout leaves a legitimate cold
# load zero margin and turns a slow-but-fine fetch into a lane failure.
_SMALL_RUN_BUDGET_S = 600

# Prefix on the child's single result line. The child shares stdout with huggingface's
# own download/progress chatter, so the parent locates the payload by marker rather than
# assuming it owns the last line.
_CHILD_RESULT_PREFIX = "GRAPH_LANE_DETERMINISM_JSON:"

# --- French real-engine constants (T-10, S1) ----------------------------------------
#
# Recorded from the real pinned checkpoint against `_FR_CORPUS`, chunked by the same
# `_paragraphs()` split the test below uses, on 2026-09-07 — CPU, macOS arm64, two
# consecutive runs byte-identical across all 82 returned spans.
#
# WHY RE-RECORDED rather than taken from K2's spike output: the plan calls its own
# examples "illustrative pending the spike" (team plan → Corpus, Q21) and names
# `Couche de présentation` / `Magasin vectoriel`, neither of which the RESOLVED PyTorch
# checkpoint returns — the spike gate moved the engine off the ONNX path K2 measured. A
# pin must be what the shipped engine actually produces, so these are measured against it.
#
# `(surface_text, label, start, end)`, where the offsets are into the CHUNK the span was
# found in. Every one of these is French morphology an English-only engine could not
# produce — accented compounds, elided articles, French noun-adjective order — which is
# the whole point: S1 requires spans that discriminate against the outgoing engine, not
# merely "some entity found". The extractable proper nouns in these documents are
# overwhelmingly English or language-neutral (LanceDB, MCP, FTS, REST, Python, pip), so a
# count-only assertion would pass unchanged against an English-only engine.
#
# A SUBSET assertion, not an equality one: pinning all 82 spans would fail on any
# checkpoint revision that legitimately found one more entity, which is not a regression.
#
# MARGIN, and why one recorded span is deliberately NOT pinned. This comparison BLOCKS,
# and it was recorded on macOS arm64 — never on the x86 GitHub runner it will gate. Torch
# CPU kernels are not bit-identical across architectures, so a score sitting just over
# `GraphConfig.ner_confidence`'s 0.5 floor could drop under it there and red the gate on a
# non-regression. `centroïdes de collection pré-calculés` scored 0.581 — the most
# characteristically French span in the output, and the one closest to the floor — so it
# is left out rather than pinned. Every span below scored 0.778-0.937, i.e. at least 0.278
# of headroom, which no cross-architecture rounding difference plausibly closes.
#
# Matched against the spans pooled across ALL chunks rather than per-paragraph, on
# purpose: the discriminating content is the surface form and its exact offset, and
# binding each pin to a paragraph INDEX as well would break the test on any reflow of the
# corpus without making it harder for an English-only engine to satisfy.
# Corpus identity for `_FR_CORPUS`, in the same shape the throughput baseline pins
# `_CORPUS`. The spans below are offsets INTO this corpus's paragraphs, so a three-character
# edit to any of these five documents shifts them — and without this pin the test would
# report a "multilingual regression" when the real cause is that the corpus changed.
_FR_CORPUS_FILE_COUNT = 5
_FR_CORPUS_TOTAL_BYTES = 6467
_FR_CORPUS_SHA256_16 = "9d5bf4be090f1b6c"

_FRENCH_SPANS: frozenset[tuple[str, str, int, int]] = frozenset(
    {
        ("architecture en couches", "system", 31, 54),
        ("routeur multi-collection", "system", 3, 27),
        ("seuil de confiance", "concept", 3, 21),
        ("variables d'environnement", "concept", 4, 29),
        ("répertoire personnel", "system", 67, 87),
    }
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


def _paragraphs(corpus: Path) -> list[str]:
    """Extractable prose paragraphs under `corpus`, in stable sorted-file order.

    Uses `pytest.exit()`, not `assert`, on a missing/empty corpus: this helper feeds the
    `xfail`-marked budget test, where a plain `assert` would be silently absorbed as an
    expected failure rather than surfacing the real problem (a broken fixture, not an
    over-budget measurement).
    """
    paragraphs = [
        block.strip()
        for path in sorted(corpus.rglob("*.md"))
        for block in path.read_text(encoding="utf-8").split("\n\n")
        if len(block.strip()) >= _MIN_PARAGRAPH_CHARS
    ]
    if not paragraphs:
        pytest.exit(
            f"graph_real_artifact lane: no prose paragraphs found under {corpus} — "
            "corpus missing?",
            returncode=1,
        )
    return paragraphs


def _build_prose_chunks(count: int) -> list[ChunkInput]:
    """`count` prose chunks drawn from the committed corpus, cycling it as needed.

    `symbol_type=None` on every chunk: that is what routes them down the prose
    extraction path rather than the C3 code-symbol path (`graph_extractor.py`).
    """
    paragraphs = _paragraphs(_CORPUS)
    return [
        ChunkInput(
            chunk_id=f"lane-{index:06d}",
            text=paragraphs[index % len(paragraphs)],
            symbol_type=None,
            symbol_subtype=None,
        )
        for index in range(count)
    ]


def _corpus_identity(corpus: Path = _CORPUS) -> tuple[int, int, str]:
    """`(file_count, total_bytes, sha256[:16])` over the files `_INGEST_GLOB_PATTERN` matches.

    Byte-for-byte the computation the deleted standalone module used to derive the pins
    above, so the two are directly comparable. Defaulted rather than required so the
    throughput test's existing call site stays unchanged; the French leg passes
    `_FR_CORPUS` to pin its own input the same way.
    """
    digest = hashlib.sha256()
    total_bytes = 0
    files = sorted(p for p in corpus.glob(_INGEST_GLOB_PATTERN) if p.is_file())
    for path in files:
        data = path.read_bytes()
        digest.update(data)
        total_bytes += len(data)
    return len(files), total_bytes, digest.hexdigest()[:16]


def _assert_extraction_non_vacuity(
    *, degraded: bool, warnings: list, nodes: list, edges: list, load_count: int, corpus: str
) -> None:
    """S26's four blocking non-vacuity asserts. Keyword-only, because the non-vacuity test
    accumulates these across batches while the French leg reads them off one result.

    Shared by `test_graph_ner_lane_non_vacuity` and the French leg, which S1 requires to
    carry the same checks — one copy, so the two cannot drift into asserting different
    things under the same name. `_assert_child_non_vacuity`'s version stays separate on
    purpose: it reads a JSON payload from another process, not live objects.

    Ordered deliberately, and each assert closes a distinct way the lane could pass while
    proving nothing. Degradation first: a degraded run skips prose extraction entirely and
    still returns `status == "ok"`, so every later assert is meaningless without it. Then
    `load_count`, so an empty graph reports "the model never loaded" rather than "the graph
    is empty" — deliberately not the resolved provider list, which is computable from
    config with no model loaded. Then emptiness (the engine can run and return nothing).
    Then the typed-edge check, split out so a graph with nodes but no real relations still
    fails: `related_to` co-occurrence edges appear whenever two entities share a chunk, so
    `len(edges) >= 1` passes without any relation extraction at all (Q28).
    """
    assert degraded is False, (
        f"prose extraction degraded over {corpus} — the real artifact did not load "
        f"or inference raised. warnings={warnings}"
    )
    assert load_count == 1, (
        "expected exactly one real model construction for this process, got "
        f"{load_count} — the engine's load path did not execute once"
    )
    assert nodes, (
        f"the real engine returned zero entity nodes over {corpus} — a silent no-op: "
        "extraction ran, did not degrade, and produced nothing"
    )
    typed_edges = [edge for edge in edges if edge.relationship_type in _TYPED_RELATIONSHIPS]
    assert typed_edges, (
        f"no typed (uses/implements/depends_on) edge was extracted from {corpus} — only "
        "co-occurrence `related_to` edges, which prove nothing about relation extraction. "
        f"edge types seen: {sorted({e.relationship_type.value for e in edges})}"
    )


def _check_host_safety_or_exit(
    current_mib: float, baseline_mib: float, started: float, trace: list[float]
) -> None:
    """Abort the whole pytest session via `pytest.exit()` if own-process RSS has crossed
    the host-safety ceiling or the wall-clock cap has been exhausted.

    Called by the three tests that run a batched loop at 1,000-chunk scale — non-vacuity,
    memory budget, throughput — after every batch: such a run is exactly as capable of
    exhausting a host's memory or hanging regardless of which test drives it, so this is
    not only the `xfail`-marked budget test's guard, whose numeric comparison is the only
    thing meant to be soft. T-10's two legs deliberately do NOT call it: the determinism
    parent holds no model at all (its copies live in child processes bounded by
    `_SMALL_RUN_BUDGET_S`), and the French leg runs ~30 paragraphs in one shot with no loop
    to sample between, bounded by the same cap. `pytest.exit()` bypasses `xfail`
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

    `_LANE_GRAPH_CONFIG.enabled=True` is what makes `create_pipeline` construct a
    `GraphExtractor` (and therefore a real `ProseExtractionBackend`).

    Module-scoped, deliberately: ONE shared backend for every test in this file that takes
    it, not one each. Each `ProseExtractionBackend` instance loads its own full GLiNER
    stack (spike: ~3.2 GiB resident, ~3.7 GiB peak) — on a standard GitHub Actions runner
    (7 GiB total), two of those resident at once in the same process would risk a genuine
    OOM, not just a tighter host-safety margin. `load()` is idempotent
    (`prose_extraction_backend.py`), so sharing costs nothing: whichever test runs first
    pays the one real load, `load_count` stays `== 1` for the rest of the module, and the
    budget test's own "warm-up" step becomes a no-op reload rather than a genuine one —
    which only makes its baseline more settled, not less.

    `test_graph_ner_determinism_across_processes` deliberately does NOT take this fixture;
    see the module docstring for why its model copies must live in child processes.
    """
    tmp_path = tmp_path_factory.mktemp("graph_real_artifact_db")
    config = SearchConfig(db_path=str(tmp_path / "db"), graph=_LANE_GRAPH_CONFIG)
    return create_pipeline(config)


def _determinism_child_main() -> None:
    """Child-process entry point — runs when this module is executed as a script.

    The parent spawns it twice with `sys.executable`. Two child processes, rather than
    two `extract()` calls in one, is the whole point of S28: torch's intra-op/inter-op
    thread counts change CPU reduction ORDER, and two ingests sharing one process share
    one already-pinned runtime, so they cannot detect a thread-count confound. Those
    counts are pinned by `ProseExtractionBackend.load()` itself
    (`_TORCH_INTRA_OP_THREADS` / `_TORCH_INTER_OP_THREADS`), which is the production
    behaviour this test exercises rather than re-implements — the pin's existence is
    proved structurally by S42(4) in `tests/test_removed_engine_repo_guard.py`.

    Prints one `_CHILD_RESULT_PREFIX`-marked JSON line; huggingface writes its own
    chatter to this stdout too, so the parent locates the payload by that marker.
    """
    # Imported here, not at module scope: this module is collected on every default run
    # (see the module-scope note above), and these are only needed by the child, which is
    # never the collecting process.
    from archon_search.graph_extractor import GraphExtractor
    from archon_search.graph_store import GraphStore

    async def _extract_and_persist() -> dict:
        extractor = GraphExtractor(_LANE_GRAPH_CONFIG)
        result = await extractor.extract(
            _build_prose_chunks(_DETERMINISM_CHUNKS), "determinism-doc", _DETERMINISM_COLLECTION
        )
        # S28 compares the PERSISTED sets, not the in-memory ones — which is why the
        # extraction is written to a real GraphStore here and read back out. It matters:
        # the mentions table is append-only with no upsert key, so persisted mention
        # counts are a property of the store's own write path, not only the extractor's.
        with tempfile.TemporaryDirectory() as db_dir:
            store = GraphStore(db_dir)
            await store.connect()
            try:
                await store.ensure_graph_tables(_DETERMINISM_COLLECTION, ns=DEFAULT_NAMESPACE)
                await store.write_graph(
                    _DETERMINISM_COLLECTION, result.nodes, result.edges, DEFAULT_NAMESPACE
                )
                await store.write_mentions(
                    _DETERMINISM_COLLECTION, result.mentions, DEFAULT_NAMESPACE
                )
                nodes = await store.get_all_nodes(_DETERMINISM_COLLECTION, DEFAULT_NAMESPACE)
                edges = await store.get_all_edges(_DETERMINISM_COLLECTION, DEFAULT_NAMESPACE)
                mentions = await store.get_all_mentions(
                    _DETERMINISM_COLLECTION, ns=DEFAULT_NAMESPACE
                )
            finally:
                await store.disconnect()
        return {
            "degraded": result.degraded,
            "warnings": result.warnings,
            "load_count": extractor.load_count,
            # Where this child read the checkpoint from. The parent asserts it is inside
            # the operator's data dir: a child that silently re-downloaded ~1.2 GB past
            # the CI cache would otherwise still report degraded=False and load_count=1,
            # which is the exact invisible failure the lane's data-dir assert exists for.
            "models_dir": str(get_graph_models_dir()),
            # Sorted id SETS and per-entity mention COUNTS — not full rows. S28 is explicit
            # that byte-identity is the wrong claim: `pagerank_score` and last-writer-stamped
            # fields are expected to vary between two independent runs.
            "node_ids": sorted(node.id for node in nodes),
            "edge_ids": sorted(edge.id for edge in edges),
            "typed_edge_count": sum(
                1 for edge in edges if edge.relationship_type in _TYPED_RELATIONSHIPS
            ),
            "mention_counts": dict(
                sorted(Counter(mention.entity_id for mention in mentions).items())
            ),
        }

    print(f"{_CHILD_RESULT_PREFIX}{json.dumps(asyncio.run(_extract_and_persist()))}")


def _run_determinism_child(run_index: int) -> dict:
    """Spawn one child and return its parsed payload.

    `ARCHON_SEARCH_DATA_DIR` is forwarded explicitly for the same reason
    `_lane_data_dir` restores it: the child must read the populated checkpoint cache
    rather than re-download ~1.2 GB. `PYTHONPATH` is prepended because running this file
    as a script puts `tests/` on `sys.path`, not the repo root.
    """
    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(_LANE_DATA_DIR),
        "PYTHONPATH": os.pathsep.join(
            [str(_REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
        ),
    }
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True,
        # Explicit UTF-8: huggingface's own progress output on this stdout is non-ASCII,
        # and `text=True` alone would decode it with the locale encoding — a C/POSIX-locale
        # runner would then raise UnicodeDecodeError instead of reporting a real result.
        encoding="utf-8",
        errors="replace",
        timeout=_SMALL_RUN_BUDGET_S,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert child.returncode == 0, (
        f"graph_real_artifact determinism child {run_index} exited "
        f"{child.returncode}.\nstdout:\n{child.stdout}\nstderr:\n{child.stderr}"
    )
    line = next(
        (
            candidate
            for candidate in child.stdout.splitlines()
            if candidate.startswith(_CHILD_RESULT_PREFIX)
        ),
        None,
    )
    assert line is not None, (
        f"graph_real_artifact determinism child {run_index} produced no "
        f"{_CHILD_RESULT_PREFIX} line — it exited 0 without extracting anything.\n"
        f"stdout:\n{child.stdout}\nstderr:\n{child.stderr}"
    )
    return json.loads(line[len(_CHILD_RESULT_PREFIX) :])


def _assert_child_non_vacuity(payload: dict, run_index: int) -> None:
    """S26's four non-vacuity asserts, applied to one child's payload.

    S28 requires these of BOTH runs *before* the comparison, so a run against a missing
    artifact cannot pass by comparing two empty graphs — which is exactly what the id-set
    equality below would otherwise report as a success.
    """
    assert payload["degraded"] is False, (
        f"determinism child {run_index} degraded — the real artifact did not load or "
        f"inference raised. warnings={payload['warnings']}"
    )
    assert payload["load_count"] == 1, (
        f"determinism child {run_index} reports load_count="
        f"{payload['load_count']}, expected exactly one real model construction"
    )
    assert payload["models_dir"].startswith(str(_LANE_DATA_DIR)), (
        f"determinism child {run_index} read models from {payload['models_dir']}, outside "
        f"ARCHON_SEARCH_DATA_DIR={_LANE_DATA_DIR!r} — it bypassed the artifact cache and "
        "re-downloaded ~1.2 GB, which no other assert here would reveal"
    )
    assert payload["node_ids"], (
        f"determinism child {run_index} returned zero entity nodes over the committed "
        "prose corpus — a silent no-op, not a determinism result"
    )
    # The PRESENCE anchor the mention-count comparison needs: two empty mention maps
    # compare equal, so without this the comparison could pass while proving nothing.
    assert payload["mention_counts"], (
        f"determinism child {run_index} recorded no mentions despite returning "
        f"{len(payload['node_ids'])} entity nodes"
    )
    assert payload["typed_edge_count"] > 0, (
        f"determinism child {run_index} extracted no typed "
        "(uses/implements/depends_on) edge — only co-occurrence `related_to` edges, "
        "which prove nothing about relation extraction"
    )


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
def test_graph_ner_determinism_across_processes(_lane_data_dir, record_property) -> None:
    """S28: the same corpus extracted in two separate processes yields identical PERSISTED
    node and edge id sets and identical per-entity mention counts.

    Defined FIRST in this file on purpose, and deliberately NOT taking `graph_pipeline` —
    see the module docstring: this is the one lane test whose model copy lives in a child
    process, so it must run before the module-scoped fixture's own copy is resident.

    The children run SEQUENTIALLY, not concurrently, for the same reason.
    """
    first = _run_determinism_child(0)
    second = _run_determinism_child(1)

    _assert_child_non_vacuity(first, 0)
    _assert_child_non_vacuity(second, 1)
    record_property("determinism_node_count", len(first["node_ids"]))
    record_property("determinism_edge_count", len(first["edge_ids"]))

    # Sorted LISTS, so this is multiset equality — strictly stronger than the id-set
    # equality S28 asks for, and free. The reported difference is therefore a Counter
    # delta, not a set difference, which would print empty if two runs agreed on the ids
    # but disagreed on how many times one of them appeared.
    for kind in ("node_ids", "edge_ids"):
        assert first[kind] == second[kind], (
            f"the two processes extracted different {kind} over the same corpus "
            f"(run 0: {len(first[kind])}, run 1: {len(second[kind])}); "
            f"only in run 0: {sorted((Counter(first[kind]) - Counter(second[kind])).elements())}; "
            f"only in run 1: {sorted((Counter(second[kind]) - Counter(first[kind])).elements())}"
        )
    # Whole-mapping equality, not a one-directional walk of run 0's keys: an entity the
    # second run mentioned and the first did not would otherwise go unreported.
    differing = {
        entity_id: (first["mention_counts"].get(entity_id), second["mention_counts"].get(entity_id))
        for entity_id in first["mention_counts"].keys() | second["mention_counts"].keys()
        if first["mention_counts"].get(entity_id) != second["mention_counts"].get(entity_id)
    }
    assert first["mention_counts"] == second["mention_counts"], (
        "the two processes recorded different per-entity mention counts over the same "
        f"corpus (entity_id -> (run 0, run 1)): {differing}"
    )


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
@pytest.mark.asyncio
async def test_graph_ner_lane_non_vacuity(graph_pipeline) -> None:
    """S26's four blocking non-vacuity asserts — no `xfail` marker, gating merges from
    day one. Ingests the same 1,000-chunk corpus scale as the budget test below (S26:
    "a separate, unmarked, always-blocking test that ingests the same corpus"), batched
    through the same host-safety ceiling and wall-clock cap: this workload is exactly as
    capable of exhausting the host as the budget test's, so it carries the same guards.

    The four asserts themselves live in `_assert_extraction_non_vacuity`, which the French
    leg shares — see that helper for what each one closes off. What is specific to this
    test is the data-dir check below and the 1,000-chunk scale.
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

    # `nodes` here are prose nodes by construction: every chunk above carries
    # `symbol_type=None`, so the C3 code-symbol path contributed nothing.
    _assert_extraction_non_vacuity(
        degraded=degraded,
        warnings=warnings,
        nodes=nodes,
        edges=edges,
        load_count=extractor.load_count,
        corpus="the committed prose corpus",
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


@pytest.mark.graph_real_artifact
@pytest.mark.xdist_group("graph_real_artifact")
@pytest.mark.asyncio
async def test_graph_ner_french_spans_with_offsets(graph_pipeline) -> None:
    """S1's real-engine half: French prose yields entity nodes and mentions, and the
    engine returns specific French-morphology spans at specific character offsets.

    The offsets are the load-bearing part. `GraphMention` carries no offsets — the graph
    stores incidence, not spans — so the only place the engine's own character offsets are
    observable is the backend's `ExtractedEntity`, which is why this test reads them
    through `extractor._backend` rather than only through `extract()`. Constructing a
    second `ProseExtractionBackend` instead would load a second ~3.2 GiB copy of the
    stack, which is exactly what the module-scoped `graph_pipeline` fixture exists to
    avoid; the shared backend is the one `extract()` above just used.

    Every check here is a plain `assert`: this test carries no `xfail`. It pins recorded
    engine output, not a host-sensitive threshold, so a failure is a real capability
    regression rather than a slow runner.
    """
    extractor = graph_pipeline._graph_extractor
    assert extractor is not None, "graph.enabled=True must construct a GraphExtractor"

    # The pinned spans are offsets into this corpus's own paragraphs, so the comparison is
    # only valid while the input is unchanged. Checked FIRST: without it, an edit to the
    # corpus reports itself as a multilingual regression in the engine.
    assert _corpus_identity(_FR_CORPUS) == (
        _FR_CORPUS_FILE_COUNT,
        _FR_CORPUS_TOTAL_BYTES,
        _FR_CORPUS_SHA256_16,
    ), (
        f"{_FR_CORPUS} no longer matches the corpus _FRENCH_SPANS was recorded over — got "
        f"{_corpus_identity(_FR_CORPUS)}, pinned ({_FR_CORPUS_FILE_COUNT}, "
        f"{_FR_CORPUS_TOTAL_BYTES}, {_FR_CORPUS_SHA256_16!r}). Re-record the spans against "
        "the new corpus rather than re-pointing this test."
    )

    paragraphs = _paragraphs(_FR_CORPUS)
    chunks = [
        ChunkInput(
            chunk_id=f"fr-{index:06d}",
            text=paragraph,
            symbol_type=None,
            symbol_subtype=None,
        )
        for index, paragraph in enumerate(paragraphs)
    ]
    # `asyncio.timeout`, mirroring the throughput test: this leg runs no sampling loop, so
    # without it a hung forward pass would be killed opaquely by the CI step instead of
    # failing here.
    async with asyncio.timeout(_SMALL_RUN_BUDGET_S):
        result = await extractor.extract(chunks, "fr-doc", "graph_real_artifact_french")

    # S26's non-vacuity asserts, which S1 requires this leg to be preceded by (K13).
    _assert_extraction_non_vacuity(
        degraded=result.degraded,
        warnings=result.warnings,
        nodes=result.nodes,
        edges=result.edges,
        load_count=extractor.load_count,
        corpus="the French corpus",
    )
    assert result.mentions, (
        "entity nodes were created from the French prose but no mentions were — S1 "
        "requires both, and salience/co-occurrence derive from the mention rows"
    )

    # --- The spans themselves, with offsets.
    async with asyncio.timeout(_SMALL_RUN_BUDGET_S):
        batch = await extractor._backend.inference(
            [chunk.text for chunk in chunks],
            _LANE_GRAPH_CONFIG.ner_confidence,
            _LANE_GRAPH_CONFIG.relation_confidence,
        )
    observed: set[tuple[str, str, int, int]] = set()
    for paragraph, chunk_extraction in zip(paragraphs, batch.chunks, strict=True):
        for entity in chunk_extraction.entities:
            # Real substrings at real offsets, checked for EVERY returned span, not only
            # the pinned ones: a byte-vs-character offset bug on accented text would
            # otherwise hide in the spans this test does not name.
            assert paragraph[entity.start : entity.end] == entity.text, (
                f"span {entity.text!r} does not sit at its own reported offsets "
                f"[{entity.start}:{entity.end}] — that slice is "
                f"{paragraph[entity.start : entity.end]!r}"
            )
            observed.add((entity.text, entity.label, entity.start, entity.end))

    missing = _FRENCH_SPANS - observed
    assert not missing, (
        f"the real engine no longer returns these recorded French spans: {sorted(missing)}. "
        f"It returned {len(observed)} spans over {len(paragraphs)} French paragraphs. "
        "Re-record the pins only after confirming this is an intended checkpoint change, "
        "not a multilingual regression."
    )

    # The pinned spans reached the graph as entity NODES. This also cross-checks the two
    # engine passes above against each other: the spans came from the `inference()` call,
    # the nodes from the `extract()` call, so a disagreement between them fails here.
    pinned_texts = {text for text, _label, _start, _end in _FRENCH_SPANS}
    french_nodes = [node for node in result.nodes if node.entity_name in pinned_texts]
    unlanded = pinned_texts - {node.entity_name for node in french_nodes}
    assert not unlanded, (
        f"French spans the engine returned never became entity nodes: {sorted(unlanded)} "
        "— the extractor dropped them between decode and graph construction"
    )

    # ...and as MENTIONS of their own. S1 asks for nodes AND mentions from the French
    # prose; without naming the French entities here, that clause would be satisfied by
    # the language-NEUTRAL entities these documents are full of (LanceDB, MCP, REST).
    unmentioned = {node.id for node in french_nodes} - {m.entity_id for m in result.mentions}
    assert not unmentioned, (
        "these French entity nodes have no mention row of their own: "
        f"{sorted(node.entity_name for node in french_nodes if node.id in unmentioned)}"
    )


# ----------------------------------------------------------------------------------
# MANUAL REVIEW — HUNGARIAN SPAN QUALITY (T-11; S1's manual half) — NEVER ASSERTED
# ----------------------------------------------------------------------------------
# S1's other half. The French leg above is automated because `fr-docs/` is committed;
# Hungarian is not (team plan → Corpus, Q21: "Hungarian stays manual"), so this is a
# recorded review, in the shape of `REGRESSION_MULTIPLIER`'s manual reference check —
# evidence beside the machinery it is about, never a second pass condition.
#
# NOT THE FIRST HUNGARIAN REVIEW, AND NOT A DUPLICATE OF IT. The K2h spike ran one
# (tasks file, K2h Notes, 2026-08-23) and its verdict was also PASS. K2h is superseded as
# evidence for what SHIPS, because it measured a configuration that no longer exists: it
# prompted a bare `other` decoy label (since removed as a bug — it absorbed nearly every
# real span), only three relation labels, and `threshold=0.3` against both an ONNX and a
# PyTorch path. This review re-runs the question against the shipping configuration —
# the current label set, the current bare relation prompts, the resolved PyTorch path, and
# `GraphConfig`'s production defaults (0.5 / 0.75). K2h's own Hungarian spans are
# consequently not reproducible today (`fastembed könyvtártól` was labelled `other`).
# K2h is also where the comparison against the OUTGOING engine lives: it ran that engine
# over the same Hungarian text and recorded it as "systematically garbled-to-near-empty"
# on both reviewed languages. This review does not re-measure that — the outgoing engine is
# deleted from the repo and uninstalled here, so re-running it would mean provisioning it
# in a throwaway environment; that was judged unnecessary given K2h's measurement, not
# impossible. Naming its model here would also trip S42(1)'s repo-wide guard, which this
# file is deliberately not allowlisted against.
#
# PROVENANCE. Pinned checkpoint `knowledgator/gliner-relex-multi-v1.0` @
# `e990d9ba6f471b846f7d78bf7e4b4dab11761ada` (`GRAPH_NER_MODEL_NAME` / `_REVISION` in
# `paths.py` — a revision bump stales this record and it must be re-run), gliner 0.2.28 /
# torch 2.13.0, macOS 26.6.2 arm64, CPU only, 2026-09-07, at `GraphConfig`'s defaults
# (ner_confidence 0.5, relation_confidence 0.75). Input: six first-party Hungarian
# paragraphs written parallel to `fr-docs/` — same subject matter (this project's
# architecture, ingest, install, reranker, operator flow, troubleshooting) — so the review
# is comparable to the French leg rather than measuring a different kind of text. Two
# consecutive runs returned byte-identical spans and scores; that is determinism on one
# machine and one torch build, and says nothing about a different build.
#
# VERDICT — PASS (Spike gate, Finding 3: non-trivial, non-garbled entity AND relation
# spans). S1's "Then" clause is about the GRAPH, not the raw decode, so it was checked
# through `GraphExtractor.extract()`, the same call the French leg asserts on — not through
# `inference()` alone: `degraded=False`, no warnings, `load_count=1`, **21 entity nodes,
# 23 mentions, 37 edges**, and zero nodes without a mention row of their own. Two of the
# edges are typed rather than co-occurrence `related_to`: `uses rendszer -> REST API-t`
# and `uses rendszer -> FastText`. 21 nodes from 23 spans because `rendszer` occurs in
# three paragraphs and collapses to one node — the backend dedupes relations, never entity
# spans, so span count and node count are different quantities.
#
# All 23 spans, recorded in full so the offset claim below is falsifiable rather than
# asserted — `¶N` is the paragraph in the reproduction at the bottom, `[start:end]` its
# offsets into that paragraph. Every one was re-sliced out of its own paragraph and
# matched its own text, including the accented and multi-word ones, so the accent-heavy
# input surfaces no offset bug in the path exercised (all six inputs sit far below
# `GRAPH_NER_TOKEN_WINDOW_WORDS`, so the truncation path is NOT exercised and this says
# nothing about it). All four prompted entity labels appear; nothing collapsed onto one.
#
#   ¶1 system  `rendszer`                 [2:10]    @0.921
#   ¶1 system  `REST API-t`               [110:120] @0.652   ← accusative suffix retained
#   ¶1 system  `LanceDB`                  [214:221] @0.602
#   ¶2 system  `rendszer`                 [28:36]   @0.918
#   ¶2 system  `FastText`                 [68:76]   @0.870
#   ¶2 system  `fastembed`                [161:170] @0.804
#   ¶3 system  `uv csomagkezelő`          [86:101]  @0.664   ← Hungarian noun phrase
#   ¶3 system  `launchd`                  [129:136] @0.709
#   ¶3 system  `macOS`                    [159:164] @0.895
#   ¶3 system  `Linuxon`                  [177:184] @0.776   ← superessive suffix retained
#   ¶3 system  `systemd`                  [187:194] @0.746
#   ¶4 system  `kereszt-kódoló modell`    [23:44]   @0.719
#   ¶4 system  `router`                   [111:117] @0.815
#   ¶4 concept `gyűjtemények centroidjai` [120:144] @0.834
#   ¶4 concept `találatokat`              [164:175] @0.590
#   ¶5 person  `Kovács Péter`             [0:12]    @0.982   ← family-name-first order held
#   ¶5 concept `gyűjteményeket`           [67:81]   @0.702
#   ¶5 system  `archon-search`            [91:104]  @0.695
#   ¶5 event   `indexelést`               [143:153] @0.574
#   ¶5 system  `rendszer`                 [173:181] @0.915
#   ¶6 system  `beágyazó modell`          [41:56]   @0.511
#   ¶6 event   `időtúllépéshez`           [83:97]   @0.514
#   ¶6 system  `szolgáltatás`             [119:131] @0.542
#
# NOTHING HERE IS ASSERTED, WHICH IS WHY THE NEAR-FLOOR SCORES ARE RECORDED RATHER THAN
# AVOIDED. T-10 dropped a French span pinned at 0.581 against the 0.5 floor because a
# BLOCKING assert on it was unsafe across architectures. Three spans here sit in the same
# band (0.511 / 0.514 / 0.542) and one more at 0.574. They are reported, not pinned, so
# the risk T-10 avoided does not arise; the judgement they support is "the engine returns
# usable Hungarian spans", which does not turn on those four. Two of them are also weak on
# their own terms, said plainly: `indexelést` ("indexing") and `időtúllépéshez` ("to a
# timeout") are common-noun forms typed `event`, a stretch against that label's "a named
# occurrence" description, and `találatokat` / `gyűjteményeket` are generic common nouns.
#
# RELATIONS — THIN, AND DISCLOSED AS THIN. Two relations, both `uses`, both headed by the
# same generic noun `rendszer`, and 4 of the 6 paragraphs returned ZERO relations
# (per-paragraph counts: 1, 1, 0, 0, 0, 0). Both are semantically correct and neither is a
# `related_to` fallback — `related_to` IS prompted to the model
# (`_RELATION_LABELS`, `prose_extraction_backend.py`), so returning a specific label
# instead is a real choice by the model, not an artifact of the label list. That is a
# claim about the decode only; the extractor's co-occurrence loop produces `related_to`
# edges on a different path this does not speak to. Two relations reads thin in isolation,
# so it was measured against the language this feature already accepts as passing: the
# same engine, thresholds and machine over `fr-docs/` returns 82 entities and 5 relations
# across 31 paragraphs — 2.65 entities and 0.16 relations per paragraph, against
# Hungarian's 3.83 and 0.33. Hungarian is denser than French on BOTH axes, so sparse
# relations are a property of this label set on short technical prose, not a Hungarian
# deficit. The sample is small (6 paragraphs) and was authored by the same agent that ran
# and judged the review — its subject matter was fixed to match `fr-docs/` to limit that,
# but it is not an independent corpus and the density comparison is the load-bearing
# evidence here, not the raw counts.
#
# FINDING — one real per-language limitation. It is NOT a Finding-3 failure, so the plan's
# "record it in Known limitations instead of blocking" disposition (which attaches to a
# language that FAILS) does not apply. It is recorded here as a passing-language
# observation, and its destination already exists rather than being hoped for: the team
# plan's Documentation-update section gives T-14 a row for
# `220_accessibility_and_internationalization.md` requiring the corpus-language versus
# interface-language distinction, "with quality varying by language" — this finding is
# that row's content. The engine returns Hungarian entity names in
# the INFLECTED surface form it found them in, and that form is what becomes the node —
# directly observed above: `Linuxon` and `REST API-t` are node names in the `extract()`
# run, not `Linux` and `REST API`. `make_stable_entity_id` (`graph_types.py:86`) hashes
# `"{type}:{name}"` lowercased, and `name` is the raw span (`graph_extractor.py`'s
# `entity_name=entity.text`), so two inflections of one name are two nodes with two
# separate salience counts. That last step is INFERRED from the ID formula, not observed:
# no paragraph here contains bare `Linux` alongside `Linuxon`, so the split itself was
# never seen, only its mechanism. Synonym enrichment (`[graph].enrichment_auto`, default
# True) may relate such a pair with a `synonym_of` edge at `>= 0.85` cosine, but relating
# is not merging, so it would not close the split. This is worse for agglutinative
# languages than for French or English; it is not a regression, since an English-only
# engine returns nothing at all for this text.
#
# RE-RUNNING IT. There is nothing to run under pytest. Construct a
# `ProseExtractionBackend`, `await load()` FIRST (`inference()` raises otherwise), then
# `await inference(paragraphs, 0.5, 0.75)`; for the node/mention half use
# `GraphExtractor(GraphConfig(enabled=True, provider=None)).extract(...)`. Pass the six
# paragraphs as six separate texts, not one joined string — offsets are per-paragraph.
# **Rebuild each paragraph by joining its wrapped continuation lines with a single
# space**, dropping the `#` and the `N.` marker; that rule was checked, not assumed —
# reconstructing all six that way reproduces the exact strings this run was given, so
# every offset above holds. A reflow that breaks the rule breaks the offsets.
#
#   1. A rendszer réteges architektúrát követ, amely világosan elválasztja a
#      felelősségeket: a megjelenítési réteg a REST API-t és az MCP felületet tartalmazza,
#      az üzleti réteg a keresési folyamatot, az adatréteg pedig a LanceDB vektortárolót
#      és az FTS indexet.
#   2. A dokumentum betöltésekor a rendszer először elemzi a fájlt, majd a FastText
#      segítségével felismeri a domináns nyelvet, ezután darabokra bontja a szöveget, és a
#      fastembed könyvtárral sűrű vektorokat készít belőle.
#   3. A telepítési útmutató szerint a Python 3.12 verzió szükséges, a függőségeket pedig
#      az uv csomagkezelő telepíti. A szolgáltatás a launchd segítségével indul el macOS
#      rendszeren, Linuxon a systemd felügyeli.
#   4. Az újrarangsorolót egy kereszt-kódoló modell valósítja meg, amely a hibrid keresés
#      eredményeit rendezi újra. A router a gyűjtemények centroidjai alapján előszűri a
#      találatokat, mielőtt a keresési folyamat lefutna.
#   5. Kovács Péter üzemeltetőként a konfigurációs fájlban állította be a gyűjteményeket,
#      majd az archon-search parancssori eszközzel indította el az indexelést. A művelet
#      során a rendszer naplózta a haladást.
#   6. A hibaelhárítási útmutató leírja, hogy a beágyazó modell letöltése lassú hálózaton
#      időtúllépéshez vezethet. Ilyenkor a szolgáltatás 503-as választ ad, amíg a modell
#      be nem töltődik a memóriába.


if __name__ == "__main__":
    # Only reachable as `python tests/test_graph_ner_real_artifact_lane.py`, which is how
    # `_run_determinism_child` spawns the two separate processes S28 requires. Under
    # pytest this module is imported, never executed, so collection never loads a model.
    _determinism_child_main()

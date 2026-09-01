"""T-2 — pre-change ingest wall-time baseline (spaCy-backed).

Standalone record of a one-off measurement, NOT an automated test. This module
exists so a future change that removes spaCy from the ingest path has a "before"
figure to diff its own "after" measurement against. The Tests section of task
T-2 in `Documentation/Backlog/2026-08-19-035-multilingual-graph-ner-tasks.md`
calls this non-automatable: once spaCy is deleted there is no pre-change state
left to re-derive, so the number is captured now and hardcoded here.

Measurement mechanism: `archon_search.pipeline.create_pipeline(SearchConfig(
graph=GraphConfig(enabled=True, provider=None)))` (the real production ingest
entrypoint) over the real corpus below. `graph.enabled=True` is what gates
`create_pipeline` constructing a `GraphExtractor` at all; `graph.provider`
is a separate gate that only controls the optional LLM enrichment call, not
whether spaCy NER runs — kept `None` here so no Ollama/LLM call is made.

The pipeline (and its `GraphExtractor`) is built ONCE and the spaCy model is
force-loaded via one throwaway `extract()` call BEFORE the timer starts, so
the timed figure below is steady-state ingest throughput, not one-time spaCy
model load. Each of the N timed runs reuses that same warmed pipeline
instance and ingests into its own fresh collection name (same on-disk store)
so repeat runs don't hit doc-already-exists shortcuts. Each run is verified
non-vacuous: `IngestResult.status == "ok"` for every file, no degraded-mode
warning in `IngestResult.warnings`, and `GraphStore.node_count(...) > 0` for
that run's collection (proof spaCy actually produced entities, not just that
ingest didn't error).

THROUGHPUT_BASELINE_MS measures a DURATION (milliseconds), not a rate: lower
is better. S27's `measured <= THROUGHPUT_BASELINE_MS * REGRESSION_MULTIPLIER`
comparison depends on that direction.
"""

from pathlib import Path

# Corpus identity.
CORPUS = Path(__file__).parent / "corpus" / "docs"
BASELINE_CORPUS_PATH = str(CORPUS) + "/"
BASELINE_CORPUS_FILE_COUNT = 17
# Glob pattern actually passed to `ingest_directory()` below — must match its
# own default (`archon_search/pipeline.py` `ingest_directory(glob_pattern:
# str = "**/*")`) so the corpus-identity pin can't silently diverge from what
# was actually ingested.
INGEST_GLOB_PATTERN = "**/*"
# Content pin: sha256 of the sorted matched files' concatenated bytes,
# truncated to 16 hex chars, plus their total byte count — both computed by
# the script in "Reproduction" below. Detects corpus mutations that keep the
# same file count.
BASELINE_CORPUS_TOTAL_BYTES = 18380
BASELINE_CORPUS_SHA256_16 = "5b5717d98f2ac6e7"

# Measured figure (median of 3 local runs, spaCy model pre-warmed outside the
# timed window, one shared pipeline instance reused across runs, each run
# ingesting into its own fresh collection in the same on-disk LanceDB store,
# ANTHROPIC_API_KEY unset for the whole run (see "Reproduction" assertion
# below) so no LLM call can occur inside the timed window:
# 1.2969s, 1.3353s, 1.3454s wall-clock, sorted; the constant below is
# statistics.median() of those three).
THROUGHPUT_BASELINE_MS = 1335.3  # ~12.7 docs/sec at 17 docs / 1.3353s; LOWER IS BETTER (duration, not a rate)

# Machine identifier — a local dev machine, NOT a CI runner. No CI runner was
# available in this session to capture this figure on; recorded honestly as
# such. This is a known, disclosed gap: the team plan (Sequencing table,
# "Throughput baseline capture") calls for this to be captured "once, as a
# temporary one-off step added to an existing CI job" — that CI step could not
# be added and run from this local session, so the figure below is a local
# dev-machine measurement, not a CI one, and should be re-captured on CI before
# being trusted as the S27 regression baseline.
BASELINE_MACHINE = (
    "Darwin 25.5.0 arm64 (macOS-26.5.2-arm64-arm-64bit-Mach-O) "
    "— local dev machine, NOT captured on CI"
)

# Embedding/graph/ingest config active during the run (SearchConfig fields
# that materially affect ingest wall time; all others left at their default).
BASELINE_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
BASELINE_EMBEDDING_PROVIDERS: tuple[str, ...] = ()  # cfg.providers == [] -> fastembed's own CPU default (see below)
BASELINE_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"  # SearchConfig default, unchanged by the script
BASELINE_CHUNK_SIZE = 512  # SearchConfig default, unchanged by the script
BASELINE_MULTILINGUAL = False  # SearchConfig default, unchanged by the script
BASELINE_GRAPH_ENABLED = True
BASELINE_GRAPH_PROVIDER: str | None = None  # gates only the LLM enrichment call, not spaCy NER — see module docstring

# Reproducibility / version info, all read at capture time on the machine
# above. Re-running "Reproduction" below also prints these live so a re-run
# can be diffed against the recorded values.
BASELINE_GIT_COMMIT_SHA = "b5b8ba6ca7a45457811d2f8bff7ce8b3e7ddf4fe"
BASELINE_PYTHON_VERSION = "3.13.14"
BASELINE_SPACY_VERSION = "3.8.14"
BASELINE_SPACY_MODEL = "en_core_web_sm==3.8.0"
BASELINE_ONNXRUNTIME_VERSION = "1.28.0"
# `onnxruntime.get_available_providers()` on the capture machine — this lists
# what onnxruntime has INSTALLED, not proof of which provider actually
# resolved/ran for this measurement. `archon_search/embedder.py`'s
# `ModelEmbedder.__init__` maps `providers=[]` (this script's config) to
# `providers or None`, i.e. fastembed's own default provider selection — the
# list below is not evidence CoreML (vs. CPU) actually executed.
BASELINE_ONNXRUNTIME_INSTALLED_PROVIDERS: tuple[str, ...] = (
    "CoreMLExecutionProvider",
    "AzureExecutionProvider",
    "CPUExecutionProvider",
)

# Date of capture (hardcoded historical record — do not derive at runtime).
BASELINE_CAPTURE_DATE = "2026-09-01"

# ---------------------------------------------------------------------------
# Reproduction — re-run this manually (`uv run python3 _graph_ner_throughput_baseline.py`
# from `tests/eval/`) to re-derive the figures above. Not collected by pytest
# (no `test_` prefix on this file or any function in it).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import hashlib
    import os
    import shutil
    import statistics
    import subprocess
    import sys
    import tempfile
    import time

    from archon_search.config import GraphConfig, SearchConfig
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.graph_types import ChunkInput
    from archon_search.pipeline import create_pipeline

    def corpus_identity() -> tuple[int, str]:
        files = sorted(p for p in CORPUS.glob(INGEST_GLOB_PATTERN) if p.is_file())
        digest = hashlib.sha256()
        total_bytes = 0
        for f in files:
            data = f.read_bytes()
            digest.update(data)
            total_bytes += len(data)
        return total_bytes, digest.hexdigest()[:16]

    def print_reproduction_info() -> None:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parents[2], text=True
            ).strip()
        except Exception as exc:  # pragma: no cover - diagnostic only
            sha = f"<unavailable: {exc}>"
        print(f"git commit: {sha}")
        print(f"python: {sys.version.splitlines()[0]}")
        print(f"platform: {sys.platform}")

    async def warm_up(pipeline) -> None:
        """Force both the embedder/reranker and spaCy loads before timing.

        Uses the public `warmup_models()` for embedder/reranker; there is no
        public warmup hook for the graph extractor's spaCy model, so one
        throwaway `extract()` call triggers its lazy `_ensure_nlp()` load.
        """
        await pipeline.warmup_models(embedder=pipeline._global_embedder)
        assert pipeline.embedder_is_warm, "embedder warm-up failed — timed runs would pay cold-load cost"
        assert pipeline.reranker_is_warm, "reranker warm-up failed — timed runs would pay cold-load cost"
        assert pipeline._graph_extractor is not None, "graph.enabled=True must construct a GraphExtractor"
        warmup_chunk = ChunkInput(
            chunk_id="warmup-000000",
            text="Warm-up text so spaCy loads its model before timing starts.",
            symbol_type=None,
            symbol_subtype=None,
        )
        warmup_result = await pipeline._graph_extractor.extract([warmup_chunk], "warmup-doc", "warmup_col")
        assert not warmup_result.degraded, (
            "spaCy model failed to load during warm-up (degraded=True) — "
            "the timed runs would silently skip NER"
        )

    async def run_once(pipeline, graph_store, run_index: int) -> float:
        collection = f"baseline_col_{run_index}"
        start = time.perf_counter()
        results = await pipeline.ingest_directory(
            CORPUS,
            collection,
            glob_pattern=INGEST_GLOB_PATTERN,
            embedder=pipeline._global_embedder,
        )
        elapsed = time.perf_counter() - start

        assert len(results) == BASELINE_CORPUS_FILE_COUNT, (
            f"expected {BASELINE_CORPUS_FILE_COUNT} ingest results, got {len(results)}"
        )
        assert all(r.status == "ok" for r in results), (
            f"non-ok ingest status(es): {[(r.doc_id, r.status, r.error) for r in results if r.status != 'ok']}"
        )
        degraded_warnings = [w for r in results for w in r.warnings]
        assert not degraded_warnings, (
            f"ingest reported warnings (possible NER degradation): {degraded_warnings}"
        )
        node_count = await graph_store.node_count(collection, DEFAULT_NAMESPACE)
        assert node_count > 0, (
            f"graph store has zero nodes for {collection!r} — spaCy NER likely never ran "
            "(status=='ok' alone does not prove NER executed)"
        )
        return elapsed

    async def main() -> None:
        print_reproduction_info()
        total_bytes, sha16 = corpus_identity()
        print(f"corpus: {total_bytes} bytes, sha256[:16]={sha16}")

        tmp_dir = tempfile.mkdtemp(prefix="archon_baseline_")
        try:
            cfg = SearchConfig(
                db_path=str(Path(tmp_dir) / "db"),
                graph=GraphConfig(enabled=True, provider=None),
            )
            pipeline = create_pipeline(cfg)
            await pipeline.store.connect()
            assert pipeline._graph_store is not None, "graph.enabled=True must construct a GraphStore"
            await pipeline._graph_store.connect()

            await warm_up(pipeline)

            # `ingest_directory` (archon_search/pipeline.py) calls `generate_description()`
            # on a fresh collection, which makes a live Anthropic Haiku call (up to 30s)
            # whenever ANTHROPIC_API_KEY is set — inside the timed window. There is no
            # config flag to disable description generation; the env var is the only gate
            # (`archon_search/description_generator.py` checks it directly), so assert it
            # unset here, matching that same gate, rather than timing a network call.
            assert os.environ.get("ANTHROPIC_API_KEY") is None, (
                "ANTHROPIC_API_KEY is set — unset it before running this baseline; "
                "generate_description() would make a live LLM call inside the timed window"
            )

            times = [await run_once(pipeline, pipeline._graph_store, i) for i in range(3)]
            print(f"runs: {times}")
            print(f"median ms: {statistics.median(times) * 1000:.1f}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    asyncio.run(main())

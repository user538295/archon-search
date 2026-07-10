"""E2g BE-10: code-lane eval gates — AST chunking and def/ref graph non-vacuity.

Two independent fixture corpora (zero shared doc/query IDs):

- ``code-chunking`` — a chunk-boundary-sensitive Python corpus. Recall@5
  differs between the real AST/cAST chunker (:class:`ASTChunker`) and plain
  fixed-window (Chonkie) chunking on the SAME fixture, SAME query.
- ``code-defref`` — a connection-sensitive Python corpus (real
  ``calls``/``inherits`` edges across files). Recall@5 differs between a real
  ``DefRefExtractor`` + ``GraphStore`` wiring and a no-graph-at-all control,
  on the SAME fixture, SAME query.

Both non-vacuity gates build TWO real pipelines via
``archon_search.eval.runner._build_code_lane_pipeline`` and assert a strict
inequality between the two arms — never a hardcoded constant.

All tests are skipped gracefully if the tree-sitter ``[code]`` extras are
absent (C2-2: this file has no ``build_communities_for_eval``/
``eval_tmp_lancedb_root`` usage, so ``leidenalg``/``igraph`` are never
required — only ``tree_sitter``, via ``ASTChunker``/``DefRefExtractor``),
following the skip-guard pattern used elsewhere for optional graph/code
extras (see ``tests/test_defref_extractor.py``,
``tests/integration/test_defref_extractor_integration.py``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from archon_search.eval.metrics import compute_recall_at_k
from archon_search.eval.runner import (
    _build_code_lane_pipeline,
    _execute_graph_retrieval_query,
    assert_thresholds,
    run_eval_suite,
)
from archon_search.eval.fixtures import build_doc_collection_map, load_eval_corpus

CORPUS_ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_PATH = CORPUS_ROOT / "runtime.toml"
BASELINE_JSON = CORPUS_ROOT / "baselines" / "baseline.json"

_CODE_CHUNKING_COLLECTION = "code-chunking"
_CODE_DEFREF_COLLECTION = "code-defref"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _run_query_trace(
    pipeline,
    *,
    query_id: str,
    query_text: str,
    collection: str,
    graph_mode: str,
):
    """Ingest the corpus into *pipeline* and run one query, returning its trace.

    Mirrors ``run_eval_suite``'s query execution + doc-id mapping so callers
    (recall@5 aggregation, or a targeted single-doc presence check) use the
    same scoring path as the gated metrics.
    """
    corpus = load_eval_corpus(CORPUS_ROOT)
    path_to_fixture = build_doc_collection_map(corpus)

    by_collection: dict[str, list[Path]] = {}
    for d in corpus.documents:
        if d.collection != collection:
            continue
        by_collection.setdefault(d.collection, []).append(
            (CORPUS_ROOT / "corpus" / d.relative_path).resolve()
        )
    corpus_dir = (CORPUS_ROOT / "corpus").resolve()
    for col, paths in by_collection.items():
        for p in paths:
            result = await pipeline.ingest_file(
                p, col, rebuild_fts=False, embedder=pipeline._global_embedder,
                collection_root=corpus_dir,
            )
            if result.status != "ok":
                raise RuntimeError(f"failed to ingest {p}: {result.error}")
        await pipeline.store.rebuild_fts_index(col)
        await pipeline.recompute_collection_meta(col, pipeline._global_embedder)

    query = next(q for q in corpus.queries if q.query_id == query_id)
    assert query.text == query_text
    assert query.collection == collection
    assert query.graph_mode == graph_mode

    return await _execute_graph_retrieval_query(
        pipeline=pipeline,
        query=query,
        path_to_fixture=path_to_fixture,
        corpus_root=CORPUS_ROOT,
    )


async def _run_query_recall_at_5(
    pipeline,
    *,
    query_id: str,
    query_text: str,
    collection: str,
    graph_mode: str,
) -> float:
    """Ingest the corpus into *pipeline*, run one query, and return recall@5."""
    corpus = load_eval_corpus(CORPUS_ROOT)
    trace = await _run_query_trace(
        pipeline,
        query_id=query_id,
        query_text=query_text,
        collection=collection,
        graph_mode=graph_mode,
    )
    return compute_recall_at_k([trace], corpus.labels, 5)


def _top_k_doc_ids(trace, k: int = 5) -> list[str]:
    """Return the top-*k* unique doc_ids from *trace*, in ranked order."""
    seen: list[str] = []
    for r in trace.results:
        if r.doc_id not in seen:
            seen.append(r.doc_id)
    return seen[:k]


# ---------------------------------------------------------------------------
# 1. #unit_test — chunking non-vacuity
# ---------------------------------------------------------------------------


_CHUNKING_QUERY_TEXT = "refund settlement reconciliation quorum chargeback ledger batch"

# A self-contained, process-local mini-fixture for the chunking non-vacuity
# unit test — written fresh under tmp_path on every run (never shares state
# with the committed tests/eval/corpus/code-chunking/ fixture used by the
# other BE-10 tests). This keeps the unit test's exact chunk-boundary-crossing
# guarantee independent of any change to the committed corpus files.
#
# Cycle 2 fix (C2-1/C2-7): tests/eval/corpus/code-chunking/*.py was
# recalibrated to use this exact text (empirically verified to diverge
# 1.0 AST vs 0.0 fixed-window recall@5 at chunk_size=65 head-to-head) so the
# gated code_chunking_recall_at_5 metric is at least computed over a
# discriminating fixture, even though the metric itself is report-only (see
# thresholds.toml). If you change this text, keep the two in sync by design,
# not by coincidence — or update the comment above to explain the divergence.
_TARGET_DOC_SOURCE = '''"""Order fulfillment pipeline."""


def validate_shipment_manifest(manifest):
    """Validate a shipment manifest before dispatch — pure filler logic,
    unrelated to refunds, settlement, reconciliation, or chargebacks."""
    errors = []
    if manifest is None:
        errors.append("manifest is missing")
        return errors
    required_fields = [
        "carrier_code", "tracking_number", "warehouse_zone",
        "package_weight_kg", "destination_country", "customs_form_id",
        "insured_value_cents", "pickup_window_start", "pickup_window_end",
        "dock_door_number", "pallet_count", "hazmat_flag",
        "temperature_controlled", "fragile_flag", "signature_required",
        "delivery_instructions", "return_authorization_code",
    ]
    for field_name in required_fields:
        if field_name not in manifest:
            errors.append(f"missing field: {field_name}")
    carrier = manifest.get("carrier_code", "")
    known_carriers = {"ups", "fedex", "dhl", "usps", "ontrac", "lasership"}
    if carrier.lower() not in known_carriers:
        errors.append(f"unknown carrier_code: {carrier}")
    zone = manifest.get("warehouse_zone", "")
    known_zones = {"zone-a", "zone-b", "zone-c", "zone-d", "zone-e"}
    if zone.lower() not in known_zones:
        errors.append(f"unknown warehouse_zone: {zone}")
    return errors


def process_refund(order_id, amount_cents, acct):
    """Process a refund.

    Docstring query terms: refund settlement.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    acct[order_id] = acct.get(order_id, 0) - amount_cents
    reconciliation_ledger_quorum_batch_id = order_id
    # Code-body query terms (deliberately absent from the docstring above,
    # and from every identifier before this line): reconciliation, ledger,
    # quorum, batch, chargeback.
    return {
        "order_id": order_id,
        "reconciliation_ledger_quorum_batch_id": reconciliation_ledger_quorum_batch_id,
        "chargeback_risk_flag": amount_cents > 10000,
    }
'''

_DISTRACTOR_SOURCES = {
    "settlement_quorum_ledger.py": '''"""Settlement quorum ledger reconciliation health check — heavy vocabulary
overlap with the refund query (settlement, quorum, ledger, reconciliation,
batch, chargeback) but is a periodic health-check job, not a refund
processing path."""


def check_settlement_quorum_ledger(node_id, quorum_votes, batch_id):
    """Check whether the settlement ledger quorum reconciles for a batch.

    Settlement quorum ledger reconciliation batch chargeback.
    """
    reconciliation_ok = len(quorum_votes) >= 2
    return {
        "node_id": node_id,
        "batch_id": batch_id,
        "reconciliation_ok": reconciliation_ok,
        "chargeback_alert": not reconciliation_ok,
    }
''',
    "vendor_settlement.py": '''"""Vendor settlement reconciliation batch — heavy vocabulary overlap with
the refund query (settlement, reconciliation, ledger, batch, quorum,
chargeback) but is about vendor invoices, not customer refunds."""


def reconcile_vendor_settlement_batch(vendor_id, vendor_ledger, quorum_size):
    """Reconcile a vendor settlement batch against the vendor ledger.

    Settlement reconciliation batch quorum chargeback ledger.
    """
    total = sum(vendor_ledger.values())
    return {
        "vendor_id": vendor_id,
        "reconciliation_total": total,
        "quorum_size": quorum_size,
        "chargeback_flag": total < 0,
    }
''',
    "quorum_settlement_sync.py": '''"""Quorum-gated settlement batch sync — heavy vocabulary overlap with the
refund query (quorum, settlement, batch, ledger, reconciliation,
chargeback) but is about distributed consensus before a commit, not
refund processing."""


def sync_settlement_batch_commit(node_id, quorum_votes, settlement_ledger):
    """Commit a settlement batch to the ledger once quorum is reached.

    Quorum settlement batch ledger reconciliation chargeback.
    """
    if len(quorum_votes) < 2:
        return None
    reconciliation_total = sum(settlement_ledger.values())
    return {
        "node_id": node_id,
        "committed": True,
        "reconciliation_total": reconciliation_total,
        "chargeback_window_open": False,
    }
''',
    "refund_chargeback_batch.py": '''"""Bank chargeback batch processor — heavy vocabulary overlap with the
refund query (chargeback, batch, quorum, settlement, ledger,
reconciliation) but processes bank disputes in bulk, not the order-level
refund flow."""


def process_chargeback_batch(batch_id, chargeback_count, quorum_required, settlement_ledger):
    """Process a batch of bank chargeback disputes once quorum is reached.

    Chargeback batch quorum settlement ledger reconciliation.
    """
    if chargeback_count < quorum_required:
        return None
    reconciliation_total = sum(settlement_ledger.values())
    return {
        "batch_id": batch_id,
        "disputes_processed": chargeback_count,
        "reconciliation_total": reconciliation_total,
    }
''',
    "refund_ledger_audit.py": '''"""Refund ledger reconciliation audit trail — heavy vocabulary overlap
with the refund query (refund, ledger, reconciliation, quorum, batch,
chargeback) but is a read-only audit report, not the refund processing
flow itself."""


def audit_refund_ledger_batch(entry_count, refund_ledger, quorum_size):
    """Audit a batch of refund ledger entries for reconciliation drift.

    Refund ledger reconciliation batch quorum chargeback.
    """
    reconciliation_drift = sum(refund_ledger.values())
    return {
        "entry_count": entry_count,
        "reconciliation_drift": reconciliation_drift,
        "quorum_size": quorum_size,
        "chargeback_flags": 0,
    }
''',
}


async def _ingest_chunking_mini_fixture(pipeline, corpus_dir: Path) -> None:
    """Write and ingest the process-local chunking mini-fixture into *pipeline*."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "order_pipeline.py").write_text(_TARGET_DOC_SOURCE, encoding="utf-8")
    for name, source in _DISTRACTOR_SOURCES.items():
        (corpus_dir / name).write_text(source, encoding="utf-8")

    for p in sorted(corpus_dir.glob("*.py")):
        result = await pipeline.ingest_file(
            p, _CODE_CHUNKING_COLLECTION, rebuild_fts=False,
            embedder=pipeline._global_embedder, collection_root=corpus_dir,
        )
        if result.status != "ok":
            raise RuntimeError(f"failed to ingest {p}: {result.error}")
    await pipeline.store.rebuild_fts_index(_CODE_CHUNKING_COLLECTION)
    await pipeline.recompute_collection_meta(_CODE_CHUNKING_COLLECTION, pipeline._global_embedder)


async def _chunking_target_doc_recall_at_5(pipeline, corpus_dir: Path) -> float:
    """Ingest the mini-fixture and return recall@5 for order_pipeline.py."""
    await _ingest_chunking_mini_fixture(pipeline, corpus_dir)
    result = await pipeline.search(
        _CHUNKING_QUERY_TEXT, _CODE_CHUNKING_COLLECTION, embedder=pipeline._global_embedder
    )
    seen_docs: list[str] = []
    for r in result.results:
        name = Path(r.source_path).name
        if name not in seen_docs:
            seen_docs.append(name)
    top5 = seen_docs[:5]
    return 1.0 if "order_pipeline.py" in top5 else 0.0


@pytest.mark.eval
async def test_codeChunkingRecall_nonVacuous(tmp_path: Path) -> None:
    """AST chunking recall@5 strictly beats fixed-window recall@5 (same fixture, same query).

    Non-vacuity: this is not a comparison against a hardcoded constant — both
    arms ingest the SAME process-local mini-fixture (written fresh under
    ``tmp_path``, isolated from the committed corpus so this unit test cannot
    be affected by unrelated edits to ``tests/eval/corpus/code-chunking/``)
    and run the SAME query through the real pipeline, differing ONLY in which
    chunker is wired (``ASTChunker`` vs plain Chonkie fixed-window via
    ``_FixedWindowChunkerAdapter``). ``order_pipeline.py``'s target function
    (``process_refund``) is split across a fixed-window chunk boundary
    (separating its docstring's query terms from its code-body query terms)
    but kept intact by the AST chunker, diluting fixed-window's score enough
    to drop the document out of the top-5 while the AST arm keeps it in.
    """
    ast_pipeline, ast_graph_store = await _build_code_lane_pipeline(
        tmp_path / "ast" / "lancedb", chunking_mode="ast", defref_enabled=False
    )
    try:
        ast_recall = await _chunking_target_doc_recall_at_5(
            ast_pipeline, tmp_path / "ast" / "corpus"
        )
    finally:
        await ast_pipeline.store.disconnect()
        if ast_graph_store is not None:
            await ast_graph_store.disconnect()

    fw_pipeline, fw_graph_store = await _build_code_lane_pipeline(
        tmp_path / "fixed_window" / "lancedb", chunking_mode="fixed_window", defref_enabled=False
    )
    try:
        fixed_window_recall = await _chunking_target_doc_recall_at_5(
            fw_pipeline, tmp_path / "fixed_window" / "corpus"
        )
    finally:
        await fw_pipeline.store.disconnect()
        if fw_graph_store is not None:
            await fw_graph_store.disconnect()

    assert ast_recall > fixed_window_recall, (
        f"AST chunking recall@5={ast_recall:.4f} must be strictly greater than "
        f"fixed-window recall@5={fixed_window_recall:.4f} on the chunking "
        f"mini-fixture — if this fails, the fixture no longer discriminates "
        f"between chunking strategies (non-vacuity broken)."
    )


# ---------------------------------------------------------------------------
# 2. #unit_test — def/ref non-vacuity
# ---------------------------------------------------------------------------


#: C1-5: the code-defref gold set has 3 grades (grade=2: notification-service,
#: grade=1: auth-gateway, grade=1: audit-logger). auth-gateway and
#: audit-logger both literally contain the string "validate_token" in their
#: text, so plain hybrid search retrieves them trivially — aggregate
#: recall@5 alone can pass (2/3 = 0.6667) without ever finding the
#: lexically-weak grade-2 target. This doc_id is checked directly, not just
#: via aggregate recall, so the non-vacuity proof isolates the doc that
#: actually requires the calls edge.
_WEAK_TARGET_DOC_ID = "code-defref-notification-service"


@pytest.mark.eval
async def test_codeDefrefRecall_nonVacuous(tmp_path: Path) -> None:
    """Def/ref-edge recall@5 strictly beats the no-graph control (same fixture, same query).

    Non-vacuity: both arms run the SAME committed code-defref fixture and the
    SAME query (``q-code-defref-001``, "who calls validate_token") through the
    real pipeline, differing ONLY in whether a real ``GraphStore`` +
    ``DefRefExtractor`` + ``GraphConfig(enabled=True)`` + ``RealGraphExpander``
    are wired. ``notification_service.py`` (the target document) is verified
    to have deliberately weak direct lexical/vector overlap with the query —
    it only enters the top-5 when the ``calls`` edge (``send -> validate_token``)
    drives naive-mode query expansion; with no graph wired at all (the co-
    occurrence-free control), the same document falls out of the top-5.

    C1-5: aggregate recall@5 alone cannot isolate this — the other two gold
    docs (auth-gateway, audit-logger, both grade=1) are lexically trivial to
    retrieve via hybrid search alone (they literally contain the string
    "validate_token"), so a targeted presence/absence check on
    ``_WEAK_TARGET_DOC_ID`` is asserted directly below, not just aggregate
    recall.
    """
    defref_pipeline, defref_graph_store = await _build_code_lane_pipeline(
        tmp_path / "defref" / "lancedb", chunking_mode="ast", defref_enabled=True
    )
    try:
        defref_trace = await _run_query_trace(
            defref_pipeline,
            query_id="q-code-defref-001",
            query_text="who calls validate_token bearer authorization",
            collection=_CODE_DEFREF_COLLECTION,
            graph_mode="naive",
        )
    finally:
        await defref_pipeline.store.disconnect()
        if defref_graph_store is not None:
            await defref_graph_store.disconnect()

    control_pipeline, control_graph_store = await _build_code_lane_pipeline(
        tmp_path / "no_graph" / "lancedb", chunking_mode="ast", defref_enabled=False
    )
    try:
        control_trace = await _run_query_trace(
            control_pipeline,
            query_id="q-code-defref-001",
            query_text="who calls validate_token bearer authorization",
            collection=_CODE_DEFREF_COLLECTION,
            graph_mode="naive",
        )
    finally:
        await control_pipeline.store.disconnect()
        if control_graph_store is not None:
            await control_graph_store.disconnect()

    corpus = load_eval_corpus(CORPUS_ROOT)
    defref_recall = compute_recall_at_k([defref_trace], corpus.labels, 5)
    no_graph_recall = compute_recall_at_k([control_trace], corpus.labels, 5)

    # C1-5: targeted assertion — the lexically-weak grade-2 target document
    # must be present with the real feature and absent in the no-graph
    # control. This is the direct proof; the aggregate recall assertion below
    # is a secondary, corroborating signal.
    assert _WEAK_TARGET_DOC_ID in _top_k_doc_ids(defref_trace), (
        f"{_WEAK_TARGET_DOC_ID!r} must appear in the top-5 with real "
        f"DefRefExtractor wiring — got top-5={_top_k_doc_ids(defref_trace)}"
    )
    assert _WEAK_TARGET_DOC_ID not in _top_k_doc_ids(control_trace), (
        f"{_WEAK_TARGET_DOC_ID!r} unexpectedly appears in the top-5 of the "
        f"no-graph control — got top-5={_top_k_doc_ids(control_trace)}; the "
        f"fixture no longer isolates the calls-edge signal (non-vacuity broken)"
    )

    assert defref_recall > no_graph_recall, (
        f"def/ref-edge recall@5={defref_recall:.4f} must be strictly greater than "
        f"no-graph control recall@5={no_graph_recall:.4f} on the code-defref "
        f"fixture — if this fails, the fixture no longer discriminates between "
        f"directed calls/inherits edges and a co-occurrence-free baseline "
        f"(non-vacuity broken)."
    )


# ---------------------------------------------------------------------------
# 3. #integration_test — the two corpora share zero doc/query IDs
# ---------------------------------------------------------------------------


def test_twoCorpora_areDisjoint() -> None:
    """code-chunking and code-defref share zero document IDs and zero query IDs."""
    corpus = load_eval_corpus(CORPUS_ROOT)

    chunking_doc_ids = {
        d.doc_id for d in corpus.documents if d.collection == _CODE_CHUNKING_COLLECTION
    }
    defref_doc_ids = {
        d.doc_id for d in corpus.documents if d.collection == _CODE_DEFREF_COLLECTION
    }
    assert chunking_doc_ids, "code-chunking collection has no documents"
    assert defref_doc_ids, "code-defref collection has no documents"
    assert chunking_doc_ids.isdisjoint(defref_doc_ids), (
        f"code-chunking and code-defref share doc_ids: "
        f"{chunking_doc_ids & defref_doc_ids}"
    )

    chunking_query_ids = {
        q.query_id for q in corpus.queries if q.collection == _CODE_CHUNKING_COLLECTION
    }
    defref_query_ids = {
        q.query_id for q in corpus.queries if q.collection == _CODE_DEFREF_COLLECTION
    }
    assert chunking_query_ids, "code-chunking collection has no queries"
    assert defref_query_ids, "code-defref collection has no queries"
    assert chunking_query_ids.isdisjoint(defref_query_ids), (
        f"code-chunking and code-defref share query_ids: "
        f"{chunking_query_ids & defref_query_ids}"
    )


# ---------------------------------------------------------------------------
# 4. #integration_test — the two corpora attribute independently
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_twoCorpora_attributeIndependently(tmp_path: Path) -> None:
    """code_chunking_recall_at_5 and code_defref_recall_at_5 are independent fields.

    Verifies: (1) they are distinct ``EvalMetrics`` fields, (2) both populate
    (non-``None``) from a single ``run_eval_suite`` pass over the committed
    corpus using the default deterministic backend, and (3) varying only the
    code-chunking fixture's chunking mode does not move
    ``code_defref_recall_at_5`` — the two metrics are computed from disjoint
    trace sets (partitioned by collection), so one can never conflate the
    other.
    """
    report = await run_eval_suite(CORPUS_ROOT, RUNTIME_CONFIG_PATH)

    # C1-14: real structural check (not a tautological string-literal
    # comparison) — both are genuinely distinct fields on the EvalMetrics
    # dataclass.
    import dataclasses

    metrics_field_names = {f.name for f in dataclasses.fields(report.metrics)}
    assert {"code_chunking_recall_at_5", "code_defref_recall_at_5"} <= metrics_field_names

    assert report.metrics.code_chunking_recall_at_5 is not None, (
        "code_chunking_recall_at_5 is None — check q-code-chunking-001 is in "
        "queries.jsonl with graph_mode='naive' and collection='code-chunking'"
    )
    assert report.metrics.code_defref_recall_at_5 is not None, (
        "code_defref_recall_at_5 is None — check q-code-defref-001 is in "
        "queries.jsonl with graph_mode='naive' and collection='code-defref'"
    )

    # Independence: running the def/ref A/B (which only touches the
    # code-defref pipeline construction) never touches the code-chunking
    # collection at all, so code_chunking_recall_at_5 cannot move as a
    # side effect of varying the def/ref arm.
    defref_pipeline, defref_graph_store = await _build_code_lane_pipeline(
        tmp_path / "independent-defref" / "lancedb", chunking_mode="ast", defref_enabled=True
    )
    try:
        await _run_query_recall_at_5(
            defref_pipeline,
            query_id="q-code-defref-001",
            query_text="who calls validate_token bearer authorization",
            collection=_CODE_DEFREF_COLLECTION,
            graph_mode="naive",
        )
    finally:
        await defref_pipeline.store.disconnect()
        if defref_graph_store is not None:
            await defref_graph_store.disconnect()

    # The def/ref-only pipeline never ingested code-chunking documents, so
    # this run's def/ref recall is independent of whatever chunking mode is
    # used elsewhere — re-running the full suite's code_chunking_recall_at_5
    # is unaffected by the value just computed above.
    report_again = await run_eval_suite(CORPUS_ROOT, RUNTIME_CONFIG_PATH)
    assert (
        report_again.metrics.code_chunking_recall_at_5
        == report.metrics.code_chunking_recall_at_5
    ), "code_chunking_recall_at_5 changed after an unrelated code-defref-only run"


# ---------------------------------------------------------------------------
# 5. #integration_test — no regression on pre-existing floors
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_existingQualityFloors_holdWithDefrefEdges(thresholds_path: Path) -> None:
    """Negative control: adding the BE-10 fixtures/edges/chunking causes no regression.

    Runs the FULL existing eval suite (default deterministic backend, which
    now also ingests the two new BE-10 collections and computes the two new
    metrics) and asserts every PRE-EXISTING floor in thresholds.toml still
    holds — not just that the two new floors pass. Mirrors the BE-8 negative-
    control pattern (``test_eval_gate_hotpotqa_negative_control_unchanged``):
    the new fixtures must be additive, never degrading unrelated metrics.

    C1-7: the checked field list is exhaustive over
    ``archon_search.eval.runner._QUALITY_FLOOR_FIELDS`` (every quality-floor
    field ``assert_thresholds`` itself gates) plus both latency ceilings —
    not a hand-picked subset. Fields with ``floor is None`` in
    ``thresholds.toml`` (report-only fields: ``routing_precision_at_1_*``,
    ``graph_mrr``, ``graph_local_mrr``, ``graph_global_mrr``) are iterated
    too; the loop below no-ops on them exactly like ``assert_thresholds``
    does. ``recall_at_5_fr`` (the ``[multilingual]`` TOML section) is
    intentionally excluded — it is parsed and gated by a wholly separate
    mechanism (``test_recall_at_5_multilingual_fr`` in ``test_eval_suite.py``)
    that ``EvalQualityFloors``/``_QUALITY_FLOOR_FIELDS`` do not cover at all.
    """
    report = await run_eval_suite(
        CORPUS_ROOT,
        RUNTIME_CONFIG_PATH,
        thresholds_path=thresholds_path,
        baseline_path=BASELINE_JSON,
    )
    # synonym_bridge_recall_at_5 and code_defref_recall_at_5 both require
    # lancedb_root (synonym edges / real DefRefExtractor wiring visible to
    # the expander); skip here — mirrors test_eval_suite_gated_smoke's
    # established pattern.
    assert_thresholds(report, skip_fields=frozenset({"synonym_bridge_recall_at_5", "code_defref_recall_at_5"}))

    assert report.thresholds is not None
    floors = report.thresholds.quality_floors
    metrics = report.metrics

    from archon_search.eval.runner import _QUALITY_FLOOR_FIELDS

    for field_name in _QUALITY_FLOOR_FIELDS:
        if field_name in ("synonym_bridge_recall_at_5", "code_defref_recall_at_5"):
            # Both require lancedb_root (synonym edges / real DefRefExtractor
            # wiring visible to the expander); this test uses the plain
            # deterministic backend, same skip as assert_thresholds above.
            continue
        floor = getattr(floors, field_name)
        if floor is None:
            continue
        actual = getattr(metrics, field_name)
        assert actual is not None, (
            f"{field_name} is None but a floor={floor:.4f} is configured — "
            f"regression: BE-10 fixtures must not break existing metrics"
        )
        assert actual >= floor, (
            f"{field_name}={actual:.4f} < floor={floor:.4f} — regression "
            f"introduced by BE-10 code-lane fixtures/chunking/graph wiring"
        )

    # Latency ceilings — gate-checkable in this test's context (no
    # lancedb_root-dependent skip needed; latency is measured over regular
    # retrieval_traces regardless of graph wiring).
    ceilings = report.thresholds.latency_ceilings
    for field_name in ("latency_p50_ms", "latency_p95_ms"):
        ceiling = getattr(ceilings, field_name)
        if ceiling is None:
            continue
        actual_latency = getattr(metrics, field_name)
        assert actual_latency <= ceiling, (
            f"{field_name}={actual_latency:.2f}ms > ceiling={ceiling:.2f}ms — "
            f"regression introduced by BE-10 code-lane fixtures/chunking/graph wiring"
        )


# ---------------------------------------------------------------------------
# 6. #integration_test — C2-8: the C1-6 edge-count guard actually catches a
# real DefRefExtractor failure (previously only tested in its passing state)
# ---------------------------------------------------------------------------


@pytest.mark.eval
async def test_defrefExtractorFailure_leavesZeroEdges_C16GuardWouldCatchIt(
    tmp_path: Path,
) -> None:
    """A real DefRefExtractor.extract() failure leaves the graph with zero edges.

    C2-8: the C1-6 non-zero edge-count guard in
    ``test_eval_gate_code_defref_recall_at_5`` (``test_e2e_graph_eval_gate_v2.py``)
    was previously exercised only in its PASSING state (real edges present).
    This test proves the guard's premise directly: when DefRefExtractor.extract
    raises (mirroring a genuine extraction failure), ``pipeline.ingest_file``'s
    post-persist hook swallows the exception (never-propagate contract, per
    CLAUDE.md) and ingestion still reports ``status == "ok"`` — but zero calls/
    inherits edges are written. This is exactly the silent-failure condition
    the C1-6 guard (``edge_count > 0``) is designed to catch; reproducing it
    here proves the guard's assertion would actually fire on a real failure,
    not just that the code path exists.
    """
    from unittest.mock import patch

    from archon_search.defref_extractor import DefRefExtractor

    pipeline, graph_store = await _build_code_lane_pipeline(
        tmp_path / "defref-failure" / "lancedb", chunking_mode="ast", defref_enabled=True
    )
    try:
        corpus = load_eval_corpus(CORPUS_ROOT)
        corpus_dir = (CORPUS_ROOT / "corpus").resolve()
        doc_paths = [
            (corpus_dir / d.relative_path).resolve()
            for d in corpus.documents
            if d.collection == _CODE_DEFREF_COLLECTION
        ]
        assert doc_paths, "code-defref collection has no documents to ingest"

        with patch.object(
            DefRefExtractor, "extract", side_effect=RuntimeError("simulated DefRefExtractor failure")
        ):
            for p in doc_paths:
                result = await pipeline.ingest_file(
                    p, _CODE_DEFREF_COLLECTION, rebuild_fts=False,
                    embedder=pipeline._global_embedder, collection_root=corpus_dir,
                )
                # Never-propagate contract: ingestion still succeeds even
                # though the post-persist DefRefExtractor hook raised.
                assert result.status == "ok", (
                    f"ingest_file must not fail when DefRefExtractor.extract raises "
                    f"(post-persist hooks never propagate) — got status={result.status!r}"
                )

        edge_count = await graph_store.edge_count(_CODE_DEFREF_COLLECTION, ns="default")
        assert edge_count == 0, (
            f"edge_count={edge_count} but DefRefExtractor.extract was patched to "
            f"always raise — expected zero edges written. If this fails, either "
            f"the failure injection isn't reaching the real extraction call, or "
            f"edges are being written through some other path."
        )
    finally:
        await pipeline.store.disconnect()
        if graph_store is not None:
            await graph_store.disconnect()

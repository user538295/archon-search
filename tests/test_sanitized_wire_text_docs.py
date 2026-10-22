"""Pin the fixed wire texts that replaced exception text, and every doc's quote of them.

Each case builds the wire text from the source (constant or helper) and compares it with the
literal a client sees; the same literal must appear verbatim in every doc that quotes it (their
quoting style varies: backticks, a JSON ``"error"`` value, a double-quoted string), and, for a
text this change introduced or reworded, back-quoted in ``BREAKING.md``. A reworded constant
then fails here until the literal and every quoting doc are updated, so the documented wording
cannot drift from what the server sends.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from archon_search import defref_extractor, model_validation, pipeline
from archon_search import parser as archon_parser
from archon_search.jobs import maintenance_loop
from archon_search.jobs import store as job_store
from archon_search.server import _ingest_lock, routes_explain, routes_export
from archon_search.sync import collection_drop_deferred_detail, collection_sync_busy_detail, collection_sync_failed_detail

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BREAKING_MD = _REPO_ROOT / "BREAKING.md"
_API_REFERENCE = "Documentation/Architecture/600_api_reference_or_public_interface.md"
_COMPONENT_CATALOG = "Documentation/Architecture/110_component_catalog_and_layer_breakdown.md"
_SERVICES = "Documentation/Architecture/120_services_and_integration_architecture.md"
_ERROR_STRATEGY = "Documentation/Architecture/140_error_handling_strategy.md"
_TROUBLESHOOTING = "Documentation/UserManual/160_troubleshooting.md"
_GRAPH_OPERATIONS = "Documentation/OperatorGuide/60_graph_operations.md"
_INCIDENT_RUNBOOK = "Documentation/OperatorGuide/90_incident_runbook.md"
_MCP_INTEGRATION = "Documentation/DeveloperGuide/05_mcp_integration.md"
_DEV_ERROR_HANDLING = "Documentation/DeveloperGuide/06_error_handling.md"
_BREAKING_INDEX = "Documentation/MigrationGuide/03_breaking_changes_index.md"
_MONITORING = "Documentation/OperatorGuide/20_monitoring_and_alerts.md"
_JOBS_MANUAL = "Documentation/UserManual/100_jobs_and_async_operations.md"
_MAINTENANCE_GUIDE = "Documentation/OperatorGuide/50_maintenance_and_jobs.md"
_EXPORT_IMPORT_MANUAL = "Documentation/UserManual/90_export_import.md"

# ``in_breaking``: whether ``BREAKING.md`` quotes the literal back-quoted — True for every text a
# change introduced or reworded. Asserted both ways, so the flag cannot drift from the file.
_CASES = [
    pytest.param(
        lambda: collection_sync_failed_detail("<name>"),
        "sync of collection '<name>' failed; see the server log for details",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _TROUBLESHOOTING, _BREAKING_INDEX),
        True,
        id="collection-sync",
    ),
    pytest.param(
        lambda: collection_sync_busy_detail("<name>"),
        "sync of collection '<name>' skipped: the collection is busy with another write; the next sync retries it",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _TROUBLESHOOTING, _BREAKING_INDEX),
        True,
        id="collection-sync-busy",
    ),
    pytest.param(
        lambda: collection_drop_deferred_detail("<name>"),
        "collection '<name>' is no longer configured but was not removed: it is busy; the next sync retries",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _TROUBLESHOOTING, _BREAKING_INDEX),
        True,
        id="collection-drop-deferred",
    ),
    pytest.param(
        lambda: archon_parser.PARSE_FAILED_TEMPLATE.format(
            path="<absolute path>", reason=archon_parser.PARSE_FAILED_DETAIL
        ),
        "Failed to parse <absolute path>: the document could not be parsed; see the server log for details",
        (_API_REFERENCE, _MCP_INTEGRATION, _BREAKING_INDEX),
        True,
        id="parse-error",
    ),
    pytest.param(
        lambda: archon_parser.PARSE_WORKER_LOST_DETAIL,
        "The document parser worker was lost while converting this file "
        "(it may have been killed by the OS, e.g. out of memory); retry the ingest.",
        (),
        True,
        id="parse-worker-lost",
    ),
    pytest.param(
        lambda: archon_parser.MARKITDOWN_MISSING_DETAIL,
        "markitdown is not installed; reinstall archon-search to restore it",
        (_API_REFERENCE, _MCP_INTEGRATION, _BREAKING_INDEX),
        True,
        id="markitdown-missing",
    ),
    pytest.param(
        lambda: pipeline.FILE_SIZE_UNKNOWN_TEMPLATE.format(path="<path>"),
        "Cannot determine file size for <path>",
        (_API_REFERENCE, _BREAKING_INDEX),
        True,
        id="file-size-unknown",
    ),
    pytest.param(
        lambda: model_validation.ONNX_PROVIDER_QUERY_FAILED_WARNING,
        "could not query ONNX providers; see the log for details",
        (_COMPONENT_CATALOG, _BREAKING_INDEX),
        True,
        id="onnx-provider-query",
    ),
    pytest.param(
        lambda: model_validation.EMBEDDER_PROBE_FAILED_WARNING,
        "embedder probe failed; see the log for details",
        (_BREAKING_INDEX,),
        True,
        id="embedder-probe",
    ),
    pytest.param(
        lambda: model_validation.RERANKER_PROBE_FAILED_WARNING,
        "reranker probe failed; see the log for details",
        (_BREAKING_INDEX,),
        True,
        id="reranker-probe",
    ),
    pytest.param(
        lambda: model_validation.VALIDATION_FAILED_WARNING,
        "model validation failed unexpectedly; see the log for details",
        (_TROUBLESHOOTING, _BREAKING_INDEX),
        True,
        id="validation-failed",
    ),
    pytest.param(
        lambda: model_validation.RERANKER_SPLIT_VALIDATION_FAILED_WARNING,
        "reranker split-provider validation failed unexpectedly; see the log for details",
        (_BREAKING_INDEX,),
        True,
        id="reranker-split-validation-failed",
    ),
    pytest.param(
        lambda: model_validation.VALIDATION_TASK_FAILED_WARNING,
        "validation task failed unexpectedly; see the log for details",
        (_SERVICES, _TROUBLESHOOTING),
        True,
        id="validation-task-failed",
    ),
    pytest.param(
        lambda: model_validation.GRAPH_NER_UNDETERMINED_WARNING,
        "graph NER model presence could not be determined; see the log for details",
        (_API_REFERENCE, _COMPONENT_CATALOG, _GRAPH_OPERATIONS, _INCIDENT_RUNBOOK),
        True,
        id="graph-ner-undetermined",
    ),
    pytest.param(
        lambda: model_validation.GLINER_NOT_INSTALLED_WARNING,
        "graph prose entity extraction is disabled: gliner is not installed. "
        "Install archon-search[graph] to use graph prose entity extraction",
        (
            _API_REFERENCE,
            _COMPONENT_CATALOG,
            _ERROR_STRATEGY,
            _GRAPH_OPERATIONS,
            _INCIDENT_RUNBOOK,
            _MONITORING,
            _BREAKING_INDEX,
        ),
        True,
        id="gliner-not-installed",
    ),
    pytest.param(
        lambda: model_validation.VALIDATION_TIMED_OUT_TEMPLATE.format(seconds="<N>"),
        "validation timed out after <N>s",
        (_INCIDENT_RUNBOOK, _MONITORING),
        False,
        id="validation-timed-out",
    ),
    pytest.param(
        lambda: model_validation.VALIDATION_TIMED_OUT_TEMPLATE.format(seconds="{N}"),
        "validation timed out after {N}s",
        (_API_REFERENCE,),
        False,
        id="validation-timed-out-api-reference",
    ),
    pytest.param(
        lambda: model_validation.VALIDATION_TIMED_OUT_TEMPLATE.format(seconds=60),
        "validation timed out after 60s",
        (_TROUBLESHOOTING,),
        # The float `validation_timeout_seconds` entry quotes it: whole seconds still render "60s".
        True,
        id="validation-timed-out-default",
    ),
    # A ``:.1f`` field cannot take a placeholder, so the docs' placeholder replaces it textually.
    pytest.param(
        lambda: model_validation.RERANKER_SPLIT_TIMED_OUT_TEMPLATE.replace("{seconds:.1f}", "<X.X>"),
        "reranker split-provider validation timed out after <X.X>s",
        (_INCIDENT_RUNBOOK, _MONITORING),
        False,
        id="reranker-split-timed-out",
    ),
    pytest.param(
        lambda: model_validation.RERANKER_SPLIT_TIMED_OUT_TEMPLATE.replace("{seconds:.1f}", "{X.X}"),
        "reranker split-provider validation timed out after {X.X}s",
        (_API_REFERENCE,),
        False,
        id="reranker-split-timed-out-api-reference",
    ),
    pytest.param(
        lambda: model_validation.RERANKER_SPLIT_BUDGET_EXHAUSTED_WARNING,
        "reranker split-provider validation skipped: timeout budget exhausted",
        (_INCIDENT_RUNBOOK, _MONITORING, _API_REFERENCE),
        False,
        id="reranker-split-budget-exhausted",
    ),
    pytest.param(
        lambda: defref_extractor.DEFREF_PARSE_FAILED_WARNING.format(file_path="<path>"),
        "def/ref extraction failed to parse '<path>'; see the server log for details",
        (_BREAKING_INDEX,),
        True,
        id="defref-parse",
    ),
    pytest.param(
        lambda: pipeline.METADATA_STORE_UNAVAILABLE_DETAIL,
        "service unavailable: metadata store could not be reached",
        (_API_REFERENCE, _ERROR_STRATEGY, _TROUBLESHOOTING, _INCIDENT_RUNBOOK, _DEV_ERROR_HANDLING),
        False,
        id="metadata-store-unavailable",
    ),
    pytest.param(
        lambda: routes_explain.ROUTING_UNAVAILABLE_DETAIL,
        "service unavailable: routing could not be completed",
        (_API_REFERENCE, _ERROR_STRATEGY),
        False,
        id="routing-unavailable",
    ),
    pytest.param(
        lambda: maintenance_loop.RETRY_DISPATCH_FAILED_DETAIL,
        "maintenance retry could not be started; see the server log for details",
        (_ERROR_STRATEGY, _DEV_ERROR_HANDLING, _JOBS_MANUAL, _MAINTENANCE_GUIDE, _BREAKING_INDEX),
        True,
        id="maintenance-retry-dispatch",
    ),
    pytest.param(
        lambda: job_store.NOT_STARTED_BEFORE_RESTART_DETAIL,
        "never started; the server restarted before it ran",
        (_ERROR_STRATEGY, _DEV_ERROR_HANDLING, _JOBS_MANUAL, _INCIDENT_RUNBOOK, _BREAKING_INDEX),
        True,
        id="ingest-not-started-before-restart",
    ),
    pytest.param(
        lambda: _ingest_lock.COLLECTION_JOB_IN_PROGRESS_DETAIL,
        "a job is in progress on this collection; retry after it completes",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _DEV_ERROR_HANDLING, _INCIDENT_RUNBOOK, _BREAKING_INDEX),
        True,
        id="delete-collection-job-in-progress",
    ),
    pytest.param(
        lambda: _ingest_lock.COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id="<id>"),
        "job <id> is in progress; retry after it completes",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _DEV_ERROR_HANDLING, _INCIDENT_RUNBOOK, _BREAKING_INDEX),
        True,
        id="delete-collection-own-job-in-progress",
    ),
    pytest.param(
        lambda: _ingest_lock.COLLECTION_WRITER_IN_PROGRESS_DETAIL,
        "an MCP ingest or a collection sync is in progress on this collection; retry after it completes",
        (_API_REFERENCE, _COMPONENT_CATALOG, _ERROR_STRATEGY, _DEV_ERROR_HANDLING, _INCIDENT_RUNBOOK, _BREAKING_INDEX),
        True,
        id="delete-collection-jobless-writer-in-progress",
    ),
    pytest.param(
        lambda: routes_export.OVERWRITE_REFUSED_PREFIX
        + _ingest_lock.COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id="<id>"),
        "force_overwrite refused: job <id> is in progress; retry after it completes",
        (_API_REFERENCE, _SERVICES, _EXPORT_IMPORT_MANUAL, _DEV_ERROR_HANDLING, _INCIDENT_RUNBOOK, _BREAKING_INDEX),
        True,
        id="import-overwrite-own-namespace-job-in-progress",
    ),
    pytest.param(
        lambda: routes_export.OVERWRITE_REFUSED_PREFIX + _ingest_lock.COLLECTION_JOB_IN_PROGRESS_DETAIL,
        "force_overwrite refused: a job is in progress on this collection; retry after it completes",
        (_API_REFERENCE, _SERVICES, _EXPORT_IMPORT_MANUAL, _DEV_ERROR_HANDLING),
        True,
        id="import-overwrite-job-in-progress",
    ),
    pytest.param(
        lambda: routes_export.IMPORT_OVERWRITE_LOCK_BUSY_DETAIL,
        "force_overwrite refused: collection is busy with another write; retry after it completes",
        (_API_REFERENCE, _SERVICES, _EXPORT_IMPORT_MANUAL, _DEV_ERROR_HANDLING),
        True,
        id="import-overwrite-lock-busy",
    ),
    # Quoted by no doc verbatim (they paraphrase it); pinned so the OpenAPI wording is deliberate.
    pytest.param(
        lambda: routes_explain._EXPLAIN_500_DESCRIPTION,
        "JSON `detail` for a pipeline-stage failure (`<stage> error: <ExceptionType>`) or a failed "
        "HyDE resolution or pipeline call (`explain failed`); see the server log for details. "
        "Any other failure (e.g. loading the collection's embedder failing for a reason other than "
        "the embedder not being ready yet) is a plain-text 500.",
        (),
        False,
        id="explain-500-description",
    ),
]


@pytest.mark.parametrize(("produce", "literal", "docs", "in_breaking"), _CASES)
def test_wire_text_matches_source_and_every_quoting_doc(
    produce: Callable[[], str], literal: str, docs: tuple[str, ...], in_breaking: bool
) -> None:
    assert produce() == literal
    assert (f"`{literal}`" in _BREAKING_MD.read_text(encoding="utf-8")) is in_breaking, (
        f"in_breaking={in_breaking} but BREAKING.md {'does not quote' if in_breaking else 'quotes'} {literal!r}"
    )
    for doc in docs:
        assert literal in (_REPO_ROOT / doc).read_text(encoding="utf-8"), (
            f"{doc} must quote the wire text {literal!r} verbatim"
        )

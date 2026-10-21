"""``GET /status`` ``model_validation.provider_warnings`` never carries a probe exception.

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` or job
``error`` field for a caught exception whose message embeds internals. The warning list
``validate_providers_shared`` builds is copied verbatim into ``ModelValidationResult.
provider_warnings``, which ``GET /status`` serves as ``model_validation.provider_warnings``
(``routes_status._build_model_validation_status``). Its per-probe handlers catch the
fastembed / onnxruntime exception, which can name model files under the data dir, so
each must log it with its traceback and publish only its fixed warning text.

Each case boots a real app whose lifespan runs the real background validation with one
probe raising (or the validation task itself crashing into its fallback), then reads
``GET /status``. The def/ref parse-failure warning (the other wire-facing warning list) is
read here from an ingest job's ``result["warnings"]`` (``GET /jobs/{id}``) and through the
MCP ``ingest_file`` tool in ``tests/server/test_no_internal_error_leak_mcp.py``.
"""
from __future__ import annotations

import logging
import time
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search import defref_extractor, model_validation
from archon_search.server import app as app_module
from tests._internal_error_leak import (
    DEFREF_PARSE_MSG,
    LANCE_MSG,
    LEAK_NEEDLE,
    ONNX_MSG,
    assert_logged_with_traceback,
    defref_pipeline,
    write_defref_source,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_VALIDATION_POLL_DEADLINE_S = 10.0
_VALIDATION_POLL_INTERVAL_S = 0.05

_CPU_SELECTION = SimpleNamespace(providers=None, unavailable=[])

# The three probes validate_providers_shared runs, and the fixed warning each failure publishes.
_PROBE_WARNINGS: dict[str, str] = {
    "select_onnx_providers": model_validation.ONNX_PROVIDER_QUERY_FAILED_WARNING,
    "TextEmbedding": model_validation.EMBEDDER_PROBE_FAILED_WARNING,
    "_load_cross_encoder": model_validation.RERANKER_PROBE_FAILED_WARNING,
}


def _benign_probes() -> dict[str, MagicMock]:
    return {
        "select_onnx_providers": MagicMock(return_value=_CPU_SELECTION),
        "TextEmbedding": MagicMock(),
        "_load_cross_encoder": MagicMock(),
    }


def _completed_model_validation(client: Any, api_key: str) -> tuple[dict[str, Any], str]:
    """Poll ``GET /status`` until the lifespan's background validation has published a result."""
    deadline = time.monotonic() + _VALIDATION_POLL_DEADLINE_S
    while time.monotonic() < deadline:
        response = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert response.status_code == HTTPStatus.OK, response.text
        validation = response.json()["model_validation"]
        if validation["validated_at"] is not None:
            return validation, response.text
        time.sleep(_VALIDATION_POLL_INTERVAL_S)
    pytest.fail(f"model validation did not complete within {_VALIDATION_POLL_DEADLINE_S}s")


@pytest.mark.parametrize("failing_probe", list(_PROBE_WARNINGS))
def test_get_status_provider_warnings_do_not_echo_probe_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_probe: str,
) -> None:
    probes = _benign_probes()
    probes[failing_probe] = MagicMock(side_effect=RuntimeError(ONNX_MSG))
    for attr, mock in probes.items():
        monkeypatch.setattr(model_validation, attr, mock)

    with (
        caplog.at_level(logging.DEBUG, logger=model_validation.__name__),
        make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key),
    ):
        validation, wire = _completed_model_validation(client, api_key)

    probes[failing_probe].assert_called()
    assert _PROBE_WARNINGS[failing_probe] in validation["provider_warnings"], validation
    assert LEAK_NEEDLE not in wire, f"internal model path leaked into GET /status: {validation!r}"
    # An expected probe failure keeps its cause in the WARNING text and its traceback at DEBUG.
    probe_records = [r for r in caplog.records if r.name == model_validation.__name__ and ONNX_MSG in r.getMessage()]
    assert [r.levelno for r in probe_records] == [logging.WARNING], [r.getMessage() for r in caplog.records]
    assert any(
        r.levelno == logging.DEBUG and r.exc_info is not None and str(r.exc_info[1]) == ONNX_MSG
        for r in caplog.records
    ), "the probe traceback must be logged at DEBUG"


_DEFREF_COLLECTION = "defref-col"


def test_get_status_validation_task_crash_publishes_fixed_reason_and_logs_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``validate_models_async`` itself raising lands in the task's fallback: fixed reason on
    ``GET /status``, the cause in the log with its traceback."""
    crash = AsyncMock(side_effect=RuntimeError(ONNX_MSG))
    monkeypatch.setattr(app_module, "validate_models_async", crash)

    with caplog.at_level(logging.WARNING), make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        validation, wire = _completed_model_validation(client, api_key)

    crash.assert_awaited_once()
    assert model_validation.VALIDATION_TASK_FAILED_WARNING in validation["provider_warnings"], validation
    assert LEAK_NEEDLE not in wire, f"internal model path leaked into GET /status: {validation!r}"
    assert_logged_with_traceback(caplog, ONNX_MSG)


def test_get_status_graph_ner_check_crash_in_fallback_publishes_fixed_warning_and_logs_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``failed_result``'s own ``graph_ner_status`` call raising still yields a fixed warning,
    and its cause is logged with its traceback, not swallowed."""
    monkeypatch.setattr(app_module, "validate_models_async", AsyncMock(side_effect=RuntimeError(ONNX_MSG)))
    graph_check = MagicMock(side_effect=RuntimeError(LANCE_MSG))
    monkeypatch.setattr(model_validation, "graph_ner_status", graph_check)

    with caplog.at_level(logging.WARNING), make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        validation, wire = _completed_model_validation(client, api_key)

    graph_check.assert_called_once()
    assert validation["provider_warnings"] == [
        model_validation.GRAPH_NER_UNDETERMINED_WARNING, model_validation.VALIDATION_TASK_FAILED_WARNING,
    ], validation
    assert LEAK_NEEDLE not in wire, f"internal path leaked into GET /status: {validation!r}"
    assert_logged_with_traceback(caplog, LANCE_MSG)


def test_ingest_job_result_warnings_do_not_echo_defref_parse_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``POST /ingest`` -> ``GET /jobs/{id}`` ``result["warnings"]`` (routes_jobs) carries the
    def/ref parse failure as fixed text naming the file, never the parser's exception."""
    source = write_defref_source(tmp_path)
    walk = MagicMock(side_effect=RuntimeError(DEFREF_PARSE_MSG))
    monkeypatch.setattr(defref_extractor, "_parse_and_walk", walk)

    with caplog.at_level(logging.WARNING), make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.pipeline = defref_pipeline(client.app.state.search_store)
        job_id = ingest_file_via_path(client, _DEFREF_COLLECTION, str(source), api_key=api_key)
        response = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})

    walk.assert_called_once()
    assert response.status_code == HTTPStatus.OK, response.text
    expected = defref_extractor.DEFREF_PARSE_FAILED_WARNING.format(file_path=str(source.resolve()))
    assert expected in response.json()["result"]["warnings"], response.text
    assert LEAK_NEEDLE not in response.text, f"def/ref parser exception leaked into GET /jobs: {response.text!r}"
    assert_logged_with_traceback(caplog, DEFREF_PARSE_MSG)

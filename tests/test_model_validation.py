"""Tests for archon_search.model_validation."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from archon_search.config import GraphConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.model_validation import (
    EMBEDDER_PROBE_FAILED_WARNING,
    EXTRACTION_MODEL_PROBE_ERROR_WARNING,
    GRAPH_NER_UNDETERMINED_WARNING,
    RERANKER_PROBE_FAILED_WARNING,
    VALIDATION_FAILED_WARNING,
    VALIDATION_TIMED_OUT_TEMPLATE,
    ModelValidationError,
    ModelValidationResult,
    _probe_extraction_model,
    graph_ner_status,
    validate_device,
    validate_embedding_model,
    validate_models_async,
)
from archon_search.server.app import create_app
from tests._internal_error_leak import ONNX_MSG, assert_logged_with_traceback


@pytest.mark.asyncio
async def test_dimension_comes_from_spec_without_loading():
    """The dimension is the spec's; no weights are loaded."""
    with patch("archon_search.torch_runtime.load_encoder") as load_encoder:
        dims = {
            name: await validate_embedding_model(name)
            for name in ("BAAI/bge-small-en-v1.5", "BAAI/bge-large-en-v1.5", "intfloat/multilingual-e5-large")
        }

    assert dims == {
        "BAAI/bge-small-en-v1.5": 384,
        "BAAI/bge-large-en-v1.5": 1024,
        "intfloat/multilingual-e5-large": 1024,
    }
    load_encoder.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_model_is_a_validation_error():
    """An unknown name raises ModelValidationError (the 422 / MCP-validation family) listing the supported names."""
    with pytest.raises(ModelValidationError) as excinfo:
        await validate_embedding_model("custom/unknown-model")

    message = str(excinfo.value)
    assert message.startswith("unsupported model 'custom/unknown-model'; supported: ")
    assert "BAAI/bge-small-en-v1.5" in message
    assert "intfloat/multilingual-e5-large" in message


def test_route_returns_422_for_unsupported_model(tmp_path: Path):
    """POST /collections maps the unsupported-model error to 422 with the supported list in the detail."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = client.post(
        "/collections/", json={"path": str(tmp_path), "embedding_model": "custom/unknown-model"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("unsupported model 'custom/unknown-model'; supported: ")


# ---------------------------------------------------------------------------
# validate_device — synchronous, never raises
# ---------------------------------------------------------------------------

_EMBEDDER = "BAAI/bge-small-en-v1.5"
_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"
_VALIDATE_DEVICE = "archon_search.model_validation.validate_device"
_RESOLVE_DEVICE = "archon_search.model_validation.resolve_device"
_PROBE_TEXT = "archon search validation probe"
_PROBE_PAIR = ("archon search query", "archon search document")
_TRICKLE_SECONDS = 2.0  # a GET that stays open far past any bound under test
_SHORT_PROBE_BOUND_SECONDS = 0.05


class _Probes:
    """Records what ``validate_device`` constructs and runs on its model backends."""

    def __init__(self) -> None:
        self.embedders: list[tuple[str, str]] = []
        self.rerankers: list[tuple[str, str]] = []
        self.encoded: list[list[str]] = []
        self.scored: list[list[tuple[str, str]]] = []
        self.embed_error: Exception | None = None
        self.rerank_error: Exception | None = None


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> _Probes:
    """Replace the backends ``validate_device`` loads with recording fakes."""
    recorder = _Probes()

    class _FakeEmbedder:
        def __init__(self, model_name: str, device: str = "auto") -> None:
            recorder.embedders.append((model_name, device))

        def encode(self, texts: list[str]) -> list[list[float]]:
            recorder.encoded.append(texts)
            if recorder.embed_error is not None:
                raise recorder.embed_error
            return [[0.0] for _ in texts]

    class _FakeReranker:
        def __init__(self, model_name: str, device: str = "auto") -> None:
            recorder.rerankers.append((model_name, device))

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            recorder.scored.append(pairs)
            if recorder.rerank_error is not None:
                raise recorder.rerank_error
            return [0.5 for _ in pairs]

    monkeypatch.setattr("archon_search.model_validation.ModelEmbedder", _FakeEmbedder)
    monkeypatch.setattr("archon_search.model_validation.ModelReranker", _FakeReranker)
    return recorder


def test_probe_runs_one_inference_per_model(probes: _Probes) -> None:
    result = validate_device("cpu", _EMBEDDER, _RERANKER)

    assert result == (True, True, [])
    assert probes.encoded == [[_PROBE_TEXT]]
    assert probes.scored == [[_PROBE_PAIR]]


@pytest.mark.parametrize("device", ["auto", "cpu"])
def test_probes_load_both_models_on_the_requested_device(probes: _Probes, device: str) -> None:
    validate_device(device, _EMBEDDER, _RERANKER)

    assert probes.embedders == [(_EMBEDDER, device)]
    assert probes.rerankers == [(_RERANKER, device)]


@pytest.mark.parametrize("requested", ["mps", "cuda"])
def test_unavailable_device_warns_and_uses_cpu(probes: _Probes, requested: str) -> None:
    with patch(_RESOLVE_DEVICE, return_value="cpu") as resolve:
        result = validate_device(requested, _EMBEDDER, _RERANKER)

    resolve.assert_called_once_with(requested)
    assert result == (True, True, [f"device unavailable: {requested}; models loaded on cpu"])


def test_available_accelerator_publishes_no_warning(probes: _Probes) -> None:
    with patch(_RESOLVE_DEVICE, return_value="mps"):
        result = validate_device("mps", _EMBEDDER, _RERANKER)

    assert result == (True, True, [])


@pytest.mark.parametrize("setting", ["auto", "cpu"])
def test_auto_and_cpu_never_step_down(probes: _Probes, setting: str) -> None:
    with patch(_RESOLVE_DEVICE, side_effect=AssertionError("must not resolve")):
        result = validate_device(setting, _EMBEDDER, _RERANKER)

    assert result == (True, True, [])


def test_device_resolution_failure_does_not_raise_and_probes_still_decide(probes: _Probes) -> None:
    with patch(_RESOLVE_DEVICE, side_effect=RuntimeError(ONNX_MSG)):
        result = validate_device("cuda", _EMBEDDER, _RERANKER)

    assert result == (True, True, [])


def test_empty_embedding_model_skips_the_embedder_probe(probes: _Probes) -> None:
    result = validate_device("cpu", "", _RERANKER)

    assert result == (True, True, [])
    assert probes.embedders == []
    assert probes.encoded == []


def test_empty_reranker_model_disables_the_reranker_probe(probes: _Probes) -> None:
    result = validate_device("cpu", _EMBEDDER, "")

    assert result == (True, True, [])
    assert probes.rerankers == []
    assert probes.scored == []


def test_probe_failure_text_is_fixed_for_the_embedder(probes: _Probes) -> None:
    probes.embed_error = RuntimeError(ONNX_MSG)

    result = validate_device("cpu", _EMBEDDER, _RERANKER)

    assert result == (False, True, ["embedder probe failed; see the log for details"])


def test_probe_failure_text_is_fixed_for_the_reranker(probes: _Probes) -> None:
    probes.rerank_error = RuntimeError(ONNX_MSG)

    result = validate_device("cpu", _EMBEDDER, _RERANKER)

    assert result == (True, False, ["reranker probe failed; see the log for details"])


def test_both_probes_failing_reports_both(probes: _Probes) -> None:
    probes.embed_error = RuntimeError("embed-boom")
    probes.rerank_error = RuntimeError("rerank-boom")

    result = validate_device("cpu", _EMBEDDER, _RERANKER)

    assert result == (
        False,
        False,
        [EMBEDDER_PROBE_FAILED_WARNING, RERANKER_PROBE_FAILED_WARNING],
    )


def test_a_failed_probe_after_a_step_down_reports_both_warnings(probes: _Probes) -> None:
    probes.rerank_error = RuntimeError(ONNX_MSG)

    with patch(_RESOLVE_DEVICE, return_value="cpu"):
        result = validate_device("mps", _EMBEDDER, _RERANKER)

    assert result == (
        True,
        False,
        ["device unavailable: mps; models loaded on cpu", RERANKER_PROBE_FAILED_WARNING],
    )


def test_unsupported_model_name_fails_the_probe_without_raising() -> None:
    """The real backends reject a name outside the registry at construction."""
    result = validate_device("cpu", "custom/unknown-model", "")

    assert result == (False, True, [EMBEDDER_PROBE_FAILED_WARNING])


class _SharedBackends:
    """Serving backends handed to the probe: they record calls instead of loading weights."""

    def __init__(self) -> None:
        self.encoded: list[list[str]] = []
        self.scored: list[list[tuple[str, str]]] = []
        self.embed_error: Exception | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encoded.append(texts)
        if self.embed_error is not None:
            raise self.embed_error
        return [[0.0] for _ in texts]

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.scored.append(pairs)
        return [0.5 for _ in pairs]


def test_supplied_backends_are_probed_and_no_second_copy_is_built(probes: _Probes) -> None:
    """The probe runs the serving instances: a throwaway copy doubles the weights in memory at startup."""
    shared = _SharedBackends()

    result = validate_device("cpu", _EMBEDDER, _RERANKER, embedder=shared, reranker=shared)

    assert result == (True, True, [])
    assert shared.encoded == [[_PROBE_TEXT]]
    assert shared.scored == [[_PROBE_PAIR]]
    assert probes.embedders == []
    assert probes.rerankers == []


def test_a_failing_supplied_backend_reports_the_fixed_warning(probes: _Probes) -> None:
    shared = _SharedBackends()
    shared.embed_error = RuntimeError(ONNX_MSG)

    result = validate_device("cpu", _EMBEDDER, _RERANKER, embedder=shared, reranker=shared)

    assert result == (False, True, [EMBEDDER_PROBE_FAILED_WARNING])
    assert probes.embedders == []


def test_a_disabled_reranker_is_not_probed_even_when_a_backend_is_supplied(probes: _Probes) -> None:
    shared = _SharedBackends()

    result = validate_device("cpu", _EMBEDDER, "", embedder=shared, reranker=shared)

    assert result == (True, True, [])
    assert shared.scored == []


_PROBE_FAILURES = [
    pytest.param(
        "embed_error",
        f"embedder probe failed for {_EMBEDDER} on device cpu: ",
        id="embedder-probe",
    ),
    pytest.param(
        "rerank_error",
        f"reranker probe failed for {_RERANKER} on device cpu: ",
        id="reranker-probe",
    ),
]


@pytest.mark.parametrize(("which", "warning_prefix"), _PROBE_FAILURES)
def test_probe_failure_logs_message_at_warning_and_traceback_only_at_debug(
    caplog: pytest.LogCaptureFixture, probes: _Probes, which: str, warning_prefix: str
) -> None:
    """An expected probe failure (e.g. an unloadable model) must not log its traceback at
    WARNING: the install wizard configures no logging, so Python's last-resort handler would
    print it to the user. The cause stays in the WARNING text, the traceback at DEBUG."""
    setattr(probes, which, RuntimeError(ONNX_MSG))

    with caplog.at_level(logging.DEBUG, logger="archon_search.model_validation"):
        validate_device("cpu", _EMBEDDER, _RERANKER)

    own = [r for r in caplog.records if r.name == "archon_search.model_validation"]
    warnings = [r for r in own if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [f"{warning_prefix}{ONNX_MSG}"]
    assert warnings[0].exc_info is None, "the WARNING must not carry the traceback"
    tracebacks = [r for r in own if r.levelno == logging.DEBUG and r.exc_info is not None]
    assert [str(r.exc_info[1]) for r in tracebacks] == [ONNX_MSG], [r.getMessage() for r in own]
    assert [r.getMessage() for r in tracebacks] == [warning_prefix.removesuffix(": ")]


# ---------------------------------------------------------------------------
# BE-1: validate_models_async (C1) — async wrapper, never raises
# ---------------------------------------------------------------------------


def _cfg(**kw) -> SearchConfig:
    return SearchConfig(
        embedding_model=kw.get("embedding_model", _EMBEDDER),
        reranker_model=kw.get("reranker_model", _RERANKER),
        device=kw.get("device", "cpu"),
    )


@pytest.mark.asyncio
async def test_validate_models_async_both_ok():
    with patch(_VALIDATE_DEVICE, return_value=(True, True, [])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is True
    assert result.reranker_ok is True
    assert result.provider_warnings == []
    assert result.validated_at is not None


@pytest.mark.asyncio
async def test_validate_models_async_probes_the_configured_device_and_models():
    with patch(_VALIDATE_DEVICE, return_value=(True, True, [])) as probe:
        await validate_models_async(_cfg(device="mps"), timeout_seconds=5)
    probe.assert_called_once_with("mps", _EMBEDDER, _RERANKER, embedder=None, reranker=None)


@pytest.mark.asyncio
async def test_validate_models_async_embedder_fails():
    with patch(_VALIDATE_DEVICE, return_value=(False, True, ["embedder load failed"])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is False
    assert result.reranker_ok is True
    assert result.provider_warnings == ["embedder load failed"]


@pytest.mark.asyncio
async def test_validate_models_async_reranker_fails():
    with patch(_VALIDATE_DEVICE, return_value=(True, False, ["reranker load failed"])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is True
    assert result.reranker_ok is False


@pytest.mark.asyncio
async def test_validate_models_async_publishes_the_device_step_down_warning():
    """The step-down is the WARN signal: both models loaded, one warning."""
    with patch(_RESOLVE_DEVICE, return_value="cpu"), patch(
        "archon_search.model_validation.ModelEmbedder"
    ), patch("archon_search.model_validation.ModelReranker"):
        result = await validate_models_async(_cfg(device="mps"), timeout_seconds=5)
    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert result.provider_warnings == ["device unavailable: mps; models loaded on cpu"]


@pytest.mark.asyncio
async def test_validate_models_async_fail_with_warnings():
    """FAIL precedence: embedder fails AND a warning is present."""
    with patch(_VALIDATE_DEVICE, return_value=(False, False, ["device warning", "embedder failed"])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings == ["device warning", "embedder failed"]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_models_async_timeout():
    def _slow(*_a, **_k):
        import time
        time.sleep(1.0)
        return (True, True, [])

    with patch(_VALIDATE_DEVICE, side_effect=_slow):
        # Pass a sub-second timeout_seconds directly (config's float
        # validation_timeout_seconds accepts fractions too). 10x margin
        # (0.1s vs 1.0s) avoids CI flakiness.
        result = await validate_models_async(_cfg(), timeout_seconds=0.1)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings == [VALIDATION_TIMED_OUT_TEMPLATE.format(seconds=0.1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeout_seconds", "rendered"), [(60.0, "60s"), (1000000.0, "1000000s")])
async def test_validate_models_async_timeout_message_renders_whole_seconds(
    timeout_seconds: float, rendered: str
) -> None:
    """Whole-second floats render without ".0" or an exponent: 60.0 -> "60s" (the form the docs quote), 1e6 -> "1000000s"."""

    async def _timed_out(coro, timeout):
        coro.close()  # never awaited — close it so no "coroutine was never awaited" RuntimeWarning leaks
        raise TimeoutError

    with patch.object(asyncio, "wait_for", side_effect=_timed_out):
        result = await validate_models_async(_cfg(), timeout_seconds=timeout_seconds)
    assert f"validation timed out after {rendered}" in result.provider_warnings


@pytest.mark.asyncio
async def test_validate_models_async_reranker_disabled():
    with patch(_VALIDATE_DEVICE, return_value=(True, True, [])) as probe:
        result = await validate_models_async(_cfg(reranker_model=""), timeout_seconds=5)
    assert result.reranker_ok is True
    assert probe.call_args.args[2] == ""


@pytest.mark.asyncio
async def test_validate_models_async_probes_the_supplied_serving_backends():
    """The caller's warm instances are what get probed — the embedder is no longer skipped when warm."""
    embedder, reranker = _SharedBackends(), _SharedBackends()
    with patch(_VALIDATE_DEVICE, return_value=(True, True, [])) as probe:
        result = await validate_models_async(_cfg(), timeout_seconds=5, embedder=embedder, reranker=reranker)
    assert result.embedder_ok is True
    probe.assert_called_once_with("cpu", _EMBEDDER, _RERANKER, embedder=embedder, reranker=reranker)


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_models_async_never_raises():
    with patch(_VALIDATE_DEVICE, side_effect=RuntimeError("unexpected")):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings == [VALIDATION_FAILED_WARNING]


@pytest.mark.asyncio
async def test_validate_models_async_cancelled_returns_result():
    """CancelledError (BaseException) is caught → returns a result, never re-raises."""
    with patch(_VALIDATE_DEVICE, side_effect=asyncio.CancelledError):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings == [VALIDATION_FAILED_WARNING]


@pytest.mark.asyncio
async def test_validate_models_async_propagates_task_cancellation(caplog: pytest.LogCaptureFixture):
    """A cancelled validation task (server shutdown mid-probe) ends cancelled, silently — no WARNING, no fake result."""
    probe_started = threading.Event()
    release_probe = threading.Event()

    def blocking_probe(*_args: object, **_kwargs: object) -> tuple[bool, bool, list[str]]:
        probe_started.set()
        release_probe.wait(timeout=10)
        return True, True, []

    with patch(_VALIDATE_DEVICE, side_effect=blocking_probe), caplog.at_level(logging.WARNING):
        task = asyncio.create_task(validate_models_async(_cfg(), timeout_seconds=30))
        try:
            await asyncio.to_thread(probe_started.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release_probe.set()

    assert task.cancelled()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# BE-9: non-blocking llama-server reachability probe (S7, S17, S25)
# ---------------------------------------------------------------------------


def _llama_cpp_cfg(**kw) -> SearchConfig:
    """SearchConfig with [hyde] enabled on provider "llama_cpp" — triggers the probe."""
    cfg = _cfg()
    cfg.hyde.enabled = True
    cfg.hyde.provider = "llama_cpp"
    cfg.hyde.llama_cpp_base_url = kw.get("llama_cpp_base_url", "http://localhost:8080")
    return cfg


def _make_async_client_cls(response: MagicMock | None = None, get_side_effect=None) -> MagicMock:
    """Mock class standing in for ``httpx.AsyncClient`` (grounding note: patch
    ``httpx.AsyncClient`` directly, not ``httpx.MockTransport``)."""
    mock_client = AsyncMock()
    if get_side_effect is not None:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
    else:
        mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_client)


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_probe_sets_llama_cpp_ok_true():
    """Reachable llama-server (GET /v1/models succeeds) → llama_cpp_ok=True."""
    mock_cls = _make_async_client_cls(response=_ok_response())
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is True


@pytest.mark.asyncio
async def test_probe_sets_llama_cpp_ok_false():
    """Unreachable llama-server (httpx.ConnectError) → llama_cpp_ok=False."""
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is False


@pytest.mark.asyncio
async def test_probe_pending_is_none():
    """llama_cpp not configured as provider anywhere → llama_cpp_ok stays None."""
    with patch(_VALIDATE_DEVICE, return_value=(True, True, [])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is None


@pytest.mark.asyncio
async def test_probe_failure_does_not_block_boot(caplog: pytest.LogCaptureFixture):
    """Probe ConnectError never raises — validate_models_async returns normally and logs one "not reachable" WARNING."""
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.WARNING):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)

    assert isinstance(result, ModelValidationResult)
    assert result.llama_cpp_ok is False
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "localhost:8080" in r.getMessage()]
    assert "not reachable" in warning
    assert "ConnectError" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:s3cr3t@gpu.test:8080/?token=t0k3n",
        "http://user:s3cr3t/x@gpu.test:8080",
        "http://user:s3cr3t?x@gpu.test:8080",
        "http://user:s3cr3t#x@gpu.test:8080",
        "http://user:12345/x@gpu.test:80",
        "http://user/x@gpu.test:80",
        "http://k3y?en@gpu.test:8080",
    ],
)
async def test_probe_failure_logs_never_carry_the_credentials_of_the_url(base_url: str, caplog: pytest.LogCaptureFixture):
    """Neither the WARNING nor the DEBUG line (message and traceback) holds a piece of the userinfo or the query."""
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.DEBUG):
        await validate_models_async(_llama_cpp_cfg(llama_cpp_base_url=base_url), timeout_seconds=5)

    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.startswith("llama_cpp: the server at ")
    logged = caplog.text + "".join(logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info)
    for secret in ("s3cr3t", "t0k3n", "user", "12345", "k3y"):
        assert secret not in logged, secret


@pytest.mark.asyncio
async def test_probe_failure_warning_is_the_shared_text_of_the_client_logger(caplog: pytest.LogCaptureFixture):
    """Deliberate: the one line every path shares is attributed to the client's logger, not to this module's."""
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.WARNING):
        await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.name == "archon_search.llm_client.llama_cpp_client"
    assert record.getMessage() == (
        "llama_cpp: the server at localhost:8080 is not reachable (ConnectError); requests to it fail until it answers, "
        "and where its context window could not be read, 4096 tokens are assumed"
    )


def _http_error_response(status: int) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=httpx.Request("GET", "http://x"), response=httpx.Response(status))
    )
    return response


@pytest.mark.asyncio
async def test_probe_http_error_reason_is_the_status_code_only(caplog: pytest.LogCaptureFixture):
    """A non-2xx answer reads ``HTTP <code>``: no server text reaches the line (this probe has no body to bound)."""
    mock_cls = _make_async_client_cls(response=_http_error_response(503))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.WARNING):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)

    assert result.llama_cpp_ok is False
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "is not reachable (HTTP 503);" in warning


@pytest.mark.asyncio
async def test_probe_failure_keeps_the_exception_types_at_debug(caplog: pytest.LogCaptureFixture):
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.DEBUG, logger="archon_search.llm_client.llama_cpp_client"):
        await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)

    [debug] = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == "llama_cpp: probe of localhost:8080 failed (ConnectError)"


@pytest.mark.asyncio
async def test_a_trickling_server_is_cut_off_by_an_overall_deadline_and_reads_unreachable(caplog: pytest.LogCaptureFixture):
    """httpx limits each phase, not the request: a server that keeps the GET open must still end as ``False`` + one WARNING."""
    async def trickle(*_args: object, **_kwargs: object) -> MagicMock:
        await asyncio.sleep(_TRICKLE_SECONDS)
        return _ok_response()

    mock_cls = _make_async_client_cls(get_side_effect=trickle)
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch("archon_search.model_validation._LLAMA_CPP_PROBE_TIMEOUT_SECONDS", _SHORT_PROBE_BOUND_SECONDS), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         caplog.at_level(logging.WARNING):
        result = await asyncio.wait_for(validate_models_async(_llama_cpp_cfg(), timeout_seconds=5), _TRICKLE_SECONDS / 2)

    assert result.llama_cpp_ok is False
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "is not reachable (TimeoutError)" in warning


# The probe asks every llama-server an ENABLED feature uses, once per ``host:port``: a disabled section is not a server
# anyone uses, and a second server is not hidden behind the first.


def _servers_client_cls(*, down: frozenset[str] = frozenset()) -> MagicMock:
    """Stands in for ``httpx.AsyncClient``: a client built for a base URL in *down* fails its GET, any other answers 200."""
    def build(*, base_url: str, **_: object) -> AsyncMock:
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused")) if base_url in down else AsyncMock(return_value=_ok_response())
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    return MagicMock(side_effect=build)


def _probed_urls(mock_cls: MagicMock) -> list[str]:
    return [call.kwargs["base_url"] for call in mock_cls.call_args_list]


async def _validate(config: SearchConfig, mock_cls: MagicMock, **kwargs) -> ModelValidationResult:
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch(_VALIDATE_DEVICE, return_value=(True, True, [])), \
         patch("archon_search.model_validation.graph_ner_status", return_value=[]):
        return await validate_models_async(config, timeout_seconds=5, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["hyde", "rag_fusion", "graph", "description"])
async def test_a_disabled_llama_cpp_section_is_not_probed_and_warns_nothing(section: str, caplog: pytest.LogCaptureFixture):
    config = _cfg()
    target = getattr(config, section)
    target.enabled = False
    target.provider = "llama_cpp"
    target.llama_cpp_base_url = "http://down.test:8080"
    mock_cls = _servers_client_cls(down=frozenset({"http://down.test:8080"}))

    with caplog.at_level(logging.WARNING):
        result = await _validate(config, mock_cls)

    assert result.llama_cpp_ok is None
    assert _probed_urls(mock_cls) == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["rag_fusion", "graph", "description"])
async def test_each_enabled_llama_cpp_section_is_probed(section: str):
    config = _cfg()
    target = getattr(config, section)
    target.enabled = True
    target.provider = "llama_cpp"
    target.llama_cpp_base_url = "http://gpu.test:9000"
    mock_cls = _servers_client_cls()

    result = await _validate(config, mock_cls)

    assert _probed_urls(mock_cls) == ["http://gpu.test:9000"]
    assert result.llama_cpp_ok is True


@pytest.mark.asyncio
async def test_a_disabled_section_does_not_hide_the_server_of_an_enabled_one():
    config = _cfg()
    config.hyde.provider = "llama_cpp"  # disabled, default URL
    config.rag_fusion.enabled = True
    config.rag_fusion.provider = "llama_cpp"
    config.rag_fusion.llama_cpp_base_url = "http://gpu.test:9000"
    mock_cls = _servers_client_cls()

    await _validate(config, mock_cls)

    assert _probed_urls(mock_cls) == ["http://gpu.test:9000"]


@pytest.mark.asyncio
async def test_features_on_one_server_probe_it_once_whatever_the_spelling(caplog: pytest.LogCaptureFixture):
    config = _cfg()
    for section, url in (("hyde", "http://GPU.test:8080"), ("rag_fusion", "http://gpu.test.:8080/v1"), ("description", "http://u:p@gpu.test:8080")):
        target = getattr(config, section)
        target.enabled = True
        target.provider = "llama_cpp"
        target.llama_cpp_base_url = url
    mock_cls = _servers_client_cls(down=frozenset({"http://GPU.test:8080"}))

    with caplog.at_level(logging.WARNING):
        result = await _validate(config, mock_cls)

    assert _probed_urls(mock_cls) == ["http://GPU.test:8080"]
    assert result.llama_cpp_ok is False
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def _two_server_config(*, graph_url: str = "http://graph.test:9000") -> SearchConfig:
    config = _cfg()
    config.hyde.enabled = True
    config.hyde.provider = "llama_cpp"
    config.hyde.llama_cpp_base_url = "http://hyde.test:8080"
    config.graph = GraphConfig(enabled=True, provider="llama_cpp", extraction_model="m", llama_cpp_base_url=graph_url)
    return config


def _graph_client() -> AsyncMock:
    client = AsyncMock()
    client.call = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_llama_cpp_ok_is_false_when_any_enabled_server_is_down_and_each_down_server_warns_once(caplog: pytest.LogCaptureFixture):
    mock_cls = _servers_client_cls(down=frozenset({"http://hyde.test:8080", "http://graph.test:9000"}))

    with caplog.at_level(logging.WARNING):
        result = await _validate(_two_server_config(), mock_cls, graph_client_factory=_graph_client)

    assert result.llama_cpp_ok is False
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 2
    assert sum("hyde.test:8080" in m for m in messages) == sum("graph.test:9000" in m for m in messages) == 1
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.asyncio
async def test_a_down_hyde_server_does_not_skip_the_graph_probe_of_a_live_graph_server(caplog: pytest.LogCaptureFixture):
    mock_cls = _servers_client_cls(down=frozenset({"http://hyde.test:8080"}))
    graph_client = _graph_client()

    with caplog.at_level(logging.WARNING):
        result = await _validate(_two_server_config(), mock_cls, graph_client_factory=lambda: graph_client)

    assert result.llama_cpp_ok is False
    graph_client.call.assert_awaited_once()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


@pytest.mark.asyncio
async def test_a_down_graph_server_is_one_warning_and_no_second_one_from_the_graph_probe(caplog: pytest.LogCaptureFixture):
    """hyde's server is up, the graph's is down: the graph probe must not call the dead server and log a traceback too."""
    mock_cls = _servers_client_cls(down=frozenset({"http://graph.test:9000"}))
    graph_client = _graph_client()
    graph_client.call.side_effect = RuntimeError("would be a second WARNING with a traceback")

    with caplog.at_level(logging.WARNING):
        result = await _validate(_two_server_config(), mock_cls, graph_client_factory=lambda: graph_client)

    assert result.llama_cpp_ok is False
    graph_client.call.assert_not_awaited()
    assert EXTRACTION_MODEL_PROBE_ERROR_WARNING not in result.provider_warnings
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "graph.test:9000" in warning.getMessage()
    assert warning.exc_info is None


@pytest.mark.asyncio
async def test_a_graph_on_the_same_server_as_a_down_hyde_is_skipped_by_label_not_by_spelling(caplog: pytest.LogCaptureFixture):
    mock_cls = _servers_client_cls(down=frozenset({"http://hyde.test:8080"}))
    graph_client = _graph_client()

    with caplog.at_level(logging.WARNING):
        await _validate(_two_server_config(graph_url="http://HYDE.test:8080/v1"), mock_cls, graph_client_factory=lambda: graph_client)

    graph_client.call.assert_not_awaited()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


# ---------------------------------------------------------------------------
# Startup-probe handlers publish fixed warnings, so the traceback is where the cause survives.
# ---------------------------------------------------------------------------


def _extraction_probe_config() -> SearchConfig:
    return SearchConfig(graph=GraphConfig(enabled=True, provider="ollama", extraction_model="m"))


def test_graph_ner_status_failure_is_logged_with_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = SearchConfig(graph=GraphConfig(enabled=True))

    with patch("importlib.util.find_spec", side_effect=RuntimeError(ONNX_MSG)) as find_spec, \
         caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        warnings = graph_ner_status(config)

    find_spec.assert_called()
    assert warnings == [GRAPH_NER_UNDETERMINED_WARNING]
    assert_logged_with_traceback(caplog, ONNX_MSG)


@pytest.mark.asyncio
async def test_extraction_probe_call_failure_is_logged_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    client = AsyncMock()
    client.call.side_effect = RuntimeError(ONNX_MSG)

    with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        warnings = await _probe_extraction_model(_extraction_probe_config(), graph_client_factory=lambda: client)

    client.call.assert_awaited_once()
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert_logged_with_traceback(caplog, ONNX_MSG)


@pytest.mark.asyncio
async def test_extraction_probe_client_build_failure_is_logged_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    build = MagicMock(side_effect=RuntimeError(ONNX_MSG))

    with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        warnings = await _probe_extraction_model(_extraction_probe_config(), graph_client_factory=build)

    build.assert_called_once()
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert_logged_with_traceback(caplog, ONNX_MSG)


# ---------------------------------------------------------------------------
# ModelValidationResult is an immutable value with a coherent timestamp
# ---------------------------------------------------------------------------


def test_model_validation_result_is_immutable() -> None:
    result = ModelValidationResult(embedder_ok=True, reranker_ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.embedder_ok = False  # type: ignore[misc]


def test_pending_model_validation_result_is_valid() -> None:
    result = ModelValidationResult()
    assert (result.embedder_ok, result.reranker_ok, result.validated_at) == (None, None, None)


@pytest.mark.parametrize(("embedder_ok", "reranker_ok"), [(None, None), (True, None), (None, False)])
def test_a_finished_validation_must_have_resolved_both_probes(
    embedder_ok: bool | None, reranker_ok: bool | None
) -> None:
    with pytest.raises(ValueError, match="validated_at"):
        ModelValidationResult(embedder_ok=embedder_ok, reranker_ok=reranker_ok, validated_at=datetime.now(UTC))


def test_a_finished_validation_with_both_probes_resolved_is_valid() -> None:
    stamp = datetime.now(UTC)
    result = ModelValidationResult(embedder_ok=False, reranker_ok=True, validated_at=stamp)
    assert result.validated_at == stamp

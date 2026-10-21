"""Tests for archon_search.model_validation."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.model_validation import (
    EMBEDDER_PROBE_FAILED_WARNING,
    EXTRACTION_MODEL_PROBE_ERROR_WARNING,
    GRAPH_NER_UNDETERMINED_WARNING,
    ONNX_PROVIDER_QUERY_FAILED_WARNING,
    RERANKER_PROBE_FAILED_WARNING,
    RERANKER_SPLIT_BUDGET_EXHAUSTED_WARNING,
    RERANKER_SPLIT_TIMED_OUT_TEMPLATE,
    VALIDATION_TIMED_OUT_TEMPLATE,
    ModelValidationError,
    ModelValidationResult,
    _probe_extraction_model,
    graph_ner_status,
    validate_embedding_model,
    validate_models_async,
    validate_providers_shared,
)
from tests._internal_error_leak import ONNX_MSG, assert_logged_with_traceback

_AVAILABLE_PROVIDERS = "archon_search.onnx_providers._available_providers"


@pytest.mark.asyncio
async def test_validate_known_model_returns_dim():
    """If the model is in list_supported_models, return its dim directly."""
    with patch("archon_search.model_validation.TextEmbedding") as mock_te:
        # Real fastembed descriptors key the model identifier under "model".
        mock_te.list_supported_models.return_value = [{"model": "model-X", "dim": 384}]
        result = await validate_embedding_model("model-X")
    assert result == 384


@pytest.mark.asyncio
async def test_validate_unknown_model_falls_back_to_instantiation():
    """If model is not in list, fall back to instantiating make_embedder."""
    mock_embedder = MagicMock()
    mock_embedder.embedding_dim = 512
    mock_embedder.embed = AsyncMock(return_value=[[0.0] * 512])

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.return_value = []
        result = await validate_embedding_model("custom-model")

    assert result == 512
    mock_embedder.embed.assert_awaited_once_with(["probe"])


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_timeout_raises_model_validation_error():
    """If the embed probe times out, raise ModelValidationError."""
    mock_embedder = MagicMock()
    # AsyncMock raises TimeoutError when awaited — the real wait_for propagates it.
    # Do NOT mock asyncio.wait_for directly: it would leak the inner coroutine (RuntimeWarning).
    mock_embedder.embed = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.return_value = []
        with pytest.raises(ModelValidationError) as exc_info:
            await validate_embedding_model("slow-model")

    assert "could not determine model output dimension" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_list_supported_models_unavailable_falls_back():
    """If list_supported_models raises AttributeError, fall back to instantiation."""
    mock_embedder = MagicMock()
    mock_embedder.embedding_dim = 256
    mock_embedder.embed = AsyncMock(return_value=[[0.0] * 256])

    with patch("archon_search.model_validation.TextEmbedding") as mock_te, \
         patch("archon_search.model_validation.make_embedder", return_value=mock_embedder):
        mock_te.list_supported_models.side_effect = AttributeError
        result = await validate_embedding_model("custom-model")

    assert result == 256
    mock_embedder.embed.assert_awaited_once_with(["probe"])


# ---------------------------------------------------------------------------
# BE-1: validate_providers_shared (C3) — synchronous, never raises
# ---------------------------------------------------------------------------


def _patch_probes(*, embedder_exc=None, reranker_exc=None, available=None):
    """Helper: patch the embedder/reranker probe entry points + onnxruntime."""
    te = MagicMock()
    if embedder_exc is not None:
        te.side_effect = embedder_exc
    else:
        instance = MagicMock()
        instance.embed.return_value = iter([[0.0]])
        te.return_value = instance

    ce = MagicMock()
    if reranker_exc is not None:
        ce.side_effect = reranker_exc
    else:
        rinstance = MagicMock()
        rinstance.rerank.return_value = iter([0.0])
        ce.return_value = rinstance

    avail = available if available is not None else ["CPUExecutionProvider"]
    return te, ce, avail


def test_validate_providers_shared_both_ok():
    te, ce, avail = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is True
    assert rer_ok is True
    assert warnings == []


def test_validate_providers_shared_reranker_disabled():
    te, ce, avail = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", ""
        )
    assert emb_ok is True
    assert rer_ok is True
    ce.assert_not_called()


def test_validate_providers_shared_embedder_fails():
    te, ce, avail = _patch_probes(embedder_exc=RuntimeError("boom"))
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is False
    assert rer_ok is True
    assert any("boom" in w or "embedder" in w.lower() for w in warnings)


def test_validate_providers_shared_reranker_fails():
    te, ce, avail = _patch_probes(reranker_exc=RuntimeError("rerank-boom"))
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is True
    assert rer_ok is False
    assert any("rerank-boom" in w or "reranker" in w.lower() for w in warnings)


def test_validate_providers_shared_provider_unavailable_probes_both_on_cpu():
    """S635: non-CPU provider missing → both probes run on CPU, ok, one warning names it."""
    te, ce, avail = _patch_probes(available=["CPUExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CoreMLExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is True
    assert rer_ok is True
    assert warnings == ["configured ONNX providers not available: CoreMLExecutionProvider"]
    # Both models are probed on the providers the host has — never the missing one.
    assert te.call_args.kwargs["providers"] == ["CPUExecutionProvider"]
    assert ce.call_args.args[1] == ["CPUExecutionProvider"]


def test_validate_providers_shared_provider_unavailable_probe_failure_still_reported():
    """S635: the CPU fallback probe failing is a probe failure, reported after the warning."""
    te, ce, avail = _patch_probes(
        embedder_exc=RuntimeError("emb-boom"), available=["CPUExecutionProvider"]
    )
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CUDAExecutionProvider", "CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is False
    assert rer_ok is True
    assert warnings == [
        "configured ONNX providers not available: CUDAExecutionProvider",
        EMBEDDER_PROBE_FAILED_WARNING,
    ], "the unavailable-provider warning must precede the probe failure"


def test_validate_providers_shared_provider_unavailable_reranker_disabled():
    """S7 ∩ S8: provider missing + reranker disabled → embedder probed on CPU, reranker skipped."""
    te, ce, avail = _patch_probes(available=["CPUExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CoreMLExecutionProvider"], "emb-model", ""
        )
    assert emb_ok is True
    assert rer_ok is True
    assert warnings == ["configured ONNX providers not available: CoreMLExecutionProvider"]
    ce.assert_not_called()


def test_validate_providers_shared_embedder_empty_skips_probe():
    """embedding_model="" → embedder probe skipped, embedder_ok=True (warm path)."""
    te, ce, avail = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "", "rer-model"
        )
    assert emb_ok is True
    assert rer_ok is True
    te.assert_not_called()


def test_validate_providers_shared_both_probes_fail():
    """Embedder AND reranker raise independently (not via provider gate)."""
    te, ce, avail = _patch_probes(
        embedder_exc=RuntimeError("emb-boom"), reranker_exc=RuntimeError("rer-boom")
    )
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is False
    assert rer_ok is False
    assert len(warnings) == 2


def test_validate_providers_shared_empty_providers_default_path():
    """providers=[] (SearchConfig default) → no gate, probes run with providers=None."""
    te, ce, avail = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te) as te_p, \
         patch("archon_search.model_validation._load_cross_encoder", ce) as ce_p, \
         patch(_AVAILABLE_PROVIDERS, return_value=avail) as avail_p:
        emb_ok, rer_ok, warnings = validate_providers_shared([], "emb-model", "rer-model")
    assert emb_ok is True
    assert rer_ok is True
    assert warnings == []
    # No non-CPU providers → provider gate skipped entirely.
    avail_p.assert_not_called()
    # Probes run with providers coerced to None.
    assert te_p.call_args.kwargs["providers"] is None
    assert ce_p.call_args.args[1] is None


def test_validate_providers_shared_never_raises():
    """Any internal failure (even querying providers) → returns, does not raise."""
    with patch(
        _AVAILABLE_PROVIDERS,
        side_effect=RuntimeError("onnx exploded"),
    ):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CoreMLExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is False
    assert rer_ok is False
    assert warnings


# ---------------------------------------------------------------------------
# BE-1: validate_models_async (C1) — async wrapper, never raises
# ---------------------------------------------------------------------------


def _cfg(**kw) -> SearchConfig:
    return SearchConfig(
        embedding_model=kw.get("embedding_model", "emb-model"),
        reranker_model=kw.get("reranker_model", "rer-model"),
        providers=kw.get("providers", ["CPUExecutionProvider"]),
    )


@pytest.mark.asyncio
async def test_validate_models_async_both_ok():
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, True, []),
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is True
    assert result.reranker_ok is True
    assert result.provider_warnings == []
    assert result.validated_at is not None


@pytest.mark.asyncio
async def test_validate_models_async_embedder_fails():
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(False, True, ["embedder load failed"]),
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is False
    assert result.reranker_ok is True
    assert result.provider_warnings == ["embedder load failed"]


@pytest.mark.asyncio
async def test_validate_models_async_reranker_fails():
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, False, ["reranker load failed"]),
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is True
    assert result.reranker_ok is False


@pytest.mark.asyncio
async def test_validate_models_async_provider_unavailable():
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(False, False, ["provider CoreMLExecutionProvider not available"]),
    ):
        result = await validate_models_async(
            _cfg(providers=["CoreMLExecutionProvider"]), timeout_seconds=5
        )
    assert result.embedder_ok is False
    assert any("CoreMLExecutionProvider" in w for w in result.provider_warnings)


@pytest.mark.asyncio
async def test_validate_models_async_fail_with_warnings():
    """FAIL precedence: embedder fails AND a provider warning is present."""
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(False, False, ["provider warning", "embedder failed"]),
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_models_async_timeout():
    def _slow(*_a, **_k):
        import time
        time.sleep(1.0)
        return (True, True, [])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_slow):
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
    captured = {}

    def _shared(providers, embedding_model, reranker_model):
        captured["reranker_model"] = reranker_model
        return (True, True, [])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(_cfg(reranker_model=""), timeout_seconds=5)
    assert result.reranker_ok is True
    assert captured["reranker_model"] == ""


@pytest.mark.asyncio
async def test_validate_models_async_embedder_warm_skip():
    """embedder_is_warm=True → embedder probe skipped, embedder_ok=True."""
    captured = {}

    def _shared(providers, embedding_model, reranker_model):
        captured["embedding_model"] = embedding_model
        # When warm, the caller passes embedding_model="" → probe skipped → ok=True.
        return (True, True, [])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(
            _cfg(), timeout_seconds=5, embedder_is_warm=True
        )
    assert result.embedder_ok is True
    assert captured["embedding_model"] == ""


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::RuntimeWarning")
async def test_validate_models_async_never_raises():
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        side_effect=RuntimeError("unexpected"),
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings


@pytest.mark.asyncio
async def test_validate_models_async_cancelled_returns_result():
    """CancelledError (BaseException) is caught → returns a result, never re-raises."""
    with patch(
        "archon_search.model_validation.validate_providers_shared",
        side_effect=asyncio.CancelledError,
    ):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert isinstance(result, ModelValidationResult)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert result.provider_warnings


# ---------------------------------------------------------------------------
# Stale-config warning tests
# ---------------------------------------------------------------------------


def _coreml_cfg(**kw) -> SearchConfig:
    cfg = SearchConfig(
        embedding_model=kw.get("embedding_model", "emb-model"),
        reranker_model=kw.get("reranker_model", "rer-model"),
        providers=kw.get("providers", ["CoreMLExecutionProvider"]),
    )
    cfg.reranker_providers = kw.get("reranker_providers", None)
    return cfg


@pytest.mark.asyncio
async def test_stale_coreml_config_warning_fires(caplog: pytest.LogCaptureFixture) -> None:
    """Warning fires when CoreML set, no reranker_providers, reranker enabled, AND reranker fails."""
    import logging

    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, False, ["reranker probe failed"]),
    ), patch(
        _AVAILABLE_PROVIDERS, return_value=["CoreMLExecutionProvider", "CPUExecutionProvider"]
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            await validate_models_async(_coreml_cfg(), timeout_seconds=5)

    assert "split configuration" in caplog.text


@pytest.mark.asyncio
async def test_stale_coreml_warning_no_fire_when_reranker_ok(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warning does NOT fire when reranker_ok=True (both-pass case — not stale)."""
    import logging

    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, True, []),
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            await validate_models_async(_coreml_cfg(), timeout_seconds=5)

    assert "split configuration" not in caplog.text


@pytest.mark.asyncio
async def test_stale_coreml_warning_no_fire_when_reranker_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warning does NOT fire when reranker_model="" (disabled)."""
    import logging

    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, True, []),
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            await validate_models_async(
                _coreml_cfg(reranker_model=""), timeout_seconds=5
            )

    assert "split configuration" not in caplog.text


@pytest.mark.asyncio
async def test_stale_coreml_warning_no_fire_when_coreml_unavailable_on_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S635: CoreML absent from onnxruntime → the reranker runs on CPU, so its result says
    nothing about CoreML: no split-config advice, only the missing-provider warning."""
    import logging

    te, ce, _ = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            result = await validate_models_async(_coreml_cfg(), timeout_seconds=5)

    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert "configured ONNX providers not available: CoreMLExecutionProvider" in (
        result.provider_warnings
    )
    assert "split configuration" not in caplog.text


@pytest.mark.asyncio
async def test_stale_coreml_warning_no_fire_when_cpu_fallback_reranker_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S635: CoreML absent from onnxruntime and the CPU-fallback reranker fails → the
    failure is not CoreML's, so no split-config advice; the probe failure is reported."""
    import logging

    te, ce, _ = _patch_probes(reranker_exc=RuntimeError("rer-boom"))
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            result = await validate_models_async(_coreml_cfg(), timeout_seconds=5)

    assert result.reranker_ok is False
    assert RERANKER_PROBE_FAILED_WARNING in result.provider_warnings
    assert "split configuration" not in caplog.text


@pytest.mark.asyncio
async def test_split_config_reports_a_provider_missing_from_both_lists_once() -> None:
    """S635: providers and reranker_providers dropping the same provider → one warning."""
    te, ce, _ = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]):
        result = await validate_models_async(
            _coreml_cfg(reranker_providers=["CoreMLExecutionProvider"]), timeout_seconds=5
        )

    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert result.provider_warnings.count(
        "configured ONNX providers not available: CoreMLExecutionProvider"
    ) == 1, result.provider_warnings


@pytest.mark.asyncio
async def test_reranker_only_provider_missing_loads_reranker_with_warning() -> None:
    """S635: only [database].reranker_providers names a missing provider → the reranker
    still loads on CPU and the warning is reported."""
    te, ce, _ = _patch_probes()
    cfg = _coreml_cfg(providers=[], reranker_providers=["CUDAExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert "configured ONNX providers not available: CUDAExecutionProvider" in (
        result.provider_warnings
    )
    assert ce.call_args.args[1] == ["CPUExecutionProvider"]


@pytest.mark.asyncio
async def test_split_config_reranker_passing_under_reranker_providers_publishes_no_warning() -> None:
    """A healthy split install (reranker fails under ``providers`` but loads under
    ``reranker_providers``) must leave ``provider_warnings`` empty, or ``/ready`` grades
    ``checks.models`` WARN forever on a host that is working as configured."""
    te, ce, _ = _patch_probes()

    def _load(model_name: str, providers: list[str] | None) -> MagicMock:
        if providers and "CoreMLExecutionProvider" in providers:
            raise RuntimeError(ONNX_MSG)
        instance = MagicMock()
        instance.rerank.return_value = iter([0.0])
        return instance

    ce.side_effect = _load
    cfg = _coreml_cfg(reranker_providers=["CPUExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CoreMLExecutionProvider", "CPUExecutionProvider"]):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert result.provider_warnings == []
    assert [c.args[1] for c in ce.call_args_list] == [["CPUExecutionProvider"]], (
        "the reranker must be probed only under reranker_providers"
    )


@pytest.mark.asyncio
async def test_split_config_provider_query_failing_both_passes_publishes_one_warning_and_logs_each_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """onnxruntime cannot be queried in either pass: the fixed warning is published once,
    while the log keeps one WARNING per pass, each naming the provider list it queried."""
    # Both lists name an accelerator: a CPU-only list never queries onnxruntime.
    cfg = _coreml_cfg(
        providers=["CoreMLExecutionProvider"],
        reranker_providers=["CoreMLExecutionProvider", "CPUExecutionProvider"],
    )
    with patch(_AVAILABLE_PROVIDERS, side_effect=RuntimeError(ONNX_MSG)), \
         caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert (result.embedder_ok, result.reranker_ok) == (False, False)
    assert result.provider_warnings == [ONNX_PROVIDER_QUERY_FAILED_WARNING]
    per_pass = [
        r.getMessage() for r in caplog.records
        if r.name == "archon_search.model_validation" and r.levelno == logging.WARNING
    ]
    assert per_pass == [
        f"could not query ONNX providers for ['CoreMLExecutionProvider']: {ONNX_MSG}",
        f"could not query ONNX providers for ['CoreMLExecutionProvider', 'CPUExecutionProvider']: {ONNX_MSG}",
    ]


@pytest.mark.asyncio
async def test_split_config_reranker_failing_under_reranker_providers_logs_that_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reranker fails under ``reranker_providers``: one fixed warning, and the WARNING
    names the list the reranker was actually probed under — not the first pass's ``providers``."""
    te, ce, _ = _patch_probes(reranker_exc=RuntimeError(ONNX_MSG))
    cfg = _coreml_cfg(providers=["CPUExecutionProvider"], reranker_providers=[])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]), \
         caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert te.call_count == 1, "the first pass still probes the embedder"
    assert ce.call_count == 1, "a split config probes the reranker only under reranker_providers"
    assert result.embedder_ok is True
    assert result.reranker_ok is False
    assert result.provider_warnings == [RERANKER_PROBE_FAILED_WARNING]
    logged = [
        r.getMessage() for r in caplog.records
        if r.name == "archon_search.model_validation" and r.levelno == logging.WARNING
    ]
    assert logged == [f"reranker probe failed for rer-model under providers []: {ONNX_MSG}"]


# validate_providers_shared's three probe handlers: the patched raise point and its WARNING prefix.
_PROBE_FAILURES = [
    pytest.param("archon_search.model_validation.select_onnx_providers",
                 "could not query ONNX providers for ['CPUExecutionProvider']: ", id="onnx-provider-query"),
    pytest.param("archon_search.model_validation.TextEmbedding",
                 "embedder probe failed for emb-model under providers ['CPUExecutionProvider']: ", id="embedder-probe"),
    pytest.param("archon_search.model_validation._load_cross_encoder",
                 "reranker probe failed for rer-model under providers ['CPUExecutionProvider']: ", id="reranker-probe"),
]


@pytest.mark.parametrize(("target", "warning_prefix"), _PROBE_FAILURES)
def test_probe_failure_logs_message_at_warning_and_traceback_only_at_debug(
    caplog: pytest.LogCaptureFixture, target: str, warning_prefix: str
) -> None:
    """An expected probe failure (e.g. a CoreML/CUDA fallback) must not log its traceback at
    WARNING: the install wizard configures no logging, so Python's last-resort handler would
    print it to the user. The cause stays in the WARNING text, the traceback at DEBUG; both
    name the provider list, so the two passes of a split config are distinguishable."""
    te, ce, _ = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, return_value=["CPUExecutionProvider"]), \
         patch(target, side_effect=RuntimeError(ONNX_MSG)) as failing, \
         caplog.at_level(logging.DEBUG, logger="archon_search.model_validation"):
        validate_providers_shared(["CPUExecutionProvider"], "emb-model", "rer-model")

    failing.assert_called()
    own = [r for r in caplog.records if r.name == "archon_search.model_validation"]
    warnings = [r for r in own if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [f"{warning_prefix}{ONNX_MSG}"]
    assert warnings[0].exc_info is None, "the WARNING must not carry the traceback"
    tracebacks = [r for r in own if r.levelno == logging.DEBUG and r.exc_info is not None]
    assert [str(r.exc_info[1]) for r in tracebacks] == [ONNX_MSG], [r.getMessage() for r in own]
    assert [r.getMessage() for r in tracebacks] == [warning_prefix.removesuffix(": ")]


def test_validate_providers_shared_query_failure_with_reranker_disabled() -> None:
    """onnxruntime query fails, reranker disabled → embedder unvalidated, reranker ok."""
    te, ce, _ = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch(_AVAILABLE_PROVIDERS, side_effect=RuntimeError("onnx exploded")):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CUDAExecutionProvider"], "emb-model", ""
        )

    assert (emb_ok, rer_ok) == (False, True)
    assert warnings == [ONNX_PROVIDER_QUERY_FAILED_WARNING]
    te.assert_not_called()
    ce.assert_not_called()


@pytest.mark.asyncio
async def test_stale_coreml_warning_no_fire_when_split_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warning does NOT fire when reranker_providers is set (proper split config)."""
    import logging

    with patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, False, ["reranker probe failed"]),
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
            await validate_models_async(
                _coreml_cfg(reranker_providers=[]), timeout_seconds=5
            )

    assert "split configuration" not in caplog.text


# ---------------------------------------------------------------------------
# Split-config reranker re-validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_config_reranker_validated_under_reranker_providers() -> None:
    """A split config probes the embedder alone under ``providers`` (the reranker skipped), then
    the reranker alone under ``reranker_providers``."""
    calls: list[tuple] = []

    def _shared(providers, embedding_model, reranker_model):
        calls.append((providers, embedding_model, reranker_model))
        return (True, True, [])

    cfg = _coreml_cfg(reranker_providers=[])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert calls == [(["CoreMLExecutionProvider"], "emb-model", ""), ([], "", "rer-model")]
    assert (result.embedder_ok, result.reranker_ok) == (True, True)
    assert result.provider_warnings == []


@pytest.mark.asyncio
async def test_split_config_reranker_failure_propagates() -> None:
    """A reranker failing under ``reranker_providers`` sets reranker_ok False with its warning;
    the embedder, probed in the first pass, stays ok."""
    calls: list[tuple] = []

    def _shared(providers, embedding_model, reranker_model):
        calls.append((providers, embedding_model, reranker_model))
        if reranker_model:
            return (True, False, [RERANKER_PROBE_FAILED_WARNING])
        return (True, True, [])

    cfg = _coreml_cfg(reranker_providers=["CPUExecutionProvider"])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert calls[0] == (["CoreMLExecutionProvider"], "emb-model", "")
    assert (result.embedder_ok, result.reranker_ok) == (True, False)
    assert result.provider_warnings == [RERANKER_PROBE_FAILED_WARNING]


@pytest.mark.asyncio
async def test_split_validation_passes_reduced_timeout_to_second_wait_for() -> None:
    """Second asyncio.wait_for receives _remaining (< timeout_seconds), not the full budget."""
    wait_for_timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _capturing_wait_for(coro, timeout):
        wait_for_timeouts.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    cfg = _coreml_cfg(reranker_providers=[])
    # _start=100.0, second read=102.0 → _remaining = 5 - (102 - 100) = 3.0
    # Using non-zero _start guards against dropping the "- _start" subtraction.
    monotonic_calls = iter([100.0, 102.0])

    with patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])), \
         patch("archon_search.model_validation.time") as mock_time, \
         patch.object(asyncio, "wait_for", side_effect=_capturing_wait_for):
        mock_time.monotonic.side_effect = lambda: next(monotonic_calls)
        await validate_models_async(cfg, timeout_seconds=5)

    assert len(wait_for_timeouts) == 2, "expected exactly two wait_for calls (first + split)"
    # First call gets full budget (5.0), split call gets _remaining (3.0).
    # Assert on min/max rather than position to avoid brittle call-order coupling.
    assert max(wait_for_timeouts) == pytest.approx(5.0, abs=0.1), "first wait_for must use full budget"
    assert min(wait_for_timeouts) == pytest.approx(3.0, abs=0.1), (
        "split wait_for must receive remaining budget (3.0s), not full timeout (5.0s)"
    )


@pytest.mark.asyncio
async def test_split_validation_skips_when_budget_exhausted() -> None:
    """When elapsed time > timeout_seconds, second validation is skipped and reranker_ok=False."""
    call_count = 0

    def _shared(providers, embedding_model, reranker_model):
        nonlocal call_count
        call_count += 1
        return (True, True, [])

    cfg = _coreml_cfg(reranker_providers=[])
    # Simulate 10 s elapsed against a 5 s budget: _remaining = 5 - 10 = -5 <= 0 → skip
    monotonic_calls = iter([0.0, 10.0])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared), \
         patch("archon_search.model_validation.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: next(monotonic_calls)
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert call_count == 1, "second validation must be skipped when budget exhausted"
    assert result.reranker_ok is False
    assert result.provider_warnings == [RERANKER_SPLIT_BUDGET_EXHAUSTED_WARNING]


@pytest.mark.asyncio
async def test_split_validation_skips_at_exact_zero_boundary() -> None:
    """_remaining == 0.0 is treated as exhausted (proves <= not <)."""
    call_count = 0

    def _shared(providers, embedding_model, reranker_model):
        nonlocal call_count
        call_count += 1
        return (True, True, [])

    cfg = _coreml_cfg(reranker_providers=[])
    # _start=0.0, second read=5.0 → _remaining = 5 - (5 - 0) = 0.0 exactly
    monotonic_calls = iter([0.0, 5.0])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared), \
         patch("archon_search.model_validation.time") as mock_time:
        mock_time.monotonic.side_effect = lambda: next(monotonic_calls)
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert call_count == 1, "second validation must be skipped at exactly zero remaining"
    assert result.reranker_ok is False
    assert result.provider_warnings == [RERANKER_SPLIT_BUDGET_EXHAUSTED_WARNING]


@pytest.mark.asyncio
async def test_split_second_probe_timeout_sets_reranker_ok_false() -> None:
    """When the split reranker probe times out, reranker_ok=False and warning mentions the timeout."""
    call_count = 0
    real_wait_for = asyncio.wait_for

    async def _timeout_on_second(coro, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            coro.close()  # prevent leaked-coroutine ResourceWarning
            raise asyncio.TimeoutError
        return await real_wait_for(coro, timeout=timeout)

    cfg = _coreml_cfg(reranker_providers=[])
    monotonic_calls = iter([0.0, 4.5])  # _remaining = 5 - 4.5 > 0 → enters else branch

    with patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])), \
         patch("archon_search.model_validation.time") as mock_time, \
         patch.object(asyncio, "wait_for", side_effect=_timeout_on_second):
        mock_time.monotonic.side_effect = lambda: next(monotonic_calls)
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert result.reranker_ok is False
    assert result.provider_warnings == [RERANKER_SPLIT_TIMED_OUT_TEMPLATE.format(seconds=0.5)]


# ---------------------------------------------------------------------------
# BE-9: non-blocking llama-server reachability probe (S7, S17, S25)
# ---------------------------------------------------------------------------


def _llama_cpp_cfg(**kw) -> SearchConfig:
    """SearchConfig with [hyde].provider="llama_cpp" — triggers the probe."""
    cfg = SearchConfig(
        embedding_model=kw.get("embedding_model", "emb-model"),
        reranker_model=kw.get("reranker_model", "rer-model"),
        providers=kw.get("providers", ["CPUExecutionProvider"]),
    )
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
         patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is True


@pytest.mark.asyncio
async def test_probe_sets_llama_cpp_ok_false():
    """Unreachable llama-server (httpx.ConnectError) → llama_cpp_ok=False."""
    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is False


@pytest.mark.asyncio
async def test_probe_pending_is_none():
    """llama_cpp not configured as provider anywhere → llama_cpp_ok stays None."""
    with patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])):
        result = await validate_models_async(_cfg(), timeout_seconds=5)
    assert result.llama_cpp_ok is None


@pytest.mark.asyncio
async def test_probe_failure_does_not_block_boot(caplog: pytest.LogCaptureFixture):
    """Probe ConnectError never raises — validate_models_async returns normally and logs a WARNING."""
    import logging

    mock_cls = _make_async_client_cls(get_side_effect=httpx.ConnectError("refused"))
    with patch("archon_search.model_validation.httpx.AsyncClient", mock_cls), \
         patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])), \
         caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        result = await validate_models_async(_llama_cpp_cfg(), timeout_seconds=5)

    assert isinstance(result, ModelValidationResult)
    assert result.llama_cpp_ok is False
    assert "llama-server unreachable" in caplog.text


def _s493_probe_cache_dirs(
    embedder: str = "some-embedder", reranker: str = "some-reranker"
) -> dict[str, str]:
    """The ``cache_dir`` each validate_providers_shared probe hands to fastembed: the
    TextEmbedding probe and the ``_load_cross_encoder`` TextCrossEncoder probe."""
    te, _unused_ce, avail = _patch_probes()
    ce_instance = MagicMock()
    ce_instance.rerank.return_value = iter([0.0])
    ce_cls = MagicMock(return_value=ce_instance)
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("fastembed.rerank.cross_encoder.TextCrossEncoder", ce_cls), \
         patch(_AVAILABLE_PROVIDERS, return_value=avail):
        validate_providers_shared(["CPUExecutionProvider"], embedder, reranker)
    return {
        "embedder": te.call_args.kwargs["cache_dir"],
        "reranker": ce_cls.call_args.kwargs["cache_dir"],
    }


def test_s493_fastembed_cache_path_relocates_both_startup_probe_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S493: with FASTEMBED_CACHE_PATH set to a directory OUTSIDE ARCHON_SEARCH_DATA_DIR,
    both startup probes warm that directory, the one the runtime models read from
    (UserManual/30_configuration.md, 150_multi_instance_setup.md)."""
    data_dir = tmp_path / "archon-data"
    fastembed_cache = tmp_path / "fastembed-cache"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(fastembed_cache))

    assert _s493_probe_cache_dirs() == {
        "embedder": str(fastembed_cache),
        "reranker": str(fastembed_cache),
    }


def test_validate_providers_shared_probes_the_archon_models_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S278 (S493 presence anchor): with FASTEMBED_CACHE_PATH unset, both startup probes
    must read/write ``$ARCHON_SEARCH_DATA_DIR/models``, the runtime models' cache.

    ``validate_providers_shared`` once passed no ``cache_dir``, so the startup probe
    downloaded the profile's weights into fastembed's default cache while the pipeline's
    own models still saw an empty ``$ARCHON_SEARCH_DATA_DIR/models`` — ``/ready``
    reported ``models: ok`` and the first ``POST /search`` then paid the balanced
    profile's full ~330 MB download inside the request, so the client got no response.
    """
    from archon_search.profiles import get_profile

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    balanced = get_profile("balanced", multilingual=False)

    expected = str(tmp_path / "models")
    assert _s493_probe_cache_dirs(balanced.embedder, balanced.reranker) == {
        "embedder": expected,
        "reranker": expected,
    }


# ---------------------------------------------------------------------------
# Startup-probe handlers publish fixed warnings, so the traceback is where the cause survives.
# ---------------------------------------------------------------------------

_EXTRACTION_FACTORY_BUILD = "archon_search.enrichment.factory.EnrichmentClientFactory.build"


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
async def test_extraction_probe_call_failure_is_logged_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = AsyncMock()
    client.summarize_community.side_effect = RuntimeError(ONNX_MSG)
    monkeypatch.setattr(_EXTRACTION_FACTORY_BUILD, lambda _config: client)

    with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        warnings = await _probe_extraction_model(_extraction_probe_config())

    client.summarize_community.assert_awaited_once()
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert_logged_with_traceback(caplog, ONNX_MSG)


@pytest.mark.asyncio
async def test_extraction_probe_client_build_failure_is_logged_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    build = MagicMock(side_effect=RuntimeError(ONNX_MSG))
    monkeypatch.setattr(_EXTRACTION_FACTORY_BUILD, build)

    with caplog.at_level(logging.WARNING, logger="archon_search.model_validation"):
        warnings = await _probe_extraction_model(_extraction_probe_config())

    build.assert_called_once()
    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
    assert_logged_with_traceback(caplog, ONNX_MSG)

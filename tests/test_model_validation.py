"""Tests for archon_search.model_validation."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import SearchConfig
from archon_search.model_validation import (
    ModelValidationError,
    ModelValidationResult,
    validate_embedding_model,
    validate_models_async,
    validate_providers_shared,
)


@pytest.mark.asyncio
async def test_validate_known_model_returns_dim():
    """If the model is in list_supported_models, return its dim directly."""
    with patch("archon_search.model_validation.TextEmbedding") as mock_te:
        mock_te.list_supported_models.return_value = [{"name": "model-X", "dim": 384}]
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
         patch("archon_search.model_validation._available_providers", return_value=avail):
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
         patch("archon_search.model_validation._available_providers", return_value=avail):
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
         patch("archon_search.model_validation._available_providers", return_value=avail):
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
         patch("archon_search.model_validation._available_providers", return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CPUExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is True
    assert rer_ok is False
    assert any("rerank-boom" in w or "reranker" in w.lower() for w in warnings)


def test_validate_providers_shared_provider_unavailable_gates_both():
    """Non-CPU provider missing → both ok=False, warning names the provider."""
    te, ce, avail = _patch_probes(available=["CPUExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch("archon_search.model_validation._available_providers", return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CoreMLExecutionProvider"], "emb-model", "rer-model"
        )
    assert emb_ok is False
    assert rer_ok is False
    assert any("CoreMLExecutionProvider" in w for w in warnings)
    # Probes must not run once the provider gate fails.
    te.assert_not_called()
    ce.assert_not_called()


def test_validate_providers_shared_provider_unavailable_reranker_disabled():
    """S7 ∩ S8: provider missing + reranker disabled → emb_ok=False, rer_ok=True."""
    te, ce, avail = _patch_probes(available=["CPUExecutionProvider"])
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch("archon_search.model_validation._available_providers", return_value=avail):
        emb_ok, rer_ok, warnings = validate_providers_shared(
            ["CoreMLExecutionProvider"], "emb-model", ""
        )
    assert emb_ok is False
    assert rer_ok is True
    ce.assert_not_called()


def test_validate_providers_shared_embedder_empty_skips_probe():
    """embedding_model="" → embedder probe skipped, embedder_ok=True (warm path)."""
    te, ce, avail = _patch_probes()
    with patch("archon_search.model_validation.TextEmbedding", te), \
         patch("archon_search.model_validation._load_cross_encoder", ce), \
         patch("archon_search.model_validation._available_providers", return_value=avail):
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
         patch("archon_search.model_validation._available_providers", return_value=avail):
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
         patch("archon_search.model_validation._available_providers", return_value=avail) as avail_p:
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
        "archon_search.model_validation._available_providers",
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
        # validation_timeout_seconds is whole seconds in config, but the wrapper
        # accepts the value directly; use a sub-second internal override via the
        # dedicated parameter. 10x margin (0.1s vs 1.0s) avoids CI flakiness.
        result = await validate_models_async(_cfg(), timeout_seconds=0.1)
    assert result.embedder_ok is False
    assert result.reranker_ok is False
    assert any("timed out" in w for w in result.provider_warnings)


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
    """When reranker_providers is set, reranker is re-validated under reranker_providers."""
    calls: list[tuple] = []

    def _shared(providers, embedding_model, reranker_model):
        calls.append((providers, embedding_model, reranker_model))
        # First call (combined): reranker fails under CoreML
        if providers == ["CoreMLExecutionProvider"]:
            return (True, False, ["reranker probe failed"])
        # Second call (split reranker): reranker passes under []
        return (True, True, [])

    cfg = _coreml_cfg(reranker_providers=[])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert result.embedder_ok is True
    assert result.reranker_ok is True  # split re-validation overrides initial False
    # Second call used reranker_providers (empty list), skipped embedder
    assert calls[1] == ([], "", "rer-model")


@pytest.mark.asyncio
async def test_split_config_reranker_failure_propagates() -> None:
    """When split reranker validation also fails, reranker_ok stays False."""
    def _shared(providers, embedding_model, reranker_model):
        return (True, False, ["probe failed"])

    cfg = _coreml_cfg(reranker_providers=["CPUExecutionProvider"])

    with patch("archon_search.model_validation.validate_providers_shared", side_effect=_shared):
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert result.reranker_ok is False


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
    assert any("exhausted" in w for w in result.provider_warnings)


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
    assert any("exhausted" in w for w in result.provider_warnings)


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
    monotonic_calls = iter([0.0, 4.95])  # _remaining = 5 - 4.95 > 0 → enters else branch

    with patch("archon_search.model_validation.validate_providers_shared", return_value=(True, True, [])), \
         patch("archon_search.model_validation.time") as mock_time, \
         patch.object(asyncio, "wait_for", side_effect=_timeout_on_second):
        mock_time.monotonic.side_effect = lambda: next(monotonic_calls)
        result = await validate_models_async(cfg, timeout_seconds=5)

    assert result.reranker_ok is False
    assert any("timed out" in w for w in result.provider_warnings)

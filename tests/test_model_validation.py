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

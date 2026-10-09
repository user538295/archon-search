"""Unit tests for archon_search.onnx_providers (S635).

A configured ONNX provider the installed onnxruntime lacks must be dropped before the
list reaches fastembed — which raises for any unavailable name — so models load on the
providers the host has, with one WARNING per distinct configured list.
"""
from __future__ import annotations

import logging
import sys
import threading
import types
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from archon_search import onnx_providers
from archon_search.embedder import ModelEmbedder
from archon_search.onnx_providers import (
    CPU_PROVIDER,
    missing_providers_message,
    select_onnx_providers,
    unavailable_onnx_providers,
)
from archon_search.reranker import ModelReranker

_AVAILABLE = f"{onnx_providers.__name__}._available_providers"
_CUDA = "CUDAExecutionProvider"
_COREML = "CoreMLExecutionProvider"
_HOST_WITHOUT_CUDA = [_COREML, CPU_PROVIDER]
_CONCURRENT_CALLERS = 32
_THREAD_TIMEOUT_SECONDS = 10.0


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == onnx_providers.__name__ and r.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# select_onnx_providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", [None, []], ids=["none", "empty"])
def test_unset_providers_pass_none_without_querying_onnxruntime(configured) -> None:
    avail = MagicMock(side_effect=AssertionError("must not query onnxruntime"))
    with patch(_AVAILABLE, avail):
        selection = select_onnx_providers(configured)

    assert selection.providers is None
    assert selection.unavailable == []


def test_cpu_only_list_passes_through_without_querying_onnxruntime() -> None:
    avail = MagicMock(side_effect=AssertionError("must not query onnxruntime"))
    with patch(_AVAILABLE, avail):
        selection = select_onnx_providers([CPU_PROVIDER])

    assert selection.providers == [CPU_PROVIDER]
    assert selection.unavailable == []


def test_available_providers_pass_through_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_AVAILABLE, return_value=_HOST_WITHOUT_CUDA), caplog.at_level(logging.WARNING):
        selection = select_onnx_providers([_COREML, CPU_PROVIDER])

    assert selection.providers == [_COREML, CPU_PROVIDER]
    assert selection.unavailable == []
    assert _warnings(caplog) == []


def test_unavailable_provider_is_dropped_keeping_the_rest_in_order() -> None:
    with patch(_AVAILABLE, return_value=_HOST_WITHOUT_CUDA):
        selection = select_onnx_providers([_CUDA, _COREML, CPU_PROVIDER])

    assert selection.providers == [_COREML, CPU_PROVIDER]
    assert selection.unavailable == [_CUDA]


def test_no_available_provider_left_falls_back_to_cpu() -> None:
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]):
        selection = select_onnx_providers([_CUDA])

    # Explicit CPU, not None: None lets fastembed pick CUDA on a CUDA build.
    assert selection.providers == [CPU_PROVIDER]
    assert selection.unavailable == [_CUDA]


def test_fallback_warning_names_dropped_and_used_providers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]), caplog.at_level(logging.WARNING):
        select_onnx_providers([_CUDA])

    (message,) = _warnings(caplog)
    assert message.startswith(f"configured ONNX providers not available: {_CUDA}")
    assert f"loading models on {CPU_PROVIDER}" in message
    assert f"this onnxruntime build provides: {CPU_PROVIDER})" in message


def test_fallback_warning_is_logged_once_per_configured_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(_AVAILABLE, return_value=_HOST_WITHOUT_CUDA), caplog.at_level(logging.WARNING):
        for _ in range(3):
            select_onnx_providers([_CUDA])
        select_onnx_providers([_CUDA, CPU_PROVIDER])

    assert len(_warnings(caplog)) == 2


def test_duplicate_entries_do_not_make_a_list_warn_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(_AVAILABLE, return_value=_HOST_WITHOUT_CUDA), caplog.at_level(logging.WARNING):
        select_onnx_providers([_CUDA])
        select_onnx_providers([_CUDA, _CUDA])

    assert len(_warnings(caplog)) == 1


def test_onnxruntime_query_failure_propagates() -> None:
    with patch(_AVAILABLE, side_effect=ImportError("no onnxruntime")), pytest.raises(
        ImportError, match="no onnxruntime"
    ):
        select_onnx_providers([_CUDA])


def test_duplicate_unavailable_provider_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]), caplog.at_level(logging.WARNING):
        selection = select_onnx_providers([_CUDA, CPU_PROVIDER, _CUDA])

    assert selection.providers == [CPU_PROVIDER]
    assert selection.unavailable == [_CUDA]
    (message,) = _warnings(caplog)
    assert message.count(_CUDA) == 1, message


@pytest.mark.parametrize(
    ("configured", "available", "expected"),
    [
        ([CPU_PROVIDER, CPU_PROVIDER], [CPU_PROVIDER], [CPU_PROVIDER]),
        ([_COREML, CPU_PROVIDER, _COREML], _HOST_WITHOUT_CUDA, [_COREML, CPU_PROVIDER]),
        ([_CUDA, _COREML, CPU_PROVIDER, _COREML, CPU_PROVIDER], _HOST_WITHOUT_CUDA, [_COREML, CPU_PROVIDER]),
    ],
    ids=["cpu-only", "all-available", "with-fallback"],
)
def test_duplicate_providers_are_loaded_once_in_configured_order(
    configured: list[str], available: list[str], expected: list[str]
) -> None:
    with patch(_AVAILABLE, return_value=available):
        selection = select_onnx_providers(configured)

    assert selection.providers == expected


def test_concurrent_selections_of_one_list_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    barrier = threading.Barrier(_CONCURRENT_CALLERS, timeout=_THREAD_TIMEOUT_SECONDS)

    def select() -> None:
        barrier.wait()
        select_onnx_providers([_CUDA])

    threads = [threading.Thread(target=select) for _ in range(_CONCURRENT_CALLERS)]
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]), caplog.at_level(logging.WARNING):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_THREAD_TIMEOUT_SECONDS)

    assert not any(thread.is_alive() for thread in threads), "a selecting thread is stuck"
    assert len(_warnings(caplog)) == 1


def test_reset_hook_lets_the_same_list_warn_again(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]), caplog.at_level(logging.WARNING):
        select_onnx_providers([_CUDA])
        onnx_providers.reset_fallback_warnings()
        select_onnx_providers([_CUDA])

    assert len(_warnings(caplog)) == 2


def test_missing_providers_message_is_the_reported_contract() -> None:
    assert (
        missing_providers_message([_CUDA, "ROCMExecutionProvider"])
        == "configured ONNX providers not available: CUDAExecutionProvider, ROCMExecutionProvider"
    )


# ---------------------------------------------------------------------------
# unavailable_onnx_providers
# ---------------------------------------------------------------------------


def test_unavailable_names_missing_providers_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch(_AVAILABLE, return_value=_HOST_WITHOUT_CUDA), caplog.at_level(logging.WARNING):
        missing = unavailable_onnx_providers([_CUDA, _COREML, CPU_PROVIDER])

    assert missing == [_CUDA]
    assert _warnings(caplog) == []


def test_unavailable_is_empty_for_cpu_only_without_querying_onnxruntime() -> None:
    avail = MagicMock(side_effect=AssertionError("must not query onnxruntime"))
    with patch(_AVAILABLE, avail):
        assert unavailable_onnx_providers([CPU_PROVIDER]) == []
        assert unavailable_onnx_providers(None) == []


def test_unavailable_reports_a_duplicated_provider_once() -> None:
    with patch(_AVAILABLE, return_value=[CPU_PROVIDER]):
        assert unavailable_onnx_providers([_CUDA, CPU_PROVIDER, _CUDA]) == [_CUDA]


def test_unavailable_query_failure_propagates() -> None:
    with patch(_AVAILABLE, side_effect=RuntimeError("ort exploded")), pytest.raises(
        RuntimeError, match="ort exploded"
    ):
        unavailable_onnx_providers([_COREML])


# ---------------------------------------------------------------------------
# Runtime backends load on the selected providers, resolved lazily
# ---------------------------------------------------------------------------


class _FakeModel:
    def embed(self, documents: list[str]) -> Iterator[np.ndarray]:
        for _ in documents:
            yield np.zeros(1, dtype=np.float32)

    def rerank(self, _query: str, documents: list[str]) -> Iterator[float]:
        for _ in documents:
            yield 0.0


def _fastembed_modules(te: MagicMock, tce: MagicMock) -> dict[str, types.ModuleType]:
    fe_mod = types.ModuleType("fastembed")
    fe_mod.TextEmbedding = te  # type: ignore[attr-defined]
    rerank_mod = types.ModuleType("fastembed.rerank")
    cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder_mod.TextCrossEncoder = tce  # type: ignore[attr-defined]
    return {
        "fastembed": fe_mod,
        "fastembed.rerank": rerank_mod,
        "fastembed.rerank.cross_encoder": cross_encoder_mod,
    }


def _fake_cls() -> MagicMock:
    return MagicMock(side_effect=lambda *_a, **_k: _FakeModel())


@pytest.mark.parametrize(
    ("load", "role"),
    [
        (lambda providers: ModelEmbedder("emb", providers=providers).encode(["q"]), "embedder"),
        (
            lambda providers: ModelReranker("rer", providers=providers).predict([("q", "d")]),
            "reranker",
        ),
    ],
    ids=["embedder", "reranker"],
)
def test_backend_loads_on_cpu_when_configured_provider_is_unavailable(
    load: Any, role: str, caplog: pytest.LogCaptureFixture
) -> None:
    te, tce = _fake_cls(), _fake_cls()
    with patch.dict(sys.modules, _fastembed_modules(te, tce)), patch(
        _AVAILABLE, return_value=_HOST_WITHOUT_CUDA
    ), caplog.at_level(logging.WARNING):
        load([_CUDA, CPU_PROVIDER])

    built = te if role == "embedder" else tce
    assert built.call_args.kwargs["providers"] == [CPU_PROVIDER]
    assert _warnings(caplog) == [
        missing_providers_message([_CUDA])
        + " — loading models on CPUExecutionProvider instead (this onnxruntime build provides:"
        f" {_COREML}, {CPU_PROVIDER}). Install the onnxruntime build for the accelerator, or"
        " change the providers in the [database] config section."
    ]


@pytest.mark.parametrize(
    "construct",
    [
        lambda: ModelEmbedder("emb", providers=[_CUDA]),
        lambda: ModelReranker("rer", providers=[_CUDA]),
    ],
    ids=["embedder", "reranker"],
)
def test_backend_construction_does_not_query_onnxruntime(construct: Any) -> None:
    avail = MagicMock(side_effect=AssertionError("must not query onnxruntime"))
    with patch(_AVAILABLE, avail):
        construct()

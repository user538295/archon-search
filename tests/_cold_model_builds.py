"""Simulated cold model builds for the cold-start regression tests (S283, S288, S290).

The default suite stubs ``ModelEmbedder.encode`` / ``ModelReranker.predict``
(``_stub_model_backends`` in tests/conftest.py), so a model "loads" instantly. The
helpers here wrap that stub and put the build cost exactly where the real backends pay
it: on the first non-empty call of a backend that is not warm yet, serialised on the
backend's own ``_lock`` like ``ModelEmbedder._load``. Install them BEFORE the app starts so
the cost lands in the background startup warm-up, where production puts it.

The startup validation probe (``model_validation.validate_device``) builds its own throwaway
backend; it is exempt, otherwise it would take the transient failure meant for the
backend that serves searches.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from archon_search.embedder import ModelEmbedder
from archon_search.reranker import ModelReranker

_VALIDATION_MODULE_SUFFIX = f"archon_search{os.sep}model_validation.py"


def _called_by_validation_probe() -> bool:
    """True when the current call stack runs inside ``model_validation.py``."""
    frame = sys._getframe()
    while frame is not None:
        if frame.f_code.co_filename.endswith(_VALIDATION_MODULE_SUFFIX):
            return True
        frame = frame.f_back
    return False


def install_cold_embedder_build(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make every serving embedder's first build take *seconds* before it turns warm."""
    stubbed_encode = ModelEmbedder.encode

    def encode(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        if texts and not self.is_warm and not _called_by_validation_probe():
            with self._lock:
                if not self.is_warm:
                    time.sleep(seconds)
                    return stubbed_encode(self, texts)
        return stubbed_encode(self, texts)

    monkeypatch.setattr(ModelEmbedder, "encode", encode)


def install_cold_reranker_build(
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
    *,
    transient_failure: Exception | None = None,
) -> None:
    """Make every serving reranker's first build take *seconds* before it turns warm.

    With *transient_failure*, the first serving build raises it instead (and leaves the
    backend cold); every later build succeeds after the cold-build delay.
    """
    stubbed_predict = ModelReranker.predict
    failure_slot = [transient_failure]  # consumed by the first serving build

    def predict(self: ModelReranker, pairs: list[tuple[str, str]]) -> list[float]:
        if pairs and not self.is_warm and not _called_by_validation_probe():
            with self._lock:
                if not self.is_warm:
                    failure, failure_slot[0] = failure_slot[0], None
                    if failure is not None:
                        raise failure
                    time.sleep(seconds)
                    return stubbed_predict(self, pairs)
        return stubbed_predict(self, pairs)

    monkeypatch.setattr(ModelReranker, "predict", predict)

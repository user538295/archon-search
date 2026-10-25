"""Simulated cold model builds for the cold-start regression tests (S283, S288, S290).

The default suite stubs ``ModelEmbedder.encode`` / ``ModelReranker.predict``
(``_stub_model_backends`` in tests/conftest.py), so a model "loads" instantly. The
helpers here wrap that stub and put the build cost exactly where the real backends pay
it: on the first non-empty call of a backend that is not warm yet, serialised on the
backend's own ``_lock`` like ``ModelEmbedder._load``. Install them BEFORE the app starts so
the cost lands in the background startup warm-up, where production puts it.

The startup validation probe runs the serving backends too, after the warm-up has built them,
so it needs no exemption.
"""
from __future__ import annotations

import threading
import time

import pytest

from archon_search.embedder import ModelEmbedder
from archon_search.reranker import ModelReranker


def install_cold_embedder_build(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make every serving embedder's first build take *seconds* before it turns warm."""
    stubbed_encode = ModelEmbedder.encode

    def encode(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        if texts and not self.is_warm:
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
) -> threading.Event:
    """Make every serving reranker's first build take *seconds* before it turns warm.

    With *transient_failure*, the first serving build raises it instead (and leaves the
    backend cold); every later build succeeds after the cold-build delay. The returned event is
    set the moment that failure is raised (it never is without *transient_failure*).
    """
    failure_raised = threading.Event()
    stubbed_predict = ModelReranker.predict
    failure_slot = [transient_failure]  # consumed by the first serving build

    def predict(self: ModelReranker, pairs: list[tuple[str, str]]) -> list[float]:
        if pairs and not self.is_warm:
            with self._lock:
                if not self.is_warm:
                    failure, failure_slot[0] = failure_slot[0], None
                    if failure is not None:
                        failure_raised.set()
                        raise failure
                    time.sleep(seconds)
                    return stubbed_predict(self, pairs)
        return stubbed_predict(self, pairs)

    monkeypatch.setattr(ModelReranker, "predict", predict)
    return failure_raised

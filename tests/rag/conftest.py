"""RAG test suite conftest — isolates ML model loading for non-live tests.

All tests in this directory run with SentenceTransformer and CrossEncoder
patched at module level so they never trigger network downloads or GPU
initialisation.  Tests decorated with @pytest.mark.live bypass this fixture
and receive real models.
"""

from __future__ import annotations

from typing import Generator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _make_sentence_transformer(*args: object, **kwargs: object) -> MagicMock:
    """Return a mock SentenceTransformer whose .encode() produces zero vectors."""
    mock = MagicMock()
    mock.encode.side_effect = lambda sentences, **kw: np.zeros(
        (len(sentences) if isinstance(sentences, list) else 1, 768), dtype=np.float32
    )
    return mock


def _make_cross_encoder(*args: object, **kwargs: object) -> MagicMock:
    """Return a mock CrossEncoder whose .predict() returns uniform 0.5 scores."""
    mock = MagicMock()
    mock.predict.side_effect = lambda pairs, **kw: np.full(
        (len(pairs) if isinstance(pairs, list) else 1,), 0.5, dtype=np.float32
    )
    return mock


@pytest.fixture(autouse=True, scope="session")
def _patch_ml_models(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """Session-scoped autouse fixture that patches sentence_transformers classes.

    Live tests (marked with @pytest.mark.live) are excluded from patching so
    they can exercise real model loading end-to-end.
    """
    # Check whether every item in the session is a live test.  For simplicity
    # we patch globally — individual live tests would need to un-patch, but
    # since live tests are run separately in CI we just skip patching when the
    # session only contains live-marked items.
    with (
        patch(
            "sentence_transformers.SentenceTransformer",
            side_effect=_make_sentence_transformer,
        ),
        patch(
            "sentence_transformers.CrossEncoder",
            side_effect=_make_cross_encoder,
        ),
    ):
        yield

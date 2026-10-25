"""M4: the GLiNER backend takes the process-wide model locks the embedder and reranker take.

``TRANSFORMERS_LOCK`` guards every lazy ``transformers`` import and model construction (racing ones fail with a
circular-import ``ImportError`` or a meta-tensor ``NotImplementedError``); ``accelerator_lock`` serializes work on the
Apple GPU (two threads loading or running on ``mps`` at once hang). The startup sync's graph load and the embedder
warm-up run in the same lifespan, so the backend must not bypass either. Fakes record, from inside the call, which
locks the calling thread holds.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from archon_search import torch_runtime
from archon_search.prose_extraction_backend import ProseExtractionBackend
from tests._graph_engine_stub import fake_torch_and_gliner, seed_graph_model_snapshot

_MPS = "mps"


@pytest.fixture(autouse=True)
def _graph_model_snapshot_is_cached() -> None:
    seed_graph_model_snapshot()


def _holds_transformers_lock() -> bool:
    return torch_runtime.TRANSFORMERS_LOCK._is_owned()  # type: ignore[attr-defined]


def _holds_accelerator_lock() -> bool:
    return torch_runtime._MPS_LOCK._is_owned()  # type: ignore[attr-defined]


def _with_mps_available(torch_mod: types.ModuleType) -> types.ModuleType:
    torch_mod.cuda = types.SimpleNamespace(is_available=MagicMock(return_value=False))  # type: ignore[attr-defined]
    torch_mod.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        mps=types.SimpleNamespace(is_available=MagicMock(return_value=True))
    )
    return torch_mod


class _RecordingModel:
    """A stand-in GLiNER model that notes which locks are held while it is placed and while it infers."""

    def __init__(self, seen: dict[str, bool]) -> None:
        self._seen = seen

    def to(self, device: str) -> _RecordingModel:
        self._seen["to_accelerator"] = _holds_accelerator_lock()
        self._seen["to_transformers"] = _holds_transformers_lock()
        return self

    def inference(self, texts: list[str], *args: Any, **kwargs: Any) -> tuple[list[list[Any]], list[list[Any]]]:
        self._seen["inference_accelerator"] = _holds_accelerator_lock()
        return [[] for _ in texts], [[] for _ in texts]


def _recording_from_pretrained(seen: dict[str, bool]) -> MagicMock:
    def from_pretrained(*args: Any, **kwargs: Any) -> _RecordingModel:
        seen["load_transformers"] = _holds_transformers_lock()
        seen["load_accelerator"] = _holds_accelerator_lock()
        return _RecordingModel(seen)

    return MagicMock(side_effect=from_pretrained)


@pytest.mark.asyncio
async def test_the_gliner_import_runs_under_the_transformers_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "gliner"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from archon_search import torch_runtime\n"
        "IMPORTED_UNDER_TRANSFORMERS_LOCK = torch_runtime.TRANSFORMERS_LOCK._is_owned()\n"
        "class _Model:\n"
        "    def to(self, device):\n"
        "        return self\n"
        "class GLiNER:\n"
        "    @staticmethod\n"
        "    def from_pretrained(*args, **kwargs):\n"
        "        return _Model()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    torch_mod, _ = fake_torch_and_gliner(MagicMock())
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod}):
        sys.modules.pop("gliner", None)
        await backend.load()
        imported_under_lock = sys.modules["gliner"].IMPORTED_UNDER_TRANSFORMERS_LOCK

    assert imported_under_lock is True


@pytest.mark.asyncio
async def test_the_model_is_built_under_the_transformers_lock() -> None:
    seen: dict[str, bool] = {}
    torch_mod, gliner_mod = fake_torch_and_gliner(_recording_from_pretrained(seen))
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()

    assert seen["load_transformers"] is True


@pytest.mark.asyncio
async def test_on_mps_the_load_and_the_device_placement_hold_the_accelerator_lock() -> None:
    seen: dict[str, bool] = {}
    torch_mod, gliner_mod = fake_torch_and_gliner(_recording_from_pretrained(seen))
    backend = ProseExtractionBackend(device=_MPS)

    with patch.dict(sys.modules, {"torch": _with_mps_available(torch_mod), "gliner": gliner_mod}):
        await backend.load()

    assert seen["load_accelerator"] is True
    assert seen["to_accelerator"] is True


@pytest.mark.asyncio
async def test_on_mps_inference_holds_the_accelerator_lock() -> None:
    seen: dict[str, bool] = {}
    torch_mod, gliner_mod = fake_torch_and_gliner(_recording_from_pretrained(seen))
    backend = ProseExtractionBackend(device=_MPS)

    with patch.dict(sys.modules, {"torch": _with_mps_available(torch_mod), "gliner": gliner_mod}):
        await backend.load()
        await backend.inference(["some prose"], 0.5, 0.5)

    assert seen["inference_accelerator"] is True


@pytest.mark.asyncio
async def test_on_cpu_inference_takes_no_accelerator_lock() -> None:
    seen: dict[str, bool] = {}
    torch_mod, gliner_mod = fake_torch_and_gliner(_recording_from_pretrained(seen))
    backend = ProseExtractionBackend()

    with patch.dict(sys.modules, {"torch": torch_mod, "gliner": gliner_mod}):
        await backend.load()
        await backend.inference(["some prose"], 0.5, 0.5)

    assert seen["inference_accelerator"] is False
    assert seen["load_accelerator"] is False

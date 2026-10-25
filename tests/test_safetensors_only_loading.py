"""The encoder and cross-encoder loaders read safetensors weights only (SecReview #9, defense in depth).

A snapshot whose only weights are a pickle ``pytorch_model.bin`` is refused instead of unpickled.
"""
from __future__ import annotations

from pathlib import Path

import _tiny_encoder as tiny_encoder
import pytest
import torch

from archon_search.reranker_loading import load_cross_encoder
from archon_search.torch_runtime import load_encoder

_SAFETENSORS = "model.safetensors"
_PICKLE = "pytorch_model.bin"


def _swap_safetensors_for_pickle(snapshot: Path, state_dict: dict[str, torch.Tensor]) -> None:
    (snapshot / _SAFETENSORS).unlink()
    torch.save(state_dict, snapshot / _PICKLE)


def test_encoder_with_only_a_pickle_checkpoint_is_refused(tmp_path: Path) -> None:
    snapshot = tiny_encoder.build_snapshot(tmp_path / "enc")
    _, model = load_encoder(tiny_encoder.tiny_spec(), snapshot, "cpu")
    _swap_safetensors_for_pickle(snapshot, model.state_dict())

    with pytest.raises(OSError, match="safetensors"):
        load_encoder(tiny_encoder.tiny_spec(), snapshot, "cpu")


@pytest.mark.parametrize("architecture", ["bert", "xlm_roberta"])
def test_cross_encoder_with_only_a_pickle_checkpoint_is_refused(tmp_path: Path, architecture: str) -> None:
    snapshot = tiny_encoder.build_cross_encoder_snapshot(tmp_path / "ce", architecture)
    spec = tiny_encoder.tiny_reranker_spec(architecture)
    _, model = load_cross_encoder(spec, snapshot, "cpu")
    _swap_safetensors_for_pickle(snapshot, model.state_dict())

    with pytest.raises(OSError, match="safetensors"):
        load_cross_encoder(spec, snapshot, "cpu")


def test_safetensors_snapshots_still_load(tmp_path: Path) -> None:
    snapshot = tiny_encoder.build_snapshot(tmp_path / "enc")

    _, model = load_encoder(tiny_encoder.tiny_spec(), snapshot, "cpu")

    assert next(model.parameters()).dtype == torch.float32

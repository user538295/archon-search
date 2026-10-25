"""Tests for archon_search.torch_runtime (device resolution, cache release)."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import _tiny_encoder as tiny_encoder
from archon_search import torch_runtime
from archon_search.torch_runtime import load_encoder, release_accelerator_cache, resolve_device

if TYPE_CHECKING:
    from tokenizers import Encoding

pytestmark = pytest.mark.real_torch_backends

_CUDA = "torch.cuda.is_available"
_MPS = "torch.backends.mps.is_available"


@pytest.fixture(autouse=True)
def _fresh_warning_state() -> Iterator[None]:
    """Warn-once memory is per process; isolate each test from the others."""
    torch_runtime._warned_settings.clear()
    yield
    torch_runtime._warned_settings.clear()


def test_auto_prefers_cuda_then_mps_then_cpu() -> None:
    with patch(_CUDA, return_value=True), patch(_MPS, return_value=True):
        assert resolve_device("auto") == "cuda"
    with patch(_CUDA, return_value=False), patch(_MPS, return_value=True):
        assert resolve_device("auto") == "mps"
    with patch(_CUDA, return_value=False), patch(_MPS, return_value=False):
        assert resolve_device("auto") == "cpu"


def test_auto_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_CUDA, return_value=False), patch(_MPS, return_value=False):
        with caplog.at_level(logging.WARNING):
            resolve_device("auto")
    assert caplog.records == []


# ---------------------------------------------------------------------------
# accelerator_lock
# ---------------------------------------------------------------------------

_LOCK_CALLS_PER_THREAD = 5
_LOCK_THREADS = 4
_LOCK_HOLD_SECONDS = 0.01


def _peak_concurrency(device: str) -> int:
    """Run threads that each enter ``accelerator_lock(device)`` and report how many overlapped."""
    state_lock = threading.Lock()
    inside = 0
    peak = 0

    def work() -> None:
        nonlocal inside, peak
        for _ in range(_LOCK_CALLS_PER_THREAD):
            with torch_runtime.accelerator_lock(device):
                with state_lock:
                    inside += 1
                    peak = max(peak, inside)
                time.sleep(_LOCK_HOLD_SECONDS)
                with state_lock:
                    inside -= 1

    threads = [threading.Thread(target=work) for _ in range(_LOCK_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return peak


def test_accelerator_lock_runs_apple_gpu_work_one_thread_at_a_time() -> None:
    """Loading or first-running models on ``mps`` from two threads at once hangs the process."""
    assert _peak_concurrency("mps") == 1


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_accelerator_lock_leaves_other_devices_concurrent(device: str) -> None:
    assert _peak_concurrency(device) > 1


def test_accelerator_lock_can_be_re_entered_by_the_holder() -> None:
    """A load that runs a probe inference inside the lock must not deadlock on itself."""
    with torch_runtime.accelerator_lock("mps"):
        with torch_runtime.accelerator_lock("mps"):
            pass


def test_available_accelerator_is_used() -> None:
    with patch(_CUDA, return_value=True):
        assert resolve_device("cuda") == "cuda"
    with patch(_MPS, return_value=True):
        assert resolve_device("mps") == "mps"
    assert resolve_device("cpu") == "cpu"


def test_unavailable_accelerator_steps_down_once(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_MPS, return_value=False), caplog.at_level(logging.WARNING):
        assert resolve_device("mps") == "cpu"
        assert resolve_device("mps") == "cpu"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "mps" in warnings[0].getMessage()


def test_each_unavailable_setting_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    with patch(_MPS, return_value=False), patch(_CUDA, return_value=False):
        with caplog.at_level(logging.WARNING):
            resolve_device("mps")
            resolve_device("cuda")
            resolve_device("cuda")

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


@pytest.mark.parametrize("setting", ["gpu", "", "CUDA", "coreml"])
def test_unknown_device_raises(setting: str) -> None:
    with pytest.raises(ValueError, match="auto, cpu, mps, cuda"):
        resolve_device(setting)


class _SlowMembershipSet(set[str]):
    """A set whose ``in`` test lingers after answering, so unguarded check-then-add races surface."""

    def __contains__(self, item: object) -> bool:
        answer = super().__contains__(item)
        time.sleep(0.05)
        return answer


def test_concurrent_loads_warn_once_per_unavailable_setting(caplog: pytest.LogCaptureFixture) -> None:
    """The embedder and reranker resolve the device from different threads at startup."""
    threads = [threading.Thread(target=resolve_device, args=("cuda",)) for _ in range(4)]
    with (
        patch.object(torch_runtime, "_warned_settings", _SlowMembershipSet()),
        patch(_CUDA, return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len([r for r in caplog.records if "not available" in r.getMessage()]) == 1


def _encodings() -> list[Encoding]:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "a": 1, "b": 2, "c": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    return [tokenizer.encode("a b c"), tokenizer.encode("b"), tokenizer.encode("c", "a b")]


def test_pad_encodings_right_pads_and_masks() -> None:
    import torch

    inputs = torch_runtime.pad_encodings(torch, _encodings(), pad_token_id=9, device="cpu", with_token_types=False)

    assert inputs["input_ids"].tolist() == [[1, 2, 3], [2, 9, 9], [3, 1, 2]]
    assert inputs["attention_mask"].tolist() == [[1, 1, 1], [1, 0, 0], [1, 1, 1]]
    assert "token_type_ids" not in inputs


def test_pad_encodings_adds_token_types_of_text_pairs_on_request() -> None:
    import torch

    inputs = torch_runtime.pad_encodings(torch, _encodings(), pad_token_id=9, device="cpu", with_token_types=True)

    assert inputs["token_type_ids"].tolist() == [[0, 0, 0], [0, 0, 0], [0, 1, 1]]


def test_release_is_a_no_op_on_cpu() -> None:
    with patch("torch.mps.empty_cache") as mps, patch("torch.cuda.empty_cache") as cuda:
        release_accelerator_cache("cpu")
    mps.assert_not_called()
    cuda.assert_not_called()


def test_release_empties_the_mps_cache() -> None:
    with patch("torch.mps.empty_cache") as mps, patch("torch.cuda.empty_cache") as cuda:
        release_accelerator_cache("mps")
    mps.assert_called_once_with()
    cuda.assert_not_called()


def test_release_empties_the_cuda_cache() -> None:
    with patch("torch.mps.empty_cache") as mps, patch("torch.cuda.empty_cache") as cuda:
        release_accelerator_cache("cuda")
    cuda.assert_called_once_with()
    mps.assert_not_called()


def test_torch_is_imported_lazily() -> None:
    code = (
        "import sys; import archon_search.torch_runtime as r; "
        "assert 'torch' not in sys.modules, 'torch imported at module import'; "
        "r.release_accelerator_cache('cpu'); "
        "assert 'torch' not in sys.modules, 'cpu release imported torch'; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "ok", result.stderr


# ---------------------------------------------------------------------------
# load_encoder
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tiny_encoder.build_snapshot(tmp_path_factory.mktemp("tiny-bert"))


def test_loads_a_tiny_synthetic_bert_in_fp32(tiny_snapshot: Path) -> None:
    import torch

    tokenizer, model = load_encoder(tiny_encoder.tiny_spec(), tiny_snapshot, "cpu")

    assert tokenizer.encode("w3 w4").ids == tiny_encoder.token_ids("w3 w4")
    assert tokenizer.padding is None
    assert model.dtype == torch.float32
    assert model.device.type == "cpu"
    assert model.training is False


def test_truncation_is_set_from_the_spec(tiny_snapshot: Path) -> None:
    spec = tiny_encoder.tiny_spec(max_length=5)

    tokenizer, _ = load_encoder(spec, tiny_snapshot, "cpu")

    assert tokenizer.truncation is not None
    assert tokenizer.truncation["max_length"] == 5
    assert len(tokenizer.encode(" ".join(["w1"] * 10)).ids) == 5



def test_loading_weights_prints_no_progress_bar(
    tiny_snapshot: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A server start loads several models; ``transformers``' per-load 'Loading weights' bars would
    flood its log (and the wizard's output) with noise that carries no information."""
    load_encoder(tiny_encoder.tiny_spec(), tiny_snapshot, "cpu")

    assert "Loading weights" not in capfd.readouterr().err

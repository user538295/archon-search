"""Tests for archon_search.torch_runtime (device resolution, cache release)."""

from __future__ import annotations

import gc
import logging
import os
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import _tiny_encoder as tiny_encoder
import pytest

from archon_search import torch_runtime
from archon_search.torch_runtime import (
    accelerator_lock,
    load_encoder,
    release_accelerator_cache,
    release_model_memory,
    resolve_device,
)

_LOCK_WAIT_SECONDS = 0.2
_JOIN_SECONDS = 5.0

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
    with (
        patch(_CUDA, return_value=False),
        patch(_MPS, return_value=False),
        caplog.at_level(logging.WARNING),
    ):
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
    with torch_runtime.accelerator_lock("mps"), torch_runtime.accelerator_lock("mps"):
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
    with (
        patch(_MPS, return_value=False),
        patch(_CUDA, return_value=False),
        caplog.at_level(logging.WARNING),
    ):
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


class _SelfReferencing:
    """Garbage that only the cyclic collector can free, as a ``transformers`` model is."""

    def __init__(self) -> None:
        self.itself = self


def test_release_model_memory_collects_cycles_before_returning_the_accelerator_cache() -> None:
    """A dropped model survives in its reference cycles until the cyclic collector runs, and
    the accelerator cache only returns blocks that are already free: collect first."""
    seen: list[tuple[str, bool]] = []
    garbage = weakref.ref(_SelfReferencing())
    gc.disable()
    try:
        assert garbage() is not None  # what an evicted embedder looks like: dropped, not freed
        with patch.object(
            torch_runtime, "release_accelerator_cache", lambda device: seen.append((device, garbage() is None))
        ):
            release_model_memory("cpu")
    finally:
        gc.enable()

    assert seen == [("cpu", True)]


def test_release_model_memory_resolves_the_device_setting() -> None:
    seen: list[str] = []
    with (
        patch.object(torch_runtime, "resolve_device", return_value="mps") as resolve,
        patch.object(torch_runtime, "release_accelerator_cache", seen.append),
    ):
        release_model_memory("auto")

    resolve.assert_called_once_with("auto")
    assert seen == ["mps"]


def test_release_model_memory_waits_for_the_running_accelerator_work() -> None:
    released = threading.Event()
    with (
        patch.object(torch_runtime, "resolve_device", return_value="mps"),
        patch("torch.mps.empty_cache", side_effect=released.set),
    ):
        with accelerator_lock("mps"):
            releaser = threading.Thread(target=release_model_memory, args=("mps",))
            releaser.start()
            assert not released.wait(timeout=_LOCK_WAIT_SECONDS)
        releaser.join(timeout=_JOIN_SECONDS)

    assert released.is_set()


def test_release_accelerator_cache_waits_for_the_running_accelerator_work() -> None:
    """The embedder and reranker free their cache between oversize batches straight through this function:
    it must not interleave with another thread's ``mps`` forward pass."""
    released = threading.Event()
    with patch("torch.mps.empty_cache", side_effect=released.set):
        with accelerator_lock("mps"):
            releaser = threading.Thread(target=release_accelerator_cache, args=("mps",))
            releaser.start()
            assert not released.wait(timeout=_LOCK_WAIT_SECONDS)
        releaser.join(timeout=_JOIN_SECONDS)

    assert released.is_set()



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


# ---------------------------------------------------------------------------
# concurrent first load
# ---------------------------------------------------------------------------

_LOADER_THREADS_PER_KIND = 3
_CONCURRENT_FIRST_LOAD_SCRIPT = """
import sys
import threading
from pathlib import Path

import torch  # imported up front so the threads below race on transformers alone

import _tiny_encoder as tiny_encoder
from archon_search.reranker_loading import load_cross_encoder
from archon_search.torch_runtime import disable_weight_progress_bar, load_encoder

encoder_snapshot, reranker_snapshot, threads_per_kind = sys.argv[1], sys.argv[2], int(sys.argv[3])
assert "transformers" not in sys.modules, "the import must happen inside the racing threads"
barrier = threading.Barrier(3 * threads_per_kind)
failures = []


def load_embedder():
    barrier.wait()
    try:
        load_encoder(tiny_encoder.tiny_spec(), Path(encoder_snapshot), "cpu")
    except BaseException as exc:
        failures.append(repr(exc))


def silence_progress_bar():
    barrier.wait()
    try:
        disable_weight_progress_bar()
    except BaseException as exc:
        failures.append(repr(exc))


def load_reranker():
    barrier.wait()
    try:
        load_cross_encoder(tiny_encoder.tiny_reranker_spec("bert"), Path(reranker_snapshot), "cpu")
    except BaseException as exc:
        failures.append(repr(exc))


threads = [
    threading.Thread(target=target)
    for _ in range(threads_per_kind)
    for target in (silence_progress_bar, load_embedder, load_reranker)
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print("ok" if not failures else "\\n".join(failures))
"""


def test_concurrent_first_loads_do_not_race_the_transformers_import(
    tiny_snapshot: Path, tmp_path: Path
) -> None:
    """``transformers`` is not safe to import first from several threads at once (circular-import
    ``ImportError``). A server start does exactly that: the embedder probe and the warm-up threads
    all load a model before anything has imported it."""
    reranker_snapshot = tiny_encoder.build_cross_encoder_snapshot(tmp_path / "tiny-reranker", "bert")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}

    result = subprocess.run(
        [sys.executable, "-c", _CONCURRENT_FIRST_LOAD_SCRIPT, str(tiny_snapshot), str(reranker_snapshot), str(_LOADER_THREADS_PER_KIND)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.stdout.strip() == "ok", result.stdout + result.stderr


_MODELS_BUILT_AT_ONCE = 4


def test_concurrent_encoder_loads_build_one_model_at_a_time(tiny_snapshot: Path) -> None:
    """``from_pretrained`` swaps torch globals while it builds a model (``transformers`` 4.x), so two
    threads building at once leave meta tensors behind ('Cannot copy out of meta tensor'). Every
    device serializes the build; ``accelerator_lock`` alone does that for ``mps`` only."""
    from transformers import AutoModel

    meter = tiny_encoder.OverlapMeter()
    with patch.object(AutoModel, "from_pretrained", new=meter):
        tiny_encoder.run_concurrently(
            lambda: load_encoder(tiny_encoder.tiny_spec(), tiny_snapshot, "cpu"), _MODELS_BUILT_AT_ONCE
        )

    assert meter.calls == _MODELS_BUILT_AT_ONCE
    assert meter.peak == 1

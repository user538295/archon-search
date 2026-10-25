"""PyTorch runtime helpers: device resolution, tokenizer and encoder loading, accelerator-cache release.

``torch`` is imported lazily inside the functions so importing this module stays
cheap (the CLI and config validation import it without loading PyTorch).
"""

from __future__ import annotations

import gc
import logging
import threading
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from archon_search.model_specs import TOKENIZER_FILE

if TYPE_CHECKING:
    from tokenizers import Encoding, Tokenizer
    from torch import Tensor
    from transformers import PreTrainedModel

    from archon_search.model_specs import EmbedderSpec

logger = logging.getLogger(__name__)

AUTO_DEVICE = "auto"
CPU_DEVICE = "cpu"
MPS_DEVICE = "mps"
CUDA_DEVICE = "cuda"
#: Values accepted by ``[database].device`` / ``[graph].device``.
DEVICE_SETTINGS: tuple[str, ...] = (AUTO_DEVICE, CPU_DEVICE, MPS_DEVICE, CUDA_DEVICE)
#: Padding id for a model config that declares none (``pad_token_id`` unset or ``0``).
DEFAULT_PAD_TOKEN_ID = 0

#: Settings already reported as unavailable; the step-down warns once per process per setting.
_warned_settings: set[str] = set()
_WARNED_LOCK = threading.Lock()
#: Process-wide guard of Apple GPU work, see ``accelerator_lock``.
_MPS_LOCK = threading.RLock()
#: Held around every lazy ``transformers`` import and every model construction
#: (``from_pretrained``, a model class called by hand). Neither is thread-safe. Its first import
#: raises a circular-import ``ImportError`` in threads that import it (or ``transformers.utils``)
#: at once, and a server start does exactly that from its warm-up and probe threads. On
#: ``transformers`` 4.57.1 racing loads fail with ``NotImplementedError: Cannot copy out of meta
#: tensor`` (``from_pretrained`` swaps torch globals for the length of a build); 5.0-5.13 survived
#: the same race. Re-entrant so a holder may take it again.
TRANSFORMERS_LOCK = threading.RLock()


def _accelerator_available(device: str) -> bool:
    import torch  # lazy: keeps module import free of PyTorch

    if device == CUDA_DEVICE:
        return bool(torch.cuda.is_available())
    return bool(torch.backends.mps.is_available())


def resolve_device(setting: str) -> str:
    """Map a ``device`` setting onto the torch device the models run on.

    ``auto`` picks ``cuda`` > ``mps`` > ``cpu`` and never warns. An explicit
    ``mps``/``cuda`` that this host lacks steps down to ``cpu`` with one WARNING
    per process per setting (a stale config must not latch a failure).

    Raises:
        ValueError: if *setting* is not one of ``DEVICE_SETTINGS``.
    """
    if setting not in DEVICE_SETTINGS:
        raise ValueError(f"unknown device {setting!r}; expected one of: {', '.join(DEVICE_SETTINGS)}")
    if setting == CPU_DEVICE:
        return CPU_DEVICE
    if setting == AUTO_DEVICE:
        for accelerator in (CUDA_DEVICE, MPS_DEVICE):
            if _accelerator_available(accelerator):
                return accelerator
        return CPU_DEVICE
    if _accelerator_available(setting):
        return setting
    with _WARNED_LOCK:
        first_report = setting not in _warned_settings
        _warned_settings.add(setting)
    if first_report:
        logger.warning("device %r is not available on this host; using cpu", setting)
    return CPU_DEVICE


def pad_encodings(
    torch: ModuleType, encodings: list[Encoding], *, pad_token_id: int, device: str, with_token_types: bool
) -> dict[str, Tensor]:
    """Right-pad *encodings* into model input tensors on *device*.

    Right padding is load-bearing: last-token pooling reads row ``i`` at ``attention_mask[i].sum() - 1``.
    ``token_type_ids`` (the segment of each token) are built only when *with_token_types* is set.
    """
    shape = (len(encodings), max(len(e.ids) for e in encodings))
    input_ids = torch.full(shape, pad_token_id, dtype=torch.long)
    attention_mask = torch.zeros(shape, dtype=torch.long)
    inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
    if with_token_types:
        inputs["token_type_ids"] = torch.zeros(shape, dtype=torch.long)
    for row, encoding in enumerate(encodings):
        length = len(encoding.ids)
        input_ids[row, :length] = torch.tensor(encoding.ids)
        attention_mask[row, :length] = 1
        if with_token_types:
            inputs["token_type_ids"][row, :length] = torch.tensor(encoding.type_ids)
    return {name: tensor.to(device) for name, tensor in inputs.items()}


def release_accelerator_cache(device: str) -> None:
    """Return cached accelerator memory to the driver; a no-op on ``cpu``.

    Holds ``accelerator_lock``, so it never interleaves with another thread's ``mps`` forward pass (the embedder and
    reranker call it between oversize batches, outside their own lock scope).
    """
    if device == CPU_DEVICE:
        return
    import torch  # lazy: keeps module import free of PyTorch

    with accelerator_lock(device):
        if device == MPS_DEVICE:
            torch.mps.empty_cache()
        elif device == CUDA_DEVICE:
            torch.cuda.empty_cache()


def release_model_memory(device_setting: str) -> None:
    """Free the memory of models the caller has just dropped, on the *device_setting* device.

    A loaded ``transformers`` model lives in reference cycles, so its weights outlive the last
    reference until the cyclic collector happens to run, and the collector never sees the tensor
    storage's size, so it can wait for a long time. Collect first, then return the accelerator's
    now-free blocks to the driver (``release_accelerator_cache`` only returns blocks that are free
    and holds ``accelerator_lock`` while it does).
    """
    gc.collect()
    release_accelerator_cache(resolve_device(device_setting))


def disable_weight_progress_bar() -> None:
    """Turn off ``transformers``' 'Loading weights' bar (a server start loads several models; the
    bar floods its log and the wizard's output). Process-wide and idempotent."""
    with TRANSFORMERS_LOCK:
        # Lazy: keeps module import free of transformers.
        from transformers.utils import logging as transformers_logging

    transformers_logging.disable_progress_bar()


def accelerator_lock(device: str) -> AbstractContextManager[object]:
    """Serialize the work that touches the Apple GPU (``mps``) across the whole process.

    Two models loading, or running their first inference, on ``mps`` from two threads at once
    hang forever (measured on PyTorch 2.13, 3 of 3 runs; a server start does exactly that from
    its warm-up and probe threads). The GPU executes one command stream anyway: four threads
    gave no throughput over one. Every other device runs concurrently, so it gets a no-op.
    The lock is re-entrant so a holder may take it again.
    """
    return _MPS_LOCK if device == MPS_DEVICE else nullcontext()


def load_tokenizer(path: Path, max_length: int) -> Tokenizer:
    """Load ``tokenizer.json`` from the snapshot at *path* with padding off and truncation at
    *max_length* (callers pad per batch)."""
    from tokenizers import Tokenizer  # lazy: keeps module import free of tokenizers

    tokenizer = Tokenizer.from_file(str(path / TOKENIZER_FILE))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length)
    return tokenizer


def load_encoder(spec: EmbedderSpec, path: Path, device: str) -> tuple[Tokenizer, PreTrainedModel]:
    """Load the tokenizer and the fp32 encoder of *spec* from the snapshot at *path*.

    The tokenizer is the raw ``tokenizer.json`` with padding off and truncation at
    ``spec.max_length`` (callers pad per batch). *device* is an already-resolved
    torch device (``resolve_device``); the model is returned in eval mode on it.
    """
    import torch  # lazy: keeps module import free of PyTorch

    with TRANSFORMERS_LOCK:
        # Lazy: keeps module import free of transformers.
        from transformers import AutoModel

    disable_weight_progress_bar()
    tokenizer = load_tokenizer(path, spec.max_length)
    with accelerator_lock(device):
        with TRANSFORMERS_LOCK:
            # safetensors only: a pickle checkpoint (``pytorch_model.bin``) would execute code on load.
            model = AutoModel.from_pretrained(path, dtype=torch.float32, use_safetensors=True)
        model = model.to(device).eval()
    return tokenizer, model

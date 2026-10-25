"""PyTorch runtime helpers: device resolution, tokenizer and encoder loading, accelerator-cache release.

``torch`` is imported lazily inside the functions so importing this module stays
cheap (the CLI and config validation import it without loading PyTorch).
"""

from __future__ import annotations

import logging
import threading
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tokenizers import Encoding, Tokenizer
    from transformers import PreTrainedModel

    from archon_search.model_specs import EmbedderSpec

logger = logging.getLogger(__name__)

AUTO_DEVICE = "auto"
CPU_DEVICE = "cpu"
MPS_DEVICE = "mps"
CUDA_DEVICE = "cuda"
TOKENIZER_FILE = "tokenizer.json"
#: Values accepted by ``[database].device`` / ``[graph].device``.
DEVICE_SETTINGS: tuple[str, ...] = (AUTO_DEVICE, CPU_DEVICE, MPS_DEVICE, CUDA_DEVICE)

#: Settings already reported as unavailable; the step-down warns once per process per setting.
_warned_settings: set[str] = set()
_WARNED_LOCK = threading.Lock()
#: Process-wide guard of Apple GPU work, see ``accelerator_lock``.
_MPS_LOCK = threading.RLock()


def _accelerator_available(device: str) -> bool:
    import torch  # noqa: PLC0415 — lazy: keeps module import free of PyTorch

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
    torch: Any, encodings: list[Encoding], *, pad_token_id: int, device: str, with_token_types: bool
) -> dict[str, Any]:
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
    """Return cached accelerator memory to the driver; a no-op on ``cpu``."""
    if device == CPU_DEVICE:
        return
    import torch  # noqa: PLC0415 — lazy: keeps module import free of PyTorch

    if device == MPS_DEVICE:
        torch.mps.empty_cache()
    elif device == CUDA_DEVICE:
        torch.cuda.empty_cache()


def disable_weight_progress_bar() -> None:
    """Turn off ``transformers``' 'Loading weights' bar (a server start loads several models; the
    bar floods its log and the wizard's output). Process-wide and idempotent."""
    from transformers.utils import logging as transformers_logging  # noqa: PLC0415 — lazy import

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
    from tokenizers import Tokenizer  # noqa: PLC0415 — lazy: keeps module import free of tokenizers

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
    import torch  # noqa: PLC0415 — lazy: keeps module import free of PyTorch
    from transformers import AutoModel  # noqa: PLC0415

    disable_weight_progress_bar()
    tokenizer = load_tokenizer(path, spec.max_length)
    with accelerator_lock(device):
        model = AutoModel.from_pretrained(path, dtype=torch.float32).to(device).eval()
    return tokenizer, model

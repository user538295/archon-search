"""S671 regression: the embedder loads an external-data ONNX model from a shared-blob cache.

huggingface_hub>=1.32 stores downloaded files in a store shared by the whole cache,
``<cache>/blobs/<first 2 hex of the xet hash>/<xet hash>``. A snapshot's ``model.onnx``
and ``model.onnx_data`` then resolve into different directories, and onnxruntime>=1.24
refuses the load with "External data path escapes model directory". Every ingest on the
multilingual ``max`` profile failed this way until fastembed 0.9.0, which hard-links both
files into ``models--<repo>/onnx_snapshots/<revision>/`` before loading them.

The default suite stubs fastembed (``tests/_search_stubs.py``), so the load runs in a
clean child process through archon's real ``make_embedder`` with the real fastembed and
onnxruntime, offline, against a tiny synthetic model laid out the way huggingface_hub
lays out the real one. The guard covers archon's embedder load path and holds while
onnxruntime rejects the split layout, which the test asserts first. It fails if fastembed
drops below 0.9.0 or archon stops loading models through fastembed from
``get_fastembed_cache_dir()``.

S493: the same real load, with the snapshot in a directory outside
``ARCHON_SEARCH_DATA_DIR`` that ``FASTEMBED_CACHE_PATH`` names, proves the env var
relocates the weights fastembed actually reads — not only the ``cache_dir`` argument.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import pytest

from archon_search.profiles import get_profile

# onnx, onnxruntime, tokenizers and huggingface_hub are imported inside the functions that
# use them: a module-level import would load them into every xdist worker at collection.

# The embedder of the multilingual max profile, the one S671 reported.
_MODEL_NAME = get_profile("max", multilingual=True).embedder
_REVISION = "5" * 40
# Xet hashes with different 2-hex prefixes, so the two files land in different shard
# directories of the shared store, as in the S671 report (`blobs/29/…` vs `blobs/9e/…`).
_MODEL_XET_HASH = "29" + "a" * 62
_DATA_XET_HASH = "9e" + "b" * 62
# Layout version huggingface_hub writes into the store marker (`_shared_blobs.py`).
_SHARED_BLOBS_MARKER = (".huggingface-shared-blobs", "1\n")
_S671_ERROR = "External data path escapes model directory"
# fastembed's error once no source holds the model (`model_management.py`).
_NO_SOURCE_ERROR = "from any source"

_PAD_TOKEN = "[PAD]"
_UNK_TOKEN = "[UNK]"
_QUERY_TOKEN = "s671"
_VOCAB = {_PAD_TOKEN: 0, _UNK_TOKEN: 1, _QUERY_TOKEN: 2}
_EMBEDDING_DIM = 4
_MAX_CONTEXT = 512
_WEIGHTS_NAME = "embeddings.word_embeddings.weight"
_ONNX_OPSET = 17
# IR version of opset 17 (onnx 1.12). Pinned: `make_model` defaults to the newest IR the
# installed onnx knows, which can be ahead of what the installed onnxruntime reads.
_ONNX_IR_VERSION = 8
_CHILD_TIMEOUT_S = 120
_ONE_HOT_TOLERANCE = 1e-6

# Both scripts run in a fresh interpreter: the parent's fastembed is a stub.
# The installed fastembed's description of _MODEL_NAME, through its public API.
_DESCRIPTOR_SCRIPT = """
import json, sys
from fastembed import TextEmbedding
models = TextEmbedding.list_supported_models()
print(json.dumps([m for m in models if m["model"] == sys.argv[1]]))
"""
# The same call path an ingest takes.
_LOADER_SCRIPT = """
import asyncio, json, sys
from archon_search.embedder import make_embedder
print(json.dumps(asyncio.run(make_embedder(sys.argv[1]).embed([sys.argv[2]]))))
"""
_LOADER_FAILURE_HINT = (
    f'"{_S671_ERROR}" below is the S671 regression (fastembed < 0.9.0, or archon'
    " bypassing fastembed's loader). "
    f'"{_NO_SOURCE_ERROR}" means archon no longer passes `get_fastembed_cache_dir()` as'
    " `cache_dir`, or fastembed changed the model's source. Any other error means this"
    " test's scaffolding has rotted."
)


@dataclass(frozen=True)
class _ModelFiles:
    """Where fastembed looks for the model: HF cache repo dir, snapshot-relative files."""

    repo_dir: str
    model_file: str
    data_file: str


def _run_child(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )


def _offline_child_env(
    data_dir: Path, tmpdir: Path, *, fastembed_cache: Path | None = None
) -> dict[str, str]:
    """Child env that loads models offline, only from the cache archon resolves.

    *tmpdir* is created empty and becomes the child's TMPDIR, so fastembed's own
    ``<tempdir>/fastembed_cache`` fallback — which may hold the real model — stays
    empty: a site that stopped passing ``cache_dir`` fails with "from any source".
    ``FASTEMBED_CACHE_PATH`` is set only to *fastembed_cache* (S493); otherwise it is
    removed, so archon uses BE-26's ``<data_dir>/models``.
    """
    tmpdir.mkdir()
    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        "TMPDIR": str(tmpdir),
        "HF_HUB_OFFLINE": "1",
    }
    env.pop("FASTEMBED_CACHE_PATH", None)
    if fastembed_cache is not None:
        env["FASTEMBED_CACHE_PATH"] = str(fastembed_cache)
    return env


def _last_line_json(stdout: str) -> Any:
    """Parse the child's result line, ignoring anything a library printed before it."""
    return json.loads(stdout.splitlines()[-1])


def _fastembed_model_files() -> _ModelFiles:
    from huggingface_hub.file_download import repo_folder_name  # noqa: PLC0415

    child = _run_child(_DESCRIPTOR_SCRIPT, _MODEL_NAME)
    assert child.returncode == 0, child.stderr
    matches = _last_line_json(child.stdout)
    assert len(matches) == 1, f"expected one fastembed model named {_MODEL_NAME}: {matches}"
    descriptor = matches[0]
    model_file = descriptor["model_file"]
    # Precondition: one external-data file next to the model, the S671 shape.
    additional_files = descriptor["additional_files"]
    assert len(additional_files) == 1, f"expected one external-data file: {descriptor}"
    data_file = additional_files[0]
    model_parent = PurePosixPath(model_file).parent
    assert PurePosixPath(data_file).parent == model_parent, descriptor
    return _ModelFiles(
        repo_dir=repo_folder_name(repo_id=descriptor["sources"]["hf"], repo_type="model"),
        model_file=model_file,
        data_file=data_file,
    )


def _embedding_table() -> np.ndarray:
    """One-hot row per vocabulary token, so the output names the row that was read."""
    return np.eye(len(_VOCAB), _EMBEDDING_DIM, dtype=np.float32)


def _build_model(workdir: Path, files: _ModelFiles) -> tuple[bytes, bytes]:
    """Return (model, external data): a Gather over an external embedding table."""
    import onnx  # noqa: PLC0415
    from onnx import TensorProto, helper, numpy_helper  # noqa: PLC0415

    graph = helper.make_graph(
        [helper.make_node("Gather", [_WEIGHTS_NAME, "input_ids"], ["last_hidden_state"])],
        "s671",
        inputs=[
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"])
        ],
        outputs=[
            helper.make_tensor_value_info(
                "last_hidden_state", TensorProto.FLOAT, ["batch", "seq", _EMBEDDING_DIM]
            )
        ],
        initializer=[numpy_helper.from_array(_embedding_table(), name=_WEIGHTS_NAME)],
    )
    model = helper.make_model(
        graph,
        opset_imports=[helper.make_opsetid("", _ONNX_OPSET)],
        ir_version=_ONNX_IR_VERSION,
    )
    model_path = workdir / files.model_file
    model_path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save_model(
        model,
        str(model_path),
        save_as_external_data=True,
        # ONNX resolves `location` against the model file's directory.
        location=str(
            PurePosixPath(files.data_file).relative_to(PurePosixPath(files.model_file).parent)
        ),
        size_threshold=0,
    )
    return model_path.read_bytes(), (workdir / files.data_file).read_bytes()


def _tokenizer_files() -> dict[str, bytes]:
    from tokenizers import Tokenizer, models, pre_tokenizers  # noqa: PLC0415

    tokenizer = Tokenizer(models.WordLevel(_VOCAB, unk_token=_UNK_TOKEN))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    config = {"model_max_length": _MAX_CONTEXT, "pad_token": _PAD_TOKEN}
    return {
        "tokenizer.json": tokenizer.to_str().encode(),
        "tokenizer_config.json": json.dumps(config).encode(),
    }


def _symlink(link: Path, target: Path) -> None:
    """Relative symlink, as huggingface_hub writes them, at any snapshot depth."""
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent))


def _write_shared_blob_cache(
    cache_dir: Path,
    repo_dir_name: str,
    xet_files: dict[str, tuple[bytes, str]],
    git_files: dict[str, bytes],
) -> Path:
    """Lay files out as huggingface_hub>=1.32 does and return the snapshot directory.

    ``snapshots/<rev>/<file> -> blobs/<etag>`` in the repo; for ``xet_files`` (name ->
    payload, xet hash) the repo blob is in turn a symlink into the shared store,
    ``<cache>/blobs/<hh>/<xet hash>``; ``git_files`` (name -> payload) are plain blobs.
    """
    # Built by hand on purpose: caches huggingface_hub>=1.32 already wrote stay on users'
    # disks whatever later versions do, and its `publish_blob_to_shared_store` is private.
    repo_dir = cache_dir / repo_dir_name
    snapshot_dir = repo_dir / "snapshots" / _REVISION
    repo_blobs = repo_dir / "blobs"
    store = cache_dir / "blobs"
    for directory in (snapshot_dir, repo_blobs, store, repo_dir / "refs"):
        directory.mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(_REVISION)
    marker_name, marker_content = _SHARED_BLOBS_MARKER
    (store / marker_name).write_text(marker_content)

    for name, (payload, xet_hash) in xet_files.items():
        stored = store / xet_hash[:2] / xet_hash
        stored.parent.mkdir(exist_ok=True)
        stored.write_bytes(payload)
        _symlink(repo_blobs / xet_hash, stored)
        _symlink(snapshot_dir / name, repo_blobs / xet_hash)
    for index, (name, payload) in enumerate(git_files.items()):
        etag = f"{index:040x}"
        (repo_blobs / etag).write_bytes(payload)
        _symlink(snapshot_dir / name, repo_blobs / etag)
    return snapshot_dir


def _write_model_snapshot(
    cache_dir: Path, files: _ModelFiles, model_bytes: bytes, data_bytes: bytes
) -> Path:
    """The synthetic ``_MODEL_NAME`` snapshot, shared-blob layout, under *cache_dir*."""
    return _write_shared_blob_cache(
        cache_dir,
        files.repo_dir,
        xet_files={
            files.model_file: (model_bytes, _MODEL_XET_HASH),
            files.data_file: (data_bytes, _DATA_XET_HASH),
        },
        git_files=_tokenizer_files(),
    )


def _assert_loaded_the_synthetic_model(child: subprocess.CompletedProcess[str]) -> None:
    assert child.returncode == 0, f"{_LOADER_FAILURE_HINT}\n--- stderr ---\n{child.stderr}"
    vectors = _last_line_json(child.stdout)
    assert len(vectors) == 1, f"expected one vector for one query: {vectors}"
    # The query is one token, and mean pooling over one token returns its row of the table:
    # a one-hot row, unit length, so even an L2 normalisation would leave it unchanged.
    # Getting that row back proves the weights came from the external data file.
    row = _VOCAB[_QUERY_TOKEN]
    expected = _embedding_table()[row]
    assert vectors[0] == pytest.approx(expected.tolist(), abs=_ONE_HOT_TOLERANCE), (
        f"expected one-hot row {row} of the table; a mismatch means the pooling or"
        " tokenizer contract this test relies on changed"
    )


def test_embedder_loads_external_data_model_from_shared_blob_cache(tmp_path: Path) -> None:
    import onnxruntime  # noqa: PLC0415

    files = _fastembed_model_files()
    workdir = tmp_path / "build"
    model_bytes, data_bytes = _build_model(workdir, files)
    data_dir = tmp_path / "data"
    snapshot_dir = _write_model_snapshot(data_dir / "models", files, model_bytes, data_bytes)

    # Precondition: onnxruntime rejects the layout, which proves the two files resolve into
    # different directories. If the load below stops raising, this environment no longer
    # reproduces S671 and the test guards nothing: revisit it. (ORT's `Fail` lives in a
    # non-public module.)
    with pytest.raises(Exception, match=_S671_ERROR):
        onnxruntime.InferenceSession(str(snapshot_dir / files.model_file))

    # BE-26: with FASTEMBED_CACHE_PATH unset, archon passes `<data>/models` as `cache_dir`.
    env = _offline_child_env(data_dir, tmp_path / "tmpdir")
    child = _run_child(_LOADER_SCRIPT, _MODEL_NAME, _QUERY_TOKEN, env=env)

    _assert_loaded_the_synthetic_model(child)


def test_s493_embedder_loads_from_fastembed_cache_path_outside_the_data_dir(
    tmp_path: Path,
) -> None:
    """S493: the real fastembed reads the weights from ``FASTEMBED_CACHE_PATH``.

    The snapshot exists ONLY in that directory, outside ``ARCHON_SEARCH_DATA_DIR``;
    ``<data>/models`` and fastembed's own TMPDIR fallback are empty and the child is
    offline. Before S493 archon passed ``<data>/models`` unconditionally, so this load
    failed with "from any source".
    """
    files = _fastembed_model_files()
    model_bytes, data_bytes = _build_model(tmp_path / "build", files)
    data_dir = tmp_path / "archon-data"
    (data_dir / "models").mkdir(parents=True)
    fastembed_cache = tmp_path / "fastembed-cache"
    _write_model_snapshot(fastembed_cache, files, model_bytes, data_bytes)

    env = _offline_child_env(data_dir, tmp_path / "tmpdir", fastembed_cache=fastembed_cache)
    child = _run_child(_LOADER_SCRIPT, _MODEL_NAME, _QUERY_TOKEN, env=env)

    _assert_loaded_the_synthetic_model(child)
    assert not any((data_dir / "models").iterdir()), "nothing may land in <data>/models"

"""Everything the model downloads put under the data dir is private (S882).

``OperatorGuide/10_deployment_topologies.md`` and ``UserManual/140_running_with_docker.md`` promise that everything
under ``/data`` is ``0700`` (directories) / ``0600`` (files). The model cache lives under ``<data dir>/models``
(``get_models_dir()``) and ``<data dir>/models/graph/...`` (``get_graph_models_dir()``) and is filled by the real
``huggingface_hub.snapshot_download`` through ``model_store.ensure_model`` / ``ensure_pinned_snapshot``. That library
creates directories and blobs with the process umask, so on a normal ``umask 022`` the cache comes out ``0755`` / ``0644``.

These tests run the real production download path (real ``huggingface_hub``, no stub of ``snapshot_download``) against
a local HTTP server standing in for the Hub, under ``umask 022``, and check every entry of the resulting data dir.
Symlinks are skipped: ``huggingface_hub`` links ``snapshots/<rev>/<file>`` to its blob and a symlink's own mode bits
are not meaningful (the blob it points at is checked).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import huggingface_hub.constants as hub_constants
import pytest

from archon_search import model_store
from archon_search._durable_io import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    restrict_tree_to_owner,
)
from archon_search.model_specs import EMBEDDER_SPECS
from archon_search.model_store import (
    MODEL_FILES,
    ModelDownloadError,
    ensure_model,
    ensure_pinned_snapshot,
)
from archon_search.paths import (
    GRAPH_NER_MODEL_FILES,
    GRAPH_NER_MODEL_NAME,
    GRAPH_NER_MODEL_REVISION,
    get_graph_models_dir,
    get_models_dir,
)

_LOOSE_UMASK = 0o022
_WORLD_READABLE_DIR = 0o755
_LOOSE_FILE = 0o644
_LOOPBACK = "127.0.0.1"
_BLOB_CONTENT = b"fake-weights"
_FAKE_BLOB_ID = "a" * 40
_EMBEDDER = EMBEDDER_SPECS["BAAI/bge-small-en-v1.5"]


class _FakeHub(BaseHTTPRequestHandler):
    """The three Hub endpoints ``snapshot_download`` uses: revision info, tree listing, file resolve (HEAD + GET)."""

    repo: str
    revision: str
    files: tuple[str, ...]

    def log_message(self, format: str, *args: object) -> None:
        """Keep pytest output quiet."""

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file_headers(self, name: str) -> None:
        self.send_response(200)
        self.send_header("ETag", f'"{abs(hash(name))}"')
        self.send_header("X-Repo-Commit", self.revision)
        self.send_header("Content-Length", str(len(_BLOB_CONTENT)))
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if "/tree/" in path:
            self._send_json(
                [
                    {
                        "type": "file",
                        "path": n,
                        "size": len(_BLOB_CONTENT),
                        "oid": _FAKE_BLOB_ID,
                    }
                    for n in self.files
                ]
            )
        elif path.startswith("/api/models/"):
            self._send_json(
                {
                    "id": self.repo,
                    "sha": self.revision,
                    "siblings": [{"rfilename": n} for n in self.files],
                }
            )
        else:
            self._send_file_headers(path.rsplit("/", 1)[-1])
            self.wfile.write(_BLOB_CONTENT)

    def do_HEAD(self) -> None:
        self._send_file_headers(self.path.split("?")[0].rsplit("/", 1)[-1])


@pytest.fixture
def serve_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str, str, tuple[str, ...]], None]]:
    """Point the real ``huggingface_hub`` at a local server that serves *files* of *repo* at *revision*."""
    servers: list[ThreadingHTTPServer] = []

    def serve(repo: str, revision: str, files: tuple[str, ...]) -> None:
        handler = type("Hub", (_FakeHub,), {"repo": repo, "revision": revision, "files": files})
        server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        endpoint = f"http://{_LOOPBACK}:{server.server_port}"
        monkeypatch.setattr(hub_constants, "ENDPOINT", endpoint)
        monkeypatch.setattr(
            hub_constants,
            "HUGGINGFACE_CO_URL_TEMPLATE",
            endpoint + "/{repo_id}/resolve/{revision}/{filename}",
        )

    monkeypatch.setenv("NO_PROXY", _LOOPBACK)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(_LOOSE_UMASK)
    yield
    os.umask(previous)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data dir, as the app leaves it at startup (``ensure_private_dir`` tightens the data dir itself)."""
    root = tmp_path / "data"
    root.mkdir(mode=PRIVATE_DIR_MODE)
    root.chmod(PRIVATE_DIR_MODE)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(root))
    return root


def _not_private(root: Path) -> list[str]:
    """``"<mode> <path>"`` of every directory under *root* (and *root*) not ``0700`` and every file not ``0600``."""
    offenders: list[str] = []
    for path in (root, *sorted(root.rglob("*"))):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        expected = PRIVATE_DIR_MODE if path.is_dir() else PRIVATE_FILE_MODE
        if mode != expected:
            offenders.append(f"{mode:o} {path.relative_to(root.parent)}")
    return offenders


def _precreate_models_dir_like_the_image() -> None:
    """The Dockerfile's ``mkdir -p /data/models`` leaves it ``0755`` and owned by the app user."""
    models = get_models_dir()
    models.mkdir(mode=_WORLD_READABLE_DIR)
    models.chmod(_WORLD_READABLE_DIR)


@pytest.mark.parametrize(
    "image_precreated_models_dir",
    [False, True],
    ids=["fresh-models-dir", "image-seeded-models-dir"],
)
def test_embedder_download_leaves_the_data_dir_private(
    data_dir: Path,
    serve_hub: Callable[[str, str, tuple[str, ...]], None],
    image_precreated_models_dir: bool,
) -> None:
    serve_hub(_EMBEDDER.repo, _EMBEDDER.revision, MODEL_FILES)
    if image_precreated_models_dir:
        _precreate_models_dir_like_the_image()

    snapshot = ensure_model(_EMBEDDER)

    assert all((snapshot / name).is_file() for name in MODEL_FILES), "the stand-in Hub served no model"
    offenders = _not_private(data_dir)
    assert not offenders, f"{len(offenders)} entries under the data dir are not 0700/0600:\n" + "\n".join(offenders)


def test_graph_model_download_leaves_the_data_dir_private(
    data_dir: Path, serve_hub: Callable[[str, str, tuple[str, ...]], None]
) -> None:
    serve_hub(GRAPH_NER_MODEL_NAME, GRAPH_NER_MODEL_REVISION, GRAPH_NER_MODEL_FILES)

    snapshot = ensure_pinned_snapshot(
        GRAPH_NER_MODEL_NAME,
        GRAPH_NER_MODEL_REVISION,
        GRAPH_NER_MODEL_FILES,
        get_graph_models_dir(),
    )

    assert all((snapshot / name).is_file() for name in GRAPH_NER_MODEL_FILES), "the stand-in Hub served no model"
    offenders = _not_private(data_dir)
    assert not offenders, f"{len(offenders)} entries under the data dir are not 0700/0600:\n" + "\n".join(offenders)


def _loosen(root: Path) -> None:
    """Re-create what an older version left on a volume: directories ``0755``, files ``0644``."""
    for path in (root, *root.rglob("*")):
        if not path.is_symlink():
            path.chmod(_WORLD_READABLE_DIR if path.is_dir() else _LOOSE_FILE)


def test_cache_hit_tightens_a_cache_an_older_version_left_loose(
    data_dir: Path,
    serve_hub: Callable[[str, str, tuple[str, ...]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_hub(_EMBEDDER.repo, _EMBEDDER.revision, MODEL_FILES)
    snapshot = ensure_model(_EMBEDDER)
    _loosen(data_dir)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # a hit must not need the network

    assert ensure_model(_EMBEDDER) == snapshot

    assert not _not_private(get_models_dir()), "the models tree was not tightened on a cache hit"


def test_cache_hit_on_a_private_cache_does_not_walk_the_tree(
    data_dir: Path,
    serve_hub: Callable[[str, str, tuple[str, ...]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_hub(_EMBEDDER.repo, _EMBEDDER.revision, MODEL_FILES)
    ensure_model(_EMBEDDER)
    monkeypatch.setattr(
        model_store,
        "restrict_tree_to_owner",
        lambda root: pytest.fail("walked a private cache"),
    )

    ensure_model(_EMBEDDER)


def test_restrict_tree_does_not_follow_symlinks_or_touch_foreign_targets(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    outside.chmod(_WORLD_READABLE_DIR)
    (outside / "secret.txt").chmod(_LOOSE_FILE)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "dir_link").symlink_to(outside, target_is_directory=True)
    (tree / "file_link").symlink_to(outside / "secret.txt")

    restrict_tree_to_owner(tree)

    assert stat.S_IMODE(outside.stat().st_mode) == _WORLD_READABLE_DIR
    assert stat.S_IMODE((outside / "secret.txt").stat().st_mode) == _LOOSE_FILE
    assert stat.S_IMODE(tree.stat().st_mode) == PRIVATE_DIR_MODE


def test_restrict_tree_skips_a_symlinked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    (outside / "f").chmod(_LOOSE_FILE)
    outside.chmod(_WORLD_READABLE_DIR)
    root = tmp_path / "models"
    root.symlink_to(outside, target_is_directory=True)

    restrict_tree_to_owner(root)

    assert stat.S_IMODE(outside.stat().st_mode) == _WORLD_READABLE_DIR
    assert stat.S_IMODE((outside / "f").stat().st_mode) == _LOOSE_FILE


def test_failed_download_raises_the_download_error_and_still_tightens(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def partial_then_fail(**kwargs: object) -> str:
        cache = Path(str(kwargs["cache_dir"]))
        (cache / "partial").mkdir()
        (cache / "partial" / "blob").write_text("x")
        raise RuntimeError("boom")

    monkeypatch.setattr(model_store, "snapshot_download", partial_then_fail)

    with pytest.raises(ModelDownloadError):
        ensure_model(_EMBEDDER)

    assert not _not_private(get_models_dir())


def test_a_failing_tighten_never_masks_the_download_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: object) -> str:
        raise RuntimeError("boom")

    def cannot_chmod(root: Path) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(model_store, "snapshot_download", fail)
    monkeypatch.setattr(model_store, "restrict_tree_to_owner", cannot_chmod)

    with pytest.raises(ModelDownloadError) as raised:
        ensure_model(_EMBEDDER)

    assert isinstance(raised.value.__cause__, RuntimeError)


def test_entrypoint_hands_off_under_umask_077(tmp_path: Path) -> None:
    """The real entrypoint (core-only, so no install) starts its CMD with umask 077 even from a 022 parent."""
    entrypoint = Path(__file__).parents[1] / "scripts" / "docker-entrypoint.sh"
    env = {**os.environ, "ARCHON_EXTRAS": ""}

    result = subprocess.run(
        ["sh", str(entrypoint), "sh", "-c", "umask"], capture_output=True, text=True, env=env, cwd=tmp_path, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "0077"


def test_restrict_tree_keeps_going_after_one_entry_fails_then_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").write_text("x")
    (tmp_path / "b").write_text("x")
    for name in ("a", "b"):
        (tmp_path / name).chmod(_LOOSE_FILE)
    real_chmod = os.chmod

    def chmod(path: str, mode: int) -> None:
        if str(path).endswith("/a"):
            raise PermissionError("denied")
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", chmod)

    with pytest.raises(PermissionError):
        restrict_tree_to_owner(tmp_path)

    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == PRIVATE_FILE_MODE


def test_an_unusable_cache_dir_is_a_download_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def cannot_create(path: Path) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(model_store, "ensure_private_dir", cannot_create)

    with pytest.raises(ModelDownloadError):
        ensure_model(_EMBEDDER)

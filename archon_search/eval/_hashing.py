"""Hashing helpers for FEAT-039 eval baseline staleness gates.

`compute_eval_hash` hashes every eval-determinism-defining input: the JSONL
manifests under *corpus_root*, every file under ``corpus_root/corpus``, the
optional ``routing/collections.jsonl``, and ``archon_search/eval/backends.py``.
``metrics.py`` and ``runner.py`` are intentionally excluded — algorithm changes
there require a manual baseline refresh and are caught by unit tests, not
staleness gates (see FEAT-039 Task 4.3 spec).
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def _hash_file(h: hashlib._Hash, path: Path) -> None:
    """Mix the path label and file bytes into *h* deterministically."""
    h.update(path.name.encode("utf-8"))
    h.update(b"\x00")
    h.update(path.read_bytes())
    h.update(b"\x00")


def _default_backends_path() -> Path:
    return Path(__file__).resolve().parent / "backends.py"


def compute_eval_hash(
    corpus_root: Path,
    *,
    backends_path: Path | None = None,
) -> str:
    """Return a hex SHA256 over all eval-determinism-defining inputs.

    Inputs hashed (sorted-path order for file traversals):
    - ``corpus_root/documents.jsonl``
    - every file under ``corpus_root/corpus`` (recursive)
    - ``corpus_root/queries.jsonl``
    - ``corpus_root/labels.jsonl``
    - ``corpus_root/routing/collections.jsonl`` (when present)
    - ``backends_path`` (defaults to the package's ``eval/backends.py``)
    """
    corpus_root = Path(corpus_root)
    backends_path = (
        Path(backends_path) if backends_path is not None else _default_backends_path()
    )

    h = hashlib.sha256()

    # Manifests — fixed order, labelled.
    for label in ("documents.jsonl", "queries.jsonl", "labels.jsonl"):
        manifest = corpus_root / label
        if manifest.exists():
            h.update(label.encode("utf-8"))
            h.update(b"\x00")
            h.update(manifest.read_bytes())
            h.update(b"\x00")

    # Corpus tree — sorted by relative path for determinism.
    corpus_dir = corpus_root / "corpus"
    if corpus_dir.exists():
        files = sorted(
            (p for p in corpus_dir.rglob("*") if p.is_file()),
            key=lambda p: str(p.relative_to(corpus_dir)),
        )
        for p in files:
            rel = str(p.relative_to(corpus_dir))
            h.update(b"corpus/")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")

    # Optional routing collections manifest.
    routing = corpus_root / "routing" / "collections.jsonl"
    if routing.exists():
        h.update(b"routing/collections.jsonl\x00")
        h.update(routing.read_bytes())
        h.update(b"\x00")

    # backends.py content (algorithm-determinism input).
    h.update(b"backends.py\x00")
    h.update(backends_path.read_bytes())
    h.update(b"\x00")

    return h.hexdigest()


def compute_runtime_config_hash(path: Path) -> str:
    """Return hex SHA256 over the runtime.toml content."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compute_thresholds_hash(path: Path) -> str:
    """Return hex SHA256 over the thresholds.toml content."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

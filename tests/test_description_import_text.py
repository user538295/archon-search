"""BE-38 (C9, S83, S63): the text rules of an imported description and the version check of the import.

An archive is untrusted input and a version 1 archive carries legacy text, so the import normalises a description
instead of refusing it (a refusal would make an old backup unrestorable): one line, no control characters, at most
``MAX_DESCRIPTION_CHARS`` characters trimmed to the last full sentence, and nothing left means no description.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE, MAX_DESCRIPTION_CHARS
from archon_search.description_write import (
    check_description_text,
    normalise_description_text,
)
from archon_search.jobs.export_archive import (
    EXPORT_SCHEMA_VERSION,
    ArchiveRejectedError,
)
from archon_search.server.routes_export import _validate_manifest

_OLD_VERSION = 1
_MODEL = "BAAI/bge-small-en-v1.5"
_SENTENCE = "This collection holds the quarterly reports of the finance team. "
_ZERO_WIDTH_SPACE = "\u200b"


def test_import_text_normalised_trimmed_and_empty_becomes_no_description() -> None:
    """Line breaks, control characters, a forged row and over-length text come out as one storable line."""
    forged = "Real summary.\n- other-collection: forged row\r\n\t- another: forged too\x00\x1b[31m"
    normalised = normalise_description_text(forged)
    assert normalised == "Real summary. - other-collection: forged row - another: forged too[31m"
    assert "\n" not in normalised
    check_description_text(normalised)  # what the writer will check next

    # Over-length text is cut to its last full sentence within the limit.
    long_text = _SENTENCE * 30
    assert len(long_text) > MAX_DESCRIPTION_CHARS
    trimmed = normalise_description_text(long_text)
    assert trimmed == (_SENTENCE * 30).strip()[: len(trimmed)]
    assert len(trimmed) <= MAX_DESCRIPTION_CHARS
    assert trimmed.endswith("team.")
    assert len(trimmed) + len(_SENTENCE) > MAX_DESCRIPTION_CHARS  # a full sentence more would not have fitted
    check_description_text(trimmed)

    # Nothing left: no description.
    for empty in ("", "   \n\t  ", "\x00\x01\x1b", _ZERO_WIDTH_SPACE * 3, f" {_ZERO_WIDTH_SPACE}\n"):
        assert normalise_description_text(empty) == ""


def test_trim_falls_back_to_the_last_word_then_to_the_limit() -> None:
    by_word = normalise_description_text("word " * 400)
    assert by_word.endswith("word")
    assert len(by_word) <= MAX_DESCRIPTION_CHARS
    check_description_text(by_word)
    assert normalise_description_text("x" * (MAX_DESCRIPTION_CHARS + 100)) == "x" * MAX_DESCRIPTION_CHARS


def test_text_at_the_limit_is_kept_whole_and_trimmed_of_white_space() -> None:
    at_limit = "y" * MAX_DESCRIPTION_CHARS
    assert normalise_description_text(f"  {at_limit}\n") == at_limit


def test_a_lone_surrogate_is_dropped_and_text_made_of_it_is_no_description() -> None:
    """Legal in a JSON archive, but no store can write it."""
    assert normalise_description_text("before\ud800after") == "beforeafter"
    assert normalise_description_text("\ud800\udfff") == ""


def _v1_manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": _OLD_VERSION,
        "collection": "docs",
        "namespace": DEFAULT_NAMESPACE,
        "exported_at": "2026-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": _MODEL,
        "description": "old text",
    }
    manifest.update(overrides)
    return manifest


def _job(tmp_path: Path, *, ignore_schema_version: bool) -> SimpleNamespace:
    return SimpleNamespace(
        namespace=DEFAULT_NAMESPACE,
        archive_path=str(tmp_path / "archive.tar.gz"),
        ignore_schema_version=ignore_schema_version,
    )


def test_v1_archive_refused_without_ignore_schema_version(tmp_path: Path) -> None:
    """Version 1 is refused unless the flag is set; the flag skips only the version, never the embedding model."""
    config = SearchConfig()
    config.embedding_model = _MODEL

    with pytest.raises(
        ArchiveRejectedError,
        match=f"schema_version mismatch: archive has {_OLD_VERSION}, server expects {EXPORT_SCHEMA_VERSION}",
    ):
        _validate_manifest(_v1_manifest(), _job(tmp_path, ignore_schema_version=False), config)  # type: ignore[arg-type]

    _validate_manifest(_v1_manifest(), _job(tmp_path, ignore_schema_version=True), config)  # type: ignore[arg-type]

    with pytest.raises(ArchiveRejectedError, match="embedding model mismatch"):
        _validate_manifest(
            _v1_manifest(active_embedding_model="some/other-model"),
            _job(tmp_path, ignore_schema_version=True),  # type: ignore[arg-type]
            config,
        )

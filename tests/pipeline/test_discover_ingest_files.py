"""``_discover_ingest_files``: what a directory ingest enumerates, and what it never even walks.

L2: the files skipped for living in a denied location are counted in one INFO line per run, not logged per file.
L5: ``max_discovered_files`` bounds the files that *qualify*; ignored directories (``node_modules``, hidden
directories, virtualenvs) are pruned from the walk, so they neither trip the cap nor cost a directory listing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from archon_search._path_safety import IngestPathPolicy
from archon_search._types import IngestTooManyFilesError
from archon_search.pipeline import _BINARY_EXTENSIONS, _discover_ingest_files

_NO_LIMIT = 10_000
_POLICY = IngestPathPolicy()


def _touch(root: Path, *relative: str) -> list[Path]:
    paths = []
    for name in relative:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("text", encoding="utf-8")
        paths.append(path)
    return paths


def _discover(root: Path, pattern: str = "**/*", *, max_files: int = _NO_LIMIT) -> list[Path]:
    return _discover_ingest_files(root, pattern, _POLICY, max_files)


def _reference(root: Path, pattern: str) -> list[Path]:
    """The pre-L5 behaviour: ``Path.glob`` plus the same filters, on a tree that holds no pruned directory."""
    found = []
    for path in root.glob(pattern):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in _BINARY_EXTENSIONS or path.name.endswith(".acl"):
            continue
        found.append(path)
    return sorted(found)


# ---------------------------------------------------------------------------
# L2: one INFO line per run
# ---------------------------------------------------------------------------


def test_denied_files_are_counted_in_one_info_line_not_logged_one_by_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _touch(tmp_path, "notes.md", "a.pem", "b.pem", "id_rsa", "sub/c.pem")

    with caplog.at_level(logging.DEBUG, logger="archon_search.pipeline"):
        files = _discover(tmp_path)

    assert [path.name for path in files] == ["notes.md"]
    skipped = [r for r in caplog.records if "denied" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.INFO]
    assert "4" in skipped[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_skip_line_is_logged_when_nothing_was_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _touch(tmp_path, "notes.md")

    with caplog.at_level(logging.DEBUG, logger="archon_search.pipeline"):
        _discover(tmp_path)

    assert not [r for r in caplog.records if "denied" in r.getMessage()]


# ---------------------------------------------------------------------------
# L5: the cap counts qualifying files; ignored directories are never walked
# ---------------------------------------------------------------------------


def test_files_in_ignored_directories_do_not_count_against_the_cap(tmp_path: Path) -> None:
    _touch(tmp_path, "a.md", "b.md")
    _touch(tmp_path, *(f"node_modules/pkg/f{i}.md" for i in range(50)))
    _touch(tmp_path, *(f".git/objects/o{i}.md" for i in range(50)))
    _touch(tmp_path, *(f"venv/lib/v{i}.md" for i in range(50)))

    assert [p.name for p in _discover(tmp_path, max_files=2)] == ["a.md", "b.md"]


def test_the_cap_still_counts_qualifying_files(tmp_path: Path) -> None:
    _touch(tmp_path, *(f"f{i}.md" for i in range(5)))

    with pytest.raises(IngestTooManyFilesError):
        _discover(tmp_path, max_files=4)


@pytest.mark.parametrize("ignored", ["node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages"])
def test_an_ignored_directory_is_never_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignored: str
) -> None:
    _touch(tmp_path, "keep.md", f"{ignored}/inner/x.md", f"src/{ignored}/y.md")
    listed: list[str] = []
    real_scandir = os.scandir

    def spy(path: str | os.PathLike[str] | int = ".") -> object:
        listed.append(os.fspath(path) if not isinstance(path, int) else str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)

    files = _discover(tmp_path)

    assert [p.name for p in files] == ["keep.md"]
    assert not [path for path in listed if ignored in Path(path).parts]


def test_a_tree_of_many_ignored_files_costs_no_listing_of_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in for the 200k-file ``node_modules``: its directories are never opened."""
    _touch(tmp_path, "readme.md")
    for package in range(40):
        _touch(tmp_path, *(f"node_modules/p{package}/f{i}.md" for i in range(25)))
    listed: list[str] = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda path=".": (listed.append(str(path)), real_scandir(path))[1])

    assert [p.name for p in _discover(tmp_path, max_files=1)] == ["readme.md"]
    assert listed == [str(tmp_path)]


def test_hidden_files_and_directories_are_skipped(tmp_path: Path) -> None:
    _touch(tmp_path, "a.md", ".hidden.md", ".config/b.md", "docs/.draft/c.md", "docs/d.md")

    assert [p.relative_to(tmp_path).as_posix() for p in _discover(tmp_path)] == ["a.md", "docs/d.md"]


def test_binary_acl_and_symlinked_files_are_skipped(tmp_path: Path) -> None:
    (real,) = _touch(tmp_path, "real.md")
    _touch(tmp_path, "lib.so", "a.md.acl")
    (tmp_path / "link.md").symlink_to(real)

    assert [p.name for p in _discover(tmp_path)] == ["real.md"]


_TREE = (
    "a.md", "b.md", "c.txt", "ab.md", "x.py",
    "docs/one.md", "docs/two.txt", "docs/deep/three.txt", "docs/deep/four.md",
    "src/m.py", "src/pkg/n.py", "pkg/src/o.py", "pkg/src/deep/p.py",
    "other/q.md",
)


@pytest.mark.parametrize(
    "pattern",
    ["**/*", "*", "*.md", "**/*.md", "docs/*", "docs/**/*.txt", "**/src/**/*.py", "*/*.md", "?.md", "[ab].md"],
)
def test_the_walk_matches_what_path_glob_matched(tmp_path: Path, pattern: str) -> None:
    _touch(tmp_path, *_TREE)

    expected = _reference(tmp_path, pattern)

    assert _discover(tmp_path, pattern) == expected


def test_a_bare_recursive_pattern_matches_every_file_below_the_root(tmp_path: Path) -> None:
    """``Path.glob("**")`` yields directories only on Python 3.12 and files too from 3.13, so it cannot be the oracle here."""
    expected = sorted(_touch(tmp_path, *_TREE))

    assert _discover(tmp_path, "**") == expected

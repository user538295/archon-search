"""Tests that _search_stubs.py exists as a direct copy in the package tests directory."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

PACKAGE_TESTS_DIR = Path(__file__).parent
STUBS_FILE = PACKAGE_TESTS_DIR / "_search_stubs.py"

ROOT_TESTS_DIR = Path(__file__).parents[3] / "tests"
ROOT_STUBS_FILE = ROOT_TESTS_DIR / "_search_stubs.py"

_ROOT_COMMENT_PREFIX = "# Canonical copy"


def test_search_stubs_file_exists() -> None:
    """_search_stubs.py must exist directly in packages/archon-search/tests/."""
    assert STUBS_FILE.exists(), (
        f"Expected {STUBS_FILE} to exist as a direct copy, but it was not found."
    )


def test_install_stubs_importable_directly() -> None:
    """install_stubs must be importable directly from the package-local _search_stubs.py."""
    spec = importlib.util.spec_from_file_location("_pkg_search_stubs", STUBS_FILE)
    assert spec is not None and spec.loader is not None, "Could not create module spec"

    module = importlib.util.module_from_spec(spec)
    unique_name = "_test_pkg_search_stubs_direct"
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        assert hasattr(module, "install_stubs"), (
            "_search_stubs.py must export install_stubs"
        )
        assert callable(module.install_stubs), "install_stubs must be callable"
    finally:
        sys.modules.pop(unique_name, None)


def test_install_stubs_is_idempotent() -> None:
    """install_stubs() must be callable without raising (idempotent)."""
    spec = importlib.util.spec_from_file_location("_pkg_search_stubs_idem", STUBS_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    unique_name = "_test_pkg_search_stubs_idem"
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        # Call twice to verify idempotency — must not raise
        module.install_stubs()
        module.install_stubs()
    finally:
        sys.modules.pop(unique_name, None)


def test_search_stubs_importable_via_sys_path() -> None:
    """_search_stubs.py must be importable via sys.path.insert (the conftest mechanism)."""
    pkg_tests_str = str(PACKAGE_TESTS_DIR)
    unique_name = "_test_syspath_search_stubs"
    inserted = False
    try:
        sys.path.insert(0, pkg_tests_str)
        inserted = True
        module = importlib.import_module("_search_stubs")
        # Register under unique name so the real module slot is unaffected
        sys.modules[unique_name] = module
        assert hasattr(module, "install_stubs"), (
            "_search_stubs imported via sys.path must export install_stubs"
        )
    finally:
        if inserted and pkg_tests_str in sys.path:
            sys.path.remove(pkg_tests_str)
        sys.modules.pop("_search_stubs", None)
        sys.modules.pop(unique_name, None)


def test_package_copy_content_identical_to_root() -> None:
    """Package copy must be content-identical to root original (ignoring root's top comment)."""
    assert ROOT_STUBS_FILE.exists(), (
        f"Root stubs file not found: {ROOT_STUBS_FILE}"
    )

    root_lines = ROOT_STUBS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    pkg_content = STUBS_FILE.read_text(encoding="utf-8")

    # Strip the root-only comment line before comparing
    if root_lines and root_lines[0].startswith(_ROOT_COMMENT_PREFIX):
        root_lines = root_lines[1:]
    root_content_stripped = "".join(root_lines)

    assert root_content_stripped == pkg_content, (
        "Package copy of _search_stubs.py is not content-identical to the root original "
        "(after stripping the root-only comment line)."
    )


def test_root_stubs_starts_with_canonical_comment() -> None:
    """Root _search_stubs.py must start with the 'Canonical copy' redirect comment."""
    assert ROOT_STUBS_FILE.exists(), f"Root stubs file not found: {ROOT_STUBS_FILE}"
    root_first_line = ROOT_STUBS_FILE.read_text(encoding="utf-8").splitlines()[0]
    assert root_first_line.startswith(_ROOT_COMMENT_PREFIX), (
        f"Root stubs file must start with '{_ROOT_COMMENT_PREFIX}', got: {root_first_line!r}"
    )


def test_package_copy_does_not_start_with_canonical_comment() -> None:
    """Package _search_stubs.py must NOT start with the root-only 'Canonical copy' comment."""
    pkg_first_line = STUBS_FILE.read_text(encoding="utf-8").splitlines()[0]
    assert not pkg_first_line.startswith(_ROOT_COMMENT_PREFIX), (
        f"Package copy must not contain the root-only redirect comment, got: {pkg_first_line!r}"
    )

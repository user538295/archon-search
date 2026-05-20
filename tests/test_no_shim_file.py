"""Verify that _search_stubs_shim.py has been deleted from the test directory."""

from pathlib import Path


def test_shim_file_does_not_exist() -> None:
    shim = Path(__file__).parent / "_search_stubs_shim.py"
    assert not shim.exists(), f"_search_stubs_shim.py must be deleted, but found: {shim}"

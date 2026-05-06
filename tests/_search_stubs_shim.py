"""Thin shim so packages/archon-search tests can import the shared stubs from the repo root."""
from __future__ import annotations

import importlib.util
import os
import sys

_repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_stubs_path = os.path.join(_repo_root, "tests", "_search_stubs.py")

if "_root_search_stubs" not in sys.modules:
    spec = importlib.util.spec_from_file_location("_root_search_stubs", _stubs_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_root_search_stubs"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

from _root_search_stubs import install_stubs  # noqa: E402  # type: ignore[import]

__all__ = ["install_stubs"]

"""Thin shim so packages/archon-search tests can import the shared stubs from the repo root."""
from __future__ import annotations

import os
import sys

_repo_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from tests._search_stubs import install_stubs  # noqa: E402

__all__ = ["install_stubs"]

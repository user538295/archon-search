"""Shared CLI helpers for archon-search."""
from __future__ import annotations

import sys

from archon_search.platform.service import SearchServiceLifecycle


def _get_service() -> SearchServiceLifecycle:
    """Return the platform-appropriate service implementation."""
    if sys.platform == "darwin":
        from archon_search.platform.macos import LaunchdSearchService
        return LaunchdSearchService()
    if sys.platform.startswith("linux"):
        from archon_search.platform.linux import SystemdSearchService
        return SystemdSearchService()
    if sys.platform == "win32":
        from archon_search.platform.windows import WindowsSearchService
        return WindowsSearchService()
    raise NotImplementedError(f"Unsupported platform: {sys.platform}")

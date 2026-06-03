"""Minimal runtime helpers for archon-search — binary discovery and GPU detection."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from archon_search.platform.service import SearchServiceLifecycle
from archon_search.platform.types import GpuType

_runtime_singleton: SearchRuntime | None = None


def find_binary(name: str, extra_paths: list[str] | None = None) -> Path | None:
    """Return path to *name* binary, checking PATH then extra_paths. None if not found."""
    if not name:
        return None

    found = shutil.which(name)
    if found:
        return Path(found)

    for raw in extra_paths or ():
        p = Path(raw) / name
        if p.is_file() and os.access(p, os.X_OK):
            return p

    return None


class SearchRuntime:
    """Thin runtime helper: binary discovery and GPU detection."""

    def find_binary(self, name: str, extra_paths: list[str] | None = None) -> Path | None:
        return find_binary(name, extra_paths)

    def detect_gpu_type(self) -> GpuType:
        """Detect available GPU acceleration: CUDA on Linux, METAL on ARM macOS, NONE otherwise."""
        try:
            result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return GpuType.CUDA
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return GpuType.METAL

        return GpuType.NONE


def get_runtime() -> SearchRuntime:
    """Return the process-level SearchRuntime singleton."""
    global _runtime_singleton
    if _runtime_singleton is None:
        _runtime_singleton = SearchRuntime()
    return _runtime_singleton


def get_search_service() -> SearchServiceLifecycle:
    """Return the platform-appropriate service lifecycle implementation."""
    system = platform.system()
    if system == "Darwin":
        from archon_search.platform.macos import LaunchdSearchService  # noqa: PLC0415
        return LaunchdSearchService()
    if system == "Linux":
        from archon_search.platform.linux import SystemdSearchService  # noqa: PLC0415
        return SystemdSearchService()
    if system == "Windows":
        from archon_search.platform.windows import WindowsSearchService  # noqa: PLC0415
        return WindowsSearchService()
    raise NotImplementedError(f"Service lifecycle not implemented for platform: {system}")

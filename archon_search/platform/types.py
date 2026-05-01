"""GPU and platform type definitions for archon-search."""
from __future__ import annotations

from enum import Enum


class GpuType(str, Enum):
    NONE = "none"
    CUDA = "cuda"
    METAL = "metal"

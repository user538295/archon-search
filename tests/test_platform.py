"""Tests for archon_search.platform.types and archon_search.platform.runtime — Task 2.3."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestGpuTypeValues:
    def test_gpu_type_none_value(self) -> None:
        from archon_search.platform.types import GpuType

        assert GpuType.NONE == "none"

    def test_gpu_type_cuda_value(self) -> None:
        from archon_search.platform.types import GpuType

        assert GpuType.CUDA == "cuda"

    def test_gpu_type_metal_value(self) -> None:
        from archon_search.platform.types import GpuType

        assert GpuType.METAL == "metal"

    def test_gpu_type_is_str(self) -> None:
        from archon_search.platform.types import GpuType

        assert isinstance(GpuType.METAL, str)
        assert isinstance(GpuType.CUDA, str)
        assert isinstance(GpuType.NONE, str)

    def test_gpu_type_string_comparison(self) -> None:
        from archon_search.platform.types import GpuType

        assert GpuType.CUDA == "cuda"
        assert GpuType.METAL == "metal"
        assert GpuType.NONE == "none"

    def test_all_three_values_exist(self) -> None:
        from archon_search.platform.types import GpuType

        values = {g.value for g in GpuType}
        assert "none" in values
        assert "cuda" in values
        assert "metal" in values


class TestFindBinary:
    def test_find_binary_not_found(self) -> None:
        from archon_search.platform.runtime import find_binary

        result = find_binary("__nonexistent_binary_xyz_abc__")
        assert result is None

    def test_find_binary_found(self) -> None:
        from archon_search.platform.runtime import find_binary

        result = find_binary("python3")
        assert result is not None
        assert isinstance(result, Path)

    def test_find_binary_empty_name_returns_none(self) -> None:
        from archon_search.platform.runtime import find_binary

        result = find_binary("")
        assert result is None

    def test_find_binary_extra_paths_checked(self, tmp_path: Path) -> None:
        from archon_search.platform.runtime import find_binary

        fake_bin = tmp_path / "my_custom_binary"
        fake_bin.touch()
        fake_bin.chmod(0o755)

        result = find_binary("my_custom_binary", extra_paths=[str(tmp_path)])
        assert result == fake_bin

    def test_find_binary_returns_path_object(self) -> None:
        from archon_search.platform.runtime import find_binary

        result = find_binary("python3")
        if result is not None:
            assert isinstance(result, Path)


class TestSearchRuntime:
    def test_search_runtime_has_find_binary(self) -> None:
        from archon_search.platform.runtime import SearchRuntime

        rt = SearchRuntime()
        assert callable(rt.find_binary)

    def test_search_runtime_find_binary_delegates_to_module(self) -> None:
        from archon_search.platform.runtime import SearchRuntime

        rt = SearchRuntime()
        result = rt.find_binary("__nonexistent_xyz__")
        assert result is None

    def test_search_runtime_find_binary_found(self) -> None:
        from archon_search.platform.runtime import SearchRuntime

        rt = SearchRuntime()
        result = rt.find_binary("python3")
        assert result is not None

    def test_search_runtime_has_detect_gpu_type(self) -> None:
        from archon_search.platform.runtime import SearchRuntime

        rt = SearchRuntime()
        assert callable(rt.detect_gpu_type)

    def test_search_runtime_detect_gpu_type_returns_gpu_type(self) -> None:
        from archon_search.platform.runtime import SearchRuntime
        from archon_search.platform.types import GpuType

        rt = SearchRuntime()
        result = rt.detect_gpu_type()
        assert isinstance(result, GpuType)


class TestGetRuntime:
    def test_get_runtime_returns_search_runtime(self) -> None:
        from archon_search.platform.runtime import SearchRuntime, get_runtime

        rt = get_runtime()
        assert isinstance(rt, SearchRuntime)

    def test_get_runtime_is_singleton(self) -> None:
        from archon_search.platform.runtime import get_runtime

        rt1 = get_runtime()
        rt2 = get_runtime()
        assert rt1 is rt2

    def test_no_archon_platform_imports(self) -> None:
        import archon_search.platform.runtime as mod

        src = Path(mod.__file__).read_text()
        assert "from archon.platform" not in src
        assert "import archon.platform" not in src


class TestDetectGpuType:
    def test_cuda_when_nvidia_smi_succeeds(self) -> None:
        from archon_search.platform.runtime import SearchRuntime
        from archon_search.platform.types import GpuType

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rt = SearchRuntime()
            assert rt.detect_gpu_type() == GpuType.CUDA

    def test_metal_when_nvidia_smi_fails_and_darwin_arm64(self) -> None:
        from archon_search.platform.runtime import SearchRuntime
        from archon_search.platform.types import GpuType

        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="arm64"):
            rt = SearchRuntime()
            assert rt.detect_gpu_type() == GpuType.METAL

    def test_none_when_nvidia_smi_fails_and_not_darwin_arm64(self) -> None:
        from archon_search.platform.runtime import SearchRuntime
        from archon_search.platform.types import GpuType

        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("platform.system", return_value="Linux"):
            rt = SearchRuntime()
            assert rt.detect_gpu_type() == GpuType.NONE

    def test_timeout_falls_through_to_none(self) -> None:
        from archon_search.platform.runtime import SearchRuntime
        from archon_search.platform.types import GpuType

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["nvidia-smi"], timeout=5)), \
             patch("platform.system", return_value="Linux"):
            rt = SearchRuntime()
            assert rt.detect_gpu_type() == GpuType.NONE


class TestGetSearchService:
    def test_get_search_service_raises_not_implemented(self) -> None:
        from archon_search.platform.runtime import get_search_service

        with pytest.raises(NotImplementedError):
            get_search_service()

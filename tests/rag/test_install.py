"""Tests for RagInstaller (Task 7.1) — TDD first (RED phase)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_rag_config(tmp_path: Path) -> object:
    """Build a minimal RagConfig-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class FakeRagConfig:
        enabled: bool = True
        host: str = "localhost"
        port: int = 8282
        db_path: str = str(tmp_path / "rag_db")
        history_collection: str = "archon-history"
        embedding_model: str = "BAAI/bge-small-en-v1.5"
        reranker_model: str = "BAAI/bge-reranker-v2-m3"
        providers: list[str] = field(default_factory=list)
        top_k_retrieve: int = 20
        top_k_return: int = 5
        chunk_size: int = 512

    return FakeRagConfig()


def _make_full_config(tmp_path: Path) -> object:
    """Build a minimal full Config-like object for tests."""
    from dataclasses import dataclass

    @dataclass
    class FakeHistoryConfig:
        directory: str = str(tmp_path / "history")

    @dataclass
    class FakeFullConfig:
        history: FakeHistoryConfig = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            self.history = FakeHistoryConfig()

    return FakeFullConfig()


def _make_installer(tmp_path: Path, dry_run: bool = False) -> object:
    """Create a RagInstaller with a fake config injected."""
    from archon.rag.install import RagInstaller

    installer = RagInstaller.__new__(RagInstaller)
    installer.dry_run = dry_run
    installer.cfg = _make_rag_config(tmp_path)
    installer._full_cfg = _make_full_config(tmp_path)
    installer.config_file = str(tmp_path / "config.toml")
    return installer


# ---------------------------------------------------------------------------
# check_deps
# ---------------------------------------------------------------------------


class TestCheckDeps:
    def test_check_deps_all_present(self, tmp_path: Path) -> None:
        """When all packages importable → empty list returned."""
        installer = _make_installer(tmp_path)

        with patch("archon.rag.install.importlib.import_module", return_value=MagicMock()):
            missing = installer.check_deps()

        assert missing == []

    def test_check_deps_missing_package(self, tmp_path: Path) -> None:
        """When one package not importable → its name in the returned list."""
        installer = _make_installer(tmp_path)

        def fake_import(name: str) -> object:
            if name == "lancedb":
                raise ImportError("No module named 'lancedb'")
            return MagicMock()

        with patch("archon.rag.install.importlib.import_module", side_effect=fake_import):
            missing = installer.check_deps()

        assert "lancedb" in missing


# ---------------------------------------------------------------------------
# detect_gpu
# ---------------------------------------------------------------------------


class TestDetectGpu:
    def test_detect_gpu_delegates_to_platform_runtime(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "cuda"
        with patch("archon.rag.install.get_runtime", return_value=mock_runtime):
            result = installer.detect_gpu()
        assert result == "cuda"
        mock_runtime.detect_gpu_type.assert_called_once()

    def test_detect_gpu_returns_cuda(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "cuda"
        with patch("archon.rag.install.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "cuda"

    def test_detect_gpu_returns_apple_silicon(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "apple_silicon"
        with patch("archon.rag.install.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "apple_silicon"

    def test_detect_gpu_returns_none_on_intel_mac(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "none"
        with patch("archon.rag.install.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "none"

    def test_detect_gpu_returns_none_on_linux_no_cuda(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "none"
        with patch("archon.rag.install.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "none"


# ---------------------------------------------------------------------------
# install_deps
# ---------------------------------------------------------------------------


class TestInstallDeps:
    def _capture_pip_args(self, tmp_path: Path, gpu: bool) -> list[list[str]]:
        installer = _make_installer(tmp_path)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            calls.append(cmd)

        with patch("subprocess.run", side_effect=fake_run):
            installer.install_deps(gpu=gpu)

        return calls

    def test_install_deps_gpu_installs_fastembed_gpu(self, tmp_path: Path) -> None:
        calls = self._capture_pip_args(tmp_path, gpu=True)
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "fastembed-gpu" in all_args

    def test_install_deps_cpu_installs_fastembed(self, tmp_path: Path) -> None:
        calls = self._capture_pip_args(tmp_path, gpu=False)
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "fastembed" in all_args
        assert "fastembed-gpu" not in all_args

    def test_install_deps_dry_run_no_op(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path, dry_run=True)
        with patch("subprocess.run") as mock_run:
            installer.install_deps(gpu=False)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# configure_providers
# ---------------------------------------------------------------------------


class TestConfigureProviders:
    def test_configure_providers_writes_cuda_when_gpu(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[rag]\nenabled = true\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu=True)

        content = config_file.read_text()
        assert "CUDAExecutionProvider" in content

    def test_configure_providers_no_op_when_no_gpu(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        original = "[rag]\nenabled = true\n"
        config_file.write_text(original)

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu=False)

        assert config_file.read_text() == original

    def test_configure_providers_dry_run_no_op(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        original = "[rag]\nenabled = true\n"
        config_file.write_text(original)

        installer = _make_installer(tmp_path, dry_run=True)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu=True)

        assert config_file.read_text() == original


# ---------------------------------------------------------------------------
# create_data_dir
# ---------------------------------------------------------------------------


class TestCreateDataDir:
    def test_create_data_dir_creates_path(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        target = tmp_path / "rag_db"
        installer.cfg.db_path = str(target)

        installer.create_data_dir()

        assert target.exists()

    def test_create_data_dir_dry_run_no_op(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path, dry_run=True)
        target = tmp_path / "rag_db"
        installer.cfg.db_path = str(target)

        installer.create_data_dir()

        assert not target.exists()


# ---------------------------------------------------------------------------
# write_service_file / load_service / unload_service
# ---------------------------------------------------------------------------


class TestServiceDelegation:
    def _mock_rag_service(self) -> MagicMock:
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0
        svc.stop.return_value = 0
        svc.unregister.return_value = 0
        return svc

    def test_write_service_file_delegates_to_platform(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        svc = self._mock_rag_service()

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            installer.write_service_file()

        svc.register.assert_called_once_with(dry_run=False)

    def test_write_service_file_dry_run(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path, dry_run=True)
        svc = self._mock_rag_service()

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            installer.write_service_file()

        svc.register.assert_called_once_with(dry_run=True)


# ---------------------------------------------------------------------------
# create_history_collection
# ---------------------------------------------------------------------------


class TestCreateHistoryCollection:
    def test_create_history_collection_builds_pipeline_and_ingests(self, tmp_path: Path) -> None:
        """connect() called before ingest_directory, disconnect() called in finally."""
        installer = _make_installer(tmp_path)

        call_order: list[str] = []

        mock_store = AsyncMock()
        mock_store.connect.side_effect = lambda: call_order.append("connect")
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store
        mock_pipeline.ingest_directory = AsyncMock(
            side_effect=lambda *a, **kw: (call_order.append("ingest"), [])[1]
        )

        with patch("archon.rag.install.create_pipeline", return_value=mock_pipeline):
            asyncio.run(installer.create_history_collection())

        mock_store.connect.assert_called_once()
        mock_pipeline.ingest_directory.assert_called_once()
        mock_store.disconnect.assert_called_once()
        assert call_order.index("connect") < call_order.index("ingest")

    def test_create_history_collection_disconnects_on_ingest_failure(self, tmp_path: Path) -> None:
        """disconnect() called even when ingest_directory raises."""
        installer = _make_installer(tmp_path)

        mock_store = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store
        mock_pipeline.ingest_directory = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("archon.rag.install.create_pipeline", return_value=mock_pipeline):
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(installer.create_history_collection())

        mock_store.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# run() — full install flow
# ---------------------------------------------------------------------------


class TestRun:
    def _base_patches(self, tmp_path: Path, gpu: bool = False) -> dict[str, object]:
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0
        return {
            "archon.rag.install.get_rag_service": MagicMock(return_value=svc),
            "archon.rag.install.create_pipeline": MagicMock(
                return_value=MagicMock(
                    store=AsyncMock(),
                    ingest_directory=AsyncMock(return_value=[]),
                )
            ),
        }

    def test_run_aborts_on_user_decline(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        with patch("builtins.input", return_value="n"), \
             patch.object(installer, "detect_gpu", return_value=False), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps") as mock_install:
            result = installer.run(non_interactive=False)

        assert result != 0
        mock_install.assert_not_called()

    def test_installer_run_calls_create_history_collection(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_store = AsyncMock()
        mock_pipeline.store = mock_store
        mock_pipeline.ingest_directory = AsyncMock(return_value=[])

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch.object(installer, "detect_gpu", return_value=False), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_pipeline.ingest_directory.assert_called_once()

    def test_installer_run_warns_when_service_running(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """If service is already running, installer warns but continues."""
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_store = AsyncMock()
        mock_pipeline.store = mock_store
        mock_pipeline.ingest_directory = AsyncMock(return_value=[])

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch.object(installer, "detect_gpu", return_value=False), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        captured = capsys.readouterr()
        assert "running" in captured.out.lower() or "warning" in captured.out.lower() or "already" in captured.out.lower()


# ---------------------------------------------------------------------------
# run_uninstall()
# ---------------------------------------------------------------------------


class TestRunUninstall:
    def test_run_uninstall_stops_and_unregisters_service(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            result = installer.run_uninstall(delete_db=False)

        assert result == 0
        svc.stop.assert_called_once()
        svc.unregister.assert_called_once()

    def test_run_uninstall_delete_db_true_removes_directory(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        db_path = tmp_path / "rag_db"
        db_path.mkdir()
        installer.cfg.db_path = str(db_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        assert not db_path.exists()

    def test_run_uninstall_delete_db_false_preserves_directory(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        db_path = tmp_path / "rag_db"
        db_path.mkdir()
        installer.cfg.db_path = str(db_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            result = installer.run_uninstall(delete_db=False)

        assert result == 0
        assert db_path.exists()

    def test_run_uninstall_dry_run_preserves_database(self, tmp_path: Path) -> None:
        """dry_run=True with delete_db=True must NOT delete the database directory."""
        installer = _make_installer(tmp_path, dry_run=True)
        db_path = tmp_path / "rag_db"
        db_path.mkdir()
        installer.cfg.db_path = str(db_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        assert db_path.exists()


# ---------------------------------------------------------------------------
# load_service / unload_service
# ---------------------------------------------------------------------------


class TestLoadUnloadService:
    def test_load_service_delegates_to_platform(self, tmp_path: Path) -> None:
        """load_service() must call get_rag_service().start(dry_run=False)."""
        installer = _make_installer(tmp_path, dry_run=False)
        svc = MagicMock()
        svc.start.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            rc = installer.load_service()

        assert rc == 0
        svc.start.assert_called_once_with(dry_run=False)

    def test_unload_service_delegates_to_platform(self, tmp_path: Path) -> None:
        """unload_service() must call get_rag_service().stop(dry_run=False)."""
        installer = _make_installer(tmp_path, dry_run=False)
        svc = MagicMock()
        svc.stop.return_value = 0

        with patch("archon.rag.install.get_rag_service", return_value=svc):
            rc = installer.unload_service()

        assert rc == 0
        svc.stop.assert_called_once_with(dry_run=False)

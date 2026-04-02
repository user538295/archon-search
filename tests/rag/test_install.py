"""Tests for RagInstaller (Task 7.1) — TDD first (RED phase)."""
from __future__ import annotations

import asyncio
import sys
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
        embedding_model: str = "BAAI/bge-small-en-v1.5"
        reranker_model: str = "BAAI/bge-reranker-v2-m3"
        providers: list[str] = field(default_factory=list)
        top_k_retrieve: int = 20
        top_k_return: int = 5
        chunk_size: int = 512
        collections: list[str] = field(default_factory=list)

    return FakeRagConfig()


def _make_full_config(tmp_path: Path) -> object:
    """Build a minimal full Config-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class FakeHistoryConfig:
        directory: str = str(tmp_path / "history")

    @dataclass
    class FakeRagConfigInner:
        collections: list[str] = field(default_factory=list)
        pinned_collections: list[str] = field(default_factory=list)

    @dataclass
    class FakeFullConfig:
        history: FakeHistoryConfig = None  # type: ignore[assignment]
        rag: FakeRagConfigInner = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            self.history = FakeHistoryConfig()
            self.rag = FakeRagConfigInner()

    return FakeFullConfig()


class TestRagInstallerInit:
    def test_init_succeeds_without_telegram_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """RagInstaller.__init__ must not require TELEGRAM_BOT_TOKEN — exercises real load_config path."""
        from archon.rag.install import RagInstaller
        from archon.config.loader import load_config

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            "[access]\nallowed_user_ids = [1]\n[session]\nworking_directory = \"/tmp\"\n"
        )
        env_file = tmp_path / ".env"  # empty — no token

        with patch("archon.config.loader.load_config",
                   side_effect=lambda **kw: load_config(env_file=str(env_file), **kw)):
            installer = RagInstaller(config_file=str(config_toml))

        assert installer.cfg is not None
        assert installer._full_cfg.telegram_bot_token is None

    def test_default_config_file_path(self) -> None:
        """RagInstaller default config_file must point to ~/.archon/config.toml."""
        from archon.rag.install import RagInstaller
        from unittest.mock import MagicMock

        fake_cfg = MagicMock()
        fake_cfg.rag = MagicMock()
        with patch("archon.config.loader.load_config", return_value=fake_cfg) as mock_load:
            installer = RagInstaller()
        assert installer.config_file == str(Path.home() / ".archon" / "config.toml")
        mock_load.assert_called_once_with(config_file=str(Path.home() / ".archon" / "config.toml"), require_token=False)


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
    def _capture_pip_args(self, tmp_path: Path, gpu: str) -> list[list[str]]:
        installer = _make_installer(tmp_path)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            calls.append(cmd)

        with patch("subprocess.run", side_effect=fake_run):
            installer.install_deps(gpu=gpu)

        return calls

    def test_install_deps_cuda_still_installs_gpu_packages(self, tmp_path: Path) -> None:
        calls = self._capture_pip_args(tmp_path, gpu="cuda")
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "fastembed-gpu" in all_args
        assert "onnxruntime-gpu" in all_args

    def test_install_deps_apple_silicon_installs_standard_fastembed(self, tmp_path: Path) -> None:
        calls = self._capture_pip_args(tmp_path, gpu="apple_silicon")
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "fastembed" in all_args
        assert "fastembed-gpu" not in all_args

    def test_install_deps_none_installs_standard_fastembed(self, tmp_path: Path) -> None:
        calls = self._capture_pip_args(tmp_path, gpu="none")
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "fastembed" in all_args
        assert "fastembed-gpu" not in all_args

    def test_install_deps_dry_run_no_op(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path, dry_run=True)
        with patch("subprocess.run") as mock_run:
            installer.install_deps(gpu="none")
        mock_run.assert_not_called()

    def test_install_deps_cuda_passes_python_to_all_calls(self, tmp_path: Path) -> None:
        """All 3 subprocess calls for CUDA path include --python sys.executable."""
        calls = self._capture_pip_args(tmp_path, gpu="cuda")
        assert len(calls) == 3  # uninstall fastembed + install fastembed-gpu + install common
        for cmd in calls:
            assert "--python" in cmd
            assert cmd[cmd.index("--python") + 1] == sys.executable

    @pytest.mark.parametrize("gpu", ["none", "apple_silicon"])
    def test_install_deps_cpu_passes_python_to_all_calls(self, tmp_path: Path, gpu: str) -> None:
        """All 2 subprocess calls for CPU/Apple Silicon path include --python sys.executable."""
        calls = self._capture_pip_args(tmp_path, gpu=gpu)
        assert len(calls) == 2  # install fastembed + install common
        for cmd in calls:
            assert "--python" in cmd
            assert cmd[cmd.index("--python") + 1] == sys.executable

    def test_install_deps_cpu_common_packages_present(self, tmp_path: Path) -> None:
        """lancedb and docling appear in the flat args of all captured commands for cpu."""
        calls = self._capture_pip_args(tmp_path, gpu="none")
        all_args = " ".join(arg for cmd in calls for arg in cmd)
        assert "lancedb" in all_args
        assert "docling" in all_args

    def test_install_deps_dry_run_cuda_no_op(self, tmp_path: Path) -> None:
        """dry_run=True with CUDA GPU skips all subprocess calls."""
        installer = _make_installer(tmp_path, dry_run=True)
        with patch("subprocess.run") as mock_run:
            installer.install_deps(gpu="cuda")
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

        installer.configure_providers(gpu="cuda")

        content = config_file.read_text()
        assert "CUDAExecutionProvider" in content

    def test_configure_providers_no_op_when_no_gpu(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        original = "[rag]\nenabled = true\n"
        config_file.write_text(original)

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="none")

        assert config_file.read_text() == original

    def test_configure_providers_dry_run_no_op(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        original = "[rag]\nenabled = true\n"
        config_file.write_text(original)

        installer = _make_installer(tmp_path, dry_run=True)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="cuda")

        assert config_file.read_text() == original

    def test_configure_providers_apple_silicon_writes_coreml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[rag]\nenabled = true\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="apple_silicon")

        content = config_file.read_text()
        assert "CoreMLExecutionProvider" in content
        assert "CUDAExecutionProvider" not in content

    def test_configure_providers_cuda_unchanged(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[rag]\nenabled = true\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="cuda")

        content = config_file.read_text()
        assert "CUDAExecutionProvider" in content

    def test_configure_providers_none_is_noop(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        original = "[rag]\nenabled = true\n"
        config_file.write_text(original)

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="none")

        assert config_file.read_text() == original

    def test_configure_providers_idempotent_if_already_set(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[rag]\nenabled = true\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="cuda")
        content_after_first = config_file.read_text()

        installer.configure_providers(gpu="cuda")
        content_after_second = config_file.read_text()

        assert content_after_first == content_after_second
        assert "CUDAExecutionProvider" in content_after_second

    def test_configure_providers_apple_silicon_idempotent_with_fallback_chain(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        # providers already has CoreML + CPU fallback chain
        config_file.write_text(
            '[rag]\nenabled = true\nproviders = ["CoreMLExecutionProvider", "CPUExecutionProvider"]\n'
        )

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="apple_silicon")

        content = config_file.read_text()
        # must NOT clobber the existing chain
        assert "CPUExecutionProvider" in content
        assert "CoreMLExecutionProvider" in content

    def test_configure_providers_replaces_cuda_with_coreml(self, tmp_path: Path) -> None:
        """Switching from cuda to apple_silicon must fully replace providers list."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('[rag]\nenabled = true\nproviders = ["CUDAExecutionProvider"]\n')

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        installer.configure_providers(gpu="apple_silicon")

        content = config_file.read_text()
        assert "CoreMLExecutionProvider" in content
        assert "CUDAExecutionProvider" not in content


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
# _bootstrap_collections (replaces _bootstrap_collections)
# ---------------------------------------------------------------------------


class TestBootstrapCollections:
    def test_bootstrap_collections_builds_pipeline_and_syncs(self, tmp_path: Path) -> None:
        """connect() called before sync, disconnect() called in finally."""
        installer = _make_installer(tmp_path)

        call_order: list[str] = []

        mock_store = AsyncMock()
        mock_store.connect.side_effect = lambda: call_order.append("connect")
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        mock_sync_result = MagicMock()
        mock_sync = AsyncMock(side_effect=lambda *a, **kw: (call_order.append("sync"), mock_sync_result)[1])

        with patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync:
            MockSync.return_value.sync = mock_sync
            asyncio.run(installer._bootstrap_collections())

        mock_store.connect.assert_called_once()
        mock_sync.assert_called_once()
        mock_store.disconnect.assert_called_once()
        assert call_order.index("connect") < call_order.index("sync")

    def test_bootstrap_collections_disconnects_on_sync_failure(self, tmp_path: Path) -> None:
        """disconnect() called even when sync raises."""
        installer = _make_installer(tmp_path)

        mock_store = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        with patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync:
            MockSync.return_value.sync = AsyncMock(side_effect=RuntimeError("boom"))
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(installer._bootstrap_collections())

        mock_store.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# run() — full install flow
# ---------------------------------------------------------------------------


class TestRun:
    def _base_patches(self, tmp_path: Path) -> dict[str, object]:
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
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps") as mock_install:
            result = installer.run(non_interactive=False)

        assert result != 0
        mock_install.assert_not_called()

    def test_installer_run_calls__bootstrap_collections(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_store = AsyncMock()
        mock_pipeline.store = mock_store

        mock_sync = AsyncMock(return_value=MagicMock())

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = mock_sync
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_sync.assert_called_once()

    def test_installer_run_warns_when_service_running(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """If service is already running, installer warns but continues."""
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_store = AsyncMock()
        mock_pipeline.store = mock_store

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        assert result == 0
        captured = capsys.readouterr()
        assert "running" in captured.out.lower() or "warning" in captured.out.lower() or "already" in captured.out.lower()

    def test_run_prints_step_labels(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """All 5 step labels [1/5]–[5/5] must appear in stdout for a successful run."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[1/5]" in captured.out
        assert "[2/5]" in captured.out
        assert "[3/5]" in captured.out
        assert "[4/5]" in captured.out
        assert "[5/5]" in captured.out

    def test_run_prints_validating_message_for_apple_silicon(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """For apple_silicon GPU, stdout must contain '[2/5] Validating GPU acceleration'."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[2/5] Validating GPU acceleration" in captured.out

    def test_run_prints_coreml_validation_failed_message(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """For apple_silicon GPU where validate_providers returns False, stdout must contain warning message."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=False), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[2/5] Warning: CoreML validation failed" in captured.out

    def test_run_prints_providers_configured_for_non_apple_silicon(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """For non-apple_silicon GPU (e.g. 'none'), stdout must contain '[2/5] Providers configured for none'."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[2/5] Providers configured for none" in captured.out

    def test_run_prints_packages_already_installed_when_no_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When check_deps() returns [], stdout must contain 'already installed'."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "already installed" in captured.out

    def test_run_prints_installing_packages_when_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When check_deps() returns ['lancedb'], stdout must contain '[1/5] Installing packages: lancedb'."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=["lancedb"]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[1/5] Installing packages: lancedb" in captured.out

    def test_run_prints_packages_installed_confirmation_when_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When check_deps() returns ['lancedb'], stdout must contain '[1/5] Packages installed.'."""
        installer = _make_installer(tmp_path)
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_pipeline.store = AsyncMock()

        with patch("archon.rag.install.get_rag_service", return_value=svc), \
             patch("archon.rag.install.create_pipeline", return_value=mock_pipeline), \
             patch("archon.rag.sync.RagCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=["lancedb"]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "validate_providers", return_value=True), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "[1/5] Packages installed." in captured.out


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
# validate_providers
# ---------------------------------------------------------------------------


class TestValidateProviders:
    def test_validate_providers_returns_true_on_empty_list(self, tmp_path: Path) -> None:
        """Empty providers list → no GPU provider check needed → embed test passes → True."""
        installer = _make_installer(tmp_path)

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"fastembed": mock_te_module}):
            result = installer.validate_providers([])

        assert result is True
        mock_te_module.TextEmbedding.assert_called_once_with(
            installer.cfg.embedding_model, providers=[]
        )

    def test_validate_providers_returns_true_on_success(self, tmp_path: Path) -> None:
        """All GPU providers available and embed succeeds → True."""
        installer = _make_installer(tmp_path)

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is True

    def test_validate_providers_returns_false_on_exception(self, tmp_path: Path) -> None:
        """TextEmbedding constructor raises → False, no exception propagated."""
        installer = _make_installer(tmp_path)

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = MagicMock(side_effect=RuntimeError("init failed"))

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False

    def test_validate_providers_returns_false_on_embed_exception(self, tmp_path: Path) -> None:
        """embed() call raises → False, no exception propagated."""
        installer = _make_installer(tmp_path)

        mock_model = MagicMock()
        mock_model.embed.side_effect = RuntimeError("embed failed")

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = MagicMock(return_value=mock_model)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False

    def test_validate_providers_returns_false_when_provider_not_in_available(self, tmp_path: Path) -> None:
        """GPU provider not in onnxruntime available providers → False immediately."""
        installer = _make_installer(tmp_path)

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CPUExecutionProvider"]
        mock_te_cls = MagicMock(return_value=mock_model)
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = mock_te_cls

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False
        # TextEmbedding should NOT be called — returned False early
        mock_te_cls.assert_not_called()

    def test_validate_providers_returns_false_when_onnxruntime_not_installed(self, tmp_path: Path) -> None:
        """onnxruntime not importable → False."""
        installer = _make_installer(tmp_path)

        with patch.dict("sys.modules", {"onnxruntime": None}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False

    def test_validate_providers_passes_correct_providers_to_text_embedding(self, tmp_path: Path) -> None:
        """The exact providers list is forwarded to TextEmbedding constructor."""
        installer = _make_installer(tmp_path)
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        mock_te_cls = MagicMock(return_value=mock_model)
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = mock_te_cls

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(providers)

        assert result is True
        mock_te_cls.assert_called_once_with(installer.cfg.embedding_model, providers=providers)

    def test_validate_providers_uses_configured_embedding_model(self, tmp_path: Path) -> None:
        """validate_providers uses self.cfg.embedding_model, not a hardcoded string."""
        installer = _make_installer(tmp_path)
        installer.cfg.embedding_model = "custom/model-v2"

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        mock_te_cls = MagicMock(return_value=mock_model)
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = mock_te_cls

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is True
        mock_te_cls.assert_called_once_with("custom/model-v2", providers=["CUDAExecutionProvider"])

    def test_validate_providers_returns_false_when_fastembed_not_installed(self, tmp_path: Path) -> None:
        """fastembed not importable → False, no exception propagated."""
        installer = _make_installer(tmp_path)

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": None}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False

    def test_validate_providers_cpu_only_skips_onnxruntime(self, tmp_path: Path) -> None:
        """CPUExecutionProvider-only list → onnxruntime check skipped → embed test still runs."""
        installer = _make_installer(tmp_path)

        mock_model = MagicMock()
        mock_model.embed.return_value = iter([[0.1, 0.2]])
        mock_te_cls = MagicMock(return_value=mock_model)
        mock_te_module = MagicMock()
        mock_te_module.TextEmbedding = mock_te_cls

        mock_ort = MagicMock()

        with patch.dict("sys.modules", {"onnxruntime": mock_ort, "fastembed": mock_te_module}):
            result = installer.validate_providers(["CPUExecutionProvider"])

        assert result is True
        # onnxruntime.get_available_providers must NOT have been called — non_cpu is empty
        mock_ort.get_available_providers.assert_not_called()
        # but embed test still ran
        mock_te_cls.assert_called_once()


# ---------------------------------------------------------------------------
# _wait_for_service — progress dots and timeout constant
# ---------------------------------------------------------------------------


class TestWaitForService:
    def test_wait_for_service_default_timeout_is_60(self) -> None:
        """_WAIT_FOR_SERVICE_TIMEOUT module constant must equal 60."""
        from archon.rag.install import _WAIT_FOR_SERVICE_TIMEOUT

        assert _WAIT_FOR_SERVICE_TIMEOUT == 60

    def test_wait_for_service_prints_dots_then_ready(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints dots while waiting, then ' ready.' when service comes up."""
        import time as time_module

        installer = _make_installer(tmp_path)

        # Provide enough monotonic values: first call sets start, then loop checks deadline.
        # timeout=60 → deadline = start + 60
        # [0.0] → start=0.0, deadline=60.0
        # [1.0, 2.0, 3.0] → three while-condition checks (False, False, True → exits)
        monotonic_values = [0.0, 1.0, 2.0, 3.0]

        with patch.object(installer, "_is_service_running", side_effect=[False, False, True]), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service()

        captured = capsys.readouterr()
        assert result is True
        assert ".." in captured.out
        assert "ready." in captured.out

    def test_wait_for_service_prints_timed_out_on_timeout(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints ' timed out.' when deadline is exceeded."""
        import time as time_module

        installer = _make_installer(tmp_path)

        # monotonic values: start=0.0 (deadline=60.0), then 10.0, 30.0, 70.0 (exceeds deadline)
        monotonic_values = [0.0, 10.0, 30.0, 70.0]

        with patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service()

        captured = capsys.readouterr()
        assert result is False
        assert "timed out." in captured.out

    def test_wait_for_service_keyboard_interrupt_prints_newline(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """KeyboardInterrupt in the loop must print a newline before re-raising."""
        import time as time_module

        installer = _make_installer(tmp_path)

        monotonic_values = [0.0, 1.0]

        with patch.object(installer, "_is_service_running", side_effect=KeyboardInterrupt), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            with pytest.raises(KeyboardInterrupt):
                installer._wait_for_service()

        captured = capsys.readouterr()
        assert "Waiting for RAG service" in captured.out
        assert "\n" in captured.out


# ---------------------------------------------------------------------------
# run() — service-readiness error path and call-site changes
# ---------------------------------------------------------------------------


class TestRunServiceReadiness:
    def _base_run_patches(self, installer: object) -> dict:
        return {
            "detect_gpu": MagicMock(return_value="none"),
            "check_deps": MagicMock(return_value=[]),
            "install_deps": MagicMock(),
            "configure_providers": MagicMock(),
            "validate_providers": MagicMock(return_value=True),
            "create_data_dir": MagicMock(),
            "_bootstrap_collections": AsyncMock(),
            "write_service_file": MagicMock(),
            "load_service": MagicMock(return_value=0),
        }

    def test_run_returns_error_code_when_service_not_ready(self, tmp_path: Path) -> None:
        """run() must return 1 when _wait_for_service returns False."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "_wait_for_service", return_value=False), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True)

        assert result == 1

    def test_run_prints_error_message_when_service_not_ready(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run() error message must mention the timeout value from the constant (60 seconds)."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "_wait_for_service", return_value=False), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert "within 60 seconds" in captured.out

    def test_run_calls_wait_for_service_without_explicit_timeout(self, tmp_path: Path) -> None:
        """run() must call _wait_for_service() with no arguments (uses default timeout)."""
        from unittest.mock import call

        installer = _make_installer(tmp_path)

        mock_wait = MagicMock(return_value=True)
        with patch.object(installer, "_wait_for_service", mock_wait), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        assert mock_wait.call_args == call()

    def test_run_returns_error_code_when_load_service_fails(self, tmp_path: Path) -> None:
        """run() must return the error code from load_service() and not call _wait_for_service."""
        installer = _make_installer(tmp_path)

        mock_wait = MagicMock(return_value=True)
        with patch.object(installer, "_wait_for_service", mock_wait), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=2), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True)

        assert result == 2
        mock_wait.assert_not_called()

    def test_dry_run_does_not_print_wait_for_service_output(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """dry_run=True must not print 'Waiting for RAG service' progress output."""
        installer = _make_installer(tmp_path, dry_run=True)

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "configure_providers"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert "Waiting for RAG service" not in captured.out

    def test_validate_providers_returns_false_when_get_available_providers_raises(self, tmp_path: Path) -> None:
        """onnxruntime imports fine but get_available_providers() raises → False."""
        installer = _make_installer(tmp_path)

        mock_ort = MagicMock()
        mock_ort.get_available_providers.side_effect = RuntimeError("ort internal error")

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False

    def test_validate_providers_logs_warning_on_missing_provider(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A warning is logged when a provider is not in onnxruntime's available list."""
        import logging
        installer = _make_installer(tmp_path)

        mock_ort = MagicMock()
        mock_ort.get_available_providers.return_value = ["CPUExecutionProvider"]

        with caplog.at_level(logging.WARNING, logger="archon"), \
             patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            result = installer.validate_providers(["CUDAExecutionProvider"])

        assert result is False
        assert any("CUDAExecutionProvider" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# run() — validation-first flow (Task 3.2)
# ---------------------------------------------------------------------------


class TestRunFlow:
    """Tests for the validation-first flow wired into run()."""

    def test_run_flow_apple_silicon_validation_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """apple_silicon: validate → success → configure_providers called → success message printed."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "validate_providers", return_value=True) as mock_validate, \
             patch.object(installer, "configure_providers") as mock_configure, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new=AsyncMock()), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_validate.assert_called_once_with(["CoreMLExecutionProvider"])
        mock_configure.assert_called_once_with(gpu="apple_silicon")
        captured = capsys.readouterr()
        assert "CoreML acceleration validated" in captured.out
        assert "Warning" not in captured.out

    def test_run_flow_apple_silicon_validation_fails_falls_back(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """apple_silicon: validate → failure → configure_providers NOT called → warning printed."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "validate_providers", return_value=False) as mock_validate, \
             patch.object(installer, "configure_providers") as mock_configure, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new=AsyncMock()), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_validate.assert_called_once_with(["CoreMLExecutionProvider"])
        mock_configure.assert_not_called()
        captured = capsys.readouterr()
        assert "Warning: CoreML validation failed" in captured.out

    def test_run_flow_cuda_skips_validation(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """cuda: validation NOT called → configure_providers called directly, no CoreML messages."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="cuda"), \
             patch.object(installer, "install_deps") as mock_install, \
             patch.object(installer, "check_deps", return_value=["some-dep"]), \
             patch.object(installer, "validate_providers") as mock_validate, \
             patch.object(installer, "configure_providers") as mock_configure, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new=AsyncMock()), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_install.assert_called_once_with(gpu="cuda")
        mock_validate.assert_not_called()
        mock_configure.assert_called_once_with(gpu="cuda")
        captured = capsys.readouterr()
        assert "CoreML" not in captured.out

    def test_run_flow_no_gpu_unchanged(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """no gpu: validation NOT called → configure_providers called (no-op internally), no CoreML messages."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "install_deps") as mock_install, \
             patch.object(installer, "check_deps", return_value=["some-dep"]), \
             patch.object(installer, "validate_providers") as mock_validate, \
             patch.object(installer, "configure_providers") as mock_configure, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new=AsyncMock()), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_install.assert_called_once_with(gpu="none")
        mock_validate.assert_not_called()
        mock_configure.assert_called_once_with(gpu="none")
        captured = capsys.readouterr()
        assert "CoreML" not in captured.out

    def test_run_flow_apple_silicon_fallback_does_not_write_providers_to_config(self, tmp_path: Path) -> None:
        """Fallback path: providers key must be absent from the real config.toml."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[rag]\nenabled = true\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        with patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "validate_providers", return_value=False), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new=AsyncMock()), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        import tomlkit
        doc = tomlkit.parse(config_file.read_text())
        rag_section = doc.get("rag", {})
        assert "providers" not in rag_section

    def test_run_flow_dry_run_skips_validation(self, tmp_path: Path) -> None:
        """dry_run=True: validate_providers skipped, configure_providers called directly."""
        installer = _make_installer(tmp_path, dry_run=True)

        with patch.object(installer, "detect_gpu", return_value="apple_silicon"), \
             patch.object(installer, "install_deps"), \
             patch.object(installer, "check_deps", return_value=[]), \
             patch.object(installer, "validate_providers") as mock_validate, \
             patch.object(installer, "configure_providers") as mock_configure, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_validate.assert_not_called()
        mock_configure.assert_called_once_with(gpu="apple_silicon")


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


# ---------------------------------------------------------------------------
# _is_service_running
# ---------------------------------------------------------------------------


class TestIsServiceRunning:
    def test_is_service_running_returns_true_on_200(self, tmp_path: Path) -> None:
        """urlopen succeeds → _is_service_running returns True."""
        installer = _make_installer(tmp_path)
        cm = MagicMock()
        with patch("urllib.request.urlopen", return_value=cm) as mock_urlopen:
            assert installer._is_service_running() is True
        expected_url = f"http://{installer.cfg.host}:{installer.cfg.port}/health"
        mock_urlopen.assert_called_once_with(expected_url, timeout=1)

    def test_is_service_running_returns_false_on_http_error(self, tmp_path: Path) -> None:
        """urlopen raises HTTPError → _is_service_running returns False."""
        import urllib.error
        installer = _make_installer(tmp_path)
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            url=None, code=404, msg="Not Found", hdrs=None, fp=None  # type: ignore[arg-type]
        )):
            assert installer._is_service_running() is False

    def test_is_service_running_returns_false_on_connection_refused(self, tmp_path: Path) -> None:
        """urlopen raises ConnectionRefusedError → _is_service_running returns False."""
        installer = _make_installer(tmp_path)
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            assert installer._is_service_running() is False

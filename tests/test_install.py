"""Tests for SearchInstaller — TDD first (RED phase)."""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_search_config(tmp_path: Path) -> object:
    """Build a minimal SearchConfig-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class FakeProviderConfig:
        ollama_base_url: str = "http://localhost:11434"
        llama_cpp_base_url: str = "http://localhost:8080"

    @dataclass
    class FakeSearchConfig:
        enabled: bool = True
        host: str = "localhost"
        port: int = 8282
        db_path: str = str(tmp_path / "rag_db")
        log_file: str = ""
        embedding_model: str = "BAAI/bge-small-en-v1.5"
        reranker_model: str = "BAAI/bge-reranker-v2-m3"
        device: str = "auto"
        top_k_retrieve: int = 20
        top_k_return: int = 5
        chunk_size: int = 512
        collections: list[str] = field(default_factory=list)
        pinned_collections: list[str] = field(default_factory=lambda: ["/pinned/docs"])
        auto_reindex_on_chunk_size_change: bool = True
        # G10 Ollama picker: run() threads these into the wizard's base-URL prompt.
        hyde: FakeProviderConfig = field(default_factory=FakeProviderConfig)
        rag_fusion: FakeProviderConfig = field(default_factory=FakeProviderConfig)
        graph: FakeProviderConfig = field(default_factory=FakeProviderConfig)

    return FakeSearchConfig()


def _make_full_config(tmp_path: Path) -> object:
    """Build a minimal full Config-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class FakeHistoryConfig:
        directory: str = str(tmp_path / "history")

    @dataclass
    class FakeSearchConfigInner:
        collections: list[str] = field(default_factory=list)
        pinned_collections: list[str] = field(default_factory=lambda: ["/pinned/docs"])
        embedding_model: str = "BAAI/bge-small-en-v1.5"
        chunk_size: int = 512
        auto_reindex_on_chunk_size_change: bool = True

        @property
        def all_indexed_collections(self) -> list[str]:
            from pathlib import Path as _Path
            seen: set[str] = set()
            result: list[str] = []
            for raw in self.pinned_collections + self.collections:
                resolved = str(_Path(raw).expanduser().resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(raw)
            return result

    @dataclass
    class FakeFullConfig:
        history: FakeHistoryConfig = None  # type: ignore[assignment]
        search: FakeSearchConfigInner = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            self.history = FakeHistoryConfig()
            self.search = FakeSearchConfigInner()

    return FakeFullConfig()


class TestSearchInstallerInit:
    def test_init_succeeds_without_telegram_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SearchInstaller.__init__ must not require TELEGRAM_BOT_TOKEN — uses archon_search.config."""
        from archon_search.install import create_installer

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config_toml = tmp_path / "archon-search.toml"
        config_toml.write_text("")  # empty — valid minimal config

        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            installer = create_installer(config_file=str(config_toml))

        assert installer.cfg is not None

    def test_default_config_file_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SearchInstaller default config_file is None; it loads the default <data dir>/archon-search.toml."""
        from archon_search.install import create_installer

        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
        with patch("archon_search.install.installer.load_config", return_value=MagicMock()) as mock_load:
            installer = create_installer()
        assert installer.config_file is None
        assert installer.config_path == tmp_path / "archon-search.toml"
        mock_load.assert_called_once_with(tmp_path / "archon-search.toml", drop_removed_keys=False)

    @pytest.mark.parametrize("config_arg", ["conf/archon-search.toml", "~/conf/archon-search.toml"])
    def test_config_path_is_absolute_for_relative_or_tilde_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_arg: str
    ) -> None:
        """A relative / ``~`` ``--config`` is made absolute: it is baked into the unit/plist, whose cwd differs."""
        from archon_search.install import create_installer

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            installer = create_installer(config_file=config_arg)
        assert installer.config_path == tmp_path / "conf" / "archon-search.toml"

    def test_config_path_keeps_symlinks(self, tmp_path: Path) -> None:
        """A ``--config`` through a symlinked directory is baked in as given, not as the link's target."""
        from archon_search.install import create_installer

        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        config_arg = tmp_path / "link" / "archon-search.toml"
        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            installer = create_installer(config_file=str(config_arg))
        assert installer.config_path == config_arg


def _make_installer(tmp_path: Path, dry_run: bool = False) -> object:
    """Create an installer with a fake config injected (bypasses __init__)."""
    from archon_search.install import DryRunInstaller, RealInstaller

    cls = DryRunInstaller if dry_run else RealInstaller
    installer = cls.__new__(cls)
    installer.cfg = _make_search_config(tmp_path)
    installer.config_file = str(tmp_path / "archon-search.toml")
    return installer


# ---------------------------------------------------------------------------
# detect_gpu
# ---------------------------------------------------------------------------


class TestDetectGpu:
    def test_detect_gpu_delegates_to_platform_runtime(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = GpuType.CUDA
        with patch("archon_search.install.installer.get_runtime", return_value=mock_runtime):
            result = installer.detect_gpu()
        assert result == GpuType.CUDA
        mock_runtime.detect_gpu_type.assert_called_once()

    def test_detect_gpu_returns_cuda(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = GpuType.CUDA
        with patch("archon_search.install.installer.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == GpuType.CUDA

    def test_detect_gpu_returns_apple_silicon(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = GpuType.METAL
        with patch("archon_search.install.installer.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == GpuType.METAL

    def test_detect_gpu_returns_none_on_intel_mac(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "none"
        with patch("archon_search.install.installer.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "none"

    def test_detect_gpu_returns_none_on_linux_no_cuda(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        mock_runtime = MagicMock()
        mock_runtime.detect_gpu_type.return_value = "none"
        with patch("archon_search.install.installer.get_runtime", return_value=mock_runtime):
            assert installer.detect_gpu() == "none"


# ---------------------------------------------------------------------------
# configure_device
# ---------------------------------------------------------------------------


class TestConfigureDevice:
    @staticmethod
    def _installer(tmp_path: Path, content: str | None, dry_run: bool = False) -> tuple[object, Path]:
        config_file = tmp_path / "archon-search.toml"
        if content is not None:
            config_file.write_text(content)
        installer = _make_installer(tmp_path, dry_run=dry_run)
        installer.config_file = str(config_file)
        return installer, config_file

    def test_writes_device_into_database_section(self, tmp_path: Path) -> None:
        import tomlkit

        installer, config_file = self._installer(tmp_path, "[database]\n")

        installer.configure_device("mps")

        assert tomlkit.parse(config_file.read_text())["database"]["device"] == "mps"

    def test_replaces_existing_device(self, tmp_path: Path) -> None:
        import tomlkit

        installer, config_file = self._installer(tmp_path, '[database]\ndevice = "cpu"\n')

        installer.configure_device("auto")

        assert tomlkit.parse(config_file.read_text())["database"]["device"] == "auto"

    def test_creates_missing_section(self, tmp_path: Path) -> None:
        import tomlkit

        installer, config_file = self._installer(tmp_path, "")

        installer.configure_device("cpu")

        assert tomlkit.parse(config_file.read_text())["database"]["device"] == "cpu"

    def test_graph_section_leaves_database_untouched(self, tmp_path: Path) -> None:
        import tomlkit

        installer, config_file = self._installer(tmp_path, "[database]\n")

        installer.configure_device("cuda", section="graph")

        doc = tomlkit.parse(config_file.read_text())
        assert doc["graph"]["device"] == "cuda"
        assert "device" not in doc["database"]

    def test_unchanged_value_does_not_rewrite_file(self, tmp_path: Path) -> None:
        original = '[database]\n# keep this comment\ndevice   =   "cpu"\n'
        installer, config_file = self._installer(tmp_path, original)

        installer.configure_device("cpu")

        assert config_file.read_text() == original

    def test_unknown_device_raises_and_leaves_file_untouched(self, tmp_path: Path) -> None:
        installer, config_file = self._installer(tmp_path, "[database]\n")

        with pytest.raises(ValueError, match="unknown device 'tpu'"):
            installer.configure_device("tpu")

        assert config_file.read_text() == "[database]\n"

    def test_missing_config_file_is_skipped(self, tmp_path: Path) -> None:
        installer, config_file = self._installer(tmp_path, None)

        installer.configure_device("cpu")

        assert not config_file.exists()

    def test_dry_run_narrates_and_writes_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        installer, config_file = self._installer(tmp_path, "[database]\n", dry_run=True)

        installer.configure_device("cuda")
        installer.configure_device("cpu", section="graph")

        assert capsys.readouterr().out.splitlines() == [
            "[DRY RUN] Would set [database] device = 'cuda'.",
            "[DRY RUN] Would set [graph] device = 'cpu'.",
        ]
        assert config_file.read_text() == "[database]\n"

    def test_dry_run_rejects_unknown_device(self, tmp_path: Path) -> None:
        installer, _ = self._installer(tmp_path, "[database]\n", dry_run=True)

        with pytest.raises(ValueError, match="unknown device 'tpu'"):
            installer.configure_device("tpu")


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
    @pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
    def test_write_service_file_replaces_registration_via_platform(self, tmp_path: Path, dry_run: bool) -> None:
        """write_service_file() hands validate → stop → register to the platform's replace_registration()."""
        installer = _make_installer(tmp_path, dry_run=dry_run)
        svc = MagicMock()

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            installer.write_service_file()

        svc.replace_registration.assert_called_once_with(
            dry_run=dry_run, config_path=str(tmp_path / "archon-search.toml")
        )

    @pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
    def test_write_service_file_refused_systemd_registration_leaves_service_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
    ) -> None:
        """The real systemd service refuses an interpreter path it cannot run before stopping anything or writing a unit."""
        from archon_search.platform.linux import SystemdSearchService

        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(sys, "executable", str(tmp_path / "py$env" / "bin" / "python3"))
        installer = _make_installer(tmp_path, dry_run=dry_run)
        svc = SystemdSearchService()
        unit = tmp_path / "archon-search.service"

        with (
            patch("archon_search.install.installer.get_search_service", return_value=svc),
            patch.object(SystemdSearchService, "_unit_path", property(lambda self: unit)),
            patch.object(svc, "_run") as run,
            patch.object(svc, "stop") as stop,
            pytest.raises(RuntimeError, match="interpreter path"),
        ):
            installer.write_service_file()

        stop.assert_not_called()
        run.assert_not_called()
        assert not unit.exists()

    @pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
    @pytest.mark.parametrize(
        ("config_dir", "data_name", "python", "refusal"),
        [
            ("bad\x01conf", "data", sys.executable, "launchd plist cannot hold a config path"),
            ("conf", "bad\x01data", sys.executable, "launchd plist cannot hold a data directory"),
            ("conf", "data", "/opt/py\x00thon/bin/python3", "wrapper script cannot hold an interpreter path"),
        ],
        ids=["config-path", "data-dir", "interpreter"],
    )
    def test_write_service_file_refused_launchd_registration_leaves_service_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        dry_run: bool,
        config_dir: str,
        data_name: str,
        python: str,
        refusal: str,
    ) -> None:
        """The real launchd service refuses a value the plist or wrapper cannot carry before stopping anything or writing."""
        from archon_search.platform.macos import LaunchdSearchService

        data_dir = tmp_path / data_name
        monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
        monkeypatch.setattr(sys, "executable", python)
        installer = _make_installer(tmp_path, dry_run=dry_run)
        installer.config_file = str(tmp_path / config_dir / "archon-search.toml")
        svc = LaunchdSearchService()
        plist = tmp_path / "LaunchAgents" / "com.archon.search.plist"

        with (
            patch("archon_search.install.installer.get_search_service", return_value=svc),
            patch.object(LaunchdSearchService, "_plist_path", property(lambda self: plist)),
            patch.object(svc, "_run") as run,
            patch.object(svc, "stop") as stop,
            pytest.raises(RuntimeError, match=f"^{re.escape(refusal)} "),
        ):
            installer.write_service_file()

        stop.assert_not_called()
        run.assert_not_called()
        assert not plist.exists()
        assert not data_dir.exists()


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

        with patch("archon_search.install.installer.create_pipeline", return_value=mock_pipeline), \
             patch("archon_search.sync.SearchCollectionSync") as MockSync:
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

        with patch("archon_search.install.installer.create_pipeline", return_value=mock_pipeline), \
             patch("archon_search.sync.SearchCollectionSync") as MockSync:
            MockSync.return_value.sync = AsyncMock(side_effect=RuntimeError("boom"))
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(installer._bootstrap_collections())

        mock_store.disconnect.assert_called_once()

    def test_bootstrap_collections_passes_config_params(self, tmp_path: Path) -> None:
        """SearchCollectionSync must receive chunk_size, auto_reindex_on_chunk_size_change, pinned_collections."""
        installer = _make_installer(tmp_path)

        mock_store = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store

        with patch("archon_search.install.installer.create_pipeline", return_value=mock_pipeline), \
             patch("archon_search.sync.SearchCollectionSync") as MockSync:
            MockSync.return_value.sync = AsyncMock()
            asyncio.run(installer._bootstrap_collections())

        MockSync.assert_called_once()
        _, kwargs = MockSync.call_args
        assert "global_embedding_model" not in kwargs
        assert kwargs["chunk_size"] == installer.cfg.chunk_size
        assert kwargs["auto_reindex_on_chunk_size_change"] == installer.cfg.auto_reindex_on_chunk_size_change
        assert kwargs["pinned_collections"] == installer.cfg.pinned_collections

    def test_bootstrap_collections_syncs_all_indexed_collections(self, tmp_path: Path) -> None:
        """sync.sync() is called with pinned_collections from cfg."""
        installer = _make_installer(tmp_path)
        installer.cfg.pinned_collections = ["/pinned/sys", "/user/notes"]
        installer.cfg.embedding_model = "BAAI/bge-small-en-v1.5"
        installer.cfg.chunk_size = 512
        installer.cfg.auto_reindex_on_chunk_size_change = False

        mock_store = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.store = mock_store
        mock_sync = AsyncMock()

        with patch("archon_search.install.installer.create_pipeline", return_value=mock_pipeline), \
             patch("archon_search.sync.SearchCollectionSync") as MockSync:
            MockSync.return_value.sync = mock_sync
            asyncio.run(installer._bootstrap_collections())

        mock_sync.assert_called_once()
        synced_paths = mock_sync.call_args[0][0]
        assert "/pinned/sys" in synced_paths
        assert "/user/notes" in synced_paths


# ---------------------------------------------------------------------------
# run() — full install flow
# ---------------------------------------------------------------------------


class TestRun:
    def _base_patches(self, tmp_path: Path) -> dict[str, object]:
        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0
        return {
            "archon_search.install.installer.get_search_service": MagicMock(return_value=svc),
            "archon_search.install.installer.create_pipeline": MagicMock(
                return_value=MagicMock(
                    store=AsyncMock(),
                    ingest_directory=AsyncMock(return_value=[]),
                )
            ),
        }

    def test_run_aborts_on_user_decline(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        write_service_mock = MagicMock()
        with patch("builtins.input", return_value="n"), \
             patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "write_service_file", write_service_mock), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(
                non_interactive=False,
                profile="minimal",
                skip_preload=True,
            )

        assert result != 0
        write_service_mock.assert_not_called()

    def test_installer_run_warns_when_service_running(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """If service is already running, installer warns but continues."""
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.register.return_value = 0
        svc.start.return_value = 0

        mock_pipeline = MagicMock()
        mock_store = AsyncMock()
        mock_pipeline.store = mock_store

        with patch("archon_search.install.installer.get_search_service", return_value=svc), \
             patch("archon_search.install.installer.create_pipeline", return_value=mock_pipeline), \
             patch("archon_search.sync.SearchCollectionSync") as MockSync, \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=True):
            MockSync.return_value.sync = AsyncMock(return_value=MagicMock())
            result = installer.run(non_interactive=True)

        assert result == 0
        captured = capsys.readouterr()
        assert "running" in captured.out.lower() or "warning" in captured.out.lower() or "already" in captured.out.lower()

    def test_run_prints_step_labels(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Step labels [4/5] and [5/5] must appear in stdout for a successful run."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=False)

        captured = capsys.readouterr()
        assert result == 0
        assert "[4/5]" in captured.out
        assert "[5/5]" in captured.out

    def test_run_threads_config_ollama_base_url_into_wizard(self, tmp_path: Path) -> None:
        """run() must thread the saved config Ollama base URLs into the wizard prompt (C1-I-46).

        This is the re-run prefill wiring: on a reconfigure, the base-URL prompt should
        pre-fill the address already in config so the operator keeps it with one Enter.
        """
        from archon_search.install import WizardFeatures

        installer = _make_installer(tmp_path)
        installer.cfg.hyde.ollama_base_url = "http://saved-hyde:11434"
        installer.cfg.rag_fusion.ollama_base_url = "http://saved-rag:11434"
        installer.cfg.hyde.llama_cpp_base_url = "http://saved-hyde:8080"
        installer.cfg.rag_fusion.llama_cpp_base_url = "http://saved-rag:8080"
        installer.cfg.graph.llama_cpp_base_url = "http://saved-graph:8080"
        installer.cfg.graph.ollama_base_url = "http://saved-graph:11434"
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        captured: dict[str, object] = {}

        def _capture(*_args: object, **kwargs: object) -> WizardFeatures:
            captured.update(kwargs)
            return WizardFeatures()

        with patch("archon_search.install.installer._prompt_optional_features", side_effect=_capture), \
             patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        assert result == 0
        assert captured["hyde_ollama_base_url_default"] == "http://saved-hyde:11434"
        assert captured["rag_fusion_ollama_base_url_default"] == "http://saved-rag:11434"
        assert captured["hyde_llama_cpp_base_url_default"] == "http://saved-hyde:8080"
        assert captured["rag_fusion_llama_cpp_base_url_default"] == "http://saved-rag:8080"
        assert captured["graph_llama_cpp_base_url_default"] == "http://saved-graph:8080"
        assert captured["graph_ollama_base_url_default"] == "http://saved-graph:11434"

    def test_run_prints_apple_gpu_device_when_validation_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """METAL GPU whose models pass on mps: the summary names the Apple GPU as the device."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.METAL), \
             patch.object(installer, "_accelerator_validated", return_value=True), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "  Device:     Apple GPU (mps)\n" in captured.out

    def test_run_prints_apple_gpu_validation_failed_message(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """METAL GPU whose validation fails: the warning is printed and CPU is written."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.METAL), \
             patch.object(installer, "_accelerator_validated", return_value=False), \
             patch.object(installer, "configure_device") as configure_mock, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "Warning: Apple GPU validation failed — falling back to CPU." in captured.out
        configure_mock.assert_called_once_with("cpu")

    def test_run_writes_cpu_device_and_prints_cpu_for_no_gpu(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """For NONE GPU, cpu is written and the summary names CPU as the device."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        configure_mock = MagicMock()
        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device", configure_mock), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        configure_mock.assert_called_once_with("cpu")
        assert "  Device:     CPU\n" in captured.out

    def test_run_prints_packages_already_installed_when_no_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run() succeeds and prints completion message (no longer has package step)."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "archon-search installed and running" in captured.out

    def test_run_prints_installing_packages_when_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run() succeeds and prints profile summary (package installation no longer part of run())."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "Embedder" in captured.out

    def test_run_prints_packages_installed_confirmation_when_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run() prints profile_name in completion message."""
        installer = _make_installer(tmp_path)
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=GpuType.NONE), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

        captured = capsys.readouterr()
        assert result == 0
        assert "Minimal" in captured.out


# ---------------------------------------------------------------------------
# run_uninstall()
# ---------------------------------------------------------------------------


class TestRunUninstall:
    def test_run_uninstall_stops_and_unregisters_service(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
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

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        assert not db_path.exists()

    def test_run_uninstall_delete_db_true_shows_deleted_message(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When delete_db=True and DB exists, stdout must mention 'deleted'."""
        installer = _make_installer(tmp_path)
        db_path = tmp_path / "rag_db"
        db_path.mkdir()
        installer.cfg.db_path = str(db_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        out = capsys.readouterr().out
        assert "deleted" in out.lower(), (
            f"Expected 'deleted' in uninstall message when delete_db=True, got: {out}"
        )

    def test_run_uninstall_delete_db_false_shows_preserved_message(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When delete_db=False, stdout must mention 'preserved' (settings preserved)."""
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=False)

        assert result == 0
        out = capsys.readouterr().out
        assert "preserved" in out.lower(), (
            f"Expected 'preserved' in uninstall message when delete_db=False, got: {out}"
        )

    def test_run_uninstall_delete_db_false_preserves_directory(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)
        db_path = tmp_path / "rag_db"
        db_path.mkdir()
        installer.cfg.db_path = str(db_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
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

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        assert db_path.exists()

    def test_run_uninstall_delete_db_true_db_nonexistent_still_succeeds(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """delete_db=True but DB path does not exist → still returns 0 with 'preserved' message."""
        installer = _make_installer(tmp_path)
        # db_path is set to tmp_path / "rag_db" by _make_search_config, but we do NOT mkdir it
        db_path = Path(installer.cfg.db_path)
        assert not db_path.exists(), "pre-condition: DB must not exist for this test"

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=True)

        assert result == 0
        out = capsys.readouterr().out
        # DB was absent → nothing to delete → config preserved message
        assert "preserved" in out.lower(), (
            f"Expected 'preserved' in uninstall message when DB is absent, got: {out}"
        )

    def test_run_uninstall_does_not_modify_config_toml(self, tmp_path: Path) -> None:
        """Uninstall must NEVER write to config.toml — verified by snapshot comparison."""
        installer = _make_installer(tmp_path)

        # Create a config file to snapshot
        config_file = Path(installer.config_file)
        original_content = "[search]\nenabled = true\ndb_path = '/tmp/search'\n"
        config_file.write_text(original_content)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=False)

        assert result == 0
        assert config_file.read_text() == original_content, (
            "run_uninstall must not modify config.toml"
        )


# ---------------------------------------------------------------------------
# _accelerator_validated / _probe_and_configure_device
# ---------------------------------------------------------------------------


_INSTALLER = "archon_search.install.installer"
_VALIDATE_DEVICE = "archon_search.model_validation.validate_device"
_CUDA_GUIDE_LINE = (
    "  Commands for each install method: https://github.com/user538295/archon-search/blob/main/"
    "Documentation/UserManual/10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda"
)


class TestAcceleratorValidated:
    """``_accelerator_validated``: auto must resolve to an accelerator and both models must pass on it."""

    def test_cpu_resolution_is_false_without_probing(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.resolve_device", return_value="cpu"), \
             patch(_VALIDATE_DEVICE) as validate:
            assert installer._accelerator_validated() is False

        validate.assert_not_called()

    @pytest.mark.parametrize("resolved", ["mps", "cuda"])
    def test_both_models_pass_on_accelerator(self, tmp_path: Path, resolved: str) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.resolve_device", return_value=resolved), \
             patch(_VALIDATE_DEVICE, return_value=(True, True, [])) as validate:
            assert installer._accelerator_validated() is True

        validate.assert_called_once_with("auto", "BAAI/bge-small-en-v1.5", "BAAI/bge-reranker-v2-m3")

    @pytest.mark.parametrize("outcome", [(False, True, []), (True, False, ["reranker probe failed"])])
    def test_either_model_failing_is_false(self, tmp_path: Path, outcome: tuple) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.resolve_device", return_value="mps"), \
             patch(_VALIDATE_DEVICE, return_value=outcome):
            assert installer._accelerator_validated() is False

    def test_probe_warnings_are_logged_even_when_passing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        installer = _make_installer(tmp_path)

        with caplog.at_level(logging.WARNING), \
             patch(f"{_INSTALLER}.resolve_device", return_value="mps"), \
             patch(_VALIDATE_DEVICE, return_value=(True, True, ["slow first inference"])):
            assert installer._accelerator_validated() is True

        assert any("slow first inference" in r.message for r in caplog.records)

    def test_exception_is_false_and_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        installer = _make_installer(tmp_path)

        with caplog.at_level(logging.WARNING), \
             patch(f"{_INSTALLER}.resolve_device", return_value="cuda"), \
             patch(_VALIDATE_DEVICE, side_effect=RuntimeError("driver crashed")):
            assert installer._accelerator_validated() is False

        assert any("driver crashed" in r.message for r in caplog.records)


class TestProbeAndConfigureDevice:
    """``_probe_and_configure_device``: Step 9's write of ``[database].device`` and the summary labels."""

    def test_no_gpu_writes_cpu_without_probing(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        with patch.object(installer, "configure_device") as configure, \
             patch.object(installer, "_accelerator_validated") as validated:
            labels = installer._probe_and_configure_device(GpuType.NONE)

        assert labels == ["CPU"]
        configure.assert_called_once_with("cpu")
        validated.assert_not_called()

    def test_dry_run_metal_narrates_auto_and_probes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        installer = _make_installer(tmp_path, dry_run=True)

        with patch.object(installer, "_accelerator_validated") as validated, \
             patch(f"{_INSTALLER}._install_cuda_torch") as swap:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["Apple GPU (mps)"]
        assert capsys.readouterr().out == "[DRY RUN] Would set [database] device = 'auto'.\n"
        validated.assert_not_called()
        swap.assert_not_called()

    @pytest.mark.parametrize(
        ("platform_name", "machine", "swaps"),
        [("linux", "x86_64", True), ("linux", "aarch64", False), ("darwin", "x86_64", False)],
    )
    def test_dry_run_cuda_narrates_torch_swap_only_on_linux_x86_64(
        self, tmp_path: Path, platform_name: str, machine: str, swaps: bool
    ) -> None:
        installer = _make_installer(tmp_path, dry_run=True)

        with patch(f"{_INSTALLER}.sys.platform", platform_name), \
             patch(f"{_INSTALLER}.platform.machine", return_value=machine), \
             patch.object(installer, "_accelerator_validated") as validated, \
             patch(f"{_INSTALLER}._install_cuda_torch") as swap:
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CUDA"]
        assert swap.call_args_list == ([call(dry_run=True)] if swaps else [])
        validated.assert_not_called()

    def test_real_metal_validated_writes_auto(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        with patch.object(installer, "_accelerator_validated", return_value=True), \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["Apple GPU (mps)"]
        configure.assert_called_once_with("auto")

    def test_real_metal_validation_failure_falls_back_to_cpu(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        installer = _make_installer(tmp_path)

        with caplog.at_level(logging.WARNING), \
             patch.object(installer, "_accelerator_validated", return_value=False), \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["CPU"]
        configure.assert_called_once_with("cpu")
        assert capsys.readouterr().out == "Warning: Apple GPU validation failed — falling back to CPU.\n"
        assert any(r.message == "Apple GPU validation failed — falling back to CPU" for r in caplog.records)

    def test_real_cuda_validation_failure_prints_guidance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.sys.platform", "darwin"), \
             patch.object(installer, "_accelerator_validated", return_value=False), \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        lines = capsys.readouterr().out.splitlines()
        assert labels == ["CPU"]
        configure.assert_called_once_with("cpu")
        assert lines[0] == "Warning: CUDA validation failed — falling back to CPU."
        assert len(lines) == 4
        assert lines[-1] == _CUDA_GUIDE_LINE

    def test_cuda_on_linux_x86_64_without_cuda_torch_swaps_then_checks_fresh_interpreter(
        self, tmp_path: Path
    ) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.sys.platform", "linux"), \
             patch(f"{_INSTALLER}.platform.machine", return_value="x86_64"), \
             patch(f"{_INSTALLER}._torch_cuda_available", return_value=False), \
             patch(f"{_INSTALLER}._install_cuda_torch") as swap, \
             patch(f"{_INSTALLER}._cuda_visible_in_subprocess", return_value=True), \
             patch.object(installer, "_accelerator_validated") as validated, \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CUDA"]
        swap.assert_called_once_with(dry_run=False)
        configure.assert_called_once_with("auto")
        validated.assert_not_called()

    def test_cuda_swap_not_visible_in_fresh_interpreter_falls_back_to_cpu(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.sys.platform", "linux"), \
             patch(f"{_INSTALLER}.platform.machine", return_value="x86_64"), \
             patch(f"{_INSTALLER}._torch_cuda_available", return_value=False), \
             patch(f"{_INSTALLER}._install_cuda_torch"), \
             patch(f"{_INSTALLER}._cuda_visible_in_subprocess", return_value=False), \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CPU"]
        configure.assert_called_once_with("cpu")
        assert "Warning: CUDA validation failed — falling back to CPU." in capsys.readouterr().out

    def test_cuda_on_linux_x86_64_with_cuda_torch_skips_swap_and_validates(self, tmp_path: Path) -> None:
        installer = _make_installer(tmp_path)

        with patch(f"{_INSTALLER}.sys.platform", "linux"), \
             patch(f"{_INSTALLER}.platform.machine", return_value="x86_64"), \
             patch(f"{_INSTALLER}._torch_cuda_available", return_value=True), \
             patch(f"{_INSTALLER}._install_cuda_torch") as swap, \
             patch.object(installer, "_accelerator_validated", return_value=True), \
             patch.object(installer, "configure_device") as configure:
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CUDA"]
        swap.assert_not_called()
        configure.assert_called_once_with("auto")


# ---------------------------------------------------------------------------
# _wait_for_service — progress dots and timeout constant
# ---------------------------------------------------------------------------


class TestWaitForService:
    def test_wait_for_service_default_timeout_is_60(self) -> None:
        """_WAIT_FOR_SERVICE_TIMEOUT module constant must equal 60."""
        from archon_search.install import _WAIT_FOR_SERVICE_TIMEOUT

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

    def test_wait_for_service_tolerates_one_crash_and_restart(self, tmp_path: Path) -> None:
        """S02: a first-launch import crash + supervisor restart must not be reported as failure.

        A fresh install can launch the service against a not-yet-materialised
        environment; it crashes on import (ModuleNotFoundError), launchd restarts
        it, and the second launch becomes healthy — but only after the crash plus
        the restart throttle have eaten the initial budget. When the service log
        shows that startup crash, the readiness gate must extend the window once so
        the poll survives the restart and returns True.
        """
        import time as time_module

        installer = _make_installer(tmp_path)
        log_path = tmp_path / "archon-search.log"
        log_path.write_text(
            "Traceback (most recent call last):\n"
            "  File \".../fastapi/__init__.py\", line 5, in <module>\n"
            "    from starlette import status as status\n"
            "ModuleNotFoundError: No module named 'starlette'\n"
        )
        installer.cfg.log_file = str(log_path)

        # timeout=10 → initial deadline=10. The crash extends it once to 20.
        # Service only becomes healthy at t=15 — inside the extended window,
        # past the un-extended deadline. Current (un-fixed) code returns False.
        monotonic_values = [0.0, 5.0, 15.0]

        with patch.object(installer, "_is_service_running", side_effect=[False, True]), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service(timeout=10)

        assert result is True

    def test_wait_for_service_surfaces_crash_on_timeout(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """When the service never comes up, the readiness gate surfaces the crash from the log."""
        import time as time_module

        installer = _make_installer(tmp_path)
        log_path = tmp_path / "archon-search.log"
        log_path.write_text(
            "Traceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'starlette'\n"
        )
        installer.cfg.log_file = str(log_path)

        # timeout=10 → deadline 10, extended once to 20; t=25 exceeds it.
        monotonic_values = [0.0, 1.0, 25.0]

        with patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service(timeout=10)

        captured = capsys.readouterr()
        assert result is False
        assert "timed out." in captured.out
        assert "ModuleNotFoundError" in captured.out

    def test_wait_for_service_no_crash_does_not_extend(self, tmp_path: Path) -> None:
        """A service that never comes up with no crash in the log must fail at the normal deadline.

        Regression guard: the crash tolerance must not silently double every
        timeout — only an import-time startup crash grants the extra window.
        """
        import time as time_module

        installer = _make_installer(tmp_path)
        log_path = tmp_path / "archon-search.log"
        log_path.write_text("INFO archon_search.server.app starting up\n")
        installer.cfg.log_file = str(log_path)

        # t=15 exceeds the normal deadline (10) but would be inside an extended
        # window (20). No crash → no extension → must return False.
        monotonic_values = [0.0, 5.0, 15.0]

        with patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service(timeout=10)

        assert result is False

    def test_wait_for_service_ignores_stale_crash_before_offset(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A crash from an earlier run (before log_offset) must not extend the window.

        The service log is rotated daily and appended across runs, so a same-day
        reinstall can find a previous run's ModuleNotFoundError. Only crashes
        written after the offset captured at service (re)start count.
        """
        import time as time_module

        installer = _make_installer(tmp_path)
        log_path = tmp_path / "archon-search.log"
        log_path.write_text("ModuleNotFoundError: No module named 'starlette'\n")
        installer.cfg.log_file = str(log_path)
        stale_offset = log_path.stat().st_size  # everything so far is from a prior run

        # t=15 is inside an extended window (20) but past the normal deadline (10).
        # With the stale crash correctly ignored, no extension → returns False.
        monotonic_values = [0.0, 5.0, 15.0]

        with patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values):
            result = installer._wait_for_service(timeout=10, log_offset=stale_offset)

        captured = capsys.readouterr()
        assert result is False
        assert "ModuleNotFoundError" not in captured.out

    def test_service_log_crash_ignores_non_startup_markers(self, tmp_path: Path) -> None:
        """Only lines starting with a startup-crash marker count — not substring matches."""
        installer = _make_installer(tmp_path)
        log_path = tmp_path / "archon-search.log"
        log_path.write_text(
            "DEBUG caught ImportError for optional gliner, continuing without graph\n"
            "INFO archon_search.server.app started\n"
        )
        installer.cfg.log_file = str(log_path)

        assert installer._service_log_crash() is None

    def test_wait_for_service_keyboard_interrupt_prints_newline(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """KeyboardInterrupt in the loop must print a newline before re-raising."""
        import time as time_module

        installer = _make_installer(tmp_path)

        monotonic_values = [0.0, 1.0]

        with patch.object(installer, "_is_service_running", side_effect=KeyboardInterrupt), \
             patch.object(time_module, "sleep"), \
             patch.object(time_module, "monotonic", side_effect=monotonic_values), pytest.raises(KeyboardInterrupt):
            installer._wait_for_service()

        captured = capsys.readouterr()
        assert "Waiting for search service" in captured.out
        assert "\n" in captured.out


# ---------------------------------------------------------------------------
# run() — service-readiness error path and call-site changes
# ---------------------------------------------------------------------------


class TestRunServiceReadiness:
    def _base_run_patches(self, installer: object) -> dict:
        return {
            "detect_gpu": MagicMock(return_value="none"),
            "configure_device": MagicMock(),
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
             patch.object(installer, "configure_device"), \
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
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert "within 60 seconds" in captured.out

    def test_run_without_eager_load_waits_default_timeout(self, tmp_path: Path) -> None:
        """run() without --eager-load waits the default _WAIT_FOR_SERVICE_TIMEOUT."""
        installer = _make_installer(tmp_path)

        mock_wait = MagicMock(return_value=True)
        with patch.object(installer, "_wait_for_service", mock_wait), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        from archon_search.install import _WAIT_FOR_SERVICE_TIMEOUT
        assert mock_wait.call_args.kwargs["timeout"] == _WAIT_FOR_SERVICE_TIMEOUT

    def test_run_with_eager_load_waits_scaled_timeout(self, tmp_path: Path) -> None:
        """run(eager_load=True) ends with a planned-bytes-scaled wait timeout, not the 60 s default.

        Proves the flag reaches the timeout end-to-end; it does not pin whether the
        value is read from the reloaded config or from WizardFeatures.
        """
        from archon_search.install import _WAIT_FOR_SERVICE_TIMEOUT
        from archon_search.install.installer import _compute_svc_timeout
        from archon_search.install.provisioning import BYTES_PER_MB

        planned_bytes = 3000 * BYTES_PER_MB  # large enough to scale past the 60 s floor
        expected_timeout = _compute_svc_timeout(True, planned_bytes)
        assert expected_timeout != _WAIT_FOR_SERVICE_TIMEOUT
        installer = _make_installer(tmp_path)

        mock_wait = MagicMock(return_value=True)
        with patch.object(installer, "_wait_for_service", mock_wait), \
             patch("archon_search.install.installer._planned_total_bytes", return_value=planned_bytes), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True, eager_load=True)

        assert mock_wait.call_args.kwargs["timeout"] == expected_timeout

    def test_run_returns_error_code_when_load_service_fails(self, tmp_path: Path) -> None:
        """run() must return the error code from load_service() and not call _wait_for_service."""
        installer = _make_installer(tmp_path)

        mock_wait = MagicMock(return_value=True)
        with patch.object(installer, "_wait_for_service", mock_wait), \
             patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "_bootstrap_collections", new_callable=AsyncMock), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=2), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True)

        assert result == 2
        mock_wait.assert_not_called()

    def test_dry_run_does_not_print_wait_for_service_output(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """dry_run=True must not print 'Waiting for search service' progress output."""
        installer = _make_installer(tmp_path, dry_run=True)

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            installer.run(non_interactive=True)

        captured = capsys.readouterr()
        assert "Waiting for search service" not in captured.out


# ---------------------------------------------------------------------------
# run — validation-first flow 
# ---------------------------------------------------------------------------


class TestRunFlow:
    """Tests for the device probe wired into run()."""

    def _run(
        self, installer: object, tmp_path: Path, gpu: object, *, validated: object | None = None, **run_kwargs: object
    ) -> int:
        config_path = Path(installer.config_file)
        fake_legacy = tmp_path / "fake.plist"

        with patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy), \
             patch("archon_search.install.installer._remove_legacy_service"), \
             patch("archon_search.install.installer._prewarm_models"), \
             patch("archon_search.install.installer._check_disk_space"), \
             patch("archon_search.install.installer._install_cuda_torch"), \
             patch("archon_search.install.installer.get_default_config_path", return_value=config_path), \
             patch.object(installer, "detect_gpu", return_value=gpu), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            if validated is None:
                return installer.run(non_interactive=True, profile="minimal", skip_preload=True, **run_kwargs)
            with patch.object(installer, "_accelerator_validated", return_value=validated) as probe:
                self.probe = probe
                return installer.run(non_interactive=True, profile="minimal", skip_preload=True, **run_kwargs)

    def test_run_flow_apple_silicon_validation_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """METAL GPU: validation passes -> auto is written and the device line names the Apple GPU."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "configure_device") as mock_configure:
            result = self._run(installer, tmp_path, GpuType.METAL, validated=True)

        assert result == 0
        self.probe.assert_called_once_with()
        assert mock_configure.call_args_list == [call("auto")]
        out = capsys.readouterr().out
        assert "  Device:     Apple GPU (mps)\n" in out
        assert "validation failed" not in out

    def test_run_flow_apple_silicon_validation_fails_falls_back(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """METAL GPU: validation fails -> cpu is written and the warning is printed."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "configure_device") as mock_configure:
            result = self._run(installer, tmp_path, GpuType.METAL, validated=False)

        assert result == 0
        self.probe.assert_called_once_with()
        assert mock_configure.call_args_list == [call("cpu")]
        assert "Warning: Apple GPU validation failed — falling back to CPU." in capsys.readouterr().out

    @pytest.mark.usefixtures("off_swap_platform")
    @pytest.mark.parametrize(
        ("cuda_ok", "device"), [(True, "auto"), (False, "cpu")], ids=["probe-passes", "probe-fails"]
    )
    def test_run_flow_cuda_validates_before_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, cuda_ok: bool, device: str
    ) -> None:
        """CUDA: auto is written only when validation passes, else a warning and cpu."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "configure_device") as mock_configure:
            result = self._run(installer, tmp_path, GpuType.CUDA, validated=cuda_ok)

        assert result == 0
        assert mock_configure.call_args_list == [call(device)]
        out = capsys.readouterr().out
        assert ("Warning: CUDA validation failed" in out) is (not cuda_ok)
        assert "Apple GPU" not in out

    def test_run_flow_no_gpu_unchanged(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """NONE GPU: nothing is validated, cpu is written, no accelerator messages."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "_accelerator_validated") as mock_validate, \
             patch.object(installer, "configure_device") as mock_configure:
            result = self._run(installer, tmp_path, GpuType.NONE)

        assert result == 0
        mock_validate.assert_not_called()
        assert mock_configure.call_args_list == [call("cpu")]
        out = capsys.readouterr().out
        assert "Apple GPU" not in out
        assert "  Device:     CPU\n" in out

    def test_run_flow_apple_silicon_fallback_writes_cpu_to_config(self, tmp_path: Path) -> None:
        """Fallback path: the real config.toml ends with [database].device = "cpu"."""
        config_file = tmp_path / "archon-search.toml"
        config_file.write_text("[database]\n")

        installer = _make_installer(tmp_path)
        installer.config_file = str(config_file)

        result = self._run(installer, tmp_path, GpuType.METAL, validated=False)

        assert result == 0
        import tomlkit
        doc = tomlkit.parse(config_file.read_text())
        assert doc["database"]["device"] == "cpu"

    def test_run_flow_dry_run_skips_validation(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """dry_run=True: nothing is validated; the auto write is narrated."""
        installer = _make_installer(tmp_path, dry_run=True)

        with patch.object(installer, "detect_gpu", return_value=GpuType.METAL), \
             patch.object(installer, "_accelerator_validated") as mock_validate, \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False):
            result = installer.run(non_interactive=True)

        assert result == 0
        mock_validate.assert_not_called()
        assert "[DRY RUN] Would set [database] device = 'auto'." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# load_service / unload_service
# ---------------------------------------------------------------------------


class TestLoadUnloadService:
    def test_load_service_delegates_to_platform(self, tmp_path: Path) -> None:
        """load_service() must call get_rag_service().start(dry_run=False)."""
        installer = _make_installer(tmp_path, dry_run=False)
        svc = MagicMock()
        svc.start.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            rc = installer.load_service()

        assert rc == 0
        svc.start.assert_called_once_with(dry_run=False)

    def test_unload_service_delegates_to_platform(self, tmp_path: Path) -> None:
        """unload_service() must call get_rag_service().stop(dry_run=False)."""
        installer = _make_installer(tmp_path, dry_run=False)
        svc = MagicMock()
        svc.stop.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
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
        with urllib.error.HTTPError(
            url=None, code=404, msg="Not Found", hdrs=None, fp=None  # type: ignore[arg-type]
        ) as error, patch("urllib.request.urlopen", side_effect=error):
            assert installer._is_service_running() is False

    def test_is_service_running_returns_false_on_connection_refused(self, tmp_path: Path) -> None:
        """urlopen raises ConnectionRefusedError → _is_service_running returns False."""
        installer = _make_installer(tmp_path)
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            assert installer._is_service_running() is False


# ---------------------------------------------------------------------------
# Non-blocking install flow (P1-001 bug fix)
# ---------------------------------------------------------------------------


class TestRunNonBlocking:
    """Tests verifying that run() is non-blocking: service starts first, no asyncio.run bootstrap.

    State file gap note: TestServerInstallTrigger in test_server.py verifies that
    set_trigger('install') is called (the trigger field), NOT that individual collection
    status is written as IN_PROGRESS. Per-collection IN_PROGRESS status writing is not
    currently covered by unit tests — it is verified by the sync/indexing integration tests.
    Tests here focus solely on install.py not blocking in run().
    """

    def test_run_does_not_call_bootstrap_collections(self, tmp_path: Path) -> None:
        """run() must NOT call _bootstrap_collections — server handles sync on startup via asyncio.create_task."""
        installer = _make_installer(tmp_path)
        bootstrap_mock = AsyncMock()

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_bootstrap_collections", new=bootstrap_mock), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        bootstrap_mock.assert_not_called()

    def test_run_does_not_import_asyncio_for_blocking_call(self, tmp_path: Path) -> None:
        """asyncio must not be imported in install.py for blocking bootstrap — module is sync-only."""
        import importlib
        import importlib.util
        spec = importlib.util.find_spec("archon_search.install")
        assert spec is not None
        # Reload the module and inspect its globals — asyncio must not be present
        # (it was removed when we dropped the asyncio.run(_bootstrap_collections()) call)
        import archon_search.install as install_mod
        assert "asyncio" not in vars(install_mod), (
            "asyncio is still imported in archon_search/install.py — the blocking asyncio.run call may still exist"
        )

    def test_run_step_4_is_service_start_not_bootstrap(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """After the fix, [4/5] must describe starting the service, not bootstrapping collections."""
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True):
            result = installer.run(non_interactive=True)

        assert result == 0
        captured = capsys.readouterr()
        # [4/5] must now be about starting the service
        assert "[4/5]" in captured.out
        # The old "[4/5] Bootstrapping collections" text must be gone
        assert "Bootstrapping collections" not in captured.out

    def test_service_starts_without_waiting_for_indexing(self, tmp_path: Path) -> None:
        """run() exits with 0 as soon as service is ready — it does NOT block on indexing completion."""
        installer = _make_installer(tmp_path)
        # Simulate that _wait_for_service returns True immediately
        # If bootstrap was still blocking, this would hang; since it's removed the call succeeds fast.
        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True) as mock_wait:
            result = installer.run(non_interactive=True)

        assert result == 0
        # _wait_for_service must still be called (service readiness check)
        mock_wait.assert_called_once()

    def test_exact_call_order_service_before_wait_no_bootstrap(self, tmp_path: Path) -> None:
        """run() must call load_service before _wait_for_service, and _bootstrap_collections must never appear.

        The relative ordering of configure_device/create_data_dir/write_service_file is an
        implementation detail — only the load_service→_wait_for_service pair is a strict contract.
        """
        installer = _make_installer(tmp_path)
        call_order: list[str] = []

        def record(name: str):
            def _side_effect(*args: object, **kwargs: object) -> object:
                call_order.append(name)
                if name == "load_service":
                    return 0
                if name == "_wait_for_service":
                    return True
                return None
            return _side_effect

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device", side_effect=record("configure_device")), \
             patch.object(installer, "create_data_dir", side_effect=record("create_data_dir")), \
             patch.object(installer, "write_service_file", side_effect=record("write_service_file")), \
             patch.object(installer, "load_service", side_effect=record("load_service")), \
             patch.object(installer, "_bootstrap_collections", side_effect=record("_bootstrap_collections")), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", side_effect=record("_wait_for_service")):
            result = installer.run(non_interactive=True)

        assert result == 0

        # _bootstrap_collections must NEVER be called
        assert "_bootstrap_collections" not in call_order, (
            f"_bootstrap_collections was called — run() must not block on indexing. order={call_order}"
        )

        # Critical contract: load_service must come before _wait_for_service
        assert "load_service" in call_order, "load_service was never called"
        assert "_wait_for_service" in call_order, "_wait_for_service was never called"
        assert call_order.index("load_service") < call_order.index("_wait_for_service"), (
            f"load_service must precede _wait_for_service. order={call_order}"
        )

    def test_run_completes_quickly_without_blocking(self, tmp_path: Path) -> None:
        """run() completes in under 2 seconds when all subprocesses are mocked.

        Since all methods are mocked, run() should complete in microseconds. This catches
        any inline blocking code added directly to run() between the patched method calls —
        e.g., a sleep() or synchronous HTTP call inserted directly in the method body.
        It does NOT catch regressions to existing patched methods (those are covered by
        the ordering tests above).
        """
        installer = _make_installer(tmp_path)

        with patch.object(installer, "detect_gpu", return_value="none"), \
             patch.object(installer, "configure_device"), \
             patch.object(installer, "create_data_dir"), \
             patch.object(installer, "write_service_file"), \
             patch.object(installer, "load_service", return_value=0), \
             patch.object(installer, "_is_service_running", return_value=False), \
             patch.object(installer, "_wait_for_service", return_value=True), \
             patch.object(installer, "_bootstrap_collections"):
            start = time.monotonic()
            result = installer.run(non_interactive=True)
            elapsed = time.monotonic() - start

        assert result == 0
        assert elapsed < 2.0, (
            f"run() took {elapsed:.2f}s — a blocking call may have been reintroduced"
        )


# ---------------------------------------------------------------------------
# Console integration# ---------------------------------------------------------------------------


class TestConsoleIntegration:
    """Tests for output messages during run() and run_uninstall().

    The new archon_search.install.SearchInstaller uses print() instead of _console.
    These tests use capsys to capture stdout.
    """

    @staticmethod
    def _run_patches() -> dict:
        return {
            "detect_gpu": MagicMock(return_value="none"),
            "configure_device": MagicMock(),
            "create_data_dir": MagicMock(),
            "_bootstrap_collections": AsyncMock(),
            "write_service_file": MagicMock(),
            "load_service": MagicMock(return_value=0),
            "_wait_for_service": MagicMock(return_value=True),
            "_is_service_running": MagicMock(return_value=False),
        }

    def test_search_run_final_message_uses_console(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run() final success message printed to stdout contains 'installed and running'."""
        installer = _make_installer(tmp_path)

        patches = self._run_patches()
        with patch.object(installer, "detect_gpu", patches["detect_gpu"]), \
             patch.object(installer, "configure_device", patches["configure_device"]), \
             patch.object(installer, "create_data_dir", patches["create_data_dir"]), \
             patch.object(installer, "_bootstrap_collections", patches["_bootstrap_collections"]), \
             patch.object(installer, "write_service_file", patches["write_service_file"]), \
             patch.object(installer, "load_service", patches["load_service"]), \
             patch.object(installer, "_wait_for_service", patches["_wait_for_service"]), \
             patch.object(installer, "_is_service_running", patches["_is_service_running"]):
            result = installer.run(non_interactive=True)

        assert result == 0
        out = capsys.readouterr().out
        assert "installed and running" in out.lower(), (
            f"Expected 'installed and running' in stdout: {out}"
        )

    def test_search_uninstall_uses_console(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """run_uninstall() stdout must mention 'uninstalled'."""
        installer = _make_installer(tmp_path)

        svc = MagicMock()
        svc.stop.return_value = 0
        svc.unregister.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            result = installer.run_uninstall(delete_db=False)

        assert result == 0
        out = capsys.readouterr().out
        assert "uninstalled" in out.lower(), (
            f"Expected 'uninstalled' in stdout: {out}"
        )

    def test_search_run_error_path_uses_console_error(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Service start failure prints exit code info to stderr."""
        installer = _make_installer(tmp_path)

        patches = self._run_patches()
        patches["load_service"] = MagicMock(return_value=42)
        with patch.object(installer, "detect_gpu", patches["detect_gpu"]), \
             patch.object(installer, "configure_device", patches["configure_device"]), \
             patch.object(installer, "create_data_dir", patches["create_data_dir"]), \
             patch.object(installer, "_bootstrap_collections", patches["_bootstrap_collections"]), \
             patch.object(installer, "write_service_file", patches["write_service_file"]), \
             patch.object(installer, "load_service", patches["load_service"]), \
             patch.object(installer, "_is_service_running", patches["_is_service_running"]):
            result = installer.run(non_interactive=True)

        assert result == 42
        captured = capsys.readouterr()
        assert "exit code" in captured.err.lower() or "42" in captured.err, (
            f"Expected exit code info in stderr: {captured.err}"
        )

    def test_search_run_timeout_uses_console_warn(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Service readiness timeout is printed to stdout."""
        installer = _make_installer(tmp_path)

        patches = self._run_patches()
        patches["_wait_for_service"] = MagicMock(return_value=False)
        with patch.object(installer, "detect_gpu", patches["detect_gpu"]), \
             patch.object(installer, "configure_device", patches["configure_device"]), \
             patch.object(installer, "create_data_dir", patches["create_data_dir"]), \
             patch.object(installer, "_bootstrap_collections", patches["_bootstrap_collections"]), \
             patch.object(installer, "write_service_file", patches["write_service_file"]), \
             patch.object(installer, "load_service", patches["load_service"]), \
             patch.object(installer, "_wait_for_service", patches["_wait_for_service"]), \
             patch.object(installer, "_is_service_running", patches["_is_service_running"]):
            result = installer.run(non_interactive=True)

        assert result == 1
        out = capsys.readouterr().out
        assert "ready" in out.lower() or "timed out" in out.lower() or "timeout" in out.lower(), (
            f"Expected timeout/ready info in stdout: {out}"
        )


# ---------------------------------------------------------------------------
# Strategy-pattern structural guarantees (BaseInstaller ABC / InstallerProtocol)
# ---------------------------------------------------------------------------


class TestInstallerStructure:
    def test_base_installer_cannot_be_instantiated_directly(self) -> None:
        """BaseInstaller is an ABC with abstract destructive methods — cannot be built."""
        from archon_search.install import BaseInstaller

        with pytest.raises(TypeError):
            BaseInstaller()  # type: ignore[abstract]

    def test_base_installer_abstractmethods_are_exactly_the_destructive_ops(self) -> None:
        """Locks the abstract boundary: exactly these mutation methods are abstract.

        Every step in run()/run_uninstall() that touches the filesystem, packages,
        or the service is routed through one of these abstract methods (Real
        executes, Dry narrates). Adding a mutation here without marking it abstract
        would let it run unguarded in dry-run — this test fails if the set drifts.

        Note: this documents the routed mutation surface. It is NOT a full safety
        proof — a raw mutation typed inline into run() bypasses it. That case is
        caught by test_dry_run_backstop_touches_nothing_real, which asserts a full
        dry-run invokes no real seam anywhere.
        """
        from archon_search.install import BaseInstaller

        assert BaseInstaller.__abstractmethods__ == frozenset({
            "configure_device",
            "create_data_dir",
            "write_service_file",
            "load_service",
            "unload_service",
            "install_lock",
            "create_logs_dir",
            "download_fasttext_model",
            "prepare_db_path",
            "persist_fresh_config",
            "persist_reinstall_config",
            "record_wizard_values",
            "write_server_key",
            "preload_models",
            "register_and_start",
            "delete_database",
        })


class TestCreateInstallerFactory:
    def test_dry_run_true_returns_dry_run_installer(self, tmp_path: Path) -> None:
        from archon_search.install import (
            DryRunInstaller,
            InstallerProtocol,
            create_installer,
        )

        config_toml = tmp_path / "archon-search.toml"
        config_toml.write_text("")

        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            installer = create_installer(config_file=str(config_toml), dry_run=True)

        assert isinstance(installer, DryRunInstaller)
        assert isinstance(installer, InstallerProtocol)
        assert installer.config_file == str(config_toml)

    def test_dry_run_false_returns_real_installer(self, tmp_path: Path) -> None:
        from archon_search.install import (
            InstallerProtocol,
            RealInstaller,
            create_installer,
        )

        config_toml = tmp_path / "archon-search.toml"
        config_toml.write_text("")

        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            installer = create_installer(config_file=str(config_toml), dry_run=False)

        assert isinstance(installer, RealInstaller)
        assert isinstance(installer, InstallerProtocol)
        assert installer.config_file == str(config_toml)


class TestDryRunGuardAgainstRealServiceCalls:
    """S37 regression guard: dry-run must never reach the real service path."""

    def _dry_run_installer(self, tmp_path: Path) -> object:
        from archon_search.install import create_installer

        config_toml = tmp_path / "archon-search.toml"
        config_toml.write_text("")
        with patch("archon_search.install.installer.load_config", return_value=MagicMock()):
            return create_installer(config_file=str(config_toml), dry_run=True)

    def test_write_service_file_calls_service_with_dry_run_true(self, tmp_path: Path) -> None:
        installer = self._dry_run_installer(tmp_path)
        svc = MagicMock()

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            installer.write_service_file()

        svc.replace_registration.assert_called_once_with(
            dry_run=True, config_path=str(tmp_path / "archon-search.toml")
        )

    def test_load_service_calls_service_with_dry_run_true(self, tmp_path: Path) -> None:
        installer = self._dry_run_installer(tmp_path)
        svc = MagicMock()
        svc.start.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            installer.load_service()

        svc.start.assert_called_once_with(dry_run=True)

    def test_unload_service_calls_service_with_dry_run_true(self, tmp_path: Path) -> None:
        installer = self._dry_run_installer(tmp_path)
        svc = MagicMock()
        svc.stop.return_value = 0

        with patch("archon_search.install.installer.get_search_service", return_value=svc):
            installer.unload_service()

        svc.stop.assert_called_once_with(dry_run=True)

"""Tests for SearchInstaller.run() — Task 1.3 (C15 new keyword params)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import SearchInstaller, WizardFeatures
from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")

# ---------------------------------------------------------------------------
# Shared helper — patch everything except what the test is verifying
# ---------------------------------------------------------------------------

_COMMON_PATCHES = {
    "archon_search.install._prewarm_models": MagicMock(),
    "archon_search.install._check_disk_space": MagicMock(),
    "archon_search.install._legacy_service_path": MagicMock,  # overridden per test
    "archon_search.install._remove_legacy_service": MagicMock(),
}


def _base_run_patches(tmp_path: Path, features_override: WizardFeatures | None = None):
    """Return a dict of module-level patches for a standard non-interactive run."""
    fake_legacy = tmp_path / "fake.plist"
    features = features_override or WizardFeatures()
    return {
        "archon_search.install._legacy_service_path": MagicMock(return_value=fake_legacy),
        "archon_search.install._remove_legacy_service": MagicMock(),
        "archon_search.install._prewarm_models": MagicMock(),
        "archon_search.install._check_disk_space": MagicMock(),
        "archon_search.install._prompt_multilingual": MagicMock(return_value=False),
        "archon_search.install._prompt_optional_features": MagicMock(return_value=features),
        "archon_search.install._prompt_gpu_confirm": MagicMock(return_value=True),
        # Mock the subprocess-shelling provider install so enable_hyde/enable_rag_fusion
        # runs never shell out to a real `pip install archon-search[hyde]`.
        "archon_search.install._install_query_expansion_extras": MagicMock(return_value=[]),
    }


def _method_patches():
    return {
        "detect_gpu": MagicMock(return_value=GpuType.NONE),
        "validate_providers": MagicMock(return_value=False),
        "configure_providers": MagicMock(),
        "write_service_file": MagicMock(),
        "load_service": MagicMock(return_value=0),
        "_wait_for_service": MagicMock(return_value=True),
        "_is_service_running": MagicMock(return_value=False),
    }


def _run_installer(
    tmp_path: Path,
    run_kwargs: dict,
    features_override: WizardFeatures | None = None,
    extra_module_patches: dict | None = None,
    extra_method_patches: dict | None = None,
):
    """Run SearchInstaller.run() with all infrastructure patched out."""
    config_path = tmp_path / "archon-search.toml"
    module_patches = _base_run_patches(tmp_path, features_override)
    module_patches["archon_search.install.get_default_config_path"] = MagicMock(return_value=config_path)
    if extra_module_patches:
        module_patches.update(extra_module_patches)

    method_patches = _method_patches()
    if extra_method_patches:
        method_patches.update(extra_method_patches)

    with patch.multiple(
        "archon_search.install",
        **{k.replace("archon_search.install.", ""): v for k, v in module_patches.items()},
    ):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
                **run_kwargs,
            )
    return rc, config_path


# ---------------------------------------------------------------------------
# Tests: Tier 1 flags passed through to WizardFeatures
# ---------------------------------------------------------------------------


def test_run_passes_host_to_features(tmp_path: Path) -> None:
    """run(host='0.0.0.0') → features.host == '0.0.0.0' written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"host": "0.0.0.0"})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["server"]["host"] == "0.0.0.0"


def test_run_passes_port_to_features(tmp_path: Path) -> None:
    """run(port=9000) → features.port == 9000 written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"port": 9000})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["server"]["port"] == 9000


def test_run_passes_top_k_to_features(tmp_path: Path) -> None:
    """run(top_k=20) → top_k_return=20, top_k_retrieve=60 written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"top_k": 20})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["top_k_return"] == 20
    assert doc["database"]["top_k_retrieve"] == 60


def test_run_passes_log_level_to_features(tmp_path: Path) -> None:
    """run(log_level='DEBUG') → [logging].level = 'DEBUG' written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"log_level": "DEBUG"})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["logging"]["level"] == "DEBUG"


def test_run_passes_enable_hyde_to_features(tmp_path: Path) -> None:
    """run(enable_hyde=True) → [hyde].enabled = true written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"enable_hyde": True})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True


def test_run_passes_enable_rag_fusion_to_features(tmp_path: Path) -> None:
    """run(enable_rag_fusion=True) → [rag_fusion].enabled = true written to TOML."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {"enable_rag_fusion": True})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["rag_fusion"]["enabled"] is True


def test_run_mixed_provider_partial_failure_reverts_only_failed_section(tmp_path: Path) -> None:
    """run(): HyDE=anthropic ok + RAG Fusion=ollama fails → only rag_fusion reverted on disk."""
    import tomlkit

    features = WizardFeatures(
        enable_hyde=True, hyde_provider="anthropic",
        enable_rag_fusion=True, rag_fusion_provider="ollama", rag_fusion_model="qwen2.5:3b",
    )
    rc, config_path = _run_installer(
        tmp_path,
        {},
        features_override=features,
        extra_module_patches={
            "archon_search.install._install_query_expansion_extras": MagicMock(return_value=["rag_fusion"]),
        },
    )
    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["hyde"]["enabled"] is True, "hyde installed fine — must stay enabled"
    assert doc["rag_fusion"]["enabled"] is False, "rag_fusion package failed — must be reverted"
    assert "provider" not in doc["rag_fusion"], "reverted section must have provider stripped"


def test_run_passes_telemetry_retention_days_to_features(tmp_path: Path) -> None:
    """run(telemetry_retention_days=7) with telemetry enabled features → [telemetry].retention_days = 7."""
    import tomlkit

    # Use a features override that already has enable_telemetry=True so the guard fires.
    # telemetry_retention_days is a C15 Tier 1 flag overlaid AFTER prompt returns.
    features = WizardFeatures(enable_telemetry=True)

    rc, config_path = _run_installer(
        tmp_path,
        {"telemetry_retention_days": 7},
        features_override=features,
    )

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["telemetry"]["retention_days"] == 7


def test_run_none_flags_do_not_write_toml(tmp_path: Path) -> None:
    """run() without host/port flags → server.host stays at template default (127.0.0.1)."""
    import tomlkit

    rc, config_path = _run_installer(tmp_path, {})

    assert rc == 0
    assert config_path.exists(), "Config file should be written on fresh install"
    doc = tomlkit.parse(config_path.read_text())
    # Default TOML has [server] section but host should be the template default, not custom
    assert doc.get("server", {}).get("host") == "127.0.0.1", (
        "host should be template default '127.0.0.1' when --host not passed"
    )
    # No custom port (template default is 8765)
    assert doc.get("server", {}).get("port") == 8765


# ---------------------------------------------------------------------------
# Tests: server_key write logic
# ---------------------------------------------------------------------------

_VALID_SERVER_KEY = "a" * 32  # 32-char lowercase hex


def _run_with_server_key(
    tmp_path: Path,
    server_key: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, Path, list]:
    """Run installer with server_key, patching os.chmod and atomic_write_bytes for the key file."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"
    atomic_calls: list = []

    real_atomic_write = None
    from archon_search._durable_io import atomic_write_bytes as _real_aw

    def capturing_atomic_write(path, data, **kwargs):
        atomic_calls.append((path, data))
        if path != key_file:
            _real_aw(path, data, **kwargs)

    module_patches = _base_run_patches(tmp_path)
    module_patches["archon_search.install.get_default_config_path"] = MagicMock(return_value=config_path)

    method_patches = _method_patches()

    env_context = patch.dict(
        os.environ,
        {"ARCHON_SEARCH_DATA_DIR": str(tmp_path), **(extra_env or {})},
    )

    with env_context:
        with patch.multiple(
            "archon_search.install",
            **{k.replace("archon_search.install.", ""): v for k, v in module_patches.items()},
        ):
            with patch.multiple(SearchInstaller, **method_patches):
                with patch("archon_search.install.atomic_write_bytes", side_effect=capturing_atomic_write):
                    with patch("archon_search.install.os.chmod"):
                        installer = SearchInstaller(config_file=str(config_path))
                        rc = installer.run(
                            non_interactive=True,
                            profile="minimal",
                            skip_preload=True,
                            server_key=server_key,
                        )
    return rc, config_path, atomic_calls


def test_run_server_key_writes_key_file(tmp_path: Path) -> None:
    """run(server_key=<32-hex>) → KEY_FILE written with correct content."""
    key_file = tmp_path / ".search.env"
    rc, config_path, atomic_calls = _run_with_server_key(tmp_path, _VALID_SERVER_KEY)

    assert rc == 0
    # Find the call that targeted key_file
    key_calls = [(path, data) for path, data in atomic_calls if path == key_file]
    assert len(key_calls) == 1, f"Expected one write to key_file, got: {key_calls}"
    assert key_calls[0][1] == f"ARCHON_SEARCH_API_KEY={_VALID_SERVER_KEY}\n".encode()


def test_run_server_key_sets_mode_600(tmp_path: Path) -> None:
    """run(server_key=...) → os.chmod(KEY_FILE, 0o600) called."""
    key_file = tmp_path / ".search.env"
    chmod_calls: list = []

    config_path = tmp_path / "archon-search.toml"
    module_patches = _base_run_patches(tmp_path)
    module_patches["archon_search.install.get_default_config_path"] = MagicMock(return_value=config_path)

    method_patches = _method_patches()

    # Track all os.chmod calls and record them
    import os as _os
    real_os_chmod = _os.chmod

    def capturing_chmod(path, mode, **kwargs):
        chmod_calls.append((path, mode))
        # Don't actually chmod in tests (key_file doesn't need real mode change)

    with patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}):
        with patch.multiple(
            "archon_search.install",
            **{k.replace("archon_search.install.", ""): v for k, v in module_patches.items()},
        ):
            with patch.multiple(SearchInstaller, **method_patches):
                with patch("archon_search.install.os.chmod", side_effect=capturing_chmod):
                    installer = SearchInstaller(config_file=str(config_path))
                    rc = installer.run(
                        non_interactive=True,
                        profile="minimal",
                        skip_preload=True,
                        server_key=_VALID_SERVER_KEY,
                    )

    assert rc == 0
    # Find the chmod call for the key file (may be other chmod calls e.g. from shutil)
    key_chmod_calls = [(path, mode) for path, mode in chmod_calls if Path(path) == key_file]
    assert len(key_chmod_calls) == 1, f"Expected one chmod for key_file, got: {chmod_calls}"
    assert key_chmod_calls[0] == (key_file, 0o600)


def test_run_server_key_prints_history_warning(tmp_path: Path, capsys) -> None:
    """run(server_key=...) → shell history warning printed to stdout."""
    rc, config_path, _ = _run_with_server_key(tmp_path, _VALID_SERVER_KEY)

    assert rc == 0
    captured = capsys.readouterr()
    assert "shell history" in captured.out


def test_run_server_key_prints_restart_note(tmp_path: Path, capsys) -> None:
    """run(server_key=...) → restart note printed."""
    rc, config_path, _ = _run_with_server_key(tmp_path, _VALID_SERVER_KEY)

    assert rc == 0
    captured = capsys.readouterr()
    assert "archon-search restart" in captured.out


def test_run_server_key_with_env_var_prints_priority_warning(tmp_path: Path, capsys) -> None:
    """run(server_key=...) with ARCHON_SEARCH_API_KEY env set → priority warning printed."""
    rc, config_path, _ = _run_with_server_key(
        tmp_path,
        _VALID_SERVER_KEY,
        extra_env={"ARCHON_SEARCH_API_KEY": "b" * 64},
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "ARCHON_SEARCH_API_KEY" in captured.out
    assert "priority" in captured.out.lower() or "takes priority" in captured.out or "env var" in captured.out


def test_run_no_server_key_does_not_write_key_file(tmp_path: Path) -> None:
    """run() without server_key → key file not written."""
    key_file = tmp_path / ".search.env"

    # Run without server_key and verify key_file is not created
    rc, config_path = _run_installer(tmp_path, {})

    assert rc == 0
    assert not key_file.exists(), "Key file should not be created when server_key not passed"


def test_run_dry_run_server_key_prints_message(tmp_path: Path, capsys) -> None:
    """run(dry_run=True, server_key=...) → [dry-run] message printed, key file not written."""
    config_path = tmp_path / "archon-search.toml"
    key_file = tmp_path / ".search.env"

    module_patches = _base_run_patches(tmp_path)
    module_patches["archon_search.install.get_default_config_path"] = MagicMock(return_value=config_path)

    method_patches = _method_patches()

    with patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}):
        with patch.multiple(
            "archon_search.install",
            **{k.replace("archon_search.install.", ""): v for k, v in module_patches.items()},
        ):
            with patch.multiple(SearchInstaller, **method_patches):
                with patch("archon_search.install.atomic_write_bytes") as mock_write:
                    installer = SearchInstaller(config_file=str(config_path), dry_run=True)
                    rc = installer.run(
                        non_interactive=True,
                        profile="minimal",
                        skip_preload=True,
                        server_key=_VALID_SERVER_KEY,
                    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "[dry-run] Would write server key to" in captured.out
    assert str(key_file) in captured.out
    # key file must not be written in dry-run mode
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: db_path special handling
# ---------------------------------------------------------------------------


def test_run_db_path_creates_directory(tmp_path: Path) -> None:
    """run(db_path=...) → directory created via expanduser().mkdir()."""
    db_dir = tmp_path / "custom_db"
    assert not db_dir.exists()

    rc, config_path = _run_installer(tmp_path, {"db_path": str(db_dir)})

    assert rc == 0
    assert db_dir.exists()


def test_run_db_path_not_writable_exits(tmp_path: Path) -> None:
    """run(db_path=...) with non-writable dir → SystemExit raised (rc=1)."""
    db_dir = tmp_path / "custom_db"
    db_dir.mkdir()

    with patch("archon_search.install.os.access", return_value=False):
        rc, config_path = _run_installer(tmp_path, {"db_path": str(db_dir)})

    assert rc == 1


def test_run_db_path_writes_to_toml(tmp_path: Path) -> None:
    """run(db_path='~/custom') → TOML has database.db_path = '~/custom' (tilde preserved)."""
    import tomlkit
    db_dir = tmp_path / "custom_db"

    rc, config_path = _run_installer(tmp_path, {"db_path": str(db_dir)})

    assert rc == 0
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["db_path"] == str(db_dir)


def test_run_db_path_migration_note_when_different(tmp_path: Path, capsys) -> None:
    """Existing config has a different db_path → migration note printed."""
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    db_dir = tmp_path / "new_db"

    module_patches = _base_run_patches(tmp_path)
    module_patches["archon_search.install.get_default_config_path"] = MagicMock(return_value=config_path)

    method_patches = _method_patches()

    with patch.multiple(
        "archon_search.install",
        **{k.replace("archon_search.install.", ""): v for k, v in module_patches.items()},
    ):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
                db_path=str(db_dir),
            )

    assert rc == 0
    captured = capsys.readouterr()
    # Migration note should mention the path change
    assert "db_path" in captured.out or "database" in captured.out.lower() or "migrat" in captured.out.lower()


def test_run_summary_shows_passed_host_and_port(tmp_path: Path, capsys) -> None:
    """run(host='10.0.0.1', port=9999) → summary screen contains 'http://10.0.0.1:9999'."""
    rc, _ = _run_installer(tmp_path, {"host": "10.0.0.1", "port": 9999})

    assert rc == 0
    captured = capsys.readouterr()
    assert "http://10.0.0.1:9999" in captured.out, (
        f"Expected 'http://10.0.0.1:9999' in summary output, got:\n{captured.out}"
    )

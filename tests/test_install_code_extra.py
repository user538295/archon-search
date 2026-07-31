"""Tests for _install_code_extra() — Task C8-2.3; _install_extra() — Task C15-4.1."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, call, patch

import pytest

from archon_search.install import (
    InstallError,
    _install_code_extra,
    _install_extra,
    _install_graph_extra,
    _install_multilingual_extra,
)


class TestInstallCodeExtra:
    """Unit tests for _install_code_extra()."""

    def test_dry_run_no_subprocess(self, capsys):
        """dry_run=True must not call any subprocess."""
        with patch("subprocess.run") as mock_run:
            _install_code_extra(dry_run=True)
            mock_run.assert_not_called()

    def test_dry_run_prints_message(self, capsys):
        """dry_run=True should print what would be run."""
        with patch("subprocess.run"):
            _install_code_extra(dry_run=True)
        captured = capsys.readouterr()
        # Should indicate dry-run, not silent
        assert "dry" in captured.out.lower() or "would" in captured.out.lower() or "code" in captured.out.lower()

    def test_uv_success(self, capsys):
        """Successful uv invocation should not call pip fallback."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_code_extra()
            # uv should have been called exactly once
            assert mock_run.call_count == 1
            first_call_args = mock_run.call_args_list[0][0][0]
            assert first_call_args[0] == "uv"

    def test_uv_success_prints_messages(self, capsys):
        """Successful install should print start and success messages."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_code_extra()
        captured = capsys.readouterr()
        assert "Installing" in captured.out or "code" in captured.out.lower()

    def test_uv_not_found_falls_back_to_pip(self, capsys):
        """FileNotFoundError from uv (not on PATH) should trigger pip fallback."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("uv not found"),
                MagicMock(returncode=0),  # pip succeeds
            ]
            _install_code_extra()
            assert mock_run.call_count == 2
            pip_call_args = mock_run.call_args_list[1][0][0]
            assert pip_call_args[0] == sys.executable
            assert "-m" in pip_call_args
            assert "pip" in pip_call_args

    def test_uv_failure_falls_back_to_pip(self):
        """CalledProcessError from uv should trigger pip fallback."""
        uv_error = subprocess.CalledProcessError(1, "uv", stderr=b"uv failed")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                uv_error,
                MagicMock(returncode=0),  # pip succeeds
            ]
            _install_code_extra()
            assert mock_run.call_count == 2
            pip_call_args = mock_run.call_args_list[1][0][0]
            assert pip_call_args[0] == sys.executable

    def test_pip_failure_raises_install_error(self):
        """Both uv and pip failing should raise InstallError with stderr in message."""
        uv_error = subprocess.CalledProcessError(1, "uv", stderr=b"uv error")
        pip_error = subprocess.CalledProcessError(1, "pip", stderr=b"pip error detail")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [uv_error, pip_error]
            with pytest.raises(InstallError, match="pip error detail"):
                _install_code_extra()

    def test_pip_success_after_uv_failure(self, capsys):
        """uv CalledProcessError then pip success → no exception, success message printed."""
        uv_error = subprocess.CalledProcessError(1, "uv", stderr=b"uv failed")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [uv_error, MagicMock(returncode=0)]
            _install_code_extra()  # must not raise
        captured = capsys.readouterr()
        assert "installed" in captured.out.lower() or "success" in captured.out.lower() or "code" in captured.out.lower()

    def test_uv_called_with_correct_args(self):
        """uv call must use --python sys.executable and target archon-search[code]."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_code_extra()
            call_args = mock_run.call_args_list[0][0][0]
            assert "uv" in call_args
            assert "pip" in call_args
            assert "install" in call_args
            assert "--python" in call_args
            assert sys.executable in call_args
            assert "archon-search[code]" in call_args

    def test_pip_called_with_correct_args(self):
        """pip fallback call must target archon-search[code]."""
        uv_error = FileNotFoundError("uv not found")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [uv_error, MagicMock(returncode=0)]
            _install_code_extra()
            pip_call_args = mock_run.call_args_list[1][0][0]
            assert "archon-search[code]" in pip_call_args
            assert "install" in pip_call_args


class TestInstallExtra:
    """Unit tests for _install_extra() — Task C15-4.1."""

    def test_install_extra_dry_run_echoes_package(self, capsys):
        """dry_run=True must not call subprocess but print a message containing the package name."""
        with patch("subprocess.run") as mock_run:
            _install_extra("my-package[extra]", "my label", dry_run=True)
            mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "my-package[extra]" in captured.out

    def test_install_extra_calls_uv_pip_install(self):
        """Successful uv call should invoke uv pip install with the given package."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_extra("some-pkg", "some label")
            assert mock_run.call_count == 1
            cmd = mock_run.call_args_list[0][0][0]
            assert cmd[0] == "uv"
            assert "pip" in cmd
            assert "install" in cmd
            assert "some-pkg" in cmd

    def test_install_extra_falls_back_to_pip_when_uv_absent(self):
        """FileNotFoundError from uv must trigger pip fallback with correct package."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("uv not on PATH"),
                MagicMock(returncode=0),
            ]
            _install_extra("my-pkg", "my label")
            assert mock_run.call_count == 2
            pip_cmd = mock_run.call_args_list[1][0][0]
            assert pip_cmd[0] == sys.executable
            assert "-m" in pip_cmd
            assert "pip" in pip_cmd
            assert "my-pkg" in pip_cmd

    def test_install_extra_raises_install_error_on_pip_failure(self):
        """Both uv and pip failing must raise InstallError containing the package name."""
        uv_err = subprocess.CalledProcessError(1, "uv", stderr=b"uv fail")
        pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"pip fail for my-pkg")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [uv_err, pip_err]
            with pytest.raises(InstallError, match="my-pkg"):
                _install_extra("my-pkg", "my label")

    def test_install_code_extra_delegates_to_install_extra(self):
        """_install_code_extra() must delegate to _install_extra with the code package and label."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_code_extra(dry_run=False)
            mock_extra.assert_called_once_with("archon-search[code]", "code enrichment", False)

    def test_install_code_extra_delegates_dry_run_to_install_extra(self):
        """_install_code_extra(dry_run=True) must delegate dry_run=True to _install_extra."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_code_extra(dry_run=True)
            mock_extra.assert_called_once_with("archon-search[code]", "code enrichment", True)


class TestInstallGraphExtra:
    """Unit tests for _install_graph_extra() — spaCy download logic."""

    def test_install_graph_extra_spacy_download_called(self):
        """On success: the spaCy model installs via ``uv pip install en-core-web-sm``.

        The old ``python -m spacy download`` route assumed a virtual environment
        and failed in a uv-tool install context — assert we no longer use it.
        """
        with patch("archon_search.install.extras._install_extra"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_graph_extra(dry_run=False)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd == ["uv", "pip", "install", "--python", sys.executable, "en-core-web-sm"]
            # Must NOT use the venv-dependent `python -m spacy download` route.
            assert "download" not in cmd
            assert "en_core_web_sm" not in cmd
            assert mock_run.call_args[1].get("check") is True
            assert mock_run.call_args[1].get("capture_output") is True

    def test_install_graph_extra_spacy_download_failure_is_nonfatal(self, capsys):
        """CalledProcessError from spaCy subprocess must not raise; a warning is printed to stderr."""
        spacy_error = subprocess.CalledProcessError(1, "spacy", stderr=b"model not found")
        with patch("archon_search.install.extras._install_extra"), \
             patch("subprocess.run", side_effect=spacy_error):
            _install_graph_extra(dry_run=False)  # must not raise
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "spacy" in captured.err.lower()

    def test_install_graph_extra_dry_run_no_subprocess(self):
        """dry_run=True must not call subprocess.run at all."""
        with patch("archon_search.install.extras._install_extra"), \
             patch("subprocess.run") as mock_run:
            _install_graph_extra(dry_run=True)
            mock_run.assert_not_called()

    def test_install_graph_extra_dry_run_prints_message(self, capsys):
        """dry_run=True should print a message indicating what would be run."""
        with patch("archon_search.install.extras._install_extra"), \
             patch("subprocess.run"):
            _install_graph_extra(dry_run=True)
        captured = capsys.readouterr()
        assert "spacy" in captured.out.lower() or "dry" in captured.out.lower()

    def test_install_graph_extra_partial_failure_extras_succeed_spacy_fails(self):
        """When _install_extra succeeds but spaCy download fails, InstallError is NOT raised."""
        spacy_error = subprocess.CalledProcessError(1, "spacy", stderr=b"download failed")
        with patch("archon_search.install.extras._install_extra"), \
             patch("subprocess.run", side_effect=spacy_error):
            # Must not raise InstallError — caller's except InstallError block must NOT trigger
            try:
                _install_graph_extra(dry_run=False)
            except InstallError:
                pytest.fail("_install_graph_extra raised InstallError on spaCy download failure")


class TestInstallMultilingualExtra:
    """Unit tests for _install_multilingual_extra() — mirrors _install_code_extra."""

    def test_delegates_to_install_extra(self):
        """_install_multilingual_extra() must delegate to _install_extra with the multilingual package."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_multilingual_extra(dry_run=False)
            mock_extra.assert_called_once_with(
                "archon-search[multilingual]", "multilingual language detection", False
            )

    def test_delegates_dry_run(self):
        """_install_multilingual_extra(dry_run=True) must delegate dry_run=True to _install_extra."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_multilingual_extra(dry_run=True)
            mock_extra.assert_called_once_with(
                "archon-search[multilingual]", "multilingual language detection", True
            )

    def test_dry_run_no_subprocess(self):
        """dry_run=True must not call any subprocess."""
        with patch("subprocess.run") as mock_run:
            _install_multilingual_extra(dry_run=True)
            mock_run.assert_not_called()

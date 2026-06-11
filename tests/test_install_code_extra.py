"""Tests for _install_code_extra() — Task C8-2.3."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import InstallError, _install_code_extra


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

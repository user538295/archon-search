"""Tests for _install_code_extra() — Task C8-2.3; _install_extra() — Task C15-4.1."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    InstallError,
    _install_code_extra,
    _install_extra,
    _install_graph_extra,
    _install_multilingual_extra,
)


@pytest.mark.usefixtures("pip_prefix")
class TestInstallCodeExtra:
    """Unit tests for _install_code_extra() outside a uv tool env."""

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
            _install_extra("graph", "my label", dry_run=True)
            mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "[dry-run] Would install archon-search[graph]" in captured.out

    def test_install_extra_calls_uv_pip_install(self, pip_prefix):
        """Outside a uv tool env, uv pip install runs with the extra's package."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _install_extra("hyde", "some label")
            assert mock_run.call_count == 1
            cmd = mock_run.call_args_list[0][0][0]
            assert cmd == ["uv", "pip", "install", "--python", sys.executable, "archon-search[hyde]"]

    def test_install_extra_falls_back_to_pip_when_uv_absent(self, pip_prefix):
        """FileNotFoundError from uv must trigger pip fallback with correct package."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("uv not on PATH"),
                MagicMock(returncode=0),
            ]
            _install_extra("ollama", "my label")
            assert mock_run.call_count == 2
            pip_cmd = mock_run.call_args_list[1][0][0]
            assert pip_cmd == [sys.executable, "-m", "pip", "install", "archon-search[ollama]"]

    def test_install_extra_raises_install_error_on_pip_failure(self, pip_prefix):
        """Both uv and pip failing must raise InstallError containing the package name."""
        uv_err = subprocess.CalledProcessError(1, "uv", stderr=b"uv fail")
        pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"pip fail detail")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [uv_err, pip_err]
            with pytest.raises(InstallError, match=r"archon-search\[ollama\]: pip fail detail"):
                _install_extra("ollama", "my label")

    def test_install_code_extra_delegates_to_install_extra(self):
        """_install_code_extra() must delegate to _install_extra with the code package and label."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_code_extra(dry_run=False)
            mock_extra.assert_called_once_with("code", "code enrichment", False)

    def test_install_code_extra_delegates_dry_run_to_install_extra(self):
        """_install_code_extra(dry_run=True) must delegate dry_run=True to _install_extra."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_code_extra(dry_run=True)
            mock_extra.assert_called_once_with("code", "code enrichment", True)


class TestInstallGraphExtra:
    """Unit tests for _install_graph_extra() — thin wrapper over _install_extra.

    The gliner NER checkpoint is fetched lazily by ``GLiNER.from_pretrained`` at
    first ingest, so the wizard only installs the pip extra (BE-15).
    """

    def test_install_graph_extra_delegates_to_install_extra(self):
        """_install_graph_extra() delegates to _install_extra with the graph package."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_graph_extra(dry_run=False)
            mock_extra.assert_called_once_with("graph", "graph enrichment", False)

    def test_install_graph_extra_delegates_dry_run_to_install_extra(self):
        """_install_graph_extra(dry_run=True) delegates dry_run=True to _install_extra."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_graph_extra(dry_run=True)
            mock_extra.assert_called_once_with("graph", "graph enrichment", True)


class TestInstallMultilingualExtra:
    """Unit tests for _install_multilingual_extra() — mirrors _install_code_extra."""

    def test_delegates_to_install_extra(self):
        """_install_multilingual_extra() must delegate to _install_extra with the multilingual package."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_multilingual_extra(dry_run=False)
            mock_extra.assert_called_once_with("multilingual", "multilingual language detection", False)

    def test_delegates_dry_run(self):
        """_install_multilingual_extra(dry_run=True) must delegate dry_run=True to _install_extra."""
        with patch("archon_search.install.extras._install_extra") as mock_extra:
            _install_multilingual_extra(dry_run=True)
            mock_extra.assert_called_once_with("multilingual", "multilingual language detection", True)

    def test_dry_run_no_subprocess(self):
        """dry_run=True must not call any subprocess."""
        with patch("subprocess.run") as mock_run:
            _install_multilingual_extra(dry_run=True)
            mock_run.assert_not_called()


class TestInstallExtraInUvToolEnv:
    """In a ``uv tool install archon-search`` env the extra must land in uv's receipt.

    ``uv tool upgrade`` re-syncs the env to ``uv-receipt.toml``, so the extra is
    installed by re-running ``uv tool install`` with the receipt's own
    requirements plus the extra — never by mutating the env with ``uv pip``.
    """

    def _set_receipt(self, prefix, text):
        (prefix / "uv-receipt.toml").write_text(text)

    def test_runs_uv_tool_install_with_the_receipt_and_derived_env(self, uv_tool_prefix, monkeypatch):
        bin_dir = uv_tool_prefix.parent.parent / "bin"
        self._set_receipt(
            uv_tool_prefix,
            '[tool]\nrequirements = [{ name = "archon-search", extras = ["code"] }, { name = "rich" }]\n'
            f'entrypoints = [{{ name = "archon-search", install-path = "{bin_dir}/archon-search", '
            'from = "archon-search" }]\n'
            '[tool.options]\nfind-links = ["file:///links"]\n',
        )
        monkeypatch.setenv("UV_PYTHON", "3.13")
        with patch("subprocess.run") as mock_run:
            _install_graph_extra()
        assert mock_run.call_count == 1, "no `uv pip` fallback in a uv tool env"
        args, kwargs = mock_run.call_args
        assert args[0] == [
            "uv", "tool", "install", "archon-search[code,graph]", "--with", "rich", "--find-links", "file:///links",
        ]
        assert kwargs["check"] is True
        env = kwargs["env"]
        assert env["UV_TOOL_DIR"] == str(uv_tool_prefix.parent)
        assert env["UV_TOOL_BIN_DIR"] == str(bin_dir)
        assert "UV_PYTHON" not in env

    def test_keeps_the_installed_torch_build(self, uv_tool_prefix, monkeypatch):
        """A re-resolving install without --torch-backend would swap a +cu126/+cpu torch for PyPI's build."""
        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata, "version", lambda name: "2.14.1+cu126" if name == "torch" else "0"
        )
        with patch("subprocess.run") as mock_run:
            _install_extra("multilingual", "multilingual language detection")
        assert mock_run.call_args[0][0] == [
            "uv", "tool", "install", "archon-search[multilingual]", "--torch-backend", "cu126",
        ]

    def test_uv_failure_raises_install_error_without_pip_fallback(self, uv_tool_prefix):
        error = subprocess.CalledProcessError(1, "uv", stderr=b"No solution found")
        with (
            patch("subprocess.run", side_effect=error) as mock_run,
            pytest.raises(InstallError, match=r"Failed to install archon-search\[graph\]: No solution found"),
        ):
            _install_graph_extra()
        assert mock_run.call_count == 1

    def test_missing_uv_raises_install_error(self, uv_tool_prefix):
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("uv")) as mock_run,
            pytest.raises(InstallError, match=r"Failed to install archon-search\[code\]"),
        ):
            _install_code_extra()
        assert mock_run.call_count == 1

    @pytest.mark.parametrize(
        ("receipt", "reason"),
        [
            ('[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n', "python"),
            ("not [valid toml", "uv-receipt.toml"),
        ],
        ids=["unsupported", "unreadable"],
    )
    def test_unreproducible_receipt_warns_and_uses_uv_pip(self, uv_tool_prefix, capsys, receipt, reason):
        self._set_receipt(uv_tool_prefix, receipt)
        with patch("subprocess.run") as mock_run:
            _install_graph_extra()
        assert mock_run.call_count == 1, "the fallback is a single `uv pip` call, no pip retry"
        args, kwargs = mock_run.call_args
        assert args[0] == ["uv", "pip", "install", "--python", sys.executable, "archon-search[graph]"]
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        warning = capsys.readouterr().err
        assert reason in warning
        assert "uv tool upgrade" in warning
        assert "uv tool install 'archon-search[graph]'" in warning
        assert "--torch-backend" in warning

    def test_unsupported_receipt_fallback_keeps_the_receipt_index_options(self, uv_tool_prefix):
        """uv pip must resolve from the same indexes the tool was installed with."""
        self._set_receipt(
            uv_tool_prefix,
            '[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n'
            '[tool.options]\nindex-url = "https://idx/simple"\nfind-links = ["file:///links"]\n',
        )
        with patch("subprocess.run") as mock_run:
            _install_graph_extra()
        assert mock_run.call_args_list[0][0][0] == [
            "uv", "pip", "install", "--python", sys.executable, "archon-search[graph]",
            "--index-url", "https://idx/simple", "--find-links", "file:///links",
        ]

    def test_unsupported_receipt_with_unrenderable_option_falls_back_without_options(self, uv_tool_prefix):
        self._set_receipt(
            uv_tool_prefix,
            '[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n'
            '[tool.options]\nresolution = "lowest"\n',
        )
        with patch("subprocess.run") as mock_run:
            _install_graph_extra()
        assert mock_run.call_args[0][0] == [
            "uv", "pip", "install", "--python", sys.executable, "archon-search[graph]",
        ]

    def test_failing_fallback_raises_uv_stderr_without_pip(self, uv_tool_prefix):
        """uv tool envs ship no pip, so pip's "No module named pip" must not mask uv's error."""
        self._set_receipt(uv_tool_prefix, '[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n')
        error = subprocess.CalledProcessError(1, "uv", stderr=b"No solution found")
        with (
            patch("subprocess.run", side_effect=error) as mock_run,
            pytest.raises(InstallError, match=r"Failed to install archon-search\[graph\]: No solution found"),
        ):
            _install_graph_extra()
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0][:3] == ["uv", "pip", "install"]

    def test_fallback_without_uv_raises_install_error(self, uv_tool_prefix):
        self._set_receipt(uv_tool_prefix, '[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n')
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("uv")) as mock_run,
            pytest.raises(InstallError, match=r"Failed to install archon-search\[graph\]: uv"),
        ):
            _install_graph_extra()
        assert mock_run.call_count == 1

    def test_unmapped_torch_build_falls_back_to_uv_pip(self, uv_tool_prefix, monkeypatch, capsys):
        """A torch build uv has no --torch-backend for would be swapped by a re-resolve."""
        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata, "version", lambda name: "2.14.1+git1234" if name == "torch" else "0"
        )
        with patch("subprocess.run") as mock_run:
            _install_graph_extra()
        assert mock_run.call_args_list[0][0][0] == [
            "uv", "pip", "install", "--python", sys.executable, "archon-search[graph]",
        ]
        warning = capsys.readouterr().err
        assert "git1234" in warning
        assert "uv tool install 'archon-search[graph]'" in warning
        assert "--torch-backend" in warning

    def test_dry_run_runs_nothing(self, uv_tool_prefix, capsys):
        with patch("subprocess.run") as mock_run:
            _install_graph_extra(dry_run=True)
        mock_run.assert_not_called()
        assert "[dry-run] Would install archon-search[graph]" in capsys.readouterr().out

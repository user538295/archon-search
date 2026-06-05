"""Tests for Task 4.2 — _download_fasttext_model() in archon_search/install.py."""
from __future__ import annotations

import io
import stat
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from archon_search.install import _download_fasttext_model, InstallError, FASTTEXT_MODEL_URL


# ---------------------------------------------------------------------------
# FASTTEXT_MODEL_URL constant
# ---------------------------------------------------------------------------


def test_fasttext_model_url_constant():
    """FASTTEXT_MODEL_URL must point to the fbaipublicfiles hosted model."""
    assert FASTTEXT_MODEL_URL == "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"


def test_fasttext_model_url_is_string():
    """FASTTEXT_MODEL_URL must be a string."""
    assert isinstance(FASTTEXT_MODEL_URL, str)


# ---------------------------------------------------------------------------
# _download_fasttext_model — skip if exists
# ---------------------------------------------------------------------------


def test_download_fasttext_model_skips_if_exists(tmp_path: Path):
    """If lid.176.ftz already exists, urlopen must not be called."""
    target = tmp_path / "lid.176.ftz"
    target.write_bytes(b"fake model content")

    with patch("urllib.request.urlopen") as mock_urlopen:
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_not_called()


def test_download_fasttext_model_skips_returns_without_error(tmp_path: Path):
    """Skip path must return None (no exception)."""
    target = tmp_path / "lid.176.ftz"
    target.write_bytes(b"fake model content")

    result = _download_fasttext_model(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# _download_fasttext_model — directory creation
# ---------------------------------------------------------------------------


def test_download_fasttext_model_creates_dir(tmp_path: Path):
    """models_dir that does not exist must be created."""
    models_dir = tmp_path / "new_models_dir"
    assert not models_dir.exists()

    fake_response = MagicMock()
    fake_response.read.return_value = b"fake content"
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj"):
        # Write dummy bytes to make file non-zero size
        def create_file_side_effect(src, dst, **kwargs):
            (models_dir / "lid.176.ftz").write_bytes(b"x" * 100)
        with patch("shutil.copyfileobj", side_effect=create_file_side_effect):
            _download_fasttext_model(models_dir)

    assert models_dir.exists()


def test_download_fasttext_model_creates_dir_with_mode_700(tmp_path: Path):
    """Created models_dir must have mode 0o700."""
    models_dir = tmp_path / "new_models_dir"
    assert not models_dir.exists()

    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    def create_file_side_effect(src, dst, **kwargs):
        (models_dir / "lid.176.ftz").write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(models_dir)

    mode = oct(stat.S_IMODE(models_dir.stat().st_mode))
    assert mode == oct(0o700), f"Expected 0o700, got {mode}"


# ---------------------------------------------------------------------------
# _download_fasttext_model — network error handling
# ---------------------------------------------------------------------------


def test_download_fasttext_model_network_error_raises_install_error(tmp_path: Path):
    """URLError from urlopen must raise InstallError."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        with pytest.raises(InstallError):
            _download_fasttext_model(tmp_path)


def test_download_fasttext_model_network_error_message(tmp_path: Path):
    """InstallError from network failure must contain useful context."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)
    assert "fasttext" in str(exc_info.value).lower() or "download" in str(exc_info.value).lower() or "lid.176" in str(exc_info.value).lower()


def test_download_fasttext_model_oserror_raises_install_error(tmp_path: Path):
    """OSError from copyfileobj (e.g., disk full) must raise InstallError."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=OSError("No space left on device")):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert "disk" in str(exc_info.value).lower() or "space" in str(exc_info.value).lower() or "write" in str(exc_info.value).lower() or "lid.176" in str(exc_info.value).lower()


def test_download_fasttext_model_oserror_cleans_up_partial_file(tmp_path: Path):
    """OSError during write must clean up the partial file so re-download is possible."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    target = tmp_path / "lid.176.ftz"

    def write_partial_then_fail(src, dst, **kwargs):
        # Simulate partial write before failure
        target.write_bytes(b"partial")
        raise OSError("No space left on device")

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=write_partial_then_fail):
        with pytest.raises(InstallError):
            _download_fasttext_model(tmp_path)

    assert not target.exists(), "Partial file must be deleted on OSError so re-download is possible"


# ---------------------------------------------------------------------------
# _download_fasttext_model — uses urlopen with timeout (not urlretrieve)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_uses_urlopen_not_urlretrieve(tmp_path: Path):
    """Must use urllib.request.urlopen, not urlretrieve."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    def create_file_side_effect(src, dst, **kwargs):
        (tmp_path / "lid.176.ftz").write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response) as mock_urlopen, \
         patch("urllib.request.urlretrieve") as mock_retrieve, \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_called_once()
    mock_retrieve.assert_not_called()


def test_download_fasttext_model_urlopen_called_with_timeout(tmp_path: Path):
    """urlopen must be called with the correct URL and timeout=120."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    def create_file_side_effect(src, dst, **kwargs):
        (tmp_path / "lid.176.ftz").write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response) as mock_urlopen, \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_called_once_with(FASTTEXT_MODEL_URL, timeout=120)


# ---------------------------------------------------------------------------
# _download_fasttext_model — corrupt/empty file detection
# ---------------------------------------------------------------------------


def test_download_fasttext_model_empty_file_raises_install_error(tmp_path: Path):
    """If downloaded file has size 0, InstallError must be raised."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    # copyfileobj writes nothing → file stays empty (size 0)
    def write_empty(src, dst, **kwargs):
        (tmp_path / "lid.176.ftz").write_bytes(b"")

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=write_empty):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert "corrupt" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower() or "size" in str(exc_info.value).lower()


def test_download_fasttext_model_corrupt_file_deleted_on_error(tmp_path: Path):
    """After a corrupt-file InstallError, the corrupt file must be deleted."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    target = tmp_path / "lid.176.ftz"

    def write_empty(src, dst, **kwargs):
        target.write_bytes(b"")

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=write_empty):
        with pytest.raises(InstallError):
            _download_fasttext_model(tmp_path)

    assert not target.exists(), "Corrupt file must be deleted after InstallError"


# ---------------------------------------------------------------------------
# _download_fasttext_model — target path
# ---------------------------------------------------------------------------


def test_download_fasttext_model_target_filename(tmp_path: Path):
    """Downloaded file must be named lid.176.ftz in models_dir."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    target = tmp_path / "lid.176.ftz"

    def create_file_side_effect(src, dst, **kwargs):
        target.write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    assert target.exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — print progress
# ---------------------------------------------------------------------------


def test_download_fasttext_model_prints_step_label(tmp_path: Path, capsys):
    """Must print the [4b/5] step label during download."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    def create_file_side_effect(src, dst, **kwargs):
        (tmp_path / "lid.176.ftz").write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    captured = capsys.readouterr()
    assert "[4b/5]" in captured.out or "[4b/5]" in captured.err


def test_download_fasttext_model_prints_fasttext_language_model(tmp_path: Path, capsys):
    """Step label must mention 'fasttext' and 'language model'."""
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    def create_file_side_effect(src, dst, **kwargs):
        (tmp_path / "lid.176.ftz").write_bytes(b"x" * 100)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "fasttext" in combined


# ---------------------------------------------------------------------------
# _download_fasttext_model — successful download end-to-end
# ---------------------------------------------------------------------------


def test_download_fasttext_model_successful_download(tmp_path: Path):
    """Successful download creates the file with non-zero content."""
    fake_content = b"fake fasttext model" * 100
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = MagicMock(return_value=False)

    target = tmp_path / "lid.176.ftz"

    def create_file_side_effect(src, dst, **kwargs):
        target.write_bytes(fake_content)

    with patch("urllib.request.urlopen", return_value=fake_response), \
         patch("shutil.copyfileobj", side_effect=create_file_side_effect):
        _download_fasttext_model(tmp_path)

    assert target.exists()
    assert target.stat().st_size > 0

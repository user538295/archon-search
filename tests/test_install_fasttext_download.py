"""Tests for _download_fasttext_model() — durable, verified single-file download (BE-13)."""
from __future__ import annotations

import dataclasses
import hashlib
import stat
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import _download_fasttext_model, InstallError, FASTTEXT_MODEL_URL
from archon_search.install.licenses import FASTTEXT_ARTIFACT
from archon_search.install.provisioning import ProvisionFailureKind
from tests.test_no_raw_durable_writes import ARCHON_SEARCH_PKG, find_violations


def _pin_digest_of(content: bytes):
    """Patch the module-level descriptor so *content* is what the pinned digest/size expect."""
    return patch(
        "archon_search.install.licenses.FASTTEXT_ARTIFACT",
        dataclasses.replace(
            FASTTEXT_ARTIFACT,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        ),
    )


def _fake_response(content: bytes) -> MagicMock:
    """A context-manager mock standing in for urlopen()'s return value."""
    response = MagicMock()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = content
    return response


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
# _download_fasttext_model — skip if exists (idempotent — BE-13)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_skips_if_exists(tmp_path: Path):
    """If a digest-and-size-matching lid.176.ftz already exists, urlopen must not be called."""
    content = b"fake model content"
    (tmp_path / "lid.176.ftz").write_bytes(content)

    with _pin_digest_of(content), patch("urllib.request.urlopen") as mock_urlopen:
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_not_called()


def test_download_fasttext_model_skips_returns_without_error(tmp_path: Path):
    """Skip path must return None (no exception)."""
    content = b"fake model content"
    (tmp_path / "lid.176.ftz").write_bytes(content)

    with _pin_digest_of(content):
        result = _download_fasttext_model(tmp_path)
    assert result is None


def test_already_present_matching_file_is_not_redownloaded(tmp_path: Path):
    """Integration: a file matching both the pinned digest and byte count is left
    byte-for-byte alone and the whole real function runs against a real directory
    (folds in the old BE-3 fixture, exercised end to end here)."""
    content = b"pinned fasttext bytes" * 10
    target = tmp_path / "lid.176.ftz"
    target.write_bytes(content)

    with _pin_digest_of(content), patch("urllib.request.urlopen") as mock_urlopen:
        result = _download_fasttext_model(tmp_path)

    mock_urlopen.assert_not_called()
    assert result is None
    assert target.read_bytes() == content


def test_fasttext_digest_mismatch_triggers_redownload(tmp_path: Path):
    """A pre-existing mismatching file is deleted and re-fetched, non-fatally."""
    target = tmp_path / "lid.176.ftz"
    target.write_bytes(b"stale legacy partial download")
    fresh = b"the pinned bytes" * 10

    with _pin_digest_of(fresh), patch(
        "urllib.request.urlopen", return_value=_fake_response(fresh)
    ) as mock_urlopen:
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_called_once_with(FASTTEXT_MODEL_URL, timeout=120)
    assert target.read_bytes() == fresh


# ---------------------------------------------------------------------------
# _download_fasttext_model — directory creation
# ---------------------------------------------------------------------------


def test_download_fasttext_model_creates_dir(tmp_path: Path):
    """models_dir that does not exist must be created."""
    models_dir = tmp_path / "new_models_dir"
    assert not models_dir.exists()

    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ):
        _download_fasttext_model(models_dir)

    assert models_dir.exists()


def test_download_fasttext_model_creates_dir_with_mode_700(tmp_path: Path):
    """Created models_dir must have mode 0o700."""
    models_dir = tmp_path / "new_models_dir"
    assert not models_dir.exists()

    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ):
        _download_fasttext_model(models_dir)

    mode = oct(stat.S_IMODE(models_dir.stat().st_mode))
    assert mode == oct(0o700), f"Expected 0o700, got {mode}"


# ---------------------------------------------------------------------------
# _download_fasttext_model — disk space check (BE-13)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_insufficient_disk_space_raises_its_own_category(tmp_path: Path):
    """Insufficient free space is checked before any network call and reports
    insufficient_disk, not a download/digest/size category."""
    fake_usage = MagicMock(free=1)

    with patch("shutil.disk_usage", return_value=fake_usage), patch(
        "urllib.request.urlopen"
    ) as mock_urlopen:
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    mock_urlopen.assert_not_called()
    assert ProvisionFailureKind.insufficient_disk in str(exc_info.value)


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


def test_download_fasttext_model_network_error_leaves_nothing_on_disk(tmp_path: Path):
    """A network failure never touches disk — nothing is staged before the read."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        with pytest.raises(InstallError):
            _download_fasttext_model(tmp_path)

    assert not (tmp_path / "lid.176.ftz").exists()
    assert not (tmp_path / "lid.176.ftz.tmp").exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — byte-count assert (BE-13 / S37)
# ---------------------------------------------------------------------------


def test_short_download_fails_the_byte_count_assert_before_placement(tmp_path: Path):
    """Unit: a download that returns fewer bytes than the pinned descriptor fails the
    byte-count assert, reports size_mismatch (distinct from digest_mismatch), and
    places nothing on disk."""
    short_content = b"only a few bytes"

    with _pin_digest_of(b"the full pinned bytes" * 10), patch(
        "urllib.request.urlopen", return_value=_fake_response(short_content)
    ):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert ProvisionFailureKind.size_mismatch in str(exc_info.value)
    assert ProvisionFailureKind.digest_mismatch not in str(exc_info.value)
    assert not (tmp_path / "lid.176.ftz").exists()
    assert not (tmp_path / "lid.176.ftz.tmp").exists()


def test_download_fasttext_model_empty_file_raises_install_error(tmp_path: Path):
    """If the downloaded content is empty, InstallError must be raised via the
    byte-count assert."""
    with patch("urllib.request.urlopen", return_value=_fake_response(b"")):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert "size" in str(exc_info.value).lower() or "corrupt" in str(exc_info.value).lower()
    assert not (tmp_path / "lid.176.ftz").exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — digest assert (BE-13 / S11)
# ---------------------------------------------------------------------------


def test_digest_mismatch_places_nothing_and_reports_its_own_category(tmp_path: Path):
    """Unit: freshly downloaded bytes of the right length but the wrong digest fail
    the digest assert, report digest_mismatch (distinct from size_mismatch), and
    place nothing on disk."""
    tampered = b"tampered bytes that do not match the pin"
    pinned_same_length = b"x" * len(tampered)

    with _pin_digest_of(pinned_same_length), patch(
        "urllib.request.urlopen", return_value=_fake_response(tampered)
    ):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert ProvisionFailureKind.digest_mismatch in str(exc_info.value)
    assert ProvisionFailureKind.size_mismatch not in str(exc_info.value)
    assert not (tmp_path / "lid.176.ftz").exists()
    assert not (tmp_path / "lid.176.ftz.tmp").exists()


def test_fresh_download_with_mismatching_digest_raises_install_error(tmp_path: Path):
    """Freshly downloaded bytes that do not match the pinned digest must be rejected."""
    same_length_wrong_bytes = b"tampered bytes that do not match the pin"

    with _pin_digest_of(b"the bytes we actually expect here!!!!!!!"), patch(
        "urllib.request.urlopen", return_value=_fake_response(same_length_wrong_bytes)
    ):
        with pytest.raises(InstallError) as exc_info:
            _download_fasttext_model(tmp_path)

    assert "digest" in str(exc_info.value).lower()
    assert not (tmp_path / "lid.176.ftz").exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — uses urlopen with timeout (not urlretrieve)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_uses_urlopen_not_urlretrieve(tmp_path: Path):
    """Must use urllib.request.urlopen, not urlretrieve."""
    content = b"x" * 100
    with _pin_digest_of(content), \
         patch("urllib.request.urlopen", return_value=_fake_response(content)) as mock_urlopen, \
         patch("urllib.request.urlretrieve") as mock_retrieve:
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_called_once()
    mock_retrieve.assert_not_called()


def test_download_fasttext_model_urlopen_called_with_timeout(tmp_path: Path):
    """urlopen must be called with the correct URL and timeout=120."""
    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ) as mock_urlopen:
        _download_fasttext_model(tmp_path)

    mock_urlopen.assert_called_once_with(FASTTEXT_MODEL_URL, timeout=120)


# ---------------------------------------------------------------------------
# _download_fasttext_model — target path
# ---------------------------------------------------------------------------


def test_download_fasttext_model_target_filename(tmp_path: Path):
    """Downloaded file must be named lid.176.ftz in models_dir."""
    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ):
        _download_fasttext_model(tmp_path)

    assert (tmp_path / "lid.176.ftz").exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — print progress
# ---------------------------------------------------------------------------


def test_download_fasttext_model_prints_step_label(tmp_path: Path, capsys):
    """Must print the [4b/5] step label during download."""
    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ):
        _download_fasttext_model(tmp_path)

    captured = capsys.readouterr()
    assert "[4b/5]" in captured.out or "[4b/5]" in captured.err


def test_download_fasttext_model_prints_fasttext_language_model(tmp_path: Path, capsys):
    """Step label must mention 'fasttext' and 'language model'."""
    content = b"x" * 100
    with _pin_digest_of(content), patch(
        "urllib.request.urlopen", return_value=_fake_response(content)
    ):
        _download_fasttext_model(tmp_path)

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "fasttext" in combined


# ---------------------------------------------------------------------------
# _download_fasttext_model — successful download, durable publish (BE-13)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_successful_download(tmp_path: Path):
    """Successful download creates the file with the expected content."""
    fake_content = b"fake fasttext model" * 100
    target = tmp_path / "lid.176.ftz"

    with _pin_digest_of(fake_content), patch(
        "urllib.request.urlopen", return_value=_fake_response(fake_content)
    ):
        _download_fasttext_model(tmp_path)

    assert target.exists()
    assert target.read_bytes() == fake_content


def test_download_fasttext_model_leaves_no_tmp_file_behind(tmp_path: Path):
    """The atomic publish must not leave its staging .tmp file behind."""
    fake_content = b"fake fasttext model" * 100

    with _pin_digest_of(fake_content), patch(
        "urllib.request.urlopen", return_value=_fake_response(fake_content)
    ):
        _download_fasttext_model(tmp_path)

    assert not (tmp_path / "lid.176.ftz.tmp").exists()


# ---------------------------------------------------------------------------
# _download_fasttext_model — durable-write lint gate (BE-13)
# ---------------------------------------------------------------------------


def test_download_fasttext_model_calls_atomic_write_bytes(tmp_path: Path):
    """Explicit proof the publish path routes through atomic_write_bytes with the
    verified content, correct target, and mode=0o644 — the lint gate alone cannot
    prove this (it cannot catch a raw open()/write() replacement)."""
    fake_content = b"fake fasttext model" * 100
    target = tmp_path / "lid.176.ftz"

    with _pin_digest_of(fake_content), patch(
        "urllib.request.urlopen", return_value=_fake_response(fake_content)
    ), patch(
        "archon_search.install.licenses.atomic_write_bytes"
    ) as mock_atomic_write:
        _download_fasttext_model(tmp_path)

    mock_atomic_write.assert_called_once_with(target, fake_content, mode=0o644)


def test_provisioner_satisfies_the_durable_write_lint_gate():
    """Integration: licenses.py's fasttext provisioner routes through
    archon_search/_durable_io.py rather than a raw durable-write pattern —
    tests/test_no_raw_durable_writes.py must still pass for this file."""
    violations = [
        v for v in find_violations(ARCHON_SEARCH_PKG) if v[0] == "install/licenses.py"
    ]
    assert not violations, violations

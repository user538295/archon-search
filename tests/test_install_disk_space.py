"""Tests for _check_disk_space() in archon_search/install.py (Task C0-2.2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import InstallError, _check_disk_space
from archon_search.profiles import get_profile


@pytest.fixture()
def minimal_profile():
    return get_profile("minimal", multilingual=False)


def test_disk_space_sufficient_does_not_raise(minimal_profile, tmp_path):
    usage = MagicMock()
    usage.free = minimal_profile.download_mb * 1024 * 1024 * 100  # way more than needed
    with patch("archon_search.install.shutil.disk_usage", return_value=usage):
        _check_disk_space(minimal_profile, base_path=tmp_path)  # must not raise


def test_disk_space_insufficient_raises_install_error(minimal_profile, tmp_path):
    usage = MagicMock()
    usage.free = 1  # essentially zero
    with patch("archon_search.install.shutil.disk_usage", return_value=usage):
        with pytest.raises(InstallError, match="Insufficient disk space"):
            _check_disk_space(minimal_profile, base_path=tmp_path)


def test_disk_space_walks_up_to_existing_ancestor(minimal_profile, tmp_path):
    # Construct a path that does not exist under tmp_path
    non_existent = tmp_path / "a" / "b" / "c"
    assert not non_existent.exists()
    assert not non_existent.parent.exists()
    assert tmp_path.exists()  # the ancestor exists

    usage = MagicMock()
    usage.free = minimal_profile.download_mb * 1024 * 1024 * 100
    with patch("archon_search.install.shutil.disk_usage", return_value=usage) as mock_du:
        _check_disk_space(minimal_profile, base_path=non_existent)

    # disk_usage should have been called on tmp_path (first existing ancestor)
    mock_du.assert_called_once_with(tmp_path)


def test_disk_space_usage_raises_is_propagated(minimal_profile, tmp_path):
    with patch("archon_search.install.shutil.disk_usage", side_effect=PermissionError("no access")):
        with pytest.raises(PermissionError, match="no access"):
            _check_disk_space(minimal_profile, base_path=tmp_path)

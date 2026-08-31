"""Tests for _check_disk_space() in archon_search/install.py (Task C0-2.2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    InstallError,
    _check_disk_space,
    _compute_svc_timeout,
    _planned_total_bytes,
    _prewarm_timeout,
    _render_summary,
)
from archon_search.install.config_writer import WizardFeatures
from archon_search.install.extras import CODE_EXTRA_SIZE_ESTIMATE, GRAPH_MODEL_SIZE_ESTIMATE
from archon_search.install.licenses import FASTTEXT_ARTIFACT
from archon_search.install.provisioning import BYTES_PER_MB
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


# ---------------------------------------------------------------------------
# Planned-download total (BE-4 / S38)
# ---------------------------------------------------------------------------

def test_planned_total_sums_profile_and_selected_extras(minimal_profile):
    features = WizardFeatures(
        install_code_extra=True,
        install_graph_extra=True,
        install_multilingual_extra=True,
    )
    assert _planned_total_bytes(minimal_profile, features) == (
        minimal_profile.download_mb * BYTES_PER_MB
        + CODE_EXTRA_SIZE_ESTIMATE.declared_mb * BYTES_PER_MB
        + GRAPH_MODEL_SIZE_ESTIMATE.declared_mb * BYTES_PER_MB
        + FASTTEXT_ARTIFACT.size_bytes
    )


def test_planned_total_covers_profile_alone_when_nothing_is_selected(minimal_profile):
    assert _planned_total_bytes(minimal_profile) == minimal_profile.download_mb * BYTES_PER_MB
    assert _planned_total_bytes(minimal_profile, WizardFeatures()) == (
        minimal_profile.download_mb * BYTES_PER_MB
    )


def test_required_free_bytes_is_twice_the_total(minimal_profile, tmp_path):
    features = WizardFeatures(install_graph_extra=True)
    total = _planned_total_bytes(minimal_profile, features)

    usage = MagicMock()
    usage.free = total * 2
    with patch("archon_search.install.shutil.disk_usage", return_value=usage):
        _check_disk_space(minimal_profile, base_path=tmp_path, features=features)

    usage.free = total * 2 - 1
    with patch("archon_search.install.shutil.disk_usage", return_value=usage):
        with pytest.raises(InstallError, match="Insufficient disk space"):
            _check_disk_space(minimal_profile, base_path=tmp_path, features=features)


def test_prewarm_timeout_still_reads_the_profile_figure(minimal_profile):
    """_prewarm_timeout is deliberately NOT moved onto the summed total."""
    features = WizardFeatures(install_graph_extra=True)
    assert _planned_total_bytes(minimal_profile, features) > (
        minimal_profile.download_mb * BYTES_PER_MB
    )
    with patch("archon_search.install.prewarm._planned_total_bytes") as spy:
        assert _prewarm_timeout(minimal_profile) == min(
            1800, max(300, minimal_profile.download_mb * 1_000_000 // 100_000)
        )
    spy.assert_not_called()


def test_disk_guard_timeouts_and_display_all_derive_from_one_total(minimal_profile, tmp_path):
    """S38: one total drives the disk guard, both service timeouts and the display."""
    features = WizardFeatures(
        install_code_extra=True,
        install_graph_extra=True,
        install_multilingual_extra=True,
        eager_load_embedders=True,
    )
    total = _planned_total_bytes(minimal_profile, features)

    usage = MagicMock()
    usage.free = total * 2 - 1
    with patch("archon_search.install.shutil.disk_usage", return_value=usage):
        with pytest.raises(InstallError, match=f"~{total * 2 // BYTES_PER_MB} MB free"):
            _check_disk_space(minimal_profile, base_path=tmp_path, features=features)

    assert _compute_svc_timeout(True, total) == min(600, max(60, total // (10 * BYTES_PER_MB)))
    assert _compute_svc_timeout(True, total) > _compute_svc_timeout(
        True, _planned_total_bytes(minimal_profile)
    )

    output = _render_summary(
        "minimal", minimal_profile, multilingual=False, providers=[],
        features=features, total_bytes=total,
    )
    assert f"~{total // BYTES_PER_MB} MB" in output

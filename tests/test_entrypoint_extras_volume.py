"""Regression: the extras volume must never shadow the image's own packages.

``scripts/docker-entrypoint.sh`` installs optional extras into a persistent
named volume (``/pip-packages``) and prepends it to ``PYTHONPATH``. Whatever
lands in that volume therefore wins over the image's site-packages on every
later start — including after the user pulls a newer image and keeps the
volume. These tests run the REAL entrypoint with a REAL interpreter and REAL
pip, fully offline, on the harness in ``tests/_entrypoint_harness.py``:

* an image venv (first on ``PATH``, so the entrypoint's ``python3`` is it)
  holding ``archon-search`` + ``basedep``;
* the image's ``/app`` as the working directory: an offline-buildable
  ``archon-search`` source tree of the image's version;
* the offline wheelhouse of :func:`tests._entrypoint_harness.build_wheelhouse`,
  which these tests use for ``archon-search``/``basedep`` 1.0 and 2.0 and the
  ``code``-only ``extradep``.
"""
from tests._entrypoint_harness import (
    BASEDEP_NEW,
    NEW_IMAGE_VERSION,
    Container,
    loaded_from,
)

EXTRAS = "code"


def test_first_start_keeps_image_packages_ahead_of_volume(entrypoint_container: Container) -> None:
    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert not loaded_from(start, "archon_search", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "basedep", entrypoint_container.image_venv), start.imports
    assert not loaded_from(start, "basedep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports


def test_image_upgrade_with_reused_volume_runs_new_code(entrypoint_container: Container) -> None:
    first_start = entrypoint_container.start(EXTRAS)
    assert first_start.returncode == 0, first_start.output

    # Pull the newer image: new archon-search + base deps baked in, new /app,
    # same named volume.
    entrypoint_container.upgrade_image(NEW_IMAGE_VERSION)
    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert start.imports["archon_search"]["version"] == NEW_IMAGE_VERSION, start.imports
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert start.imports["basedep"]["version"] == BASEDEP_NEW, start.imports
    assert loaded_from(start, "basedep", entrypoint_container.image_venv), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports

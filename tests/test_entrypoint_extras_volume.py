"""Regression: the extras volume must never shadow the image's own packages.

``scripts/docker-entrypoint.sh`` installs optional extras into a persistent
named volume (``/pip-packages``) and prepends it to ``PYTHONPATH``. Whatever
lands in that volume therefore wins over the image's site-packages on every
later start — including after the user pulls a newer image and keeps the
volume. These tests run the REAL entrypoint with a REAL interpreter and REAL
pip, fully offline, on the harness in ``tests/_entrypoint_harness.py``:

* an image venv (first on ``PATH``, so the entrypoint's ``python3`` is it)
  holding ``archon-search`` + ``basedep`` + the CPU ``onnxruntime``, or, for
  the GPU image, ``onnxruntime-gpu`` in its place;
* the image's ``/app`` as the working directory: an offline-buildable
  ``archon-search`` source tree of the image's version;
* the offline wheelhouse of :func:`tests._entrypoint_harness.build_wheelhouse`,
  which these tests use for ``archon-search``/``basedep`` 1.0 and 2.0, both
  ``onnxruntime`` builds and the ``code``-only ``extradep``.
"""
from tests._entrypoint_harness import (
    BASEDEP_NEW,
    EXTRADEP_VERSION,
    NEW_IMAGE_VERSION,
    ONNXRUNTIME_DISTRIBUTIONS,
    ONNXRUNTIME_GPU_VERSION,
    ONNXRUNTIME_VERSION,
    Container,
    WheelSpec,
    loaded_from,
)

EXTRAS = "code"
LEFT_OUT_LOG_PREFIX = "Leaving out "
NOTHING_TO_INSTALL_LOG = f"The image already provides every package of [{EXTRAS}] — nothing to install."
# A newer CPU onnxruntime whose metadata Name differs from `onnxruntime` only in case (same PEP 503 name).
RESPELLED_ONNXRUNTIME = WheelSpec("ONNXRuntime", "1.1", module_name="onnxruntime")


def _left_out_log(left_out: str) -> str:
    """The entrypoint's log line for CPU onnxruntime ``left_out`` (``name==version``) on the GPU image."""
    return (
        f"{LEFT_OUT_LOG_PREFIX}{left_out}: the image's onnxruntime-gpu {ONNXRUNTIME_GPU_VERSION} "
        "replaces it (same onnxruntime import package)."
    )


def test_first_start_keeps_image_packages_ahead_of_volume(entrypoint_container: Container) -> None:
    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert not loaded_from(start, "archon_search", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "basedep", entrypoint_container.image_venv), start.imports
    assert not loaded_from(start, "basedep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    # The stock CPU image ships onnxruntime and no onnxruntime-gpu: nothing is left out, onnxruntime stays image-side.
    assert LEFT_OUT_LOG_PREFIX not in start.output, start.output
    assert loaded_from(start, "onnxruntime", entrypoint_container.image_venv), start.imports
    assert not ONNXRUNTIME_DISTRIBUTIONS & set(entrypoint_container.volume_distributions()), start.output


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


def test_gpu_image_onnxruntime_is_not_shadowed_by_the_cpu_build(entrypoint_container: Container) -> None:
    # The GPU image swaps CPU onnxruntime for onnxruntime-gpu (Dockerfile); fastembed still requires `onnxruntime`.
    entrypoint_container.swap_onnxruntime_for_gpu()

    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    onnxruntime = start.imports["onnxruntime"]
    assert onnxruntime["version"] == ONNXRUNTIME_GPU_VERSION, f"{onnxruntime}\n{start.output}"
    assert loaded_from(start, "onnxruntime", entrypoint_container.image_venv), f"{onnxruntime}\n{start.output}"
    assert not ONNXRUNTIME_DISTRIBUTIONS & set(entrypoint_container.volume_distributions()), start.output


def test_gpu_image_logs_the_onnxruntime_it_leaves_out_and_the_gpu_build_replacing_it(
    entrypoint_container: Container,
) -> None:
    entrypoint_container.swap_onnxruntime_for_gpu()

    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert _left_out_log(f"onnxruntime=={ONNXRUNTIME_VERSION}") in start.output, start.output


def test_image_without_onnxruntime_gpu_installs_the_cpu_onnxruntime_its_extras_need(
    entrypoint_container: Container,
) -> None:
    # No onnxruntime build in the image at all: the resolution adds CPU onnxruntime and nothing replaces it.
    entrypoint_container.uninstall("onnxruntime")

    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert LEFT_OUT_LOG_PREFIX not in start.output, start.output
    assert "onnxruntime" in entrypoint_container.volume_distributions(), start.output
    assert start.imports["onnxruntime"]["version"] == ONNXRUNTIME_VERSION, start.imports
    assert loaded_from(start, "onnxruntime", entrypoint_container.volume_root), start.imports


def test_gpu_image_whose_extras_add_only_onnxruntime_installs_nothing(entrypoint_container: Container) -> None:
    # The `code` extra's own package is already in the image, so the resolution adds only CPU onnxruntime.
    entrypoint_container.bake(f"extradep=={EXTRADEP_VERSION}")
    entrypoint_container.swap_onnxruntime_for_gpu()

    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert _left_out_log(f"onnxruntime=={ONNXRUNTIME_VERSION}") in start.output, start.output
    assert NOTHING_TO_INSTALL_LOG in start.output, start.output
    assert not ONNXRUNTIME_DISTRIBUTIONS & set(entrypoint_container.volume_distributions()), start.output
    assert loaded_from(start, "onnxruntime", entrypoint_container.image_venv), start.imports


def test_gpu_image_leaves_out_cpu_onnxruntime_whatever_the_case_of_its_metadata_name(
    entrypoint_container: Container,
) -> None:
    entrypoint_container.swap_onnxruntime_for_gpu()
    entrypoint_container.publish(RESPELLED_ONNXRUNTIME)

    start = entrypoint_container.start(EXTRAS)

    assert start.returncode == 0, start.output
    assert _left_out_log(f"{RESPELLED_ONNXRUNTIME.name}=={RESPELLED_ONNXRUNTIME.version}") in start.output, (
        start.output
    )
    assert not ONNXRUNTIME_DISTRIBUTIONS & set(entrypoint_container.volume_distributions()), start.output
    assert start.imports["onnxruntime"]["version"] == ONNXRUNTIME_GPU_VERSION, start.imports

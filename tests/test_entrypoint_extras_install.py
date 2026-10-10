"""docker-entrypoint.sh: when the extras install runs, what it leaves in the
``/pip-packages`` volume, and what the CMD ends up importing.

The stamp fingerprints the image (its package set and Python) plus
``ARCHON_EXTRAS``; any change reinstalls the packages the extras add to the
image, and the volume's content is replaced only once an install attempt
succeeds. Runs the real script with real, offline pip — see
``tests/_entrypoint_harness.py``.
"""
import pytest

from tests._entrypoint_harness import (
    BASEDEP_NEW,
    BASEDEP_OLD,
    EXTRADEP_VERSION,
    IMAGE_VERSION,
    STAMP_NAME,
    Container,
    WheelSpec,
    loaded_from,
)

# pip's own default read timeout, which the entrypoint must raise.
PIP_DEFAULT_TIMEOUT_S = 15
MAX_ATTEMPTS = 3
# More failures than the entrypoint has attempts: every attempt fails.
FAIL_EVERY_ATTEMPT = 10 * MAX_ATTEMPTS


def test_first_start_installs_only_missing_extras(entrypoint_container: Container) -> None:
    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert loaded_from(start, "basedep", entrypoint_container.image_venv), start.imports
    assert entrypoint_container.stamp.is_file()


def test_unchanged_image_second_start_skips_pip(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0

    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert start.pip_install_timeouts == [], start.output
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports


def test_extras_change_triggers_reinstall(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0

    start = entrypoint_container.start("code,graph")

    assert start.returncode == 0, start.output
    assert loaded_from(start, "graphdep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports


def test_rebuilt_image_with_same_version_but_new_deps_reinstalls_cleanly(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0
    leftover = entrypoint_container.volume_root / "left-by-previous-image"
    leftover.write_text("")

    entrypoint_container.bake(f"basedep=={BASEDEP_NEW}")  # archon-search stays at IMAGE_VERSION
    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert start.imports["archon_search"]["version"] == IMAGE_VERSION, start.imports
    assert start.imports["basedep"]["version"] == BASEDEP_NEW, start.imports
    assert loaded_from(start, "basedep", entrypoint_container.image_venv), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    assert not leftover.exists(), "a reinstall for a changed image must replace the whole volume content"


def test_base_dep_version_an_extra_pins_wins_over_the_image(entrypoint_container: Container) -> None:
    entrypoint_container.bake(f"basedep=={BASEDEP_NEW}")  # graphdep needs basedep<2.0

    start = entrypoint_container.start("graph")

    assert start.returncode == 0, start.output
    assert start.imports["basedep"]["version"] == BASEDEP_OLD, start.imports
    assert loaded_from(start, "basedep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "graphdep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports


def test_legacy_volume_is_cleaned_and_no_longer_shadows_the_image(entrypoint_container: Container) -> None:
    entrypoint_container.stamp.write_text("code")  # a legacy stamp: the extras list only
    legacy_copy = entrypoint_container.plant_legacy_app_copy()

    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert start.imports["archon_search"]["version"] == IMAGE_VERSION, start.imports
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    assert not legacy_copy.exists(), "the legacy archon-search copy must be removed from the volume"


def test_empty_extras_skips_install_and_keeps_volume_off_pythonpath(entrypoint_container: Container) -> None:
    entrypoint_container.plant_legacy_app_copy()

    start = entrypoint_container.start("")

    assert start.returncode == 0, start.output
    assert start.pip_install_timeouts == [], start.output
    assert loaded_from(start, "archon_search", entrypoint_container.image_venv), start.imports
    assert start.imports["extradep"] is None, start.imports
    assert not entrypoint_container.stamp.exists()


def test_unset_extras_installs_the_default_extras(entrypoint_container: Container) -> None:
    start = entrypoint_container.start(None)

    assert start.returncode == 0, start.output
    assert "ARCHON_EXTRAS=graph,code,multilingual" in start.output
    for module in ("graphdep", "extradep", "multilingualdep"):
        assert loaded_from(start, module, entrypoint_container.volume_root), start.imports
    assert start.imports["fusiondep"] is None, start.imports


def test_extras_the_image_already_provides_install_nothing(entrypoint_container: Container) -> None:
    entrypoint_container.bake(f"extradep=={EXTRADEP_VERSION}")

    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert loaded_from(start, "extradep", entrypoint_container.image_venv), start.imports
    assert entrypoint_container.volume_entries() == [STAMP_NAME]


def test_extra_names_match_after_pep685_normalisation(entrypoint_container: Container) -> None:
    # pyproject.toml spells it `rag_fusion`; the wheel metadata says `rag-fusion`.
    start = entrypoint_container.start("rag_fusion,CODE")

    assert start.returncode == 0, start.output
    assert loaded_from(start, "fusiondep", entrypoint_container.volume_root), start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports


def test_unknown_extra_fails_loudly_and_leaves_the_volume_alone(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0
    previous_entries = entrypoint_container.volume_entries()
    previous_stamp = entrypoint_container.stamp.read_text()

    start = entrypoint_container.start("code,grph")

    assert start.returncode != 0, start.output
    assert "unknown extra(s) in ARCHON_EXTRAS: grph" in start.output
    assert "valid extras: code, graph, multilingual, rag-fusion" in start.output
    assert "ERROR: entrypoint aborted" in start.output
    assert start.pip_install_timeouts == [], start.output
    assert entrypoint_container.volume_entries() == previous_entries
    assert entrypoint_container.stamp.read_text() == previous_stamp


@pytest.mark.parametrize("extras", ["code,", ",code", "code,,graph", " "])
def test_empty_extras_entry_fails_loudly_naming_the_problem(entrypoint_container: Container, extras: str) -> None:
    start = entrypoint_container.start(extras)

    assert start.returncode != 0, start.output
    assert "ERROR: empty entry in ARCHON_EXTRAS" in start.output
    assert "unknown extra(s)" not in start.output
    assert "ERROR: entrypoint aborted" in start.output
    assert start.pip_install_timeouts == [], start.output
    assert not entrypoint_container.stamp.exists()


def test_installs_the_exact_artifacts_the_resolution_chose(entrypoint_container: Container) -> None:
    entrypoint_container.publish_after_resolution(
        WheelSpec("extradep", EXTRADEP_VERSION, build_tag="1", module_version="rebuilt after resolution")
    )

    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert start.imports["extradep"]["version"] == EXTRADEP_VERSION, start.imports
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports


def test_transient_pip_failure_retries_then_succeeds(entrypoint_container: Container) -> None:
    start = entrypoint_container.start("code", fail_first=MAX_ATTEMPTS - 1)

    assert start.returncode == 0, start.output
    assert f"attempt {MAX_ATTEMPTS - 1}/{MAX_ATTEMPTS} failed" in start.output
    assert f"attempt {MAX_ATTEMPTS}/{MAX_ATTEMPTS} failed" not in start.output
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    assert entrypoint_container.stamp.is_file()


def test_attempt_that_failed_after_writing_is_retried_without_leftovers(entrypoint_container: Container) -> None:
    start = entrypoint_container.start("code", fail_after_write=1)

    assert start.returncode == 0, start.output
    assert f"attempt 1/{MAX_ATTEMPTS} failed" in start.output
    assert loaded_from(start, "extradep", entrypoint_container.volume_root), start.imports
    assert entrypoint_container.volume_entries() == sorted(
        [STAMP_NAME, "extradep", f"extradep-{EXTRADEP_VERSION}.dist-info"]
    )


def test_gives_up_after_max_attempts_without_writing_stamp(entrypoint_container: Container) -> None:
    start = entrypoint_container.start("code", fail_first=FAIL_EVERY_ATTEMPT)

    assert start.returncode != 0, "entrypoint must fail once retries are exhausted"
    assert len(start.pip_install_timeouts) == MAX_ATTEMPTS, start.output
    assert "ERROR: entrypoint aborted" in start.output
    assert not entrypoint_container.stamp.exists(), "stamp must not be written when the install never succeeds"


def test_failed_reinstall_keeps_the_previous_volume_for_a_rollback(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0
    previous_entries = entrypoint_container.volume_entries()
    previous_stamp = entrypoint_container.stamp.read_text()

    entrypoint_container.bake(f"basedep=={BASEDEP_NEW}")  # the new image's first start has no network
    failed = entrypoint_container.start("code", fail_first=FAIL_EVERY_ATTEMPT)

    assert failed.returncode != 0, failed.output
    assert entrypoint_container.volume_entries() == previous_entries
    assert entrypoint_container.stamp.read_text() == previous_stamp

    entrypoint_container.bake(f"basedep=={BASEDEP_OLD}")  # roll back to the previous image
    rollback = entrypoint_container.start("code")

    assert rollback.returncode == 0, rollback.output
    assert rollback.pip_install_timeouts == [], "the kept volume must still match the previous image"
    assert loaded_from(rollback, "extradep", entrypoint_container.volume_root), rollback.imports


def test_attempts_that_all_fail_after_writing_leave_the_previous_volume_intact(entrypoint_container: Container) -> None:
    assert entrypoint_container.start("code").returncode == 0
    previous_entries = entrypoint_container.volume_entries()
    previous_stamp = entrypoint_container.stamp.read_text()

    failed = entrypoint_container.start("code,graph", fail_after_write=FAIL_EVERY_ATTEMPT)

    assert failed.returncode != 0, failed.output
    assert entrypoint_container.volume_entries() == previous_entries, "the failed attempts must leave nothing behind"
    assert entrypoint_container.stamp.read_text() == previous_stamp


def test_pip_default_timeout_raised_above_pip_default(entrypoint_container: Container) -> None:
    start = entrypoint_container.start("code")

    assert start.returncode == 0, start.output
    assert start.pip_install_timeouts, start.output
    for observed_timeout_s in start.pip_install_timeouts:
        assert observed_timeout_s.isdigit() and int(observed_timeout_s) > PIP_DEFAULT_TIMEOUT_S, (
            f"PIP_DEFAULT_TIMEOUT={observed_timeout_s!r} must be raised above pip's tight {PIP_DEFAULT_TIMEOUT_S}s default"
        )

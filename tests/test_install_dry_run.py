"""TDD tests for --dry-run correctness across all install branches (C14 Tasks 1.1, 1.2, 1.3).

Covers:
- Branch B (fresh install) dry-run: no config written, no .bak created,
  self.cfg reflects selected profile, [DRY RUN] prefix printed, exit code 0.
- Branch C (idempotent reinstall) dry-run: .bak not modified, config unchanged,
  [DRY RUN] prefix printed.
- Task 1.3: _download_fasttext_model and _prewarm_models not called in dry-run;
  _execute_force_reinstall .bak not created in dry-run.
"""
from __future__ import annotations

import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import DryRunInstaller, _execute_force_reinstall, create_installer
from archon_search.platform.types import GpuType
from archon_search.profiles import get_profile

pytestmark = pytest.mark.xdist_group("install")

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


@contextmanager
def _patched_install(
    config_path: Path,
    legacy_path: Path,
    *,
    skip_remove_legacy_service: bool = False,
    extra_patches: dict[str, dict | None] | None = None,
):
    """Yield the common patch set shared by every dry-run install test.

    Patches ``get_default_config_path``/``_legacy_service_path`` to the given
    paths, plus the standard no-op patches every test in this module relies
    on (``_remove_legacy_service``, ``_prewarm_models``, ``_check_disk_space``,
    and the ``SearchInstaller`` instance methods that touch GPU detection,
    provider config, and the service lifecycle).

    Keys in the returned dict are the exact target strings passed to
    ``patch``/``patch.object`` — e.g. ``"write_service_file"`` for
    ``patch.object(DryRunInstaller, "write_service_file")``, or
    ``"archon_search.install._prewarm_models"`` for
    ``patch("archon_search.install._prewarm_models")`` — so callers can grab
    a specific mock for assertions without re-patching it.

    ``skip_remove_legacy_service=True`` leaves ``_remove_legacy_service``
    unpatched — used by the one test that exercises the real cleanup.

    ``extra_patches`` maps additional module-level patch targets (full
    dotted ``archon_search.install.*`` strings) to a kwargs dict (or
    ``None`` for no kwargs), each patched via ``patch(target, **kwargs)``.
    """
    with ExitStack() as stack:
        mocks: dict[str, object] = {}
        mocks["archon_search.install.get_default_config_path"] = stack.enter_context(
            patch("archon_search.install.get_default_config_path", return_value=config_path)
        )
        mocks["archon_search.install._legacy_service_path"] = stack.enter_context(
            patch("archon_search.install._legacy_service_path", return_value=legacy_path)
        )
        if not skip_remove_legacy_service:
            mocks["archon_search.install._remove_legacy_service"] = stack.enter_context(
                patch("archon_search.install._remove_legacy_service")
            )
        mocks["archon_search.install._prewarm_models"] = stack.enter_context(
            patch("archon_search.install._prewarm_models")
        )
        mocks["archon_search.install._check_disk_space"] = stack.enter_context(
            patch("archon_search.install._check_disk_space")
        )
        mocks["detect_gpu"] = stack.enter_context(
            patch.object(DryRunInstaller, "detect_gpu", return_value=GpuType.NONE)
        )
        mocks["validate_providers"] = stack.enter_context(
            patch.object(DryRunInstaller, "validate_providers", return_value=False)
        )
        mocks["configure_providers"] = stack.enter_context(
            patch.object(DryRunInstaller, "configure_providers")
        )
        mocks["write_service_file"] = stack.enter_context(
            patch.object(DryRunInstaller, "write_service_file")
        )
        mocks["load_service"] = stack.enter_context(
            patch.object(DryRunInstaller, "load_service", return_value=0)
        )
        mocks["_wait_for_service"] = stack.enter_context(
            patch.object(DryRunInstaller, "_wait_for_service", return_value=True)
        )
        mocks["_is_service_running"] = stack.enter_context(
            patch.object(DryRunInstaller, "_is_service_running", return_value=False)
        )
        for target, kwargs in (extra_patches or {}).items():
            kwargs = kwargs or {}
            mocks[target] = stack.enter_context(patch(target, **kwargs))
        yield mocks


def _run_dry_run_fresh(tmp_path: Path, profile: str = "balanced", **run_kwargs):
    """Run wizard with --dry-run on a fresh install (no pre-existing config).

    Returns (installer, rc, config_path).
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"  # does NOT exist

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(config_path, fake_legacy):
        rc = installer.run(
            non_interactive=True,
            profile=profile,
            skip_preload=True,
            **run_kwargs,
        )

    return installer, rc, config_path


# ---------------------------------------------------------------------------
# Task 1.1 — Branch B (fresh install) dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_branch_b_no_config_written(tmp_path: Path) -> None:
    """--dry-run on fresh install must NOT create the config file."""
    _, rc, config_path = _run_dry_run_fresh(tmp_path)
    assert rc == 0
    assert not config_path.exists(), "config file must NOT be created in dry-run mode"


def test_dry_run_branch_b_no_bak_written(tmp_path: Path) -> None:
    """--dry-run on fresh install must NOT create a .toml.bak file."""
    _, rc, config_path = _run_dry_run_fresh(tmp_path)
    bak = config_path.with_suffix(".toml.bak")
    assert not bak.exists(), ".toml.bak must NOT be created in dry-run mode"


def test_dry_run_branch_b_cfg_reflects_profile(tmp_path: Path) -> None:
    """After dry-run, installer.cfg must reflect the selected profile, not stale defaults."""
    from archon_search.profiles import get_profile

    installer, rc, _ = _run_dry_run_fresh(tmp_path, profile="balanced")
    assert rc == 0
    expected_model = get_profile("balanced", False).embedder
    assert installer.cfg.embedding_model == expected_model, (
        f"Expected embedding_model={expected_model!r}, got {installer.cfg.embedding_model!r}"
    )


def test_dry_run_branch_b_cfg_not_stale_defaults(tmp_path: Path) -> None:
    """installer.cfg.embedding_model must NOT be the SearchConfig() default after balanced dry-run."""
    from archon_search.config import SearchConfig
    from archon_search.profiles import get_profile

    installer, rc, _ = _run_dry_run_fresh(tmp_path, profile="balanced")
    assert rc == 0
    stale_default = SearchConfig().embedding_model
    expected_model = get_profile("balanced", False).embedder
    # These differ — confirm the test is meaningful
    assert expected_model != stale_default, "Test assumption violated: profiles must differ from default"
    assert installer.cfg.embedding_model != stale_default, (
        "cfg must reflect the selected profile, not stale SearchConfig defaults"
    )


def test_dry_run_branch_b_prints_dry_run_prefix(tmp_path: Path, capsys) -> None:
    """--dry-run must print [DRY RUN] prefix so ops users see what would happen."""
    _run_dry_run_fresh(tmp_path)
    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out, "Expected [DRY RUN] in stdout for dry-run fresh install"


def test_dry_run_branch_b_exits_zero(tmp_path: Path) -> None:
    """--dry-run on a clean fresh install must return exit code 0."""
    _, rc, _ = _run_dry_run_fresh(tmp_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# Task 1.2 — Branch C (idempotent reinstall) dry-run tests
# ---------------------------------------------------------------------------


def _write_idempotent_config(tmp_path: Path, profile: str = "balanced") -> Path:
    """Create a valid config for a given profile so Branch C is triggered."""
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml(profile, False))
    return config_path


def _run_dry_run_idempotent(tmp_path: Path, profile: str = "balanced", **run_kwargs):
    """Run wizard with --dry-run on an existing config (Branch C path).

    Returns (installer, rc, config_path).
    """
    config_path = _write_idempotent_config(tmp_path, profile)
    fake_legacy = tmp_path / "fake.plist"  # does NOT exist

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(config_path, fake_legacy):
        rc = installer.run(
            non_interactive=True,
            profile=profile,
            skip_preload=True,
            **run_kwargs,
        )

    return installer, rc, config_path


def test_dry_run_branch_c_no_bak_overwrite(tmp_path: Path) -> None:
    """--dry-run on idempotent reinstall must NOT modify the .toml.bak file."""
    config_path = _write_idempotent_config(tmp_path)
    # Pre-create a .bak file so we can verify it wasn't touched
    bak_path = config_path.with_suffix(".toml.bak")
    bak_original_content = "# original bak content"
    bak_path.write_text(bak_original_content)
    original_mtime = bak_path.stat().st_mtime

    _, rc, _ = _run_dry_run_idempotent(tmp_path)
    assert rc == 0
    assert bak_path.stat().st_mtime == original_mtime, (
        ".toml.bak modification time must be unchanged in dry-run mode"
    )
    assert bak_path.read_text() == bak_original_content, (
        ".toml.bak content must be unchanged in dry-run mode"
    )


def test_dry_run_branch_c_config_unchanged(tmp_path: Path) -> None:
    """--dry-run on idempotent reinstall must NOT modify the config file."""
    config_path = _write_idempotent_config(tmp_path)
    original_content = config_path.read_text()

    _, rc, _ = _run_dry_run_idempotent(tmp_path)
    assert rc == 0
    assert config_path.read_text() == original_content, (
        "config file content must be unchanged after dry-run idempotent install"
    )


def test_dry_run_branch_c_prints_dry_run_prefix(tmp_path: Path, capsys) -> None:
    """--dry-run on idempotent reinstall must print [DRY RUN] prefix."""
    _write_idempotent_config(tmp_path)
    _run_dry_run_idempotent(tmp_path)
    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out, (
        "Expected [DRY RUN] in stdout for dry-run idempotent install"
    )


# ---------------------------------------------------------------------------
# Task 1.3 — fasttext download, prewarm, force-reinstall .bak dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_no_fasttext_download(tmp_path: Path) -> None:
    """With --dry-run and multilingual, _download_fasttext_model must NOT be called."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(
        config_path,
        fake_legacy,
        extra_patches={
            "archon_search.install._download_fasttext_model": None,
            "archon_search.install._prompt_fasttext_license": None,
        },
    ) as mocks:
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            multilingual=True,
            skip_preload=False,
            accept_fasttext_license=True,
            accept_jina_license=True,
        )

    assert rc == 0
    mock_dl = mocks["archon_search.install._download_fasttext_model"]
    mock_dl.assert_not_called()


def test_dry_run_no_prewarm(tmp_path: Path) -> None:
    """With --dry-run, _prewarm_models must NOT be called."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(config_path, fake_legacy) as mocks:
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            skip_preload=False,
        )

    assert rc == 0
    mocks["archon_search.install._prewarm_models"].assert_not_called()


def test_dry_run_force_no_bak(tmp_path: Path) -> None:
    """With --dry-run + --force + --delete-db, _execute_force_reinstall must NOT create .toml.bak."""
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("balanced", False))
    bak_path = config_path.with_suffix(".toml.bak")
    fake_legacy = tmp_path / "fake.plist"

    assert not bak_path.exists(), "precondition: no .bak before test"

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(
        config_path,
        fake_legacy,
        extra_patches={
            "archon_search.install.get_search_service": {"return_value": MagicMock()},
        },
    ):
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            force=True,
            delete_db=True,
            skip_preload=True,
        )

    assert rc == 0
    assert not bak_path.exists(), ".toml.bak must NOT be created during force-reinstall dry-run"


def test_dry_run_force_no_service_stop(tmp_path: Path) -> None:
    """With --dry-run + --force + --delete-db, service stop() must NOT be called (regression guard)."""
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("balanced", False))
    fake_legacy = tmp_path / "fake.plist"

    installer = create_installer(config_file=str(config_path), dry_run=True)
    mock_service = MagicMock()

    with _patched_install(
        config_path,
        fake_legacy,
        extra_patches={
            "archon_search.install.get_search_service": {"return_value": mock_service},
        },
    ):
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            force=True,
            delete_db=True,
            skip_preload=True,
        )

    assert rc == 0
    mock_service.stop.assert_not_called()


def test_dry_run_does_not_register_or_start_service(tmp_path: Path, monkeypatch) -> None:
    """S37: `wizard --profile minimal --non-interactive --skip-preload --dry-run`
    must NOT register the service (write_service_file) nor start the server
    (load_service). Step 15 must be gated on dry_run — a dry-run only describes
    what it would do, it never touches the launchd/systemd service.
    """
    # Isolate the data dir so the run never touches the real ~/.archon-search.
    data_dir = tmp_path / "data"  # does NOT exist
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))

    # In production config_path.parent IS the data dir (get_default_config_path
    # -> ~/.archon-search/archon-search.toml), so the config MUST live inside
    # data_dir here — otherwise the fresh-install branch's parent.mkdir would
    # create tmp_path (already present) and the leak-guard below could not see it.
    config_path = data_dir / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"  # does NOT exist

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(
        config_path,
        fake_legacy,
        extra_patches={
            "archon_search.install.get_search_service": {"return_value": MagicMock()},
        },
    ) as mocks:
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    mocks["write_service_file"].assert_not_called()
    mocks["load_service"].assert_not_called()
    # A dry-run must not materialise the data dir: if ~/.archon-search/
    # (here the isolated ARCHON_SEARCH_DATA_DIR) did not exist before, it must
    # still not exist after.
    assert not data_dir.exists(), "dry-run must not create the data dir"


def test_dry_run_does_not_remove_legacy_service(tmp_path: Path, capsys) -> None:
    """--dry-run must NOT stop/delete the legacy launchd plist (regression guard).

    Step 0 of ``SearchInstaller.run`` performs legacy-service cleanup. That
    cleanup (``_remove_legacy_service``) runs ``launchctl unload`` and unlinks
    the plist — a destructive side effect that a dry-run must never perform.
    A real, existing legacy plist is used here (not a stubbed no-op) so the
    actual delete is exercised.
    """
    # A real legacy plist that exists on disk — the destructive path only runs
    # when ``legacy.exists()`` is true.
    legacy_plist = tmp_path / "com.archon.search.plist"
    legacy_plist.write_text("<plist>legacy</plist>")

    config_path = tmp_path / "archon-search.toml"  # fresh install (does NOT exist)

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(
        config_path,
        legacy_plist,
        # NOTE: _remove_legacy_service is deliberately NOT patched — we exercise
        # the real cleanup. subprocess is patched so no real launchctl/systemctl
        # runs, isolating the filesystem side effect (the unlink) under test.
        skip_remove_legacy_service=True,
        extra_patches={"archon_search.install.subprocess.run": None},
    ) as mocks:
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            skip_preload=True,
        )

    assert rc == 0
    assert legacy_plist.exists(), (
        "dry-run must NOT delete the legacy launchd plist"
    )
    mocks["archon_search.install.subprocess.run"].assert_not_called()
    captured = capsys.readouterr()
    assert f"[DRY RUN] Would remove legacy service file: {legacy_plist}" in captured.out, (
        "dry-run must print the exact [DRY RUN] wording for the legacy service file"
    )


@pytest.mark.parametrize(
    "platform_value,expected_cmd",
    [
        ("darwin", "launchctl"),
        ("linux", "systemctl"),
    ],
)
def test_remove_legacy_service_real_removes_plist(
    tmp_path: Path, monkeypatch, capsys, platform_value: str, expected_cmd: str
) -> None:
    """The REAL (non-dry-run) `_remove_legacy_service` must unlink the plist
    and print the removal message, on both the darwin (launchctl) and linux
    (systemctl) branches (C1-I-20/23 regression guard).

    Exercises the actual function body — the destructive path that Step 0
    only reaches when ``self.dry_run`` is False — proving the guard added in
    this bugfix doesn't leave the real removal logic uncovered.
    """
    from archon_search.install import _remove_legacy_service

    plist = tmp_path / "com.archon.search.plist"
    plist.write_text("<plist>legacy</plist>")

    monkeypatch.setattr(sys, "platform", platform_value)

    with patch("archon_search.install.subprocess.run") as mock_run:
        _remove_legacy_service(plist)

    assert mock_run.called, f"expected subprocess.run to be invoked on platform={platform_value!r}"
    assert mock_run.call_args_list[0].args[0][0] == expected_cmd
    assert not plist.exists(), "real _remove_legacy_service must unlink the legacy plist"

    captured = capsys.readouterr()
    assert f"Removed legacy service file: {plist}" in captured.out


@pytest.mark.parametrize("scenario", ["fresh", "idempotent", "force"])
def test_dry_run_all_three_branches_no_files(tmp_path: Path, scenario: str, monkeypatch) -> None:
    """--dry-run must leave the filesystem state unchanged for all three install branches."""
    from archon_search.install import _profile_toml

    # Isolate the data dir under tmp_path (else get_data_dir() resolves to the
    # developer's real ~/.archon-search — polluting it and hiding data-dir leaks
    # from the tmp_path-scoped rglob below). Mirror production where the config
    # lives INSIDE the data dir, so the fresh-branch parent.mkdir would surface.
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    config_path = data_dir / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    if scenario in ("idempotent", "force"):
        data_dir.mkdir()
        config_path.write_text(_profile_toml("balanced", False))

    # Capture filesystem state before run. A set-diff catches new files but not
    # an in-place config rewrite (same path), so also snapshot config content —
    # the force branch is the one that rewrites config, and only in non-dry-run.
    files_before = set(tmp_path.rglob("*"))
    config_before = config_path.read_text() if config_path.exists() else None

    installer = create_installer(config_file=str(config_path), dry_run=True)
    run_kwargs: dict = {
        "non_interactive": True,
        "profile": "balanced",
        "skip_preload": True,
    }
    if scenario == "force":
        run_kwargs["force"] = True
        run_kwargs["delete_db"] = True

    with _patched_install(
        config_path,
        fake_legacy,
        extra_patches={
            "archon_search.install.get_search_service": {"return_value": MagicMock()},
        },
    ):
        rc = installer.run(**run_kwargs)

    assert rc == 0

    # Filesystem state must be unchanged (no new files created)
    files_after = set(tmp_path.rglob("*"))
    new_files = files_after - files_before
    assert not new_files, (
        f"Dry-run ({scenario} branch) must not create new files; found: {new_files}"
    )
    config_after = config_path.read_text() if config_path.exists() else None
    assert config_after == config_before, (
        f"Dry-run ({scenario} branch) must not modify config content in place"
    )


# ---------------------------------------------------------------------------
# db_path override — dry-run must preview, never mutate, and still surface
# a writability failure a real run would hit (regression: dry-run silently
# skipped the os.access(W_OK) check and gave a false all-clear).
# ---------------------------------------------------------------------------


def test_dry_run_db_path_not_created(tmp_path: Path) -> None:
    """--dry-run with a db_path override must NOT create the directory."""
    db_dir = tmp_path / "custom_db"
    assert not db_dir.exists()

    _, rc, _ = _run_dry_run_fresh(tmp_path, db_path=str(db_dir))

    assert rc == 0
    assert not db_dir.exists(), "db_path directory must NOT be created in dry-run mode"


def test_dry_run_db_path_banner_printed(tmp_path: Path, capsys) -> None:
    """--dry-run with a db_path override announces what it would do."""
    db_dir = tmp_path / "custom_db"

    _, rc, _ = _run_dry_run_fresh(tmp_path, db_path=str(db_dir))

    assert rc == 0
    assert "[DRY RUN] Would create db_path directory" in capsys.readouterr().out


def test_dry_run_db_path_not_writable_previews_failure(tmp_path: Path) -> None:
    """--dry-run must fail on an unwritable db_path, mirroring the real run."""
    db_dir = tmp_path / "custom_db"

    with patch("archon_search.install.os.access", return_value=False):
        _, rc, _ = _run_dry_run_fresh(tmp_path, db_path=str(db_dir))

    assert rc == 1, "dry-run must surface the writability failure a real run would hit"
    assert not db_dir.exists(), "db_path directory must NOT be created in dry-run mode"


# ---------------------------------------------------------------------------
# BACKSTOP: the guarantee the class split alone cannot enforce.
# A full dry-run must never invoke a system-changing seam — anywhere in run(),
# including inline steps not routed through the abstract methods. If a future
# edit lets a mutation reach dry-run, exactly one of these assertions fails.
# ---------------------------------------------------------------------------

_DANGEROUS_SEAMS = (
    "archon_search.install._download_fasttext_model",
    "archon_search.install.atomic_write_bytes",
    "archon_search.install.subprocess.run",
    "archon_search.install.shutil.copy2",
    "archon_search.install.rmtree",
    "archon_search.install.os.chmod",
)


def test_dry_run_backstop_touches_nothing_real(tmp_path: Path) -> None:
    """A feature-rich dry-run must not call any real filesystem/subprocess seam."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    installer = create_installer(config_file=str(config_path), dry_run=True)

    with _patched_install(config_path, fake_legacy) as mocks, ExitStack() as stack:
        seams = {name: stack.enter_context(patch(name)) for name in _DANGEROUS_SEAMS}
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            multilingual=True,
            accept_fasttext_license=True,
            accept_jina_license=True,
            skip_preload=False,
            server_key="ab" * 32,
            db_path=str(tmp_path / "custom_db"),
            enable_hyde=True,
        )

    assert rc == 0
    for name, mock in seams.items():
        assert not mock.called, f"dry-run called a real seam: {name}"
    mocks["archon_search.install._prewarm_models"].assert_not_called()
    mocks["archon_search.install._remove_legacy_service"].assert_not_called()

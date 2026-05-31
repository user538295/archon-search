"""Tests for release.sh pre-flight checks, specifically the git-cliff version gate."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

RELEASE_SH = Path(__file__).parent.parent / "release.sh"


def _make_stub_git_cliff(bin_dir: Path, version_output: str) -> None:
    """Write a stub git-cliff script that prints version_output and exits 0."""
    stub = bin_dir / "git-cliff"
    stub.write_text(f"#!/usr/bin/env bash\necho '{version_output}'\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup_repo(tmp_path: Path) -> Path:
    """
    Set up a minimal git repo that passes all pre-flight checks preceding
    check_git_cliff:
      - on branch main
      - clean working tree
      - local HEAD == origin/main HEAD

    Copies release.sh into the worker repo so the script's BASH_SOURCE[0]-based
    REPO_ROOT resolves to the temp repo, not the real project directory.

    Returns the path to the worker repo.
    """
    bare = tmp_path / "bare.git"
    worker = tmp_path / "worker"

    # Create bare remote
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    # Create worker repo
    subprocess.run(["git", "init", "-b", "main", str(worker)], check=True, capture_output=True)

    # Configure identity so commits work
    for key, val in [("user.email", "test@test.com"), ("user.name", "Test")]:
        subprocess.run(["git", "config", key, val], check=True, capture_output=True, cwd=worker)

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        check=True,
        capture_output=True,
        cwd=worker,
    )

    # Copy release.sh into the worker repo so BASH_SOURCE[0] resolves there
    dest_release_sh = worker / "release.sh"
    shutil.copy2(RELEASE_SH, dest_release_sh)

    # Initial commit (includes release.sh so tree is clean after commit)
    readme = worker / "README.md"
    readme.write_text("hello\n")
    subprocess.run(["git", "add", "README.md", "release.sh"], check=True, capture_output=True, cwd=worker)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        check=True,
        capture_output=True,
        cwd=worker,
    )

    # Push so origin/main tracks local main
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", "main"],
        check=True,
        capture_output=True,
        cwd=worker,
    )

    return worker


def _run_release_sh(
    args: list[str],
    env_overrides: dict | None = None,
    repo_path: Path | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    # Use the release.sh copy inside the repo so BASH_SOURCE[0] resolves there
    script = str(repo_path / "release.sh") if repo_path else str(RELEASE_SH)
    return subprocess.run(
        ["bash", script] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_path) if repo_path else None,
    )


@pytest.fixture()
def valid_repo(tmp_path):
    return _setup_repo(tmp_path)


def test_missing_git_cliff_exits_with_error(valid_repo, tmp_path):
    """release.sh must fail with a clear message when git-cliff is not on PATH."""
    # Build a PATH that has git but no git-cliff by creating a stub bin dir
    # with everything except git-cliff
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()

    # Strip git-cliff from PATH by using a clean PATH that definitely has no git-cliff
    original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Keep standard system paths but exclude any directory containing git-cliff
    filtered_path = ":".join(
        p for p in original_path.split(":")
        if not (Path(p) / "git-cliff").exists()
    )
    # Prepend our empty bin so it's first
    no_cliff_path = f"{empty_bin}:{filtered_path}"

    result = _run_release_sh(
        ["--dry-run"],
        env_overrides={"PATH": no_cliff_path},
        repo_path=valid_repo,
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit code, got 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "git-cliff not found" in result.stderr, (
        f"Expected 'git-cliff not found' in stderr.\nstderr: {result.stderr}"
    )


def test_old_git_cliff_version_exits_with_error(valid_repo, tmp_path):
    """release.sh must fail when git-cliff version is below 2.4."""
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _make_stub_git_cliff(stub_bin, "git-cliff 2.3.0")

    original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    new_path = f"{stub_bin}:{original_path}"

    result = _run_release_sh(
        ["--dry-run"],
        env_overrides={"PATH": new_path},
        repo_path=valid_repo,
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit code, got 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert ">= 2.4" in result.stderr, (
        f"Expected '>= 2.4' in stderr.\nstderr: {result.stderr}"
    )


def test_valid_git_cliff_version_passes_preflight(valid_repo, tmp_path):
    """release.sh must not emit a version-check error when git-cliff >= 2.4 is present."""
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _make_stub_git_cliff(stub_bin, "git-cliff 2.4.0")

    original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    new_path = f"{stub_bin}:{original_path}"

    result = _run_release_sh(
        ["--dry-run"],
        env_overrides={"PATH": new_path},
        repo_path=valid_repo,
    )

    # The version check must not have triggered — no version-related error in stderr
    assert "git-cliff not found" not in result.stderr, (
        f"Unexpected 'git-cliff not found' in stderr.\nstderr: {result.stderr}"
    )
    assert ">= 2.4" not in result.stderr, (
        f"Unexpected '>= 2.4' version error in stderr.\nstderr: {result.stderr}"
    )
    # Script must reach past pre-flight and print the dry-run banner
    assert "[dry-run]" in result.stdout, (
        f"Expected '[dry-run]' in stdout — script did not reach post-preflight output.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

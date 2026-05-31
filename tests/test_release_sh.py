"""Tests for release.sh pre-flight checks, specifically the git-cliff version gate."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

RELEASE_SH = Path(__file__).parent.parent / "release.sh"


CHANGELOG_STUB = """\
# Changelog

All notable changes to archon-search are recorded here.
Prior release history is available via `git log`.
"""

SAMPLE_NOTES = """\
## [26.5.2] - 2026-05-31

### Features
- feat(core): add new search capability
"""


def _make_stub_git_cliff(bin_dir: Path, version_output: str) -> None:
    """Write a stub git-cliff script that prints version_output and exits 0."""
    stub = bin_dir / "git-cliff"
    stub.write_text(f"#!/usr/bin/env bash\necho '{version_output}'\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_git_cliff_with_notes(
    bin_dir: Path, version_output: str, notes: str, exit_code: int = 0
) -> None:
    """
    Write a stub git-cliff that:
      - responds to --version by printing version_output and exiting 0
      - responds to --unreleased --tag by printing notes and exiting exit_code
    """
    stub = bin_dir / "git-cliff"
    # Use printf %s to avoid interpretation of backslashes/special chars in notes
    # We write the notes into a file to avoid quoting issues in the script body.
    notes_file = bin_dir / "cliff_notes.txt"
    notes_file.write_text(notes)
    script = f"""\
#!/usr/bin/env bash
if echo "$*" | grep -q -- '--version'; then
    echo '{version_output}'
    exit 0
fi
# --unreleased --tag ... invocation
cat '{notes_file}'
exit {exit_code}
"""
    stub.write_text(script)
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

    # Initial commit (includes release.sh and CHANGELOG.md so tree is clean after commit)
    readme = worker / "README.md"
    readme.write_text("hello\n")
    changelog = worker / "CHANGELOG.md"
    changelog.write_text(CHANGELOG_STUB)
    subprocess.run(["git", "add", "README.md", "release.sh", "CHANGELOG.md"], check=True, capture_output=True, cwd=worker)
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


class TestProvisionalTag:
    def test_provisional_tag_is_count_plus_one(self, tmp_path):
        """--dry-run output must contain a tag with count+1, not count."""
        import re

        worker = _setup_repo(tmp_path)

        # Determine N (current commit count) before running the script
        result_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            cwd=worker,
        )
        n = int(result_count.stdout.strip())

        stub_bin = tmp_path / "stub_bin"
        stub_bin.mkdir()
        _make_stub_git_cliff(stub_bin, "git-cliff 2.4.0")

        original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        new_path = f"{stub_bin}:{original_path}"

        result = _run_release_sh(
            ["--dry-run"],
            env_overrides={"PATH": new_path},
            repo_path=worker,
        )

        assert result.returncode == 0, (
            f"Expected exit 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Extract the tag from the script's own output to avoid midnight-UTC race.
        # The script prints "  tag    : YY.M.N" in the banner.
        tag_match = re.search(r"tag\s*:\s*(\S+)", result.stdout)
        assert tag_match, (
            f"Could not find 'tag : ...' in stdout.\nstdout: {result.stdout}"
        )
        actual_tag = tag_match.group(1)

        # The tag's numeric suffix must be N+1 (count+1 formula).
        suffix = int(actual_tag.rsplit(".", 1)[-1])
        assert suffix == n + 1, (
            f"Expected tag suffix {n + 1} (count+1), got {suffix} (tag={actual_tag}).\n"
            f"stdout: {result.stdout}"
        )

    def test_count_mismatch_bails(self, tmp_path):
        """A forced count mismatch via EXPECTED_COUNT_OVERRIDE must cause a bail."""
        worker = _setup_repo(tmp_path)

        stub_bin = tmp_path / "stub_bin"
        stub_bin.mkdir()
        _make_stub_git_cliff(stub_bin, "git-cliff 2.4.0")

        original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        new_path = f"{stub_bin}:{original_path}"

        result = _run_release_sh(
            ["-y"],
            env_overrides={
                "PATH": new_path,
                "EXPECTED_COUNT_OVERRIDE": "9999",
                "RELEASE_SH_TEST_MODE": "1",
            },
            repo_path=worker,
        )

        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Unexpected commit count" in result.stderr, (
            f"Expected 'Unexpected commit count' in stderr.\nstderr: {result.stderr}"
        )


def test_override_without_test_mode_bails(tmp_path):
    """Setting EXPECTED_COUNT_OVERRIDE without RELEASE_SH_TEST_MODE must bail immediately."""
    worker = _setup_repo(tmp_path)

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _make_stub_git_cliff(stub_bin, "git-cliff 2.4.0")

    original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    new_path = f"{stub_bin}:{original_path}"

    result = _run_release_sh(
        ["--dry-run"],
        env_overrides={
            "PATH": new_path,
            "EXPECTED_COUNT_OVERRIDE": "5",
            # RELEASE_SH_TEST_MODE intentionally NOT set
        },
        repo_path=worker,
    )

    assert result.returncode != 0, (
        f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "unset it before running a real release" in result.stderr, (
        f"Expected guard message in stderr.\nstderr: {result.stderr}"
    )


def _make_cliff_path(tmp_path: Path, notes: str = SAMPLE_NOTES, exit_code: int = 0) -> str:
    """Create a stub bin dir with git-cliff 2.4.0 and given notes. Returns PATH string."""
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir(exist_ok=True)
    _make_stub_git_cliff_with_notes(stub_bin, "git-cliff 2.4.0", notes, exit_code)
    original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return f"{stub_bin}:{original_path}"


class TestChangelogPrepend:
    """Tests for the CHANGELOG.md prepend step in release.sh (Task 2.3)."""

    def _run_full(
        self,
        tmp_path: Path,
        worker: Path,
        notes: str = SAMPLE_NOTES,
        exit_code: int = 0,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess:
        """Run release.sh -y in full (non-dry-run) mode with the stub cliff."""
        # Compute commit count so EXPECTED_COUNT_OVERRIDE = N+1 (after CHANGELOG commit)
        result_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=worker,
        )
        n = int(result_count.stdout.strip())
        # EXPECTED_COUNT = N+1 because the CHANGELOG.md commit brings count to N+1
        expected = n + 1

        new_path = _make_cliff_path(tmp_path, notes, exit_code)
        env = {
            "PATH": new_path,
            "RELEASE_SH_TEST_MODE": "1",
            "EXPECTED_COUNT_OVERRIDE": str(expected),
        }
        if extra_env:
            env.update(extra_env)
        return _run_release_sh(["-y"], env_overrides=env, repo_path=worker)

    def test_changelog_prepend_preserves_header(self, tmp_path):
        """After a successful release, the first line of CHANGELOG.md must be '# Changelog'."""
        worker = _setup_repo(tmp_path)
        result = self._run_full(tmp_path, worker)
        assert result.returncode == 0, (
            f"Expected exit 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        lines = (worker / "CHANGELOG.md").read_text().splitlines()
        assert lines[0] == "# Changelog", (
            f"First line is not '# Changelog': {lines[:5]}"
        )

    def test_changelog_prepend_adds_notes_after_header(self, tmp_path):
        """Notes must appear after '# Changelog' but before the original preamble text."""
        worker = _setup_repo(tmp_path)
        result = self._run_full(tmp_path, worker)
        assert result.returncode == 0, (
            f"Expected exit 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        content = (worker / "CHANGELOG.md").read_text()
        lines = content.splitlines()
        header_idx = lines.index("# Changelog")
        # Find where notes start (look for '## [')
        notes_idx = next(
            (i for i, l in enumerate(lines) if l.startswith("## [")), None
        )
        # Find where the original preamble text appears
        preamble_idx = next(
            (i for i, l in enumerate(lines) if "All notable changes" in l), None
        )
        assert notes_idx is not None, f"Notes not found in CHANGELOG.md:\n{content}"
        assert preamble_idx is not None, f"Preamble text not found in CHANGELOG.md:\n{content}"
        assert header_idx < notes_idx < preamble_idx, (
            f"Expected header({header_idx}) < notes({notes_idx}) < preamble({preamble_idx})\n{content}"
        )

    def test_commit_message_format(self, tmp_path):
        """The CHANGELOG commit must have the exact message 'chore(release): update CHANGELOG.md for TAG'."""
        import re
        worker = _setup_repo(tmp_path)
        result = self._run_full(tmp_path, worker)
        assert result.returncode == 0, (
            f"Expected exit 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        log = subprocess.run(
            ["git", "log", "--oneline", "-2"],
            capture_output=True, text=True, cwd=worker,
        )
        # The CHANGELOG commit is the most recent commit (before the tag)
        commit_msg = log.stdout.strip().splitlines()[0]
        assert re.search(r"chore\(release\): update CHANGELOG\.md for \d+\.\d+\.\d+", commit_msg), (
            f"Commit message doesn't match expected pattern: {commit_msg}"
        )

    def test_empty_notes_bails(self, tmp_path):
        """git-cliff producing empty output must cause bail with 'No conventional commits found'."""
        worker = _setup_repo(tmp_path)
        result = self._run_full(tmp_path, worker, notes="   \n  \n")
        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "No conventional commits found" in result.stderr, (
            f"Expected 'No conventional commits found' in stderr.\nstderr: {result.stderr}"
        )

    def test_git_cliff_execution_failure_bails(self, tmp_path):
        """git-cliff exiting with code 1 must cause bail with 'git-cliff failed'."""
        worker = _setup_repo(tmp_path)
        result = self._run_full(tmp_path, worker, notes=SAMPLE_NOTES, exit_code=1)
        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "git-cliff failed" in result.stderr, (
            f"Expected 'git-cliff failed' in stderr.\nstderr: {result.stderr}"
        )

    def test_missing_changelog_md_exits_with_error(self, tmp_path):
        """Absence of CHANGELOG.md must cause a clear bail (not a raw awk error)."""
        worker = _setup_repo(tmp_path)
        # Remove CHANGELOG.md and commit the deletion so the working tree is clean
        (worker / "CHANGELOG.md").unlink()
        subprocess.run(["git", "rm", "CHANGELOG.md"], check=True, capture_output=True, cwd=worker)
        subprocess.run(
            ["git", "commit", "-m", "remove changelog for test"],
            check=True, capture_output=True, cwd=worker,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            check=True, capture_output=True, cwd=worker,
        )

        result_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=worker,
        )
        n = int(result_count.stdout.strip())

        new_path = _make_cliff_path(tmp_path)
        result = _run_release_sh(
            ["-y"],
            env_overrides={
                "PATH": new_path,
                "RELEASE_SH_TEST_MODE": "1",
                "EXPECTED_COUNT_OVERRIDE": str(n + 1),
            },
            repo_path=worker,
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "CHANGELOG.md not found" in result.stderr, (
            f"Expected 'CHANGELOG.md not found' in stderr.\nstderr: {result.stderr}"
        )

    def test_malformed_changelog_header_exits_with_error(self, tmp_path):
        """A CHANGELOG.md without '# Changelog' as first line must bail with a clear message."""
        worker = _setup_repo(tmp_path)
        # Overwrite CHANGELOG.md with wrong header and commit + push so tree is clean
        (worker / "CHANGELOG.md").write_text("# CHANGELOG\n\nSome content.\n")
        subprocess.run(["git", "add", "CHANGELOG.md"], check=True, capture_output=True, cwd=worker)
        subprocess.run(
            ["git", "commit", "-m", "bad changelog header for test"],
            check=True, capture_output=True, cwd=worker,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            check=True, capture_output=True, cwd=worker,
        )

        result_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=worker,
        )
        n = int(result_count.stdout.strip())

        new_path = _make_cliff_path(tmp_path)
        result = _run_release_sh(
            ["-y"],
            env_overrides={
                "PATH": new_path,
                "RELEASE_SH_TEST_MODE": "1",
                "EXPECTED_COUNT_OVERRIDE": str(n + 1),
            },
            repo_path=worker,
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "missing the exact # Changelog header" in result.stderr, (
            f"Expected 'missing the exact # Changelog header' in stderr.\nstderr: {result.stderr}"
        )

    def test_dry_run_does_not_modify_changelog(self, tmp_path):
        """--dry-run must leave CHANGELOG.md completely untouched."""
        worker = _setup_repo(tmp_path)
        original = (worker / "CHANGELOG.md").read_text()

        stub_bin = tmp_path / "stub_bin"
        stub_bin.mkdir()
        _make_stub_git_cliff_with_notes(stub_bin, "git-cliff 2.4.0", SAMPLE_NOTES)
        original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        new_path = f"{stub_bin}:{original_path}"

        _run_release_sh(["--dry-run"], env_overrides={"PATH": new_path}, repo_path=worker)

        assert (worker / "CHANGELOG.md").read_text() == original, (
            "CHANGELOG.md was modified by --dry-run"
        )

    def test_changelog_prepend_with_existing_sections(self, tmp_path):
        """Prepend must insert new notes between header and existing sections."""
        worker = _setup_repo(tmp_path)

        existing_section = (
            "## [26.4.1] - 2026-04-15\n\n### Bug Fixes\n- fix: prior release bug\n"
        )
        # Pre-populate CHANGELOG.md with an existing release section
        prior_content = f"{CHANGELOG_STUB}\n{existing_section}"
        (worker / "CHANGELOG.md").write_text(prior_content)
        subprocess.run(["git", "add", "CHANGELOG.md"], check=True, capture_output=True, cwd=worker)
        subprocess.run(
            ["git", "commit", "-m", "add prior release section for test"],
            check=True, capture_output=True, cwd=worker,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            check=True, capture_output=True, cwd=worker,
        )

        result = self._run_full(tmp_path, worker)
        assert result.returncode == 0, (
            f"Expected exit 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        content = (worker / "CHANGELOG.md").read_text()
        lines = content.splitlines()
        header_idx = lines.index("# Changelog")
        new_notes_idx = next((i for i, l in enumerate(lines) if "26.5.2" in l), None)
        prior_idx = next((i for i, l in enumerate(lines) if "26.4.1" in l), None)
        preamble_idx = next((i for i, l in enumerate(lines) if "All notable changes" in l), None)

        assert new_notes_idx is not None, f"New notes section not found:\n{content}"
        assert prior_idx is not None, f"Prior section not found:\n{content}"
        assert header_idx < new_notes_idx < prior_idx, (
            f"New notes must appear between header and prior sections.\n"
            f"header={header_idx}, new={new_notes_idx}, prior={prior_idx}\n{content}"
        )
        # Preamble text must still be present somewhere (not lost)
        assert preamble_idx is not None, f"Preamble text was lost from CHANGELOG.md:\n{content}"


class TestDryRunOutput:
    """Tests for the updated --dry-run output (Task 2.4)."""

    def _run_dry(self, tmp_path: Path, worker: Path) -> subprocess.CompletedProcess:
        stub_bin = tmp_path / "stub_bin"
        stub_bin.mkdir(exist_ok=True)
        _make_stub_git_cliff_with_notes(stub_bin, "git-cliff 2.4.0", SAMPLE_NOTES)
        original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        new_path = f"{stub_bin}:{original_path}"
        return _run_release_sh(["--dry-run"], env_overrides={"PATH": new_path}, repo_path=worker)

    def test_dry_run_prints_provisional_tag(self, tmp_path):
        """--dry-run must print '[dry-run] provisional tag' in stdout."""
        worker = _setup_repo(tmp_path)
        result = self._run_dry(tmp_path, worker)
        assert "[dry-run] provisional tag" in result.stdout, (
            f"Expected '[dry-run] provisional tag' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_prints_notes(self, tmp_path):
        """--dry-run must print the cliff notes content in stdout."""
        worker = _setup_repo(tmp_path)
        result = self._run_dry(tmp_path, worker)
        assert "26.5.2" in result.stdout, (
            f"Expected SAMPLE_NOTES content ('26.5.2') in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_prints_curl_command(self, tmp_path):
        """--dry-run must print a curl command preview with api.github.com and releases."""
        worker = _setup_repo(tmp_path)
        result = self._run_dry(tmp_path, worker)
        assert "curl" in result.stdout, (
            f"Expected 'curl' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "api.github.com" in result.stdout, (
            f"Expected 'api.github.com' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "releases" in result.stdout, (
            f"Expected 'releases' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_makes_no_git_changes(self, tmp_path):
        """--dry-run must not create commits, tags, or modify CHANGELOG.md."""
        worker = _setup_repo(tmp_path)
        original_changelog = (worker / "CHANGELOG.md").read_text()
        count_before = int(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                capture_output=True, text=True, cwd=worker,
            ).stdout.strip()
        )

        self._run_dry(tmp_path, worker)

        count_after = int(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                capture_output=True, text=True, cwd=worker,
            ).stdout.strip()
        )
        tags = subprocess.run(
            ["git", "tag", "-l"],
            capture_output=True, text=True, cwd=worker,
        ).stdout.strip()

        assert count_after == count_before, (
            f"Git log count changed from {count_before} to {count_after}"
        )
        assert tags == "", f"Unexpected tags after --dry-run: {tags}"
        assert (worker / "CHANGELOG.md").read_text() == original_changelog, (
            "CHANGELOG.md was modified by --dry-run"
        )

    def test_dry_run_exits_zero(self, tmp_path):
        """--dry-run must exit with code 0."""
        worker = _setup_repo(tmp_path)
        result = self._run_dry(tmp_path, worker)
        assert result.returncode == 0, (
            f"Expected exit code 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_dry_run_ends_with_no_writes_trailer(self, tmp_path):
        """--dry-run stdout must end with the 'no writes, no pushes, no API calls.' trailer."""
        worker = _setup_repo(tmp_path)
        result = self._run_dry(tmp_path, worker)
        assert "[dry-run] no writes, no pushes, no API calls." in result.stdout, (
            f"Expected trailer line in stdout.\nstdout: {result.stdout}"
        )

    def test_dry_run_empty_notes_still_bails(self, tmp_path):
        """--dry-run must bail with 'No conventional commits found' when notes are empty."""
        worker = _setup_repo(tmp_path)
        stub_bin = tmp_path / "stub_bin"
        stub_bin.mkdir()
        _make_stub_git_cliff_with_notes(stub_bin, "git-cliff 2.4.0", "   \n  \n")
        original_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        new_path = f"{stub_bin}:{original_path}"

        result = _run_release_sh(
            ["--dry-run"],
            env_overrides={"PATH": new_path},
            repo_path=worker,
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "No conventional commits found" in result.stderr, (
            f"Expected 'No conventional commits found' in stderr.\nstderr: {result.stderr}"
        )

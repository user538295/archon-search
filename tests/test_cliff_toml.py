"""Tests for cliff.toml configuration validity and correctness."""

import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CLIFF_TOML = REPO_ROOT / "cliff.toml"


def _load_cliff_toml() -> dict:
    with open(CLIFF_TOML, "rb") as f:
        return tomllib.load(f)


def test_cliff_toml_is_valid_toml():
    """cliff.toml must exist and be parseable TOML."""
    config = _load_cliff_toml()
    assert isinstance(config, dict)


def test_required_keys_present():
    """Required top-level keys must be present."""
    config = _load_cliff_toml()
    assert "git" in config, "missing [git] section"
    assert "tag_pattern" in config["git"], "missing git.tag_pattern"
    assert "changelog" in config, "missing [changelog] section"
    assert "body" in config["changelog"], "missing changelog.body"
    assert "commit_parsers" in config["git"], "missing git.commit_parsers"


def test_chore_release_skip_filter_present():
    """commit_parsers must contain a skip entry for chore(release) commits at index 0,
    and the catch-all skip must be last."""
    config = _load_cliff_toml()
    parsers = config["git"]["commit_parsers"]

    # chore(release) must be first so it fires before the broader ^chore pattern
    first = parsers[0]
    assert first.get("message") == "^chore\\(release\\)" and first.get("skip") is True, (
        f"parsers[0] must be chore(release) skip, got {first!r}"
    )

    # catch-all must be last so it doesn't shadow anything
    last = parsers[-1]
    assert last.get("message") == ".*" and last.get("skip") is True, (
        f"parsers[-1] must be the catch-all skip '.*', got {last!r}"
    )


def test_tag_pattern_matches_calver():
    """tag_pattern must match CalVer without 'v' prefix and reject others."""
    config = _load_cliff_toml()
    pattern = config["git"]["tag_pattern"]

    # Must match three-segment CalVer
    assert re.fullmatch(pattern, "26.5.42"), f"pattern {pattern!r} should match '26.5.42'"

    # Must NOT match v-prefixed semver
    assert not re.fullmatch(pattern, "v1.2.3"), f"pattern {pattern!r} should not match 'v1.2.3'"

    # Must NOT match two-segment version
    assert not re.fullmatch(pattern, "26.5"), f"pattern {pattern!r} should not match '26.5'"

    # Must NOT match four-segment version
    assert not re.fullmatch(pattern, "1.2.3.4"), f"pattern {pattern!r} should not match '1.2.3.4'"


def test_body_template_heading_matches_awk_pattern():
    """The first non-empty, non-whitespace line of changelog.body must start with '## ['."""
    config = _load_cliff_toml()
    body = config["changelog"]["body"]
    first_content_line = next(
        (line for line in body.splitlines() if line.strip()),
        None,
    )
    assert first_content_line is not None, "changelog.body has no non-empty lines"
    assert first_content_line.strip().startswith("## ["), (
        f"First content line {first_content_line!r} does not start with '## ['"
    )


def _make_test_repo(tmpdir: Path, commits: list[str]) -> None:
    subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmpdir, check=True, capture_output=True)
    for msg in commits:
        subprocess.run(["git", "commit", "--allow-empty", "-m", msg], cwd=tmpdir, check=True, capture_output=True)


def _cliff_output(tmpdir: Path) -> str:
    result = subprocess.run(
        ["git-cliff", "--config", str(CLIFF_TOML), "--unreleased", "--tag", "26.5.1"],
        cwd=tmpdir, capture_output=True, text=True,
    )
    return result.stdout


def test_cliff_output_strips_scope():
    """Integration: git-cliff output should contain description but not 'feat(scope)' prefix."""
    if not shutil.which("git-cliff"):
        import pytest
        pytest.skip("git-cliff not found on PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _make_test_repo(tmp, ["feat(myscope): add feature"])
        stdout = _cliff_output(tmp)
        assert "add feature" in stdout.lower(), f"Expected 'add feature' in output:\n{stdout}"
        assert "feat(myscope)" not in stdout, (
            f"Expected scope to be stripped, but found 'feat(myscope)' in:\n{stdout}"
        )


def test_cliff_output_flat_list_no_section_headings():
    """Integration: output must be a flat bullet list — no ### section headings."""
    if not shutil.which("git-cliff"):
        import pytest
        pytest.skip("git-cliff not found on PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _make_test_repo(tmp, [
            "feat(A): add search endpoint",
            "fix(B): correct model name",
            "docs(C): update config docs",
            "refactor(D): simplify install flow",
        ])
        stdout = _cliff_output(tmp)
        assert "###" not in stdout, f"Section headings should not appear:\n{stdout}"
        for phrase in ["add search endpoint", "correct model name", "update config docs", "simplify install flow"]:
            assert f"- {phrase.capitalize()}" in stdout or f"- {phrase}" in stdout, (
                f"Expected bullet for {phrase!r}:\n{stdout}"
            )


def test_cliff_output_bold_title_from_feat():
    """Integration: a feat commit should produce a bold title line."""
    if not shutil.which("git-cliff"):
        import pytest
        pytest.skip("git-cliff not found on PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _make_test_repo(tmp, ["feat(X): add router endpoint", "fix(Y): patch timeout"])
        stdout = _cliff_output(tmp)
        assert "**Add router endpoint**" in stdout, (
            f"Expected bold title from feat commit:\n{stdout}"
        )


def test_cliff_output_no_bold_title_without_feat():
    """Integration: without feat commits, no bold title line should appear."""
    if not shutil.which("git-cliff"):
        import pytest
        pytest.skip("git-cliff not found on PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        _make_test_repo(tmp, ["fix(Y): patch timeout", "docs(Z): update readme"])
        stdout = _cliff_output(tmp)
        assert "**" not in stdout, f"No bold title expected without feat commit:\n{stdout}"

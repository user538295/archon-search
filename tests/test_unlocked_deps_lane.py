"""Behaviour of scripts/test-unlocked-deps.sh's own guards, with `uv` and the venv stubbed.

The lane's value rests on testing exactly the versions it installed. These tests drive the
real script against a fake `uv` (whose `pip freeze` reads a file) and a fake venv python
(whose `-m pytest` run can rewrite that file, as a test that runs a package install did),
so no network, resolution or real suite is involved.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test-unlocked-deps.sh"
LOCAL_BUILD = "archon-search @ file:///checkout"
FROZEN = f"{LOCAL_BUILD}\niniconfig==2.3.1\n"
PREFETCH_SCRIPT = "scripts/prefetch-token-counter.py"


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _lane(
    tmp_path: Path,
    *,
    freeze: str,
    pytest_rewrites_freeze_to: str | None = None,
    platform: tuple[str, str] | None = None,
    prefetch_failures: int = 0,
) -> dict:
    """A repo copy holding the script, a fake `uv` and a work dir; returns the run env.

    The fake `uv` appends each `pip install` argument line to ``tmp_path / "install_args.txt"``. The fake venv python
    appends every call's arguments to ``tmp_path / "python_calls.txt"`` and fails the first *prefetch_failures* runs of
    the token-counter prefetch script; a fake `sleep` keeps the retry backoff instant.
    *platform* is a ``(uname -s, uname -m)`` pair served by a fake `uname`.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    freeze_file = tmp_path / "freeze.txt"
    freeze_file.write_text(freeze)
    work = tmp_path / "work"
    venv_python = work / "venv" / "bin" / "python"
    rewrite = (
        f"printf '%s' '{pytest_rewrites_freeze_to}' > '{freeze_file}'"
        if pytest_rewrites_freeze_to is not None
        else ":"
    )
    calls, counter = tmp_path / "python_calls.txt", tmp_path / "prefetch_runs.txt"
    python_body = (
        f"echo \"$*\" >> '{calls}'\n"
        f'if [ "$1 $2" = "-m pytest" ]; then {rewrite}; exit 0; fi\n'
        f'if [ "$1" = "{PREFETCH_SCRIPT}" ]; then\n'
        f"    runs=$(cat '{counter}' 2>/dev/null || echo 0); echo $((runs + 1)) > '{counter}'\n"
        f'    [ "$runs" -ge {prefetch_failures} ] || exit 1\n'
        "fi\nexit 0\n"
    )
    _executable(tmp_path / "bin" / "sleep", ":\n")
    _executable(
        tmp_path / "bin" / "uv",
        f"""case "$1 $2" in
    "pip freeze") cat '{freeze_file}' ;;
    "pip install") echo "$*" >> '{tmp_path / "install_args.txt"}' ;;
    venv*) rm -rf '{venv_python.parent.parent}'  # what --clear does
        mkdir -p '{venv_python.parent}' && cat > '{venv_python}' <<'EOF'
#!/usr/bin/env bash
{python_body}EOF
        chmod +x '{venv_python}' ;;
esac
""",
    )
    if platform is not None:
        system, machine = platform
        _executable(tmp_path / "bin" / "uname", f'case "$1" in -s) echo {system} ;; -m) echo {machine} ;; esac\n')
    return {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "ARCHON_UNLOCKED_DIR": str(work),
    }


def _run(tmp_path: Path, env: dict, step: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tmp_path / "repo" / "scripts" / SCRIPT.name), step],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_suite_passes_when_the_venv_is_unchanged(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN)
    assert _run(tmp_path, env, "install").returncode == 0

    result = _run(tmp_path, env, "suite")

    assert result.returncode == 0, result.stderr


def test_suite_fails_when_a_test_changes_the_venv(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN, pytest_rewrites_freeze_to=f"{LOCAL_BUILD}\niniconfig==2.3.0\n")
    assert _run(tmp_path, env, "install").returncode == 0

    result = _run(tmp_path, env, "suite")

    assert result.returncode != 0
    assert "changed the venv under test" in result.stderr
    assert "iniconfig==2.3.0" in result.stderr, "the diff names what moved"


def _python_calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "python_calls.txt").read_text().splitlines()


def test_suite_fetches_the_pinned_tokenizer_with_the_venv_python_before_pytest(tmp_path: Path) -> None:
    # The default suite's real-gpt2 tests fail on CI (`CI` set) when the tokenizer is not cached.
    env = _lane(tmp_path, freeze=FROZEN)
    assert _run(tmp_path, env, "install").returncode == 0

    result = _run(tmp_path, env, "suite")

    assert result.returncode == 0, result.stderr
    calls = [c for c in _python_calls(tmp_path) if c == PREFETCH_SCRIPT or c.startswith("-m pytest")]
    assert [c.split()[0] for c in calls] == [PREFETCH_SCRIPT, "-m"], calls


def test_suite_retries_the_tokenizer_prefetch_before_running_pytest(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN, prefetch_failures=2)
    assert _run(tmp_path, env, "install").returncode == 0

    result = _run(tmp_path, env, "suite")

    assert result.returncode == 0, result.stderr
    assert _python_calls(tmp_path).count(PREFETCH_SCRIPT) == 3


def test_suite_fails_without_running_pytest_when_the_tokenizer_prefetch_keeps_failing(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN, prefetch_failures=3)
    assert _run(tmp_path, env, "install").returncode == 0

    result = _run(tmp_path, env, "suite")

    assert result.returncode != 0
    assert "tokenizer prefetch failed after 3 attempts" in result.stderr
    assert _python_calls(tmp_path).count(PREFETCH_SCRIPT) == 3
    assert not any(c.startswith("-m pytest") for c in _python_calls(tmp_path))


def test_lane_step_without_an_install_says_to_install_first(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN)

    result = _run(tmp_path, env, "suite")

    assert result.returncode != 0
    assert "run the 'install' step first" in result.stderr


def test_a_rejected_reinstall_leaves_no_snapshot_from_the_earlier_install(tmp_path: Path) -> None:
    env = _lane(tmp_path, freeze=FROZEN)
    assert _run(tmp_path, env, "install").returncode == 0
    (tmp_path / "freeze.txt").write_text("archon-search==26.10.2150\niniconfig==2.3.1\n")

    install = _run(tmp_path, env, "install")
    suite = _run(tmp_path, env, "suite")

    assert install.returncode != 0
    assert "not installed from this checkout" in install.stderr
    assert suite.returncode != 0, "the earlier install's snapshot must not survive a rejected one"
    assert "run the 'install' step first" in suite.stderr


@pytest.mark.parametrize(
    ("platform", "expects_cpu_backend"),
    [
        (("Linux", "x86_64"), True),
        (("Linux", "aarch64"), True),
        (("Darwin", "arm64"), False),
    ],
)
def test_install_selects_the_cpu_torch_backend_on_every_linux_arch(
    tmp_path: Path, platform: tuple[str, str], expects_cpu_backend: bool
) -> None:
    """PyPI's linux torch is the CUDA build on amd64 and arm64 alike (as `[tool.uv.sources]` pins)."""
    env = _lane(tmp_path, freeze=FROZEN, platform=platform)

    result = _run(tmp_path, env, "install")

    assert result.returncode == 0, result.stderr
    install_line = (tmp_path / "install_args.txt").read_text()
    assert ("--torch-backend cpu" in install_line) is expects_cpu_backend, install_line

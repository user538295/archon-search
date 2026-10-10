"""Regression: extras the setup wizard installs must survive ``uv tool upgrade``.

The wizard installs optional extras (``archon-search[graph]`` etc.) into the
environment it runs from. For a ``uv tool install archon-search`` user that
environment is the uv tool environment, and ``uv tool upgrade`` re-syncs it to
the requirements recorded in ``uv-receipt.toml``. An extra that is not recorded
in the receipt is therefore uninstalled by the next upgrade.

The test drives real ``uv`` fully offline against hand-built wheels: a fake
``archon-search`` (module ``fake_archon``, never the real package) whose
``graph`` extra pulls ``fake-gliner`` (which depends on ``fake-onnxruntime``,
a dependency a user can exclude). Every uv location is redirected into
``tmp_path`` so the user's real tool installs, caches and Python installs are
never touched.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from archon_search.install.extras import (
    _install_graph_extra,
    _install_multilingual_extra,
)

_TOOL_NAME = "archon-search"
_EXTRA_DIST = "fake-gliner"
_SECOND_EXTRA_DIST = "fake-fasttext"
# A dependency of the graph extra's dist that a user can `--excludes`.
_EXCLUDABLE_DIST = "fake-onnxruntime"
_OLD_VERSION = "1.0.0"
_NEW_VERSION = "2.0.0"
# Generous ceiling for one offline uv command; a hang must fail, not stall the suite.
_UV_TIMEOUT_SECONDS = 120


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def _build_wheel(
    links_dir: Path,
    dist_name: str,
    version: str,
    *,
    module: str,
    metadata_lines: tuple[str, ...] = (),
    entry_points: str | None = None,
) -> Path:
    """Write a minimal, valid pure-Python wheel for *dist_name* into *links_dir*."""
    stem = dist_name.replace("-", "_")
    dist_info = f"{stem}-{version}.dist-info"
    metadata = "\n".join(
        ["Metadata-Version: 2.1", f"Name: {dist_name}", f"Version: {version}", *metadata_lines]
    ) + "\n"
    files: dict[str, str] = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n\n\ndef main() -> None:\n    print(__version__)\n',
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: archon-search-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if entry_points is not None:
        files[f"{dist_info}/entry_points.txt"] = entry_points
    record_path = f"{dist_info}/RECORD"
    record_lines = []
    for path, text in files.items():
        data = text.encode()
        record_lines.append(f"{path},{_record_hash(data)},{len(data)}")
    record_lines.append(f"{record_path},,")
    files[record_path] = "\n".join(record_lines) + "\n"

    wheel = links_dir / f"{stem}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel


def _build_tool_wheel(links_dir: Path, version: str) -> Path:
    return _build_wheel(
        links_dir,
        _TOOL_NAME,
        version,
        module="fake_archon",
        metadata_lines=(
            "Provides-Extra: graph",
            f'Requires-Dist: {_EXTRA_DIST}; extra == "graph"',
            "Provides-Extra: multilingual",
            f'Requires-Dist: {_SECOND_EXTRA_DIST}; extra == "multilingual"',
        ),
        entry_points=f"[console_scripts]\n{_TOOL_NAME} = fake_archon:main\n",
    )


def _run_uv(*args: str) -> subprocess.CompletedProcess[str]:
    """Run uv with the (monkeypatched, isolated) process environment, output captured."""
    return subprocess.run(
        ["uv", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_UV_TIMEOUT_SECONDS,
    )


def _installed_versions(env_dir: Path, dist_name: str) -> list[str]:
    """Versions of *dist_name* installed in the venv at *env_dir* (from dist-info dirs)."""
    stem = dist_name.replace("-", "_")
    return sorted(
        info.name.removeprefix(f"{stem}-").removesuffix(".dist-info")
        for info in env_dir.glob(f"lib/python*/site-packages/{stem}-*.dist-info")
    )


@pytest.fixture
def isolated_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every uv location at *tmp_path* and keep uv offline."""
    if shutil.which("uv") is None:
        pytest.skip("uv executable not found on PATH")
    dirs = {
        name: tmp_path / name
        for name in ("links", "tools", "bin", "xdg_data", "xdg_bin", "xdg_cache", "xdg_config", "uv_cache")
    }
    for path in dirs.values():
        path.mkdir()
    env = {
        "UV_TOOL_DIR": dirs["tools"],
        "UV_TOOL_BIN_DIR": dirs["bin"],
        # Backstop: uv's defaults for the tool dir, bin dir, managed Pythons and
        # cache all derive from these, so a command that drops UV_TOOL_DIR still
        # lands in tmp_path, never in the user's real install.
        "XDG_DATA_HOME": dirs["xdg_data"],
        "XDG_BIN_HOME": dirs["xdg_bin"],
        "XDG_CACHE_HOME": dirs["xdg_cache"],
        "XDG_CONFIG_HOME": dirs["xdg_config"],
        "UV_CACHE_DIR": dirs["uv_cache"],
        "UV_FIND_LINKS": dirs["links"],
    }
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    # UV_OFFLINE is what keeps every uv call off the network: uv 0.12.23 has no
    # env var for --no-index (`UV_NO_INDEX=1` still queries the index), so only the
    # offline mode blocks that request; packages come from UV_FIND_LINKS.
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    # `uv run pytest` exports the repo venv; nothing here may target it.
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Managed Pythons are hidden by the XDG redirect and downloads are off, so make
    # the test runner's own base interpreter the first one uv discovers on PATH.
    # (No --python / UV_PYTHON: the receipt must look like a plain user install.)
    base_python_dir = Path(sys._base_executable).parent
    monkeypatch.setenv("PATH", f"{base_python_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)
    return dirs


def _install_tool(isolated_uv: dict[str, Path], excludes: tuple[str, ...] = ()) -> Path:
    """Build the old tool wheel plus the extra dists and ``uv tool install`` it plainly; return the env dir.

    *excludes* are passed as ``--excludes -`` on stdin, as a user excluding a dependency would.
    """
    links = isolated_uv["links"]
    _build_tool_wheel(links, _OLD_VERSION)
    _build_wheel(
        links, _EXTRA_DIST, "1.0.0", module="fake_gliner", metadata_lines=(f"Requires-Dist: {_EXCLUDABLE_DIST}",)
    )
    _build_wheel(links, _SECOND_EXTRA_DIST, "1.0.0", module="fake_fasttext")
    _build_wheel(links, _EXCLUDABLE_DIST, "1.0.0", module="fake_onnxruntime")

    # A plain user install: no --python, no extras.
    args = ("--excludes", "-") if excludes else ()
    install = subprocess.run(
        ["uv", "tool", "install", _TOOL_NAME, *args],
        input="".join(f"{name}\n" for name in excludes),
        capture_output=True,
        text=True,
        check=False,
        timeout=_UV_TIMEOUT_SECONDS,
    )
    assert install.returncode == 0, f"uv tool install failed:\n{install.stdout}\n{install.stderr}"
    env_dir = isolated_uv["tools"] / _TOOL_NAME
    receipt = env_dir / "uv-receipt.toml"
    assert receipt.is_file(), f"no uv receipt at {receipt}:\n{install.stderr}"
    assert _installed_versions(env_dir, _TOOL_NAME) == [_OLD_VERSION]
    return env_dir


def _run_in_tool_env(monkeypatch: pytest.MonkeyPatch, env_dir: Path, *installers: Callable[[], None]) -> None:
    """Run the real wizard install steps as if from inside the tool environment.

    UV_TOOL_DIR / UV_TOOL_BIN_DIR are dropped so the code under test has to find
    the tool environment on its own (XDG_* still confine it to tmp_path).
    """
    tool_python = env_dir / "bin" / "python"
    assert tool_python.exists(), f"tool env has no interpreter at {tool_python}"
    with monkeypatch.context() as wizard:
        wizard.setattr(sys, "prefix", str(env_dir))
        wizard.setattr(sys, "executable", str(tool_python))
        wizard.delenv("UV_TOOL_DIR")
        wizard.delenv("UV_TOOL_BIN_DIR")
        for install in installers:
            install()


def _upgrade(links: Path) -> subprocess.CompletedProcess[str]:
    _build_tool_wheel(links, _NEW_VERSION)
    upgrade = _run_uv("tool", "upgrade", _TOOL_NAME)
    assert upgrade.returncode == 0, f"uv tool upgrade failed:\n{upgrade.stdout}\n{upgrade.stderr}"
    return upgrade


def test_wizard_extra_survives_uv_tool_upgrade(
    isolated_uv: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = _install_tool(isolated_uv)
    receipt = env_dir / "uv-receipt.toml"
    _run_in_tool_env(monkeypatch, env_dir, _install_graph_extra)
    assert _installed_versions(env_dir, _EXTRA_DIST) == ["1.0.0"], "wizard did not install the graph extra"

    upgrade = _upgrade(isolated_uv["links"])
    assert _installed_versions(env_dir, _TOOL_NAME) == [_NEW_VERSION], upgrade.stderr
    assert _installed_versions(env_dir, _EXTRA_DIST) == ["1.0.0"], (
        f"`uv tool upgrade {_TOOL_NAME}` removed the wizard-installed graph extra.\n"
        f"--- {receipt.name} ---\n{receipt.read_text()}\n"
        f"--- upgrade stdout ---\n{upgrade.stdout}\n--- upgrade stderr ---\n{upgrade.stderr}"
    )


def test_second_wizard_extra_keeps_the_first_recorded(
    isolated_uv: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = _install_tool(isolated_uv)
    _run_in_tool_env(monkeypatch, env_dir, _install_graph_extra, _install_multilingual_extra)

    receipt = tomllib.loads((env_dir / "uv-receipt.toml").read_text())
    assert receipt["tool"]["requirements"][0]["extras"] == ["graph", "multilingual"]
    upgrade = _upgrade(isolated_uv["links"])
    assert _installed_versions(env_dir, _TOOL_NAME) == [_NEW_VERSION], upgrade.stderr
    for dist in (_EXTRA_DIST, _SECOND_EXTRA_DIST):
        assert _installed_versions(env_dir, dist) == ["1.0.0"], f"`uv tool upgrade` removed {dist}:\n{upgrade.stderr}"


def test_wizard_extra_keeps_the_receipts_excludes(
    isolated_uv: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = _install_tool(isolated_uv, excludes=(_EXCLUDABLE_DIST,))
    _run_in_tool_env(monkeypatch, env_dir, _install_graph_extra)

    receipt = tomllib.loads((env_dir / "uv-receipt.toml").read_text())
    assert receipt["tool"]["requirements"][0]["extras"] == ["graph"]
    assert receipt["tool"]["excludes"] == [_EXCLUDABLE_DIST]
    assert _installed_versions(env_dir, _EXTRA_DIST) == ["1.0.0"]
    assert _installed_versions(env_dir, _EXCLUDABLE_DIST) == []

    upgrade = _upgrade(isolated_uv["links"])
    assert _installed_versions(env_dir, _TOOL_NAME) == [_NEW_VERSION], upgrade.stderr
    assert _installed_versions(env_dir, _EXTRA_DIST) == ["1.0.0"], f"`uv tool upgrade` removed {_EXTRA_DIST}:\n{upgrade.stderr}"
    assert _installed_versions(env_dir, _EXCLUDABLE_DIST) == [], f"`uv tool upgrade` installed {_EXCLUDABLE_DIST}:\n{upgrade.stderr}"

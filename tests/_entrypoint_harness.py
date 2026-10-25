"""Offline harness that runs the REAL ``scripts/docker-entrypoint.sh`` like a container start.

A :class:`Container` is one image plus one ``/pip-packages`` named volume:

* the image's system Python is a venv (``archon-search`` + ``basedep`` + the
  CPU ``onnxruntime`` it requires baked in) reached through a ``python3``
  wrapper first on ``PATH``. The wrapper delegates
  to the real interpreter, so the entrypoint drives REAL pip, but it also
  records every ``pip install`` call (the ``PIP_DEFAULT_TIMEOUT`` each one saw)
  and can simulate the failures listed at :data:`PYTHON_WRAPPER`;
* pip is offline (``PIP_NO_INDEX``), ignores every pip config file
  (``PIP_CONFIG_FILE=os.devnull``), keeps no cache (``PIP_NO_CACHE_DIR``, as the
  image's Dockerfile sets it) and resolves from a wheelhouse of tiny hand-built
  wheels (:func:`build_wheelhouse`);
* the working directory is the image's ``/app`` source tree: an offline-buildable
  ``archon-search`` project, so an entrypoint that installs from it would put a
  second ``archon-search`` into the volume and the probe would see it.

A "legacy" volume is one an older entrypoint filled: it ran
``pip install --target`` over the whole closure, so the volume holds its own
``archon-search`` copy, and it stamped only the extras list.

``/pip-packages`` is a hardcoded literal in the script and cannot be created on
a test host, so each container runs a copy of the script with that literal
replaced by a temp directory. No env seam exists in production code for this;
the substitution keeps working as long as the script keeps ``/pip-packages`` as
its volume root literal.
"""
import base64
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENTRYPOINT = Path(__file__).parents[1] / "scripts" / "docker-entrypoint.sh"
VOLUME_ROOT_LITERAL = "/pip-packages"
STAMP_NAME = ".extras-installed"
IMAGE_VERSION = "1.0"
NEW_IMAGE_VERSION = "2.0"
BASEDEP_OLD = "1.0"
BASEDEP_NEW = "2.0"
EXTRADEP_VERSION = "1.0"
GRAPHDEP_VERSION = "1.0"
FUSIONDEP_VERSION = "1.0"
MULTILINGUALDEP_VERSION = "1.0"
ONNXRUNTIME_VERSION = "1.0"
SUBPROCESS_TIMEOUT_S = 180
# Modules the CMD probe reports on: the app, a base dependency, one package per extra.
PROBED_MODULES = (
    "archon_search", "basedep", "onnxruntime", "extradep", "graphdep", "fusiondep", "multilingualdep",
)

# The container CMD: reports, per module, which file the running process imports.
IMPORT_ORIGIN_PROBE = """
import importlib, json, sys
found = {}
for name in sys.argv[1:]:
    try:
        module = importlib.import_module(name)
    except ImportError:
        found[name] = None
    else:
        found[name] = {"file": module.__file__, "version": module.VERSION}
print(json.dumps(found))
"""

# Stands in for the image's python3 and delegates to the real one. Every
# `pip install` call appends the PIP_DEFAULT_TIMEOUT it saw to the calls file.
# Simulated failures: the first {fail_first} `pip install` calls fail before pip
# runs, like a dropped connection; the first {fail_after_write} `--target`
# installs let pip write into the target and then fail, like a crash
# mid-install. Right before every `--target` install, wheels waiting in
# {pending} are published to {late}, a find-links directory: an index that
# changed between the resolution and the install.
PYTHON_WRAPPER = """#!/bin/sh
case " $* " in
    *" -m pip install "*)
        echo "${{PIP_DEFAULT_TIMEOUT:-unset}}" >> "{calls}"
        if [ "$(wc -l < "{calls}")" -le {fail_first} ]; then
            echo "simulated network failure" >&2
            exit 1
        fi
        ;;
esac
case " $* " in
    *" -m pip install "*" --target "*)
        find "{pending}" -type f -exec mv {{}} "{late}" \\;
        echo >> "{target_calls}"
        if [ "$(wc -l < "{target_calls}")" -le {fail_after_write} ]; then
            "{python}" "$@" || exit
            echo "simulated failure after pip wrote into the target" >&2
            exit 1
        fi
        ;;
esac
exec "{python}" "$@"
"""

# In-tree PEP 517 backend for the image's /app: "builds" the source tree by
# copying the prebuilt archon-search wheel next to it (no network, no build deps).
BACKEND = '''
import shutil
from pathlib import Path

_PREBUILT = Path(__file__).parent / "prebuilt"


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    (wheel,) = _PREBUILT.glob("*.whl")
    shutil.copy(wheel, wheel_directory)
    return wheel.name
'''


@dataclass(frozen=True)
class WheelSpec:
    """A pure-Python wheel holding one module that exposes ``VERSION``."""

    name: str
    version: str
    requires: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    # A wheel build tag: pip prefers a tagged build over an untagged one of the same version.
    build_tag: str = ""
    # What the module's ``VERSION`` reports; defaults to ``version``.
    module_version: str = ""

    @property
    def distribution(self) -> str:
        return self.name.replace("-", "_")

    @property
    def filename(self) -> str:
        build = f"-{self.build_tag}" if self.build_tag else ""
        return f"{self.distribution}-{self.version}{build}-py3-none-any.whl"


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arcname},sha256={digest},{len(data)}"


def _build_wheel(directory: Path, spec: WheelSpec) -> Path:
    dist_info = f"{spec.distribution}-{spec.version}.dist-info"
    metadata = ["Metadata-Version: 2.1", f"Name: {spec.name}", f"Version: {spec.version}"]
    metadata += [f"Provides-Extra: {extra}" for extra in spec.extras]
    metadata += [f"Requires-Dist: {req}" for req in spec.requires]
    files = {
        f"{spec.distribution}/__init__.py": f'VERSION = "{spec.module_version or spec.version}"\n'.encode(),
        f"{dist_info}/METADATA": ("\n".join(metadata) + "\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [_record_line(arc, data) for arc, data in files.items()] + [f"{dist_info}/RECORD,,"]
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    path = directory / spec.filename
    with zipfile.ZipFile(path, "w") as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
    return path


def build_wheelhouse(directory: Path) -> None:
    """archon-search 1.0 (needs basedep>=1.0) and 2.0 (needs basedep>=2.0) plus every dependency.

    Both declare the extras ``code`` → extradep, ``graph`` → graphdep,
    ``multilingual`` → multilingualdep and ``rag-fusion`` → fusiondep (its PEP
    685-normalised spelling, as hatchling writes it). graphdep needs
    basedep<2.0, like the real ``code`` extra's tree-sitter pin: on an image
    that ships basedep 2.0 the ``graph`` extra adds basedep 1.0 to the volume,
    and that copy must win. Both also need ``onnxruntime``, as the real
    archon-search does through gliner, magika and RapidOCR.
    """
    for app_version, basedep_min in ((IMAGE_VERSION, BASEDEP_OLD), (NEW_IMAGE_VERSION, BASEDEP_NEW)):
        _build_wheel(directory, WheelSpec(
            "archon-search",
            app_version,
            requires=(
                f"basedep>={basedep_min}",
                "onnxruntime",
                'extradep; extra == "code"',
                'graphdep; extra == "graph"',
                'multilingualdep; extra == "multilingual"',
                'fusiondep; extra == "rag-fusion"',
            ),
            extras=("code", "graph", "multilingual", "rag-fusion"),
        ))
    for basedep_version in (BASEDEP_OLD, BASEDEP_NEW):
        _build_wheel(directory, WheelSpec("basedep", basedep_version))
    _build_wheel(directory, WheelSpec("extradep", EXTRADEP_VERSION))
    _build_wheel(directory, WheelSpec("graphdep", GRAPHDEP_VERSION, requires=(f"basedep<{BASEDEP_NEW}",)))
    _build_wheel(directory, WheelSpec("fusiondep", FUSIONDEP_VERSION))
    _build_wheel(directory, WheelSpec("multilingualdep", MULTILINGUALDEP_VERSION))
    _build_wheel(directory, WheelSpec("onnxruntime", ONNXRUNTIME_VERSION))


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "ARCHON_", "PYTHON", "VIRTUAL_ENV", "UV_"))
    }
    # pip loads no config file at all (host pip.conf, XDG, /etc) when this is os.devnull.
    env["PIP_CONFIG_FILE"] = os.devnull
    env.update(extra)
    return env


def _run(command: list[str], *, check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=check, **kwargs
    )


def _create_dir(path: Path) -> Path:
    path.mkdir()
    return path.resolve()


def _create_venv(path: Path) -> Path:
    _run([sys.executable, "-m", "venv", str(path)], env=_clean_env(), check=True)
    return path.resolve()


def _write_entrypoint_copy(path: Path, volume_root: Path) -> Path:
    script = ENTRYPOINT.read_text()
    assert VOLUME_ROOT_LITERAL in script, "entrypoint no longer uses the /pip-packages literal this harness rewrites"
    path.write_text(script.replace(VOLUME_ROOT_LITERAL, str(volume_root)))
    return path


def _create_source_tree(path: Path, wheelhouse: Path, app_version: str) -> Path:
    """The image's ``/app``: an offline-buildable archon-search ``app_version`` source tree."""
    (path / "prebuilt").mkdir(parents=True)
    wheel = wheelhouse / WheelSpec("archon-search", app_version).filename
    (path / "prebuilt" / wheel.name).write_bytes(wheel.read_bytes())
    (path / "in_tree_backend.py").write_text(BACKEND)
    (path / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = []\n"
        'build-backend = "in_tree_backend"\n'
        'backend-path = ["."]\n'
    )
    return path


@dataclass(frozen=True)
class StartResult:
    """What one container start left behind."""

    returncode: int
    output: str
    imports: dict[str, dict[str, str] | None]
    # One entry per `pip install` call the entrypoint made: the PIP_DEFAULT_TIMEOUT that call saw.
    pip_install_timeouts: list[str]


class Container:
    """One image (a venv standing in for its system Python) plus one ``/pip-packages`` volume."""

    def __init__(self, tmp_path: Path, wheelhouse: Path) -> None:
        self._tmp_path = tmp_path
        self._wheelhouse = wheelhouse
        self._bin = _create_dir(tmp_path / "bin")
        self._pending_uploads = _create_dir(tmp_path / "pending-uploads")
        self._late_uploads = _create_dir(tmp_path / "late-uploads")
        self._calls = tmp_path / "pip-install-calls"
        self._target_calls = tmp_path / "pip-target-install-calls"
        self.image_venv = _create_venv(tmp_path / "image-venv")
        self.volume_root = _create_dir(tmp_path / "pip-packages")
        self.stamp = self.volume_root / STAMP_NAME
        self._entrypoint = _write_entrypoint_copy(tmp_path / "docker-entrypoint.sh", self.volume_root)
        self.bake(f"archon-search=={IMAGE_VERSION}", f"basedep=={BASEDEP_OLD}")
        self._app = _create_source_tree(tmp_path / f"app-{IMAGE_VERSION}", wheelhouse, IMAGE_VERSION)

    def bake(self, *requirements: str) -> None:
        """Rebuild the image with these packages installed into its system Python."""
        _run(
            [str(self.image_venv / "bin" / "python"), "-m", "pip", "install", "--no-index",
             "--find-links", str(self._wheelhouse), *requirements],
            env=_clean_env(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1"), check=True,
        )

    def uninstall(self, *distributions: str) -> None:
        """Rebuild the image with these distributions removed from its system Python."""
        _run(
            [str(self.image_venv / "bin" / "python"), "-m", "pip", "uninstall", "-y", *distributions],
            env=_clean_env(PIP_DISABLE_PIP_VERSION_CHECK="1"), check=True,
        )

    def upgrade_image(self, app_version: str) -> None:
        """Pull a newer image: archon-search ``app_version`` and the base deps it needs baked in, its own /app."""
        self.bake(f"archon-search=={app_version}")
        self._app = _create_source_tree(self._tmp_path / f"app-{app_version}", self._wheelhouse, app_version)

    def publish_after_resolution(self, spec: WheelSpec) -> None:
        """Make ``spec`` appear on the index after the entrypoint resolved, right before it installs."""
        _build_wheel(self._pending_uploads, spec)

    def start(self, extras: str | None, *, fail_first: int = 0, fail_after_write: int = 0) -> StartResult:
        """Run the entrypoint with ``ARCHON_EXTRAS=extras`` (left unset when None) and the import-origin probe as CMD."""
        self._calls.write_text("")
        self._target_calls.write_text("")
        wrapper = self._bin / "python3"
        wrapper.write_text(PYTHON_WRAPPER.format(
            calls=self._calls,
            target_calls=self._target_calls,
            fail_first=fail_first,
            fail_after_write=fail_after_write,
            pending=self._pending_uploads,
            late=self._late_uploads,
            python=self.image_venv / "bin" / "python3",
        ))
        wrapper.chmod(0o755)
        env = _clean_env(
            PATH=f"{self._bin}:{self.image_venv / 'bin'}:{os.environ.get('PATH', '')}",
            ARCHON_PIP_RETRY_DELAY_S="0",
            PIP_NO_INDEX="1",
            PIP_FIND_LINKS=f"{self._wheelhouse} {self._late_uploads}",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_NO_CACHE_DIR="1",
        )
        if extras is not None:
            env["ARCHON_EXTRAS"] = extras
        result = _run(
            ["sh", str(self._entrypoint), "python3", "-c", IMPORT_ORIGIN_PROBE, *PROBED_MODULES],
            cwd=self._app, env=env,
        )
        output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        imports = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {}
        return StartResult(result.returncode, output, imports, self._calls.read_text().split())

    def volume_entries(self) -> list[str]:
        """The names directly under the volume root, sorted."""
        return sorted(entry.name for entry in self.volume_root.iterdir())

    def volume_distributions(self) -> list[str]:
        """The (lower-cased) names of the distributions installed in the volume, sorted."""
        return sorted(
            dist.metadata["Name"].lower() for dist in importlib.metadata.distributions(path=[str(self.volume_root)])
        )

    def plant_legacy_app_copy(self) -> Path:
        """The archon-search copy a legacy volume holds."""
        legacy_copy = self.volume_root / "archon_search"
        legacy_copy.mkdir()
        (legacy_copy / "__init__.py").write_text('VERSION = "0.0.0+local"\n')
        return legacy_copy


def loaded_from(start: StartResult, module: str, directory: Path) -> bool:
    """True when the CMD imported ``module`` from a file under ``directory``."""
    found = start.imports.get(module)
    assert found is not None, f"CMD could not import {module}:\n{start.output}"
    return Path(found["file"]).resolve().is_relative_to(directory)

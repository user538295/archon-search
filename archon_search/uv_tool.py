"""Keep wizard-installed extras recorded in a ``uv tool`` environment's receipt.

A ``uv tool install archon-search`` environment (``<UV_TOOL_DIR>/archon-search``)
is declarative: ``uv tool upgrade`` re-syncs it to the requirements recorded in
its ``uv-receipt.toml`` and removes anything else, so an extra installed with
``uv pip install`` disappears on the next upgrade. The supported way to add an
extra is to re-run ``uv tool install`` with the receipt's own requirements and
options plus the extra; uv then records it and keeps it on upgrade
(https://docs.astral.sh/uv/concepts/tools/#upgrading-tools).

``uv tool install`` replaces the receipt with exactly what it is given, so the
command must reproduce every recorded setting. ``excludes`` is reproduced as
``--excludes -`` with the names on stdin (:class:`ToolInstall` carries both). Receipt
state outside the supported subset raises :class:`UnsupportedReceiptError` —
callers fall back to ``uv pip install`` and tell the user how to record the
extra themselves.

Stdlib-only on purpose: the server's startup guards import it, and it must not
pull in the installer package.
"""
from __future__ import annotations

import importlib.metadata
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

TOOL_NAME = "archon-search"
RECEIPT_FILENAME = "uv-receipt.toml"

# `[tool]` keys `uv tool install` re-applies from its own CLI flags.
_REPRODUCIBLE_TOOL_KEYS = frozenset({"requirements", "entrypoints", "options", "excludes"})
# `[tool]` keys this module does not reproduce: `python` (an explicit Python
# request that does not match the env interpreter recreates the env under the
# running wizard) and settings that need extra files. Present-but-empty is fine.
_UNREPRODUCED_TOOL_KEYS = frozenset({"python", "constraints", "overrides", "build-constraint-dependencies"})
_SOURCE_KEYS = ("path", "directory", "url")
_REQUIREMENT_KEYS = frozenset({"name", "extras", "marker", "specifier", "subdirectory", *_SOURCE_KEYS})
_STRING_OPTIONS = frozenset({"index-url", "index-strategy", "prerelease"})
_LIST_OPTIONS = frozenset({"extra-index-url", "find-links"})
_BOOL_OPTIONS = frozenset({"no-index"})
# Local-version tags of torch builds that map onto a uv `--torch-backend` value.
_TORCH_BACKEND_TAG = re.compile(r"cpu|xpu|cu\d+|rocm\d+(?:\.\d+){1,2}")
_NAME_SEPARATORS = re.compile(r"[-_.]+")
_REPEAT_INSTALL_NOTE = (
    f"also repeat every option {TOOL_NAME} was installed with: the extras already installed, --with, "
    "index options, --python, constraints/overrides and --torch-backend"
)


class UnsupportedReceiptError(ValueError):
    """The receipt holds state a ``uv tool install`` command cannot reproduce faithfully."""


class ToolInstall(NamedTuple):
    """A ``uv tool install`` command and the standard input it reads (``None``: no stdin)."""

    argv: list[str]
    stdin: str | None


def find_receipt(prefix: Path) -> Path | None:
    """Return the uv tool receipt of the environment at *prefix*, or ``None``.

    uv names the environment directory after the tool, so only an
    ``archon-search`` directory holding the receipt file qualifies.
    """
    receipt = prefix / RECEIPT_FILENAME
    return receipt if _canonical_name(prefix.name) == TOOL_NAME and receipt.is_file() else None


def load_receipt(path: Path) -> dict[str, Any]:
    """Parse the receipt at *path*; an unreadable one is unsupported."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UnsupportedReceiptError(f"cannot read {path}: {exc}") from exc


def torch_backend(version: str) -> str | None:
    """Map an installed torch *version*'s local tag to a uv ``--torch-backend``.

    ``2.14.1+cu126`` → ``cu126``; no tag (the PyPI default build) → ``None``. A
    tag uv has no backend for raises :class:`UnsupportedReceiptError`: a
    re-resolve would silently swap that specialised build for PyPI's.
    """
    _, separator, local = version.partition("+")
    if not separator:
        return None
    if not _TORCH_BACKEND_TAG.fullmatch(local):
        raise UnsupportedReceiptError(f"the installed torch build `{version}` has no uv --torch-backend for `{local}`")
    return local


def installed_torch_backend() -> str | None:
    """The ``--torch-backend`` matching the torch build in this interpreter, if any."""
    try:
        return torch_backend(importlib.metadata.version("torch"))
    except importlib.metadata.PackageNotFoundError:
        return None


def install_extra_command(receipt: Mapping[str, Any], extra: str, *, backend: str | None) -> ToolInstall:
    """Build the ``uv tool install`` command that reproduces *receipt* plus *extra*.

    *receipt* is the parsed ``uv-receipt.toml``. *backend* is the
    ``--torch-backend`` that keeps the installed torch build: re-resolving
    without it swaps a ``+cpu``/``+cu126`` torch for PyPI's default build.
    Recorded ``excludes`` end the argv with ``--excludes -`` and become the
    returned stdin, one name per line.
    Raises :class:`UnsupportedReceiptError` naming the first receipt setting
    the command could not reproduce.
    """
    tool = receipt.get("tool")
    if not isinstance(tool, Mapping):
        raise UnsupportedReceiptError("the receipt has no [tool] table")
    for key, value in tool.items():
        if key in _UNREPRODUCED_TOOL_KEYS:
            if value:
                raise UnsupportedReceiptError(f"the receipt records `{key}`, which is not reproduced")
        elif key not in _REPRODUCIBLE_TOOL_KEYS:
            raise UnsupportedReceiptError(f"the receipt records unknown setting `{key}`")

    requirements = tool.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise UnsupportedReceiptError("the receipt records no requirements")
    main, *with_requirements = (_requirement_table(entry) for entry in requirements)
    main_name = main.get("name")
    if not isinstance(main_name, str) or _canonical_name(main_name) != TOOL_NAME:
        raise UnsupportedReceiptError(f"the receipt's first requirement is `{main_name}`, not `{TOOL_NAME}`")
    _check_entrypoints(tool.get("entrypoints", []))

    command = ["uv", "tool", "install", _pep508(main, added_extra=extra)]
    for requirement in with_requirements:
        command += ["--with", _pep508(requirement)]
    command += install_option_flags(receipt)
    if backend:
        command += ["--torch-backend", backend]
    stdin = _excludes_stdin(tool)
    if stdin is not None:
        command += ["--excludes", "-"]
    return ToolInstall(command, stdin)


def _excludes_stdin(tool: Mapping[str, Any]) -> str | None:
    """The receipt's ``[tool].excludes`` one name per line; ``None`` when it records none."""
    excludes = tool.get("excludes", [])
    if not _is_string_list(excludes):
        raise UnsupportedReceiptError(f"the receipt records `excludes` as {excludes!r}, not a list of names")
    return "".join(f"{name}\n" for name in excludes) or None


def install_option_flags(receipt: Mapping[str, Any]) -> list[str]:
    """The receipt's ``[tool.options]`` as install flags (``uv tool install`` and ``uv pip install`` share them)."""
    tool = receipt.get("tool")
    if not isinstance(tool, Mapping):
        raise UnsupportedReceiptError("the receipt has no [tool] table")
    return _option_flags(tool.get("options", {}))


def tool_env(receipt_path: Path, receipt: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    """Environment for the ``uv tool install`` run: target the receipt's own env.

    ``UV_TOOL_DIR`` is the directory holding the env, ``UV_TOOL_BIN_DIR`` the
    directory of the installed ``archon-search`` executable. When the receipt
    records no such executable, ``UV_TOOL_BIN_DIR`` is left as inherited, so uv
    uses that or its default bin dir. ``UV_PYTHON`` is dropped: the receipt
    pins no Python, and an inherited request that does not match the env
    interpreter makes uv recreate the env.
    """
    env = dict(environ)
    env.pop("UV_PYTHON", None)
    env["UV_TOOL_DIR"] = str(receipt_path.parent.parent)
    bin_dir = _entrypoint_bin_dir(receipt)
    if bin_dir is not None:
        env["UV_TOOL_BIN_DIR"] = str(bin_dir)
    return env


def extra_install_hint(extra: str) -> str:
    """Shell command that installs ``archon-search[<extra>]`` for this interpreter.

    In a uv tool environment it is the receipt-preserving ``uv tool install``
    command, fed the receipt's excludes through ``printf`` when it records any;
    when the receipt cannot be reproduced, a plain ``uv tool install`` with a
    shell comment telling the user what else to repeat. Anywhere else,
    ``pip install``.
    """
    package = f"{TOOL_NAME}[{extra}]"
    receipt_path = find_receipt(Path(sys.prefix))
    if receipt_path is None:
        return shlex.join(["pip", "install", package])
    try:
        install = install_extra_command(load_receipt(receipt_path), extra, backend=installed_torch_backend())
    except UnsupportedReceiptError:
        return unrecorded_install_hint(extra)
    if install.stdin is None:
        return shlex.join(install.argv)
    return f"{shlex.join(['printf', '%s\\n', *install.stdin.splitlines()])} | {shlex.join(install.argv)}"


def unrecorded_install_hint(extra: str) -> str:
    """Plain ``uv tool install`` for *extra*, with a note to repeat the original install options.

    For a receipt the wizard cannot reproduce: only the user knows the rest.
    """
    return f"{shlex.join(['uv', 'tool', 'install', f'{TOOL_NAME}[{extra}]'])}  # {_REPEAT_INSTALL_NOTE}"


def _canonical_name(name: str) -> str:
    """PEP 503 normalisation: lowercase, runs of ``-_.`` become ``-``."""
    return _NAME_SEPARATORS.sub("-", name).lower()


def _requirement_table(entry: object) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise UnsupportedReceiptError(f"the receipt records requirement `{entry}` in the deprecated string form")
    return entry


def _string(requirement: Mapping[str, Any], key: str) -> str:
    value = requirement[key]
    if not isinstance(value, str):
        raise UnsupportedReceiptError(f"requirement `{requirement.get('name')}` has a non-string `{key}`")
    return value


def _pep508(requirement: Mapping[str, Any], added_extra: str | None = None) -> str:
    """Render a receipt requirement table as a PEP 508 string."""
    name = requirement.get("name")
    if not isinstance(name, str):
        raise UnsupportedReceiptError(f"the receipt records a requirement without a name: {dict(requirement)}")
    unknown = sorted(set(requirement) - _REQUIREMENT_KEYS)
    if unknown:
        raise UnsupportedReceiptError(f"requirement `{name}` uses unsupported receipt field(s): {', '.join(unknown)}")
    text = _with_source(requirement, name, _head(requirement, name, added_extra))
    if requirement.get("marker"):
        text += f" ; {_string(requirement, 'marker')}"
    return text


def _head(requirement: Mapping[str, Any], name: str, added_extra: str | None) -> str:
    """``name[extras]`` with *added_extra* appended unless already recorded."""
    recorded_extras = requirement.get("extras", [])
    if not _is_string_list(recorded_extras):
        raise UnsupportedReceiptError(f"requirement `{name}` has malformed `extras`")
    extras = list(recorded_extras)
    if added_extra is not None and added_extra not in extras:
        extras.append(added_extra)
    return f"{name}[{','.join(extras)}]" if extras else name


def _source_key(requirement: Mapping[str, Any], name: str) -> str | None:
    """The requirement's single source key (``path``/``directory``/``url``), or ``None`` for a registry one."""
    sources = [key for key in _SOURCE_KEYS if key in requirement]
    if len(sources) > 1:
        raise UnsupportedReceiptError(f"requirement `{name}` records several sources: {', '.join(sources)}")
    source = sources[0] if sources else None
    if source and "specifier" in requirement:
        raise UnsupportedReceiptError(f"requirement `{name}` records a `specifier` with a {source} source")
    if "subdirectory" in requirement and source != "url":
        raise UnsupportedReceiptError(f"requirement `{name}` records a `subdirectory` without a url source")
    return source


def _with_source(requirement: Mapping[str, Any], name: str, head: str) -> str:
    """Append the requirement's specifier or ``@ <source>`` to *head*."""
    source = _source_key(requirement, name)
    if source is None:
        return head + (_string(requirement, "specifier") if "specifier" in requirement else "")
    if source == "url":
        text = f"{head} @ {_string(requirement, 'url')}"
        if "subdirectory" in requirement:
            text += f"#subdirectory={_string(requirement, 'subdirectory')}"
        return text
    location = Path(_string(requirement, source))
    if not location.is_absolute():
        raise UnsupportedReceiptError(f"requirement `{name}` records a relative {source}: {location}")
    return f"{head} @ {location.as_uri()}"


def _check_entrypoints(entrypoints: object) -> None:
    """Every executable must come from the tool itself.

    Executables from ``--with-executables-from`` packages record that package
    in ``from``; re-installing without the flag would drop them. Receipts that
    predate the flag carry no ``from`` (uv ``ToolEntrypoint.from`` is optional)
    and only ever held the tool's own executables.
    """
    if not isinstance(entrypoints, list):
        raise UnsupportedReceiptError("the receipt has malformed `entrypoints`")
    for entry in entrypoints:
        source = entry.get("from", TOOL_NAME) if isinstance(entry, Mapping) else None
        if not isinstance(source, str) or _canonical_name(source) != TOOL_NAME:
            raise UnsupportedReceiptError(f"the receipt installs executables from `{source}`")


def _option_flags(options: object) -> list[str]:
    """Render ``[tool.options]`` back into ``uv tool install`` flags."""
    if not isinstance(options, Mapping):
        raise UnsupportedReceiptError("the receipt has a malformed [tool.options] table")
    flags: list[str] = []
    for key, value in options.items():
        if key in _STRING_OPTIONS and isinstance(value, str):
            flags += [f"--{key}", value]
        elif key in _LIST_OPTIONS and _is_string_list(value):
            for item in value:
                flags += [f"--{key}", item]
        elif key in _BOOL_OPTIONS and isinstance(value, bool):
            if value:
                flags.append(f"--{key}")
        else:
            raise UnsupportedReceiptError(f"the receipt records install option `{key}`, which is not reproduced")
    return flags


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _entrypoint_bin_dir(receipt: Mapping[str, Any]) -> Path | None:
    tool = receipt.get("tool")
    entrypoints = tool.get("entrypoints", []) if isinstance(tool, Mapping) else []
    for entry in entrypoints if isinstance(entrypoints, list) else []:
        if isinstance(entry, Mapping) and entry.get("name") == TOOL_NAME:
            install_path = entry.get("install-path")
            if isinstance(install_path, str):
                return Path(install_path).parent
    return None

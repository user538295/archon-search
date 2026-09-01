"""Tests for the wizard's spaCy model provisioning (2026-08-19-030).

The runtime never downloads ``en_core_web_sm``; the wizard fetches the pinned
model wheel and places it under the data dir, where ``GraphExtractor`` resolves
it by path. These tests mock the two network fetches and exercise the real
version-pin, unzip, place and smoke-load logic.

The compatibility table (https://raw.githubusercontent.com/explosion/
spacy-models/master/compatibility.json) is keyed by spaCy's MAJOR.MINOR
version (e.g. ``"3.8"``), not the full patch version — confirmed against the
live table and against the vendored spaCy's own
``spacy.cli.download.get_compatibility()``
(``.venv/lib/python3.13/site-packages/spacy/cli/download.py:136-153``), which
resolves the same key spaCy 3.8.14 uses: ``spacy.util.get_minor_version(
"3.8.14") == "3.8"``. Prereleases are the one exception — they are keyed by
their full version.
"""
from __future__ import annotations

import io
import json
import stat
import sys
import types
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip(
    "spacy",
    reason="spacy removed from [graph] extra by BE-6; this file tests spacy-specific "
    "provisioning logic slated for deletion at BE-15",
)

# Bound at module import (collection time), BEFORE any test fixture can install a
# spaCy stub into sys.modules. Resolving this lazily inside _make_spacy_stub made
# the fixture inherit whatever the worker's sys.modules happened to hold, which
# turned these tests into an ordering-dependent flake under -n 8 (2026-08-19-030).
import spacy.util as _REAL_SPACY_UTIL

from archon_search.install import (
    SPACY_COMPATIBILITY_URL,
    SPACY_MODEL_NAME,
    SPACY_MODEL_WHEEL_URL,
    InstallError,
    _download_spacy_model,
)

_SPACY_VERSION = "3.8.14"
_SPACY_MINOR = "3.8"
_MODEL_VERSION = "3.8.0"


def _make_model_wheel(version: str = _MODEL_VERSION) -> bytes:
    """Return a byte-for-byte plausible model wheel (a zip, per PEP 427)."""
    buffer = io.BytesIO()
    inner = f"{SPACY_MODEL_NAME}/{SPACY_MODEL_NAME}-{version}"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{SPACY_MODEL_NAME}/__init__.py", "")
        archive.writestr(f"{inner}/config.cfg", "[nlp]\nlang = 'en'\n")
        archive.writestr(f"{inner}/meta.json", json.dumps({"version": version}))
    return buffer.getvalue()


def _make_urlopen(
    wheel_bytes: bytes | None = None,
    compatibility: dict | None = None,
    wheel_error: Exception | None = None,
    compatibility_error: Exception | None = None,
    compatibility_body: bytes | None = None,
):
    """Return a urlopen replacement serving the compatibility table and the wheel."""
    if wheel_bytes is None:
        wheel_bytes = _make_model_wheel()
    if compatibility is None:
        # Real shape: keyed by MAJOR.MINOR, never the full patch version.
        compatibility = {"spacy": {_SPACY_MINOR: {SPACY_MODEL_NAME: [_MODEL_VERSION]}}}

    def _urlopen(url: str, timeout: float | None = None):
        if url == SPACY_COMPATIBILITY_URL:
            if compatibility_error is not None:
                raise compatibility_error
            if compatibility_body is not None:
                return io.BytesIO(compatibility_body)
            return io.BytesIO(json.dumps(compatibility).encode())
        if wheel_error is not None:
            raise wheel_error
        assert url == SPACY_MODEL_WHEEL_URL.format(
            name=SPACY_MODEL_NAME, version=_MODEL_VERSION
        ), f"unexpected wheel URL: {url}"
        return io.BytesIO(wheel_bytes)

    return _urlopen


def _make_spacy_stub(
    load_calls: list[str],
    load_error: Exception | None = None,
    spacy_version: str = _SPACY_VERSION,
):
    """A spaCy stub recording every ``spacy.load()`` path the code smoke-loads.

    ``get_minor_version`` / ``is_prerelease_version`` are bound to the REAL
    ``spacy.util`` implementations (not reimplemented here) so the fixture
    stays honest about what the compatibility-table lookup key actually is —
    verified once, directly against the vendored spaCy, in
    ``test_resolve_spacy_model_version_matches_real_spacy_util`` below.
    """
    def _load(name: str):
        load_calls.append(name)
        if load_error is not None:
            raise load_error
        return object()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: []  # type: ignore[attr-defined]
    fake_util.get_minor_version = _REAL_SPACY_UTIL.get_minor_version  # type: ignore[attr-defined]
    fake_util.is_prerelease_version = _REAL_SPACY_UTIL.is_prerelease_version  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.__version__ = spacy_version  # type: ignore[attr-defined]
    fake_spacy.load = _load  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]

    return {"spacy": fake_spacy, "spacy.util": fake_util}


def test_resolve_spacy_model_version_matches_real_spacy_util() -> None:
    """Ground truth: the vendored spaCy keys its own compatibility lookup by
    MAJOR.MINOR for a release, and by the full version for a prerelease.

    This is the fact the whole fix (2026-08-19-030 C1-I-1) rests on — verified
    directly against ``.venv``, not against our own mock.
    """
    import spacy
    import spacy.util

    # Deliberately NOT `spacy.__version__ == _SPACY_VERSION`: a routine
    # `uv lock --upgrade` to 3.8.15 would red-fail the suite with something that
    # looks like a spaCy regression but is fixture drift (C2-B-13). The claim
    # this test actually makes — that the table key is major.minor — holds for
    # any installed patch release, so assert THAT against the live library.
    assert spacy.__version__.startswith("3.8."), (
        f"these tests assume a spaCy 3.8.x line; .venv has {spacy.__version__}"
    )
    assert spacy.util.get_minor_version(spacy.__version__) == "3.8"
    assert spacy.util.get_minor_version(_SPACY_VERSION) == _SPACY_MINOR
    assert spacy.util.is_prerelease_version(_SPACY_VERSION) is False
    assert spacy.util.is_prerelease_version("3.9.0a1") is True


def test_download_spacy_model_places_and_smoke_loads(tmp_path: Path) -> None:
    """The wheel is unpacked, placed under the data dir, and loaded before success."""
    load_calls: list[str] = []
    models_dir = tmp_path / "models" / "spacy"

    with patch.dict(sys.modules, _make_spacy_stub(load_calls)):
        with patch("urllib.request.urlopen", _make_urlopen()):
            target = _download_spacy_model(models_dir)

    assert target == models_dir / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}"
    assert (target / "config.cfg").is_file(), "the inner model directory must be placed"
    assert not (target / "__init__.py").exists(), "the wheel's package wrapper must be dropped"
    assert load_calls == [str(target)], (
        f"the placed model must be smoke-loaded before success; got {load_calls}"
    )


def test_download_spacy_model_creates_dir_with_mode_700(tmp_path: Path) -> None:
    """Created models_dir must have mode 0o700 (mkdir's `mode=` is a no-op for
    parents=True intermediates, so this must be set explicitly)."""
    models_dir = tmp_path / "models" / "spacy"
    assert not models_dir.exists()

    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen()):
            _download_spacy_model(models_dir)

    mode = oct(stat.S_IMODE(models_dir.stat().st_mode))
    assert mode == oct(0o700), f"Expected 0o700, got {mode}"


def test_download_spacy_model_is_resolvable_by_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closes the loop: what the wizard places is what `find_spacy_model()` returns."""
    from archon_search.graph_extractor import find_spacy_model
    from archon_search.paths import get_spacy_models_dir

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    load_calls: list[str] = []
    stub = _make_spacy_stub(load_calls)

    with patch.dict(sys.modules, stub):
        with patch("urllib.request.urlopen", _make_urlopen()):
            target = _download_spacy_model(get_spacy_models_dir())
        assert find_spacy_model() == str(target)


def test_download_spacy_model_pins_version_to_installed_spacy(tmp_path: Path) -> None:
    """An unlisted spaCy MINOR version is an InstallError, never a guessed model version."""
    compatibility = {"spacy": {"4.1": {SPACY_MODEL_NAME: ["4.1.0"]}}}  # spaCy stub is 3.8.14 -> minor "3.8", absent

    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen(compatibility=compatibility)):
            with pytest.raises(InstallError, match="no en_core_web_sm release is listed"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_does_not_key_by_full_version(tmp_path: Path) -> None:
    """Regression guard for C1-I-1: a table keyed by the FULL spaCy version
    (the pre-fix, wrong shape — upstream never publishes this) must NOT be
    found. Only the MAJOR.MINOR key spaCy's own compatibility.json uses may
    resolve a model version.
    """
    compatibility = {"spacy": {_SPACY_VERSION: {SPACY_MODEL_NAME: [_MODEL_VERSION]}}}

    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen(compatibility=compatibility)):
            with pytest.raises(InstallError, match="no en_core_web_sm release is listed"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_prerelease_keys_by_full_version(tmp_path: Path) -> None:
    """A prerelease spaCy version is looked up by its FULL version, not its
    minor — mirrors ``spacy.cli.download.get_compatibility()`` exactly.
    """
    prerelease = "3.9.0a1"
    compatibility = {"spacy": {prerelease: {SPACY_MODEL_NAME: [_MODEL_VERSION]}}}
    models_dir = tmp_path / "models" / "spacy"

    with patch.dict(sys.modules, _make_spacy_stub([], spacy_version=prerelease)):
        with patch("urllib.request.urlopen", _make_urlopen(compatibility=compatibility)):
            target = _download_spacy_model(models_dir)

    assert target == models_dir / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}"


def test_download_spacy_model_rejects_malformed_model_version(tmp_path: Path) -> None:
    """A malformed version string from the (remote) compatibility table is an
    InstallError — it must never be interpolated unchecked into a download URL
    or a filesystem path join.
    """
    compatibility = {"spacy": {_SPACY_MINOR: {SPACY_MODEL_NAME: ["../../etc/passwd"]}}}

    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen(compatibility=compatibility)):
            with pytest.raises(InstallError, match="not a well-formed dotted version"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_skips_when_already_provisioned(tmp_path: Path) -> None:
    """A present pinned version means no wheel fetch."""
    models_dir = tmp_path / "spacy"
    target = models_dir / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}"
    target.mkdir(parents=True)
    (target / "config.cfg").write_text("[nlp]\n")

    urlopen = MagicMock(side_effect=_make_urlopen())
    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", urlopen):
            assert _download_spacy_model(models_dir) == target

    fetched = [c.args[0] for c in urlopen.call_args_list]
    assert fetched == [SPACY_COMPATIBILITY_URL], (
        f"only the compatibility table may be fetched; got {fetched}"
    )


def test_download_spacy_model_replaces_rubble_target(tmp_path: Path) -> None:
    """A partial/rubble target dir (no config.cfg, e.g. from an interrupted
    prior run) is replaced, not merged into — ``shutil.move`` moves INTO an
    existing directory instead of replacing it, which would otherwise produce
    a nested, unloadable layout.
    """
    models_dir = tmp_path / "spacy"
    target = models_dir / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}"
    target.mkdir(parents=True)
    (target / "junk.txt").write_text("leftover from an interrupted run")

    load_calls: list[str] = []
    with patch.dict(sys.modules, _make_spacy_stub(load_calls)):
        with patch("urllib.request.urlopen", _make_urlopen()):
            result = _download_spacy_model(models_dir)

    assert result == target
    assert (target / "config.cfg").is_file()
    assert not (target / "junk.txt").exists(), "rubble must be replaced, not merged into"
    assert not (target / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}").exists(), (
        "must not have nested inside the old directory"
    )


def test_download_spacy_model_network_failure_raises_install_error(tmp_path: Path) -> None:
    """A failed wheel fetch is an InstallError — the caller warns and continues."""
    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch(
            "urllib.request.urlopen",
            _make_urlopen(wheel_error=urllib.error.URLError("no route to host")),
        ):
            with pytest.raises(InstallError, match="failed to fetch en_core_web_sm"):
                _download_spacy_model(tmp_path / "spacy")

    assert not (tmp_path / "spacy" / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}").exists()


def test_download_spacy_model_compatibility_fetch_failure_raises_install_error(
    tmp_path: Path,
) -> None:
    """A failed fetch of the compatibility table itself is an InstallError."""
    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch(
            "urllib.request.urlopen",
            _make_urlopen(compatibility_error=urllib.error.URLError("no route to host")),
        ):
            with pytest.raises(InstallError, match="could not read the spaCy compatibility table"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_malformed_compatibility_body_raises_install_error(
    tmp_path: Path,
) -> None:
    """A non-JSON compatibility table body is an InstallError, not an unhandled
    ``json.JSONDecodeError``."""
    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch(
            "urllib.request.urlopen",
            _make_urlopen(compatibility_body=b"not json"),
        ):
            with pytest.raises(InstallError, match="could not read the spaCy compatibility table"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_bad_zip_raises_install_error(tmp_path: Path) -> None:
    """A downloaded wheel that is not a valid zip is an InstallError, not an
    unhandled ``zipfile.BadZipFile``."""
    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen(wheel_bytes=b"not a zip file")):
            with pytest.raises(InstallError, match="failed to fetch en_core_web_sm"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_unexpected_wheel_layout_raises_install_error(
    tmp_path: Path,
) -> None:
    """A wheel missing the expected inner ``en_core_web_sm-<ver>/config.cfg``
    is an InstallError — the guard against upstream changing the wheel layout,
    the single most likely future breakage of this whole path.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{SPACY_MODEL_NAME}/__init__.py", "")  # no inner model dir at all

    with patch.dict(sys.modules, _make_spacy_stub([])):
        with patch("urllib.request.urlopen", _make_urlopen(wheel_bytes=buffer.getvalue())):
            with pytest.raises(InstallError, match="unexpected layout"):
                _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_spacy_not_importable_raises_install_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spaCy import failure (e.g. ``[graph]`` extra not installed) is an
    InstallError — reachable because ``importlib.invalidate_caches()`` admits
    the import may not have landed yet.
    """
    monkeypatch.setitem(sys.modules, "spacy", None)
    with pytest.raises(InstallError, match="spaCy is not importable"):
        _download_spacy_model(tmp_path / "spacy")


def test_download_spacy_model_removes_unloadable_model(tmp_path: Path) -> None:
    """A placed-but-unloadable model is removed, not reported as success."""
    models_dir = tmp_path / "spacy"
    stub = _make_spacy_stub([], load_error=RuntimeError("incompatible model"))

    with patch.dict(sys.modules, stub):
        with patch("urllib.request.urlopen", _make_urlopen()):
            with pytest.raises(InstallError, match="failed to load"):
                _download_spacy_model(models_dir)

    assert not (models_dir / f"{SPACY_MODEL_NAME}-{_MODEL_VERSION}").exists(), (
        "an unloadable model must not be left behind for the runtime to resolve"
    )


def test_download_spacy_model_smoke_load_is_real(tmp_path: Path) -> None:
    """Proves the smoke-load genuinely loads a spaCy pipeline, not a stub.

    Uses the real ``en_core_web_sm`` dev dependency (pyproject.toml
    ``[tool.uv.sources]``, pinned to 3.8.0 — matching ``_MODEL_VERSION`` above)
    as the source of a byte-for-byte real model directory, packaged into the
    wheel shape ``_download_spacy_model`` expects. Only network access is
    mocked — ``spacy`` itself is the real, installed module, so
    ``spacy.load(str(target))`` inside the function under test does a real
    load, not a structurally-verified stub call.
    """
    en_core_web_sm = pytest.importorskip("en_core_web_sm")
    import spacy
    from spacy.language import Language

    version = en_core_web_sm.__version__
    assert version == _MODEL_VERSION, (
        "the dev dependency's pinned version drifted from the wheel-URL "
        f"fixture; update _MODEL_VERSION to match (got {version})"
    )
    model_dir = Path(en_core_web_sm.__file__).parent / f"{SPACY_MODEL_NAME}-{version}"

    buffer = io.BytesIO()
    inner = f"{SPACY_MODEL_NAME}/{SPACY_MODEL_NAME}-{version}"
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in model_dir.rglob("*"):
            if path.is_file():
                archive.write(path, f"{inner}/{path.relative_to(model_dir)}")

    models_dir = tmp_path / "models" / "spacy"
    with patch("urllib.request.urlopen", _make_urlopen(wheel_bytes=buffer.getvalue())):
        target = _download_spacy_model(models_dir)

    assert target == models_dir / f"{SPACY_MODEL_NAME}-{version}"
    nlp = spacy.load(str(target))
    assert isinstance(nlp, Language)


def test_en_core_web_sm_dev_dependency_is_installed() -> None:
    """C2-T-11: `test_download_spacy_model_smoke_load_is_real` — the only test
    that loads a genuine spaCy pipeline — opens with `importorskip`. If the
    dev-group URL source ever stops resolving, that guarantee would vanish with
    a green suite. Fail loudly here instead of disarming silently there.
    """
    import importlib.util

    assert importlib.util.find_spec("en_core_web_sm") is not None, (
        "the en_core_web_sm dev dependency is missing, which silently skips the "
        "only real spaCy smoke-load in the suite; see pyproject.toml "
        "[tool.uv.sources] and re-run `uv sync --dev`"
    )

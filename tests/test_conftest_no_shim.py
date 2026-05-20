"""Tests that conftest.py uses _search_stubs directly, with no inline stub definitions."""
from __future__ import annotations

import pathlib

_CONFTEST = pathlib.Path(__file__).parent / "conftest.py"


def _src() -> str:
    return _CONFTEST.read_text()


def test_no_shim_import() -> None:
    assert "_search_stubs_shim" not in _src(), "conftest.py must not import _search_stubs_shim"


def test_no_fake_text_embedding_class() -> None:
    assert "_FakeTextEmbedding" not in _src(), "conftest.py must not define _FakeTextEmbedding"


def test_no_fake_text_cross_encoder_class() -> None:
    assert "_FakeTextCrossEncoder" not in _src(), "conftest.py must not define _FakeTextCrossEncoder"


def test_uses_search_stubs_import() -> None:
    assert "from _search_stubs import install_stubs" in _src(), (
        "conftest.py must import install_stubs from _search_stubs"
    )


def test_install_stubs_is_called() -> None:
    assert "install_stubs()" in _src(), "conftest.py must call install_stubs()"

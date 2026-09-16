"""S274 regression — a ``.py`` ingest must surface symbol metadata or a warning.

Reproduces bug S274 (reported against 26.9.2111): ingesting a Python source
file and then searching it should yield EITHER

* (a) ``code`` extra installed  → at least one search result whose ``metadata``
  dict carries ``_symbol_type``; OR
* (b) ``code`` extra NOT installed → a non-empty ``warnings`` list on the ingest
  job body noting the missing extra.

Both outcomes are correct — the test passes on whichever branch matches the
runtime, but never vacuously (it requires a completed job body and at least one
search result before checking either branch).

This follows the ticket's reproduction verbatim: the ``/search`` request carries
``include_metadata`` as a TOP-LEVEL field, exactly as a client would send it per
the reported steps.

Run with:
    uv run pytest tests/integration/test_s274_code_metadata_or_warning.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _code_extra_installed() -> bool:
    """True when the tree-sitter Python grammar (``[code]`` extra) is importable.

    Mirrors the runtime dependency ``CodeEnricher`` needs to emit ``_symbol_type``
    (see ``archon_search/code_enricher.py::_get_grammar``).
    """
    try:
        import tree_sitter_python  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        return False
    return True


def test_py_ingest_yields_symbol_metadata_or_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    py_file = tmp_path / "example.py"
    py_file.write_text(
        "def greet(name):\n"
        "    return f'hello {name}'\n"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        col = "code_test"

        # 1-3. Ingest example.py and wait for the ingest job to complete.
        job_id = ingest_file_via_path(client, col, str(py_file), api_key=api_key)

        job_resp = client.get(f"/jobs/{job_id}", headers=headers)
        assert job_resp.status_code == 200, f"job fetch failed: {job_resp.text}"
        job_body = job_resp.json()["result"]
        assert job_body is not None, "ingest job body must not be null (job did not complete)"
        warnings = job_body.get("warnings") or []

        # 4. POST /search exactly as the ticket reports it — include_metadata is a
        #    top-level field on the request body.
        search_resp = client.post(
            "/search",
            json={"collection": col, "query": "greet", "include_metadata": True},
            headers=headers,
        )
        assert search_resp.status_code == 200, (
            f"POST /search returned {search_resp.status_code}, body: {search_resp.text}"
        )
        results = search_resp.json()["results"]
        assert results, "expected at least one search result for the 'greet' query"

        symbol_metadata_present = any(
            "_symbol_type" in (item.get("metadata") or {}) for item in results
        )

        # Anchor each branch explicitly so neither can pass vacuously.
        if _code_extra_installed():
            # Branch (a): code-aware chunking must surface symbol metadata.
            assert symbol_metadata_present, (
                "code extra installed: expected at least one search result with "
                "'_symbol_type' in its metadata dict, but got metadata="
                f"{[item.get('metadata') for item in results]}"
            )
        else:
            # Branch (b): with no grammar, the ingest must warn about the missing extra.
            assert warnings, (
                "code extra not installed: expected a non-empty ingest 'warnings' "
                f"list noting the missing extra, but got warnings={warnings!r}"
            )

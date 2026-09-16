"""S274 regression — missing ``[code]`` grammar must warn at ingest time.

Companion to :mod:`test_s274_code_metadata_or_warning`, which passes on
*whichever* branch matches the runtime. That test's branch (b) — no grammar
installed → the ingest job body carries a ``warnings`` list — is otherwise
untestable in a dev/CI runtime that *has* the tree-sitter Python grammar. This
test forces the missing-grammar path deterministically so branch (b) is not dead
in CI.

It monkeypatches ``code_enricher._get_grammar`` to record the extension as
missing (mirroring the real missing-grammar branch at
``code_enricher.py::_get_grammar``) and return ``None``, ingests a ``.py`` file,
and asserts the completed ingest job warns about the missing ``[code]`` extra.

Run with:
    uv run pytest tests/integration/test_s274_missing_code_extra_warning.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

import archon_search.code_enricher as code_enricher
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def test_missing_code_grammar_emits_ingest_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Snapshot the process-global grammar caches so the forced missing-ext does
    # not leak into other tests in this worker.
    saved_logged = set(code_enricher._GRAMMAR_LOGGED)
    saved_cache = dict(code_enricher._GRAMMAR_CACHE)

    def _fake_get_grammar(ext: str):
        # Mirror the real missing-grammar branch: record the ext, return None.
        code_enricher._GRAMMAR_LOGGED.add(ext)
        return None

    monkeypatch.setattr(code_enricher, "_get_grammar", _fake_get_grammar)

    try:
        py_file = tmp_path / "example.py"
        py_file.write_text(
            "def greet(name):\n"
            "    return f'hello {name}'\n"
        )

        with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
            headers = {"Authorization": f"Bearer {api_key}"}
            col = "code_test"

            job_id = ingest_file_via_path(client, col, str(py_file), api_key=api_key)

            job_resp = client.get(f"/jobs/{job_id}", headers=headers)
            assert job_resp.status_code == 200, f"job fetch failed: {job_resp.text}"
            job_body = job_resp.json()["result"]
            assert job_body is not None, "ingest job body must not be null (job did not complete)"

            warnings = job_body.get("warnings") or []
            assert warnings, (
                "missing [code] grammar: expected a non-empty ingest 'warnings' list, "
                f"but got warnings={warnings!r}"
            )
            assert any(
                "example.py" in w and ".py" in w and "code" in w.lower()
                for w in warnings
            ), f"expected a warning naming the missing [code] extra, got warnings={warnings!r}"
    finally:
        code_enricher._GRAMMAR_LOGGED.clear()
        code_enricher._GRAMMAR_LOGGED.update(saved_logged)
        code_enricher._GRAMMAR_CACHE.clear()
        code_enricher._GRAMMAR_CACHE.update(saved_cache)

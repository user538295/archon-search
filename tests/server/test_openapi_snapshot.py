"""Snapshot test for the OpenAPI spec.

On first run (no baseline): fails with instructions to generate it.
With --update-openapi-snapshot flag: writes/overwrites the baseline and passes.
On subsequent runs: fails if the spec differs from the committed baseline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

SNAPSHOT_PATH = Path(__file__).parent / "openapi_snapshot.json"
UPDATE_FLAG = "--update-openapi-snapshot"

# CPython 3.13 renamed the 422 reason phrase from "Unprocessable Entity" to
# "Unprocessable Content", and FastAPI derives auto-generated response descriptions
# from ``http.HTTPStatus``. The phrase is prose, not contract (the status code and the
# error schema are), so both spellings are folded onto one value — otherwise the
# snapshot could only ever match the interpreter it was generated on (CI pins 3.12,
# while ``requires-python`` allows 3.13+).
_CANONICAL_422_DESCRIPTION = "Unprocessable Entity"
_422_DESCRIPTION_ALIASES = frozenset({"Unprocessable Entity", "Unprocessable Content"})


def _strip_dynamic_fields(spec: dict) -> dict:  # type: ignore[type-arg]
    """Remove or canonicalise fields that change per build (dynamic CalVer version) or
    per interpreter (the HTTP 422 reason phrase) so the snapshot stays stable."""
    info = spec.get("info")
    if isinstance(info, dict):
        info.pop("version", None)
    for path_item in (spec.get("paths") or {}).values():
        # A path item also holds non-operation keys ("parameters", "summary"), hence the
        # isinstance guards rather than a bare .get() chain.
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            response_422 = responses.get("422") if isinstance(responses, dict) else None
            if isinstance(response_422, dict) and response_422.get("description") in _422_DESCRIPTION_ALIASES:
                response_422["description"] = _CANONICAL_422_DESCRIPTION
    return spec


def test_openapi_spec_matches_snapshot(tmp_path: Path, pytestconfig: pytest.Config) -> None:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    spec = _strip_dynamic_fields(app.openapi())

    if pytestconfig.getoption(UPDATE_FLAG.lstrip("-").replace("-", "_"), default=False):
        SNAPSHOT_PATH.write_text(json.dumps(spec, indent=2, sort_keys=True))
        return

    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            f"OpenAPI snapshot missing. Run with {UPDATE_FLAG} to generate:\n"
            f"  uv run pytest tests/server/test_openapi_snapshot.py {UPDATE_FLAG}"
        )

    baseline = _strip_dynamic_fields(json.loads(SNAPSHOT_PATH.read_text()))
    assert spec == baseline, (
        f"OpenAPI spec changed. If intentional, regenerate with:\n"
        f"  uv run pytest tests/server/test_openapi_snapshot.py {UPDATE_FLAG}"
    )

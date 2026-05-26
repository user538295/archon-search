"""CI guard: server modules (except app.py) must not access app.state.embedder or app.state.reranker directly.

Route handlers must access warm-status via the pipeline seam:
  app.state.pipeline.embedder_is_warm  ✓
  app.state.pipeline.reranker_is_warm  ✓
  app.state.embedder.is_warm           ✗  (bypasses the pipeline abstraction)
  app.state.reranker.is_warm           ✗  (bypasses the pipeline abstraction)

Pattern mirrors tests/test_no_fstring_sql.py.
"""
from __future__ import annotations

from pathlib import Path


def test_no_app_state_model_direct_access() -> None:
    server_dir = Path(__file__).parent.parent / "archon_search" / "server"
    violations: list[str] = []
    for py_file in sorted(server_dir.glob("*.py")):
        if py_file.name == "app.py":
            continue  # app.py is the one place that wires the models in
        source = py_file.read_text()
        for forbidden in ("app.state.embedder", "app.state.reranker"):
            if forbidden in source:
                violations.append(f"{py_file.name}: contains {forbidden!r}")
    assert not violations, "Direct app.state model access found:\n" + "\n".join(violations)

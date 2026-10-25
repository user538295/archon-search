#!/usr/bin/env bash
#
# scripts/test-unlocked-deps.sh — the unlocked-dependency lane (transitive-drift guard).
#
# Installs archon-search the way end users get it — the newest versions its published
# metadata allows, uv.lock ignored — then runs the test lanes archon-search-release.yml's
# `test` job gates publish on. `pip install archon-search`, `uv tool install archon-search`
# and the Dockerfile's `pip install .` never read uv.lock; every other CI job installs it
# (`uv sync --dev`). S671 reached users that way: the lock pinned huggingface_hub 1.26.0
# while their installs resolved 1.33.
#
# Resolution = the wheel's own metadata ([project] dependencies + extras):
#   --no-config    ignores [tool.uv] constraint-dependencies — uv-only, not in the wheel,
#                  so no end-user install applies them.
#   --no-sources   ignores [tool.uv.sources] (the torch CPU-index pin), for the same reason.
#   --torch-backend cpu on linux (x86_64 and aarch64) — PyPI's linux torch is the CUDA build on
#                  either arch (it pulls the nvidia/cuda wheels a CPU runner cannot use); this
#                  resolves the `+cpu` build the Docker image ships (S269). macOS and Windows keep
#                  PyPI torch, which is already CPU-only, as [tool.uv.sources] does.
# The dev group supplies pytest & co. and pulls every extra through its
# `archon-search[...]` self-reference, which resolves to this checkout.
#
# Usage: bash scripts/test-unlocked-deps.sh [install|suite|docling|graph|all]   (default: all)
#   install  fresh venv + unlocked install + versions that differ from uv.lock
#   suite    the full default suite (pyproject addopts: integration + eval, 85% coverage)
#   docling  the real docling/RapidOCR lane, with its non-vacuity check
#   graph    the real GLiNER lane (artifact prefetch, lane, non-vacuity check)
# Every lane also fails if it changed the venv it tests (see assert_venv_unchanged).
# Env:
#   ARCHON_UNLOCKED_DIR     work dir for the venv, basetemp and JUnit files
#                           (default /var/tmp/archon-search-unlocked). Keep it disk-backed:
#                           the crash-injection integration tests skip on tmpfs.
#   ARCHON_SEARCH_DATA_DIR  data dir whose models/graph/ holds or receives the pinned GLiNER
#                           checkpoint (graph lane only; default $HOME/.archon-search).
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORK_DIR="${ARCHON_UNLOCKED_DIR:-/var/tmp/archon-search-unlocked}"
VENV="$WORK_DIR/venv"
PY="$VENV/bin/python"
# Post-install `uv pip freeze`. Inside the venv, so `uv venv --clear` drops a stale one and a
# failed install never leaves a snapshot behind for the lane steps to trust.
SNAPSHOT="$VENV/unlocked-freeze.txt"
PYTHON_VERSION="3.12"  # the version every workflow and the Dockerfile pin
PREFETCH_ATTEMPTS=3

# Same `-o addopts=` prefix as the release workflow's lane steps.
LANE_PYTEST=(-m pytest -o addopts= --strict-markers --strict-config --no-cov -n0)

die() {
    echo "test-unlocked-deps.sh: $*" >&2
    exit 1
}

require_venv() {
    [ -x "$PY" ] && [ -f "$SNAPSHOT" ] \
        || die "no completed unlocked install in $WORK_DIR — run the 'install' step first"
}

freeze() {
    uv pip freeze --no-config --python "$PY"
}

# Tests that run a real package install re-resolve the running environment (with the
# repo's [tool.uv] constraints, from the repo root) and would silently swap the versions
# under test — five did, until the unlocked lane first ran.
assert_venv_unchanged() {
    local now
    now="$(freeze)"
    if [ "$now" != "$(cat "$SNAPSHOT")" ]; then
        diff "$SNAPSHOT" - <<<"$now" >&2 || true
        die "the lane changed the venv under test (a package install ran) — these results do not describe the unlocked resolution"
    fi
}

install() {
    mkdir -p "$WORK_DIR"
    uv venv --no-config --clear --python "$PYTHON_VERSION" "$VENV"
    local args=(--no-config --no-sources --resolution highest --python "$PY")
    if [ "$(uname -s)" = Linux ]; then
        args+=(--torch-backend cpu)
    fi
    uv pip install "${args[@]}" . --group dev
    freeze >"$SNAPSHOT.partial"
    # The dev group's `archon-search[...]` self-reference must resolve to this checkout:
    # a PyPI archon-search here would test the last release, not this tree.
    grep -q '^archon-search @ file://' "$SNAPSHOT.partial" \
        || die "archon-search was not installed from this checkout — see $SNAPSHOT.partial"
    mv "$SNAPSHOT.partial" "$SNAPSHOT"
    report_drift || echo "test-unlocked-deps.sh: drift report failed (informational only)" >&2
}

# Lists every installed distribution whose version uv.lock does not pin, so a red lane
# names its suspects up front. Informational: it never fails the lane.
report_drift() {
    "$PY" - <<'PY'
import importlib.metadata as md
import re
import tomllib


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


with open("uv.lock", "rb") as fp:
    lock = tomllib.load(fp)["package"]
locked: dict[str, set[str]] = {}
for pkg in lock:
    if "version" in pkg:  # the editable project entry carries no version
        locked.setdefault(canonical(pkg["name"]), set()).add(pkg["version"])

dists = {
    canonical(name): d.version
    for d in md.distributions()
    if (name := d.metadata["Name"])  # a broken dist-info can lack a Name
}
dists.pop("archon-search", None)
drift = {n: v for n, v in dists.items() if v not in locked.get(n, set())}
print(f"Unlocked install: {len(drift)} of {len(dists)} distributions differ from uv.lock")
for name in sorted(drift):
    pinned = ", ".join(sorted(locked.get(name, set()))) or "not locked"
    print(f"  {name}: {pinned} -> {drift[name]}")
PY
}

suite() {
    require_venv
    # COVERAGE_FILE keeps this run's data out of ./.coverage, which release.sh's own
    # --cov-append lanes accumulate into before their final fail-under check.
    # The -m filter is pyproject addopts' own plus `not benchmark`, as the release `test`
    # job deselects it: latency ceilings measured under `-n 8` on a 4-vCPU runner fail on
    # CPU contention, not on drift. release.sh lane 7 runs the benchmarks serially.
    COVERAGE_FILE="$WORK_DIR/.coverage" "$PY" -m pytest --basetemp="$WORK_DIR/pytest-suite" \
        -m "not live_benchmark and not smoke and not live_eval and not docling and not graph_real_artifact and not benchmark"
    assert_venv_unchanged
}

docling() {
    require_venv
    local results="$WORK_DIR/docling-results.xml"
    "$PY" "${LANE_PYTEST[@]}" -m docling --junitxml="$results" tests/
    # The release workflow's non-vacuity check: the two skip-prone lane tests must run.
    "$PY" - "$results" <<'PY'
import sys
import xml.etree.ElementTree as ET

cases = {c.get("name"): c for c in ET.parse(sys.argv[1]).getroot().iter("testcase")}
names = ["test_pdf_page_number_in_search_response", "test_image_file_assigns_page_start_one"]
missing = [n for n in names if n not in cases]
assert not missing, f"expected docling testcases not found - {missing} - the lane did not run"
skipped = [n for n in names if cases[n].find("skipped") is not None]
assert not skipped, f"docling testcases skipped, no real OCR exercised - {skipped}"
PY
    assert_venv_unchanged
}

graph() {
    require_venv
    export ARCHON_SEARCH_DATA_DIR="${ARCHON_SEARCH_DATA_DIR:-$HOME/.archon-search}"
    local attempt
    for attempt in $(seq "$PREFETCH_ATTEMPTS"); do
        "$PY" - <<'PY' && break
import asyncio

from archon_search.prose_extraction_backend import ProseExtractionBackend

backend = ProseExtractionBackend()
asyncio.run(backend.load())
assert backend.load_count == 1, backend.load_count
print("Graph NER artifact prefetched")
PY
        [ "$attempt" -lt "$PREFETCH_ATTEMPTS" ] || die "graph NER artifact prefetch failed after $PREFETCH_ATTEMPTS attempts"
        echo "Attempt $attempt failed; retrying in $((attempt * 5))s..."
        sleep $((attempt * 5))
    done
    local results="$WORK_DIR/graph-real-artifact-results.xml"
    "$PY" "${LANE_PYTEST[@]}" -m graph_real_artifact --junitxml="$results" tests/
    # The release workflow's non-vacuity check on the lane's blocking test.
    "$PY" - "$results" <<'PY'
import sys
import xml.etree.ElementTree as ET

c = [t for t in ET.parse(sys.argv[1]).getroot().iter("testcase") if t.get("name") == "test_graph_ner_lane_non_vacuity"]
assert len(c) == 1, f"expected exactly one test_graph_ner_lane_non_vacuity testcase, found {len(c)} - the lane did not run"
assert c[0].find("skipped") is None, "test_graph_ner_lane_non_vacuity was skipped - the real-artifact guard did not execute"
PY
    assert_venv_unchanged
}

case "${1:-all}" in
    install) install ;;
    suite) suite ;;
    docling) docling ;;
    graph) graph ;;
    # One step per line: inside an `&&` list, `set -e` stops applying within the functions.
    all) install; suite; docling; graph ;;
    *) die "unknown step '$1' — expected install, suite, docling, graph or all" ;;
esac

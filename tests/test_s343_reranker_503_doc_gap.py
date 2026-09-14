"""S343 doc-gap reopening gate.

Ticket: ``Documentation/Backlog/S343-reranker_503_mapping_is_undocumented.md``.

S343 shipped a positive-path proxy (`tests/integration/test_s343_reranker_score_in_search.py`,
which asserts a non-null ``reranker_score``) because no shipped document maps reranker readiness
to a ``503``. The ticket pairs that proxy with a reopening gate: **no shipped document may claim
``/explain`` answers ``503`` because the reranker is unavailable.** The moment a document
introduces that mapping, this test flips red so S343 is re-implemented against the then-documented
status and trigger instead of the proxy.

Scope, and why it is this narrow:

* ``Backlog/`` and ``Completed/`` are tickets and archives, not shipped documentation. S343's own
  ticket body quotes the offending phrase as its subject matter, so scanning them makes the gate
  permanently and vacuously red.
* Shipped documentation is not only ``Documentation/`` — ``README.md``, ``BREAKING.md`` and
  ``archon-search.toml.example`` all document endpoints, and a per-directory scope would orphan them.
* The claim is ``/explain``-specific. ``GET /ready`` legitimately answers ``503`` while the startup
  warm-up — which covers the reranker cross-encoder — is still running (``routes_ready.py`` gates
  ``ready_flag`` on ``warmup_pending``), and several shipped pages correctly document exactly that.
  It is not what S343 is about.
* ``/explain`` context comes from the line, any enclosing heading, or the file name — a status table
  under ``### `routes_explain.py` `` does not repeat the endpoint on every row. Enclosing means the
  whole heading stack: a ``#### Error responses`` nested under that section must not shed it. The
  match tolerates a leading ``_`` because ``routes_explain.py`` offers no word boundary. Inside a
  fenced block a ``#`` line is a shell comment, not a heading, so heading tracking is suspended
  there — offender scanning is not, because a documented error body is still a claim.
* The unit of judgement is a sentence, not a line. ``OperatorGuide/90_incident_runbook.md`` names
  ``POST /explain``, the reranker and ``503`` within one very long line while asserting none of them
  of each other, and ``Architecture/600_api_reference_or_public_interface.md`` contrasts "503 … is
  reserved for meta-lookup / router failures" against "Pipeline-stage failures (store, reranker)
  surface as 500" in adjacent sentences. Splitting on sentence boundaries keeps both green. The
  split is deliberately **periods only**: a colon or semicolon is how a status mapping is normally
  written ("``503``: the reranker is unavailable"), so splitting on those would let the guarded
  claim through in its most likely form.

``test_detector_flags_a_documented_explain_503_reranker_mapping`` is what keeps the gate honest — a
guard asserting an absence passes just as happily when its matcher is broken.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_ROOT = _REPO_ROOT / "Documentation"
# Tickets and archives discuss the guard itself; they are not the shipped contract.
_EXCLUDED_DIRS = ("Backlog", "Completed")
# Shipped documentation living outside Documentation/.
_EXTRA_SHIPPED_DOCS = ("README.md", "BREAKING.md", "archon-search.toml.example")

_HEADING = re.compile(r"^(#{1,6})\s")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# Periods only — see the module docstring.
_SENTENCE_SPLIT = re.compile(r"\.\s")
# ``_explain`` is needed because the heading that opens the endpoint's reference section is
# ``### `routes_explain.py` ``, where a leading word boundary never fires. The trailing
# ``(?![a-z])`` drops the English verb — "…nothing that explains the warn" in the incident runbook
# would otherwise pull a whole `GET /ready` warm-up paragraph into scope and fire a false red.
_EXPLAIN = re.compile(r"/explain|_explain|\bexplain(?![a-z])")
_RERANK = re.compile(r"re-?rank|cross[- ]encoder")
_STATUS = re.compile(r"\b503\b|service unavailable")


def _shipped_docs() -> list[Path]:
    docs = [
        md
        for md in _DOCS_ROOT.rglob("*.md")
        if not any(part in _EXCLUDED_DIRS for part in md.relative_to(_DOCS_ROOT).parts)
    ]
    docs += [_REPO_ROOT / name for name in _EXTRA_SHIPPED_DOCS]
    return sorted(doc for doc in docs if doc.is_file())


def _offending_lines(doc: Path) -> list[str]:
    """Return every line in ``doc`` whose ``/explain`` context ties the reranker to a 503."""
    offenders: list[str] = []
    # Heading level -> heading text, so a nested subheading does not erase its ancestors.
    headings: dict[int, str] = {}
    fenced = False
    file_is_about_explain = bool(_EXPLAIN.search(doc.name.lower()))
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        # A shell comment inside a fenced block is not a heading, and must not clear the stack.
        marker = None if fenced else _HEADING.match(line)
        if marker:
            depth = len(marker.group(1))
            headings = {d: text for d, text in headings.items() if d < depth}
            headings[depth] = line.lower()
        lowered = line.lower()
        in_explain_context = (
            file_is_about_explain
            or _EXPLAIN.search(lowered)
            or any(_EXPLAIN.search(text) for text in headings.values())
        )
        if not in_explain_context:
            continue
        for sentence in _SENTENCE_SPLIT.split(lowered):
            if _STATUS.search(sentence) and _RERANK.search(sentence):
                offenders.append(f"{doc.relative_to(_REPO_ROOT, walk_up=True)}:{lineno}: {line.strip()}")
                break
    return offenders


def test_no_shipped_doc_maps_explain_503_to_the_reranker() -> None:
    docs = _shipped_docs()
    # Without these the gate would pass vacuously if part of the corpus moved or was renamed.
    assert [doc for doc in docs if _DOCS_ROOT in doc.parents], (
        f"no shipped documentation found under {_DOCS_ROOT}"
    )
    missing = [name for name in _EXTRA_SHIPPED_DOCS if not (_REPO_ROOT / name).is_file()]
    assert not missing, f"shipped documentation outside {_DOCS_ROOT} went missing: {missing}"

    offenders = [entry for doc in docs for entry in _offending_lines(doc)]
    assert not offenders, (
        "the docs now map an /explain 503 to the reranker — re-implement S343 against the documented "
        "status and trigger instead of this positive-path proxy:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        ("colon mapping", "`POST /explain` returns 503: the reranker is unavailable.\n"),
        ("semicolon mapping", "/explain answers 503; the reranker was not warm.\n"),
        ("plain sentence", "The /explain endpoint answers 503 when the reranker is not ready.\n"),
        ("cross-encoder wording", "/explain returns 503 while the cross-encoder is still building.\n"),
        ("spelled-out status", "/explain returns Service Unavailable when the reranker is cold.\n"),
        ("heading claim", "### POST /explain returns 503 when the reranker is down\n"),
        (
            "table row under a nested heading",
            "### POST /explain\n#### Error responses\n\n"
            "| Status | Meaning |\n|---|---|\n| 503 | The reranker cross-encoder is not ready |\n",
        ),
        (
            "module heading kept across a fenced block",
            "### `routes_explain.py`\n\n```bash\n# curl the endpoint\n```\n\n"
            "| 503 | The reranker is not ready |\n",
        ),
    ],
)
def test_detector_flags_a_documented_explain_503_reranker_mapping(
    tmp_path: Path, label: str, markdown: str
) -> None:
    """Without this, a broken matcher would leave the gate silently green forever."""
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert _offending_lines(doc), f"detector missed the {label} form of the guarded claim"


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        ("503 belongs to a different subject", "/explain 503 is a router failure. The reranker returns 500.\n"),
        ("no /explain context", "`GET /ready` returns 503 while the reranker warms up.\n"),
        ("no reranker", "/explain returns 503 when the collection meta lookup fails.\n"),
        ("unanchored digits", "/explain p95 is 1503 ms with the reranker enabled.\n"),
    ],
)
def test_detector_leaves_non_claims_alone(tmp_path: Path, label: str, markdown: str) -> None:
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert not _offending_lines(doc), f"detector false-positived on {label}"

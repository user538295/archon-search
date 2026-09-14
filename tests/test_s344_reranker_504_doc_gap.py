"""S344 doc-gap reopening gate.

Ticket: ``Documentation/Backlog/S344-reranker_504_mapping_is_undocumented.md``.

S344's subject — "``/explain`` answers ``504`` because the reranker timed out" — is not documented
anywhere. The shipped documentation says the opposite, and says it in three places:

* ``Documentation/UserManual/80_explain_and_debugging.md:169`` lists ``/explain``'s status codes and
  gives its **only** ``504`` trigger as "``504`` on fanout timeout" — nothing reranker-specific.
* ``Documentation/UserManual/60_searching.md:52-53`` maps a reranker failure to ``500``
  ("pipeline stage exception (embedder, store, or reranker raised)") and explicitly excludes the
  reranker from the ``504`` ("the timer starts after the reranker is loaded, so a cold model no
  longer causes this").
* ``Documentation/OperatorGuide/90_incident_runbook.md:91`` repeats it for operators: "A cold
  cross-encoder is **not** a cause" of the ``504``.

No ``reranker_timeout``-style knob exists either; the only search timeout setting is
``[search] fanout_timeout_seconds`` (``Documentation/OperatorGuide/80_capacity_and_performance.md:46``).

So S344 is covered by positive-path fan-out-timeout tests shipped with S435/S443
(``tests/integration/test_e1b_be6_routes_search_integration.py``, ``tests/server/test_app.py``,
``tests/server/test_routes_explain.py``), and this file is the reopening gate that pairs with them:
**no shipped document may claim ``/explain`` answers ``504`` because of the reranker.** The moment a
document introduces that mapping, this test flips red so S344 is re-implemented against the
then-documented trigger and knob instead of the proxies.

Scope, and why it is this narrow (mirrors ``tests/test_s343_reranker_503_doc_gap.py``):

* ``Backlog/`` and ``Completed/`` are tickets and archives, not shipped documentation. S344's own
  ticket body quotes the offending phrasing as its subject matter, so scanning them makes the gate
  permanently and vacuously red.
* Shipped documentation is not only ``Documentation/`` — ``README.md``, ``BREAKING.md`` and
  ``archon-search.toml.example`` all document endpoints, and a per-directory scope would orphan them.
* The claim is ``/explain``-specific. ``POST /search`` legitimately documents a ``504`` (a ~30 s
  whole-pipeline timeout, 60:53) and the incident runbook discusses it at length next to the
  reranker; that is not what S344 is about.
* ``/explain`` context comes from the line, any enclosing heading, or the file name — a status table
  under ``### `routes_explain.py` `` does not repeat the endpoint on every row. Enclosing means the
  whole heading stack: a ``#### Error responses`` nested under that section must not shed it. The
  match tolerates a leading ``_`` because ``routes_explain.py`` offers no word boundary. Inside a
  fenced block a ``#`` line is a shell comment, not a heading, so heading tracking is suspended
  there — offender scanning is not, because a documented error body is still a claim.
* The unit of judgement is a sentence, not a line. ``OperatorGuide/90_incident_runbook.md:92`` names
  ``POST /explain``, the reranker and ``504`` inside one very long line while asserting none of them
  of each other. Splitting on sentence boundaries keeps it green. The split is deliberately
  **periods only**: a colon or semicolon is how a status mapping is normally written
  ("``504``: the reranker timed out"), so splitting on those would let the guarded claim through in
  its most likely form.

``test_detector_flags_a_documented_explain_504_reranker_mapping`` is what keeps the gate honest — a
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
# "Gateway Timeout" is 504's canonical reason phrase, the way a doc spells the status out in prose.
_STATUS = re.compile(r"\b504\b|gateway timeout")


def _shipped_docs() -> list[Path]:
    docs = [
        md
        for md in _DOCS_ROOT.rglob("*.md")
        if not any(part in _EXCLUDED_DIRS for part in md.relative_to(_DOCS_ROOT).parts)
    ]
    docs += [_REPO_ROOT / name for name in _EXTRA_SHIPPED_DOCS]
    return sorted(doc for doc in docs if doc.is_file())


def _offending_lines(doc: Path) -> list[str]:
    """Return every line in ``doc`` whose ``/explain`` context ties the reranker to a 504."""
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


def test_no_shipped_doc_maps_explain_504_to_the_reranker() -> None:
    docs = _shipped_docs()
    # Without these the gate would pass vacuously if part of the corpus moved or was renamed.
    assert [doc for doc in docs if _DOCS_ROOT in doc.parents], (
        f"no shipped documentation found under {_DOCS_ROOT}"
    )
    missing = [name for name in _EXTRA_SHIPPED_DOCS if not (_REPO_ROOT / name).is_file()]
    assert not missing, f"shipped documentation outside {_DOCS_ROOT} went missing: {missing}"

    offenders = [entry for doc in docs for entry in _offending_lines(doc)]
    assert not offenders, (
        "the docs now map an /explain 504 to the reranker — re-implement S344 against the documented "
        "trigger and knob instead of the positive-path proxies:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        ("colon mapping", "`POST /explain` returns 504: the reranker timed out.\n"),
        ("semicolon mapping", "/explain answers 504; the reranker exceeded its deadline.\n"),
        ("plain sentence", "The /explain endpoint answers 504 when the reranker times out.\n"),
        ("cross-encoder wording", "/explain returns 504 when the cross-encoder exceeds its budget.\n"),
        ("spelled-out status", "/explain returns Gateway Timeout when the reranker is too slow.\n"),
        ("heading claim", "### POST /explain returns 504 when the reranker times out\n"),
        (
            "table row under a nested heading",
            "### POST /explain\n#### Error responses\n\n"
            "| Status | Meaning |\n|---|---|\n| 504 | The reranker cross-encoder timed out |\n",
        ),
        (
            "module heading kept across a fenced block",
            "### `routes_explain.py`\n\n```bash\n# curl the endpoint\n```\n\n"
            "| 504 | The reranker timed out |\n",
        ),
    ],
)
def test_detector_flags_a_documented_explain_504_reranker_mapping(
    tmp_path: Path, label: str, markdown: str
) -> None:
    """Without this, a broken matcher would leave the gate silently green forever."""
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert _offending_lines(doc), f"detector missed the {label} form of the guarded claim"


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        ("504 belongs to a different subject", "/explain 504 is a fanout timeout. The reranker fails with 500.\n"),
        ("no /explain context", "`POST /search` returns 504 after ~30 s; the reranker is not a cause.\n"),
        ("no reranker", "/explain returns 504 on fanout timeout.\n"),
        ("unanchored digits", "/explain p95 is 1504 ms with the reranker enabled.\n"),
    ],
)
def test_detector_leaves_non_claims_alone(tmp_path: Path, label: str, markdown: str) -> None:
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert not _offending_lines(doc), f"detector false-positived on {label}"

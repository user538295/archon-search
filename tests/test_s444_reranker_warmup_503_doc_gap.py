"""S444 doc-gap reopening gate.

Ticket: ``Documentation/Backlog/S444-reranker_warmup_503_is_undocumented.md``.

S444's subject — "``POST /search`` answers ``503`` because the reranker is still warming up" — is
not documented anywhere, and the code says the opposite:

* ``archon_search/pipeline.py:487-512`` swallows every reranker warm-up failure (including
  ``RerankerWarmupTimeout``) into a WARNING and returns ``self._reranker.is_warm``; its docstring
  (``pipeline.py:456``) states "Never raises".
* ``archon_search/server/_search_budget.py:63-94`` (``warmup_for_search``, bounded by
  ``RERANKER_WARMUP_WAIT_SECONDS = 0.5``) turns that into ``SearchWarmup(rerank=False, ...)``, which
  ``routes_search.py:295`` / ``:473`` pass straight into the pipeline — a cold cross-encoder degrades
  the request to the fused vector+FTS ranking, it does not fail it.
* The only ``503`` reachable from ``POST /search`` is ``metadata_store_error``
  (``routes_search.py:308-312``, ``:367-370``) or ``embedder_not_ready``
  (``routes_search.py:521-546``) — the latter is the *embedder cache* wedging past its 120 s bound
  (``embedder_cache.py``), correctly documented in
  ``Documentation/Architecture/140_error_handling_strategy.md:73``, ``BREAKING.md``, and
  ``Documentation/MigrationGuide/03_breaking_changes_index.md``. Neither is the reranker.

So S444 is covered by the positive-path proxy the ticket specifies (the first ``POST /search`` on a
freshly booted instance answers ``200``), and this file is the reopening gate that pairs with it:
**no shipped document may claim ``/search`` answers ``503`` because the reranker is warming up.** The
moment a document introduces that mapping, this test flips red so S444 is re-implemented against the
then-documented status, body and window instead of the proxy.

Scope, and why it is this narrow (mirrors ``tests/test_s343_reranker_503_doc_gap.py`` and
``tests/test_s344_reranker_504_doc_gap.py``):

* ``Backlog/`` and ``Completed/`` are tickets and archives, not shipped documentation. S444's own
  ticket body quotes the offending phrasing as its subject matter, so scanning them makes the gate
  permanently and vacuously red.
* Shipped documentation is not only ``Documentation/`` — ``README.md``, ``BREAKING.md`` and
  ``archon-search.toml.example`` all document endpoints, and a per-directory scope would orphan them.
* The claim needs all three of a ``/search`` context, a warm-up/loading trigger and the reranker in
  one sentence. Dropping the warm-up leg would catch the ticket's *other* ``503`` neighbours —
  ``GET /ready`` legitimately answers ``503`` while the startup warm-up (which covers the
  cross-encoder) is still running, and the ``embedder_not_ready`` ``503`` is a real, correctly
  documented mapping whose prose sits next to "still loading". Requiring the reranker in the same
  sentence keeps the embedder mapping green; requiring ``/search`` keeps ``GET /ready`` green.
* ``/search`` context comes from the line, any enclosing heading, or the file name — a status table
  under ``### `routes_search.py` `` does not repeat the endpoint on every row. Enclosing means the
  whole heading stack: a ``#### Error responses`` nested under that section must not shed it. The
  match tolerates a leading ``_`` because ``routes_search.py`` offers no word boundary, but unlike
  the sibling gates it does **not** admit the bare English word: "search" is ordinary prose here, and
  admitting it put a legitimate ``GET /ready`` warm-up paragraph in scope
  (``OperatorGuide/80_capacity_and_performance.md:123``, which ends "...on the first search") and
  fired a false red. A document asserting the guarded mapping names the endpoint. Inside a
  fenced block a ``#`` line is a shell comment, not a heading, so heading tracking is suspended
  there — offender scanning is not, because a documented error body is still a claim.
* The unit of judgement is a sentence, not a line. ``Architecture/140_error_handling_strategy.md:73``
  and ``OperatorGuide/90_incident_runbook.md`` each name ``/search``, ``503``, the reranker and
  warm-up within one very long line while asserting none of them of each other. Splitting on
  sentence boundaries keeps both green. The split is deliberately **periods only**: a colon or
  semicolon is how a status mapping is normally written ("``503``: the reranker is still warming
  up"), so splitting on those would let the guarded claim through in its most likely form.

``test_detector_flags_a_documented_reranker_warmup_503_mapping`` is what keeps the gate honest — a
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
# The endpoint path only — unlike ``/explain`` in the sibling gates, "search" is an ordinary English
# word all over this documentation set, and admitting it pulled a `GET /ready` warm-up paragraph in
# ``OperatorGuide/80_capacity_and_performance.md:123`` into scope (it ends "...to be built lazily on
# the first search") and fired a false red. A document that actually asserts the guarded mapping
# names the endpoint. ``_search`` is kept because the heading that opens the endpoint's reference
# section is ``### `routes_search.py` ``, where a leading word boundary never fires, and because it
# is what puts ``UserManual/60_searching.md`` in scope by file name.
_SEARCH = re.compile(r"/search|_search")
_RERANK = re.compile(r"re-?rank|cross[- ]encoder")
# The warm-up leg: what makes this S444 rather than any other reranker-adjacent 503. The verb is
# inflected as often as the noun in prose ("while the reranker warms up", "once it has warmed up"),
# and a bare ``warm.?up`` matches neither — it allows only one character between the two halves, so
# it silently missed those forms and would have left the gate green on them.
_WARMUP = re.compile(
    r"warm(?:s|ed|ing)?[- ]?up|warming|still loading|still cold|cold[- ]start|preload|loading"
)
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
    """Return every line whose ``/search`` context ties a reranker warm-up to a 503."""
    offenders: list[str] = []
    # Heading level -> heading text, so a nested subheading does not erase its ancestors.
    headings: dict[int, str] = {}
    fenced = False
    file_is_about_search = bool(_SEARCH.search(doc.name.lower()))
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
        in_search_context = (
            file_is_about_search
            or _SEARCH.search(lowered)
            or any(_SEARCH.search(text) for text in headings.values())
        )
        if not in_search_context:
            continue
        for sentence in _SENTENCE_SPLIT.split(lowered):
            if _STATUS.search(sentence) and _RERANK.search(sentence) and _WARMUP.search(sentence):
                offenders.append(f"{doc.relative_to(_REPO_ROOT, walk_up=True)}:{lineno}: {line.strip()}")
                break
    return offenders


def test_no_shipped_doc_maps_a_reranker_warmup_to_503() -> None:
    docs = _shipped_docs()
    # Without these the gate would pass vacuously if part of the corpus moved or was renamed.
    assert [doc for doc in docs if _DOCS_ROOT in doc.parents], (
        f"no shipped documentation found under {_DOCS_ROOT}"
    )
    missing = [name for name in _EXTRA_SHIPPED_DOCS if not (_REPO_ROOT / name).is_file()]
    assert not missing, f"shipped documentation outside {_DOCS_ROOT} went missing: {missing}"

    offenders = [entry for doc in docs for entry in _offending_lines(doc)]
    assert not offenders, (
        "the docs now map a /search 503 to a reranker warm-up — re-implement S444 against the "
        "documented status, body and window instead of the positive-path proxy:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        ("colon mapping", "`POST /search` returns 503: the reranker is still warming up.\n"),
        ("semicolon mapping", "/search answers 503; the reranker warm-up has not finished.\n"),
        ("plain sentence", "The /search endpoint answers 503 while the reranker is still loading.\n"),
        ("cross-encoder wording", "/search returns 503 until the cross-encoder warm-up completes.\n"),
        ("spelled-out status", "/search returns Service Unavailable while the reranker is still cold.\n"),
        ("heading claim", "### POST /search returns 503 while the reranker is warming up\n"),
        ("inflected verb", "/search returns 503 while the reranker warms up.\n"),
        ("past-participle verb", "/search returns 503 until the cross-encoder has warmed up.\n"),
        ("context from an enclosing heading only", "## /search\n\n503 while the reranker warms up.\n"),
        (
            "table row under a nested heading",
            "### POST /search\n#### Error responses\n\n"
            "| Status | Meaning |\n|---|---|\n| 503 | The reranker cross-encoder is still warming up |\n",
        ),
        (
            "module heading kept across a fenced block",
            "### `routes_search.py`\n\n```bash\n# curl the endpoint\n```\n\n"
            "| 503 | The reranker is still warming up |\n",
        ),
    ],
)
def test_detector_flags_a_documented_reranker_warmup_503_mapping(
    tmp_path: Path, label: str, markdown: str
) -> None:
    """Without this, a broken matcher would leave the gate silently green forever."""
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert _offending_lines(doc), f"detector missed the {label} form of the guarded claim"


@pytest.mark.parametrize(
    ("label", "markdown"),
    [
        (
            "503 belongs to a different subject",
            "/search 503 means the metadata store could not be reached. The reranker warm-up only "
            "degrades to rerank=False.\n",
        ),
        ("no /search context", "`GET /ready` returns 503 while the reranker warm-up runs.\n"),
        ("no reranker", "/search returns 503 when the collection meta lookup fails.\n"),
        (
            "embedder-cache 503 documented correctly",
            "/search returns 503 `embedder_not_ready` when the embedding model is still loading.\n",
        ),
        ("reranker warm-up without a 503", "/search degrades to rerank=False while the reranker warms up.\n"),
        ("unanchored digits", "/search p95 is 1503 ms once the reranker warm-up is done.\n"),
    ],
)
def test_detector_leaves_non_claims_alone(tmp_path: Path, label: str, markdown: str) -> None:
    doc = tmp_path / "page.md"
    doc.write_text(markdown, encoding="utf-8")

    assert not _offending_lines(doc), f"detector false-positived on {label}"

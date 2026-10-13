"""S27 doc-gap gates: the ``/ready`` warm-up gate is neither called "eager" nor curled once.

Ticket: ``Documentation/Backlog/S27-ready_200.md`` — a user following the Docker quickstart polled
``/health`` to ``200``, then got ``503`` from a single ``curl /ready`` and expected ``200``.

The runtime is correct by design: ``/ready`` gates on model warm-up for every deployment
(``_warmup_pending`` and ``ready_flag`` in ``archon_search/server/routes_ready.py``). The
documentation is not. Commit ``a9bd9ad1`` described that gate as "eager" warm-up, accurate then
because warm-up only ran under ``eager_load_embedders``. Commit ``b7a373b4`` (S279) made warm-up
unconditional but left the "any eager warm-up" wording behind, so a reader on the default config
(``eager_load_embedders = false``) concludes there is no warm-up to wait for and ``/ready`` is
``200`` once storage connects. The Docker quickstart then curled ``/ready`` exactly once.

Scope of the wording gate mirrors ``tests/test_s444_reranker_warmup_503_doc_gap.py``; the unit of judgement is a period-split sentence.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_ROOT = _REPO_ROOT / "Documentation"
# Tickets and archives discuss the guard itself; they are not the shipped contract.
_EXCLUDED_DIRS = ("Backlog", "Completed")
# Shipped documentation living outside Documentation/.
_EXTRA_SHIPPED_DOCS = ("README.md", "BREAKING.md", "archon-search.toml.example")
# Periods only — a colon or semicolon is how a status mapping is normally written, so splitting on
# those would let a guarded claim through in its most likely form.
_SENTENCE_SPLIT = re.compile(r"\.\s")


def _shipped_docs() -> list[Path]:
    docs = [
        md
        for md in _DOCS_ROOT.rglob("*.md")
        if not any(part in _EXCLUDED_DIRS for part in md.relative_to(_DOCS_ROOT).parts)
    ]
    docs += [_REPO_ROOT / name for name in _EXTRA_SHIPPED_DOCS]
    return sorted(doc for doc in docs if doc.is_file())


def _assert_shipped_corpus_present(docs: list[Path]) -> None:
    """Without this a gate would pass vacuously if part of the corpus moved or was renamed."""
    assert [doc for doc in docs if _DOCS_ROOT in doc.parents], (
        f"no shipped documentation found under {_DOCS_ROOT}"
    )
    missing = [
        name for name in _EXTRA_SHIPPED_DOCS if not (_REPO_ROOT / name).is_file()
    ]
    assert not missing, (
        f"shipped documentation outside {_DOCS_ROOT} went missing: {missing}"
    )


_README_DOCKER_HEADING = "## Running with Docker"

# "eager"/"eagerly" directly qualifying the warm-up noun or verb, optionally through "model". The
# separators exclude ``_``, so the config key ``eager_load_embedders`` sitting next to "warm-up" in
# a correct sentence never matches.
_EAGER_WARMUP = re.compile(
    r"\beager(?:ly)?[- ](?:model[- ])?warm(?:[- ]?ups?|ing|ed)\b", re.IGNORECASE
)
# Leading ``#`` markers of a non-Markdown file, stripped so a hard-wrapped
# ``archon-search.toml.example`` comment reads as one sentence once its lines are joined.
_COMMENT_MARKER = re.compile(r"^\s*#+")
_MARKDOWN_HEADING = re.compile(r"#{1,6} ")
# A sentence ends at a period or at a paragraph break (joined as a newline).
_SENTENCE_OR_PARAGRAPH_END = re.compile(rf"{_SENTENCE_SPLIT.pattern}|\n")

_DOCKER_GUIDE = _DOCS_ROOT / "UserManual" / "140_running_with_docker.md"
_MULTI_INSTANCE_GUIDE = _DOCS_ROOT / "UserManual" / "150_multi_instance_setup.md"
_SMOKE_TEST_INTRO = "Smoke-test from the host:"
_POLL_INTRO = "Poll until healthy:"
_FENCE_PREFIX = "```"
_TRAILING_COMMENT = re.compile(r"\s+#.*$")
# The bounded ``/ready`` poll the guides ship: ``-f`` makes curl fail on a 503, ``&& break`` stops
# on the first 2xx, and 600 one-second attempts cover the image's 600s HEALTHCHECK start-period.
_READY_POLL = (
    "for _ in $(seq 1 600); do curl -sf --max-time 5 http://127.0.0.1:{port}/ready > /dev/null"
    " && break; sleep 1; done"
)


def _eager_warmup_sentences(
    text: str, *, markdown: bool = True
) -> list[tuple[int, str]]:
    """Return ``(line, sentence)`` for every sentence calling the warm-up "eager".

    Lines of one blank-line-separated paragraph are joined before the sentence split, so wording
    hard-wrapped across lines is still caught; a Markdown heading is never joined to its
    neighbours. The line reported is the one the match starts on.
    """
    lines = [
        line.strip() if markdown else _COMMENT_MARKER.sub("", line).strip()
        for line in text.splitlines()
    ]
    breaks = [
        not line or bool(markdown and _MARKDOWN_HEADING.match(line)) for line in lines
    ]
    joined = "".join(
        line + ("\n" if brk or next_brk else " ")
        for line, brk, next_brk in zip(lines, breaks, breaks[1:], strict=False)
    ) + (lines[-1] if lines else "")
    line_starts = list(accumulate((len(line) + 1 for line in lines[:-1]), initial=0))
    bounds = [
        0,
        *(
            pos
            for sep in _SENTENCE_OR_PARAGRAPH_END.finditer(joined)
            for pos in sep.span()
        ),
        len(joined),
    ]
    return [
        (bisect_right(line_starts, match.start()), joined[start:end].strip())
        for start, end in zip(bounds[::2], bounds[1::2], strict=True)
        if (match := _EAGER_WARMUP.search(joined, start, end))
    ]


def _block_after(markdown: str, intro: str) -> list[str]:
    """Return the lines of the fenced block right after ``intro``, or ``[]`` if absent."""
    lines = markdown.splitlines()
    try:
        start = lines.index(intro)
    except ValueError:
        return []
    rest = lines[start + 1 :]
    opening = next((i for i, line in enumerate(rest) if line.strip()), None)
    assert opening is not None and rest[opening].startswith(_FENCE_PREFIX), (
        f"only blank lines may separate {intro!r} from its fenced block"
    )
    closing = next(
        (i for i in range(opening + 1, len(rest)) if rest[i].startswith(_FENCE_PREFIX)),
        None,
    )
    assert closing is not None, f"the fenced block after {intro!r} is never closed"
    return rest[opening + 1 : closing]


def _first_command(block: list[str]) -> str | None:
    """Return the first non-blank, non-comment line of ``block``, its trailing comment stripped."""
    return next(
        (
            _TRAILING_COMMENT.sub("", line).strip()
            for line in block
            if line.strip() and not line.lstrip().startswith("#")
        ),
        None,
    )


def test_no_shipped_doc_calls_the_ready_warmup_gate_eager() -> None:
    docs = _shipped_docs()
    _assert_shipped_corpus_present(docs)

    offenders = [
        f"{doc.relative_to(_REPO_ROOT)}:{lineno}: {sentence}"
        for doc in docs
        for lineno, sentence in _eager_warmup_sentences(
            doc.read_text(encoding="utf-8"), markdown=doc.suffix == ".md"
        )
    ]
    assert not offenders, (
        "these shipped-doc sentences call the /ready warm-up gate 'eager' warm-up, but warm-up "
        "runs on every startup (S279) — a default-config reader expects /ready 200 too early "
        "(S27):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "`/ready` answers 200 once storage is connected and any eager warm-up finished.",
        "`/ready` answers 200 once any eager warm up finished.",
        "`/ready` answers 200 once any eager warmup finished.",
        "Eager warm-ups gate `/ready`.",
        "`/ready` waits for eager warming of the models.",
        "`/ready` waits until the models are eagerly warmed.",
        "`/ready` answers 200 once the eager model warm-up finished.",
    ],
)
def test_detector_flags_eager_warmup_wording(sentence: str) -> None:
    """Without this, a broken matcher would leave the gate silently green forever."""
    assert _eager_warmup_sentences(sentence) == [(1, sentence)]


@pytest.mark.parametrize(
    "sentence",
    [
        "`eager_load_embedders` widens the startup model warm-up to every per-collection model.",
        "Set `eager_load_embedders = true` to warm up every per-collection model.",
        "`/ready` answers 503 until the startup model warm-up finished.",
    ],
)
def test_detector_spares_correct_warmup_wording(sentence: str) -> None:
    assert _eager_warmup_sentences(sentence) == []


def test_detector_flags_eager_warmup_wording_hard_wrapped_across_comment_lines() -> (
    None
):
    text = "# intro.\n# `/ready` answers 200 once any eager\n# warm-up finished.\n"

    assert _eager_warmup_sentences(text, markdown=False) == [
        (2, "`/ready` answers 200 once any eager warm-up finished.")
    ]


def test_detector_reports_only_the_offending_sentence_of_a_line() -> None:
    line = "The server starts on every boot. `/ready` waits for any eager warm-up. Then it syncs."

    assert _eager_warmup_sentences("intro\n" + line) == [
        (2, "`/ready` waits for any eager warm-up")
    ]


def test_detector_never_joins_a_markdown_heading_into_the_next_paragraph() -> None:
    assert (
        _eager_warmup_sentences("## Eager\nWarm-up finished before `/ready`.\n") == []
    )


@pytest.mark.parametrize(
    ("guide", "intro", "port"),
    [
        (_DOCKER_GUIDE, _SMOKE_TEST_INTRO, 8765),
        (_MULTI_INSTANCE_GUIDE, _POLL_INTRO, 18765),
    ],
)
def test_guide_polls_ready_before_any_other_curl(
    guide: Path, intro: str, port: int
) -> None:
    block = _block_after(guide.read_text(encoding="utf-8"), intro)
    assert block, f"no fenced block after {intro!r} in {guide}"

    assert _first_command(block) == _READY_POLL.format(port=port), (
        f"the first command after {intro!r} in {guide} must be the bounded /ready poll — a single "
        "curl answers 503 while the startup model warm-up runs, and a one-shot /health right "
        "after `docker run` is refused (S27):\n" + "\n".join(block)
    )


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(
            "curl -sf http://127.0.0.1:8765/ready  # 503 until warm-up finished\n",
            id="one-shot-ready-curl",
        ),
        pytest.param(
            "curl http://127.0.0.1:8765/health\n"
            + _READY_POLL.format(port=8765)
            + "\n",
            id="health-curl-before-the-poll",
        ),
    ],
)
def test_detector_rejects_a_block_that_does_not_open_with_the_ready_poll(
    block: str,
) -> None:
    assert _first_command(block.splitlines()) != _READY_POLL.format(port=8765)


def test_block_after_rejects_prose_between_the_intro_and_the_fence() -> None:
    markdown = f"{_SMOKE_TEST_INTRO}\n\nSome prose.\n\n```bash\ncurl x\n```\n"

    with pytest.raises(AssertionError, match="only blank lines"):
        _block_after(markdown, _SMOKE_TEST_INTRO)


def test_readme_docker_section_says_to_poll_ready_because_it_answers_503() -> None:
    readme_path = _REPO_ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    assert _README_DOCKER_HEADING in readme, (
        f"{readme_path} lost its {_README_DOCKER_HEADING!r} section"
    )
    section = readme.split(_README_DOCKER_HEADING, 1)[1].split("\n## ", 1)[0]

    assert any(
        all(word in sentence.lower() for word in ("/ready", "503", "poll"))
        for sentence in _SENTENCE_SPLIT.split(section)
    ), (
        f"no sentence of the {_README_DOCKER_HEADING!r} quickstart in {readme_path} says to poll "
        "/ready because it answers 503 — a reader who polls /health to 200 expects /ready 200 "
        "too, but it answers 503 until the startup model warm-up finishes (S27)"
    )

"""Regression guards for image-OCR unbounded native memory growth during ingest.

Brief: `Documentation/Backlog/2026-08-19-010-image-ocr-unbounded-memory-brief.md`.

`DocumentParser` OCRs every raster image through docling -> RapidOCR -> onnxruntime.
Two mechanisms make that unbounded in the long-lived server process:

1. docling renders the OCR bitmap at ``OcrOptions.scale = 3.0`` (9x the pixels), and with
   *auto* OCR options docling silently discards user fields, so the scale must be set on a
   *concrete* ``RapidOcrOptions`` instance (brief "Also found", lines 73-76).
2. the shared long-lived ``DocumentConverter`` retains the OCR object graph plus native
   residue that only process exit reclaims — so parsing must run in a recycled worker
   process, not in-process.

The three tests below map 1:1 onto the brief's Verification section (lines 110-118).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from _docling_stubs import DOCLING_OCR_TIMEOUT_S, InlineExecutor, docling_stubbed
from _docling_stubs import reset_parser_worker_globals  # noqa: F401 — autouse fixture
from archon_search.constants import DEFAULT_DOCLING_MAX_TASKS_PER_CHILD
from archon_search.parser import DocumentParser, ParseError, _worker_initializer

# --- primary (memory) test knobs -------------------------------------------------------
# Icon-set ladder: a real source repo ships app icons at every resolution. The brief
# measured its largest single step (+788 MB) on the first 1024px icon, so >= 1024 is
# mandatory in the mix.
_ICON_SIZES = (64, 128, 256, 512, 1024)
_CORPUS_SIZE = 40  # brief Verification: ">= 40 mixed-size images"
_RSS_BUDGET_MB = 500  # brief Verification: RSS(final) - RSS(baseline) < 500 MB
_RSS_CEILING_MB = 8000  # brief repro guard: abort before hurting the host
_MODEL_LOAD_TIMEOUT_S = DOCLING_OCR_TIMEOUT_S  # single owner: tests/_docling_stubs.py
_RUN_BUDGET_S = 900  # bound the run once the models are up
_MIN_WORKER_GENERATIONS = 2  # >= 2 distinct worker PIDs == >= 1 recycle

# --- process introspection (via `ps`, no psutil: it is only a transitive dependency) ----


def _ps_rows() -> list[tuple[int, int, int, str]]:
    """Return ``(pid, ppid, rss_kb, args)`` for every visible process."""
    out = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,rss=,args="], capture_output=True, text=True, check=True
    ).stdout
    rows: list[tuple[int, int, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows.append((pid, ppid, rss, parts[3] if len(parts) > 3 else ""))
    return rows


def _descendants(rows: list[tuple[int, int, int, str]], root: int) -> set[int]:
    """Every process under *root*, root included."""
    children: dict[int, list[int]] = defaultdict(list)
    for pid, ppid, _kb, _args in rows:
        children[ppid].append(pid)
    stack, seen = [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return seen


def _tree_rss_mb(rows: list[tuple[int, int, int, str]], root: int) -> float:
    """Total RSS of *root* plus every descendant — parse workers included."""
    under = _descendants(rows, root)
    return sum(kb for pid, _ppid, kb, _args in rows if pid in under) / 1024


# Helper processes that are NOT recycled parse workers: multiprocessing's singleton
# resource tracker / forkserver.
_NON_WORKER_MARKERS = ("resource_tracker", "forkserver")


def _worker_pids(rows: list[tuple[int, int, int, str]], root: int) -> set[int]:
    """Multiprocessing task workers under *root*.

    A changing PID set is the observable signal that the parse worker was recycled, so
    everything that is merely long-lived plumbing must be filtered out.
    """
    under = _descendants(rows, root) - {root}
    return {
        pid
        for pid, _ppid, _kb, args in rows
        if pid in under
        # This predicate — not _NON_WORKER_MARKERS — is what excludes the `ps` child this
        # module spawns to take each sample; it is a child of the test process and would
        # otherwise read as a fresh worker generation, inflating the count that proves the
        # recycle. Loosen it and the >= _MIN_WORKER_GENERATIONS assertion starts passing on
        # `ps` noise.
        and "multiprocessing" in args
        and not any(marker in args for marker in _NON_WORKER_MARKERS)
    }


def _make_icon(path: Path, size: int, label: str) -> Path:
    from PIL import Image, ImageDraw  # noqa: PLC0415 — transitive dep, keep collection cheap

    img = Image.new("RGB", (size, size), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [size // 8, size // 8, size - size // 8, size - size // 8],
        outline=(20, 60, 160),
        width=max(1, size // 32),
    )
    draw.text((size // 6, size // 2), label, fill=(10, 10, 10))
    img.save(path)
    return path


def _mixed_size_corpus(root: Path, count: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    return [
        _make_icon(
            root / f"icon_{i:03d}_{_ICON_SIZES[i % len(_ICON_SIZES)]}.png",
            _ICON_SIZES[i % len(_ICON_SIZES)],
            f"ARCHON {i:03d}",
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.docling
@pytest.mark.xdist_group("docling")
async def test_image_ocr_memory_bounded_over_mixed_size_corpus(tmp_path: Path) -> None:
    """>= 40 mixed-size images must cost < 500 MB of retained RSS, with >= 1 worker recycle.

    Baseline is taken after the warm-up image, i.e. once the OCR models are resident, so the
    one-off model load is excluded and only per-image retention is measured — the same
    methodology as the brief's repro (1,229 MB after file 1 -> 3,063 MB after 60 icons).
    """
    # docling is a hard dependency (pyproject.toml:19) and PIL/Pillow is pulled in
    # transitively by docling-core / docling-ibm-models / docling-parse (uv.lock), so both are
    # always present in any correctly installed environment — importorskip here would let this
    # P0 OOM regression guard silently pass on a machine where docling half-works instead of
    # failing loudly (C1-I-4). No env-var opt-in either: a broken docling install is a real
    # signal, not a reason to skip.
    assert DEFAULT_DOCLING_MAX_TASKS_PER_CHILD < _CORPUS_SIZE, (
        f"DEFAULT_DOCLING_MAX_TASKS_PER_CHILD ({DEFAULT_DOCLING_MAX_TASKS_PER_CHILD}) must be "
        f"< _CORPUS_SIZE ({_CORPUS_SIZE}) — otherwise the worker never recycles over the corpus "
        f"and the >= {_MIN_WORKER_GENERATIONS} worker-generation assertion below can never be "
        "satisfied, silently stopping this test from testing recycling at all."
    )
    warmup, *corpus = _mixed_size_corpus(tmp_path / "icons", _CORPUS_SIZE + 1)
    me = os.getpid()
    # Baseline the worker PIDs that already exist BEFORE this test's parser is built, and
    # subtract them from every later sample. `xdist_group("docling")` pins the whole docling
    # lane to one process and tests/test_parser.py sorts first, so its parsers' pools are
    # still GC-pending here. Counting those stale PIDs would let the >= 2 generations
    # assertion pass with ZERO actual recycles, and a stale ~1 GB worker alive at baseline
    # but gone by the end would understate growth_mb — both weaken this guard silently.
    preexisting_workers = _worker_pids(_ps_rows(), me)
    parser = DocumentParser()
    seen_workers: set[int] = set()

    try:
        await asyncio.wait_for(parser.parse(warmup), timeout=_MODEL_LOAD_TIMEOUT_S)
    except TimeoutError:
        pytest.fail(
            f"docling model load exceeded {_MODEL_LOAD_TIMEOUT_S}s. If this is a first run, "
            "the RapidOCR/ONNX weights are still downloading — re-run once the model cache "
            "is warm."
        )
    except ParseError as exc:
        pytest.fail(f"docling not functional in this environment: {exc}")

    rows = _ps_rows()
    baseline_mb = _tree_rss_mb(rows, me)
    seen_workers |= _worker_pids(rows, me) - preexisting_workers
    curve: list[tuple[int, float]] = []
    started = time.monotonic()

    for i, image in enumerate(corpus):
        await parser.parse(image)
        rows = _ps_rows()
        current_mb = _tree_rss_mb(rows, me)
        seen_workers |= _worker_pids(rows, me) - preexisting_workers
        curve.append((_ICON_SIZES[(i + 1) % len(_ICON_SIZES)], current_mb))
        assert current_mb < _RSS_CEILING_MB, (
            f"aborting at image {i + 1}/{len(corpus)}: process-tree RSS {current_mb:.0f} MB "
            f"crossed the {_RSS_CEILING_MB} MB host-safety ceiling (baseline {baseline_mb:.0f} MB)"
        )
        if time.monotonic() - started > _RUN_BUDGET_S:
            pytest.fail(
                f"run budget {_RUN_BUDGET_S}s exhausted after {i + 1}/{len(corpus)} images — "
                "memory contract could not be verified"
            )

    final_mb = _tree_rss_mb(_ps_rows(), me)
    growth_mb = final_mb - baseline_mb
    trace = " ".join(f"{px}px:{mb:.0f}" for px, mb in curve)
    workers = sorted(seen_workers) if seen_workers else "none — parsing ran in-process"
    assert growth_mb < _RSS_BUDGET_MB, (
        f"image OCR retained {growth_mb:.0f} MB across {len(corpus)} mixed-size images "
        f"(baseline {baseline_mb:.0f} MB -> final {final_mb:.0f} MB); budget is "
        f"{_RSS_BUDGET_MB} MB. Worker PIDs observed: {workers}. RSS trace: {trace}"
    )
    assert len(seen_workers) >= _MIN_WORKER_GENERATIONS, (
        f"expected the docling parse worker to be recycled at least once "
        f"(>= {_MIN_WORKER_GENERATIONS} distinct worker PIDs); observed {sorted(seen_workers)}. "
        f"Retained {growth_mb:.0f} MB over {len(corpus)} images."
    )


# --- secondary (default-lane) guards ---------------------------------------------------


async def _parse_png_with_docling_mocked(tmp_path: Path) -> tuple[MagicMock, list[tuple]]:
    """Drive the image parse path with docling and the process pool both intercepted."""
    # docling is mocked here, so the file's bytes are never read: a stub keeps PIL out of the
    # default lane entirely (C2-T-11), matching the other default-lane image tests.
    image = tmp_path / "icon.png"
    image.write_bytes(b"fake png")
    docling = MagicMock()
    docling.DocumentConverter.return_value.convert.return_value.document.export_to_markdown.return_value = "ocr"

    with docling_stubbed(docling):
        await DocumentParser().parse(image)
    return docling, InlineExecutor.calls


@pytest.mark.asyncio
async def test_parse_path_requests_ocr_scale_one(tmp_path: Path) -> None:
    """The image parse path must REQUEST an explicit OCR scale of 1.0 on a concrete options
    instance, that instance must be the one actually reaching ``DocumentConverter`` under
    ``InputFormat.IMAGE``, and ``InputFormat.PDF`` must be absent from ``format_options``.

    This asserts what the parser *passes* to a mocked docling, via call args — it is a default-
    lane, docling-free guard against the code regressing the request. It does NOT verify the
    *effective* scale docling actually applies (a mock cannot regress against docling's own
    behavior, e.g. the "auto options" copy-on-build described below); that is covered separately
    by the real-docling-lane
    ``test_build_docling_converter_effective_scale_is_one_image_only_default_pdf``.

    docling's ``OcrOptions.scale`` defaults to 3.0 — 9x the pixels — and its *auto* OCR model
    rebuilds ``RapidOcrOptions`` copying only ``mode``, silently dropping a scale set on the
    default options object (brief lines 73-76). The scale override is image-only: for a PDF,
    ``scale`` is a 72-DPI render multiplier (not a bitmap-preserving no-op like for images), so
    applying it there would render scanned PDFs at 72 instead of 216 DPI (C1-B-1). This also
    closes the ``scale=True`` hole (``True == 1.0`` in Python, so a bare ``== 1.0`` check would
    pass on a boolean by accident).
    """
    docling, _calls = await _parse_png_with_docling_mocked(tmp_path)

    ocr_calls = docling.RapidOcrOptions.call_args_list
    assert ocr_calls, (
        "the image parse path never constructed a concrete RapidOcrOptions; it therefore "
        "inherits OcrOptions.scale = 3.0 and renders every image at 9x its pixel count."
    )
    ocr_kwargs = ocr_calls[0].kwargs
    assert ocr_kwargs.get("backend") == "onnxruntime", (
        f"expected RapidOcrOptions(backend='onnxruntime', ...), got {ocr_kwargs!r}"
    )
    scale = ocr_kwargs.get("scale")
    assert isinstance(scale, float) and not isinstance(scale, bool), (
        "OCR scale must be a real float, not "
        f"{type(scale).__name__} (scale=True would silently pass a naive `== 1.0` check "
        f"since True == 1.0 in Python); got {scale!r}"
    )
    assert scale == 1.0, f"effective OCR scale must be 1.0, got {scale!r}"
    ocr_options = docling.RapidOcrOptions.return_value

    # The concrete RapidOcrOptions instance must actually flow through to DocumentConverter —
    # constructing it and discarding it would reproduce the exact "auto options" regression
    # this test guards against. The pipeline options are derived from docling's OWN default for
    # this format (ImageFormatOption().pipeline_options, a ThreadedPdfPipelineOptions) rather
    # than a bare PdfPipelineOptions, so the chain runs through model_copy (C2-B-4).
    model_copy = docling.ImageFormatOption.return_value.pipeline_options.model_copy
    assert model_copy.call_args_list, (
        "the image pipeline options were not derived from docling's own default for this "
        "format (ImageFormatOption().pipeline_options.model_copy): passing a bare "
        "PdfPipelineOptions relies on duck typing against StandardPdfPipeline, which declares "
        "ThreadedPdfPipelineOptions"
    )
    update = model_copy.call_args_list[0].kwargs.get("update") or {}
    assert update.get("ocr_options") is ocr_options, (
        "the copied pipeline options were not given the concrete RapidOcrOptions instance. "
        "Overriding only `scale` on docling's default options instead would silently revert to "
        "3.0: OcrAutoModel rebuilds RapidOcrOptions copying only backend and mode, dropping "
        f"scale. got update={update!r}"
    )
    pipeline_options = model_copy.return_value

    image_format_calls = docling.ImageFormatOption.call_args_list
    assert image_format_calls, "no ImageFormatOption was constructed"
    assert any(call.kwargs.get("pipeline_options") is pipeline_options for call in image_format_calls), (
        "ImageFormatOption was not built from the pipeline options carrying the concrete scale"
    )
    image_format_option = docling.ImageFormatOption.return_value

    converter_calls = docling.DocumentConverter.call_args_list
    assert converter_calls, "DocumentConverter was never constructed"
    format_options = converter_calls[0].kwargs.get("format_options")
    assert format_options is not None, "DocumentConverter was not given format_options"
    assert format_options.get(docling.InputFormat.IMAGE) is image_format_option, (
        "the concrete scale=1.0 options did not reach DocumentConverter under InputFormat.IMAGE"
    )
    assert docling.InputFormat.PDF not in format_options, (
        "InputFormat.PDF must be absent from format_options (C1-B-1 regression guard): for a "
        "PDF, OcrOptions.scale is a 72-DPI render multiplier off the page geometry, not a "
        "bitmap-preserving no-op like for images, so PDFs must keep docling's own defaults "
        "instead of inheriting the image-only scale=1.0 override"
    )


@pytest.mark.integration
@pytest.mark.docling
@pytest.mark.xdist_group("docling")
def test_build_docling_converter_effective_scale_is_one_image_only_default_pdf() -> None:
    """Real-docling-lane complement to ``test_parse_path_requests_ocr_scale_one``, which asserts
    only against a mock. This builds a real ``DocumentConverter`` and reads the options back off
    it, so it exercises real pydantic construction and pins the IMAGE/PDF split (C1-B-1).

    Scope, stated precisely (C2-T-2): this is NOT an effective-DPI measurement.
    ``DocumentConverter.__init__`` stores the caller's ``FormatOption`` verbatim for IMAGE, so
    what is read back is a round-trip of what ``_build_docling_converter`` passed. docling's
    auto-options discard happens later, in ``OcrAutoModel.__init__`` at conversion time, and
    ``_build_docling_converter`` never builds a pipeline (construction is lazy) — so this test
    cannot catch that. A true end-to-end DPI guard would need a fixture calibrated to fail
    between 72 and 216 DPI, i.e. deliberately near the recognition cliff, trading a real
    regression guard for a permanent flake. That trade was considered and declined.

    Verified attribute path against the installed docling (2.117): a built ``DocumentConverter``
    exposes ``.format_to_options``, a mapping pre-populated with docling's own defaults for every
    ``InputFormat`` not explicitly overridden.
    """
    from docling.document_converter import InputFormat

    from archon_search.parser import _build_docling_converter

    converter = _build_docling_converter()

    image_scale = converter.format_to_options[InputFormat.IMAGE].pipeline_options.ocr_options.scale
    assert isinstance(image_scale, float) and not isinstance(image_scale, bool), (
        f"expected a real float, got {type(image_scale).__name__}"
    )
    assert image_scale == 1.0, f"effective image OCR scale must be 1.0, got {image_scale!r}"

    # C1-B-1 regression guard at the real-docling level: PDFs must NOT inherit the image-only
    # override and must keep docling's own default (3.0 == 216 DPI), not our scale=1.0 (72 DPI).
    pdf_ocr_options = converter.format_to_options[InputFormat.PDF].pipeline_options.ocr_options
    assert pdf_ocr_options.scale == 3.0, (
        f"PDFs must keep docling's own default OCR scale of 3.0 (216 DPI); scale is a 72-DPI "
        f"render multiplier on the PDF backend, so the image-only 1.0 would mean 72 DPI — one "
        f"ninth the pixels (C1-B-1). Pinned to the exact default rather than `!= 1.0`, which "
        f"would also pass on a nonsense value. got {pdf_ocr_options.scale!r}"
    )
    # C2-B-3: images are pinned to RapidOCR; PDFs deliberately keep docling's *auto* engine
    # selection. Both resolve to rapidocr today only because ocrmac/nemotron_ocr/easyocr are
    # absent from uv.lock. Pin the PDF path to `auto` so installing one of them — which would
    # silently OCR PDFs and images with different engines — fails here instead of in production.
    assert pdf_ocr_options.kind == "auto", (
        f"PDFs must keep docling's auto OCR-engine selection so the image/PDF engine split "
        f"stays visible; got kind={pdf_ocr_options.kind!r}"
    )


@pytest.mark.asyncio
async def test_docling_parsing_runs_in_recycled_worker_process(tmp_path: Path) -> None:
    """docling must be driven from a single-worker pool that recycles the worker.

    Only worker exit returns the native OCR residue to the OS — CPython can never unload a
    C-extension in-process, so an unbounded ``max_tasks_per_child`` reopens the leak.
    """
    _docling, calls = await _parse_png_with_docling_mocked(tmp_path)

    assert calls, (
        "docling parsing ran in the calling process — no ProcessPoolExecutor was constructed. "
        "Native OCR memory is then never returned to the OS for the life of the server."
    )
    args, kwargs = calls[0]
    max_workers = kwargs.get("max_workers", args[0] if args else None)
    assert max_workers == 1, f"docling parse pool must be single-worker, got max_workers={max_workers!r}"
    max_tasks = kwargs.get("max_tasks_per_child")
    assert isinstance(max_tasks, int) and max_tasks > 0, (
        f"docling parse worker must be recycled after a finite number of tasks, "
        f"got max_tasks_per_child={max_tasks!r}"
    )
    mp_context = kwargs.get("mp_context")
    assert mp_context is not None and mp_context.get_start_method() == "spawn", (
        "the docling parse pool must be constructed with an explicit "
        "mp_context=multiprocessing.get_context('spawn'): onnxruntime/torch are unsafe under "
        "'fork'. Note ProcessPoolExecutor itself already defaults to 'spawn' whenever "
        "max_tasks_per_child is set and no mp_context is given (CPython "
        "concurrent/futures/process.py), so an explicit spawn context here pins the safe choice "
        f"for clarity of intent rather than working around a platform default. got "
        f"mp_context={mp_context!r}"
    )
    assert kwargs.get("initializer") is _worker_initializer, (
        "the parse pool must install the orphan watchdog via initializer=_worker_initializer; "
        "without it _IS_PARSE_WORKER stays False in every real worker, so the watchdog thread "
        "never starts (a SIGKILLed parent then leaves a ~1 GB docling worker resident until "
        "reboot) and the per-worker converter cache is never populated (OCR models reload on "
        f"every file). got initializer={kwargs.get('initializer')!r}"
    )
    assert not kwargs.get("initargs"), (
        f"_worker_initializer takes no arguments; got initargs={kwargs.get('initargs')!r}"
    )


# --- marker/lane hygiene meta-test (C1-I-12) --------------------------------------------
#
# Mirrors tests/smoke/test_cli.py::test_smoke_marker_in_pyproject: a cheap, structural guard
# — not a real docling parse — asserting `@pytest.mark.docling` tests stay excluded from
# both the default lane (pyproject.toml addopts) and the two CI steps whose `-o addopts=`
# would otherwise silently re-admit them (C1-I-1). CI passes `-o addopts=`, which wipes the
# `-m "... and not docling"` filter from pyproject.toml entirely, so each CI invocation needs
# its own `not docling` — pyproject.toml alone does not protect CI.

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_FILES = (
    ".github/workflows/archon-search-pr.yml",
    ".github/workflows/archon-search-release.yml",
)
# Substrings that identify the "unit" and "integration" step run-lines specifically — the
# eval (`-m eval`) and live_benchmark (`-m live_benchmark`) steps also wipe addopts but
# select only their own marker, which no docling test carries, so they are deliberately
# excluded from this check (Task 3 explicitly does not touch them).
_STEP_FILTER_MARKERS = (
    "not live and not eval and not benchmark and not integration",  # unit step
    '"integration and not eval',  # integration step
)


def test_pyproject_default_lane_excludes_docling() -> None:
    """pyproject.toml's own addopts `-m` filter must exclude `docling` (default-lane guard)."""
    import tomllib

    pyproject_path = _REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as fp:
        data = tomllib.load(fp)
    addopts: str = data["tool"]["pytest"]["ini_options"]["addopts"]
    assert "not docling" in addopts, (
        "pyproject.toml [tool.pytest.ini_options].addopts must contain 'not docling' in its "
        "-m filter, or @pytest.mark.docling tests run (and hang) in the default suite"
    )


def test_ci_workflows_exclude_docling_from_unit_and_integration_steps() -> None:
    """Both CI workflows' unit and integration `uv run pytest` steps must re-assert
    `not docling` themselves: `-o addopts=` wipes pyproject.toml's own filter (C1-I-1), so
    without this each step's `-m` expression is the only thing standing between the
    `docling` lane and CI running (and hanging on) real OCR.
    """
    for rel_path in _WORKFLOW_FILES:
        text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        # Track coverage PER MARKER, not one flag for both: with a single flag, rewording just
        # one step's -m expression leaves that step silently unchecked while the other still
        # sets the flag — reopening exactly the C1-I-1 hole this guard exists to close.
        unmatched = set(_STEP_FILTER_MARKERS)
        for line in text.splitlines():
            if "uv run pytest" not in line or "-o addopts=" not in line:
                continue
            for marker in _STEP_FILTER_MARKERS:
                if marker not in line:
                    continue
                unmatched.discard(marker)
                assert "not docling" in line, (
                    f"{rel_path}: this uv run pytest step wipes addopts (-o addopts=) and must "
                    f"re-assert 'not docling' in its own -m filter, or docling tests run in CI:\n"
                    f"{line.strip()}"
                )
        assert not unmatched, (
            f"{rel_path}: these step filters no longer match any line: {sorted(unmatched)}. "
            "The workflow structure changed and this guard needs updating, rather than "
            "silently passing vacuously on the steps it can still find."
        )


def test_every_docling_test_is_pinned_to_the_docling_xdist_group() -> None:
    """Every `@pytest.mark.docling` test must also carry `@pytest.mark.xdist_group("docling")`.

    `addopts` mandates `-n 8 --dist=loadgroup`, so an unpinned docling test lands on an
    arbitrary second xdist worker and runs a real docling parse concurrently with the pinned
    group. Since the parse worker moved out of process, each `DocumentParser` also spawns a
    ~1 GB child — so one missing marker means two model stacks plus two OCR workers at once.
    `tests/CLAUDE.md` records stacked real-model runs OOM-crashing this 48 GB machine, which is
    the failure class this whole change exists to prevent (C2-B-2).
    """
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "tests").rglob("test_*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines):
            if line.strip() != "@pytest.mark.docling":
                continue
            # The decorator stack is contiguous; scan it in both directions from this line.
            start = lineno
            while start > 0 and lines[start - 1].lstrip().startswith("@"):
                start -= 1
            end = lineno
            while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("@"):
                end += 1
            stack = lines[start : end + 1]
            if not any('xdist_group("docling")' in decorator for decorator in stack):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno + 1}")
    assert not offenders, (
        "these @pytest.mark.docling tests are missing @pytest.mark.xdist_group(\"docling\"), so "
        "under -n 8 --dist=loadgroup they can run a real docling parse — and spawn a second "
        f"~1 GB parse worker — concurrently with the pinned group: {offenders}"
    )

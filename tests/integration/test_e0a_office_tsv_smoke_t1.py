"""E0a / T-1 — Office and TSV file ingest smoke tests (integration test replacement).

Exercises the full ingest pipeline with real Office and TSV fixture files
to verify that the file-type completeness changes from BE-1 and BE-2 work
end-to-end against a real server.

Scenarios completed:
  S1 — new Office extensions ingest without error:
       - .xlsx (Excel spreadsheet — pre-existing, regression-tested via S5) via openpyxl (transitive dep of markitdown[xlsx])
       - .eml (email, new in BE-2) round-trip
       - .epub (EPUB ebook, new in BE-2) via stdlib zipfile — no external deps
       - .rtf (Rich Text Format, new in BE-2) via minimal RTF text file — no external deps
  S2 — .tsv file routes to _parse_plain and its content is indexed as plain text

Note on .xls: the test exists (test_office_xls_ingest_and_search) but skips gracefully when
xlwt is not installed.  xlwt is not a declared dependency (markitdown[xls] only requires xlrd
for reading); xls write capability depends on optional xlwt.  The test documents the intent and
will execute when xlwt is available.

Note on .msg (Outlook): creating a real .msg file requires OLE2 binary structure (complex
without extract-msg or olefile as a write-mode dep).  Real-file .msg integration testing is
deferred; the unit test with mocked markitdown is the coverage point for .msg routing.

Each test:
- Creates a real file with known, verifiable content.
- Injects the file into a real ``SearchPipeline`` via ``ingest_file_via_path``.
- Verifies the ingest job completed without errors (status=DONE, no error field).
- Searches for a known phrase from the file and asserts at least one result is returned.
- Asserts at least one result is returned (``len(results) >= 1``) and the expected
  ``doc_id`` (SHA-256 of the resolved path) appears in the results — proves the
  correct document was retrieved.
"""
from __future__ import annotations

import hashlib
import io
import pathlib
import textwrap
import zipfile

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_doc_id(path_str: str) -> str:
    """Compute the doc_id that SearchPipeline assigns to a given path.

    The pipeline derives doc_id as SHA-256 of the resolved absolute path
    (see pipeline.py). Matches the pattern used in D8 / T-3 tests.
    """
    resolved = str(pathlib.Path(path_str).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()


def _assert_ingest_completed_cleanly(client, col: str, job_id: str, api_key: str) -> None:
    """Assert the ingest job completed without errors.

    Verifies via GET /jobs/{job_id} that the job has status=DONE and no error
    message.  ingest_file_via_path already fails the test on FAILED status, but
    this catches the case where status=DONE is set despite a non-fatal error field.

    Chunk-count guard: a broken parser backend could produce status=DONE with zero
    chunks indexed (vacuously passing).  This is caught by ``_assert_search_finds_doc``
    which asserts ``len(results) >= 1`` and confirms the expected ``doc_id`` appears —
    together these two assertions are equivalent to an explicit items_processed >= 1 check.
    """
    resp = client.get(
        f"/jobs/{job_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, (
        f"GET /jobs/{job_id} failed: {resp.status_code} {resp.text}"
    )
    job_data = resp.json()
    # Defensive: ingest_file_via_path already polls until DONE/FAILED and fails on FAILED,
    # so status is guaranteed to be DONE here.  The assertion below verifies the job_id is
    # valid and the API returned a well-formed response (status 200 with a status field).
    assert job_data.get("status") == "DONE", (
        f"ingest job {job_id!r} for collection {col!r} is not DONE: {job_data}"
    )
    assert job_data.get("error") is None, (
        f"ingest job {job_id!r} for collection {col!r} completed with error: "
        f"{job_data.get('error')!r}"
    )


def _assert_search_finds_doc(
    client,
    col: str,
    query: str,
    expected_doc_id: str,
    *,
    api_key: str,
) -> None:
    """Assert the query finds at least one result and the expected doc_id appears."""
    results = search(client, col, query, api_key=api_key)
    assert len(results) >= 1, (
        f"search({query!r}) in collection {col!r} returned no results — "
        f"ingest may have failed or chunker produced no chunks"
    )
    found_ids = [r.get("doc_id") for r in results if r.get("doc_id")]
    assert expected_doc_id in found_ids, (
        f"expected doc_id {expected_doc_id!r} not in search results for query {query!r}; "
        f"found doc_ids: {found_ids!r}"
    )


# ---------------------------------------------------------------------------
# Test 1 — Office file round-trip (.xlsx)
#
# S5: A .xlsx spreadsheet ingests without error via the markitdown[xlsx] path
# and its content is searchable.  .xlsx is a pre-existing format (regression test S5).
# openpyxl is a transitive dependency of markitdown[xlsx] and is always present.
# ---------------------------------------------------------------------------


def test_office_xlsx_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S5: .xlsx ingest round-trip — spreadsheet content indexed and searchable.

    Creates a real Excel spreadsheet (via openpyxl, a transitive dep of
    markitdown[xlsx]), ingests it via the full REST/pipeline stack, then
    searches for a known cell value.  Proves the markitdown xlsx path works
    end-to-end for .xlsx, a pre-existing Office extension (regression test S5).
    """
    openpyxl = pytest.importorskip("openpyxl")

    col = "e0a-smoke-xlsx"
    doc_dir = tmp_path / "office_docs"
    doc_dir.mkdir(parents=True)
    xlsx_path = doc_dir / "smoke_spreadsheet.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SmokeData"
    ws.append(["product", "category", "description"])
    ws.append([
        "archon-search",
        "retrieval-engine",
        "Hybrid spreadsheet retrieval augmented generation smoke test.",
    ])
    ws.append([
        "lancedb",
        "vector-store",
        "Embedded vector database for hybrid retrieval in archon-search.",
    ])
    wb.save(str(xlsx_path))

    expected_doc_id = _sha256_doc_id(str(xlsx_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(xlsx_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "spreadsheet retrieval augmented generation",
            expected_doc_id,
            api_key=api_key,
        )


# ---------------------------------------------------------------------------
# Test 2 — Office file round-trip (.eml)
#
# S1: A .eml file ingests without error via the markitdown path and its
# content is searchable.  This is a new BE-2 format.
# ---------------------------------------------------------------------------


def test_office_eml_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S1: .eml ingest round-trip — email body indexed and searchable.

    Creates a real RFC 822 email file, ingests it, and searches for a phrase
    from the email body.  Proves the markitdown email path works for .eml,
    one of the new Office extensions added in BE-2.
    """
    col = "e0a-smoke-eml"
    doc_dir = tmp_path / "email_docs"
    doc_dir.mkdir(parents=True)
    eml_path = doc_dir / "smoke_email.eml"

    eml_content = textwrap.dedent("""\
        From: sender@example.com
        To: archon@search.test
        Subject: E0a Smoke Test Email
        Date: Thu, 01 Jan 2026 12:00:00 +0000
        Content-Type: text/plain

        This is the email body for archon-search office smoke test.
        The electromagnetic document round-trip verifies eml ingest works correctly.
    """)
    eml_path.write_text(eml_content, encoding="utf-8")

    expected_doc_id = _sha256_doc_id(str(eml_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(eml_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "electromagnetic document round-trip",
            expected_doc_id,
            api_key=api_key,
        )


# ---------------------------------------------------------------------------
# Test 3 — TSV file round-trip (.tsv)
#
# S2: A .tsv file routes to _parse_plain and its tab-separated content is
# indexed as plain text.
# ---------------------------------------------------------------------------


def test_tsv_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S2: .tsv ingest round-trip — tab-separated content indexed as plain text.

    Creates a real .tsv file, ingests it via the full REST/pipeline stack,
    then searches for a known token from the TSV body.  Proves that .tsv
    files route to ``_parse_plain`` and their content is searchable.

    Note: this test confirms that `.tsv` content is indexed and searchable as
    plain text.  `.tsv` reaches ``_parse_plain`` via the catch-all else-branch
    in ``parse()``; ``_PLAIN_EXTENSIONS`` is documentation-only and not
    consulted by the routing logic.
    """
    col = "e0a-smoke-tsv"
    doc_dir = tmp_path / "tsv_docs"
    doc_dir.mkdir(parents=True)
    tsv_path = doc_dir / "smoke_data.tsv"

    tsv_content = textwrap.dedent("""\
        name\tvalue\tdescription
        archon-search\thyperparameter-tsv-retrieval\tTabular smoke test data
        lancedb\tvector-store\tEmbedded vector database for hybrid retrieval
        markitdown\toffice-parser\tDocument converter for Office formats
    """)
    tsv_path.write_text(tsv_content, encoding="utf-8")

    expected_doc_id = _sha256_doc_id(str(tsv_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(tsv_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "hyperparameter-tsv-retrieval",
            expected_doc_id,
            api_key=api_key,
        )


# ---------------------------------------------------------------------------
# Test 4 — Office file round-trip (.epub)
#
# S1: A .epub ebook ingests without error via the markitdown path and its
# content is searchable.  This is a new BE-2 format.
# The .epub file is created entirely from stdlib (zipfile + io) — no external
# package dependencies.
# ---------------------------------------------------------------------------


def test_office_epub_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S1: .epub ingest round-trip — ebook content indexed and searchable.

    Creates a minimal valid EPUB 3 file using only stdlib (zipfile + io),
    ingests it via the full REST/pipeline stack, then searches for a known
    phrase from the chapter content.  Proves the markitdown epub path works
    end-to-end for .epub, one of the new Office extensions added in BE-2.

    An EPUB file is a ZIP archive with specific structure — no external
    dependencies are required to produce a valid one.
    """
    col = "e0a-smoke-epub"
    doc_dir = tmp_path / "epub_docs"
    doc_dir.mkdir(parents=True)
    epub_path = doc_dir / "smoke_book.epub"

    chapter_body = (
        "This epub document contains verifiable content for archon-search "
        "epub integration testing.  The ebook format is parsed via markitdown."
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        # mimetype must be the first entry and uncompressed (EPUB spec requirement)
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles>"
            '<rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>'
            "</rootfiles>"
            "</container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
            ' unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Archon Search Smoke Test</dc:title>"
            "<dc:identifier id=\"uid\">archon-smoke-epub-1</dc:identifier>"
            "<dc:language>en</dc:language>"
            "</metadata>"
            "<manifest>"
            '<item id="c1" href="chapter1.xhtml"'
            ' media-type="application/xhtml+xml"/>'
            "</manifest>"
            "<spine>"
            '<itemref idref="c1"/>'
            "</spine>"
            "</package>",
        )
        zf.writestr(
            "OEBPS/chapter1.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<!DOCTYPE html>"
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Chapter 1</title></head>"
            f"<body><p>{chapter_body}</p></body>"
            "</html>",
        )
    epub_path.write_bytes(buf.getvalue())

    expected_doc_id = _sha256_doc_id(str(epub_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(epub_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "epub integration testing",
            expected_doc_id,
            api_key=api_key,
        )


# ---------------------------------------------------------------------------
# Test 5 — Office file round-trip (.rtf)
#
# S1: A .rtf file ingests without error via the markitdown path and its
# content is searchable.  RTF is a text-based format — no external deps
# are needed to create a minimal valid RTF file.
# ---------------------------------------------------------------------------


def test_office_rtf_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S1: .rtf ingest round-trip — RTF content indexed and searchable.

    Creates a minimal valid RTF file (text-based format, no deps required),
    ingests it via the full REST/pipeline stack, then searches for a known
    phrase.  Proves the markitdown RTF path works for .rtf, one of the new
    Office extensions added in BE-2.

    Note: markitdown processes .rtf files and returns the content with RTF
    control codes intact (no stripping).  The test phrase appears verbatim in
    the raw output, so the search succeeds.  The test verifies that ingest and
    search work end-to-end — not that the content is human-readable.
    """
    col = "e0a-smoke-rtf"
    doc_dir = tmp_path / "rtf_docs"
    doc_dir.mkdir(parents=True)
    rtf_path = doc_dir / "smoke_doc.rtf"

    rtf_content = (
        r"{\rtf1\ansi\deff0"
        r"{\fonttbl{\f0\froman\fcharset0 Times New Roman;}}"
        r"\pard\f0\fs24 This is an rtf document for archon rtf smoke testing."
        r" The rtf format verification confirms correct routing through markitdown."
        r"\par}"
    )
    rtf_path.write_text(rtf_content, encoding="ascii")

    expected_doc_id = _sha256_doc_id(str(rtf_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(rtf_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "archon rtf smoke testing",
            expected_doc_id,
            api_key=api_key,
        )


# ---------------------------------------------------------------------------
# Test 6 — Office file round-trip (.xls)
#
# S1: A .xls file ingests without error via the markitdown path and its
# content is searchable.  This test requires xlwt for file creation;
# it skips gracefully when xlwt is not installed (xlwt is not a declared
# dep — markitdown[xls] only pulls xlrd for reading).
# ---------------------------------------------------------------------------


def test_office_xls_ingest_and_search(tmp_path, monkeypatch) -> None:
    """S1: .xls ingest round-trip — Excel 97 spreadsheet content indexed and searchable.

    Creates a real Excel 97-2003 workbook using xlwt (skips if unavailable),
    ingests it via the full REST/pipeline stack, then searches for a known
    cell value.  Proves the markitdown xls path works end-to-end for .xls,
    one of the new Office extensions added in BE-2.

    xlwt is not a declared dependency (markitdown[xls] requires xlrd for read
    only).  This test skips gracefully when xlwt is absent; it executes when
    xlwt is available in the environment.
    """
    xlwt = pytest.importorskip("xlwt")

    col = "e0a-smoke-xls"
    doc_dir = tmp_path / "xls_docs"
    doc_dir.mkdir(parents=True)
    xls_path = doc_dir / "smoke_spreadsheet.xls"

    wb = xlwt.Workbook()
    ws = wb.add_sheet("SmokeData")
    ws.write(0, 0, "product")
    ws.write(0, 1, "category")
    ws.write(0, 2, "description")
    ws.write(1, 0, "archon-search")
    ws.write(1, 1, "retrieval-engine")
    ws.write(1, 2, "Hybrid xls-spreadsheet retrieval augmented generation smoke test.")
    ws.write(2, 0, "lancedb")
    ws.write(2, 1, "vector-store")
    ws.write(2, 2, "Embedded vector database for xls hybrid retrieval in archon-search.")
    wb.save(str(xls_path))

    expected_doc_id = _sha256_doc_id(str(xls_path))

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        job_id = ingest_file_via_path(
            client, col, str(xls_path), api_key=api_key, timeout_s=30.0
        )
        _assert_ingest_completed_cleanly(client, col, job_id, api_key)
        _assert_search_finds_doc(
            client,
            col,
            "xls-spreadsheet retrieval augmented generation",
            expected_doc_id,
            api_key=api_key,
        )

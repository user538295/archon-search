"""Tests for E2j FE-1: self-contained graph viewer HTML page.

All tests read the file directly — no running server needed.
"""
from __future__ import annotations

import hashlib
import importlib.resources
import re
from pathlib import Path

import pytest

_SERVER_PKG = importlib.resources.files("archon_search.server")
_HTML_PATH = Path(__file__).resolve().parents[2] / "archon_search" / "server" / "graph_viewer.html"
_SHA256_PATH = Path(__file__).resolve().parents[2] / "archon_search" / "server" / "graph_viewer.html.sha256"


def _html_bytes() -> bytes:
    return _HTML_PATH.read_bytes()


def _html_text() -> str:
    return _html_bytes().decode("utf-8")


def _strip_vendor_block(html: str) -> str:
    """Return HTML with the vis-network vendor script block removed."""
    return re.sub(
        r'<script id="vendor-vis-network">.*?</script>',
        "",
        html,
        flags=re.DOTALL,
    )


def _extract_vendor_js(html: str) -> str:
    """Return just the JS text inside <script id="vendor-vis-network">…</script>."""
    m = re.search(
        r'<script id="vendor-vis-network">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m is not None, "No <script id='vendor-vis-network'> block found in HTML"
    return m.group(1)


# ---------------------------------------------------------------------------
# Test 1: file is loadable as a package resource
# ---------------------------------------------------------------------------

def test_viewer_html_is_loadable_as_package_resource() -> None:
    """importlib.resources can find and read graph_viewer.html."""
    data = _SERVER_PKG.joinpath("graph_viewer.html").read_bytes()
    assert len(data) > 0, "graph_viewer.html is empty"


# ---------------------------------------------------------------------------
# Test 2: canvas present, placeholders present, SHA-256 sidecar matches
# ---------------------------------------------------------------------------

def test_viewer_html_contains_canvas_and_placeholders() -> None:
    """HTML contains required render container and all four C4 placeholder tokens."""
    html = _html_text()

    # vis-network creates <canvas> dynamically inside #network-container
    assert 'id="network-container"' in html, "Missing #network-container (vis-network render target)"
    assert "__ARCHON_TOKEN__" in html, "Missing __ARCHON_TOKEN__ placeholder"
    assert "__ARCHON_COLLECTION__" in html, "Missing __ARCHON_COLLECTION__ placeholder"
    assert "__ARCHON_MAX_NODES__" in html, "Missing __ARCHON_MAX_NODES__ placeholder"
    assert "__ARCHON_MAX_EDGES__" in html, "Missing __ARCHON_MAX_EDGES__ placeholder"

    # Verify placeholders are BARE in JS (not surrounded by quotes)
    # The server substitutes json.dumps(value) which adds quotes.
    # If placeholder is already in quotes → ""tok"" = JS syntax error.
    assert not re.search(r'["\']__ARCHON_TOKEN__["\']', html), (
        "__ARCHON_TOKEN__ must appear bare in JS (not in quotes) — "
        "the server substitutes json.dumps(value) which adds the quotes"
    )
    assert not re.search(r'["\']__ARCHON_COLLECTION__["\']', html), (
        "__ARCHON_COLLECTION__ must appear bare in JS (not in quotes)"
    )

    # Verify SHA-256 sidecar
    vendor_js = _extract_vendor_js(html)
    computed = hashlib.sha256(vendor_js.encode("utf-8")).hexdigest()
    recorded = _SHA256_PATH.read_text().strip()
    assert computed == recorded, (
        f"SHA-256 mismatch for vendor-vis-network block.\n"
        f"  Computed: {computed}\n"
        f"  Recorded: {recorded}\n"
        "Re-run the hash computation and update graph_viewer.html.sha256."
    )


# ---------------------------------------------------------------------------
# Test 3: no external URLs in app code (vendor block excluded)
# ---------------------------------------------------------------------------

def test_viewer_html_no_external_urls() -> None:
    """App code (outside vendor block) makes no external requests or CDN loads."""
    html = _html_text()
    app_html = _strip_vendor_block(html)

    assert '<script src="https://' not in app_html, "External <script src> found"
    assert '<link href="https://' not in app_html, "External <link href> found"
    assert 'fetch("https://' not in app_html, "External fetch() with https:// found"
    assert "fetch('https://" not in app_html, "External fetch() with https:// (single-quote) found"
    assert "new XMLHttpRequest" not in app_html, "XMLHttpRequest found — use fetch instead"


# ---------------------------------------------------------------------------
# Test 4: HTMX-compatible (no Shadow DOM, no custom elements, no type=module)
# ---------------------------------------------------------------------------

def test_viewer_htmx_compatible() -> None:
    """HTML has no Shadow DOM, no custom elements, no type=module app scripts."""
    html = _html_text()

    assert "shadowRoot" not in html, "shadowRoot found — violates HTMX-compatible requirement"
    assert "customElements.define(" not in html, "customElements.define() found — no Web Components"
    assert "<template" not in html, "<template> element found — violates HTMX-compatible requirement"

    # The app's own <script> blocks (not the vendor block) must not use type="module"
    app_html = _strip_vendor_block(html)
    # Find all <script ...> opening tags in app code
    script_tags = re.findall(r"<script\b[^>]*>", app_html, flags=re.IGNORECASE)
    for tag in script_tags:
        assert 'type="module"' not in tag and "type='module'" not in tag, (
            f"App script tag uses type=module: {tag!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: mulberry32 PRNG overrides Math.random for fixed-seed layout
# ---------------------------------------------------------------------------

def test_viewer_html_overrides_math_random() -> None:
    """HTML source contains mulberry32 PRNG and overrides Math.random before vis-network init."""
    html = _html_text()

    assert "mulberry32" in html, "Expected 'mulberry32' PRNG function definition in HTML"
    assert "Math.random =" in html, "Expected 'Math.random =' override in HTML"

    # The override must appear before vis.Network initialization for deterministic layout
    override_pos = html.index("Math.random =")
    network_init_pos = html.index("new vis.Network(")
    assert override_pos < network_init_pos, (
        "Math.random override must appear before 'new vis.Network(' for fixed-seed layout"
    )

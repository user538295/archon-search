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


def _extract_function_body(html: str, func_name: str) -> str:
    """Return the true body of ``function func_name(...) { ... }`` via brace-depth counting.

    Locating the closing brace with a fixed substring like ``"\\n}"`` silently
    truncates (or over-extends) whenever the real closing brace isn't at column
    0 or a nested block closes at column 0 first. Counting brace depth from the
    function's opening ``{`` finds the true matching close.
    """
    start = html.index("function " + func_name)
    open_brace = html.index("{", start)
    depth = 0
    for i in range(open_brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError(f"Unbalanced braces while extracting function {func_name!r}")


def _extract_json_like_block(html: str, const_decl: str) -> str:
    """Return the ``{ ... }`` object-literal body following ``const_decl`` (e.g. ``"const FOO"``),
    via brace-depth counting (robust to nested braces, unlike a fixed ``"};"`` search)."""
    start = html.index(const_decl)
    open_brace = html.index("{", start)
    depth = 0
    for i in range(open_brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[open_brace : i + 1]
    raise AssertionError(f"Unbalanced braces while extracting block for {const_decl!r}")


def _extract_undirected_relationship_types(html: str) -> set[str]:
    """Return the string values inside ``const UNDIRECTED_RELATIONSHIP_TYPES = [ ... ]``.

    Spans newlines (unlike a single-line slice) so reformatting the array onto
    multiple lines can't silently yield an empty/wrong set.
    """
    m = re.search(
        r"const UNDIRECTED_RELATIONSHIP_TYPES\s*=\s*\[(.*?)\]", html, re.DOTALL
    )
    assert m is not None, "Expected 'const UNDIRECTED_RELATIONSHIP_TYPES = [...]' in HTML"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _assert_no_external_urls(html: str) -> None:
    """Shared assertion body for the no-external-URL guard (used by two tests)."""
    app_html = _strip_vendor_block(html)

    assert '<script src="https://' not in app_html, "External <script src> found"
    assert '<link href="https://' not in app_html, "External <link href> found"
    assert 'fetch("https://' not in app_html, "External fetch() with https:// found"
    assert "fetch('https://" not in app_html, "External fetch() with https:// (single-quote) found"
    assert "new XMLHttpRequest" not in app_html, "XMLHttpRequest found — use fetch instead"


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
    """App code (outside vendor block) makes no external requests or CDN loads.

    Also serves as FE-1's required no-external-URL regression guard for any
    subsequent app-code edits (e.g. the buildVisEdge rewrite) — no separate
    "still has no external urls" test is needed.
    """
    _assert_no_external_urls(_html_text())


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


# ---------------------------------------------------------------------------
# Test 6: relationship-type colour map (S34/FE-1)
# ---------------------------------------------------------------------------

def test_viewer_defines_relationship_colour_map() -> None:
    """buildVisEdge is backed by a relationship-keyed colour map distinct from TYPE_COLORS,
    and it is exhaustive over the real RelationshipType enum (except related_to)."""
    from archon_search.graph_types import RelationshipType

    html = _html_text()

    assert "RELATIONSHIP_COLORS" in html, "Expected a RELATIONSHIP_COLORS map for edge colouring"

    type_colors_block = _extract_json_like_block(html, "const TYPE_COLORS")
    relationship_colors_block = _extract_json_like_block(html, "const RELATIONSHIP_COLORS")
    assert type_colors_block != relationship_colors_block, (
        "RELATIONSHIP_COLORS must have different contents than TYPE_COLORS "
        "(a real distinctness check, not a string-position comparison)"
    )

    # Every real RelationshipType value except related_to must key the map — derived from
    # the enum itself so the test can't drift from the wire schema, and related_to must be
    # absent so it falls back to DEFAULT_EDGE_COLOR.
    all_types = {m.value for m in RelationshipType}
    assert "related_to" in all_types  # sanity: the enum still defines it
    expected_keyed = all_types - {"related_to"}
    for rtype in expected_keyed:
        assert re.search(rf"\b{rtype}\s*:", relationship_colors_block), (
            f"Expected {rtype!r} to appear as a real key (not just a substring, e.g. in a "
            f"comment) in RELATIONSHIP_COLORS"
        )
    assert not re.search(r"\brelated_to\s*:", relationship_colors_block), (
        "related_to must NOT be a key in RELATIONSHIP_COLORS — it must fall back to "
        "DEFAULT_EDGE_COLOR"
    )

    # Reverse check: no stray/stale key exists in RELATIONSHIP_COLORS beyond the
    # enum-derived expected set (catches drift when RelationshipType shrinks or renames).
    actual_keys = set(re.findall(r"^\s*(\w+)\s*:", relationship_colors_block, re.MULTILINE))
    assert actual_keys == expected_keyed, (
        f"RELATIONSHIP_COLORS keys {actual_keys!r} do not match the expected "
        f"enum-derived set {expected_keyed!r} — drift or a stale key present"
    )

    # buildVisEdge must actually consult the map when building an edge's colour.
    build_vis_edge_body = _extract_function_body(html, "buildVisEdge")
    assert "RELATIONSHIP_COLORS" in build_vis_edge_body, (
        "buildVisEdge must use RELATIONSHIP_COLORS to colour edges"
    )


# ---------------------------------------------------------------------------
# Test 7: arrows only on directional relationship types
# ---------------------------------------------------------------------------

def test_viewer_sets_arrows_only_for_directional_types() -> None:
    """related_to and synonym_of (undirected/symmetric relations) get no arrowhead;
    directional types do — verified structurally, not just by substring presence."""
    html = _html_text()
    build_vis_edge_body = _extract_function_body(html, "buildVisEdge")

    # UNDIRECTED_RELATIONSHIP_TYPES must name exactly the two undirected relation types —
    # not merely contain them, so a stray extra entry (silently de-arrowing another type)
    # is caught.
    undirected_values = _extract_undirected_relationship_types(html)
    assert undirected_values == {"related_to", "synonym_of"}, (
        f"Expected UNDIRECTED_RELATIONSHIP_TYPES to be exactly "
        f"{{'related_to', 'synonym_of'}}, got {undirected_values!r}"
    )

    # The 'arrows' key assignment must appear guarded by an if/ternary tied to a directional
    # check, not unconditionally on the edge object literal returned by the function.
    edge_literal_match = re.search(r"var\s+edge\s*=\s*\{(.*?)\n\s*\};", build_vis_edge_body, re.DOTALL)
    assert edge_literal_match is not None, "Expected a 'var edge = {...};' object literal"
    assert "arrows" not in edge_literal_match.group(1), (
        "'arrows' must NOT be set unconditionally inside the edge object literal — "
        "it must be assigned only inside a directional guard"
    )

    # After the literal, there must be a conditional (if/ternary) block that sets
    # edge.arrows, keyed off a directional/undirected check.
    tail = build_vis_edge_body[edge_literal_match.end() :]
    guarded_arrows = re.search(
        r'if\s*\(\s*isDirectional\s*\)\s*\{[^}]*\.arrows\s*=\s*"to"', tail,
    )
    assert guarded_arrows is not None, (
        "Expected 'edge.arrows = \"to\"' to be set inside an 'if (isDirectional) { ... }' guard, "
        "after the edge object literal — not unconditionally, and not reversed to \"from\""
    )

    # isDirectional itself must be derived from membership in UNDIRECTED_RELATIONSHIP_TYPES,
    # and must be false (not true) for a listed/undirected type — i.e. the check is the
    # right way round (=== -1 meaning "not found" => directional), not inverted.
    is_directional_decl = re.search(
        r"isDirectional\s*=\s*.*UNDIRECTED_RELATIONSHIP_TYPES\.indexOf\([^)]*\)\s*===\s*-1",
        build_vis_edge_body,
        re.DOTALL,
    )
    assert is_directional_decl is not None, (
        "Expected 'isDirectional' to be computed as "
        "'UNDIRECTED_RELATIONSHIP_TYPES.indexOf(...) === -1' (not '!== -1', which would invert "
        "the condition and arrow every edge including related_to/synonym_of)"
    )


# ---------------------------------------------------------------------------
# Test 8: edge hover label (title: e.relationship_type) survives the rewrite
# ---------------------------------------------------------------------------

def test_edge_hover_label_survives_the_rewrite() -> None:
    """Colour is not the only cue for relationship type — the hover tooltip must remain (Q32)."""
    html = _html_text()

    assert "title: e.relationship_type" in html, (
        "Expected 'title: e.relationship_type' hover tooltip to remain set in buildVisEdge"
    )


# ---------------------------------------------------------------------------
# Test 9: no-external-URL guard still passes after the edit
# ---------------------------------------------------------------------------

def test_build_vis_edge_hardens_against_missing_or_hostile_relationship_type() -> None:
    """Missing relationship_type must not fall into the directional/arrowed branch, and the
    colour lookup must not be a bare object-literal index (prototype-pollution shaped)."""
    html = _html_text()
    build_vis_edge_body = _extract_function_body(html, "buildVisEdge")

    # isDirectional must short-circuit falsy relationship_type before consulting the list.
    assert re.search(
        r"isDirectional\s*=\s*!!\s*e\.relationship_type\s*&&", build_vis_edge_body,
    ), "Expected isDirectional to require a truthy relationship_type before the list check"

    # A missing/null relationship_type must also fall back to DEFAULT_EDGE_COLOR, not just
    # skip the arrowhead — hasOwnProperty on RELATIONSHIP_COLORS is false for undefined/null
    # keys, so the ternary's false branch (DEFAULT_EDGE_COLOR) must be reachable.
    assert re.search(
        r"hasColor\s*\?\s*RELATIONSHIP_COLORS\[[^\]]*\]\s*:\s*DEFAULT_EDGE_COLOR",
        build_vis_edge_body,
    ), "Expected the colour lookup to fall back to DEFAULT_EDGE_COLOR when hasColor is false"

    # Colour lookup must use hasOwnProperty (or equivalent) rather than a bare `map[key]`.
    assert "hasOwnProperty" in build_vis_edge_body, (
        "Expected a hasOwnProperty guard around the RELATIONSHIP_COLORS lookup to avoid "
        "resolving prototype members like 'constructor' or '__proto__'"
    )
    assert "RELATIONSHIP_COLORS[e.relationship_type] ||" not in build_vis_edge_body, (
        "Bare 'RELATIONSHIP_COLORS[e.relationship_type] || DEFAULT_EDGE_COLOR' lookup is "
        "prototype-pollution shaped — use a hasOwnProperty-guarded lookup instead"
    )

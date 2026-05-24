"""Tripwire over textual + AST patterns; reviewers must inspect every `# noqa: durable-write` line. Known bypasses NOT detected by current rules: `open(path, 'w')` + `f.write()`, `pathlib.Path.open('w')`, `shutil.copy*`, `tempfile.NamedTemporaryFile(delete=False)` + `os.link`. Reviewers must spot these in PR review; patch the detector when an instance is discovered in the wild. Note: telemetry/writer.py uses `path.open("ab")`, which is NOT in the pattern set, so no telemetry carve-out was needed in Phase 2."""

import ast
import re
from pathlib import Path


ARCHON_SEARCH_PKG = Path(__file__).parent.parent / "archon_search"

# The durable-write helper itself is the sanctioned home for these patterns.
HELPER_FILENAME = "_durable_io.py"

NOQA_MARKER = "# noqa: durable-write"

EXPLAINER = (
    "Raw durable write detected — route through archon_search/_durable_io.py "
    "(atomic_write_json/atomic_write_bytes), or add '# noqa: durable-write' "
    "with justification if this write is intentionally out of A7 scope."
)

# Single-line regex patterns matched per source line.
LINE_PATTERNS = [
    re.compile(r"\bos\.replace\("),
    re.compile(r"\bos\.rename\("),
    re.compile(r"\.rename\("),
    re.compile(r"\.write_text\("),
    re.compile(r"\.write_bytes\("),
    re.compile(r"\bshutil\.move\("),
]


def _line_is_allowed(lines: list[str], lineno: int) -> bool:
    """Return True if the 1-based line number carries a durable-write noqa."""
    if lineno < 1 or lineno > len(lines):
        return False
    return NOQA_MARKER in lines[lineno - 1]


def _references_o_creat(node: ast.AST) -> bool:
    """Recursively check whether an AST node references an O_CREAT flag.

    Handles the bare attribute (``os.O_CREAT``) as well as the flag being
    OR-ed together with other open flags (``os.O_WRONLY | os.O_CREAT | ...``).
    """
    if isinstance(node, ast.Attribute) and node.attr == "O_CREAT":
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _references_o_creat(node.left) or _references_o_creat(node.right)
    return False


def _o_creat_lineno(node: ast.AST) -> int | None:
    """Return the line number of the O_CREAT token within ``node``, if any."""
    if isinstance(node, ast.Attribute) and node.attr == "O_CREAT":
        return node.lineno
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _o_creat_lineno(node.left)
        if left is not None:
            return left
        return _o_creat_lineno(node.right)
    return None


def _is_os_open(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def find_violations(root: Path) -> list[tuple[str, int, str]]:
    """Walk ``root`` for raw durable-write patterns.

    Returns a list of ``(relpath, lineno, stripped_source_line)`` tuples for
    every non-allow-listed violation. ``_durable_io.py`` is excluded since it
    is the sanctioned home for these patterns.
    """
    violations: list[tuple[str, int, str]] = []

    for py_file in sorted(root.rglob("*.py")):
        if py_file.name == HELPER_FILENAME:
            continue

        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        relpath = str(py_file.relative_to(root))

        # Part 1: single-line regex patterns.
        for idx, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in LINE_PATTERNS):
                if _line_is_allowed(lines, idx):
                    continue
                violations.append((relpath, idx, line.strip()))

        # Part 2: AST detector for os.open(..., O_CREAT, ...).
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            # A syntactically broken file can't be statically scanned; the
            # regex pass above still applies. Skip the AST pass for it.
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_os_open(node):
                continue
            operands: list[ast.AST] = list(node.args)
            operands.extend(kw.value for kw in node.keywords)
            if not any(_references_o_creat(arg) for arg in operands):
                continue

            # The violation is allow-listed if the call line OR the line
            # holding the O_CREAT token carries the noqa marker.
            call_lineno = node.lineno
            token_lineno = None
            for arg in operands:
                token_lineno = _o_creat_lineno(arg)
                if token_lineno is not None:
                    break

            if _line_is_allowed(lines, call_lineno):
                continue
            if token_lineno is not None and _line_is_allowed(lines, token_lineno):
                continue

            report_lineno = call_lineno
            stripped = lines[report_lineno - 1].strip() if report_lineno <= len(lines) else ""
            violations.append((relpath, report_lineno, stripped))

    return violations


def test_no_raw_durable_writes() -> None:
    """No file in archon_search/ may use a raw durable-write pattern."""
    assert ARCHON_SEARCH_PKG.is_dir(), (
        f"archon_search package not found at {ARCHON_SEARCH_PKG}"
    )

    violations = find_violations(ARCHON_SEARCH_PKG)

    assert not violations, (
        f"{EXPLAINER}\nFound {len(violations)} violation(s):\n"
        + "\n".join(f"{rel}:{lineno}: {line}" for rel, lineno, line in violations)
    )

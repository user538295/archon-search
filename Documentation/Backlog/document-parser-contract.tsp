/**
 * TypeSpec / Python impedance note
 * ─────────────────────────────────────────────────────────────────────────────
 * TypeSpec has no first-class concept of "raises on failure". The union type
 * `ParseResult` below is a *logical documentation model* — it captures the two
 * possible outcomes at the type level so that readers and tooling can reason
 * about the contract.
 *
 * **Python reality:**
 *   - `DocumentParser.parse()` is `async def parse(self, path: Path) -> str`.
 *   - On failure it *raises* `ParseError` — it never returns it.
 *   - Callers use `try / except ParseError`, not pattern-matching on a union.
 *   - `ParseResult` and its `failure` variant are never instantiated at runtime.
 *
 * Type mapping notes:
 *   - `path: string` — Python uses `pathlib.Path`; TypeSpec has no Path type,
 *     so `string` is the string-projection of `Path`. The seam receives a str.
 *   - `cause: string` — Python stores an `Exception` object on `ParseError.cause`;
 *     callers stringify it as `str(exc.cause)`. Typed as `string` here as the
 *     string projection of the Exception.
 * ─────────────────────────────────────────────────────────────────────────────
 */
namespace DocumentParserContract;

/**
 * Raised when a document cannot be parsed; carries the file path and root cause.
 *
 * Python: `ParseError` is a dataclass / exception subclass.
 *   - `path`  → `pathlib.Path` at runtime; `string` here is the string projection.
 *   - `cause` → `Exception` object at runtime; `string` here is the string projection
 *               (callers do `str(exc.cause)`).
 */
model ParseError {
  path: string;
  cause: string;
}

/**
 * Logical model for documentation purposes only — Python uses raise/except, not
 * a returned union.
 *
 * The two logical outcomes of a parse attempt:
 *   - `success` — extracted text returned to the caller.
 *   - `failure` — a `ParseError` raised (never returned) by the implementation.
 *
 * No Python caller ever pattern-matches on this union; it exists here to make
 * the two outcomes explicit in the contract for human readers.
 */
union ParseResult {
  success: string;
  failure: ParseError;
}

/**
 * Seam: Frameworks & Drivers → Use Cases (pipeline.py)
 *
 * `DocumentParser.parse()` is `async` in Python. It accepts a file path and
 * returns extracted text on success, or *raises* `ParseError` on failure.
 * Callers need only handle `ParseError`; they never receive a `ParseResult` union.
 *
 * The return type below is written as `ParseResult` for TypeSpec modeling
 * completeness. In Python the signature is:
 *   async def parse(self, path: Path) -> str  # raises ParseError
 *
 * Extension support (OFFICE/PLAIN routing) is internal to the implementation;
 * the seam shape is unchanged by E0a.
 */
interface DocumentParser {
  // async in Python; raises ParseError on failure (never returns the failure variant)
  parse(path: string): ParseResult;
}

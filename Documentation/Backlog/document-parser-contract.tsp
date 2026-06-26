namespace DocumentParserContract;

/** Raised when a document cannot be parsed; carries the file path and root cause. */
model ParseError {
  path: string;
  cause: string;
}

/** Result of a parse attempt — either extracted text or a structured error. */
union ParseResult {
  success: string;
  failure: ParseError;
}

/**
 * Seam: Frameworks & Drivers → Use Cases (pipeline.py)
 *
 * DocumentParser.parse() accepts a file path and returns extracted text,
 * or raises ParseError. Callers need only handle ParseError.
 * Extension support (OFFICE/PLAIN routing) is internal to the implementation.
 */
interface DocumentParser {
  parse(path: string): ParseResult;
}

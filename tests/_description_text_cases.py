"""The texts a description must be refused for (S61), shared by the unit test of the rule and the route tests."""
from __future__ import annotations

from archon_search.constants import MAX_DESCRIPTION_CHARS

#: Case id -> text that is empty, not one line of plain text, or over the limit.
REJECTED_TEXTS: dict[str, str] = {
    "empty": "",
    "spaces-only": "   ",
    "invisible-only": "\u200b\u2060\ufeff",
    "line-feed": "first line\nsecond line",
    "carriage-return-line-feed": "first line\r\nsecond line",
    "carriage-return": "one\rtwo",
    "tab": "column\tcolumn",
    "vertical-tab": "one\x0btwo",
    "form-feed": "one\x0ctwo",
    "next-line": "one\x85two",
    "unicode-line-separator": "one\u2028two",
    "unicode-paragraph-separator": "one\u2029two",
    "nul": "nul\x00byte",
    "bell": "bell\x07ring",
    "escape": "escape\x1b[31mred",
    "delete": "delete\x7fchar",
    "c1-control": "c1\x9fcontrol",
    "one-over-the-limit": "x" * (MAX_DESCRIPTION_CHARS + 1),
    "far-over-the-limit": "x" * (MAX_DESCRIPTION_CHARS * 3),
}

"""Parser-raised exception carrying enough context to point at the offence.

Principles & invariants
-----------------------
* ``ParseError`` is the *only* exception type raised by the AsciiDoc
  parser, lexer, and inline parser. Callers must never catch
  :class:`Exception` to fish out parse failures — they catch this type by
  name.
* Every instance carries a ``kind`` so the UI can render context-specific
  help text without parsing the human-readable ``message``. New parse
  failures must therefore extend :class:`ParseErrorKind` rather than
  inventing a new exception class.
* ``line`` and ``column`` are 1-indexed and refer to the offending position
  in the original source as the user typed it. ``column == 0`` is reserved
  for "whole line" failures where pointing at a column would be
  misleading.
* The exception is informational only — it carries no fix-it suggestions,
  no auto-correction state. The rendered view simply pauses re-rendering
  and shows the message; the source text the user typed is preserved
  verbatim.
"""

from __future__ import annotations

from notes_app.enums import ParseErrorKind


class ParseError(Exception):
    """Raised by the AsciiDoc parser for any syntactic violation."""

    line: int
    column: int
    message: str
    kind: ParseErrorKind

    def __init__(
        self,
        line: int,
        column: int,
        message: str,
        kind: ParseErrorKind,
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column
        self.message = message
        self.kind = kind

    def __str__(self) -> str:
        if self.column > 0:
            return f"line {self.line}, col {self.column}: {self.message}"
        return f"line {self.line}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"ParseError(line={self.line!r}, column={self.column!r}, "
            f"message={self.message!r}, kind={self.kind!r})"
        )

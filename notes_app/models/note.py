"""The :class:`Note` dataclass and its pure derivation helpers.

Principles & invariants
-----------------------
* :class:`Note` is the in-memory shape of a row in the ``notes`` table —
  every field corresponds 1:1 to a column. The dataclass is frozen so
  callers cannot mutate state in place; updates flow through the
  repository, which produces a new instance.
* :func:`derive_title` and :func:`derive_snippet` are pure, deterministic
  functions of the source string. They must run in O(n) time on the source
  length and never raise. The repository invokes them at write-time and
  caches the result in the ``title`` and ``snippet`` columns; the UI never
  re-derives at display-time.
* The derivers operate on a *prefix* of source — they do not run a full
  parser. They are deliberately tolerant of malformed input: when the
  source isn't valid AsciiDoc the parser still raises in the renderer, but
  the note list keeps showing whatever fallback string these helpers chose
  so the user can at least find their note and fix it.
* A note's title is "Untitled" iff the source has no level-0 heading on
  the first non-blank line. The fallback string is part of the persistence
  contract — UI code that renders an empty title to grey it out should
  test the source itself, not the title field.
* Snippets are never longer than :data:`SNIPPET_MAX_CHARS`. They are
  bounded so the note-list query plan stays cheap and the rendered list
  cells have a predictable height.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


SNIPPET_MAX_CHARS: int = 200
"""Hard cap for snippet length, in characters."""

UNTITLED: str = "Untitled"
"""Fallback title used when the source has no level-0 heading."""

_BLOCK_DELIMITERS: frozenset[str] = frozenset({"----", "|===", "____", "===="})
"""Block-fence lines we strip from snippets — they would render as noise."""

_LEVEL_ZERO_PREFIX: str = "= "
"""The literal prefix of a level-0 AsciiDoc heading (equals + single space).

A line that starts with ``=`` but not ``= `` is either a section heading
(``==`` and deeper) or malformed — neither is a level-0 title.
"""


@dataclass(frozen=True)
class Note:
    """A single note as it appears both in storage and in memory.

    Fields
    ------
    id:
        Stable identifier (a UUID-shaped string in production; opaque
        elsewhere).
    title:
        The level-0 heading derived from ``source`` at write time, or
        ``"Untitled"`` if absent. Stored verbatim in the ``notes.title``
        column so the note-list query never re-parses source.
    notebook_id:
        The owning notebook. Required — every note lives in a notebook.
    source:
        The full AsciiDoc source as the user typed it. The user's typed
        text is sacred: it is persisted even when it cannot be parsed, and
        the renderer is the only consumer that requires valid syntax.
    snippet:
        A short, plain-text-ish preview produced by
        :func:`derive_snippet` and cached. Bounded by
        :data:`SNIPPET_MAX_CHARS`.
    created_at, modified_at:
        Timezone-aware UTC timestamps. The repository converts these to
        ISO-8601 strings on the way to SQLite.
    """

    id: str
    title: str
    notebook_id: str
    source: str
    snippet: str
    created_at: datetime
    modified_at: datetime


def derive_title(source: str) -> str:
    """Return the level-0 heading text, or :data:`UNTITLED` if absent.

    The level-0 heading must be the first non-blank line of the source and
    must start with ``"= "`` (equals + single space). Anything else — a
    blank source, a deeper heading first (``==``), a paragraph first —
    yields the fallback.
    """
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith(_LEVEL_ZERO_PREFIX):
            title = stripped[len(_LEVEL_ZERO_PREFIX):].strip()
            return title if title else UNTITLED
        return UNTITLED
    return UNTITLED


def derive_snippet(source: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Return a short, plain preview of the source body.

    The level-0 title (if present) is skipped, then non-content lines —
    section headings, block fences, attribute lines, image macros — are
    filtered. Surviving lines are joined with single spaces and truncated
    to ``max_chars`` (with an ellipsis suffix when truncation occurs).

    This helper does not invoke the parser, so it cannot raise on
    malformed input. It deliberately keeps inline markers like ``*`` and
    ``_`` in place: a "rendered" snippet would require the parser, and a
    half-stripped string is more confusing than the raw source.
    """
    if max_chars <= 0:
        return ""

    content_lines: list[str] = []
    accumulated = 0
    title_line_consumed = False

    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        # The first non-blank line is special: if it's a level-0 title we
        # consume it; otherwise we let it fall through to the content
        # filters below. Either way, this branch only runs once.
        if not title_line_consumed:
            title_line_consumed = True
            if stripped.startswith(_LEVEL_ZERO_PREFIX):
                continue

        # Skip section headings (any line starting with `=` that survived
        # the title check above).
        if stripped.startswith("="):
            continue
        if stripped in _BLOCK_DELIMITERS:
            continue
        # Attribute / block-selector lines like `[source,python]`,
        # `[NOTE]`, `[cols="1,2"]`, `[quote, …]`.
        if stripped.startswith("[") and stripped.endswith("]"):
            continue
        if stripped.startswith("image::"):
            continue

        content_lines.append(stripped)
        accumulated += len(stripped) + 1  # +1 for the joining space
        if accumulated >= max_chars:
            break

    snippet = " ".join(content_lines)
    if len(snippet) > max_chars:
        # Reserve one slot for the ellipsis character.
        snippet = snippet[: max_chars - 1].rstrip() + "\u2026"
    return snippet

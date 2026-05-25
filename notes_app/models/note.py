"""The :class:`Note` dataclass and the :class:`NoteSummary` value type.

Principles & invariants
-----------------------
* :class:`Note` is the in-memory shape of a row in the ``notes`` table —
  every field corresponds 1:1 to a column. The dataclass is frozen so
  callers cannot mutate state in place; updates flow through the
  repository, which produces a new instance.
* :class:`NoteSummary` is the pair of derived, cached presentation
  fields — ``title`` and ``snippet`` — that the repository stores in the
  ``notes.title`` / ``notes.snippet`` columns. It is a domain value
  alongside :class:`Note`, with no behaviour of its own. The single
  function that produces one from source lives in
  :mod:`notes_app.asciidoc.summary` (the parser is the source of truth
  for what is prose and what is structure); this module deliberately
  carries no derivation logic so there is exactly one classifier.
* Both dataclasses are frozen. ``NoteSummary`` is hashable and
  comparable by value, so tests can assert on a whole summary in one
  equality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Note:
    """A single note as it appears both in storage and in memory.

    Fields
    ------
    id:
        Stable identifier (a UUID-shaped string in production; opaque
        elsewhere).
    title:
        The note title derived from ``source`` at write time, or
        ``"Untitled"`` if absent. Stored verbatim in the ``notes.title``
        column so the note-list query never re-parses source. The
        repository fills it via
        :func:`notes_app.asciidoc.summary.derive_summary`.
    notebook_id:
        The owning notebook. Required — every note lives in a notebook.
    source:
        The full AsciiDoc source as the user typed it. The user's typed
        text is sacred: it is persisted even when it cannot be parsed, and
        the renderer is the only consumer that requires valid syntax.
    snippet:
        A short, plain-text preview produced by
        :func:`notes_app.asciidoc.summary.derive_summary` and cached.
        Bounded by :data:`notes_app.config.defaults.SNIPPET_MAX_CHARS`.
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


@dataclass(frozen=True)
class NoteSummary:
    """The derived ``(title, snippet)`` pair cached for a note.

    Produced by :func:`notes_app.asciidoc.summary.derive_summary` from a
    note's source and written into the ``notes.title`` / ``notes.snippet``
    columns by the repository. Keeping it a distinct value type (rather
    than a bare tuple) lets the derivation return both fields from a
    single parse and gives call sites a named, type-checked shape.
    """

    title: str
    snippet: str

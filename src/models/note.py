"""The :class:`Note` dataclass and the :class:`NoteSummary` value type.

Principles & invariants
-----------------------
* :class:`Note` is the in-memory shape of a row in the ``notes`` table —
  every field corresponds 1:1 to a column, with the sole exception of
  ``tags``: tags are stored in the ``note_tags`` junction table (one row
  per (note, tag) pair), not on the ``notes`` row itself, and the
  repository joins them in on read. The dataclass is frozen so callers
  cannot mutate state in place; updates flow through the repository,
  which produces a new instance.
* :class:`NoteSummary` is the trio of derived, cached presentation /
  classification fields — ``title``, ``snippet``, and ``tags`` — that
  the repository writes to the ``notes.title`` / ``notes.snippet``
  columns and the ``note_tags`` junction table. It is a domain value
  alongside :class:`Note`, with no behaviour of its own. The single
  function that produces one from source lives in
  :mod:`asciidoc.summary` (the parser is the source of truth for what
  is prose, what is structure, and what is a tag declaration); this
  module deliberately carries no derivation logic so there is exactly
  one classifier.
* Both dataclasses are frozen. ``NoteSummary`` is hashable and
  comparable by value, so tests can assert on a whole summary in one
  equality. ``tags`` on both is a ``tuple[str, ...]`` — sorted,
  lowercase, deduplicated — so equality and hashing are well-defined.
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
        :func:`asciidoc.summary.derive_summary`.
    source:
        The full AsciiDoc source as the user typed it. The user's typed
        text is sacred: it is persisted even when it cannot be parsed, and
        the renderer is the only consumer that requires valid syntax.
    snippet:
        A short, plain-text preview produced by
        :func:`asciidoc.summary.derive_summary` and cached.
        Bounded by :data:`config.defaults.SNIPPET_MAX_CHARS`.
    tags:
        Sorted, lowercase tuple of tags derived from the source's
        ``:tags:`` header attribute. Empty when the note has no tag
        line, when the line is empty / whitespace, or when the line
        fails to parse (the fallback in :mod:`asciidoc.summary`
        resolves to empty tags rather than raising). Stored in the
        ``note_tags`` junction table; the repository joins it in on
        read so the dataclass is always self-contained.
    created_at, modified_at:
        Timezone-aware UTC timestamps. The repository converts these to
        ISO-8601 strings on the way to SQLite.
    """

    id: str
    title: str
    source: str
    snippet: str
    tags: tuple[str, ...]
    created_at: datetime
    modified_at: datetime


@dataclass(frozen=True)
class NoteSummary:
    """The derived ``(title, snippet, tags)`` triple cached for a note.

    Produced by :func:`asciidoc.summary.derive_summary` from a
    note's source and written into the ``notes.title`` / ``notes.snippet``
    columns and the ``note_tags`` junction table by the repository.
    Keeping it a distinct value type (rather than three loose returns)
    lets the derivation return all three fields from a single parse and
    gives call sites a named, type-checked shape.
    """

    title: str
    snippet: str
    tags: tuple[str, ...]

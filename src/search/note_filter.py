"""Pure filter, sort, and smart-filter functions over note lists.

Principles & invariants
-----------------------
* This module is pure: every public function takes a ``list[Note]``
  (already materialised — the SQL repository did the per-notebook
  pre-filtering) and a small typed value, and returns a new ``list[Note]``.
  No I/O, no database access, no GTK, no global clock. Where the result
  depends on the wall clock (``RECENT``), the current time is injected
  through a keyword-only ``now`` parameter so tests are deterministic.
* The :data:`Selection` type is a discriminated union of two frozen
  dataclasses, :class:`SmartSelection` and :class:`NotebookSelection`.
  This shape makes illegal states unrepresentable at the type level —
  it is impossible to construct a "smart selection with a notebook id"
  or vice versa. Each variant carries a :attr:`kind` property that
  resolves to a :class:`SelectionKind` value, so non-pattern-matching
  consumers (e.g. a future UI label switch) can still discriminate
  without a ``match`` statement.
* :data:`RECENT_WINDOW_DAYS` defines what "recent" means for the
  ``RECENT`` smart filter. The value (7 days) matches the design
  reference in ``app.jsx``. The cutoff is computed as ``now -
  timedelta(days=RECENT_WINDOW_DAYS)`` and is *inclusive* — a note
  whose ``modified_at`` is exactly the cutoff still counts as recent
  (the design used a strict ``<`` against milliseconds; here we use
  ``>=`` against seconds-resolution timestamps, which is friendlier
  to clock skew and to the second-precision ISO-8601 timestamps the
  storage layer round-trips).
* :func:`filter_by_query` strips and case-folds the query before
  matching. An empty or whitespace-only query is a passthrough — the
  search box being empty must never hide notes. Substring matching
  spans ``title``, ``snippet``, and ``source``, mirroring the
  repository's SQL ``LIKE`` query so the in-memory and SQL-side paths
  agree on what "matches" means.
* :func:`sort_notes` always returns a fresh list — the input is never
  mutated. The order is descending by ``modified_at`` / ``created_at``
  (newest first, the convention from ``notelist.jsx``) and ascending
  by case-folded title for :data:`NoteSortKey.TITLE`. Python's sort
  is stable, so ties preserve the order of the input list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, assert_never

from enums import NoteSortKey, SelectionKind, SmartFilter
from models.note import Note


RECENT_WINDOW_DAYS: Final[int] = 7
"""Inclusive size of the ``RECENT`` smart-filter window, in days.

A note counts as recent when its ``modified_at`` is no earlier than
``now - timedelta(days=RECENT_WINDOW_DAYS)``. The 7-day choice mirrors
the ``app.jsx`` design reference. Changing this is a UX decision, not
a runtime tuning knob — the constant lives here rather than in
:mod:`config.defaults` so the search layer's behaviour is
self-contained.
"""


@dataclass(frozen=True, slots=True)
class SmartSelection:
    """Selection of one of the built-in smart filters (All / Recent).

    The :attr:`kind` property resolves to :data:`SelectionKind.SMART`
    so callers that want a uniform discriminator across selection
    variants can read it without a ``match`` statement.
    """

    smart_filter: SmartFilter

    @property
    def kind(self) -> SelectionKind:
        return SelectionKind.SMART


@dataclass(frozen=True, slots=True)
class NotebookSelection:
    """Selection of a specific notebook by id.

    Hierarchy expansion (e.g. selecting *Recipes* should also surface
    notes in its child notebooks *Baking* and *Weeknight dinners*) is
    **not** this layer's responsibility — the controller reads the
    notebook tree from the notebook repository and either calls
    :meth:`NoteRepositoryProtocol.list_by_notebook` once per id, or
    composes a list and feeds it through :func:`filter_by_selection`
    against this single id. Keeping that logic in the controller
    keeps the search layer free of any notebook-graph knowledge.
    """

    notebook_id: str

    @property
    def kind(self) -> SelectionKind:
        return SelectionKind.NOTEBOOK


type Selection = SmartSelection | NotebookSelection
"""Discriminated union of the two selection variants.

PEP 695 ``type`` alias so ``match`` statements over a :data:`Selection`
exhaustively cover both variants and the static checker can prove it.
"""


def filter_by_selection(
    notes: list[Note],
    selection: Selection,
    *,
    now: datetime,
) -> list[Note]:
    """Return the subset of ``notes`` that belongs to ``selection``.

    * :class:`SmartSelection` with :data:`SmartFilter.ALL` is a
      passthrough (a fresh list copy — never the original).
    * :class:`SmartSelection` with :data:`SmartFilter.RECENT` keeps
      notes whose ``modified_at`` is within the last
      :data:`RECENT_WINDOW_DAYS` of ``now`` (inclusive at the cutoff).
    * :class:`NotebookSelection` keeps notes whose ``notebook_id`` is
      exactly the selected id. No hierarchy expansion happens here —
      see the :class:`NotebookSelection` docstring.

    ``now`` is keyword-only and required in every call so that callers
    cannot accidentally pass a non-deterministic clock for what should
    be a pure, deterministic operation. Test code passes a fixed
    ``datetime``; production wires :func:`datetime.now` at the call
    site, where the dependency on the clock is explicit.
    """
    match selection:
        case SmartSelection(smart_filter=sf):
            # The inner ``match`` over ``SmartFilter`` lets mypy prove
            # exhaustiveness of the smart-filter branch separately
            # from the outer Selection union — without the split,
            # mypy can't tell that ``SmartSelection(SmartFilter.ALL)``
            # plus ``SmartSelection(SmartFilter.RECENT)`` covers the
            # whole inner enum.
            match sf:
                case SmartFilter.ALL:
                    return list(notes)
                case SmartFilter.RECENT:
                    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
                    return [
                        note for note in notes if note.modified_at >= cutoff
                    ]
                case _ as unhandled:
                    assert_never(unhandled)
        case NotebookSelection(notebook_id=nb_id):
            return [note for note in notes if note.notebook_id == nb_id]
        case _ as unhandled_selection:
            assert_never(unhandled_selection)


def filter_by_query(notes: list[Note], query: str) -> list[Note]:
    """Return the subset of ``notes`` that contain ``query`` somewhere.

    Matching is a case-folded substring check across each note's
    ``title``, ``snippet``, and ``source`` — the same three columns
    the SQL ``LIKE`` query in :class:`NoteRepository` searches, so the
    in-memory pipeline and the SQL-side path agree on what "matches".

    The query is stripped and case-folded once. A query that is empty
    after stripping is a passthrough (a fresh list copy) — an empty
    search box must never hide notes from the user.
    """
    needle = query.strip().casefold()
    if not needle:
        return list(notes)
    return [
        note
        for note in notes
        if needle in note.title.casefold()
        or needle in note.snippet.casefold()
        or needle in note.source.casefold()
    ]


def sort_notes(notes: list[Note], key: NoteSortKey) -> list[Note]:
    """Return ``notes`` re-ordered by the chosen :class:`NoteSortKey`.

    Direction conventions (matching ``notelist.jsx`` in the design):

    * :data:`NoteSortKey.MODIFIED` — descending ``modified_at``
      (newest first); the default of the note list dropdown.
    * :data:`NoteSortKey.CREATED` — descending ``created_at``.
    * :data:`NoteSortKey.TITLE` — ascending case-folded title; ties
      preserve insertion order because Python's sort is stable.

    The input list is never mutated. The result is always a freshly
    allocated list, even when the order would already be correct.
    """
    match key:
        case NoteSortKey.MODIFIED:
            return sorted(notes, key=_modified_at, reverse=True)
        case NoteSortKey.CREATED:
            return sorted(notes, key=_created_at, reverse=True)
        case NoteSortKey.TITLE:
            return sorted(notes, key=_title_casefold)
        case _ as unhandled:
            assert_never(unhandled)


def _modified_at(note: Note) -> datetime:
    return note.modified_at


def _created_at(note: Note) -> datetime:
    return note.created_at


def _title_casefold(note: Note) -> str:
    return note.title.casefold()

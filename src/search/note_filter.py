"""Pure filter, sort, and smart-filter functions over note lists.

Principles & invariants
-----------------------
* This module is pure: every public function takes a ``list[Note]``
  (already materialised — the SQL repository supplies the candidate
  set) and a small typed value, and returns a new ``list[Note]``.
  No I/O, no database access, no GTK, no global clock.
* The :data:`Selection` type is a discriminated union of two frozen
  dataclasses, :class:`SmartSelection` and :class:`TagSelection`.
  This shape makes illegal states unrepresentable at the type level —
  it is impossible to construct a "smart selection with a tag set"
  or vice versa. Each variant carries a :attr:`kind` property that
  resolves to a :class:`SelectionKind` value, so non-pattern-matching
  consumers (e.g. a future UI label switch) can still discriminate
  without a ``match`` statement.
* :class:`TagSelection` requires its ``tags`` frozenset to be
  non-empty — the empty case is :class:`SmartSelection(ALL)`, which
  means "no tag filter". The constructor raises :class:`ValueError`
  to enforce this; the controllers are written to never construct an
  empty :class:`TagSelection` (they fall back to ``SmartSelection(ALL)``
  when the last tag is toggled off).
* Multi-tag selection has **AND** semantics: a note is shown when
  every selected tag is on it. Adding a tag therefore narrows the
  visible set, never widens it.
* :func:`filter_by_query` strips and case-folds the query before
  matching. An empty or whitespace-only query is a passthrough — the
  search box being empty must never hide notes. Substring matching
  spans ``title``, ``snippet``, and ``source``, mirroring the
  repository's SQL ``LIKE`` query so the in-memory and SQL-side paths
  agree on what "matches" means.
* :func:`sort_notes` always returns a fresh list — the input is never
  mutated. The order is descending by ``modified_at`` / ``created_at``
  (newest first) and ascending by case-folded title for
  :data:`NoteSortKey.TITLE`. Python's sort is stable, so ties preserve
  the order of the input list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from enums import NoteSortKey, SelectionKind, SmartFilter
from models.note import Note


@dataclass(frozen=True, slots=True)
class SmartSelection:
    """Selection of one of the built-in smart filters (All / Untagged)."""

    smart_filter: SmartFilter

    @property
    def kind(self) -> SelectionKind:
        return SelectionKind.SMART


@dataclass(frozen=True, slots=True)
class TagSelection:
    """A non-empty set of tags filtered with AND semantics.

    The set is a ``frozenset[str]`` so equality and hashing of
    selections are well-defined (two ``TagSelection``\\s with the
    same tags compare equal regardless of insertion order).

    Construction raises :class:`ValueError` for an empty set: the
    UI guarantees this never happens because toggling the last
    selected tag off returns the app state to
    :class:`SmartSelection(ALL)`. Enforcing the invariant in the
    dataclass means no defensive ``if not tags`` branches in
    :func:`filter_by_selection`.
    """

    tags: frozenset[str]

    def __post_init__(self) -> None:
        if not self.tags:
            raise ValueError(
                "TagSelection.tags must be non-empty; use "
                "SmartSelection(SmartFilter.ALL) for the unfiltered case"
            )

    @property
    def kind(self) -> SelectionKind:
        return SelectionKind.TAG


type Selection = SmartSelection | TagSelection
"""Discriminated union of the two selection variants.

PEP 695 ``type`` alias so ``match`` statements over a :data:`Selection`
exhaustively cover both variants and the static checker can prove it.
"""


def filter_by_selection(
    notes: list[Note],
    selection: Selection,
) -> list[Note]:
    """Return the subset of ``notes`` that belongs to ``selection``.

    * :class:`SmartSelection` with :data:`SmartFilter.ALL` is a
      passthrough (a fresh list copy — never the original).
    * :class:`SmartSelection` with :data:`SmartFilter.UNTAGGED` keeps
      only notes whose ``tags`` tuple is empty.
    * :class:`TagSelection` keeps notes whose tag set is a superset of
      ``selection.tags`` (AND across the selected tags).

    No clock is needed because every smart filter is now time-free —
    the previous ``RECENT`` smart filter (and its ``RECENT_WINDOW_DAYS``
    constant) was dropped in favour of sort-by-date.
    """
    match selection:
        case SmartSelection(smart_filter=sf):
            match sf:
                case SmartFilter.ALL:
                    return list(notes)
                case SmartFilter.UNTAGGED:
                    return [note for note in notes if not note.tags]
                case _ as unhandled:
                    assert_never(unhandled)
        case TagSelection(tags=tags):
            return [
                note
                for note in notes
                if tags.issubset(set(note.tags))
            ]
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
    """Return ``notes`` re-ordered by the chosen :class:`NoteSortKey`."""
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

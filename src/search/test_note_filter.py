"""Tests for :mod:`search.note_filter`."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from enums import NoteSortKey, SelectionKind, SmartFilter
from models.note import Note
from search.note_filter import (
    SmartSelection,
    TagSelection,
    filter_by_query,
    filter_by_selection,
    sort_notes,
)


_FIXED_NOW: datetime = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_note(
    *,
    note_id: str,
    title: str = "Untitled",
    source: str = "",
    snippet: str = "",
    tags: tuple[str, ...] = (),
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> Note:
    """Construct a :class:`Note` with sensible defaults for tests."""
    when = created_at if created_at is not None else _FIXED_NOW
    mod = modified_at if modified_at is not None else when
    return Note(
        id=note_id,
        title=title,
        source=source,
        snippet=snippet,
        tags=tags,
        created_at=when,
        modified_at=mod,
    )


# ---------------------------------------------------------------------------
# Selection types — construction, kind discriminator
# ---------------------------------------------------------------------------


class SmartSelectionTests(unittest.TestCase):
    def test_kind_is_smart(self) -> None:
        sel = SmartSelection(smart_filter=SmartFilter.ALL)
        self.assertEqual(sel.kind, SelectionKind.SMART)

    def test_equality_by_value(self) -> None:
        self.assertEqual(
            SmartSelection(smart_filter=SmartFilter.ALL),
            SmartSelection(smart_filter=SmartFilter.ALL),
        )
        self.assertNotEqual(
            SmartSelection(smart_filter=SmartFilter.ALL),
            SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )


class TagSelectionTests(unittest.TestCase):
    def test_kind_is_tag(self) -> None:
        sel = TagSelection(tags=frozenset({"baking"}))
        self.assertEqual(sel.kind, SelectionKind.TAG)

    def test_empty_set_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TagSelection(tags=frozenset())

    def test_equality_independent_of_insertion_order(self) -> None:
        self.assertEqual(
            TagSelection(tags=frozenset({"a", "b"})),
            TagSelection(tags=frozenset({"b", "a"})),
        )


# ---------------------------------------------------------------------------
# filter_by_selection
# ---------------------------------------------------------------------------


class FilterBySmartAllTests(unittest.TestCase):
    def test_all_is_passthrough(self) -> None:
        notes = [_make_note(note_id="a"), _make_note(note_id="b")]
        result = filter_by_selection(
            notes, SmartSelection(smart_filter=SmartFilter.ALL),
        )
        self.assertEqual([n.id for n in result], ["a", "b"])

    def test_all_returns_a_fresh_list(self) -> None:
        notes = [_make_note(note_id="a")]
        result = filter_by_selection(
            notes, SmartSelection(smart_filter=SmartFilter.ALL),
        )
        self.assertIsNot(result, notes)


class FilterByUntaggedTests(unittest.TestCase):
    def test_keeps_only_notes_with_empty_tags(self) -> None:
        notes = [
            _make_note(note_id="t", tags=("foo",)),
            _make_note(note_id="u", tags=()),
        ]
        result = filter_by_selection(
            notes, SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )
        self.assertEqual([n.id for n in result], ["u"])

    def test_returns_empty_when_no_untagged_notes(self) -> None:
        notes = [_make_note(note_id="t", tags=("foo",))]
        result = filter_by_selection(
            notes, SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )
        self.assertEqual(result, [])


class FilterByTagSelectionTests(unittest.TestCase):
    def test_single_tag(self) -> None:
        notes = [
            _make_note(note_id="a", tags=("baking",)),
            _make_note(note_id="b", tags=("travel",)),
            _make_note(note_id="c", tags=("baking", "bread")),
        ]
        result = filter_by_selection(
            notes, TagSelection(tags=frozenset({"baking"})),
        )
        self.assertEqual({n.id for n in result}, {"a", "c"})

    def test_and_across_two_tags(self) -> None:
        notes = [
            _make_note(note_id="a", tags=("baking",)),
            _make_note(note_id="b", tags=("baking", "bread")),
            _make_note(note_id="c", tags=("bread",)),
            _make_note(note_id="d", tags=("baking", "bread", "sourdough")),
        ]
        result = filter_by_selection(
            notes, TagSelection(tags=frozenset({"baking", "bread"})),
        )
        # ``b`` and ``d`` both carry the entire selected set.
        self.assertEqual({n.id for n in result}, {"b", "d"})

    def test_and_across_three_tags(self) -> None:
        notes = [
            _make_note(note_id="a", tags=("baking", "bread")),
            _make_note(note_id="b", tags=("baking", "bread", "sourdough")),
        ]
        result = filter_by_selection(
            notes,
            TagSelection(tags=frozenset({"baking", "bread", "sourdough"})),
        )
        self.assertEqual([n.id for n in result], ["b"])

    def test_no_match_yields_empty_list(self) -> None:
        notes = [_make_note(note_id="a", tags=("travel",))]
        result = filter_by_selection(
            notes, TagSelection(tags=frozenset({"baking"})),
        )
        self.assertEqual(result, [])

    def test_untagged_note_never_matches_tag_selection(self) -> None:
        notes = [_make_note(note_id="u", tags=())]
        result = filter_by_selection(
            notes, TagSelection(tags=frozenset({"baking"})),
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# filter_by_query
# ---------------------------------------------------------------------------


class FilterByQueryTests(unittest.TestCase):
    def test_empty_query_passthrough(self) -> None:
        notes = [_make_note(note_id="a"), _make_note(note_id="b")]
        result = filter_by_query(notes, "")
        self.assertEqual([n.id for n in result], ["a", "b"])

    def test_whitespace_only_query_passthrough(self) -> None:
        notes = [_make_note(note_id="a")]
        result = filter_by_query(notes, "    ")
        self.assertEqual(len(result), 1)

    def test_matches_title(self) -> None:
        notes = [
            _make_note(note_id="a", title="Sourdough boule"),
            _make_note(note_id="b", title="Sandwich loaf"),
        ]
        result = filter_by_query(notes, "boule")
        self.assertEqual([n.id for n in result], ["a"])

    def test_matches_snippet(self) -> None:
        notes = [_make_note(note_id="a", snippet="Tasty crumb texture.")]
        result = filter_by_query(notes, "crumb")
        self.assertEqual([n.id for n in result], ["a"])

    def test_matches_source(self) -> None:
        notes = [_make_note(note_id="a", source="= T\n\nstart\n\n----\nzed\n----\n")]
        result = filter_by_query(notes, "zed")
        self.assertEqual([n.id for n in result], ["a"])

    def test_case_insensitive(self) -> None:
        notes = [_make_note(note_id="a", title="MIXED case")]
        result = filter_by_query(notes, "mixed")
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# sort_notes
# ---------------------------------------------------------------------------


class SortNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mid = datetime(2026, 1, 5, tzinfo=timezone.utc)
        new = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.notes = [
            _make_note(
                note_id="old", title="zeta",
                created_at=old, modified_at=old,
            ),
            _make_note(
                note_id="new", title="alpha",
                created_at=new, modified_at=new,
            ),
            _make_note(
                note_id="mid", title="mu",
                created_at=mid, modified_at=mid,
            ),
        ]

    def test_modified_descending(self) -> None:
        result = sort_notes(self.notes, NoteSortKey.MODIFIED)
        self.assertEqual([n.id for n in result], ["new", "mid", "old"])

    def test_created_descending(self) -> None:
        result = sort_notes(self.notes, NoteSortKey.CREATED)
        self.assertEqual([n.id for n in result], ["new", "mid", "old"])

    def test_title_ascending_case_folded(self) -> None:
        result = sort_notes(self.notes, NoteSortKey.TITLE)
        self.assertEqual([n.title for n in result], ["alpha", "mu", "zeta"])

    def test_input_not_mutated(self) -> None:
        snapshot = list(self.notes)
        sort_notes(self.notes, NoteSortKey.MODIFIED)
        self.assertEqual(self.notes, snapshot)


if __name__ == "__main__":
    unittest.main()

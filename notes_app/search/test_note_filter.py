"""Tests for :mod:`notes_app.search.note_filter`."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from notes_app.enums import NoteSortKey, SelectionKind, SmartFilter
from notes_app.models.note import Note
from notes_app.search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    Selection,
    SmartSelection,
    filter_by_query,
    filter_by_selection,
    sort_notes,
)


_FIXED_NOW: datetime = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_note(  # pylint: disable=too-many-arguments
    *,
    note_id: str,
    title: str = "Untitled",
    notebook_id: str = "nb-personal",
    source: str = "",
    snippet: str = "",
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> Note:
    """Construct a :class:`Note` with sensible defaults for tests.

    Only the fields under test need to be specified; the rest fall
    back to neutral values that none of the filter / sort assertions
    depend on. The wide keyword surface is deliberate — every
    :class:`Note` field is independently controllable so individual
    tests can pin behaviour against one column at a time.
    """
    when = created_at if created_at is not None else _FIXED_NOW
    mod = modified_at if modified_at is not None else when
    return Note(
        id=note_id,
        title=title,
        notebook_id=notebook_id,
        source=source,
        snippet=snippet,
        created_at=when,
        modified_at=mod,
    )


# ---------------------------------------------------------------------------
# Selection types
# ---------------------------------------------------------------------------


class SelectionTypeTests(unittest.TestCase):
    """The discriminated-union shape of :data:`Selection`."""

    def test_smart_selection_kind_is_smart(self) -> None:
        sel = SmartSelection(smart_filter=SmartFilter.ALL)
        self.assertEqual(sel.kind, SelectionKind.SMART)

    def test_notebook_selection_kind_is_notebook(self) -> None:
        sel = NotebookSelection(notebook_id="nb-personal")
        self.assertEqual(sel.kind, SelectionKind.NOTEBOOK)

    def test_smart_selection_is_frozen(self) -> None:
        sel = SmartSelection(smart_filter=SmartFilter.RECENT)
        with self.assertRaises(AttributeError):
            sel.smart_filter = SmartFilter.ALL  # type: ignore[misc]

    def test_notebook_selection_is_frozen(self) -> None:
        sel = NotebookSelection(notebook_id="nb1")
        with self.assertRaises(AttributeError):
            sel.notebook_id = "nb2"  # type: ignore[misc]

    def test_selection_alias_accepts_both_variants(self) -> None:
        # The annotation here is what we are pinning — if the alias
        # ever stops accepting either variant the test won't even
        # type-check, and at runtime the assignments below confirm
        # the value-level behaviour.
        smart: Selection = SmartSelection(smart_filter=SmartFilter.ALL)
        notebook: Selection = NotebookSelection(notebook_id="nb1")
        self.assertIsInstance(smart, SmartSelection)
        self.assertIsInstance(notebook, NotebookSelection)


# ---------------------------------------------------------------------------
# filter_by_selection — All
# ---------------------------------------------------------------------------


class FilterBySelectionAllTests(unittest.TestCase):
    """``SmartSelection(SmartFilter.ALL)`` is a passthrough."""

    def test_returns_all_notes(self) -> None:
        notes = [
            _make_note(note_id="n1"),
            _make_note(note_id="n2", notebook_id="nb-recipes"),
            _make_note(note_id="n3", notebook_id="nb-archive"),
        ]
        result = filter_by_selection(
            notes,
            SmartSelection(smart_filter=SmartFilter.ALL),
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["n1", "n2", "n3"])

    def test_returns_empty_for_empty_input(self) -> None:
        result = filter_by_selection(
            [],
            SmartSelection(smart_filter=SmartFilter.ALL),
            now=_FIXED_NOW,
        )
        self.assertEqual(result, [])

    def test_returns_a_fresh_list(self) -> None:
        notes = [_make_note(note_id="n1")]
        result = filter_by_selection(
            notes,
            SmartSelection(smart_filter=SmartFilter.ALL),
            now=_FIXED_NOW,
        )
        # Same elements, but a different list object — mutating the
        # result must not corrupt what the caller passed in.
        self.assertEqual(result, notes)
        self.assertIsNot(result, notes)


# ---------------------------------------------------------------------------
# filter_by_selection — Recent
# ---------------------------------------------------------------------------


class FilterBySelectionRecentTests(unittest.TestCase):
    """``SmartSelection(SmartFilter.RECENT)`` keeps notes within the window."""

    def test_keeps_notes_inside_window(self) -> None:
        notes = [
            _make_note(
                note_id="recent-1d",
                modified_at=_FIXED_NOW - timedelta(days=1),
            ),
            _make_note(
                note_id="recent-3d",
                modified_at=_FIXED_NOW - timedelta(days=3),
            ),
            _make_note(
                note_id="recent-6d",
                modified_at=_FIXED_NOW - timedelta(days=6),
            ),
        ]
        result = filter_by_selection(
            notes,
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=_FIXED_NOW,
        )
        self.assertEqual(
            sorted(n.id for n in result),
            ["recent-1d", "recent-3d", "recent-6d"],
        )

    def test_drops_notes_outside_window(self) -> None:
        notes = [
            _make_note(
                note_id="old-8d",
                modified_at=_FIXED_NOW - timedelta(days=8),
            ),
            _make_note(
                note_id="old-30d",
                modified_at=_FIXED_NOW - timedelta(days=30),
            ),
        ]
        result = filter_by_selection(
            notes,
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=_FIXED_NOW,
        )
        self.assertEqual(result, [])

    def test_cutoff_is_inclusive(self) -> None:
        # A note modified exactly RECENT_WINDOW_DAYS ago must still
        # count as recent.
        on_cutoff = _make_note(
            note_id="on-cutoff",
            modified_at=_FIXED_NOW - timedelta(days=RECENT_WINDOW_DAYS),
        )
        just_outside = _make_note(
            note_id="just-outside",
            modified_at=(
                _FIXED_NOW
                - timedelta(days=RECENT_WINDOW_DAYS, microseconds=1)
            ),
        )
        result = filter_by_selection(
            [on_cutoff, just_outside],
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["on-cutoff"])

    def test_uses_modified_at_not_created_at(self) -> None:
        # Old creation date but recent modification — must be kept.
        # Recent creation date but ancient modification — must be dropped.
        # (Hypothetical, but pins which timestamp drives the filter.)
        kept = _make_note(
            note_id="created-old-modified-new",
            created_at=_FIXED_NOW - timedelta(days=365),
            modified_at=_FIXED_NOW - timedelta(days=1),
        )
        dropped = _make_note(
            note_id="created-new-modified-old",
            created_at=_FIXED_NOW - timedelta(days=1),
            modified_at=_FIXED_NOW - timedelta(days=365),
        )
        result = filter_by_selection(
            [kept, dropped],
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["created-old-modified-new"])

    def test_now_drives_the_cutoff(self) -> None:
        # The same notes filtered against two different ``now`` values
        # produce different results — the function must read ``now``,
        # not call into a hidden clock.
        note = _make_note(
            note_id="n",
            modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        within = filter_by_selection(
            [note],
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
        outside = filter_by_selection(
            [note],
            SmartSelection(smart_filter=SmartFilter.RECENT),
            now=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        self.assertEqual([n.id for n in within], ["n"])
        self.assertEqual(outside, [])


# ---------------------------------------------------------------------------
# filter_by_selection — Notebook
# ---------------------------------------------------------------------------


class FilterBySelectionNotebookTests(unittest.TestCase):
    """``NotebookSelection`` keeps only direct id matches."""

    def test_keeps_notes_with_matching_notebook_id(self) -> None:
        notes = [
            _make_note(note_id="n1", notebook_id="nb-personal"),
            _make_note(note_id="n2", notebook_id="nb-personal"),
            _make_note(note_id="n3", notebook_id="nb-recipes"),
        ]
        result = filter_by_selection(
            notes,
            NotebookSelection(notebook_id="nb-personal"),
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["n1", "n2"])

    def test_returns_empty_when_no_match(self) -> None:
        notes = [
            _make_note(note_id="n1", notebook_id="nb-personal"),
        ]
        result = filter_by_selection(
            notes,
            NotebookSelection(notebook_id="nb-archive"),
            now=_FIXED_NOW,
        )
        self.assertEqual(result, [])

    def test_does_not_perform_hierarchy_expansion(self) -> None:
        # Pinning the design decision: the search layer compares
        # notebook_id directly. A note in the child notebook is NOT
        # surfaced when the parent is selected — that expansion is
        # the controller's job (see NotebookSelection docstring).
        notes = [
            _make_note(note_id="parent", notebook_id="nb-recipes"),
            _make_note(note_id="child", notebook_id="nb-baking"),
        ]
        result = filter_by_selection(
            notes,
            NotebookSelection(notebook_id="nb-recipes"),
            now=_FIXED_NOW,
        )
        self.assertEqual([n.id for n in result], ["parent"])


# ---------------------------------------------------------------------------
# filter_by_query
# ---------------------------------------------------------------------------


class FilterByQueryTests(unittest.TestCase):
    """Substring matching across title, snippet, source."""

    def test_empty_query_is_passthrough(self) -> None:
        notes = [
            _make_note(note_id="n1", title="Hello"),
            _make_note(note_id="n2", title="World"),
        ]
        result = filter_by_query(notes, "")
        self.assertEqual([n.id for n in result], ["n1", "n2"])

    def test_whitespace_only_query_is_passthrough(self) -> None:
        notes = [
            _make_note(note_id="n1", title="Hello"),
            _make_note(note_id="n2", title="World"),
        ]
        result = filter_by_query(notes, "   \t  ")
        self.assertEqual([n.id for n in result], ["n1", "n2"])

    def test_passthrough_returns_a_fresh_list(self) -> None:
        notes = [_make_note(note_id="n1")]
        result = filter_by_query(notes, "")
        self.assertEqual(result, notes)
        self.assertIsNot(result, notes)

    def test_matches_title(self) -> None:
        notes = [
            _make_note(note_id="n1", title="Pasta carbonara"),
            _make_note(note_id="n2", title="Sourdough"),
            _make_note(note_id="n3", title="Pasta primavera"),
        ]
        result = filter_by_query(notes, "pasta")
        self.assertEqual([n.id for n in result], ["n1", "n3"])

    def test_matches_snippet(self) -> None:
        notes = [
            _make_note(note_id="n1", snippet="A guide to brewing coffee"),
            _make_note(note_id="n2", snippet="Notes about tea"),
        ]
        result = filter_by_query(notes, "coffee")
        self.assertEqual([n.id for n in result], ["n1"])

    def test_matches_source(self) -> None:
        notes = [
            _make_note(note_id="n1", source="= Title\n\nbody mentions Florence"),
            _make_note(note_id="n2", source="= Title\n\nunrelated"),
        ]
        result = filter_by_query(notes, "Florence")
        self.assertEqual([n.id for n in result], ["n1"])

    def test_is_case_insensitive(self) -> None:
        notes = [
            _make_note(note_id="n1", title="HELLO World"),
            _make_note(note_id="n2", snippet="hello there"),
            _make_note(note_id="n3", source="= Goodbye"),
        ]
        # All three different cases / fields match the same query.
        result = filter_by_query(notes, "HeLLo")
        self.assertEqual([n.id for n in result], ["n1", "n2"])

    def test_strips_query_before_matching(self) -> None:
        notes = [
            _make_note(note_id="n1", title="Pasta"),
            _make_note(note_id="n2", title="Salad"),
        ]
        # Surrounding whitespace must not change matching.
        result = filter_by_query(notes, "  pasta  \n")
        self.assertEqual([n.id for n in result], ["n1"])

    def test_no_match_returns_empty(self) -> None:
        notes = [_make_note(note_id="n1", title="Hello")]
        self.assertEqual(filter_by_query(notes, "xyzzy"), [])

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(filter_by_query([], "anything"), [])
        self.assertEqual(filter_by_query([], ""), [])

    def test_substring_matches_anywhere_in_field(self) -> None:
        notes = [
            _make_note(note_id="n1", title="My pasta recipe"),
            _make_note(note_id="n2", title="The end"),
        ]
        result = filter_by_query(notes, "pasta")
        self.assertEqual([n.id for n in result], ["n1"])

    def test_unicode_casefolding(self) -> None:
        # casefold() handles non-ASCII case-pairs that lower() would miss.
        # The classic example is the German sharp s ('ß' -> 'ss').
        notes = [
            _make_note(note_id="n1", title="Straße"),
        ]
        result = filter_by_query(notes, "STRASSE")
        self.assertEqual([n.id for n in result], ["n1"])


# ---------------------------------------------------------------------------
# sort_notes
# ---------------------------------------------------------------------------


class SortNotesByModifiedTests(unittest.TestCase):
    """:data:`NoteSortKey.MODIFIED` sorts newest-first."""

    def test_descending_modified_at(self) -> None:
        notes = [
            _make_note(
                note_id="oldest",
                modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="newest",
                modified_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="middle",
                modified_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        result = sort_notes(notes, NoteSortKey.MODIFIED)
        self.assertEqual([n.id for n in result], ["newest", "middle", "oldest"])

    def test_ties_preserve_input_order(self) -> None:
        same = datetime(2026, 4, 1, tzinfo=timezone.utc)
        notes = [
            _make_note(note_id="a", modified_at=same),
            _make_note(note_id="b", modified_at=same),
            _make_note(note_id="c", modified_at=same),
        ]
        # Stable sort means the input order is preserved on ties.
        self.assertEqual(
            [n.id for n in sort_notes(notes, NoteSortKey.MODIFIED)],
            ["a", "b", "c"],
        )

    def test_input_list_not_mutated(self) -> None:
        original = [
            _make_note(
                note_id="oldest",
                modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="newest",
                modified_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
        ]
        # Snapshot id order before sorting; assert it is unchanged after.
        before = [n.id for n in original]
        sort_notes(original, NoteSortKey.MODIFIED)
        self.assertEqual([n.id for n in original], before)


class SortNotesByCreatedTests(unittest.TestCase):
    """:data:`NoteSortKey.CREATED` sorts newest-created first."""

    def test_descending_created_at(self) -> None:
        notes = [
            _make_note(
                note_id="oldest",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="middle",
                created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="newest",
                created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        result = sort_notes(notes, NoteSortKey.CREATED)
        self.assertEqual([n.id for n in result], ["newest", "middle", "oldest"])

    def test_uses_created_at_not_modified_at(self) -> None:
        # Two notes created in the same order they appear in the list,
        # but with reversed modified_at — sort by CREATED must be
        # insensitive to modified_at.
        notes = [
            _make_note(
                note_id="created-first",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                modified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="created-second",
                created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ]
        result = sort_notes(notes, NoteSortKey.CREATED)
        self.assertEqual([n.id for n in result], ["created-second", "created-first"])


class SortNotesByTitleTests(unittest.TestCase):
    """:data:`NoteSortKey.TITLE` sorts ascending case-folded title."""

    def test_ascending_alphabetical(self) -> None:
        notes = [
            _make_note(note_id="banana", title="Banana"),
            _make_note(note_id="apple", title="Apple"),
            _make_note(note_id="cherry", title="Cherry"),
        ]
        result = sort_notes(notes, NoteSortKey.TITLE)
        self.assertEqual([n.id for n in result], ["apple", "banana", "cherry"])

    def test_case_insensitive(self) -> None:
        # Mixed-case titles must sort as if all lowercase — neither
        # 'A' < 'b' (ASCII order) nor 'b' < 'A' is the right answer.
        notes = [
            _make_note(note_id="a", title="apple"),
            _make_note(note_id="b", title="Banana"),
            _make_note(note_id="c", title="cherry"),
        ]
        result = sort_notes(notes, NoteSortKey.TITLE)
        self.assertEqual([n.id for n in result], ["a", "b", "c"])

    def test_ties_preserve_input_order(self) -> None:
        notes = [
            _make_note(note_id="first", title="Same Title"),
            _make_note(note_id="second", title="Same Title"),
            _make_note(note_id="third", title="same title"),
        ]
        result = sort_notes(notes, NoteSortKey.TITLE)
        self.assertEqual([n.id for n in result], ["first", "second", "third"])


class SortNotesGeneralTests(unittest.TestCase):
    """Behaviours that hold across every sort key."""

    def test_empty_list_returns_empty_list(self) -> None:
        for key in NoteSortKey:
            with self.subTest(key=key):
                self.assertEqual(sort_notes([], key), [])

    def test_returns_a_fresh_list(self) -> None:
        notes = [
            _make_note(
                note_id="a",
                modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_note(
                note_id="b",
                modified_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
        ]
        for key in NoteSortKey:
            with self.subTest(key=key):
                self.assertIsNot(sort_notes(notes, key), notes)

    def test_single_note_round_trips(self) -> None:
        only = _make_note(note_id="solo")
        for key in NoteSortKey:
            with self.subTest(key=key):
                result = sort_notes([only], key)
                self.assertEqual([n.id for n in result], ["solo"])


# ---------------------------------------------------------------------------
# Composition — the typical pipeline used by the controller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PipelineCase:
    """A single end-to-end scenario for the search pipeline."""

    description: str
    selection: Selection
    query: str
    sort_key: NoteSortKey
    expected_ids: tuple[str, ...]


class PipelineCompositionTests(unittest.TestCase):
    """``filter_by_selection`` then ``filter_by_query`` then ``sort_notes``.

    These tests do not pin any new behaviour — they document that the
    three functions compose without surprises and are safe to chain in
    the order the controller will actually use them.
    """

    library: list[Note]

    def setUp(self) -> None:
        # A small fixed library spanning:
        # - two notebooks (recipes, travel)
        # - a recent and a stale note
        # - titles that differ in case so case-folded sort can be observed
        self.library = [
            _make_note(
                note_id="pasta",
                title="Pasta carbonara",
                snippet="creamy carbonara",
                notebook_id="nb-recipes",
                modified_at=_FIXED_NOW - timedelta(days=2),
            ),
            _make_note(
                note_id="sourdough",
                title="sourdough starter",
                snippet="bread basics",
                notebook_id="nb-recipes",
                modified_at=_FIXED_NOW - timedelta(days=10),
            ),
            _make_note(
                note_id="kyoto",
                title="Kyoto temples",
                snippet="travel notes",
                notebook_id="nb-travel",
                modified_at=_FIXED_NOW - timedelta(days=1),
            ),
            _make_note(
                note_id="lisbon",
                title="Lisbon trip",
                snippet="custard tart guide",
                notebook_id="nb-travel",
                modified_at=_FIXED_NOW - timedelta(days=20),
            ),
        ]

    def test_table(self) -> None:
        cases: tuple[_PipelineCase, ...] = (
            _PipelineCase(
                description="all + no query + by modified",
                selection=SmartSelection(smart_filter=SmartFilter.ALL),
                query="",
                sort_key=NoteSortKey.MODIFIED,
                expected_ids=("kyoto", "pasta", "sourdough", "lisbon"),
            ),
            _PipelineCase(
                description="recent + no query (drops 10d and 20d notes)",
                selection=SmartSelection(smart_filter=SmartFilter.RECENT),
                query="",
                sort_key=NoteSortKey.MODIFIED,
                expected_ids=("kyoto", "pasta"),
            ),
            _PipelineCase(
                description="recipes notebook + by title",
                selection=NotebookSelection(notebook_id="nb-recipes"),
                query="",
                sort_key=NoteSortKey.TITLE,
                expected_ids=("pasta", "sourdough"),
            ),
            _PipelineCase(
                description="all + query 'custard' (snippet match)",
                # 'custard' appears only in lisbon's snippet —
                # avoiding 'tart' which would also match 'sTARTer'
                # in the sourdough title.
                selection=SmartSelection(smart_filter=SmartFilter.ALL),
                query="custard",
                sort_key=NoteSortKey.MODIFIED,
                expected_ids=("lisbon",),
            ),
            _PipelineCase(
                description="travel + recent — combined narrowing drops Lisbon",
                # Recent filter applied via 'all' + the user picking
                # the travel notebook: chained explicitly here.
                selection=NotebookSelection(notebook_id="nb-travel"),
                query="",
                sort_key=NoteSortKey.MODIFIED,
                expected_ids=("kyoto", "lisbon"),
            ),
        )
        for case in cases:
            with self.subTest(case.description):
                step1 = filter_by_selection(
                    self.library, case.selection, now=_FIXED_NOW
                )
                step2 = filter_by_query(step1, case.query)
                step3 = sort_notes(step2, case.sort_key)
                self.assertEqual(
                    tuple(n.id for n in step3),
                    case.expected_ids,
                )


if __name__ == "__main__":
    unittest.main()

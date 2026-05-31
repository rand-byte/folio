"""Tests for :mod:`ui.sidebar`.

Two layers are covered here:

* The pure, display-free helpers — :func:`count_untagged` and
  :func:`tags_header_accent_text`. The latter locks the
  ``"(N selected)"`` wording that regressed to invisible, independently
  of GTK.
* The widget-level selection rendering — that a selected tag row shows
  the leading ✓ (opacity 1.0) while an unselected one reserves the
  column (opacity 0.0), that the check image carries the icon name and
  CSS class the stylesheet keys off, that the Tags list carries the
  scoping class, and that the header accent label tracks the selection.
  These build a real :class:`Sidebar` and so are gated behind a GDK
  display (they *skip* without one — see ``README.md`` §5).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from controllers.app_state import AppState
from enums import SmartFilter
from models.note import Note
from ui.sidebar import (
    Sidebar,
    _TAG_CHECK_ICON_NAME,
    _TAG_LIST_CSS_CLASS,
    _TAG_ROW_CHECK_CSS_CLASS,
    count_untagged,
    tags_header_accent_text,
)


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


class CountUntaggedTests(unittest.TestCase):
    """:func:`count_untagged` is a pure helper — verifiable
    without GTK."""

    def test_empty_list(self) -> None:
        self.assertEqual(count_untagged([]), 0)

    def test_counts_notes_with_empty_tags_tuple(self) -> None:
        class _Stub:  # pylint: disable=too-few-public-methods
            def __init__(self, tags: tuple[str, ...]) -> None:
                self.tags = tags

        notes = [_Stub(()), _Stub(("a",)), _Stub(()), _Stub(("b", "c"))]
        self.assertEqual(count_untagged(notes), 2)

    def test_tags_attribute_optional(self) -> None:
        # Anything without a ``tags`` attribute is treated as
        # untagged — matches the SmartFilter.UNTAGGED predicate.
        notes = [object(), object()]
        self.assertEqual(count_untagged(notes), 2)


class TagsHeaderAccentTextTests(unittest.TestCase):
    """:func:`tags_header_accent_text` is a pure helper — it is the
    single source of the Tags-header parenthetical wording, so this
    locks the string that regressed to invisible.
    """

    def test_zero_selected_is_empty(self) -> None:
        self.assertEqual(tags_header_accent_text(0), "")

    def test_negative_is_empty(self) -> None:
        # Defensive: a non-positive count never renders an accent.
        self.assertEqual(tags_header_accent_text(-1), "")

    def test_one_selected(self) -> None:
        self.assertEqual(tags_header_accent_text(1), "(1 selected)")

    def test_two_selected(self) -> None:
        self.assertEqual(tags_header_accent_text(2), "(2 selected)")


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` for the sidebar.

    The sidebar only reads :meth:`list_all` (for the Library counts) and
    :meth:`list_tags` (for the Tags rows) during ``refresh``; every other
    method raising :class:`NotImplementedError` makes any inadvertent
    call a loud test bug rather than a silent pass.
    """

    notes: list[Note]
    tag_pairs: tuple[tuple[str, int], ...]

    def __init__(
        self,
        *,
        notes: list[Note],
        tag_pairs: tuple[tuple[str, int], ...],
    ) -> None:
        self.notes = notes
        self.tag_pairs = tag_pairs

    def list_all(self) -> list[Note]:
        return list(self.notes)

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return self.tag_pairs

    def get(self, note_id: str) -> Note:
        raise NotImplementedError

    def list_modified_since(self, since: datetime) -> list[Note]:
        raise NotImplementedError

    def search(self, query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, note: Note) -> None:
        raise NotImplementedError

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> None:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError


def _note_with_tags(note_id: str, tags: tuple[str, ...]) -> Note:
    return Note(
        id=note_id,
        title=f"note {note_id}",
        source="",
        snippet="",
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


def _pump(iterations: int = 200) -> None:
    """Drive the default main context so the ListView realises its
    rows. Non-blocking iterations keep the pump bounded and crash-proof
    under the cairo software renderer."""
    context = GLib.MainContext.default()
    for _ in range(iterations):
        context.iteration(False)


def _row_box(row: Gtk.Widget) -> Gtk.Box | None:
    """Descend from a realised ListView row to the factory's row box."""
    node: Gtk.Widget | None = row
    while node is not None and not isinstance(node, Gtk.Box):
        node = node.get_first_child()
    return node


def _tag_rows(list_view: Gtk.ListView) -> dict[str, Gtk.Box]:
    """Map each visible tag label (``#name``) to its row box."""
    rows: dict[str, Gtk.Box] = {}
    child = list_view.get_first_child()
    while child is not None:
        box = _row_box(child)
        if box is not None:
            image = box.get_first_child()
            label = image.get_next_sibling() if image is not None else None
            if isinstance(label, Gtk.Label):
                rows[label.get_text()] = box
        child = child.get_next_sibling()
    return rows


@unittest.skipUnless(_display_available(), "no GDK display")
class SidebarSelectionRenderingTests(unittest.TestCase):
    """Selection must read as the leading ✓, and the header accent must
    track the tag selection. Builds a real :class:`Sidebar` realised in
    a window so the factory's rows exist."""

    app_state: AppState
    sidebar: Sidebar
    window: Gtk.Window

    def setUp(self) -> None:
        repository = _FakeNoteRepository(
            notes=[
                _note_with_tags("1", ("baking", "bread")),
                _note_with_tags("2", ("bread",)),
            ],
            tag_pairs=(("baking", 1), ("bread", 2)),
        )
        self.app_state = AppState()
        self.sidebar = Sidebar(
            note_repository=repository,
            app_state=self.app_state,
        )
        self.window = Gtk.Window()
        self.window.set_child(self.sidebar)
        self.window.present()
        _pump()

    def tearDown(self) -> None:
        self.window.set_child(None)
        self.window.destroy()
        _pump(20)

    def test_tag_list_carries_scoping_class(self) -> None:
        # Guards the softened-selection rule's scope: without the class
        # the rule cannot target the Tags list alone.
        self.assertTrue(
            self.sidebar.tag_list_view.has_css_class(_TAG_LIST_CSS_CLASS),
        )

    def test_check_icon_name_and_class(self) -> None:
        # Guards the broken-icon regression (must be the GTK-bundled
        # name) and the dead-CSS regression (class must reach the icon).
        rows = _tag_rows(self.sidebar.tag_list_view)
        self.assertIn("#baking", rows)
        image = rows["#baking"].get_first_child()
        assert isinstance(image, Gtk.Image)
        self.assertEqual(image.get_icon_name(), _TAG_CHECK_ICON_NAME)
        self.assertTrue(image.has_css_class(_TAG_ROW_CHECK_CSS_CLASS))

    def test_selected_row_shows_check_unselected_reserves_column(self) -> None:
        self.app_state.toggle_tag("baking")
        _pump()
        rows = _tag_rows(self.sidebar.tag_list_view)
        selected = rows["#baking"].get_first_child()
        unselected = rows["#bread"].get_first_child()
        assert isinstance(selected, Gtk.Image)
        assert isinstance(unselected, Gtk.Image)
        # The ✓ is shown on the selected row and merely transparent (not
        # gone) on the unselected one, so both rows keep the same column.
        self.assertEqual(selected.get_opacity(), 1.0)
        self.assertEqual(unselected.get_opacity(), 0.0)

    def test_header_accent_visible_when_selected_hidden_when_not(self) -> None:
        accent = self.sidebar.tags_header_box.get_last_child()
        assert isinstance(accent, Gtk.Label)
        # Nothing selected initially: accent hidden.
        self.assertFalse(accent.get_visible())

        self.app_state.toggle_tag("bread")
        _pump()
        self.assertTrue(accent.get_visible())
        self.assertEqual(accent.get_text(), "(1 selected)")

        # Returning to a smart selection clears the tag set and hides it.
        self.app_state.set_smart(SmartFilter.ALL)
        _pump()
        self.assertFalse(accent.get_visible())
        self.assertEqual(accent.get_text(), "")


if __name__ == "__main__":
    unittest.main()

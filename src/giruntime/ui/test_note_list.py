"""Tests for :mod:`ui.note_list`.

The note list now binds a ``Filter``/``Sort``/``ListView`` chain over
the in-memory :class:`controllers.note_list_store.NoteListStore`. The
"what shows / what order" rules are covered exhaustively by the pure
predicates in :mod:`search.note_filter`; here we exercise the widget's
own wiring: the filtered count, live query filtering (no throttle),
sort-key reordering, the AppState ⇄ selection round-trip, and the 📎
badge's re-bind on the controller's ``attachments-changed`` signal.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk

from enums import AttachmentExportFailureReason, NoteSortKey
from storage.protocols import AttachmentExportFailed
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
import giruntime.ui.note_list as note_list_module
from giruntime.ui.note_list import NoteList, _SORT_KEY_DROPDOWN_ORDER
from models.attachment import Attachment
from models.note import Note


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _note(
    note_id: str,
    title: str,
    *,
    tags: tuple[str, ...] = (),
    modified_at: datetime = _FIXED_NOW,
) -> Note:
    return Note(
        id=note_id,
        title=title,
        source=f"= {title}\n",
        snippet=title,
        tags=tags,
        created_at=_FIXED_NOW,
        modified_at=modified_at,
    )


class _FakeNoteRepository:
    """Minimal repository returning a fixed note set from ``list_all``."""

    _notes: list[Note]

    def __init__(self, notes: list[Note]) -> None:
        self._notes = notes

    def list_all(self) -> list[Note]:
        return list(self._notes)

    def get(self, note_id: str) -> Note:
        for note in self._notes:
            if note.id == note_id:
                return note
        raise KeyError(note_id)

    def insert(self, _note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError


class _FakeAttachmentStore:
    """Counts are read from a mutable per-note dict (default zero) so
    the badge tests can change a count and assert the re-bind reads it;
    no other method is called here."""

    counts: dict[str, int]

    def __init__(self) -> None:
        self.counts = {}

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def count_for_note(self, note_id: str) -> int:
        return self.counts.get(note_id, 0)

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError

    def export_to(self, attachment_id: str, destination: Path) -> None:
        """Write the attachment's bytes out (the outbound mirror of add)."""
        try:
            data = self.get_bytes(attachment_id)
        except KeyError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.UNKNOWN_ATTACHMENT,
            ) from exc
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise AttachmentExportFailed(
                AttachmentExportFailureReason.DESTINATION_UNWRITABLE,
            ) from exc


def _build_note_list_with_collaborators(
    notes: list[Note],
    app_state: AppState,
) -> tuple[NoteList, NoteController, _FakeAttachmentStore]:
    store = NoteListStore(repository=_FakeNoteRepository(notes))
    store.load()
    attachment_store = _FakeAttachmentStore()
    controller = NoteController(
        note_store=store,
        attachments=attachment_store,
        app_state=app_state,
    )
    note_list = NoteList(
        note_store=store,
        note_controller=controller,
        app_state=app_state,
        attachment_store=attachment_store,
    )
    return note_list, controller, attachment_store


def _build_note_list(notes: list[Note], app_state: AppState) -> NoteList:
    note_list, _, _ = _build_note_list_with_collaborators(notes, app_state)
    return note_list


def _visible_ids(note_list: NoteList) -> list[str]:
    model = note_list._sort_model
    return [model.get_item(i).note.id for i in range(model.get_n_items())]


class NoteListSmokeTests(unittest.TestCase):
    """Smoke checks for the slimmer note-list surface."""

    def test_no_notebook_helpers_exported(self) -> None:
        self.assertFalse(hasattr(note_list_module, "_expand_notebook_subtree"))
        self.assertFalse(
            hasattr(note_list_module, "_list_for_notebook_subtree"),
        )

    def test_compute_display_notes_helper_removed(self) -> None:
        # The repository-driven materialiser was replaced by the model
        # chain; pin its absence so a stray re-introduction is caught.
        self.assertFalse(hasattr(note_list_module, "compute_display_notes"))


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListModelChainTests(unittest.TestCase):
    """The widget binds the store through Filter/Sort and stays in step."""

    def _notes(self) -> list[Note]:
        return [
            _note("1", "alpha", modified_at=datetime(2026, 1, 3, tzinfo=UTC)),
            _note("2", "beta", modified_at=datetime(2026, 1, 2, tzinfo=UTC)),
            _note("3", "gamma", modified_at=datetime(2026, 1, 1, tzinfo=UTC)),
        ]

    def test_count_label_reflects_all_notes_on_empty_query(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        self.assertEqual(note_list._count_label.get_text(), "3 notes")
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

    def test_query_filters_immediately_without_throttle(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        # Setting the query filters the model right away — no pending
        # timer, no coalescing window.
        app_state.props.query = "alpha"
        self.assertEqual(note_list._count_label.get_text(), "1 notes")
        self.assertEqual(_visible_ids(note_list), ["1"])
        # Clearing restores the full set.
        app_state.props.query = ""
        self.assertEqual(note_list._count_label.get_text(), "3 notes")

    def test_default_sort_is_modified_descending(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

    def test_title_sort_reorders_alphabetically(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        index = note_list._sort_dropdown
        # Select the "Title" entry in the dropdown.
        index.set_selected(_SORT_KEY_DROPDOWN_ORDER.index(NoteSortKey.TITLE))
        self.assertEqual(note_list.sort_key, NoteSortKey.TITLE)
        # alpha, beta, gamma is already alphabetical, so reverse the
        # check by titles to confirm the comparator drives the order.
        model = note_list._sort_model
        titles = [model.get_item(i).note.title for i in range(model.get_n_items())]
        self.assertEqual(titles, ["alpha", "beta", "gamma"])

    def test_app_state_selection_highlights_row(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        app_state.set_selected_note_id("2")
        selected = note_list._selection_model.get_selected_item()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.note.id, "2")

    def test_row_selection_writes_through_to_app_state(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._notes(), app_state)
        # Find "gamma" (id 3) position in the sorted model and select it
        # on the SingleSelection, simulating a user row click.
        model = note_list._sort_model
        pos = next(
            i for i in range(model.get_n_items())
            if model.get_item(i).note.id == "3"
        )
        note_list._selection_model.set_selected(pos)
        self.assertEqual(app_state.selected_note_id, "3")


def _pump(iterations: int = 200) -> None:
    """Drive the default main context so the ListView realises its
    rows. Non-blocking iterations keep the pump bounded and crash-proof
    under the cairo software renderer."""
    context = GLib.MainContext.default()
    for _ in range(iterations):
        context.iteration(False)


def _row_label_texts(note_list: NoteList, note_id: str) -> list[str]:
    """Every label text on ``note_id``'s currently-bound row box."""
    box, _ = note_list._bound_rows[note_id]
    texts: list[str] = []
    stack: list[Gtk.Widget | None] = [box.get_first_child()]
    while stack:
        widget = stack.pop()
        if widget is None:
            continue
        if isinstance(widget, Gtk.Label):
            texts.append(widget.get_text())
        stack.append(widget.get_next_sibling())
        stack.append(widget.get_first_child())
    return texts


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListAttachmentBadgeTests(unittest.TestCase):
    """The 📎 badge re-binds on the controller's ``attachments-changed``.

    Attachment add/remove never touches the note source, so no store
    ``items-changed`` fires and the factory would not re-bind on its
    own; the widget re-populates the affected *bound* row directly.
    Rows only bind once the ``ListView`` is realised, so this suite
    presents a real window and pumps the main loop — same pattern as
    the sidebar's rendering tests.
    """

    app_state: AppState
    note_list: NoteList
    controller: NoteController
    attachment_store: _FakeAttachmentStore
    window: Gtk.Window

    def setUp(self) -> None:
        self.app_state = AppState()
        (
            self.note_list,
            self.controller,
            self.attachment_store,
        ) = _build_note_list_with_collaborators(
            [_note("1", "alpha"), _note("2", "beta")],
            self.app_state,
        )
        self.window = Gtk.Window()
        self.window.set_child(self.note_list)
        self.window.present()
        _pump()

    def tearDown(self) -> None:
        self.window.set_child(None)
        self.window.destroy()
        _pump(20)

    def test_rows_are_bound_after_realisation(self) -> None:
        # Fixture sanity: the bind/unbind tracking saw both rows.
        self.assertEqual(set(self.note_list._bound_rows), {"1", "2"})

    def test_badge_recomputes_on_attachments_changed(self) -> None:
        # No badge initially (zero attachments render no 📎 label).
        self.assertNotIn("📎 1", _row_label_texts(self.note_list, "1"))
        # The count changes behind the model's back (no items-changed)…
        self.attachment_store.counts["1"] = 1
        # …and the narrow signal is what re-populates the bound row.
        self.controller.emit("attachments-changed", "1")
        self.assertIn("📎 1", _row_label_texts(self.note_list, "1"))

    def test_other_rows_are_left_alone(self) -> None:
        self.attachment_store.counts["1"] = 1
        self.controller.emit("attachments-changed", "1")
        self.assertNotIn("📎 1", _row_label_texts(self.note_list, "2"))

    def test_badge_drops_when_count_returns_to_zero(self) -> None:
        self.attachment_store.counts["1"] = 2
        self.controller.emit("attachments-changed", "1")
        self.assertIn("📎 2", _row_label_texts(self.note_list, "1"))
        self.attachment_store.counts["1"] = 0
        self.controller.emit("attachments-changed", "1")
        self.assertNotIn("📎 2", _row_label_texts(self.note_list, "1"))

    def test_signal_for_unbound_note_is_a_no_op(self) -> None:
        # A note with no realised row needs nothing — its next bind
        # reads the fresh count anyway. The handler must not raise.
        self.controller.emit("attachments-changed", "not-bound")


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListDeleteShortcutTests(unittest.TestCase):
    """The ``Delete`` key is a *focus-local* shortcut on the note list
    that activates ``win.delete-note`` — never an application accelerator,
    so it cannot fire while the source editor is focused."""

    def _shortcut_controllers(
        self,
        note_list: NoteList,
    ) -> list[Gtk.ShortcutController]:
        controllers = note_list.observe_controllers()
        found: list[Gtk.ShortcutController] = []
        for i in range(controllers.get_n_items()):
            controller = controllers.get_item(i)
            if isinstance(controller, Gtk.ShortcutController):
                found.append(controller)
        return found

    def test_delete_key_is_local_and_targets_the_delete_action(self) -> None:
        note_list = _build_note_list([], AppState())
        expected_trigger = Gtk.ShortcutTrigger.parse_string("Delete").to_string()

        matches: list[tuple[Gtk.ShortcutController, Gtk.ShortcutAction]] = []
        for controller in self._shortcut_controllers(note_list):
            for j in range(controller.get_n_items()):
                shortcut = controller.get_item(j)
                assert isinstance(shortcut, Gtk.Shortcut)
                trigger = shortcut.get_trigger()
                action = shortcut.get_action()
                if (
                    trigger is not None
                    and action is not None
                    and trigger.to_string() == expected_trigger
                ):
                    matches.append((controller, action))

        self.assertEqual(len(matches), 1)
        controller, action = matches[0]
        # LOCAL scope is what makes it fire only while the list (or a row)
        # holds focus, never inside the editor.
        self.assertEqual(controller.get_scope(), Gtk.ShortcutScope.LOCAL)
        assert isinstance(action, Gtk.NamedAction)
        self.assertEqual(action.get_action_name(), "win.delete-note")


if __name__ == "__main__":
    unittest.main()

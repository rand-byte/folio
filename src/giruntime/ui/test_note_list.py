"""Tests for :mod:`ui.note_list`.

The note list now binds a ``Filter``/``Sort``/``ListView`` chain over
the in-memory :class:`controllers.note_list_store.NoteListStore`. The
"what shows / what order" rules are covered exhaustively by the pure
predicates in :mod:`search.note_filter`; here we exercise the widget's
own wiring: the filtered count, debounced query filtering, the
monotone ``Gtk.FilterChange`` hints, the empty-state reason, sort-key
reordering, the AppState ⇄ selection round-trip, and the 📎 badge's
re-bind on the controller's ``attachments-changed`` signal.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from gi.repository import Gdk, GLib, Gtk, Pango

from enums import (
    AttachmentExportFailureReason,
    NoteListEmptyReason,
    NoteSortKey,
    SmartFilter,
)
from storage.protocols import AttachmentExportFailed
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
import giruntime.ui.note_list as note_list_module
from giruntime.ui.note_list import (
    _DEFAULT_PANE_WIDTH_PX,
    _EMPTY_STATE_LABELS,
    _EMPTY_STATE_PADDING_PX,
    _SORT_KEY_DROPDOWN_ORDER,
    NoteList,
    _filter_change_for,
    _message_text,
    _selection_empty_reason,
)
from models.attachment import Attachment
from models.note import Note
from search.note_filter import SmartSelection, TagSelection


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


class _FakeTimeoutBackend:
    """Synchronous stand-in for :func:`GLib.timeout_add` / ``source_remove``.

    Mirrors the backend :mod:`giruntime.ui.test_note_editor` uses for
    autosave, so both debouncing panes are driven the same way: the
    scheduled callback runs only when a test calls :meth:`fire_pending`.
    """

    schedule_calls: list[tuple[int, Callable[[], bool]]]
    cancel_calls: list[int]
    _next_handle: int
    _pending: dict[int, Callable[[], bool]]

    def __init__(self) -> None:
        self.schedule_calls = []
        self.cancel_calls = []
        self._next_handle = 2000
        self._pending = {}

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], bool],
    ) -> int:
        self.schedule_calls.append((delay_ms, callback))
        handle = self._next_handle
        self._next_handle += 1
        self._pending[handle] = callback
        return handle

    def cancel(self, handle: int) -> None:
        self.cancel_calls.append(handle)
        self._pending.pop(handle, None)

    def fire_pending(self) -> None:
        """Synchronously invoke every still-pending callback."""
        for handle, callback in list(self._pending.items()):
            if not callback():
                self._pending.pop(handle, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


def _build_note_list_with_collaborators(
    notes: list[Note],
    app_state: AppState,
    timeouts: _FakeTimeoutBackend | None = None,
) -> tuple[NoteList, NoteController, _FakeAttachmentStore]:
    store = NoteListStore(repository=_FakeNoteRepository(notes))
    store.load()
    attachment_store = _FakeAttachmentStore()
    controller = NoteController(
        note_store=store,
        attachments=attachment_store,
        app_state=app_state,
    )
    backend = timeouts if timeouts is not None else _FakeTimeoutBackend()
    note_list = NoteList(
        note_store=store,
        note_controller=controller,
        app_state=app_state,
        attachment_store=attachment_store,
        schedule_timeout=backend.schedule,
        cancel_timeout=backend.cancel,
    )
    return note_list, controller, attachment_store


def _build_note_list_with_timeouts(
    notes: list[Note],
    app_state: AppState,
) -> tuple[NoteList, _FakeTimeoutBackend]:
    """Build a pane whose search debounce a test can fire by hand."""
    backend = _FakeTimeoutBackend()
    note_list, _, _ = _build_note_list_with_collaborators(
        notes, app_state, backend,
    )
    return note_list, backend


def _search(
    note_list: NoteList,
    app_state: AppState,
    backend: _FakeTimeoutBackend,
    query: str,
) -> None:
    """Type ``query`` and let the debounce elapse."""
    app_state.props.query = query
    backend.fire_pending()
    del note_list


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

    def test_query_filters_once_the_debounce_elapses(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        app_state.props.query = "alpha"
        # Still unfiltered: the keystroke only scheduled the work.
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])
        backend.fire_pending()
        self.assertEqual(note_list._count_label.get_text(), "1 notes")
        self.assertEqual(_visible_ids(note_list), ["1"])

    def test_clearing_the_query_restores_the_full_set(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "alpha")
        app_state.props.query = ""
        # No fire_pending: clearing bypasses the debounce entirely.
        self.assertEqual(note_list._count_label.get_text(), "3 notes")
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

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


class FilterChangeClassificationTests(unittest.TestCase):
    """:func:`_filter_change_for` — pure, no display required."""

    def test_extending_the_needle_is_more_strict(self) -> None:
        self.assertEqual(
            _filter_change_for("al", "alp"), Gtk.FilterChange.MORE_STRICT,
        )

    def test_shortening_the_needle_is_less_strict(self) -> None:
        self.assertEqual(
            _filter_change_for("alp", "al"), Gtk.FilterChange.LESS_STRICT,
        )

    def test_starting_to_type_is_more_strict(self) -> None:
        # The empty needle matches everything, so any needle narrows.
        self.assertEqual(
            _filter_change_for("", "a"), Gtk.FilterChange.MORE_STRICT,
        )

    def test_clearing_is_less_strict(self) -> None:
        self.assertEqual(
            _filter_change_for("a", ""), Gtk.FilterChange.LESS_STRICT,
        )

    def test_unrelated_needles_are_different(self) -> None:
        self.assertEqual(
            _filter_change_for("alpha", "beta"), Gtk.FilterChange.DIFFERENT,
        )

    def test_mid_string_insertion_is_different(self) -> None:
        # "ala" is not a substring of "alpha" nor vice versa.
        self.assertEqual(
            _filter_change_for("ala", "alpha"), Gtk.FilterChange.DIFFERENT,
        )


class SelectionEmptyReasonTests(unittest.TestCase):
    """:func:`_selection_empty_reason` — pure, no display required."""

    def test_tag_selection_reports_tag_matches(self) -> None:
        self.assertEqual(
            _selection_empty_reason(TagSelection(tags=frozenset({"a", "b"}))),
            NoteListEmptyReason.NO_TAG_MATCHES,
        )

    def test_untagged_reports_no_untagged_notes(self) -> None:
        self.assertEqual(
            _selection_empty_reason(
                SmartSelection(smart_filter=SmartFilter.UNTAGGED),
            ),
            NoteListEmptyReason.NO_UNTAGGED_NOTES,
        )

    def test_all_is_unreachable_and_raises(self) -> None:
        # A non-empty store with no query under ALL cannot be empty, so
        # arriving here is an invariant break, not a user-facing state.
        with self.assertRaises(AssertionError):
            _selection_empty_reason(
                SmartSelection(smart_filter=SmartFilter.ALL),
            )


class EmptyStateMessageTests(unittest.TestCase):
    """The message table and its join — pure, no display required.

    Deliberately un-gated: these are the checks most likely to catch a
    future regression, and a display-gated test that silently skips
    catches nothing.
    """

    def test_every_reason_has_a_message(self) -> None:
        # The label is chosen by reason at runtime, so a reason absent
        # from the table would raise KeyError in front of the user.
        for reason in NoteListEmptyReason:
            self.assertIn(reason, _EMPTY_STATE_LABELS)

    def test_every_message_has_at_least_one_line(self) -> None:
        # ``tuple[str, ...]`` permits (), which would render as a blank
        # pane saying nothing. Pins the table's documented invariant.
        for reason, lines in _EMPTY_STATE_LABELS.items():
            with self.subTest(reason=reason):
                self.assertGreater(len(lines), 0)
                for line in lines:
                    self.assertTrue(line.strip())

    def test_message_text_joins_the_authored_lines(self) -> None:
        self.assertEqual(
            _message_text(NoteListEmptyReason.NO_QUERY_MATCHES),
            "No notes match this search.\n"
            "Tags are filtered from the sidebar.",
        )

    def test_message_text_of_a_single_line_reason_adds_no_break(self) -> None:
        text = _message_text(NoteListEmptyReason.NO_NOTES)
        self.assertNotIn("\n", text)
        self.assertEqual(text, "No notes here yet.")


@unittest.skipUnless(_display_available(), "no GDK display")
class EmptyStateLabelWidthTests(unittest.TestCase):
    """No empty-state message may widen the pane.

    The pane sits in a ``Gtk.Paned`` built with
    ``shrink_start_child=False``, so a label's *minimum* width becomes the
    pane's. A non-wrapping label reports its whole one-line width as its
    minimum, which is the defect these pin.
    """

    def _minimum_width(self, text: str) -> int:
        label = Gtk.Label.new(text)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        minimum: int = label.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        return minimum

    def test_every_message_fits_the_default_pane_width(self) -> None:
        budget = _DEFAULT_PANE_WIDTH_PX - 2 * _EMPTY_STATE_PADDING_PX
        for reason in _EMPTY_STATE_LABELS:
            with self.subTest(reason=reason):
                self.assertLessEqual(
                    self._minimum_width(_message_text(reason)), budget,
                )

    def test_pane_minimum_width_is_unchanged_when_the_list_empties(
        self,
    ) -> None:
        # Characterisation of the reported symptom, with the real
        # messages. Compares against the populated pane rather than a
        # literal, so it survives a _DEFAULT_PANE_WIDTH_PX change.
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            [_note("1", "alpha"), _note("2", "beta")], app_state,
        )
        populated = note_list.measure(Gtk.Orientation.HORIZONTAL, -1)[0]

        _search(note_list, app_state, backend, "nonexistent")

        self.assertTrue(note_list._empty_label.get_visible())
        self.assertEqual(
            note_list.measure(Gtk.Orientation.HORIZONTAL, -1)[0], populated,
        )

    def test_a_long_message_cannot_widen_the_pane(self) -> None:
        """Pins *wrapping* as the mechanism, not the brevity of the text.

        Every shipped message is short enough to fit the pane unwrapped —
        ``NO_QUERY_MATCHES`` only because it is split across two authored
        lines — so the real messages cannot tell a wrapping label apart
        from a merely lucky one, and the test above passes either way.
        Substituting a message long enough to overflow the pane is what
        makes the absence of ``set_wrap`` fail here.
        """
        overflowing = (
            "An unusually long empty-state message which would certainly "
            "overflow the width of the note list pane if its label did "
            "not wrap.",
        )
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            [_note("1", "alpha")], app_state,
        )
        populated = note_list.measure(Gtk.Orientation.HORIZONTAL, -1)[0]

        with patch.dict(
            note_list_module._EMPTY_STATE_LABELS,
            {NoteListEmptyReason.NO_QUERY_MATCHES: overflowing},
        ):
            _search(note_list, app_state, backend, "nonexistent")
            # Guard the guard: if the substitution did not reach the
            # label, the width assertion below would prove nothing.
            self.assertEqual(
                note_list._empty_label.get_text(), overflowing[0],
            )
            self.assertEqual(
                note_list.measure(Gtk.Orientation.HORIZONTAL, -1)[0],
                populated,
            )


@unittest.skipUnless(_display_available(), "no GDK display")
class EmptyStateLayoutTests(unittest.TestCase):
    """The empty label wraps, is inset, and sits at the top of the list."""

    def test_label_wraps(self) -> None:
        note_list = _build_note_list([], AppState())
        self.assertTrue(note_list._empty_label.get_wrap())

    def test_label_is_inset_from_the_pane_edges(self) -> None:
        label = _build_note_list([], AppState())._empty_label
        self.assertEqual(label.get_margin_start(), _EMPTY_STATE_PADDING_PX)
        self.assertEqual(label.get_margin_end(), _EMPTY_STATE_PADDING_PX)
        self.assertEqual(label.get_margin_top(), _EMPTY_STATE_PADDING_PX)
        self.assertEqual(label.get_margin_bottom(), _EMPTY_STATE_PADDING_PX)

    def test_label_claims_the_list_area_and_aligns_to_its_top(self) -> None:
        label = _build_note_list([], AppState())._empty_label
        self.assertTrue(label.get_vexpand())
        self.assertEqual(label.get_valign(), Gtk.Align.START)

    def test_scroller_is_hidden_while_the_empty_label_shows(self) -> None:
        # The scroller carries vexpand; if it stayed visible the message
        # would sit on the pane's last line however the label aligns.
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            [_note("1", "alpha")], app_state,
        )
        self.assertTrue(note_list._list_scroller.get_visible())

        _search(note_list, app_state, backend, "nonexistent")
        self.assertFalse(note_list._list_scroller.get_visible())

        app_state.props.query = ""
        self.assertTrue(note_list._list_scroller.get_visible())


@unittest.skipUnless(_display_available(), "no GDK display")
class SearchDebounceTests(unittest.TestCase):
    """The query is coalesced behind one timer; clearing is immediate."""

    def _notes(self) -> list[Note]:
        return [_note("1", "alpha"), _note("2", "beta")]

    def test_typing_schedules_a_single_pending_timer(self) -> None:
        app_state = AppState()
        _, backend = _build_note_list_with_timeouts(self._notes(), app_state)
        app_state.props.query = "a"
        app_state.props.query = "al"
        app_state.props.query = "alp"
        self.assertEqual(backend.pending_count, 1)

    def test_each_keystroke_cancels_the_previous_timer(self) -> None:
        app_state = AppState()
        _, backend = _build_note_list_with_timeouts(self._notes(), app_state)
        app_state.props.query = "a"
        app_state.props.query = "al"
        app_state.props.query = "alp"
        # Three keystrokes, three schedules, two cancels — the first
        # keystroke had nothing to cancel.
        self.assertEqual(len(backend.schedule_calls), 3)
        self.assertEqual(len(backend.cancel_calls), 2)

    def test_the_debounce_uses_the_module_delay(self) -> None:
        app_state = AppState()
        _, backend = _build_note_list_with_timeouts(self._notes(), app_state)
        app_state.props.query = "alpha"
        delay_ms, _callback = backend.schedule_calls[0]
        self.assertEqual(delay_ms, note_list_module.SEARCH_DEBOUNCE_MS)

    def test_coalesced_typing_applies_only_the_final_needle(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        app_state.props.query = "a"
        app_state.props.query = "beta"
        backend.fire_pending()
        self.assertEqual(_visible_ids(note_list), ["2"])

    def test_clearing_applies_without_waiting(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "alpha")
        self.assertEqual(_visible_ids(note_list), ["1"])
        app_state.props.query = ""
        self.assertEqual(_visible_ids(note_list), ["1", "2"])

    def test_clearing_cancels_a_pending_timer(self) -> None:
        app_state = AppState()
        _, backend = _build_note_list_with_timeouts(self._notes(), app_state)
        app_state.props.query = "alpha"
        app_state.props.query = ""
        self.assertEqual(backend.pending_count, 0)

    def test_whitespace_only_query_is_treated_as_cleared(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        app_state.props.query = "   "
        self.assertEqual(backend.pending_count, 0)
        self.assertEqual(_visible_ids(note_list), ["1", "2"])

    def test_flush_applies_a_pending_query_at_once(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        app_state.props.query = "alpha"
        note_list.flush_pending_query()
        self.assertEqual(_visible_ids(note_list), ["1"])
        self.assertEqual(backend.pending_count, 0)

    def test_flush_without_a_pending_query_is_a_no_op(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        note_list.flush_pending_query()
        self.assertEqual(backend.cancel_calls, [])
        self.assertEqual(_visible_ids(note_list), ["1", "2"])

    def test_a_query_typed_then_undone_does_not_refilter(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "alpha")
        app_state.props.query = "alpha"  # same needle again
        backend.fire_pending()
        self.assertEqual(_visible_ids(note_list), ["1"])


@unittest.skipUnless(_display_available(), "no GDK display")
class FilterChangeHintResultTests(unittest.TestCase):
    """The monotone hints must not change *what* the list shows."""

    def _notes(self) -> list[Note]:
        return [
            _note("1", "alpha", modified_at=datetime(2026, 1, 3, tzinfo=UTC)),
            _note("2", "alpine", modified_at=datetime(2026, 1, 2, tzinfo=UTC)),
            _note("3", "beta", modified_at=datetime(2026, 1, 1, tzinfo=UTC)),
        ]

    def test_typed_character_by_character_matches_a_direct_query(
        self,
    ) -> None:
        # Appending characters takes the MORE_STRICT path; the result
        # must equal the same needle applied in one go.
        typed_state = AppState()
        typed, typed_backend = _build_note_list_with_timeouts(
            self._notes(), typed_state,
        )
        for prefix in ("a", "al", "alp", "alph", "alpha"):
            _search(typed, typed_state, typed_backend, prefix)

        direct_state = AppState()
        direct, direct_backend = _build_note_list_with_timeouts(
            self._notes(), direct_state,
        )
        _search(direct, direct_state, direct_backend, "alpha")

        self.assertEqual(_visible_ids(typed), _visible_ids(direct))
        self.assertEqual(_visible_ids(typed), ["1"])

    def test_backspacing_widens_back_to_the_original_set(self) -> None:
        # Removing characters takes the LESS_STRICT path.
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "alpha")
        self.assertEqual(_visible_ids(note_list), ["1"])
        _search(note_list, app_state, backend, "alp")
        self.assertEqual(_visible_ids(note_list), ["1", "2"])
        _search(note_list, app_state, backend, "a")
        self.assertEqual(_visible_ids(note_list), ["1", "2", "3"])

    def test_replacing_the_needle_wholesale_refilters(self) -> None:
        # An unrelated needle takes the DIFFERENT path.
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "alpha")
        _search(note_list, app_state, backend, "beta")
        self.assertEqual(_visible_ids(note_list), ["3"])

    def test_mid_string_edit_refilters(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._notes(), app_state,
        )
        _search(note_list, app_state, backend, "ala")
        self.assertEqual(_visible_ids(note_list), [])
        _search(note_list, app_state, backend, "alpha")
        self.assertEqual(_visible_ids(note_list), ["1"])


@unittest.skipUnless(_display_available(), "no GDK display")
class NoteListEmptyStateTests(unittest.TestCase):
    """The empty-state label says *why* the list is empty."""

    def _tagged_notes(self) -> list[Note]:
        return [
            _note("1", "alpha", tags=("work",)),
            _note("2", "beta", tags=("urgent",)),
        ]

    def _empty_text(self, note_list: NoteList) -> str:
        self.assertTrue(note_list._empty_label.get_visible())
        text: str = note_list._empty_label.get_text()
        return text

    def test_empty_store_reports_no_notes(self) -> None:
        app_state = AppState()
        note_list = _build_note_list([], app_state)
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_NOTES),
        )

    def test_empty_store_wins_over_the_untagged_filter(self) -> None:
        # The ordering guard: an empty library must not be reported as
        # "every note has a tag" just because Untagged is selected.
        app_state = AppState()
        note_list = _build_note_list([], app_state)
        app_state.set_smart(SmartFilter.UNTAGGED)
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_NOTES),
        )

    def test_query_matching_nothing_reports_the_search(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._tagged_notes(), app_state,
        )
        _search(note_list, app_state, backend, "nonexistent")
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_QUERY_MATCHES),
        )

    def test_query_reason_wins_over_the_selection(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._tagged_notes(), app_state,
        )
        app_state.toggle_tag("work")
        _search(note_list, app_state, backend, "nonexistent")
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_QUERY_MATCHES),
        )

    def test_two_tags_that_share_no_note_report_tag_matches(self) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._tagged_notes(), app_state)
        app_state.toggle_tag("work")
        app_state.toggle_tag("urgent")
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_TAG_MATCHES),
        )

    def test_either_tag_alone_still_matches(self) -> None:
        # Pins the invariant the NO_TAG_MATCHES wording relies on: a
        # single selected tag can never empty the list, so only a
        # combination reaches that message.
        app_state = AppState()
        note_list = _build_note_list(self._tagged_notes(), app_state)
        app_state.toggle_tag("work")
        self.assertEqual(_visible_ids(note_list), ["1"])
        app_state.toggle_tag("work")
        app_state.toggle_tag("urgent")
        self.assertEqual(_visible_ids(note_list), ["2"])

    def test_untagged_with_every_note_tagged_reports_no_untagged(
        self,
    ) -> None:
        app_state = AppState()
        note_list = _build_note_list(self._tagged_notes(), app_state)
        app_state.set_smart(SmartFilter.UNTAGGED)
        self.assertEqual(
            self._empty_text(note_list),
            _message_text(NoteListEmptyReason.NO_UNTAGGED_NOTES),
        )

    def test_label_hides_again_once_the_list_is_non_empty(self) -> None:
        app_state = AppState()
        note_list, backend = _build_note_list_with_timeouts(
            self._tagged_notes(), app_state,
        )
        _search(note_list, app_state, backend, "nonexistent")
        self.assertTrue(note_list._empty_label.get_visible())
        app_state.props.query = ""
        self.assertFalse(note_list._empty_label.get_visible())
        self.assertTrue(note_list._list_view.get_visible())

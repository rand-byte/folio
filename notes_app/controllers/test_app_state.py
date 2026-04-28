"""Tests for :mod:`notes_app.controllers.app_state`."""

from __future__ import annotations

import unittest

from notes_app.controllers.app_state import AppState
from notes_app.enums import SmartFilter, ViewMode
from notes_app.search.note_filter import (
    NotebookSelection,
    SmartSelection,
)


class _Recorder:
    """Captures GObject signal emissions on an :class:`AppState`.

    The recorder connects one handler per signal at construction
    and stores ``(signal_name, payload_tuple)`` for every fire. Tests
    use this rather than per-test ``connect`` calls so the assertion
    surface is a single ``events`` list.
    """

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, state: AppState) -> None:
        self.events = []
        for signal in (
            "selection-changed",
            "selected-note-changed",
            "view-mode-changed",
            "query-changed",
        ):
            state.connect(signal, self._make_handler(signal))

    def _make_handler(self, signal: str):  # type: ignore[no-untyped-def]
        def handler(_obj: AppState, *args: object) -> None:
            self.events.append((signal, args))
        return handler

    def names(self) -> list[str]:
        return [event[0] for event in self.events]


class AppStateInitialValueTests(unittest.TestCase):
    def test_default_selection_is_smart_all(self) -> None:
        state = AppState()
        self.assertEqual(
            state.selection,
            SmartSelection(smart_filter=SmartFilter.ALL),
        )

    def test_default_view_mode_is_view(self) -> None:
        state = AppState()
        self.assertIs(state.view_mode, ViewMode.VIEW)

    def test_default_selected_note_id_is_none(self) -> None:
        self.assertIsNone(AppState().selected_note_id)

    def test_default_query_is_empty_string(self) -> None:
        self.assertEqual(AppState().query, "")

    def test_initial_selection_override(self) -> None:
        chosen = NotebookSelection(notebook_id="nb-x")
        state = AppState(initial_selection=chosen)
        self.assertEqual(state.selection, chosen)

    def test_initial_view_mode_override(self) -> None:
        state = AppState(initial_view_mode=ViewMode.EDIT)
        self.assertIs(state.view_mode, ViewMode.EDIT)


class AppStateSelectionTests(unittest.TestCase):
    def test_set_selection_updates_property(self) -> None:
        state = AppState()
        target = NotebookSelection(notebook_id="nb-1")
        state.set_selection(target)
        self.assertEqual(state.selection, target)

    def test_set_selection_emits_signal(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selection(NotebookSelection(notebook_id="nb-1"))
        self.assertEqual(rec.names(), ["selection-changed"])

    def test_set_selection_no_op_emits_nothing(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        # The default selection is the same SmartSelection(ALL); setting
        # an equal value must not emit. This protects subscribers from
        # spurious re-renders caused by repeated setter calls.
        state.set_selection(SmartSelection(smart_filter=SmartFilter.ALL))
        self.assertEqual(rec.events, [])

    def test_set_selection_does_not_clear_selected_note_id(self) -> None:
        state = AppState()
        state.set_selected_note_id("n-42")
        rec = _Recorder(state)
        state.set_selection(NotebookSelection(notebook_id="nb-1"))
        self.assertEqual(state.selected_note_id, "n-42")
        self.assertEqual(rec.names(), ["selection-changed"])

    def test_smart_to_notebook_and_back_each_emit_once(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selection(NotebookSelection(notebook_id="nb-1"))
        state.set_selection(SmartSelection(smart_filter=SmartFilter.RECENT))
        state.set_selection(SmartSelection(smart_filter=SmartFilter.ALL))
        self.assertEqual(rec.names(), ["selection-changed"] * 3)

    def test_listener_observes_new_value_during_signal(self) -> None:
        # Ordering invariant: the property is updated before the signal
        # fires, so a listener that reads ``state.selection`` during
        # its handler sees the new value, never the old one.
        state = AppState()
        observed: list[object] = []

        def on_change(obj: AppState) -> None:
            observed.append(obj.selection)

        state.connect("selection-changed", on_change)
        target = NotebookSelection(notebook_id="nb-1")
        state.set_selection(target)
        self.assertEqual(observed, [target])


class AppStateSelectedNoteTests(unittest.TestCase):
    def test_set_note_id_updates_property(self) -> None:
        state = AppState()
        state.set_selected_note_id("n-1")
        self.assertEqual(state.selected_note_id, "n-1")

    def test_set_note_id_emits_signal(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selected_note_id("n-1")
        self.assertEqual(rec.names(), ["selected-note-changed"])

    def test_set_note_id_no_op_emits_nothing(self) -> None:
        state = AppState()
        state.set_selected_note_id("n-1")
        rec = _Recorder(state)
        state.set_selected_note_id("n-1")
        self.assertEqual(rec.events, [])

    def test_set_note_id_to_none_emits_when_was_set(self) -> None:
        state = AppState()
        state.set_selected_note_id("n-1")
        rec = _Recorder(state)
        state.set_selected_note_id(None)
        self.assertEqual(rec.names(), ["selected-note-changed"])
        self.assertIsNone(state.selected_note_id)

    def test_set_note_id_none_when_already_none_is_no_op(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selected_note_id(None)
        self.assertEqual(rec.events, [])


class AppStateViewModeTests(unittest.TestCase):
    def test_set_view_mode_updates_property(self) -> None:
        state = AppState()
        state.set_view_mode(ViewMode.EDIT)
        self.assertIs(state.view_mode, ViewMode.EDIT)

    def test_set_view_mode_emits_signal(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_view_mode(ViewMode.EDIT)
        self.assertEqual(rec.names(), ["view-mode-changed"])

    def test_set_view_mode_no_op_emits_nothing(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_view_mode(ViewMode.VIEW)  # default is VIEW
        self.assertEqual(rec.events, [])

    def test_toggle_view_to_edit_and_back(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_view_mode(ViewMode.EDIT)
        state.set_view_mode(ViewMode.VIEW)
        self.assertEqual(
            rec.names(),
            ["view-mode-changed", "view-mode-changed"],
        )


class AppStateQueryTests(unittest.TestCase):
    def test_set_query_updates_property(self) -> None:
        state = AppState()
        state.set_query("foo")
        self.assertEqual(state.query, "foo")

    def test_set_query_emits_signal(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_query("foo")
        self.assertEqual(rec.names(), ["query-changed"])

    def test_set_query_no_op_emits_nothing(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_query("")  # default is ""
        self.assertEqual(rec.events, [])

    def test_query_stored_verbatim_with_whitespace(self) -> None:
        # The query is stored verbatim — stripping is the search
        # layer's job. This pins the contract so a future "convenience"
        # strip in the setter (which would diverge from the SQL LIKE
        # query the repository runs) is caught immediately.
        state = AppState()
        rec = _Recorder(state)
        state.set_query("  hello  ")
        self.assertEqual(state.query, "  hello  ")
        self.assertEqual(rec.names(), ["query-changed"])

    def test_set_query_same_value_after_change_emits_nothing(self) -> None:
        state = AppState()
        state.set_query("foo")
        rec = _Recorder(state)
        state.set_query("foo")
        self.assertEqual(rec.events, [])


class AppStateSignalIsolationTests(unittest.TestCase):
    """Each setter only fires its own signal — no cross-talk.

    These tests pin the rule that ``set_X`` does not emit ``Y-changed``
    even as a side-effect, which is what makes per-signal listeners a
    safe pattern for widgets.
    """

    def test_selection_change_does_not_emit_other_signals(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selection(NotebookSelection(notebook_id="nb-1"))
        self.assertEqual(rec.names(), ["selection-changed"])

    def test_note_id_change_does_not_emit_other_signals(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_selected_note_id("n-1")
        self.assertEqual(rec.names(), ["selected-note-changed"])

    def test_view_mode_change_does_not_emit_other_signals(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_view_mode(ViewMode.EDIT)
        self.assertEqual(rec.names(), ["view-mode-changed"])

    def test_query_change_does_not_emit_other_signals(self) -> None:
        state = AppState()
        rec = _Recorder(state)
        state.set_query("foo")
        self.assertEqual(rec.names(), ["query-changed"])


if __name__ == "__main__":
    unittest.main()

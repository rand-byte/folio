"""Tests for :mod:`controllers.app_state`."""

from __future__ import annotations

import unittest
from collections.abc import Callable

from giruntime.controllers.app_state import AppState
from enums import SmartFilter, ViewMode
from search.note_filter import SmartSelection, TagSelection


class _Recorder:
    """Captures GObject property-change notifications on an :class:`AppState`.

    The four navigational fields are GObject properties; observers
    subscribe to ``notify::<prop>`` rather than to bespoke signals. The
    handler tolerates the trailing :class:`GObject.ParamSpec` argument
    GObject passes to a ``notify`` callback via its ``*args`` catch-all.
    """

    events: list[tuple[str, tuple[object, ...]]]

    def __init__(self, state: AppState) -> None:
        self.events = []
        for signal in (
            "notify::selection",
            "notify::selected-note-id",
            "notify::view-mode",
            "notify::query",
        ):
            state.connect(signal, self._make_handler(signal))

    def _make_handler(self, signal: str) -> Callable[..., None]:
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

    def test_default_selected_note_is_none(self) -> None:
        self.assertIsNone(AppState().selected_note_id)

    def test_default_view_mode_is_view(self) -> None:
        self.assertEqual(AppState().view_mode, ViewMode.VIEW)

    def test_default_query_is_empty(self) -> None:
        self.assertEqual(AppState().query, "")

    def test_initial_view_mode_override(self) -> None:
        state = AppState(initial_view_mode=ViewMode.EDIT)
        self.assertEqual(state.view_mode, ViewMode.EDIT)

    def test_initial_selection_override(self) -> None:
        state = AppState(
            initial_selection=SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )
        self.assertEqual(
            state.selection,
            SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )


class SetSmartTests(unittest.TestCase):
    def test_picks_a_different_smart_filter(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.set_smart(SmartFilter.UNTAGGED)
        self.assertEqual(
            state.selection,
            SmartSelection(smart_filter=SmartFilter.UNTAGGED),
        )
        self.assertEqual(recorder.names(), ["notify::selection"])

    def test_picking_same_smart_is_no_op(self) -> None:
        state = AppState()  # starts at ALL
        recorder = _Recorder(state)
        state.set_smart(SmartFilter.ALL)
        self.assertEqual(recorder.names(), [])

    def test_smart_wipes_a_tag_selection(self) -> None:
        state = AppState()
        state.toggle_tag("baking")
        self.assertIsInstance(state.selection, TagSelection)
        state.set_smart(SmartFilter.ALL)
        self.assertEqual(
            state.selection,
            SmartSelection(smart_filter=SmartFilter.ALL),
        )


class ToggleTagTests(unittest.TestCase):
    def test_from_smart_creates_tag_selection(self) -> None:
        state = AppState()
        state.toggle_tag("baking")
        self.assertEqual(
            state.selection,
            TagSelection(tags=frozenset({"baking"})),
        )

    def test_adding_a_second_tag_widens_set(self) -> None:
        state = AppState()
        state.toggle_tag("baking")
        state.toggle_tag("bread")
        sel = state.selection
        assert isinstance(sel, TagSelection)
        self.assertEqual(sel.tags, frozenset({"baking", "bread"}))

    def test_removing_one_of_two_leaves_one(self) -> None:
        state = AppState()
        state.toggle_tag("baking")
        state.toggle_tag("bread")
        state.toggle_tag("baking")
        self.assertEqual(
            state.selection,
            TagSelection(tags=frozenset({"bread"})),
        )

    def test_removing_last_tag_reverts_to_all(self) -> None:
        state = AppState()
        state.toggle_tag("baking")
        state.toggle_tag("baking")
        self.assertEqual(
            state.selection,
            SmartSelection(smart_filter=SmartFilter.ALL),
        )

    def test_each_toggle_emits_selection_changed(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.toggle_tag("a")
        state.toggle_tag("b")
        state.toggle_tag("a")  # back to {b}
        self.assertEqual(recorder.names(), [
            "notify::selection",
            "notify::selection",
            "notify::selection",
        ])


class SetSelectedNoteIdTests(unittest.TestCase):
    def test_setting_new_id_emits_signal(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.set_selected_note_id("n1")
        self.assertEqual(state.selected_note_id, "n1")
        self.assertEqual(recorder.names(), ["notify::selected-note-id"])

    def test_setting_same_id_is_no_op(self) -> None:
        state = AppState()
        state.set_selected_note_id("n1")
        recorder = _Recorder(state)
        state.set_selected_note_id("n1")
        self.assertEqual(recorder.names(), [])

    def test_clearing_to_none(self) -> None:
        state = AppState()
        state.set_selected_note_id("n1")
        state.set_selected_note_id(None)
        self.assertIsNone(state.selected_note_id)


class SetViewModeTests(unittest.TestCase):
    def test_switching_modes_emits(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.set_view_mode(ViewMode.EDIT)
        self.assertEqual(state.view_mode, ViewMode.EDIT)
        self.assertEqual(recorder.names(), ["notify::view-mode"])

    def test_same_mode_is_no_op(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.set_view_mode(ViewMode.VIEW)
        self.assertEqual(recorder.names(), [])


class SetQueryTests(unittest.TestCase):
    def test_setting_query_emits(self) -> None:
        state = AppState()
        recorder = _Recorder(state)
        state.props.query = "hi"
        self.assertEqual(state.query, "hi")
        self.assertEqual(recorder.names(), ["notify::query"])

    def test_setting_same_query_re_emits(self) -> None:
        # ``query`` is a stored GObject property bound bidirectionally to
        # the search entry; its generic setter notifies even when the
        # value is unchanged (DECISION 2 — accepted). This cannot loop —
        # the bidirectional binding suppresses any reverse echo — and a
        # re-filter on an identical query is idempotent, so the redundant
        # notification is harmless. The three rule-bearing fields keep
        # their strict change-only guards; only ``query`` is relaxed.
        state = AppState()
        state.props.query = "hi"
        recorder = _Recorder(state)
        state.props.query = "hi"
        self.assertEqual(recorder.names(), ["notify::query"])

    def test_whitespace_is_significant(self) -> None:
        # Stripping happens in filter_by_query, not here — the query is
        # stored verbatim (an invariant the search-entry binding relies on).
        state = AppState()
        state.props.query = "  hi  "
        self.assertEqual(state.query, "  hi  ")


if __name__ == "__main__":
    unittest.main()

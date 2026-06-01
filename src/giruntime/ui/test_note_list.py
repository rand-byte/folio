"""Tests for :mod:`ui.note_list`.

The pre-tags note-list tests exercised notebook-subtree expansion and
per-notebook list helpers — both removed in the tag migration. The
remaining list-display surface (sort, search filtering, row
construction) is covered by the pure functions in
:mod:`search.note_filter` and by the integration paths in
:mod:`ui.test_main_window`.

What is exercised directly here is the **query-refresh throttle**: a
burst of per-keystroke ``notify::query`` notifications must coalesce
into a single pending refresh, and flushing that refresh must render
the *current* query. The throttle is driven without a running GLib
main loop — the timer never fires on its own, so the tests assert the
scheduling/coalescing bookkeeping and call the flush callback directly.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from gi.repository import GLib, Gdk, Gtk

from giruntime.controllers.app_state import AppState
from models.attachment import Attachment
from models.note import Note
import giruntime.ui.note_list as note_list_module
from giruntime.ui.note_list import NoteList


_FIXED_NOW: datetime = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget
    construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


def _note(note_id: str, title: str) -> Note:
    return Note(
        id=note_id,
        title=title,
        source=f"= {title}\n",
        snippet=title,
        tags=(),
        created_at=_FIXED_NOW,
        modified_at=_FIXED_NOW,
    )


class _FakeNoteRepository:
    """Minimal repository returning a fixed note set from ``list_all``."""

    _notes: list[Note]

    def __init__(self, notes: list[Note]) -> None:
        self._notes = notes

    def list_all(self) -> list[Note]:
        return list(self._notes)

    def get(self, _note_id: str) -> Note:
        raise NotImplementedError

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, _note: Note) -> None:
        raise NotImplementedError

    def update_source(
        self,
        _note_id: str,
        _source: str,
        _modified_at: datetime,
    ) -> None:
        raise NotImplementedError

    def delete(self, _note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


class _FakeAttachmentStore:
    """Reports zero attachments; no other method is called here."""

    def add_for_note(self, _note_id: str, _source_path: Path) -> Attachment:
        raise NotImplementedError

    def remove(self, _attachment_id: str) -> None:
        raise NotImplementedError

    def list_for_note(self, _note_id: str) -> list[Attachment]:
        raise NotImplementedError

    def count_for_note(self, _note_id: str) -> int:
        return 0

    def get_bytes(self, _attachment_id: str) -> bytes:
        raise NotImplementedError


def _build_note_list(notes: list[Note], app_state: AppState) -> NoteList:
    return NoteList(
        note_repository=_FakeNoteRepository(notes),
        app_state=app_state,
        clock=lambda: _FIXED_NOW,
        attachment_store=_FakeAttachmentStore(),
    )


class NoteListSmokeTests(unittest.TestCase):
    """Smoke checks for the slimmer note-list surface."""

    def test_no_notebook_helpers_exported(self) -> None:
        # The pre-tags note list exposed a number of notebook-subtree
        # helpers at module scope. The tag-based note list drops the
        # whole concept; this pins their absence.
        self.assertFalse(hasattr(note_list_module, "_expand_notebook_subtree"))
        self.assertFalse(
            hasattr(note_list_module, "_list_for_notebook_subtree"),
        )


@unittest.skipUnless(_display_available(), "no GDK display")
class QueryRefreshThrottleTests(unittest.TestCase):
    """The per-keystroke query refresh is coalesced into one pending run."""

    def test_burst_schedules_a_single_coalesced_refresh(self) -> None:
        notes = [_note("1", "alpha"), _note("2", "beta"), _note("3", "gamma")]
        app_state = AppState()
        note_list = _build_note_list(notes, app_state)
        # The construction-time refresh ran synchronously: empty query
        # passes everything through, so all three show.
        self.assertEqual(note_list._count_label.get_text(), "3 notes")
        self.assertIsNone(note_list._pending_refresh_id)

        # A burst of per-keystroke notifications. The first schedules a
        # source; the rest see one pending and do not reschedule.
        app_state.props.query = "a"
        first_id = note_list._pending_refresh_id
        self.assertIsNotNone(first_id)
        for partial in ("al", "alp", "alph", "alpha"):
            app_state.props.query = partial
            self.assertEqual(note_list._pending_refresh_id, first_id)

        # The expensive refresh has NOT run during the burst — no main
        # loop is running, so the scheduled timer never fired and the
        # list still shows the pre-burst result.
        self.assertEqual(note_list._count_label.get_text(), "3 notes")

        # Remove the still-registered GLib source so it cannot linger.
        note_list._cancel_pending_refresh()
        self.assertIsNone(note_list._pending_refresh_id)

    def test_flush_renders_the_current_query(self) -> None:
        notes = [_note("1", "alpha"), _note("2", "beta"), _note("3", "gamma")]
        app_state = AppState()
        note_list = _build_note_list(notes, app_state)

        app_state.props.query = "alpha"  # schedules one refresh
        pending = note_list._pending_refresh_id
        self.assertIsNotNone(pending)

        # Flush directly (as the timer would). It must re-read the
        # *current* query rather than any captured snapshot.
        result = note_list._flush_pending_refresh()
        self.assertFalse(result)  # GLib.SOURCE_REMOVE
        self.assertIsNone(note_list._pending_refresh_id)
        self.assertEqual(note_list._count_label.get_text(), "1 notes")

        # The manual flush bypassed GLib's return-value handling, so the
        # one-shot source is still registered; drop it explicitly.
        GLib.source_remove(pending)


if __name__ == "__main__":
    unittest.main()

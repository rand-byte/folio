"""Tests for :mod:`giruntime.ui.application`.

Covers the window-lifetime policy (:meth:`NotesApplication.
_on_main_window_close_request`, the seam that stops a hide-on-close
help window from holding the process open after the main window is
gone), the session-state save it now also performs
(:meth:`NotesApplication._save_session_state`), and the restored →
welcome → newest → none initial-selection fallback chain
(:meth:`NotesApplication._select_initial_note`).

Everything here is checked without registering or running the
application: GTK supports only one *registered* ``GtkApplication`` per
process (see the shared ``_test_application`` in
:mod:`giruntime.ui.test_main_window`), so the application is built but
never registered, and ``quit`` is spied on rather than invoked. No GDK
display is required — window geometry is supplied by a minimal
duck-typed fake (:class:`_FakeMainWindow`) rather than a real
:class:`MainWindow`, since none of these tests need actual widgets.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gi.repository import Gdk, Gtk

from config.defaults import SEED_WELCOME_NOTE_ID
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.application import (
    _APPLICATION_ID,
    NotesApplication,
    _register_application_icon_resources,
)
from models.note import Note
from storage.session_state_store import SessionStateStore


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for icon-theme lookups."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


_EARLIER: datetime = datetime(2026, 1, 1, tzinfo=UTC)
_LATER: datetime = datetime(2026, 6, 1, tzinfo=UTC)


def _make_note(
    note_id: str,
    title: str,
    *,
    modified_at: datetime = _LATER,
) -> Note:
    """Build a minimal, fully-formed :class:`Note` for fixture use."""
    return Note(
        id=note_id,
        title=title,
        source=f"= {title}\n\nbody.\n",
        snippet="body.",
        tags=(),
        created_at=modified_at,
        modified_at=modified_at,
    )


class _FakeNoteRepository:
    """Minimal :class:`NoteRepositoryProtocol` fixture — only
    ``list_all`` is exercised by :meth:`NoteListStore.load`, called
    once per test to build the in-memory store these tests read.
    """

    notes: dict[str, Note]

    def __init__(self, notes: dict[str, Note]) -> None:
        self.notes = notes

    def get(self, note_id: str) -> Note:
        return self.notes[note_id]

    def list_modified_since(self, _since: datetime) -> list[Note]:
        raise NotImplementedError

    def list_all(self) -> list[Note]:
        return list(self.notes.values())

    def search(self, _query: str) -> list[Note]:
        raise NotImplementedError

    def insert(self, note: Note) -> Note:
        raise NotImplementedError

    def update_source(
        self,
        note_id: str,
        source: str,
        modified_at: datetime,
    ) -> Note:
        raise NotImplementedError

    def delete(self, note_id: str) -> None:
        raise NotImplementedError

    def list_tags(self) -> tuple[tuple[str, int], ...]:
        return ()


class _FakeMainWindow:
    """Duck-typed stand-in for :class:`MainWindow`.

    ``_save_session_state`` calls :meth:`Gtk.Window.get_default_size`
    and :meth:`Gtk.Window.is_maximized`; ``_on_main_window_close_request``
    additionally calls :meth:`MainWindow.flush_editor`. This fixture
    supplies exactly those three, with no GTK dependency, so both the
    session-save and the close-flush paths are testable without a
    display. The optional ``on_flush`` hook lets a test observe the flush
    (and its ordering relative to ``quit``); it defaults to a no-op so the
    geometry-only tests need not supply it.
    """

    _width: int
    _height: int
    _maximized: bool
    _on_flush: Callable[[], None]

    def __init__(
        self,
        *,
        width: int,
        height: int,
        maximized: bool,
        on_flush: Callable[[], None] = lambda: None,
    ) -> None:
        self._width = width
        self._height = height
        self._maximized = maximized
        self._on_flush = on_flush

    def get_default_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    def is_maximized(self) -> bool:
        return self._maximized

    def flush_editor(self) -> None:
        self._on_flush()


def _store_from(notes: dict[str, Note]) -> NoteListStore:
    """Build a loaded :class:`NoteListStore` over a fixed note set.

    Dict insertion order stands in for the repository's
    ``modified_at DESC`` ordering contract — callers that care about
    "the newest note" order the fixture dict newest-first themselves,
    the same way :class:`_FakeNoteRepository` is used in
    :mod:`giruntime.ui.test_main_window`.
    """
    store = NoteListStore(repository=_FakeNoteRepository(notes))
    store.load()
    return store


def _application_with_temp_session_store(
    test_case: unittest.TestCase,
) -> tuple[NotesApplication, SessionStateStore]:
    """A :class:`NotesApplication` with just enough state populated to
    exercise ``_save_session_state`` / ``_on_main_window_close_request``
    without running :meth:`NotesApplication._initialise_runtime` (which
    needs a real database and a display for CSS/icon loading)."""
    application = NotesApplication()
    application._app_state = AppState()
    # ``consider-using-with`` is appropriate when the temp dir's lifetime
    # is the surrounding scope. Here it must outlive this function (the
    # returned store keeps reading/writing it), so cleanup is deferred to
    # the TestCase via addCleanup instead — matching the same tradeoff
    # documented on storage/test_attachment_store.py's _TempAttachmentDir.
    # pylint: disable-next=consider-using-with
    tmp_dir = TemporaryDirectory()
    test_case.addCleanup(tmp_dir.cleanup)
    store = SessionStateStore(Path(tmp_dir.name) / "state.json")
    application._session_state_store = store
    return application, store


class MainWindowCloseRequestTests(unittest.TestCase):
    """Closing the primary window saves session state, then ends the
    application."""

    def test_close_request_quits_the_application(self) -> None:
        """The handler asks the application to quit exactly once.

        This is the fix: without it, a hidden hide-on-close help window
        keeps the application's main loop running after the main window
        closes, and the process hangs.
        """
        application, _store = _application_with_temp_session_store(self)
        window = _FakeMainWindow(width=1000, height=700, maximized=False)

        with patch.object(application, "quit") as quit_spy:
            application._on_main_window_close_request(window)

        quit_spy.assert_called_once_with()

    def test_close_request_does_not_veto_the_close(self) -> None:
        """The handler returns falsey so GTK still destroys the window.

        A truthy return from a ``close-request`` handler vetoes the close;
        the main window would then stay open. The lifetime handler must
        let the default close proceed.
        """
        application, _store = _application_with_temp_session_store(self)
        window = _FakeMainWindow(width=1000, height=700, maximized=False)

        with patch.object(application, "quit"):
            proceed = application._on_main_window_close_request(window)

        self.assertFalse(proceed)

    def test_close_request_flushes_editor_before_quitting(self) -> None:
        """The handler flushes the editor's pending autosave, before quit.

        The editor debounces saves, so a save may still be pending when
        the window closes; without a flush those just-typed edits are
        lost. ``quit`` tears the window (and its editor) down, so the
        flush must run first — the recorded order pins both facts.
        """
        application, _store = _application_with_temp_session_store(self)
        events: list[str] = []
        window = _FakeMainWindow(
            width=1000,
            height=700,
            maximized=False,
            on_flush=lambda: events.append("flush"),
        )

        with patch.object(
            application, "quit", side_effect=lambda: events.append("quit")
        ):
            application._on_main_window_close_request(window)

        self.assertEqual(events, ["flush", "quit"])

    def test_close_request_saves_session_state_before_quitting(self) -> None:
        application, store = _application_with_temp_session_store(self)
        assert application._app_state is not None
        application._app_state.set_selected_note_id("n1")
        window = _FakeMainWindow(width=1400, height=900, maximized=True)

        with patch.object(application, "quit"):
            application._on_main_window_close_request(window)

        saved = store.load()
        self.assertEqual(saved.selected_note_id, "n1")
        self.assertEqual(saved.window_size, (1400, 900))
        self.assertTrue(saved.window_maximized)


class SaveSessionStateTests(unittest.TestCase):
    """:meth:`NotesApplication._save_session_state` persists the
    current selection and window geometry."""

    def test_saves_the_selected_note_id(self) -> None:
        application, store = _application_with_temp_session_store(self)
        assert application._app_state is not None
        application._app_state.set_selected_note_id("n1")
        window = _FakeMainWindow(width=1000, height=700, maximized=False)

        application._save_session_state(window)

        self.assertEqual(store.load().selected_note_id, "n1")

    def test_saves_none_when_nothing_is_selected(self) -> None:
        application, store = _application_with_temp_session_store(self)
        window = _FakeMainWindow(width=1000, height=700, maximized=False)

        application._save_session_state(window)

        self.assertIsNone(store.load().selected_note_id)

    def test_saves_window_size_and_maximized_state(self) -> None:
        application, store = _application_with_temp_session_store(self)
        window = _FakeMainWindow(width=1600, height=1000, maximized=True)

        application._save_session_state(window)

        saved = store.load()
        self.assertEqual(saved.window_size, (1600, 1000))
        self.assertTrue(saved.window_maximized)

    def test_degenerate_zero_size_is_not_persisted(self) -> None:
        # get_default_size() == (0, 0) is GTK's documented "no explicit
        # size was ever set" signal; every real MainWindow always calls
        # set_default_size during construction, so this only guards a
        # degenerate case.
        application, store = _application_with_temp_session_store(self)
        window = _FakeMainWindow(width=0, height=0, maximized=False)

        application._save_session_state(window)

        self.assertIsNone(store.load().window_size)


class SelectInitialNoteTests(unittest.TestCase):
    """:meth:`NotesApplication._select_initial_note`'s restored →
    welcome → newest → none fallback chain."""

    def test_prefers_restored_note_id_over_welcome(self) -> None:
        welcome = _make_note(SEED_WELCOME_NOTE_ID, "Welcome")
        other = _make_note("n1", "Other")
        store = _store_from({welcome.id: welcome, other.id: other})
        app_state = AppState()

        NotesApplication._select_initial_note(store, app_state, "n1")

        self.assertEqual(app_state.selected_note_id, "n1")

    def test_falls_back_to_welcome_when_restored_id_unknown(self) -> None:
        welcome = _make_note(SEED_WELCOME_NOTE_ID, "Welcome")
        store = _store_from({welcome.id: welcome})
        app_state = AppState()

        NotesApplication._select_initial_note(store, app_state, "gone")

        self.assertEqual(app_state.selected_note_id, welcome.id)

    def test_falls_back_to_welcome_when_no_restored_id(self) -> None:
        # No prior run (first launch, or a state file that failed to
        # parse) — same welcome/newest/none chain as before this
        # feature existed.
        welcome = _make_note(SEED_WELCOME_NOTE_ID, "Welcome")
        store = _store_from({welcome.id: welcome})
        app_state = AppState()

        NotesApplication._select_initial_note(store, app_state, None)

        self.assertEqual(app_state.selected_note_id, welcome.id)

    def test_reset_database_reseeds_welcome_and_is_selected(self) -> None:
        # A "reset" (deleted/recreated) database re-runs the v1
        # migration from scratch, which unconditionally re-seeds the
        # welcome note at the stable SEED_WELCOME_NOTE_ID — modelled
        # here as a store that has the welcome note but not whatever
        # id was saved from before the reset.
        welcome = _make_note(SEED_WELCOME_NOTE_ID, "Welcome")
        store = _store_from({welcome.id: welcome})
        app_state = AppState()

        NotesApplication._select_initial_note(
            store,
            app_state,
            "note-from-before-the-reset",
        )

        self.assertEqual(app_state.selected_note_id, welcome.id)

    def test_falls_back_to_newest_when_welcome_and_restored_missing(
        self,
    ) -> None:
        newer = _make_note("n2", "Newer", modified_at=_LATER)
        older = _make_note("n1", "Older", modified_at=_EARLIER)
        # Newest-first, matching the repository's modified_at DESC
        # ordering contract that NoteListStore.load() relies on.
        store = _store_from({newer.id: newer, older.id: older})
        app_state = AppState()

        NotesApplication._select_initial_note(store, app_state, None)

        self.assertEqual(app_state.selected_note_id, "n2")

    def test_no_notes_at_all_leaves_selection_none(self) -> None:
        store = _store_from({})
        app_state = AppState()

        NotesApplication._select_initial_note(store, app_state, None)

        self.assertIsNone(app_state.selected_note_id)


@unittest.skipUnless(_display_available(), "no GDK display")
class RegisterApplicationIconResourcesTests(unittest.TestCase):
    """The bundled icon resolves by name once registration has run."""

    def test_icon_theme_gains_the_application_icon(self) -> None:
        """The gresource-bundled SVG becomes a resolvable icon name.

        Without :func:`_register_application_icon_resources`, the
        icon theme has never been told about ``folio.gresource``'s
        ``/org/folio/icons`` subtree, so it would not resolve
        :data:`_APPLICATION_ID` by name.
        """
        display = Gdk.Display.get_default()
        assert display is not None  # narrows for the type checker

        _register_application_icon_resources()

        icon_theme = Gtk.IconTheme.get_for_display(display)
        self.assertTrue(icon_theme.has_icon(_APPLICATION_ID))

    def test_sets_the_default_window_icon_name(self) -> None:
        """Every window without its own icon falls back to the app icon."""
        _register_application_icon_resources()

        self.assertEqual(
            Gtk.Window.get_default_icon_name(),
            _APPLICATION_ID,
        )


if __name__ == "__main__":
    unittest.main()

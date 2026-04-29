"""The :class:`Gtk.Application` subclass — wires everything together.

Principles & invariants
-----------------------
* :class:`NotesApplication` is the single point in the program that
  composes the layered architecture into a runnable system. It owns
  the :class:`Database`, the repositories, and the :class:`AppState`;
  every other module receives those as parameters. No other module
  reaches for a global "the database" or "the app state" — composition
  flows top-down from here.
* :meth:`do_activate` is the only :class:`Gtk.Application` vfunc this
  class overrides. The application is registered as a
  :class:`Gio.ApplicationFlags.FLAGS_NONE` (single-instance) app; a
  second ``python -m notes_app`` while one is already running raises
  the existing window rather than opening a new one. That matches the
  design's single-window assumption.
* Long-lived resources (the :class:`Database`, the repositories,
  :class:`AppState`) are initialised once on the first activation and
  reused on every subsequent activation. The migration runner is
  invoked exactly once per process lifetime; it is itself idempotent,
  but skipping the work avoids redundant version reads on activations
  past the first.
* The seeded welcome note is loaded by id (:data:`SEED_WELCOME_NOTE_ID`).
  If the user has deleted it, the application falls back to the most
  recently modified note in the library (:meth:`NoteRepositoryProtocol.list_all`
  is sorted by ``modified_at DESC``). If no notes exist at all, the
  selection stays ``None`` and the right pane renders empty — exactly
  the policy :class:`NoteView.refresh` already implements.
* Database errors during activation surface through
  :class:`sqlite3.DatabaseError`; we let them propagate. A failure to
  open the database is fatal for v1 (no data → nothing to show) and
  GTK 4 turns an unhandled exception in :meth:`do_activate` into a
  process-level crash with the original traceback. That is the right
  failure mode at this layer — better than swallowing the error and
  showing an empty window with no explanation.
"""

from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gio, Gtk  # noqa: E402

from notes_app.config.defaults import SEED_WELCOME_NOTE_ID
from notes_app.config.paths import database_path
from notes_app.controllers.app_state import AppState
from notes_app.storage.database import Database
from notes_app.storage.migrations import apply_pending
from notes_app.storage.note_repository import NoteRepository
from notes_app.storage.notebook_repository import NotebookRepository
from notes_app.ui.main_window import MainWindow


_APPLICATION_ID: str = "org.notes_app.NotesApp"
"""Reverse-DNS-shaped identifier registered with the session bus.

GTK uses this to enforce single-instance behaviour and to name the
application's resource bundles. The string is fixed across releases —
changing it would orphan any per-application user settings the OS may
record under it.
"""


class NotesApplication(Gtk.Application):
    """The application's :class:`Gtk.Application` subclass.

    Holds the long-lived dependencies — database, repositories, app
    state — and presents a :class:`MainWindow` on activation.
    """

    _database: Database | None
    _note_repository: NoteRepository | None
    _notebook_repository: NotebookRepository | None
    _app_state: AppState | None

    def __init__(self) -> None:
        super().__init__(
            application_id=_APPLICATION_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._database = None
        self._note_repository = None
        self._notebook_repository = None
        self._app_state = None

    def do_activate(self) -> None:  # pylint: disable=arguments-differ
        """Build the world if it does not yet exist, then present a
        window.

        Called by GTK on the first ``run()`` and on every subsequent
        activation request (e.g. a user re-launching the app's
        ``.desktop`` entry while it is already running). The
        single-instance flag keeps that re-launch from spawning a
        second process, but the activation itself fires every time.
        """
        if self._database is None:
            self._initialise_runtime()

        # Reuse the existing window when one is already open
        # (subsequent activations) — otherwise build the first one.
        window = self.get_active_window()
        if window is None:
            window = self._build_initial_window()
        window.present()

    def _initialise_runtime(self) -> None:
        """Open the database, run migrations, build repositories and
        the app state.

        Runs exactly once per process. The database path is resolved
        through :func:`database_path`, which honours
        ``XDG_DATA_HOME``. Migrations are idempotent on a current
        database, so re-invocation across activations would still be
        safe — but skipping the call avoids the redundant version
        read.
        """
        self._database = Database(database_path())
        apply_pending(self._database)
        self._note_repository = NoteRepository(self._database)
        self._notebook_repository = NotebookRepository(self._database)
        self._app_state = AppState()

    def _build_initial_window(self) -> MainWindow:
        """Construct the first :class:`MainWindow` and seed the
        selection.

        After construction, the welcome note is selected if it is
        still in the library; otherwise the most recently modified
        note is selected; otherwise nothing is selected (the right
        pane renders empty). Selection happens *after* window
        construction so that the :class:`NoteView`'s
        ``selected-note-changed`` handler is already wired by the
        time the signal fires.
        """
        # Local non-None aliases — narrows ``Optional`` for the type
        # checker and documents the precondition that
        # :meth:`_initialise_runtime` ran first.
        assert self._note_repository is not None
        assert self._notebook_repository is not None
        assert self._app_state is not None

        window = MainWindow(
            application=self,
            note_repository=self._note_repository,
            notebook_repository=self._notebook_repository,
            app_state=self._app_state,
        )
        self._select_initial_note(self._note_repository, self._app_state)
        return window

    @staticmethod
    def _select_initial_note(
        repository: NoteRepository,
        app_state: AppState,
    ) -> None:
        """Pick the welcome note, or fall back to the newest note.

        The two-step fallback (welcome → newest → none) is what keeps
        the first-launch experience predictable while not breaking the
        app for a user who has cleaned out their library. The fallback
        chain is documented at the module level — keep them in sync.
        """
        try:
            welcome = repository.get(SEED_WELCOME_NOTE_ID)
        except KeyError:
            welcome = None

        if welcome is not None:
            app_state.set_selected_note_id(welcome.id)
            return

        # ``list_all`` is sorted by ``modified_at DESC`` — the first
        # entry is the most recently touched note.
        all_notes = repository.list_all()
        if all_notes:
            app_state.set_selected_note_id(all_notes[0].id)
            return
        # No notes at all — leave the selection empty. ``NoteView``
        # renders an empty buffer in that case.

"""Top-level application window — step-8 stub.

Principles & invariants
-----------------------
* :class:`MainWindow` is the application's single top-level window. The
  step-8 form is deliberately minimal: a :class:`Gtk.ApplicationWindow`
  wrapping a single :class:`NoteView`, just enough for the smoke-
  launchable build to display the seeded welcome note. The full three-
  pane shell (sidebar + note list + note view + toolbar + status bar)
  arrives at build step 9 and rewrites this module.
* Even in the stub form, the construction signature already follows the
  long-term shape: storage is injected by protocol
  (:class:`NoteRepositoryProtocol`,
  :class:`NotebookRepositoryProtocol`); :class:`AppState` is also
  injected. This means step 9 can extend the body without touching any
  caller — :class:`NotesApplication` already passes everything the full
  shell will need.
* The window owns no data of its own. Every datum it needs is reached
  through a ``Protocol`` reference or via signals on
  :class:`AppState`. This is the property that lets future tests use
  the same fakes the controller tests already rely on.
* Even for a stub, the window must request a sensible default size so
  the wide-window branch of :class:`ArticleContainer.do_size_allocate`
  has slack to absorb on the first allocation. Without an explicit
  request a freshly-mapped GTK 4 window is allocated at its child's
  natural size — for us that would be exactly
  :meth:`ArticleContainer.target_column_width`, which would never
  exercise the margin-absorbing branch on screen.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.storage.protocols import (
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)
from notes_app.ui.note_view import NoteView


# Sized for the design's three-pane layout that step 9 will introduce —
# generous enough today that the wide-window margin branch always
# fires on a fresh launch, narrow enough that the first allocation
# fits on a 1366×768 laptop.
_DEFAULT_WIDTH_PX: int = 1200
_DEFAULT_HEIGHT_PX: int = 800

_WINDOW_TITLE: str = "Notes"
"""Window title shown in the title bar / Wayland compositor.

Replaced (or augmented with the current note's title) when the toolbar
arrives at step 12 — kept here as a constant rather than a magic
string so the search for "places that affect the title" is one grep
target.
"""


class MainWindow(Gtk.ApplicationWindow):
    """The application's single top-level window.

    Construction wires the injected dependencies straight through to
    the :class:`NoteView` child. Future build steps will replace the
    single child with a paned layout (sidebar | note list | note view)
    and a toolbar above it; the constructor signature is already the
    full long-term shape so those steps add fields without changing
    callers.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _app_state: AppState
    _note_view: NoteView

    def __init__(
        self,
        *,
        application: Gtk.Application,
        note_repository: NoteRepositoryProtocol,
        notebook_repository: NotebookRepositoryProtocol,
        app_state: AppState,
    ) -> None:
        super().__init__(application=application)
        self._note_repository = note_repository
        # Held for step 9, where the sidebar lands. Storing it now
        # keeps the constructor surface stable across the rewrite.
        self._notebook_repository = notebook_repository
        self._app_state = app_state

        self.set_title(_WINDOW_TITLE)
        self.set_default_size(_DEFAULT_WIDTH_PX, _DEFAULT_HEIGHT_PX)

        self._note_view = NoteView(
            note_repository=note_repository,
            app_state=app_state,
        )
        self.set_child(self._note_view)

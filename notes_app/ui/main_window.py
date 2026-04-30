"""Top-level application window — the three-pane shell.

Principles & invariants
-----------------------
* :class:`MainWindow` is the application's single top-level window. As
  of build step 9 it is the three-pane shell from the design:
  :class:`Sidebar` on the left, :class:`NoteList` in the middle, and
  :class:`NoteView` on the right. The toolbar (search entry, view-mode
  switch, breadcrumb, More menu) lands at step 12; the status bar
  belongs to a later UI polish pass and is not in scope today.
* Layout is a pair of nested :class:`Gtk.Paned` widgets, both
  horizontal. The outer paned splits *sidebar | rest*; its end-child
  is the inner paned, which splits *note list | note view*. Two
  nested panes give the user two independent drag handles — exactly
  the design's behaviour. :meth:`Gtk.Paned.set_start_child` and
  :meth:`Gtk.Paned.set_end_child` are the GTK 4 way to populate a
  paned; the older ``pack1`` / ``pack2`` API was deprecated in 4.0.
* The window does not subscribe to any :class:`AppState` signals
  itself. The three panes do — the window is purely a layout
  concern, owning no behaviour beyond the paned positions and the
  set of injected dependencies it threads down. This keeps the
  shell's surface trivial: future steps that change *what* the
  panes do (toolbar, status bar) extend the window's child set, but
  leave its signal subscriptions exactly empty.
* The construction signature is unchanged from step 8: caller
  (:class:`NotesApplication`) passes ``application``,
  ``note_repository``, ``notebook_repository``, and ``app_state``,
  all keyword-only. This is the property that lets step 9 land
  without touching the application module.
* Default sizes for the panes match the per-widget hints
  (:data:`_SIDEBAR_INITIAL_POSITION_PX`,
  :data:`_NOTE_LIST_INITIAL_POSITION_PX`). They are *initial*
  positions only; once the user drags either handle GTK records the
  new value internally and our defaults stop applying. Saving and
  restoring those positions across launches is a v2 feature — there
  is no settings store in v1 (decision 4 of the plan).
* The window owns no data. Everything it needs — repositories, app
  state — is reached through references that originate in
  :class:`NotesApplication`. Tests can construct the same widget
  with the same fake repositories the per-pane tests already use,
  without touching the file system or the database.
"""

from __future__ import annotations

from typing import Final

import gi

gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.storage.protocols import (
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)
from notes_app.ui.note_list import NoteList
from notes_app.ui.note_view import NoteView
from notes_app.ui.sidebar import Sidebar


# ---------------------------------------------------------------------------
# Constants — window and paned-position defaults
# ---------------------------------------------------------------------------


_DEFAULT_WINDOW_WIDTH_PX: Final[int] = 1200
"""Initial window width.

Wide enough that the rendered article column gets its full
:data:`TARGET_CHARS_PER_LINE` × char-width allocation with slack on
both sides of the article column, so the wide-window margin branch
of :meth:`ArticleContainer.do_size_allocate` fires on the first
allocation. Narrow enough to fit a 1366×768 laptop comfortably.
"""

_DEFAULT_WINDOW_HEIGHT_PX: Final[int] = 800
"""Initial window height. Matches the design's roomy default."""

_SIDEBAR_INITIAL_POSITION_PX: Final[int] = 220
"""Initial position of the outer paned divider.

Equals the sidebar's preferred width hint
(:data:`notes_app.ui.sidebar._DEFAULT_PANE_WIDTH_PX`). Setting both
to the same value avoids a momentary re-layout on first show: the
paned starts where the sidebar wants to be.
"""

_NOTE_LIST_INITIAL_POSITION_PX: Final[int] = 320
"""Initial position of the inner paned divider.

Same reasoning as the sidebar — matches
:data:`notes_app.ui.note_list._DEFAULT_PANE_WIDTH_PX`.
"""

_WINDOW_TITLE: Final[str] = "Notes"
"""Window title shown in the title bar / Wayland compositor.

Augmented with the current note's title once the toolbar arrives at
step 12; kept as a constant rather than a magic string so the search
for "places that affect the title" is one grep target.
"""


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


class MainWindow(Gtk.ApplicationWindow):
    """The application's single top-level window.

    Composes :class:`Sidebar`, :class:`NoteList`, and
    :class:`NoteView` into the three-pane shell. The construction
    signature is the long-term one; future build steps add toolbar /
    status-bar children without changing it.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _app_state: AppState
    _sidebar: Sidebar
    _note_list: NoteList
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
        self._notebook_repository = notebook_repository
        self._app_state = app_state

        self.set_title(_WINDOW_TITLE)
        self.set_default_size(
            _DEFAULT_WINDOW_WIDTH_PX,
            _DEFAULT_WINDOW_HEIGHT_PX,
        )

        # Build the three panes. Each subscribes to AppState itself;
        # the window does not arbitrate between them.
        self._sidebar = Sidebar(
            note_repository=note_repository,
            notebook_repository=notebook_repository,
            app_state=app_state,
        )
        self._note_list = NoteList(
            note_repository=note_repository,
            notebook_repository=notebook_repository,
            app_state=app_state,
        )
        self._note_view = NoteView(
            note_repository=note_repository,
            app_state=app_state,
        )

        # Inner paned: note list | note view.
        inner_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        inner_paned.set_start_child(self._note_list)
        inner_paned.set_end_child(self._note_view)
        inner_paned.set_position(_NOTE_LIST_INITIAL_POSITION_PX)
        # The note view should keep its width when the user drags
        # the *outer* divider — only the note list shrinks /
        # expands. Achieve that by letting the start child resize
        # but not shrink below its preferred minimum, and the end
        # child resize freely.
        inner_paned.set_resize_start_child(False)
        inner_paned.set_resize_end_child(True)
        inner_paned.set_shrink_start_child(False)
        inner_paned.set_shrink_end_child(False)

        # Outer paned: sidebar | (note list + note view).
        outer_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        outer_paned.set_start_child(self._sidebar)
        outer_paned.set_end_child(inner_paned)
        outer_paned.set_position(_SIDEBAR_INITIAL_POSITION_PX)
        # Sidebar holds its width; the inner paned absorbs window
        # resizes.
        outer_paned.set_resize_start_child(False)
        outer_paned.set_resize_end_child(True)
        outer_paned.set_shrink_start_child(False)
        outer_paned.set_shrink_end_child(False)

        self.set_child(outer_paned)

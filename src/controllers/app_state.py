"""Mutable application state shared across widgets.

Principles & invariants
-----------------------
* :class:`AppState` is the single in-memory source of truth for the four
  pieces of *navigational* state that every widget needs to read: the
  current sidebar :data:`Selection`, the id of the note currently being
  shown, the rendered/source :class:`ViewMode`, and the live search
  query. No widget reads any of these from another widget — every widget
  reads them here. Mutations go through methods that emit GObject
  signals so widgets can subscribe without holding direct references to
  one another.
* The class deliberately holds **only** navigational state. Domain data
  (the list of notes, the notebook tree) is not mirrored here — those
  belong in the repositories and are pulled fresh by widgets when a
  controller emits ``notes-changed`` or ``notebooks-changed``. This
  keeps the in-memory state small and removes the synchronisation
  problem that comes with a parallel cache.
* Ephemeral UI state (toasts, dialog visibility, menu position) is also
  intentionally absent. Toasts are transient notifications emitted by
  the controllers that produced them; dialogs are owned by the widgets
  that open them. Mixing those into a "single source of truth" would
  blur the controller / widget split and make a clean signal taxonomy
  impossible.
* This module imports :mod:`gi` because :class:`GObject.Object` is the
  signal substrate the rest of the application relies on. GObject is
  not GTK — it is part of GLib — and the import works in headless
  environments, which is what makes the controller tests runnable on
  CI without a display server.
* Each setter compares the proposed value against the current one and
  emits its signal **only on a real change**. This matches the React
  ``useState`` semantics that the design (``app.jsx``) ran on, and it
  keeps signal handlers from being called for no-ops.
* Setting the selection does **not** clear ``selected_note_id``. The
  React reference (``app.jsx``) keeps the selected id across selection
  changes and lets the note-list widget auto-correct when the id is
  not present in the freshly filtered list. Replicating that here is a
  UI-layer concern; this class merely carries the state.
* Signals are payload-free. Listeners pull the new value by reading
  the property they care about. This avoids the trap of two channels
  of truth — the property and the signal payload — drifting apart.
"""

from __future__ import annotations

import gi

gi.require_version("GObject", "2.0")
# pylint: disable=wrong-import-position
from gi.repository import GObject  # noqa: E402

from enums import SmartFilter, ViewMode
from search.note_filter import (
    Selection,
    SmartSelection,
)


class AppState(GObject.Object):
    """Navigational state plus signals announcing changes to it.

    Signals
    -------
    selection-changed
        Emitted after :meth:`set_selection` accepts a new value. Read
        the new selection from :attr:`selection`.
    selected-note-changed
        Emitted after :meth:`set_selected_note_id` accepts a new value.
        Read the new id (possibly ``None``) from
        :attr:`selected_note_id`.
    view-mode-changed
        Emitted after :meth:`set_view_mode` accepts a new value. Read
        the new mode from :attr:`view_mode`.
    query-changed
        Emitted after :meth:`set_query` accepts a new value. Read the
        new (already-stripped of nothing — the query is stored
        verbatim) string from :attr:`query`.
    """

    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "selected-note-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "view-mode-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "query-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    _selection: Selection
    _selected_note_id: str | None
    _view_mode: ViewMode
    _query: str

    def __init__(
        self,
        *,
        initial_selection: Selection | None = None,
        initial_view_mode: ViewMode = ViewMode.VIEW,
    ) -> None:
        """Construct the state with sensible navigational defaults.

        ``initial_selection`` defaults to *All notes* (the smart filter
        :data:`SmartFilter.ALL`) — that's the design's default starting
        point and the only choice that is meaningful before the
        notebook tree has been loaded. ``initial_view_mode`` defaults
        to :data:`ViewMode.VIEW` because the app boots straight into
        rendered prose, never the editor.
        """
        super().__init__()
        if initial_selection is None:
            initial_selection = SmartSelection(smart_filter=SmartFilter.ALL)
        self._selection = initial_selection
        self._selected_note_id = None
        self._view_mode = initial_view_mode
        self._query = ""

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def selection(self) -> Selection:
        return self._selection

    @property
    def selected_note_id(self) -> str | None:
        return self._selected_note_id

    @property
    def view_mode(self) -> ViewMode:
        return self._view_mode

    @property
    def query(self) -> str:
        return self._query

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_selection(self, selection: Selection) -> None:
        """Replace the current sidebar selection.

        No-op when the new value equals the current one. Does not
        touch :attr:`selected_note_id`; auto-correction when the
        currently selected note is not in the new filtered list is the
        note-list widget's responsibility.
        """
        if selection == self._selection:
            return
        self._selection = selection
        self.emit("selection-changed")

    def set_selected_note_id(self, note_id: str | None) -> None:
        """Replace the id of the currently displayed note.

        ``None`` is a valid value — it means "no note is currently
        displayed", which the right-hand pane renders as an empty
        state. No-op when the new value equals the current one.
        """
        if note_id == self._selected_note_id:
            return
        self._selected_note_id = note_id
        self.emit("selected-note-changed")

    def set_view_mode(self, view_mode: ViewMode) -> None:
        """Switch between the rendered view and the source editor.

        No-op when the new mode equals the current one.
        """
        if view_mode == self._view_mode:
            return
        self._view_mode = view_mode
        self.emit("view-mode-changed")

    def set_query(self, query: str) -> None:
        """Update the live global-search query.

        The query is stored verbatim, including leading and trailing
        whitespace. :func:`search.note_filter.filter_by_query`
        is the place that strips and case-folds — keeping that
        normalisation in one place avoids divergence with the SQL
        ``LIKE`` query that runs against the database. No-op when the
        new value equals the current one.
        """
        if query == self._query:
            return
        self._query = query
        self.emit("query-changed")

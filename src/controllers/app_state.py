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
* The selection state has only two mutators — :meth:`set_smart` and
  :meth:`toggle_tag` — never a generic ``set_selection``. The
  controller owns the rules for how selections move between the
  ``SmartSelection`` and ``TagSelection`` variants:

  - :meth:`set_smart` always replaces the current selection with
    ``SmartSelection(filter)``. Picking a smart filter (``All notes``
    or ``Untagged``) wipes any tag selection.
  - :meth:`toggle_tag` is XOR over a single tag. Starting from a
    smart selection, toggling tag ``t`` produces ``TagSelection({t})``.
    Starting from a tag selection containing ``t``, toggling ``t``
    again removes it; the result is ``TagSelection`` over the
    remaining tags if any, else ``SmartSelection(ALL)`` (the empty
    tag set is *not* a valid :class:`TagSelection`).

  This means the *Untagged* smart filter and any tag selection are
  always mutually exclusive — there is no representable "Untagged
  AND tag X" state.

* The class deliberately holds **only** navigational state. Domain data
  (the list of notes, the live tag list) is not mirrored here — those
  belong in the repository and are pulled fresh by widgets when a
  controller emits ``notes-changed``. This keeps the in-memory state
  small and removes the synchronisation problem that comes with a
  parallel cache.
* Ephemeral UI state (toasts, dialog visibility, menu position) is also
  intentionally absent. Toasts are transient notifications emitted by
  the controllers that produced them; dialogs are owned by the widgets
  that open them.
* This module imports :mod:`gi` because :class:`GObject.Object` is the
  signal substrate the rest of the application relies on. GObject is
  not GTK — it is part of GLib — and the import works in headless
  environments, which is what makes the controller tests runnable on
  CI without a display server.
* Each setter compares the proposed value against the current one and
  emits its signal **only on a real change**.
* Setting the selection does **not** clear ``selected_note_id``. The
  note-list widget auto-corrects when the currently-selected id is not
  present in the freshly filtered list.
* Signals are payload-free. Listeners pull the new value by reading
  the property they care about.
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
    TagSelection,
)


class AppState(GObject.Object):
    """Navigational state plus signals announcing changes to it.

    Signals
    -------
    selection-changed
        Emitted after :meth:`set_smart` or :meth:`toggle_tag` produces a
        new value. Read the new selection from :attr:`selection`.
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
        """Construct the state with sensible navigational defaults."""
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
    # Selection mutators
    # ------------------------------------------------------------------

    def set_smart(self, smart_filter: SmartFilter) -> None:
        """Replace the current selection with ``SmartSelection(smart_filter)``.

        Wipes any active tag selection. No-op when the current
        selection is already that smart filter.
        """
        target: Selection = SmartSelection(smart_filter=smart_filter)
        if target == self._selection:
            return
        self._selection = target
        self.emit("selection-changed")

    def toggle_tag(self, name: str) -> None:
        """Toggle ``name`` in the active tag set.

        * From a :class:`SmartSelection`: becomes ``TagSelection({name})``.
        * From a :class:`TagSelection` not containing ``name``: ``name`` is
          added to the set.
        * From a :class:`TagSelection` containing ``name``: ``name`` is
          removed; if the set is then empty the selection reverts to
          ``SmartSelection(SmartFilter.ALL)``.
        """
        current = self._selection
        if isinstance(current, SmartSelection):
            new_tags = frozenset({name})
            self._selection = TagSelection(tags=new_tags)
            self.emit("selection-changed")
            return
        # current is a TagSelection
        if name in current.tags:
            remaining = current.tags - {name}
            if not remaining:
                self._selection = SmartSelection(
                    smart_filter=SmartFilter.ALL,
                )
            else:
                self._selection = TagSelection(tags=remaining)
        else:
            self._selection = TagSelection(tags=current.tags | {name})
        self.emit("selection-changed")

    # ------------------------------------------------------------------
    # Other mutators
    # ------------------------------------------------------------------

    def set_selected_note_id(self, note_id: str | None) -> None:
        """Replace the id of the currently displayed note."""
        if note_id == self._selected_note_id:
            return
        self._selected_note_id = note_id
        self.emit("selected-note-changed")

    def set_view_mode(self, view_mode: ViewMode) -> None:
        """Switch between the rendered view and the source editor."""
        if view_mode == self._view_mode:
            return
        self._view_mode = view_mode
        self.emit("view-mode-changed")

    def set_query(self, query: str) -> None:
        """Update the live global-search query."""
        if query == self._query:
            return
        self._query = query
        self.emit("query-changed")

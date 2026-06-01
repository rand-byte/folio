"""Mutable application state shared across widgets.

Principles & invariants
-----------------------
* :class:`AppState` is the single in-memory source of truth for the four
  pieces of *navigational* state that every widget needs to read: the
  current sidebar :data:`Selection`, the id of the note currently being
  shown, the rendered/source :class:`ViewMode`, and the live search
  query. No widget reads any of these from another widget — every widget
  reads them here.
* State is exposed as **GObject properties** observed via
  ``notify::<prop>``; widgets subscribe to the property-change
  notification rather than to bespoke signals, so they need no direct
  references to one another. The four properties split into two shapes:

  - ``query`` is a **stored read/write** property. It round-trips with
    the toolbar's search entry through a *bidirectional* property
    binding (see :mod:`ui.toolbar`), so it is the one field external
    code may write directly (``self.props.query = ...``).
  - ``selection``, ``selected_note_id`` and ``view_mode`` are
    **read-only** to the outside world: their getters return the
    backing field and the only way to change them is through the
    explicit mutators below, which enforce the rules and then call
    :meth:`GObject.Object.notify`.

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

  This is exactly why these three stay read-only and are **not** bound
  to a widget: a generic bindable setter could not enforce the
  transition rules. It also means the *Untagged* smart filter and any
  tag selection are always mutually exclusive — there is no
  representable "Untagged AND tag X" state.

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
  property/notification substrate the rest of the application relies
  on. GObject is not GTK — it is part of GLib — and the import works in
  headless environments, which is what makes the controller tests
  runnable on CI without a display server.
* The three rule-bearing mutators emit their ``notify`` **only on a
  real change** (each keeps an equality guard). The stored ``query``
  property uses GObject's generic setter, which may notify even when
  the value is unchanged; this is harmless because the bidirectional
  binding suppresses any reverse echo and a re-filter on an identical
  query is idempotent.
* ``query`` must be stored **verbatim** — no stripping, lowercasing, or
  other normalisation. The bidirectional binding's correctness depends
  on the value round-tripping identically with the search entry: if the
  stored value differed from what the entry holds, the binding would
  write the normalised value back and reset the entry's cursor (the
  very reversal bug this design removed). Stripping for matching belongs
  in :func:`search.note_filter.filter_by_query`, not here.
* Setting the selection does **not** clear ``selected_note_id``. The
  note-list widget auto-corrects when the currently-selected id is not
  present in the freshly filtered list.
* Notifications are payload-free beyond the GObject ``ParamSpec``.
  Listeners pull the new value by reading the property they care about.
"""

from __future__ import annotations

from gi.repository import GObject

from enums import SmartFilter, ViewMode
from search.note_filter import (
    Selection,
    SmartSelection,
    TagSelection,
)


class AppState(GObject.Object):
    """Navigational state exposed as observable GObject properties.

    Properties
    ----------
    selection
        The active sidebar :data:`Selection`. Read-only; mutated only
        via :meth:`set_smart` / :meth:`toggle_tag`. Observe
        ``notify::selection``.
    selected-note-id
        The id of the note currently shown, or ``None``. Read-only;
        mutated via :meth:`set_selected_note_id`. Observe
        ``notify::selected-note-id``.
    view-mode
        The rendered/source :class:`ViewMode`. Read-only; mutated via
        :meth:`set_view_mode`. Observe ``notify::view-mode``.
    query
        The live search query, stored verbatim. Read/write — bound
        bidirectionally to the toolbar search entry. Observe
        ``notify::query``.
    """

    query: str = GObject.Property(type=str, default="")

    _selection: Selection
    _selected_note_id: str | None
    _view_mode: ViewMode

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

    # ------------------------------------------------------------------
    # Read-only properties (notify-only; mutated via the methods below)
    # ------------------------------------------------------------------

    @GObject.Property(type=object)
    def selection(self) -> Selection:
        """The active selection. Read-only externally."""
        return self._selection

    @GObject.Property(type=object)
    def selected_note_id(self) -> str | None:
        """The id of the displayed note, or ``None``. Read-only externally."""
        return self._selected_note_id

    @GObject.Property(type=object)
    def view_mode(self) -> ViewMode:
        """The rendered/source view mode. Read-only externally."""
        return self._view_mode

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
        self.notify("selection")

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
            self.notify("selection")
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
        self.notify("selection")

    # ------------------------------------------------------------------
    # Other mutators
    # ------------------------------------------------------------------

    def set_selected_note_id(self, note_id: str | None) -> None:
        """Replace the id of the currently displayed note."""
        if note_id == self._selected_note_id:
            return
        self._selected_note_id = note_id
        self.notify("selected-note-id")

    def set_view_mode(self, view_mode: ViewMode) -> None:
        """Switch between the rendered view and the source editor."""
        if view_mode == self._view_mode:
            return
        self._view_mode = view_mode
        self.notify("view-mode")

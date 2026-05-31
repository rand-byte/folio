"""The library navigation pane on the left of the window.

Principles & invariants
-----------------------
* :class:`Sidebar` is a navigation-only widget. Click handlers translate
  user gestures into mutations of :class:`AppState` (via
  :meth:`AppState.set_smart` and :meth:`AppState.toggle_tag`); widgets
  that depend on the selection (:class:`NoteList`, :class:`NoteView`)
  listen to ``selection-changed`` and pick the change up from there.
  Direct cross-widget references are not held — every shared piece of
  state flows through :class:`AppState`.
* The sidebar is split into two sections:

  - **Library** — a flat :class:`Gtk.ListView` over a
    :class:`Gio.ListStore` of two items (``All notes``, ``Untagged``),
    driven by a :class:`Gtk.SingleSelection`.
  - **Tags** — a flat :class:`Gtk.ListView` over a
    :class:`Gio.ListStore` populated from
    :meth:`NoteRepositoryProtocol.list_tags`. Selection is a
    :class:`Gtk.MultiSelection` because tags AND together.

  There is no notebook tree. The :class:`Gtk.TreeListModel` /
  :class:`Gtk.TreeExpander` machinery (and the matching
  ``treeexpander indent`` CSS rule) is gone with it.

* The Tags-section header reads ``"Tags"`` by default and
  ``f"Tags ({n} selected)"`` when ``n > 0``. The parenthetical carries
  the ``.selection-count`` class for the info-blue accent.
* Selecting any row in the *Library* section clears the *Tags*
  selection (and vice versa) — the controller / app-state owns the
  rule, so both ListViews observe the truth from :class:`AppState`
  rather than coordinating with each other.
* A selected tag row reads as the **theme selection pill** — the same
  highlight the Library list uses. Tag rows carry no special
  selection styling of their own; they fall through to the generic
  ``.sidebar row:selected`` path, so the selected row matches the
  Library list exactly under any theme.
* A plain **single click** on a tag row toggles that tag into / out of
  the selection **additively** — no Shift / Ctrl modifier needed and
  the other selected tags are untouched. A per-row
  :class:`Gtk.GestureClick` claims the click and calls
  :meth:`Sidebar._on_tag_row_clicked`, which uses
  ``select_item(pos, unselect_rest=False)`` / ``unselect_item(pos)``
  on the :class:`Gtk.MultiSelection` instead of GTK's default
  replace-selection. Selected tags still **AND** together, and the
  selection truth still flows through :class:`AppState` (the model's
  ``selection-changed`` is mirrored into it by
  :meth:`_on_tag_selection_changed`).
* Programmatic ``set_selected`` / ``select_item`` calls on a
  :class:`Gtk.SingleSelection` / :class:`Gtk.MultiSelection` emit
  ``selection-changed``, so the highlight-application path is fenced
  behind :attr:`_suppress_selection_events` to avoid loops between
  AppState ⇄ widget.
* :meth:`refresh` rebuilds the tag store from
  :meth:`NoteRepositoryProtocol.list_tags`. The current tag selection
  is snapshotted by tag name across rebuilds: tags that no longer
  exist are dropped from :class:`AppState`'s selection (the AND filter
  contracts accordingly). The Library section's items never change so
  it does not need a snapshot.
* GTK 4.18 / 4.10 deprecations are avoided: model-driven list widgets
  (:class:`Gtk.ListView`, :class:`Gtk.SingleSelection`,
  :class:`Gtk.MultiSelection`) and the GTK 4 idiomatic icon API.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gio, GObject, Gtk, Pango  # noqa: E402

from controllers.app_state import AppState
from enums import SmartFilter
from search.note_filter import SmartSelection, TagSelection
from storage.protocols import NoteRepositoryProtocol


type _TagRowClickHandler = Callable[[int], None]
"""Invoked with a tag row's live model position when the row is
clicked. Passed to the tag-row factory so the factory's per-row click
gesture can call back into the sidebar without holding a widget
reference. The sidebar's handler performs the additive multi-select
toggle against the :class:`Gtk.MultiSelection`."""


# ---------------------------------------------------------------------------
# Constants — labels and icon-name mappings
# ---------------------------------------------------------------------------


_LIBRARY_HEADER_TEXT: Final[str] = "Library"
_TAGS_HEADER_TEXT: Final[str] = "Tags"
_TAGS_HEADER_FORMAT: Final[str] = "Tags"
_TAGS_HEADER_SELECTED_SUFFIX: Final[str] = " ({n} selected)"
"""Suffix appended to the Tags header when one or more tags are
selected. The literal "(N selected)" half carries the
``.selection-count`` CSS class via a nested label."""

_SMART_FILTER_LABELS: Final[dict[SmartFilter, str]] = {
    SmartFilter.ALL: "All notes",
    SmartFilter.UNTAGGED: "Untagged",
}
"""Visible labels for the two library-section rows."""

_SMART_FILTER_DISPLAY_ORDER: Final[tuple[SmartFilter, ...]] = (
    SmartFilter.ALL,
    SmartFilter.UNTAGGED,
)
"""Order the library rows are rendered in."""

_SMART_FILTER_ICON_NAMES: Final[dict[SmartFilter, str]] = {
    SmartFilter.ALL: "view-list-symbolic",
    SmartFilter.UNTAGGED: "tag-symbolic",
}
"""Symbolic-icon names for the library-section rows.

These are FreeDesktop icon names; the active GTK icon theme provides
the actual SVGs. ``tag-symbolic`` doubles as a "no tags" indicator
since the row matches notes with an empty tag set.
"""

_ROW_SPACING_PX: Final[int] = 6
"""Horizontal spacing inside a row, between icon, label, and count."""

_SECTION_VERTICAL_SPACING_PX: Final[int] = 8
"""Padding above section headers (Library / Tags)."""

_DEFAULT_PANE_WIDTH_PX: Final[int] = 220
"""Initial width hint for the sidebar pane."""

_SIDEBAR_CSS_CLASS: Final[str] = "sidebar"
"""Class on the :class:`Sidebar` box that the stylesheet keys off."""

_SECTION_HEADER_CSS_CLASS: Final[str] = "sidebar-section-header"
"""Class on each section-header label (font treatment + dim)."""

_SECTION_COUNT_CSS_CLASS: Final[str] = "selection-count"
"""Class on the ``(N selected)`` half of the Tags header — info-blue
accent driven by ``app.css``."""

_COUNT_CSS_CLASS: Final[str] = "sidebar-count"
"""Class on each row's count label (dimmed when the row is unselected)."""

_TAG_PREFIX: Final[str] = "#"
"""Visible prefix on every tag row's label. Stored once so the
sidebar, the note-list row chips, and the note-view metadata line
agree on the literal."""


# ---------------------------------------------------------------------------
# Row item model object
# ---------------------------------------------------------------------------


class _SmartItem(GObject.Object):
    """A row in the Library section."""

    __gtype_name__ = "NotesSidebarSmartItem"

    icon_name: str
    label: str
    count: int
    smart_filter: SmartFilter

    def __init__(
        self,
        *,
        icon_name: str,
        label: str,
        count: int,
        smart_filter: SmartFilter,
    ) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.label = label
        self.count = count
        self.smart_filter = smart_filter


class _TagItem(GObject.Object):
    """A row in the Tags section."""

    __gtype_name__ = "NotesSidebarTagItem"

    name: str
    count: int

    def __init__(self, *, name: str, count: int) -> None:
        super().__init__()
        self.name = name
        self.count = count


# ---------------------------------------------------------------------------
# Pure counting helpers — testable without GTK
# ---------------------------------------------------------------------------


def count_untagged(notes: Iterable[object]) -> int:
    """Return how many notes have an empty tag tuple.

    Pure helper. Takes any iterable whose elements expose a ``tags``
    attribute (so tests can pass plain stubs). Mirrors the
    :class:`SmartFilter.UNTAGGED` predicate inside
    :func:`search.note_filter.filter_by_selection` so the sidebar
    count agrees with the note-list count by construction.
    """
    untagged = 0
    for note in notes:
        if not getattr(note, "tags", ()):
            untagged += 1
    return untagged


def tags_header_accent_text(selected_count: int) -> str:
    """Return the Tags-header parenthetical, or ``""`` when nothing is
    selected.

    Pure helper — unit-testable without GTK, and the single source of
    the parenthetical wording (which previously regressed to invisible).
    Returns ``""`` for any non-positive count so the caller can use the
    empty string as the "hide the accent label" signal. The leading
    space in :data:`_TAGS_HEADER_SELECTED_SUFFIX` (kept for the
    concatenated form) is dropped here because the accent label is a
    separate widget already spaced by the header box.
    """
    if selected_count <= 0:
        return ""
    return _TAGS_HEADER_SELECTED_SUFFIX.format(n=selected_count).strip()


# ---------------------------------------------------------------------------
# Section header
# ---------------------------------------------------------------------------


def _make_section_header(text: str) -> Gtk.Label:
    """Build a left-aligned section title (e.g. *Library*).

    The visual treatment (font size, weight, letter-spacing, dim)
    lives in ``app.css`` keyed off the :data:`_SECTION_HEADER_CSS_CLASS`
    class; this helper only positions the label.
    """
    label = Gtk.Label.new(text)
    label.set_halign(Gtk.Align.START)
    label.set_margin_top(_SECTION_VERTICAL_SPACING_PX)
    label.set_margin_bottom(_SECTION_VERTICAL_SPACING_PX // 2)
    label.set_margin_start(_ROW_SPACING_PX)
    label.add_css_class(_SECTION_HEADER_CSS_CLASS)
    return label


def _make_tags_header() -> Gtk.Box:
    """Build the Tags section header: ``Tags`` + an optional accent label.

    Layout: two labels in a horizontal box. The first is the standard
    section-header label ``Tags``; the second is hidden by default and
    is revealed with text like ``(2 selected)`` (carrying the
    ``.selection-count`` class for the info-blue accent) when the tag
    selection is non-empty.
    """
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)
    box.set_margin_top(_SECTION_VERTICAL_SPACING_PX)
    box.set_margin_bottom(_SECTION_VERTICAL_SPACING_PX // 2)
    box.set_margin_start(_ROW_SPACING_PX)
    box.set_halign(Gtk.Align.START)

    base = Gtk.Label.new(_TAGS_HEADER_FORMAT)
    base.set_halign(Gtk.Align.START)
    base.set_valign(Gtk.Align.BASELINE_CENTER)
    base.add_css_class(_SECTION_HEADER_CSS_CLASS)
    box.append(base)

    accent = Gtk.Label.new("")
    accent.set_halign(Gtk.Align.START)
    accent.set_valign(Gtk.Align.BASELINE_CENTER)
    accent.add_css_class(_SECTION_COUNT_CSS_CLASS)
    accent.set_visible(False)
    box.append(accent)
    return box


# ---------------------------------------------------------------------------
# Smart-row factory (Library section)
# ---------------------------------------------------------------------------


def _make_smart_row_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory.new()
    factory.connect("setup", _on_smart_factory_setup)
    factory.connect("bind", _on_smart_factory_bind)
    return factory


def _on_smart_factory_setup(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)

    icon = Gtk.Image.new_from_icon_name(
        _SMART_FILTER_ICON_NAMES[SmartFilter.ALL]
    )
    box.append(icon)

    label = Gtk.Label.new("")
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(label)

    count_label = Gtk.Label.new("")
    count_label.set_halign(Gtk.Align.END)
    count_label.add_css_class(_COUNT_CSS_CLASS)
    box.append(count_label)

    list_item.set_child(box)


def _on_smart_factory_bind(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    item = list_item.get_item()
    box = list_item.get_child()
    if not isinstance(item, _SmartItem) or not isinstance(box, Gtk.Box):
        return
    icon = box.get_first_child()
    if isinstance(icon, Gtk.Image):
        icon.set_from_icon_name(item.icon_name)
    label = icon.get_next_sibling() if icon is not None else None
    if isinstance(label, Gtk.Label):
        label.set_text(item.label)
    count_label = box.get_last_child()
    if isinstance(count_label, Gtk.Label):
        count_label.set_text(str(item.count))


# ---------------------------------------------------------------------------
# Tag-row factory (Tags section)
# ---------------------------------------------------------------------------


def _make_tag_row_factory(
    on_row_clicked: _TagRowClickHandler,
) -> Gtk.SignalListItemFactory:
    """Build the tag-row factory.

    Each row carries a :class:`Gtk.GestureClick` whose ``released``
    handler calls ``on_row_clicked`` with the row's live model
    position and then claims the gesture, so a plain single click
    toggles the tag additively (see :meth:`Sidebar._on_tag_row_clicked`)
    rather than letting GTK's default replace-selection handler run.
    The factory holds no widget state itself.
    """
    factory = Gtk.SignalListItemFactory.new()
    factory.connect("setup", _on_tag_factory_setup, on_row_clicked)
    factory.connect("bind", _on_tag_factory_bind)
    return factory


def _on_tag_factory_setup(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
    on_row_clicked: _TagRowClickHandler,
) -> None:
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)

    label = Gtk.Label.new("")
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(label)

    count_label = Gtk.Label.new("")
    count_label.set_halign(Gtk.Align.END)
    count_label.add_css_class(_COUNT_CSS_CLASS)
    box.append(count_label)

    # A per-row primary-button click gesture. On release it toggles the
    # tag additively and claims the sequence; because the gesture sits
    # on the row's own box (inner to the ListView's selection gesture),
    # the default BUBBLE phase delivers the event here first, so the
    # claim preempts GTK's replace-selection behaviour. The closure
    # captures ``list_item`` so the handler can read the row's *live*
    # position (rows are recycled, so the position is read at click
    # time, not bind time).
    gesture = Gtk.GestureClick.new()
    gesture.set_button(Gdk.BUTTON_PRIMARY)
    gesture.connect(
        "released",
        _on_tag_row_gesture_released,
        list_item,
        on_row_clicked,
    )
    box.add_controller(gesture)

    list_item.set_child(box)


def _on_tag_row_gesture_released(
    gesture: Gtk.GestureClick,
    _n_press: int,
    _x: float,
    _y: float,
    list_item: Gtk.ListItem,
    on_row_clicked: _TagRowClickHandler,
) -> None:
    """Translate a plain click into an additive tag toggle.

    Reads the row's live position from ``list_item`` (rows are
    recycled, so the bind-time position would be stale), invokes the
    sidebar callback, and claims the gesture so GTK's default
    replace-selection handler does not also run.
    """
    position = list_item.get_position()
    if position != Gtk.INVALID_LIST_POSITION:
        on_row_clicked(position)
    gesture.set_state(Gtk.EventSequenceState.CLAIMED)


def _on_tag_factory_bind(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    item = list_item.get_item()
    box = list_item.get_child()
    if not isinstance(item, _TagItem) or not isinstance(box, Gtk.Box):
        return
    label = box.get_first_child()
    if isinstance(label, Gtk.Label):
        label.set_text(f"{_TAG_PREFIX}{item.name}")
    count_label = box.get_last_child()
    if isinstance(count_label, Gtk.Label):
        count_label.set_text(str(item.count))


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


class Sidebar(  # pylint: disable=too-many-instance-attributes
    Gtk.Box,
):
    """The library navigation pane.

    Two model-driven sections: the Library row pair at the top
    (``All notes``, ``Untagged``) and the flat Tags list below it.
    Selection is mutually exclusive across the two; the rule is owned
    by :class:`AppState`, not by this widget.
    """

    _note_repository: NoteRepositoryProtocol
    _app_state: AppState

    _smart_store: Gio.ListStore
    _smart_selection: Gtk.SingleSelection
    _tag_store: Gio.ListStore
    _tag_selection: Gtk.MultiSelection
    _tag_list_view: Gtk.ListView
    _tags_header_box: Gtk.Box
    _suppress_selection_events: bool

    def __init__(
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        app_state: AppState,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._app_state = app_state
        self._suppress_selection_events = False

        self.add_css_class(_SIDEBAR_CSS_CLASS)

        # ---------- Library section ----------
        self.append(_make_section_header(_LIBRARY_HEADER_TEXT))
        self._smart_store = Gio.ListStore.new(_SmartItem)
        self._smart_selection = Gtk.SingleSelection.new(self._smart_store)
        self._smart_selection.set_autoselect(False)
        self._smart_selection.set_can_unselect(True)
        self._smart_selection.set_selected(Gtk.INVALID_LIST_POSITION)
        smart_view = Gtk.ListView.new(
            self._smart_selection,
            _make_smart_row_factory(),
        )
        smart_scroller = Gtk.ScrolledWindow()
        smart_scroller.set_propagate_natural_height(True)
        smart_scroller.set_child(smart_view)
        self.append(smart_scroller)

        # ---------- Tags section ----------
        self._tags_header_box = _make_tags_header()
        self.append(self._tags_header_box)

        self._tag_store = Gio.ListStore.new(_TagItem)
        self._tag_selection = Gtk.MultiSelection.new(self._tag_store)
        self._tag_list_view = Gtk.ListView.new(
            self._tag_selection,
            _make_tag_row_factory(self._on_tag_row_clicked),
        )
        tag_scroller = Gtk.ScrolledWindow()
        tag_scroller.set_propagate_natural_height(True)
        tag_scroller.set_vexpand(True)
        tag_scroller.set_child(self._tag_list_view)
        self.append(tag_scroller)

        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

        # ---------- Wiring ----------
        self._smart_selection.connect(
            "selection-changed",
            self._on_smart_selection_changed,
        )
        self._tag_selection.connect(
            "selection-changed",
            self._on_tag_selection_changed,
        )
        self._app_state.connect(
            "selection-changed",
            self._on_app_state_selection_changed,
        )

        # Initial population.
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every row and update every count.

        Idempotent. Triggered automatically on construction and (from
        the main window) on every ``notes-changed`` from the
        :class:`NoteController`.
        """
        notes = self._note_repository.list_all()
        tag_pairs = self._note_repository.list_tags()

        # ----- Library section -----
        self._smart_store.remove_all()
        all_count = len(notes)
        untagged_count = count_untagged(notes)
        for smart_filter in _SMART_FILTER_DISPLAY_ORDER:
            count = all_count if smart_filter is SmartFilter.ALL else untagged_count
            self._smart_store.append(
                _SmartItem(
                    icon_name=_SMART_FILTER_ICON_NAMES[smart_filter],
                    label=_SMART_FILTER_LABELS[smart_filter],
                    count=count,
                    smart_filter=smart_filter,
                ),
            )

        # ----- Tags section -----
        existing_names = {name for name, _ in tag_pairs}
        # Drop selected tags that no longer exist before rebuilding.
        selection = self._app_state.selection
        if isinstance(selection, TagSelection):
            survivors = selection.tags & existing_names
            if survivors != selection.tags:
                # ``AppState`` does not expose a bulk-set mutator; toggle
                # each removed tag off. Each call is idempotent on a
                # missing tag, so toggling drops the membership.
                for missing in selection.tags - survivors:
                    self._app_state.toggle_tag(missing)

        self._tag_store.remove_all()
        for name, count in tag_pairs:
            self._tag_store.append(_TagItem(name=name, count=count))

        self._apply_highlight()
        self._refresh_tags_header()

    # ------------------------------------------------------------------
    # Selection plumbing
    # ------------------------------------------------------------------

    def _on_smart_selection_changed(
        self,
        _selection: Gtk.SingleSelection,
        _position: int,
        _n_items: int,
    ) -> None:
        if self._suppress_selection_events:
            return
        pos = self._smart_selection.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION:
            return
        item = self._smart_store.get_item(pos)
        if isinstance(item, _SmartItem):
            self._app_state.set_smart(item.smart_filter)

    def _on_tag_selection_changed(
        self,
        _selection: Gtk.MultiSelection,
        _position: int,
        _n_items: int,
    ) -> None:
        if self._suppress_selection_events:
            return
        new_selected_names: set[str] = set()
        for index in range(self._tag_store.get_n_items()):
            if self._tag_selection.is_selected(index):
                item = self._tag_store.get_item(index)
                if isinstance(item, _TagItem):
                    new_selected_names.add(item.name)
        current = self._app_state.selection
        current_names: set[str] = (
            set(current.tags) if isinstance(current, TagSelection) else set()
        )
        # Toggle the symmetric difference — each name's membership in
        # AppState flips to match the widget's truth.
        for name in new_selected_names ^ current_names:
            self._app_state.toggle_tag(name)

    def _on_app_state_selection_changed(self, _state: AppState) -> None:
        self._apply_highlight()
        self._refresh_tags_header()

    def _apply_highlight(self) -> None:
        """Re-apply the selection state to both ListViews."""
        selection = self._app_state.selection
        self._suppress_selection_events = True
        try:
            match selection:
                case SmartSelection(smart_filter=sf):
                    self._select_smart_row(sf)
                    self._clear_tag_selection()
                case TagSelection(tags=tags):
                    self._smart_selection.set_selected(
                        Gtk.INVALID_LIST_POSITION,
                    )
                    self._select_tag_rows(tags)
        finally:
            self._suppress_selection_events = False

    def _select_smart_row(self, smart_filter: SmartFilter) -> None:
        for index in range(self._smart_store.get_n_items()):
            item = self._smart_store.get_item(index)
            if isinstance(item, _SmartItem) and item.smart_filter is smart_filter:
                self._smart_selection.set_selected(index)
                return
        self._smart_selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def _clear_tag_selection(self) -> None:
        if self._tag_store.get_n_items() == 0:
            return
        self._tag_selection.unselect_all()

    def _select_tag_rows(self, tags: frozenset[str]) -> None:
        for index in range(self._tag_store.get_n_items()):
            item = self._tag_store.get_item(index)
            if not isinstance(item, _TagItem):
                continue
            if item.name in tags:
                self._tag_selection.select_item(index, False)
            else:
                self._tag_selection.unselect_item(index)

    def _refresh_tags_header(self) -> None:
        accent = self._tags_header_box.get_last_child()
        if not isinstance(accent, Gtk.Label):
            return
        selection = self._app_state.selection
        selected_count = (
            len(selection.tags) if isinstance(selection, TagSelection) else 0
        )
        text = tags_header_accent_text(selected_count)
        accent.set_text(text)
        accent.set_visible(bool(text))

    # ------------------------------------------------------------------
    # Tag-row click handling
    # ------------------------------------------------------------------

    def _on_tag_row_clicked(self, position: int) -> None:
        """Additive single-click toggle on the tag row at ``position``.

        A plain click toggles that one tag into / out of the selection
        **without clearing the others** — no Shift / Ctrl needed. This
        is the ``unselect_rest=False`` arm of
        :meth:`Gtk.SelectionModel.select_item` (GTK's built-in click
        handler passes ``True``, replacing the selection; the row
        gesture claims the click so that default never runs). Mutating
        the model fires ``selection-changed``, which
        :meth:`_on_tag_selection_changed` mirrors into :class:`AppState`
        via its symmetric-difference toggle — so the AND-filter truth
        updates automatically with no extra wiring here.
        """
        if self._tag_selection.is_selected(position):
            self._tag_selection.unselect_item(position)
        else:
            self._tag_selection.select_item(position, False)

    # ------------------------------------------------------------------
    # Read-only properties exposed for tests
    # ------------------------------------------------------------------

    @property
    def smart_store(self) -> Gio.ListStore:
        return self._smart_store

    @property
    def tag_store(self) -> Gio.ListStore:
        return self._tag_store

    @property
    def tag_selection(self) -> Gtk.MultiSelection:
        return self._tag_selection

    @property
    def tag_list_view(self) -> Gtk.ListView:
        return self._tag_list_view

    @property
    def smart_selection(self) -> Gtk.SingleSelection:
        return self._smart_selection

    @property
    def tags_header_box(self) -> Gtk.Box:
        return self._tags_header_box

"""The library navigation pane on the left of the window.

Principles & invariants
-----------------------
* :class:`Sidebar` is a navigation-only widget at step 9 of the build.
  Click handlers translate user gestures into mutations of
  :attr:`AppState.selection`; widgets that depend on the selection
  (:class:`NoteList`, :class:`NoteView`) listen to ``selection-changed``
  and pick the change up from there. Direct cross-widget references
  are not held — every shared piece of state flows through
  :class:`AppState`. This is the property that lets the three panes
  be swapped, tested, or replaced independently.
* Hierarchy (top-level → optional one layer of children, per the
  plan's strict two-level rule) is expressed as **data**, not as
  hand-rendered widgets: a root :class:`Gio.ListStore` of
  :class:`_SidebarItem` objects wrapped in a :class:`Gtk.TreeListModel`
  whose create-child-model callback returns a notebook's children
  (or ``None`` for a leaf, which is what enforces the two-level rule).
  Rows are built by a :class:`Gtk.SignalListItemFactory` and wrapped in
  a :class:`Gtk.TreeExpander`, which draws the expand arrow, indents by
  depth, and reserves the arrow gutter on non-expandable rows. This
  replaces the former flat-list-with-depth-first-traversal rendering
  and its hand-tuned chevron/spacer columns.
* Expansion state is widget-local (intentional — different windows
  could disagree on what is open without affecting any shared
  meaning), now living on :meth:`Gtk.TreeListRow.set_expanded` and
  driven by the user through the :class:`Gtk.TreeExpander` arrow.
  v1 has a single window so this is moot, but the boundary keeps
  :class:`AppState` free of widget-local UI noise. Because
  :meth:`refresh` rebuilds the model from scratch, the set of open
  notebook ids is snapshotted before the rebuild and re-applied after
  it, so a refresh does not silently collapse the tree.
* CRUD on notebooks (rename, change icon, delete, create) is **not
  delivered in step 9**. Those flows depend on
  :mod:`ui.dialogs` (the icon picker popover and the
  confirm-delete dialog), which arrives at step 12. Until then the
  sidebar is read-only and surfaces only the navigation gestures the
  rest of the app already needs to react to. The factory ``bind`` is
  the natural place to attach a context-menu gesture when those
  actions ship.
* Counts are computed eagerly on every :meth:`refresh` from a single
  :meth:`NoteRepositoryProtocol.list_all` call. The counts are cheap
  (a per-row filter against the materialised list), and centralising
  the read in one method keeps the per-row logic free of repository
  calls. If the library grows large enough that ``list_all`` becomes
  a hot path, a future ``count_*`` query can be added to the
  repository protocol; the sidebar would change its callsite without
  touching its rendering code.
* Highlighting follows :attr:`AppState.selection`. The two sections
  (smart filters / notebook tree) are rendered as two independent
  :class:`Gtk.ListView`\\s, each driven by its own
  :class:`Gtk.SingleSelection`; cross-section coordination — at most
  one row highlighted across both — is the sidebar's job, performed in
  :meth:`_apply_highlight`. Unlike the former ``row-activated`` input,
  :class:`Gtk.SingleSelection` *does* emit ``selection-changed`` on a
  programmatic change, so the programmatic updates this widget makes in
  response to ``selection-changed`` are fenced behind a re-entrancy
  flag (:attr:`_suppress_selection_events`) to stop them looping back
  into a second :meth:`AppState.set_selection` call.
* The icon-column alignment *between* the two sections (and between a
  parent row and its childless siblings) is not automatic: GTK 4
  halved the tree-indent placeholder, so an expandable row's arrow
  outdents a leaf's placeholder. The residual gap is closed by the
  ``.sidebar treeexpander indent { -gtk-icon-size: 16px }`` rule in
  ``ui/css/app.css``. That CSS rule and the :class:`Gtk.TreeExpander`
  construction here are a matched pair — change one and re-check the
  other.
* The clock used for the *Recent* smart-filter count is injected so
  tests can pin the result. Production wires :func:`datetime.now`
  through :func:`_default_clock`.
* GTK 4.18 / 4.10 deprecations are avoided: the model-driven list
  widgets (:class:`Gtk.ListView`, :class:`Gtk.TreeListModel`,
  :class:`Gtk.TreeExpander`, :class:`Gtk.SingleSelection`) are the
  framework's current tree primitives, replacing the GTK3-era
  :class:`Gtk.TreeView` (deprecated since 4.10);
  :meth:`Gtk.Image.new_from_icon_name` rather than the deprecated
  ``Gtk.Image.new_from_stock``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gio, GObject, Gtk, Pango  # noqa: E402

from controllers.app_state import AppState
from enums import NotebookIcon, SmartFilter
from models.note import Note
from models.notebook import Notebook
from search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    Selection,
    SmartSelection,
)
from storage.protocols import (
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


type ClockFn = Callable[[], datetime]
"""Callable returning a timezone-aware ``datetime`` representing 'now'.

Injected so the *Recent* smart-filter count is deterministic in tests
and so production has a single explicit place that depends on the
wall clock.
"""


# ---------------------------------------------------------------------------
# Constants — labels and icon-name mappings
# ---------------------------------------------------------------------------


_LIBRARY_HEADER_TEXT: Final[str] = "Library"
_NOTEBOOKS_HEADER_TEXT: Final[str] = "Notebooks"

_SMART_FILTER_LABELS: Final[dict[SmartFilter, str]] = {
    SmartFilter.ALL: "All notes",
    SmartFilter.RECENT: "Recent",
}
"""Visible labels for the two smart-filter rows.

The ordering of presentation comes from
:data:`_SMART_FILTER_DISPLAY_ORDER`; this mapping is keyed by the
enum value so a future addition to :class:`SmartFilter` is a
compile-time reminder to add a label here too.
"""

_SMART_FILTER_DISPLAY_ORDER: Final[tuple[SmartFilter, ...]] = (
    SmartFilter.ALL,
    SmartFilter.RECENT,
)
"""The order the smart-filter rows are rendered in. Matches
``data.jsx`` / ``app.jsx`` from the design reference."""

_SMART_FILTER_ICON_NAMES: Final[dict[SmartFilter, str]] = {
    SmartFilter.ALL: "view-list-symbolic",
    SmartFilter.RECENT: "starred-symbolic",
}
"""Symbolic-icon names for the smart-filter rows.

These are FreeDesktop icon names; the active GTK icon theme provides
the actual SVGs. If a name is not in the theme GTK shows a
broken-image placeholder — visible but not crashing, which is what
we want for a v1 that ships without bundled icon assets.
"""

_NOTEBOOK_ICON_NAMES: Final[dict[NotebookIcon, str]] = {
    NotebookIcon.HOME: "user-home-symbolic",
    NotebookIcon.BOOK: "accessories-text-editor-symbolic",
    NotebookIcon.MAP: "mark-location-symbolic",
    NotebookIcon.BRAIN: "applications-science-symbolic",
    NotebookIcon.ARCHIVE: "package-x-generic-symbolic",
    NotebookIcon.BRIEFCASE: "system-run-symbolic",
    NotebookIcon.HEART: "emblem-favorite-symbolic",
    NotebookIcon.STAR: "starred-symbolic",
    NotebookIcon.FOLDER: "folder-symbolic",
    NotebookIcon.INBOX: "mail-inbox-symbolic",
    NotebookIcon.GRADUATION_CAP: "preferences-desktop-display-symbolic",
}
"""Mapping from :class:`NotebookIcon` to FreeDesktop icon names.

The mapping is exhaustive over the enum at definition time; mypy will
not enforce that automatically, but :func:`_icon_name_for_notebook`
falls back to a generic ``folder-symbolic`` so an unknown member
displays without crashing.
"""

_FALLBACK_NOTEBOOK_ICON_NAME: Final[str] = "folder-symbolic"
"""Used when a :class:`NotebookIcon` value has no entry in
:data:`_NOTEBOOK_ICON_NAMES` (e.g. a future enum addition that landed
in storage before the icon mapping was updated)."""

_ROW_SPACING_PX: Final[int] = 6
"""Horizontal spacing inside a row, between icon, label, and count."""

_SECTION_VERTICAL_SPACING_PX: Final[int] = 8
"""Padding above section headers (Library / Notebooks)."""

_DEFAULT_PANE_WIDTH_PX: Final[int] = 220
"""Initial width hint for the sidebar pane."""

_SIDEBAR_CSS_CLASS: Final[str] = "sidebar"
"""Class on the :class:`Sidebar` box that the stylesheet keys off."""

_SECTION_HEADER_CSS_CLASS: Final[str] = "sidebar-section-header"
"""Class on each section-header label (font treatment + dim)."""

_COUNT_CSS_CLASS: Final[str] = "sidebar-count"
"""Class on each row's count label (dimmed when the row is unselected)."""


# ---------------------------------------------------------------------------
# Default factories
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Production clock — UTC, full resolution preserved."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _icon_name_for_notebook(icon: NotebookIcon) -> str:
    """Look up the FreeDesktop icon name for ``icon``, with fallback."""
    return _NOTEBOOK_ICON_NAMES.get(icon, _FALLBACK_NOTEBOOK_ICON_NAME)


def _count_smart_filter(
    smart_filter: SmartFilter,
    notes: list[Note],
    *,
    now: datetime,
) -> int:
    """Return how many ``notes`` belong to ``smart_filter``.

    Pure helper. The :data:`SmartFilter.ALL` count is just the total;
    :data:`SmartFilter.RECENT` re-implements the same predicate
    :func:`filter_by_selection` uses (notes within the last
    :data:`RECENT_WINDOW_DAYS`) so that the sidebar count and the
    note-list filter agree by construction.
    """
    if smart_filter is SmartFilter.ALL:
        return len(notes)
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)
    return sum(1 for note in notes if note.modified_at >= cutoff)


def _count_notebook(
    notebook_id: str,
    notes: list[Note],
    children_ids: list[str],
) -> int:
    """Return the count of notes in ``notebook_id`` plus its children.

    Mirrors the design's ``notesInNB`` helper (``sidebar.jsx``):
    selecting a parent notebook surfaces its own notes *and* every
    child notebook's notes, so the count must reflect the same.
    """
    matching_ids = {notebook_id, *children_ids}
    return sum(1 for note in notes if note.notebook_id in matching_ids)


def _children_of(
    parent_id: str,
    all_notebooks: list[Notebook],
) -> list[Notebook]:
    """Return the direct children of ``parent_id`` in declaration order.

    The two-level invariant means a child cannot itself have children
    — every result of this function is guaranteed to be a leaf.
    """
    return [nb for nb in all_notebooks if nb.parent_id == parent_id]


def _top_level_notebooks(all_notebooks: list[Notebook]) -> list[Notebook]:
    """Return notebooks whose ``parent_id`` is ``None``."""
    return [nb for nb in all_notebooks if nb.parent_id is None]


# ---------------------------------------------------------------------------
# Row item model object
# ---------------------------------------------------------------------------


class _SidebarItem(GObject.Object):
    """A sidebar entry backing the list model.

    A plain :class:`GObject.Object` so :class:`Gio.ListStore` can hold
    it. :attr:`payload` carries the typed selection target
    (:class:`SmartSelection` or :class:`NotebookSelection`) so the
    selection handler maps an activated row straight to an
    :class:`AppState` update with no extra "row kind" enum and no
    second lookup. :attr:`children` is read by the tree model's
    create-child-model callback; an empty tuple marks a leaf (the
    two-level rule means children always have an empty tuple).
    """

    __gtype_name__ = "NotesSidebarItem"

    icon_name: str
    label: str
    count: int
    payload: Selection
    children: tuple[_SidebarItem, ...]

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        icon_name: str,
        label: str,
        count: int,
        payload: Selection,
        children: tuple[_SidebarItem, ...] = (),
    ) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.label = label
        self.count = count
        self.payload = payload
        self.children = children


def _smart_filter_item(smart_filter: SmartFilter, count: int) -> _SidebarItem:
    """Build the model item for a smart-filter row."""
    return _SidebarItem(
        icon_name=_SMART_FILTER_ICON_NAMES[smart_filter],
        label=_SMART_FILTER_LABELS[smart_filter],
        count=count,
        payload=SmartSelection(smart_filter=smart_filter),
    )


def _notebook_item(
    notebook: Notebook,
    *,
    count: int,
    children: tuple[_SidebarItem, ...],
) -> _SidebarItem:
    """Build the model item for a single notebook row."""
    return _SidebarItem(
        icon_name=_icon_name_for_notebook(notebook.icon),
        label=notebook.name,
        count=count,
        payload=NotebookSelection(notebook_id=notebook.id),
        children=children,
    )


def _build_notebook_items(
    notebooks: list[Notebook],
    notes: list[Note],
) -> list[_SidebarItem]:
    """Build the top-level notebook items, each carrying its children.

    Pure: turns the flat notebook list + note list into the tree of
    :class:`_SidebarItem`\\s the model consumes. Counts are computed
    eagerly here (parents include their children's notes).
    """
    items: list[_SidebarItem] = []
    for top_notebook in _top_level_notebooks(notebooks):
        children_notebooks = _children_of(top_notebook.id, notebooks)
        child_items = tuple(
            _notebook_item(
                child,
                count=_count_notebook(child.id, notes, []),
                children=(),
            )
            for child in children_notebooks
        )
        count = _count_notebook(
            top_notebook.id,
            notes,
            children_ids=[child.id for child in children_notebooks],
        )
        items.append(
            _notebook_item(top_notebook, count=count, children=child_items)
        )
    return items


def _create_child_model(item: GObject.Object) -> Gio.ListStore | None:
    """Tree-model child callback: a :class:`Gio.ListStore` of children.

    Returns ``None`` for a leaf (an item with no children). The
    ``None`` return is what makes a row non-expandable — and, under
    the two-level rule, every child item is a leaf.
    """
    if not isinstance(item, _SidebarItem) or not item.children:
        return None
    store = Gio.ListStore.new(_SidebarItem)
    for child in item.children:
        store.append(child)
    return store


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


# ---------------------------------------------------------------------------
# Row factory
# ---------------------------------------------------------------------------


def _make_row_factory() -> Gtk.SignalListItemFactory:
    """Build the shared factory for both sections' rows.

    ``setup`` builds ``Gtk.TreeExpander → Gtk.Box[icon, label, count]``
    once per recycled row; ``bind`` wires the row to its
    :class:`Gtk.TreeListRow` and fills the widgets from the item.
    Both sections use the *same* factory so their icon columns share
    the :class:`Gtk.TreeExpander` gutter and line up automatically
    (the smart-filter section's items are leaves, so their expander
    draws only the reserved gutter, no arrow).
    """
    factory = Gtk.SignalListItemFactory.new()
    factory.connect("setup", _on_factory_setup)
    factory.connect("bind", _on_factory_bind)
    return factory


def _on_factory_setup(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    """Build the row's widget tree (reused across recycled rows)."""
    expander = Gtk.TreeExpander.new()
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)

    icon = Gtk.Image.new_from_icon_name(_FALLBACK_NOTEBOOK_ICON_NAME)
    box.append(icon)

    label = Gtk.Label.new("")
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    # Long notebook names ellipsise at the end so the count column on
    # the right is never pushed out of view.
    label.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(label)

    count_label = Gtk.Label.new("")
    count_label.set_halign(Gtk.Align.END)
    count_label.add_css_class(_COUNT_CSS_CLASS)
    box.append(count_label)

    expander.set_child(box)
    list_item.set_child(expander)


def _on_factory_bind(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    """Fill the row widgets from the bound :class:`_SidebarItem`."""
    tree_row = list_item.get_item()
    expander = list_item.get_child()
    if not isinstance(tree_row, Gtk.TreeListRow) or not isinstance(
        expander, Gtk.TreeExpander
    ):
        return  # defensive — the model only ever holds these types
    expander.set_list_row(tree_row)

    item = tree_row.get_item()
    if not isinstance(item, _SidebarItem):
        return
    box = expander.get_child()
    if not isinstance(box, Gtk.Box):
        return

    icon = box.get_first_child()
    if isinstance(icon, Gtk.Image):
        icon.set_from_icon_name(item.icon_name)

    count_label = box.get_last_child()
    if isinstance(count_label, Gtk.Label):
        count_label.set_text(str(item.count))
        # The label sits between the icon and the count.
        label = count_label.get_prev_sibling()
        if isinstance(label, Gtk.Label):
            label.set_text(item.label)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


class Sidebar(Gtk.Box):
    """The library navigation pane.

    Two model-driven sections: the smart-filter list at the top
    (``All notes``, ``Recent``) and the notebook tree below it.
    Selection is mutually exclusive across both: selecting a row in
    one section clears the other and updates :class:`AppState`, and a
    selection change on :class:`AppState` is reflected in whichever
    section owns the matching row.

    Only the two :class:`Gtk.SingleSelection`\\s are held as
    per-section state; the tree model and its backing
    :class:`Gio.ListStore` are reached through the selection so the
    widget keeps the minimum surface it needs.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _app_state: AppState
    _clock: ClockFn

    _smart_selection: Gtk.SingleSelection
    _notebook_selection: Gtk.SingleSelection
    _suppress_selection_events: bool

    def __init__(
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        notebook_repository: NotebookRepositoryProtocol,
        app_state: AppState,
        clock: ClockFn = _default_clock,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._notebook_repository = notebook_repository
        self._app_state = app_state
        self._clock = clock
        self._suppress_selection_events = False

        self.add_css_class(_SIDEBAR_CSS_CLASS)

        factory = _make_row_factory()

        # Library section header + smart-filter list.
        self.append(_make_section_header(_LIBRARY_HEADER_TEXT))
        self._smart_selection = self._make_section_view(factory)

        # Notebooks section header + notebook tree.
        self.append(_make_section_header(_NOTEBOOKS_HEADER_TEXT))
        self._notebook_selection = self._make_section_view(factory)

        # Sidebar takes its preferred width hint from the constant.
        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

        self._smart_selection.connect(
            "selection-changed",
            self._on_section_selection_changed,
        )
        self._notebook_selection.connect(
            "selection-changed",
            self._on_section_selection_changed,
        )
        self._app_state.connect(
            "selection-changed",
            self._on_app_state_selection_changed,
        )

        # Initial population.
        self.refresh()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _make_section_view(
        self,
        factory: Gtk.SignalListItemFactory,
    ) -> Gtk.SingleSelection:
        """Build one section's ListView and append it to the sidebar.

        Returns the section's :class:`Gtk.SingleSelection`. The
        :class:`Gtk.ListView` is wrapped in a
        :class:`Gtk.ScrolledWindow` with
        ``set_propagate_natural_height(True)``: a bare ``ListView`` in
        a vertical :class:`Gtk.Box` reports ~0 natural height and
        renders blank.
        """
        root = Gio.ListStore.new(_SidebarItem)
        tree_model = Gtk.TreeListModel.new(
            root,
            False,  # passthrough — items are GtkTreeListRow wrappers
            False,  # autoexpand — expansion is user/snapshot driven
            _create_child_model,
        )
        selection = Gtk.SingleSelection.new(tree_model)
        # Full manual control of the highlight: do not auto-select the
        # first row, and allow clearing to "nothing selected" so the
        # non-owning section can be emptied.
        selection.set_autoselect(False)
        selection.set_can_unselect(True)
        selection.set_selected(Gtk.INVALID_LIST_POSITION)

        list_view = Gtk.ListView.new(selection, factory)
        scroller = Gtk.ScrolledWindow()
        scroller.set_propagate_natural_height(True)
        scroller.set_child(list_view)
        self.append(scroller)
        return selection

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every row and update every count.

        Idempotent. Called from :meth:`__init__` and (later, when
        controllers ship in step 12+) from the main window in
        response to ``notes-changed`` / ``notebooks-changed`` from
        the controllers. A full rebuild is the simplest correct
        behaviour at the v1 library size; incremental updates are an
        optimisation we will reach for only if profiling shows it
        matters.

        Open notebooks are snapshotted before the model is rebuilt and
        re-expanded afterwards so a refresh does not collapse the tree.
        """
        notes = self._note_repository.list_all()
        notebooks = self._notebook_repository.list_all()
        now = self._clock()

        expanded_ids = self._snapshot_expanded_notebook_ids()

        self._rebuild_smart_filters(notes, now=now)
        self._rebuild_notebooks(notebooks, notes)

        self._restore_expanded_notebook_ids(expanded_ids)
        self._apply_highlight()

    # ------------------------------------------------------------------
    # Model rebuilds
    # ------------------------------------------------------------------

    def _rebuild_smart_filters(
        self,
        notes: list[Note],
        *,
        now: datetime,
    ) -> None:
        """Repopulate the smart-filter section's root store."""
        root = _root_store_of(self._smart_selection)
        root.remove_all()
        for smart_filter in _SMART_FILTER_DISPLAY_ORDER:
            count = _count_smart_filter(smart_filter, notes, now=now)
            root.append(_smart_filter_item(smart_filter, count))

    def _rebuild_notebooks(
        self,
        notebooks: list[Notebook],
        notes: list[Note],
    ) -> None:
        """Repopulate the notebook section's root store from the tree."""
        root = _root_store_of(self._notebook_selection)
        root.remove_all()
        for item in _build_notebook_items(notebooks, notes):
            root.append(item)

    # ------------------------------------------------------------------
    # Expansion snapshot / restore
    # ------------------------------------------------------------------

    def _snapshot_expanded_notebook_ids(self) -> set[str]:
        """Record the ids of currently-expanded top-level notebooks.

        Expansion is widget-local and lives on the tree rows; a model
        rebuild discards it, so it is captured here and re-applied by
        :meth:`_restore_expanded_notebook_ids`. Stale ids (a notebook
        deleted between refreshes) simply find no matching row on
        restore and are dropped.
        """
        expanded: set[str] = set()
        model = _tree_model_of(self._notebook_selection)
        for tree_row in _iter_tree_rows(model):
            if not tree_row.get_expanded():
                continue
            item = tree_row.get_item()
            if isinstance(item, _SidebarItem) and isinstance(
                item.payload, NotebookSelection
            ):
                expanded.add(item.payload.notebook_id)
        return expanded

    def _restore_expanded_notebook_ids(self, expanded_ids: set[str]) -> None:
        """Re-expand the rows whose notebook id is in ``expanded_ids``."""
        if not expanded_ids:
            return
        model = _tree_model_of(self._notebook_selection)
        for tree_row in _expandable_rows_matching(model, expanded_ids):
            tree_row.set_expanded(True)

    # ------------------------------------------------------------------
    # Selection plumbing
    # ------------------------------------------------------------------

    def _on_section_selection_changed(
        self,
        selection: Gtk.SingleSelection,
        _position: int,
        _n_items: int,
    ) -> None:
        """A section's selection changed — push it into :class:`AppState`.

        Skipped while :attr:`_suppress_selection_events` is set, which
        is the case during :meth:`_apply_highlight`'s own programmatic
        ``set_selected`` calls; that fence is what stops the
        input→AppState→highlight cycle from looping. A change that
        clears the section (no item selected) is ignored here — the
        owning section's handler is the one that carries the payload.
        """
        if self._suppress_selection_events:
            return
        item = _selected_item(selection)
        if item is None:
            return
        self._app_state.set_selection(item.payload)

    def _on_app_state_selection_changed(self, _state: AppState) -> None:
        """:class:`AppState` selection changed — re-apply the highlight."""
        self._apply_highlight()

    def _apply_highlight(self) -> None:
        """Highlight the row matching the current :class:`AppState`
        selection in whichever section owns it, clearing the other.

        Programmatic ``set_selected`` on a :class:`Gtk.SingleSelection`
        emits ``selection-changed``, so the work is fenced behind
        :attr:`_suppress_selection_events` to avoid looping back into
        :meth:`_on_section_selection_changed`.
        """
        selection = self._app_state.selection
        self._suppress_selection_events = True
        try:
            match selection:
                case SmartSelection():
                    self._select_matching(self._smart_selection, selection)
                    self._notebook_selection.set_selected(
                        Gtk.INVALID_LIST_POSITION
                    )
                case NotebookSelection():
                    self._select_matching(self._notebook_selection, selection)
                    self._smart_selection.set_selected(
                        Gtk.INVALID_LIST_POSITION
                    )
        finally:
            self._suppress_selection_events = False

    @staticmethod
    def _select_matching(
        selection: Gtk.SingleSelection,
        target: Selection,
    ) -> None:
        """Select the row in ``selection`` whose payload equals ``target``.

        Clears the section (``INVALID_LIST_POSITION``) when no row
        matches — e.g. a :class:`NotebookSelection` for an id that no
        longer exists.
        """
        model = _tree_model_of(selection)
        for position, tree_row in enumerate(_iter_tree_rows(model)):
            item = tree_row.get_item()
            if isinstance(item, _SidebarItem) and item.payload == target:
                selection.set_selected(position)
                return
        selection.set_selected(Gtk.INVALID_LIST_POSITION)


# ---------------------------------------------------------------------------
# Model-access helpers (free functions, no widget state)
# ---------------------------------------------------------------------------


def _tree_model_of(selection: Gtk.SingleSelection) -> Gtk.TreeListModel:
    """Return the :class:`Gtk.TreeListModel` wrapped by ``selection``."""
    model = selection.get_model()
    assert isinstance(model, Gtk.TreeListModel)  # built that way in __init__
    return model


def _root_store_of(selection: Gtk.SingleSelection) -> Gio.ListStore:
    """Return the root :class:`Gio.ListStore` behind ``selection``."""
    root = _tree_model_of(selection).get_model()
    assert isinstance(root, Gio.ListStore)  # built that way in __init__
    return root


def _iter_tree_rows(
    model: Gtk.TreeListModel,
) -> Iterable[Gtk.TreeListRow]:
    """Yield every realised :class:`Gtk.TreeListRow` in ``model``.

    The model's item count includes children of expanded rows, so this
    walks the currently-visible tree in display order.
    """
    for position in range(model.get_n_items()):
        tree_row = model.get_item(position)
        if isinstance(tree_row, Gtk.TreeListRow):
            yield tree_row


def _expandable_rows_matching(
    model: Gtk.TreeListModel,
    notebook_ids: set[str],
) -> list[Gtk.TreeListRow]:
    """Collect expandable rows whose notebook id is in ``notebook_ids``.

    Returned as a list (not a generator) so the caller can expand them
    without mutating the model mid-iteration: expanding a row inserts
    its children and shifts later positions.
    """
    matches: list[Gtk.TreeListRow] = []
    for tree_row in _iter_tree_rows(model):
        if not tree_row.is_expandable():
            continue
        item = tree_row.get_item()
        if isinstance(item, _SidebarItem) and isinstance(
            item.payload, NotebookSelection
        ):
            if item.payload.notebook_id in notebook_ids:
                matches.append(tree_row)
    return matches


def _selected_item(selection: Gtk.SingleSelection) -> _SidebarItem | None:
    """Return the :class:`_SidebarItem` selected in ``selection``, or None."""
    position = selection.get_selected()
    if position == Gtk.INVALID_LIST_POSITION:
        return None
    tree_row = selection.get_model().get_item(position)
    if not isinstance(tree_row, Gtk.TreeListRow):
        return None
    item = tree_row.get_item()
    return item if isinstance(item, _SidebarItem) else None

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
* Hierarchy expansion (top-level → optional one layer of children, per
  the plan's strict two-level rule) is rendered as a flat list with a
  depth-first traversal of the notebook tree. Expand / collapse
  toggles which child rows appear; the expansion state lives on the
  widget (:attr:`_expanded_notebook_ids`) rather than in
  :class:`AppState` because it is purely visual — different windows
  could disagree on what is open without affecting any shared meaning.
  v1 has a single window so this is moot, but the boundary keeps
  :class:`AppState` free of widget-local UI noise.
* CRUD on notebooks (rename, change icon, delete, create) is **not
  delivered in step 9**. Those flows depend on
  :mod:`notes_app.ui.dialogs` (the icon picker popover and the
  confirm-delete dialog), which arrives at step 12. Until then the
  sidebar is read-only and surfaces only the navigation gestures the
  rest of the app already needs to react to. The two-level-hierarchy
  *enforcement* the plan describes for the *Add child notebook*
  action ships when that action does, again at step 12; the storage-
  side trigger and the repository-side check remain as defence in
  depth in the meantime.
* Counts are computed eagerly on every :meth:`refresh` from a single
  :meth:`NoteRepositoryProtocol.list_all` call. The counts are cheap
  (a per-row filter against the materialised list), and centralising
  the read in one method keeps the per-row logic free of repository
  calls. If the library grows large enough that ``list_all`` becomes
  a hot path, a future ``count_*`` query can be added to the
  repository protocol; the sidebar would change its callsite without
  touching its rendering code.
* Highlighting follows :attr:`AppState.selection` rather than relying
  solely on :class:`Gtk.ListBox`'s built-in selection. The two
  list-boxes used here (smart filters / notebook tree) each manage
  their own selection independently; cross-listbox coordination — at
  most one row highlighted across both — is the sidebar's job. We
  use ``row-activated`` (which only fires for *user* activation) for
  input, so the programmatic selection updates this widget makes in
  response to ``selection-changed`` cannot loop back into a second
  signal emission.
* The clock used for the *Recent* smart-filter count is injected so
  tests can pin the result. Production wires :func:`datetime.now`
  through :func:`_default_clock`.
* GTK 4.18 / 4.10 deprecations are avoided: :meth:`Gtk.Box.append`
  rather than the removed ``pack_start``;
  :meth:`Gtk.ListBox.connect` to ``row-activated`` rather than a
  manually-wired :class:`Gtk.GestureClick`;
  :meth:`Gtk.Image.new_from_icon_name` rather than the deprecated
  ``Gtk.Image.new_from_stock``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk, Pango  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.enums import NotebookIcon, SmartFilter
from notes_app.models.note import Note
from notes_app.models.notebook import Notebook
from notes_app.search.note_filter import (
    RECENT_WINDOW_DAYS,
    NotebookSelection,
    SmartSelection,
)
from notes_app.storage.protocols import (
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

_CHEVRON_RIGHT_ICON: Final[str] = "pan-end-symbolic"
_CHEVRON_DOWN_ICON: Final[str] = "pan-down-symbolic"

_ROW_SPACING_PX: Final[int] = 6
"""Horizontal spacing inside a row, between icon, label, and count."""

_CHILD_INDENT_PX: Final[int] = 16
"""Left margin added to a child notebook row to mark hierarchy.

Matches the design's ``paddingLeft: 10 + depth*12`` indent at depth
1; the exact pixel value is tuned for the GTK default font size and
will read correctly with the bundled CSS once that arrives.
"""

_SECTION_VERTICAL_SPACING_PX: Final[int] = 8
"""Padding above section headers (Library / Notebooks)."""

_DEFAULT_PANE_WIDTH_PX: Final[int] = 220
"""Initial width hint for the sidebar pane."""


# ---------------------------------------------------------------------------
# Row payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SmartRowPayload:
    """Marks a row that represents a smart filter."""

    smart_filter: SmartFilter


@dataclass(frozen=True)
class _NotebookRowPayload:
    """Marks a row that represents a single notebook in the tree.

    Carries enough context for the click handler to update
    :class:`AppState` without needing to look the notebook back up
    again, and enough for :meth:`Sidebar._apply_highlight` to
    discriminate parent rows (which have an expand control) from
    child rows (which don't).
    """

    notebook_id: str
    is_child: bool
    has_children: bool


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
# Custom row class
# ---------------------------------------------------------------------------


class _SidebarRow(Gtk.ListBoxRow):
    """A :class:`Gtk.ListBoxRow` that carries a typed payload.

    The payload is what the sidebar uses to translate a
    ``row-activated`` signal back into an :class:`AppState` selection
    update. Subclassing :class:`Gtk.ListBoxRow` lets us attach the
    payload as a Python attribute without invoking the deprecated
    ``Gtk.Widget.set_data`` API.
    """

    payload: _SmartRowPayload | _NotebookRowPayload

    def __init__(
        self,
        *,
        payload: _SmartRowPayload | _NotebookRowPayload,
        child: Gtk.Widget,
    ) -> None:
        super().__init__()
        self.payload = payload
        self.set_child(child)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


class Sidebar(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """The library navigation pane.

    Composed of two list-boxes: the smart-filter row group at the top
    (``All notes``, ``Recent``) and the notebook tree below it.
    Selection is mutually exclusive across both: clicking a row in
    one clears the other and updates :class:`AppState`, and a
    selection change on :class:`AppState` is reflected in whichever
    list-box owns the matching row.

    The instance-attribute count is above pylint's default of 7
    because each list-box section needs three pieces of state — the
    list-box widget itself, an id-keyed row index for highlight
    application, and (for the smart-filter section) a count-label
    index used by future incremental updates. Splitting this into a
    helper "Section" object would shuffle the same data behind a
    second class without removing it; the count is the right shape
    for what the widget actually does.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _app_state: AppState
    _clock: ClockFn

    _smart_filter_listbox: Gtk.ListBox
    _smart_filter_rows: dict[SmartFilter, _SidebarRow]
    _smart_filter_count_labels: dict[SmartFilter, Gtk.Label]

    _notebook_listbox: Gtk.ListBox
    _notebook_rows: dict[str, _SidebarRow]
    _expanded_notebook_ids: set[str]

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

        self._smart_filter_rows = {}
        self._smart_filter_count_labels = {}
        self._notebook_rows = {}
        self._expanded_notebook_ids = set()

        # Library section header.
        self.append(_make_section_header(_LIBRARY_HEADER_TEXT))

        # Smart-filter list-box: the All / Recent rows.
        self._smart_filter_listbox = Gtk.ListBox.new()
        self._smart_filter_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._smart_filter_listbox.connect(
            "row-activated",
            self._on_smart_filter_row_activated,
        )
        self.append(self._smart_filter_listbox)

        # Notebooks section header.
        self.append(_make_section_header(_NOTEBOOKS_HEADER_TEXT))

        # Notebook list-box: top-level rows + (optionally) one layer
        # of children when the parent is expanded.
        self._notebook_listbox = Gtk.ListBox.new()
        self._notebook_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._notebook_listbox.connect(
            "row-activated",
            self._on_notebook_row_activated,
        )
        self.append(self._notebook_listbox)

        # Sidebar takes its preferred width hint from the constant.
        self.set_size_request(_DEFAULT_PANE_WIDTH_PX, -1)

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

        Idempotent. Called from :meth:`__init__` and (later, when
        controllers ship in step 12+) from the main window in
        response to ``notes-changed`` / ``notebooks-changed`` from
        the controllers. A full rebuild is the simplest correct
        behaviour at the v1 library size; incremental updates are an
        optimisation we will reach for only if profiling shows it
        matters.
        """
        notes = self._note_repository.list_all()
        notebooks = self._notebook_repository.list_all()
        now = self._clock()

        self._build_smart_filter_rows(notes, now=now)
        self._build_notebook_rows(notebooks, notes)
        self._apply_highlight()

    # ------------------------------------------------------------------
    # Smart-filter rows
    # ------------------------------------------------------------------

    def _build_smart_filter_rows(
        self,
        notes: list[Note],
        *,
        now: datetime,
    ) -> None:
        """(Re)build the smart-filter list-box's rows."""
        self._smart_filter_listbox.remove_all()
        self._smart_filter_rows = {}
        self._smart_filter_count_labels = {}

        for smart_filter in _SMART_FILTER_DISPLAY_ORDER:
            count = _count_smart_filter(smart_filter, notes, now=now)
            row, count_label = _make_smart_filter_row(smart_filter, count)
            self._smart_filter_rows[smart_filter] = row
            self._smart_filter_count_labels[smart_filter] = count_label
            self._smart_filter_listbox.append(row)

    # ------------------------------------------------------------------
    # Notebook rows
    # ------------------------------------------------------------------

    def _build_notebook_rows(
        self,
        notebooks: list[Notebook],
        notes: list[Note],
    ) -> None:
        """(Re)build the notebook list-box.

        Walks the tree depth-first: each top-level notebook is added
        first, immediately followed by its children when the
        top-level row is expanded. The flat list this produces is
        what :class:`Gtk.ListBox` consumes; hierarchy is purely
        visual (chevron + indent).
        """
        self._notebook_listbox.remove_all()
        self._notebook_rows = {}

        # Drop any expansion state that points at a notebook that no
        # longer exists. Keeping stale ids in the set wouldn't break
        # rendering (they're just looked up against the live tree),
        # but it would let the set grow unbounded across sessions if
        # a future build wires this widget to a long-running app
        # state.
        live_ids = {nb.id for nb in notebooks}
        self._expanded_notebook_ids &= live_ids

        for top_notebook in _top_level_notebooks(notebooks):
            children = _children_of(top_notebook.id, notebooks)
            count = _count_notebook(
                top_notebook.id,
                notes,
                children_ids=[child.id for child in children],
            )
            self._append_notebook_row(
                top_notebook,
                count=count,
                is_child=False,
                has_children=bool(children),
            )

            if children and top_notebook.id in self._expanded_notebook_ids:
                for child in children:
                    child_count = _count_notebook(
                        child.id,
                        notes,
                        children_ids=[],
                    )
                    self._append_notebook_row(
                        child,
                        count=child_count,
                        is_child=True,
                        has_children=False,
                    )

    def _append_notebook_row(
        self,
        notebook: Notebook,
        *,
        count: int,
        is_child: bool,
        has_children: bool,
    ) -> None:
        """Build, register, and append a single notebook row."""
        is_expanded = notebook.id in self._expanded_notebook_ids
        row = _make_notebook_row(
            notebook,
            count=count,
            is_child=is_child,
            has_children=has_children,
            is_expanded=is_expanded,
            on_chevron_clicked=self._toggle_expansion,
        )
        self._notebook_rows[notebook.id] = row
        self._notebook_listbox.append(row)

    def _toggle_expansion(self, notebook_id: str) -> None:
        """Flip a top-level notebook's expansion state and rebuild.

        Wired into the chevron button on each parent row; child rows
        never call this. The rebuild is a full re-walk of the tree —
        cheap at v1 size and trivially correct.
        """
        if notebook_id in self._expanded_notebook_ids:
            self._expanded_notebook_ids.remove(notebook_id)
        else:
            self._expanded_notebook_ids.add(notebook_id)

        # Rebuild only the notebook list-box; smart-filter counts
        # haven't changed.
        notebooks = self._notebook_repository.list_all()
        notes = self._note_repository.list_all()
        self._build_notebook_rows(notebooks, notes)
        self._apply_highlight()

    # ------------------------------------------------------------------
    # Selection plumbing
    # ------------------------------------------------------------------

    def _on_smart_filter_row_activated(
        self,
        _listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        """User clicked a smart-filter row. Update :class:`AppState`."""
        if not isinstance(row, _SidebarRow):
            return  # defensive — every row we add is a _SidebarRow
        if not isinstance(row.payload, _SmartRowPayload):
            return
        self._app_state.set_selection(
            SmartSelection(smart_filter=row.payload.smart_filter)
        )

    def _on_notebook_row_activated(
        self,
        _listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        """User clicked a notebook row. Update :class:`AppState`."""
        if not isinstance(row, _SidebarRow):
            return
        if not isinstance(row.payload, _NotebookRowPayload):
            return
        self._app_state.set_selection(
            NotebookSelection(notebook_id=row.payload.notebook_id)
        )

    def _on_app_state_selection_changed(self, _state: AppState) -> None:
        """:class:`AppState` selection changed — re-apply the highlight."""
        self._apply_highlight()

    def _apply_highlight(self) -> None:
        """Highlight the row matching the current :class:`AppState`
        selection in whichever of the two list-boxes owns it.

        The other list-box is unselected. ``select_row`` /
        ``unselect_all`` do not emit ``row-activated`` (only user
        activation does), so this method cannot loop with the click
        handlers above.
        """
        selection = self._app_state.selection
        match selection:
            case SmartSelection(smart_filter=sf):
                target = self._smart_filter_rows.get(sf)
                self._notebook_listbox.unselect_all()
                if target is not None:
                    self._smart_filter_listbox.select_row(target)
                else:
                    self._smart_filter_listbox.unselect_all()
            case NotebookSelection(notebook_id=nb_id):
                target = self._notebook_rows.get(nb_id)
                self._smart_filter_listbox.unselect_all()
                if target is not None:
                    self._notebook_listbox.select_row(target)
                else:
                    self._notebook_listbox.unselect_all()


# ---------------------------------------------------------------------------
# Row-construction helpers (free functions, no widget state)
# ---------------------------------------------------------------------------


def _make_section_header(text: str) -> Gtk.Label:
    """Build a left-aligned, dim section title (e.g. *Library*).

    Returned as a :class:`Gtk.Label` rather than a fancier widget so
    the sidebar layout stays a single :class:`Gtk.Box` of stacked
    children. The actual styling — colour, weight, padding — will
    arrive with the bundled CSS at step 12+.
    """
    label = Gtk.Label.new(text)
    label.set_halign(Gtk.Align.START)
    label.set_margin_top(_SECTION_VERTICAL_SPACING_PX)
    label.set_margin_bottom(_SECTION_VERTICAL_SPACING_PX // 2)
    label.set_margin_start(_ROW_SPACING_PX)
    return label


def _make_smart_filter_row(
    smart_filter: SmartFilter,
    count: int,
) -> tuple[_SidebarRow, Gtk.Label]:
    """Build a single smart-filter row.

    Returns the row itself and the :class:`Gtk.Label` that holds the
    count, so the caller can update it later without re-walking the
    widget tree.
    """
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)
    box.set_margin_start(_ROW_SPACING_PX)
    box.set_margin_end(_ROW_SPACING_PX)

    icon = Gtk.Image.new_from_icon_name(_SMART_FILTER_ICON_NAMES[smart_filter])
    box.append(icon)

    label = Gtk.Label.new(_SMART_FILTER_LABELS[smart_filter])
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    box.append(label)

    count_label = Gtk.Label.new(str(count))
    count_label.set_halign(Gtk.Align.END)
    box.append(count_label)

    row = _SidebarRow(
        payload=_SmartRowPayload(smart_filter=smart_filter),
        child=box,
    )
    return row, count_label


def _make_notebook_row(  # pylint: disable=too-many-arguments
    notebook: Notebook,
    *,
    count: int,
    is_child: bool,
    has_children: bool,
    is_expanded: bool,
    on_chevron_clicked: Callable[[str], None],
) -> _SidebarRow:
    """Build a single notebook row.

    ``has_children`` controls whether a chevron button appears at
    the start of the row; ``is_expanded`` chooses which icon the
    chevron shows. Child rows (``is_child=True``) never have
    children themselves under the two-level rule, so their chevron
    slot is occupied by an empty :class:`Gtk.Box` of equal width to
    keep the icon column aligned.

    The chevron's click handler does not propagate to the row
    activation (a :class:`Gtk.Button` consumes its own click event)
    so toggling expansion does not also change the selection.
    """
    box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, _ROW_SPACING_PX)
    if is_child:
        box.set_margin_start(_ROW_SPACING_PX + _CHILD_INDENT_PX)
    else:
        box.set_margin_start(_ROW_SPACING_PX)
    box.set_margin_end(_ROW_SPACING_PX)

    if has_children:
        chevron = Gtk.Button.new_from_icon_name(
            _CHEVRON_DOWN_ICON if is_expanded else _CHEVRON_RIGHT_ICON
        )
        chevron.set_has_frame(False)
        chevron.connect(
            "clicked",
            lambda _btn, nb_id=notebook.id: on_chevron_clicked(nb_id),
        )
        box.append(chevron)
    else:
        # Reserve the same horizontal space the chevron would
        # occupy so that parent and leaf rows align under the icon
        # column.
        spacer = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        spacer.set_size_request(_chevron_button_width(), -1)
        box.append(spacer)

    icon = Gtk.Image.new_from_icon_name(_icon_name_for_notebook(notebook.icon))
    box.append(icon)

    label = Gtk.Label.new(notebook.name)
    label.set_halign(Gtk.Align.START)
    label.set_hexpand(True)
    # Long notebook names get ellipsised at the end so the count
    # column on the right of the row is never pushed out of view.
    label.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(label)

    count_label = Gtk.Label.new(str(count))
    count_label.set_halign(Gtk.Align.END)
    box.append(count_label)

    return _SidebarRow(
        payload=_NotebookRowPayload(
            notebook_id=notebook.id,
            is_child=is_child,
            has_children=has_children,
        ),
        child=box,
    )


def _chevron_button_width() -> int:
    """Pixel width matching the chevron :class:`Gtk.Button`.

    Used by leaf-row layouts to reserve the same horizontal slot
    so labels align across rows. The actual button has GTK's
    default minimum width for icon-only buttons; this constant
    matches it closely enough that the eye reads the columns as
    aligned and is a single point of adjustment if a future style
    changes the chevron metrics.
    """
    return 24

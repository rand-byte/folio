"""The application's top header bar — search, mode switch, More menu.

Principles & invariants
-----------------------
* :class:`Toolbar` is the application's top bar — a
  :class:`Gtk.HeaderBar` populated with the controls the design
  shows in its titlebar:

  * a *New* button on the left that creates a fresh note in the
    most-relevant target notebook (selected notebook → current note's
    notebook → first notebook);
  * a global search entry that mirrors :attr:`AppState.query`
    bidirectionally;
  * a centre title widget that renders the breadcrumb of the
    currently-selected note (notebook trail + note title);
  * a View / Source segmented toggle on the right that mirrors
    :attr:`AppState.view_mode` bidirectionally;
  * a *More* menu button whose popover surfaces the *Duplicate note*
    and *Delete note* actions matching the design's three-dot menu.

  The toolbar sits in the window via :meth:`Gtk.Window.set_titlebar`
  on :class:`MainWindow`. Setting it as the title bar (rather than
  packing it inside the window's body) is the GTK 4 idiomatic
  choice — it composes correctly with the window's standard
  decorations (minimise / maximise / close), which the design also
  shows.

* Selection of the New button's target notebook is delegated to a
  pure helper, :func:`resolve_target_notebook`, so the policy is
  testable without GTK and matches the React reference's logic
  exactly. The widget's click handler is a thin shim over the
  helper.

* Breadcrumb computation is also a pure helper,
  :func:`compute_breadcrumb`, taking only a :class:`Note` and a
  ``dict[str, Notebook]``. This lets the function be unit-tested
  with literal dataclasses and removes the temptation to make the
  toolbar's GTK signal handlers do graph traversal inline. The
  toolbar refreshes the breadcrumb on every
  ``selected-note-changed`` from :class:`AppState` — a refresh on
  notebook rename is a future extension that will require listening
  to ``notebooks-changed`` from a controller, which is wired
  separately.

* The search entry and the mode toggle are bound *bidirectionally*
  to :class:`AppState`. To prevent the obvious update cycle (user
  types → search-changed → set_query → query-changed → set_text →
  search-changed) the toolbar uses a guard flag pattern matching
  the editor's ``_loading`` field: signal-driven *programmatic*
  updates set the flag to suppress the toolbar's own write back
  into :class:`AppState`. Note that :class:`AppState` setters are
  already idempotent (they emit only on a real change), but the
  guard buys a defence-in-depth that is cheap and obvious.

* The More menu is a :class:`Gtk.MenuButton` with a hand-built
  :class:`Gtk.Popover` containing :class:`Gtk.Button` rows — not a
  :class:`Gio.Menu` model. The action set is two items, both of
  which need access to ``self._app_state.selected_note_id`` at
  click time; using buttons lets the click handlers be plain
  Python methods, without registering :class:`Gio.Action`s on the
  application or window. The menu button is **disabled** when no
  note is selected, the same way the editor's image button is.

* The Delete action goes through an injected
  :data:`ConfirmDialogPresenter` so tests can drive the post-confirm
  code path synchronously, without spinning a real
  :class:`Gtk.AlertDialog`. Production wires
  :func:`default_confirm_dialog_presenter`. The injection pattern
  mirrors :data:`FileDialogOpener` from the editor's image picker.

* GTK 4 currency: :class:`Gtk.HeaderBar` (the GTK 4 successor to
  ``Gtk.HeaderBar`` in 3.x with the modernised pack API),
  :class:`Gtk.SearchEntry`, :class:`Gtk.MenuButton`,
  :class:`Gtk.ToggleButton.set_group` (rather than the deprecated
  ``Gtk.RadioButton``), :meth:`Gtk.Widget.get_root` (rather than
  the deprecated :meth:`Gtk.Widget.get_toplevel`).
"""

from __future__ import annotations

from typing import Final

import gi

gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk, Pango  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.enums import ViewMode
from notes_app.models.note import Note
from notes_app.models.notebook import Notebook
from notes_app.search.note_filter import NotebookSelection, Selection, SmartSelection
from notes_app.storage.protocols import (
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)
from notes_app.ui.dialogs import (
    ConfirmDialogPresenter,
    default_confirm_dialog_presenter,
)


# ---------------------------------------------------------------------------
# Visible labels and tooltips
# ---------------------------------------------------------------------------


_NEW_BUTTON_LABEL: Final[str] = "New"
_NEW_BUTTON_TOOLTIP: Final[str] = "New note (Ctrl+N)"

_SEARCH_PLACEHOLDER: Final[str] = "Search notes\u2026"
"""Placeholder string for the global search entry. Matches the design
(``placeholder="Search notes…"``); the ellipsis is the U+2026 single
character rather than three dots so it reads as a single glyph at
all font sizes."""

_MODE_VIEW_LABEL: Final[str] = "View"
_MODE_SOURCE_LABEL: Final[str] = "Source"

_MORE_BUTTON_TOOLTIP: Final[str] = "More"
_MORE_BUTTON_ICON: Final[str] = "view-more-symbolic"

_NEW_BUTTON_ICON: Final[str] = "list-add-symbolic"

_MENU_DUPLICATE_LABEL: Final[str] = "Duplicate note"
_MENU_DELETE_LABEL: Final[str] = "Delete note"

_BREADCRUMB_SEPARATOR: Final[str] = " \u203a "
"""The single-glyph separator between breadcrumb segments. Matches the
design (the Unicode ``SINGLE RIGHT-POINTING ANGLE QUOTATION MARK``,
U+203A) flanked by spaces."""

_DELETE_DIALOG_TITLE_FORMAT: Final[str] = 'Delete "{title}"?'
"""``str.format`` template for the confirm-delete dialog's primary
text. The note's title is interpolated into ``{title}`` — quoted in
the message itself so a long-title note still reads naturally."""

_DELETE_DIALOG_DETAIL: Final[str] = (
    "This note and its attachments will be removed. "
    "This cannot be undone."
)
"""Detail text for the confirm-delete dialog. Matches the React
reference (``app.jsx``)."""

_DELETE_DIALOG_CONFIRM_LABEL: Final[str] = "Delete"
"""The destructive button's label inside the confirm-delete dialog.
Single-word, capitalised — matches the design."""

_TOOLBAR_INNER_SPACING_PX: Final[int] = 6
"""Spacing between sibling widgets inside packed-start / packed-end
boxes. Matches the design's compact-mode spacing."""


# ---------------------------------------------------------------------------
# Pure helpers — testable without GTK
# ---------------------------------------------------------------------------


def resolve_target_notebook(
    *,
    selection: Selection,
    current_note: Note | None,
    notebooks: list[Notebook],
) -> str | None:
    """Return the id of the notebook a freshly-created note should land in.

    Pure function. The fallback chain matches the React reference
    (``app.jsx``):

    1. If the sidebar has a notebook selected, use that notebook id.
    2. Otherwise, if a note is currently being shown, use that note's
       notebook id (the user is "in" that notebook contextually).
    3. Otherwise, fall back to the first notebook in the repository's
       declaration order.
    4. If even that is empty (the library has no notebooks), return
       :data:`None`. Callers must not invoke ``create_note`` in that
       case; the toolbar's New button is disabled until a notebook
       exists.

    Tests pin every branch with literal dataclass inputs.
    """
    match selection:
        case NotebookSelection(notebook_id=nb_id):
            return nb_id
        case SmartSelection():
            pass
    if current_note is not None:
        return current_note.notebook_id
    if notebooks:
        return notebooks[0].id
    return None


def compute_breadcrumb(
    note: Note,
    notebooks_by_id: dict[str, Notebook],
) -> list[str]:
    """Build the breadcrumb trail for ``note`` as a list of strings.

    Returned list is in *trail order* — the topmost ancestor first,
    the note title last. The intermediate elements are notebook
    names. Under the two-level hierarchy invariant the trail length
    is at most three: ``[parent_notebook, child_notebook, note_title]``
    when the note's notebook is a child, ``[notebook, note_title]``
    when it is top-level, ``[note_title]`` when the notebook
    reference is broken (orphaned note — the storage layer's foreign
    keys make this practically impossible, but the helper is defensive).

    Pure. ``notebooks_by_id`` is a snapshot — no live look-ups, no
    repository imports — so tests can pass a literal dict. The
    toolbar builds the snapshot from
    :meth:`NotebookRepositoryProtocol.list_all` immediately before
    calling this helper.
    """
    crumbs: list[str] = []
    notebook = notebooks_by_id.get(note.notebook_id)
    if notebook is not None:
        if notebook.parent_id is not None:
            parent = notebooks_by_id.get(notebook.parent_id)
            if parent is not None:
                crumbs.append(parent.name)
        crumbs.append(notebook.name)
    crumbs.append(note.title)
    return crumbs


def format_breadcrumb(trail: list[str]) -> str:
    """Join a breadcrumb ``trail`` with the standard separator.

    Returns the empty string for an empty trail (the case where no
    note is selected), so the toolbar's title label can simply set
    its text to the result without an ``if`` branch around the
    no-selection case.
    """
    return _BREADCRUMB_SEPARATOR.join(trail)


# ---------------------------------------------------------------------------
# Toolbar widget
# ---------------------------------------------------------------------------


class Toolbar(  # pylint: disable=too-many-instance-attributes
    Gtk.HeaderBar,
):
    """The application's top header bar.

    The widget composes the New button, the search entry, the
    breadcrumb label, the View / Source mode toggle, and the More
    menu, then subscribes to the :class:`AppState` signals it needs
    to keep its visual state in sync with the rest of the
    application.

    The instance-attribute count exceeds pylint's default of seven
    because the toolbar carries six injected dependencies (two
    repositories, a controller, the app state, the dialog presenter)
    plus references to every widget whose state needs programmatic
    updates (search entry, breadcrumb label, two toggle buttons,
    the More menu button). Hiding any of these behind a "Bundle"
    object would obscure rather than clarify the toolbar's
    responsibilities.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _note_controller: NoteController
    _app_state: AppState
    _confirm_dialog_presenter: ConfirmDialogPresenter

    _search_entry: Gtk.SearchEntry
    _breadcrumb_label: Gtk.Label
    _view_button: Gtk.ToggleButton
    _source_button: Gtk.ToggleButton
    _more_menu_button: Gtk.MenuButton
    _more_popover: Gtk.Popover

    _suppress_signal_writeback: bool

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        note_repository: NoteRepositoryProtocol,
        notebook_repository: NotebookRepositoryProtocol,
        note_controller: NoteController,
        app_state: AppState,
        confirm_dialog_presenter: ConfirmDialogPresenter = (
            default_confirm_dialog_presenter
        ),
    ) -> None:
        super().__init__()
        self._note_repository = note_repository
        self._notebook_repository = notebook_repository
        self._note_controller = note_controller
        self._app_state = app_state
        self._confirm_dialog_presenter = confirm_dialog_presenter
        self._suppress_signal_writeback = False

        # ---------- left side: New button + search entry ----------
        new_button = self._build_new_button()
        self._search_entry = self._build_search_entry()
        left_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        left_box.append(new_button)
        left_box.append(self._search_entry)
        self.pack_start(left_box)

        # ---------- centre: breadcrumb label ----------
        self._breadcrumb_label = self._build_breadcrumb_label()
        self.set_title_widget(self._breadcrumb_label)

        # ---------- right side: View/Source segmented + More ----------
        self._view_button, self._source_button = self._build_mode_toggle()
        self._more_popover = self._build_more_popover()
        self._more_menu_button = self._build_more_menu_button(self._more_popover)

        right_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        mode_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        mode_box.append(self._view_button)
        mode_box.append(self._source_button)
        right_box.append(mode_box)
        right_box.append(self._more_menu_button)
        # ``pack_end`` packs from the right-hand side, *outermost
        # first*. Packing ``right_box`` once means everything we
        # appended to it lands as a unit at the right edge.
        self.pack_end(right_box)

        # ---------- AppState subscriptions ----------
        # Selection and selected-note both feed into the breadcrumb
        # and the More menu's sensitivity. Query feeds the search
        # entry. View mode feeds the toggle group. Each subscription
        # is a one-way "AppState → widget" update; user-driven
        # writes back into AppState happen inside the per-widget
        # handlers above.
        self._app_state.connect(
            "selected-note-changed",
            self._on_selected_note_changed,
        )
        self._app_state.connect(
            "selection-changed",
            self._on_selection_changed,
        )
        self._app_state.connect(
            "query-changed",
            self._on_app_state_query_changed,
        )
        self._app_state.connect(
            "view-mode-changed",
            self._on_app_state_view_mode_changed,
        )

        # ---------- initial state ----------
        # All four bindings flush their starting values into the
        # widgets once at construction so the toolbar opens
        # consistent with whatever ``AppState`` was holding before
        # the toolbar existed.
        self._refresh_breadcrumb()
        self._refresh_more_menu_sensitivity()
        self._sync_search_entry_from_app_state()
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Construction helpers — one method per child widget
    # ------------------------------------------------------------------

    def _build_new_button(self) -> Gtk.Button:
        """Build the *New note* button (left of the search entry)."""
        button = Gtk.Button.new()
        # Pair an icon with the text label so the button matches
        # the design's compact-with-icon look. ``Gtk.Box`` is the
        # GTK 4 idiomatic way to combine the two on a button.
        content = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX // 2,
        )
        content.append(Gtk.Image.new_from_icon_name(_NEW_BUTTON_ICON))
        content.append(Gtk.Label.new(_NEW_BUTTON_LABEL))
        button.set_child(content)
        button.set_tooltip_text(_NEW_BUTTON_TOOLTIP)
        button.connect("clicked", self._on_new_clicked)
        return button

    def _build_search_entry(self) -> Gtk.SearchEntry:
        """Build the global search entry."""
        entry = Gtk.SearchEntry.new()
        entry.set_placeholder_text(_SEARCH_PLACEHOLDER)
        # ``search-changed`` is the debounced signal emitted shortly
        # after the user stops typing. Hooking the cheaper
        # ``changed`` instead would fire for every keystroke and is
        # not the design's intended behaviour.
        entry.connect("search-changed", self._on_search_changed)
        return entry

    def _build_breadcrumb_label(self) -> Gtk.Label:
        """Build the centre breadcrumb label."""
        label = Gtk.Label.new("")
        label.set_halign(Gtk.Align.CENTER)
        # Long breadcrumbs ellipsise at the start so the note title
        # at the right-hand end remains visible — the title is the
        # most useful segment and is also the rightmost.
        label.set_ellipsize(Pango.EllipsizeMode.START)
        return label

    def _build_mode_toggle(
        self,
    ) -> tuple[Gtk.ToggleButton, Gtk.ToggleButton]:
        """Build the View / Source segmented toggle pair.

        Returns the two buttons in the order *View, Source* so
        construction-site code can pack them into a horizontal box
        in a predictable order.
        """
        view_button = Gtk.ToggleButton.new_with_label(_MODE_VIEW_LABEL)
        source_button = Gtk.ToggleButton.new_with_label(_MODE_SOURCE_LABEL)
        # ``set_group`` makes the pair behave like a radio group:
        # exactly one is active at a time, and a click on the
        # currently-active button is a no-op (cannot deactivate).
        # This is the right behaviour — the user always wants
        # *some* mode active.
        source_button.set_group(view_button)
        view_button.connect("toggled", self._on_view_toggle_changed)
        source_button.connect("toggled", self._on_source_toggle_changed)
        return view_button, source_button

    def _build_more_popover(self) -> Gtk.Popover:
        """Build the popover containing Duplicate / Delete buttons."""
        popover = Gtk.Popover.new()
        contents = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)

        duplicate_button = Gtk.Button.new_with_label(_MENU_DUPLICATE_LABEL)
        # Buttons inside a menu-style popover read better without
        # frames so the rows look like menu items rather than a
        # column of standalone widgets.
        duplicate_button.set_has_frame(False)
        duplicate_button.connect("clicked", self._on_duplicate_clicked)
        contents.append(duplicate_button)

        delete_button = Gtk.Button.new_with_label(_MENU_DELETE_LABEL)
        delete_button.set_has_frame(False)
        # ``destructive-action`` is the GTK 4 CSS class that themes
        # apply a red accent to. The button now reads as the
        # destructive option without any per-app stylesheet.
        delete_button.add_css_class("destructive-action")
        delete_button.connect("clicked", self._on_delete_clicked)
        contents.append(delete_button)

        popover.set_child(contents)
        return popover

    def _build_more_menu_button(
        self,
        popover: Gtk.Popover,
    ) -> Gtk.MenuButton:
        """Build the More menu trigger button."""
        button = Gtk.MenuButton.new()
        button.set_icon_name(_MORE_BUTTON_ICON)
        button.set_tooltip_text(_MORE_BUTTON_TOOLTIP)
        button.set_popover(popover)
        return button

    # ------------------------------------------------------------------
    # User-driven event handlers — write into AppState / controllers
    # ------------------------------------------------------------------

    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        """Handle a click on the New button.

        Resolves the target notebook via :func:`resolve_target_notebook`
        and asks the controller to create a note. After creation the
        toolbar nudges :class:`AppState` into edit mode so the user
        can immediately start typing — matching the React reference's
        ``setMode("edit")`` after ``createNote()``.

        If no notebook exists yet, the call is a no-op. The button
        could be disabled in that case, but for v1 the seed-data
        migration guarantees notebooks exist on first launch — the
        no-op is defence-in-depth, not a normal code path.
        """
        target = resolve_target_notebook(
            selection=self._app_state.selection,
            current_note=self._current_note(),
            notebooks=self._notebook_repository.list_all(),
        )
        if target is None:
            return
        self._note_controller.create_note(target)
        self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """User typed in the search entry → mirror into AppState.

        Guarded by the suppression flag so that a programmatic
        update from :meth:`_on_app_state_query_changed` does not
        ricochet back into a redundant ``set_query`` call. The
        guard is belt-and-braces (``set_query`` is itself
        idempotent), but making the rule explicit at every
        write-site is cheaper than reasoning about edge cases.
        """
        if self._suppress_signal_writeback:
            return
        self._app_state.set_query(entry.get_text())

    def _on_view_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        """User clicked the View toggle. Drive the mode if it became active."""
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.VIEW)

    def _on_source_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        """User clicked the Source toggle. Drive the mode if it became active."""
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_duplicate_clicked(self, _button: Gtk.Button) -> None:
        """Duplicate the currently selected note via the controller."""
        self._more_popover.popdown()
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        self._note_controller.duplicate_note(note_id)

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        """Open the confirm-delete dialog for the currently selected note.

        The popover is popped down before the dialog is presented
        so the two transient surfaces don't fight each other for
        focus. The note-title lookup happens upstream of the
        dialog presenter (so a stale id surfaces a fast no-op
        rather than an empty-titled prompt).
        """
        self._more_popover.popdown()
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        try:
            note = self._note_repository.get(note_id)
        except KeyError:
            # Selection points at a deleted note — nothing to delete.
            return

        captured_note_id = note.id

        def on_confirm_result(confirmed: bool) -> None:
            if confirmed:
                self._note_controller.request_delete(captured_note_id)

        self._confirm_dialog_presenter(
            self._parent_window(),
            _DELETE_DIALOG_TITLE_FORMAT.format(title=note.title),
            _DELETE_DIALOG_DETAIL,
            _DELETE_DIALOG_CONFIRM_LABEL,
            on_confirm_result,
        )

    # ------------------------------------------------------------------
    # AppState-driven handlers — programmatic widget refreshes
    # ------------------------------------------------------------------

    def _on_selected_note_changed(self, _state: AppState) -> None:
        """Selection changed → refresh breadcrumb and menu sensitivity."""
        self._refresh_breadcrumb()
        self._refresh_more_menu_sensitivity()

    def _on_selection_changed(self, _state: AppState) -> None:
        """Sidebar selection changed.

        The breadcrumb only depends on the *note*, not on the
        sidebar selection, but the menu sensitivity does not change
        either. Currently this handler is a no-op shim — its
        presence reserves a single subscription site so future
        breadcrumb refinements (e.g. showing the selected smart
        filter when no note is selected) have a place to land
        without re-walking the connection topology.
        """

    def _on_app_state_query_changed(self, _state: AppState) -> None:
        """AppState query changed externally → push it into the entry."""
        self._sync_search_entry_from_app_state()

    def _on_app_state_view_mode_changed(self, _state: AppState) -> None:
        """AppState view mode changed externally → reflect in the toggles."""
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Refresh / sync helpers
    # ------------------------------------------------------------------

    def _refresh_breadcrumb(self) -> None:
        """Recompute the breadcrumb text from current state."""
        note = self._current_note()
        if note is None:
            self._breadcrumb_label.set_text("")
            return
        notebooks_by_id = {
            nb.id: nb for nb in self._notebook_repository.list_all()
        }
        trail = compute_breadcrumb(note, notebooks_by_id)
        self._breadcrumb_label.set_text(format_breadcrumb(trail))

    def _refresh_more_menu_sensitivity(self) -> None:
        """Disable the More menu button when there is no selected note.

        With no note selected the Duplicate and Delete actions are
        meaningless. Greying out the entire menu trigger is clearer
        than offering a popover whose buttons would be no-ops.
        """
        self._more_menu_button.set_sensitive(
            self._app_state.selected_note_id is not None
        )

    def _sync_search_entry_from_app_state(self) -> None:
        """Push :attr:`AppState.query` into the search entry."""
        self._suppress_signal_writeback = True
        try:
            self._search_entry.set_text(self._app_state.query)
        finally:
            self._suppress_signal_writeback = False

    def _sync_mode_toggle_from_app_state(self) -> None:
        """Push :attr:`AppState.view_mode` into the toggle pair."""
        self._suppress_signal_writeback = True
        try:
            mode = self._app_state.view_mode
            self._view_button.set_active(mode == ViewMode.VIEW)
            self._source_button.set_active(mode == ViewMode.EDIT)
        finally:
            self._suppress_signal_writeback = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_note(self) -> Note | None:
        """Look up the currently selected note, or :data:`None`.

        ``None`` is returned both when nothing is selected and when
        the selection points at a row that no longer exists in the
        repository. The caller treats both the same way.
        """
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return None
        try:
            return self._note_repository.get(note_id)
        except KeyError:
            return None

    def _parent_window(self) -> Gtk.Window | None:
        """Return the enclosing :class:`Gtk.Window` or :data:`None`.

        Used as the modal parent for the confirm-delete dialog. A
        ``None`` root is acceptable (the dialog is still shown,
        just without modal anchoring) — production always has a
        root because the toolbar is set as the window's titlebar.
        """
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    # ------------------------------------------------------------------
    # Read-only properties exposed for tests
    # ------------------------------------------------------------------

    @property
    def search_entry(self) -> Gtk.SearchEntry:
        """The toolbar's :class:`Gtk.SearchEntry` widget."""
        return self._search_entry

    @property
    def breadcrumb_label(self) -> Gtk.Label:
        """The toolbar's centre breadcrumb label."""
        return self._breadcrumb_label

    @property
    def view_button(self) -> Gtk.ToggleButton:
        """The View half of the View/Source mode toggle."""
        return self._view_button

    @property
    def source_button(self) -> Gtk.ToggleButton:
        """The Source half of the View/Source mode toggle."""
        return self._source_button

    @property
    def more_menu_button(self) -> Gtk.MenuButton:
        """The More menu trigger button."""
        return self._more_menu_button

    @property
    def more_popover(self) -> Gtk.Popover:
        """The popover that surfaces Duplicate / Delete actions."""
        return self._more_popover

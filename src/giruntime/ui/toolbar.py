"""The application's top header bar — New, Delete, search, mode switch, Help.

Principles & invariants
-----------------------
* :class:`Toolbar` is the application's top bar — a
  :class:`Gtk.HeaderBar` populated with the controls the design
  shows in its titlebar, **all surfaced directly with no overflow
  menus**:

  * a *New* button on the left that creates a fresh note pre-filled
    with the currently selected tag set (when the sidebar has a tag
    selection) and selects it for editing;
  * a *Delete* button immediately after *New* — a standalone
    trash-icon button (icon-only, tooltip-labelled) so the two
    note-lifecycle actions sit together. It is **separate** from the
    *New* button (its own border, a gap between them) rather than
    linked, so the pair does not read as a single split/dropdown
    button;
  * a global search entry that mirrors :attr:`AppState.query`
    bidirectionally;
  * an empty centre title widget (no breadcrumb — the tag-based
    library has no hierarchy to surface);
  * a View / Source segmented toggle on the right that mirrors
    :attr:`AppState.view_mode` bidirectionally;
  * a *Help* button on the right (icon + ``Syntax`` label) that opens
    the AsciiDoc syntax reference by activating the app-scoped
    ``app.help`` action.

  The toolbar sits in the window via :meth:`Gtk.Window.set_titlebar`
  on :class:`MainWindow`.

* There are **no menu buttons**. An earlier design split actions across
  a note-scoped *More* popover (Duplicate / Delete) and an app-scoped
  primary (hamburger) menu (Help); both are gone. *Duplicate* was
  dropped, and *Delete* and *Help* were promoted to first-class
  toolbar buttons so every action is visible and one click away.

* The +New button's seed source is produced by
  :func:`controllers.note_controller.make_initial_source`, which
  inspects the current :class:`AppState` selection and returns a
  ``:tags: a, b`` pre-fill when a non-empty
  :class:`TagSelection` is active. The toolbar's click handler is a
  thin shim over the helper plus a switch to
  :data:`ViewMode.EDIT`.

* The search entry's ``text`` is bound *bidirectionally* to
  :attr:`AppState.query` through a :meth:`GObject.Object.bind_property`
  binding established at construction (with
  :data:`GObject.BindingFlags.SYNC_CREATE` for the initial
  ``query → text`` copy). GObject's own reverse-echo suppression breaks
  the update cycle without a hand-rolled guard, and — crucially —
  avoids the re-entrant ``set_text`` that used to reset the entry's
  cursor and reverse typed characters. The binding's correctness relies
  on ``query`` being stored verbatim (see the :class:`AppState`
  invariants); any normalisation there would reintroduce the cursor bug.

* The View / Source toggle maps the :class:`ViewMode` enum onto two
  :class:`Gtk.ToggleButton` ``active`` booleans, which is not a clean
  single-property bind, so it keeps explicit handlers. To prevent its
  update cycle (programmatic ``set_active`` → ``toggled`` →
  ``set_view_mode`` → ``notify::view-mode`` → ``set_active``) those
  handlers are fenced by the ``_suppress_signal_writeback`` guard flag,
  matching the editor's ``_loading`` field.

* The *Delete* button is **note-scoped**: it is insensitive when no
  note is selected (the sensitivity rule the removed *More* menu
  carried) and goes through an injected
  :data:`ConfirmDialogPresenter`. Production wires
  :func:`default_confirm_dialog_presenter`. It is styled quietly (no
  ``destructive-action`` accent) — the confirmation dialog, not the
  toolbar icon, is where the destructive weight belongs.

* The *Help* button is **app-scoped**: it carries no click handler and
  is never note-dependent. It points at the ``app.help`` action via
  :meth:`Gtk.Actionable.set_action_name` — the same action the ``F1``
  accelerator triggers — so GTK activates it when the button is clicked.
  The action and its accelerator are registered by
  :class:`giruntime.ui.application.NotesApplication`; the button only
  references them. GTK exposes application actions to every widget in
  the window under the ``app.`` prefix, so the reference resolves once
  the toolbar is in the window's hierarchy.

* GTK 4 currency: :class:`Gtk.HeaderBar`, :class:`Gtk.SearchEntry`,
  :class:`Gtk.Button`, :meth:`Gtk.Button.set_icon_name`,
  :meth:`Gtk.Actionable.set_action_name`,
  :meth:`Gtk.ToggleButton.set_group`, :meth:`Gtk.Widget.get_root`.
"""

from __future__ import annotations

from typing import Final

from gi.repository import GObject, Gtk

from enums import ViewMode
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController, make_initial_source
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.dialogs import (
    ConfirmDialogPresenter,
    default_confirm_dialog_presenter,
)


# ---------------------------------------------------------------------------
# Visible labels and tooltips
# ---------------------------------------------------------------------------


_NEW_BUTTON_LABEL: Final[str] = "New"
_NEW_BUTTON_TOOLTIP: Final[str] = "New note (Ctrl+N)"
_NEW_BUTTON_ICON: Final[str] = "list-add-symbolic"

_DELETE_BUTTON_TOOLTIP: Final[str] = "Delete note"
_DELETE_BUTTON_ICON: Final[str] = "user-trash-symbolic"

_SEARCH_PLACEHOLDER: Final[str] = "Search notes\u2026"

_MODE_VIEW_LABEL: Final[str] = "View"
_MODE_SOURCE_LABEL: Final[str] = "Source"

_HELP_BUTTON_LABEL: Final[str] = "Syntax"
_HELP_BUTTON_TOOLTIP: Final[str] = "AsciiDoc syntax help (F1)"
_HELP_BUTTON_ICON: Final[str] = "help-about-symbolic"
_HELP_ACTION_DETAILED_NAME: Final[str] = "app.help"
"""Detailed name of the application-level help action the *Help* button
activates. The action and its ``F1`` accelerator are registered by
:class:`giruntime.ui.application.NotesApplication`; the button only
points at it via :meth:`Gtk.Actionable.set_action_name`, keeping Help
app-scoped (always available) rather than note-scoped like *Delete*."""

_DELETE_DIALOG_TITLE_FORMAT: Final[str] = 'Delete "{title}"?'
_DELETE_DIALOG_DETAIL: Final[str] = (
    "This note and its attachments will be removed. "
    "This cannot be undone."
)
_DELETE_DIALOG_CONFIRM_LABEL: Final[str] = "Delete"

_TOOLBAR_INNER_SPACING_PX: Final[int] = 6
"""Spacing between sibling widgets inside packed-start / packed-end boxes."""


# ---------------------------------------------------------------------------
# Toolbar widget
# ---------------------------------------------------------------------------


class Toolbar(  # pylint: disable=too-many-instance-attributes
    Gtk.HeaderBar,
):
    """The application's top header bar."""

    _note_store: NoteListStore
    _note_controller: NoteController
    _app_state: AppState
    _confirm_dialog_presenter: ConfirmDialogPresenter

    _search_entry: Gtk.SearchEntry
    _query_binding: GObject.Binding
    _view_button: Gtk.ToggleButton
    _source_button: Gtk.ToggleButton
    _delete_button: Gtk.Button
    _help_button: Gtk.Button

    _suppress_signal_writeback: bool

    def __init__(
        self,
        *,
        note_store: NoteListStore,
        note_controller: NoteController,
        app_state: AppState,
        confirm_dialog_presenter: ConfirmDialogPresenter = (
            default_confirm_dialog_presenter
        ),
    ) -> None:
        super().__init__()
        self._note_store = note_store
        self._note_controller = note_controller
        self._app_state = app_state
        self._confirm_dialog_presenter = confirm_dialog_presenter
        self._suppress_signal_writeback = False

        # ---------- left side: New + Delete + search entry ----------
        # New and Delete are the two note-lifecycle actions, kept
        # adjacent but as separate buttons (not a linked pair) so they
        # do not read as one split button.
        new_button = self._build_new_button()
        self._delete_button = self._build_delete_button()
        self._search_entry = self._build_search_entry()
        left_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        left_box.append(new_button)
        left_box.append(self._delete_button)
        left_box.append(self._search_entry)
        self.pack_start(left_box)

        # ---------- centre: intentionally empty ----------
        # No breadcrumb in the tag-based library. An empty label is set
        # as the title widget so GTK does not auto-fill the centre slot
        # with the window title (which would duplicate the OS title bar).
        self.set_title_widget(Gtk.Label.new(""))

        # ---------- right side: View/Source segmented + Help ----------
        self._view_button, self._source_button = self._build_mode_toggle()
        self._help_button = self._build_help_button()

        right_box = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX,
        )
        mode_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        mode_box.append(self._view_button)
        mode_box.append(self._source_button)
        right_box.append(mode_box)
        right_box.append(self._help_button)
        self.pack_end(right_box)

        # ---------- AppState bindings & subscriptions ----------
        # The search entry round-trips with AppState.query through a
        # bidirectional property binding. SYNC_CREATE performs the
        # initial query -> text copy, so no explicit sync call is needed.
        # GObject suppresses the reverse echo within a propagation cycle,
        # which is what removes the re-entrant set_text() that used to
        # reset the cursor and reverse typed characters.
        self._query_binding = self._app_state.bind_property(
            "query",
            self._search_entry,
            "text",
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.SYNC_CREATE,
        )
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_changed,
        )
        self._app_state.connect(
            "notify::view-mode",
            self._on_app_state_view_mode_changed,
        )

        # ---------- initial state ----------
        # The search entry is seeded by the binding's SYNC_CREATE above.
        self._refresh_delete_sensitivity()
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Construction helpers — one method per child widget
    # ------------------------------------------------------------------

    def _build_new_button(self) -> Gtk.Button:
        button = Gtk.Button.new()
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

    def _build_delete_button(self) -> Gtk.Button:
        """Build the standalone, note-scoped *Delete* button.

        Icon-only (a trash can) with a tooltip carrying the label, so it
        is compact yet accessible. It is **not** given the
        ``destructive-action`` accent: the toolbar icon stays quiet and
        the confirmation dialog supplies the destructive weight.
        """
        button = Gtk.Button.new()
        button.set_icon_name(_DELETE_BUTTON_ICON)
        button.set_tooltip_text(_DELETE_BUTTON_TOOLTIP)
        button.connect("clicked", self._on_delete_clicked)
        return button

    def _build_search_entry(self) -> Gtk.SearchEntry:
        entry = Gtk.SearchEntry.new()
        entry.set_placeholder_text(_SEARCH_PLACEHOLDER)
        return entry

    def _build_mode_toggle(
        self,
    ) -> tuple[Gtk.ToggleButton, Gtk.ToggleButton]:
        view_button = Gtk.ToggleButton.new_with_label(_MODE_VIEW_LABEL)
        source_button = Gtk.ToggleButton.new_with_label(_MODE_SOURCE_LABEL)
        source_button.set_group(view_button)
        view_button.connect("toggled", self._on_view_toggle_changed)
        source_button.connect("toggled", self._on_source_toggle_changed)
        return view_button, source_button

    def _build_help_button(self) -> Gtk.Button:
        """Build the app-scoped *Help* button.

        An icon + ``Syntax`` label that points at the application-level
        ``app.help`` action — the same action the ``F1`` accelerator
        triggers — via :meth:`Gtk.Actionable.set_action_name`. The button
        carries no click handler: GTK activates the referenced action on
        click and resolves ``app.help`` against the window's application.
        Because it targets an always-enabled app action rather than a
        per-note one, it is never note-scoped.
        """
        button = Gtk.Button.new()
        content = Gtk.Box.new(
            Gtk.Orientation.HORIZONTAL,
            _TOOLBAR_INNER_SPACING_PX // 2,
        )
        content.append(Gtk.Image.new_from_icon_name(_HELP_BUTTON_ICON))
        content.append(Gtk.Label.new(_HELP_BUTTON_LABEL))
        button.set_child(content)
        button.set_tooltip_text(_HELP_BUTTON_TOOLTIP)
        button.set_action_name(_HELP_ACTION_DETAILED_NAME)
        return button

    # ------------------------------------------------------------------
    # User-driven event handlers — write into AppState / controllers
    # ------------------------------------------------------------------

    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        """Create a note pre-filled from the current selection."""
        initial = make_initial_source(self._app_state.selection)
        self._note_controller.create_note(initial)
        self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_view_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.VIEW)

    def _on_source_toggle_changed(self, button: Gtk.ToggleButton) -> None:
        if self._suppress_signal_writeback:
            return
        if button.get_active():
            self._app_state.set_view_mode(ViewMode.EDIT)

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        note_id = self._app_state.selected_note_id
        if note_id is None:
            return
        try:
            note = self._note_store.get_note(note_id)
        except KeyError:
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

    def _on_selected_note_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._refresh_delete_sensitivity()

    def _on_app_state_view_mode_changed(
        self,
        _state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._sync_mode_toggle_from_app_state()

    # ------------------------------------------------------------------
    # Refresh / sync helpers
    # ------------------------------------------------------------------

    def _refresh_delete_sensitivity(self) -> None:
        self._delete_button.set_sensitive(
            self._app_state.selected_note_id is not None
        )

    def _sync_mode_toggle_from_app_state(self) -> None:
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

    def _parent_window(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    # ------------------------------------------------------------------
    # Read-only properties exposed for tests
    # ------------------------------------------------------------------

    @property
    def search_entry(self) -> Gtk.SearchEntry:
        return self._search_entry

    @property
    def view_button(self) -> Gtk.ToggleButton:
        return self._view_button

    @property
    def source_button(self) -> Gtk.ToggleButton:
        return self._source_button

    @property
    def delete_button(self) -> Gtk.Button:
        return self._delete_button

    @property
    def help_button(self) -> Gtk.Button:
        return self._help_button

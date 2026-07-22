"""Top-level application window — the three-pane shell.

Principles & invariants
-----------------------
* :class:`MainWindow` is the application's single top-level window.
  As of build step 10 the right pane is a :class:`Gtk.Stack` switching
  between :class:`NoteView` (rendered prose) and :class:`NoteEditor`
  (the source editor) keyed on :attr:`AppState.view_mode`. The
  toolbar that exposes the View/Source toggle to the user lands at
  step 12; today the swap is driven directly by mutating
  :attr:`AppState.view_mode`, which is the same plumbing the toolbar
  will use.
* Layout is a pair of nested :class:`Gtk.Paned` widgets, both
  horizontal. The outer paned splits *sidebar | rest*; its end-child
  is the inner paned, which splits *note list | view-or-editor stack*.
  Two nested panes give the user two independent drag handles —
  exactly the design's behaviour. :meth:`Gtk.Paned.set_start_child`
  and :meth:`Gtk.Paned.set_end_child` are the GTK 4 way to populate
  a paned; the older ``pack1`` / ``pack2`` API was deprecated in 4.0.
* As of build step 12 a :class:`Toolbar` is set as the window's
  title bar via :meth:`Gtk.Window.set_titlebar`. The header bar
  carries the New button, the global search entry, the breadcrumb
  label, the View / Source segmented toggle, and the More menu —
  matching the design's titlebar. ``set_titlebar`` is independent
  of ``set_child``, so the existing outer-paned-as-child invariant
  is preserved unchanged.
* This widget owns a **single** signal subscription:
  ``AppState:notify::view-mode``. On every mode change the window
  flushes the editor's pending autosave (so any just-typed edits hit
  disk under the current note id) and asks the view to refresh from the
  in-memory store before the stack swap reveals it. Flush + refresh are
  both idempotent, so doing them on every mode change — not just on the
  EDIT→VIEW direction — keeps the dispatch branch-free without paying
  any extra cost on the no-op path.

  The same flush is exposed as :meth:`flush_editor` for the
  application-shutdown path: window close ends the process, so a save
  still inside the editor's debounce window would be lost without it.
  :class:`giruntime.ui.application.NotesApplication` calls it from its
  ``close-request`` handler before quitting. The window keeps the flush
  behind a named method rather than reaching into the editor from the
  application, so the editor stays the window's private child.

  There is no longer a ``NoteController::notes-changed`` fan-out: the
  note list, the rendered view, and the sidebar all update by observing
  the store's ``items-changed`` (directly or through the derived
  :class:`controllers.tag_counts_model.TagCountsModel`), so a create /
  edit / delete propagates without the window arbitrating. Per-pane
  selection plumbing likewise belongs to the panes; the window's
  surface stays minimal, owning only the layout and the view-mode
  dispatch.
* :class:`NoteEditor` and :class:`NoteView` both stay constructed
  and live across mode switches. Tearing one down on every toggle
  would discard the editor's undo history and the view's child
  anchors (a non-trivial cost for images and tables once those land).
  GTK's :class:`Gtk.Stack` simply hides the inactive child — both
  remain wired to :class:`AppState` for selection updates so a
  freshly-revealed pane is always up-to-date.
* The editor pane subscribes to ``notify::selected-note-id`` like any
  other pane, but with the added invariant that selection-change
  flushes any pending auto-save *before* the buffer is overwritten.
  That guarantee lives inside :class:`NoteEditor`; this window is
  blissfully unaware of it.
* The construction signature is the long-term one: caller
  (:class:`NotesApplication`) passes ``application``, ``note_store``,
  ``note_controller``, ``app_state``, and ``attachment_store``, all
  keyword-only. ``attachment_store`` is optional with a ``None``
  default so the existing per-pane test suites that pre-date the
  attachment build keep constructing :class:`MainWindow` without that
  injected dependency; in that mode :class:`NoteView` falls back to its
  placeholder image resolver.
* The initial pane positions match the per-widget hints
  (:data:`_SIDEBAR_INITIAL_POSITION_PX`,
  :data:`_NOTE_LIST_INITIAL_POSITION_PX`), and the initial *window
  width* — when no size was restored from a previous run (see below) —
  is derived from them plus the rendered article column via
  :func:`_default_window_width`, called once after :class:`NoteView`
  has measured the body font (:meth:`NoteView.preferred_column_width_px`).
  This guarantees the fixed-width column opens fully visible and
  centred rather than overflowing into a horizontal scroll — the
  derived width scales with the font instead of being a literal guess.
  The pane positions themselves are always the fixed initial hints —
  restoring a paned-divider position is out of scope for the current
  session-state feature. Once the user drags either handle or resizes
  the window, GTK records the new value internally and our defaults
  stop applying for the rest of that run.
* The constructor's ``restored_state`` parameter is the window's half
  of :class:`SessionState`
  (:mod:`giruntime.ui.application`'s :class:`NotesApplication` owns the
  other half — the initial note selection). When
  ``restored_state.window_size`` is set, it replaces the computed
  ``_default_window_width(...)`` × :data:`_DEFAULT_WINDOW_HEIGHT_PX`
  pair above; when ``restored_state.window_maximized`` is true, the
  window additionally calls :meth:`Gtk.Window.maximize` once, after
  :meth:`Gtk.Window.set_default_size` — maximizing before the window is
  ever shown is documented GTK behaviour ("the window will be maximized
  when it appears onscreen initially"), and the pre-maximize size is
  still worth setting first so there is a sane size to fall back to if
  the window is ever un-maximized. GTK 4 has no window**-position** API
  (Wayland compositors own placement, not the app), so there is
  deliberately nothing to restore there — only size and maximized
  state. ``restored_state`` defaults to
  :data:`models.session_state.DEFAULT_SESSION_STATE` for the same
  reason ``attachment_store`` defaults to ``None`` below: existing
  per-pane test suites construct :class:`MainWindow` directly and
  should not all need to know about session restoration to keep
  working.
* The window owns no data. Everything it needs — the note store, the
  controller, the app state — is reached through references that
  originate in :class:`NotesApplication`. Tests can construct the same
  widget with the same in-memory store the per-pane tests use, without
  touching the file system or the database.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from gi.repository import Gio, GObject, Gtk

from enums import ViewMode, WindowAction, window_action_detailed_name
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui.note_editor import NoteEditor
from giruntime.ui.note_list import NoteList
from giruntime.ui.note_view import NoteView
from giruntime.ui.sidebar import Sidebar
from giruntime.ui.toolbar import Toolbar
from models.session_state import DEFAULT_SESSION_STATE, SessionState
from storage.protocols import AttachmentStoreProtocol


# ---------------------------------------------------------------------------
# Constants — window and paned-position defaults
# ---------------------------------------------------------------------------


_DEFAULT_WINDOW_HEIGHT_PX: Final[int] = 800
"""Initial window height. Matches the design's roomy default."""

_PANED_HANDLE_ALLOWANCE_PX: Final[int] = 24
"""Horizontal space reserved for the two ``Gtk.Paned`` drag handles.

The layout has a divider between *sidebar | rest* and another between
*note list | stack*; neither handle belongs to any pane, so the read
pane loses a few pixels to each. This is a deliberately generous flat
allowance (not a measured handle width) so the default-width formula
in :func:`_default_window_width` never under-reserves and pushes the
article column into a horizontal scroll on first show. Any rounding
gap between a paned's *position* and the start child's allocated width
is absorbed here too.
"""

_ARTICLE_SIDE_SLACK_PX: Final[int] = 96
"""Breathing room added beyond the article column at the default size.

:meth:`ArticleContainer.do_size_allocate` only centres the column when
the read pane is *strictly wider* than the column. Sizing the pane to
exactly the column width would leave the column edge-to-edge and skip
that centring branch on the first allocation. This slack guarantees
the pane opens wider than the column — splitting roughly in half it is
~48px of gutter on each side — so the column starts centred, which is
the behaviour the old fixed-1200 default claimed but did not deliver.
"""

_MIN_DEFAULT_WINDOW_WIDTH_PX: Final[int] = 1000
"""Floor for the computed default window width.

The article-column term in :func:`_default_window_width` is *measured*
at runtime (it scales with the body font), and the underlying M-width
measurer falls back to a tiny value if it cannot read a real font (see
:data:`ui.note_view._FALLBACK_CHAR_WIDTH_PX`). The floor
keeps a degenerate measurement from opening an unusably narrow window.
Under any normal font the computed sum exceeds this floor, so it only
ever matters as insurance.
"""

_SIDEBAR_INITIAL_POSITION_PX: Final[int] = 220
"""Initial position of the outer paned divider.

Equals the sidebar's preferred width hint
(:data:`ui.sidebar._DEFAULT_PANE_WIDTH_PX`). Setting both
to the same value avoids a momentary re-layout on first show: the
paned starts where the sidebar wants to be.
"""

_NOTE_LIST_INITIAL_POSITION_PX: Final[int] = 320
"""Initial position of the inner paned divider.

Same reasoning as the sidebar — matches
:data:`ui.note_list._DEFAULT_PANE_WIDTH_PX`.
"""

_WINDOW_TITLE: Final[str] = "Notes"
"""Window title shown in the title bar / Wayland compositor.

Augmented with the current note's title once the toolbar arrives at
step 12; kept as a constant rather than a magic string so the search
for "places that affect the title" is one grep target.
"""

_STACK_NAME_VIEW: Final[str] = "view"
"""Name of the rendered-view child within the right-pane stack.

Stack children are addressed by name in
:meth:`Gtk.Stack.set_visible_child_name`, so a stable string
constant per child is what makes the view-mode dispatch resilient
to future refactors that re-order the children.
"""

_STACK_NAME_EDIT: Final[str] = "edit"
"""Name of the source-editor child within the right-pane stack."""


_MODE_TO_STACK_NAME: Final[dict[ViewMode, str]] = {
    ViewMode.VIEW: _STACK_NAME_VIEW,
    ViewMode.EDIT: _STACK_NAME_EDIT,
}


def _stack_name_for_mode(mode: ViewMode) -> str:
    """Map a :class:`ViewMode` to the stack child name to display.

    Centralises the mapping so adding a third mode in a future build
    is a one-place edit and so the dispatch logic in
    :meth:`MainWindow._on_view_mode_changed` stays a one-line lookup.
    Raises :class:`KeyError` for unknown enum members — a deliberate
    fail-loud choice that makes a forgotten branch impossible to
    miss in code review.
    """
    return _MODE_TO_STACK_NAME[mode]


_WINDOW_ACTION_ACCELERATORS: Final[dict[WindowAction, str]] = {
    WindowAction.NEW_NOTE: "<Control>n",
    WindowAction.FOCUS_SEARCH: "<Control>f",
    WindowAction.TOGGLE_MODE: "<Control>e",
}
"""Application accelerators for the window actions that get one.

:class:`WindowAction.DELETE_NOTE` is intentionally **absent**: binding
``Delete`` as an application accelerator would swallow the key inside the
source editor. The note list installs it as a focus-local shortcut instead
(see :class:`giruntime.ui.note_list.NoteList`), so ``Delete`` only fires
when a list row has focus. Accelerator strings are in
:func:`Gtk.accelerator_parse` form; they are registered against the
``win.``-prefixed detailed names via
:meth:`Gtk.Application.set_accels_for_action`, which is an application-level
call even for window-scoped actions.
"""


def _default_window_width(article_column_px: int) -> int:
    """Compute the initial window width that fits the article column.

    The window must hold, left to right: the sidebar pane
    (:data:`_SIDEBAR_INITIAL_POSITION_PX`), the note-list pane
    (:data:`_NOTE_LIST_INITIAL_POSITION_PX`), the two paned handles
    (:data:`_PANED_HANDLE_ALLOWANCE_PX`), the rendered article column
    (``article_column_px`` — the only runtime-measured term, so the
    result tracks the body font), and a slack margin
    (:data:`_ARTICLE_SIDE_SLACK_PX`) that opens the read pane wider
    than the column so its centring branch fires on first allocation.

    Both ``_*_POSITION_PX`` terms are horizontal ``Gtk.Paned`` divider
    positions, which for a horizontal paned equal the width allocated
    to the start child — i.e. the sidebar and note-list pane widths at
    the initial layout. They live in different paned coordinate spaces,
    but they are summed here as independent *widths*, not chained
    offsets, which is the correct quantity.

    The result is clamped up to :data:`_MIN_DEFAULT_WINDOW_WIDTH_PX` so
    a degenerate (tiny) font measurement cannot yield an unusable
    window. Pure arithmetic — no GTK — so it is unit-testable without a
    display.
    """
    needed = (
        _SIDEBAR_INITIAL_POSITION_PX
        + _NOTE_LIST_INITIAL_POSITION_PX
        + _PANED_HANDLE_ALLOWANCE_PX
        + article_column_px
        + _ARTICLE_SIDE_SLACK_PX
    )
    return max(_MIN_DEFAULT_WINDOW_WIDTH_PX, needed)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


class MainWindow(  # pylint: disable=too-many-instance-attributes
    Gtk.ApplicationWindow,
):
    """The application's single top-level window.

    Composes :class:`Sidebar`, :class:`NoteList`, and a
    :class:`Gtk.Stack` over :class:`NoteView` + :class:`NoteEditor`
    into the three-pane shell. The stack's visible child tracks
    :attr:`AppState.view_mode`.

    The instance-attribute count is intentional: the window is a
    composition root, holding refs to the four panes plus the
    injected dependencies (the in-memory note store, the note
    controller, the app state, and the optional attachment store) that
    those panes share. Hiding any of them behind a "Bundle" object
    would obscure rather than clarify.
    """

    _note_store: NoteListStore
    _note_controller: NoteController
    _app_state: AppState
    _attachment_store: AttachmentStoreProtocol | None
    _toolbar: Toolbar
    _sidebar: Sidebar
    _note_list: NoteList
    _note_view: NoteView
    _note_editor: NoteEditor
    _right_pane_stack: Gtk.Stack
    _delete_note_action: Gio.SimpleAction

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        application: Gtk.Application,
        note_store: NoteListStore,
        note_controller: NoteController,
        app_state: AppState,
        attachment_store: AttachmentStoreProtocol | None = None,
        restored_state: SessionState = DEFAULT_SESSION_STATE,
    ) -> None:
        super().__init__(application=application)
        self._note_store = note_store
        self._note_controller = note_controller
        self._app_state = app_state
        self._attachment_store = attachment_store

        self.set_title(_WINDOW_TITLE)
        # The default *size* is set further down, once ``self._note_view``
        # exists: the default width is derived from the article column the
        # view will actually render (see :func:`_default_window_width`),
        # and that column width is only known after :class:`NoteView` has
        # measured the body font. Setting it here would force a literal
        # guess — which is exactly the bug this replaced.

        # Build the top header bar (toolbar) and install it as the
        # window's title bar. ``set_titlebar`` replaces the default
        # window decorations with our custom widget while preserving
        # the standard min/max/close buttons that the header bar
        # automatically adds to its end.
        self._toolbar = Toolbar(
            note_store=note_store,
            note_controller=note_controller,
            app_state=app_state,
        )
        self.set_titlebar(self._toolbar)

        # Build the three panes. Each subscribes to AppState itself;
        # the window does not arbitrate between them.
        self._sidebar = Sidebar(
            note_store=note_store,
            app_state=app_state,
        )
        self._note_list = NoteList(
            note_store=note_store,
            note_controller=note_controller,
            app_state=app_state,
            attachment_store=attachment_store,
        )
        # NoteView accepts the attachment store so its internal
        # image-bytes resolver can fetch attachment BLOBs by
        # filename. ``None`` is acceptable here — the resolver
        # falls back to the placeholder bytes contract from build
        # step 8 — and existing tests rely on that default.
        # The controller is passed for the *outbound* attachment path:
        # clicking an ``attachment:`` save link in the rendered view
        # routes the chosen destination through
        # ``NoteController.export_attachment``.
        self._note_view = NoteView(
            note_store=note_store,
            app_state=app_state,
            attachments=attachment_store,
            note_controller=note_controller,
        )
        # The editor embeds the attachments panel, which lists the
        # selected note's attachment metadata — so the editor now
        # receives the attachment store on the same optional contract
        # as the list and the view.
        self._note_editor = NoteEditor(
            note_store=note_store,
            note_controller=note_controller,
            app_state=app_state,
            attachments=attachment_store,
        )

        # Now that the rendered view exists (and has measured the body
        # font), size the window. A restored size from the previous run
        # wins outright; otherwise fall back to the computed default —
        # the fixed-width article column fitting alongside the two left
        # panes with slack on both sides, so the centring branch of
        # ``ArticleContainer`` fires on the very first allocation instead
        # of the column overflowing into a horizontal scroll. The default
        # width tracks the font because ``preferred_column_width_px`` is
        # the measured column.
        if restored_state.window_size is not None:
            width, height = restored_state.window_size
        else:
            width = _default_window_width(
                self._note_view.preferred_column_width_px(),
            )
            height = _DEFAULT_WINDOW_HEIGHT_PX
        self.set_default_size(width, height)
        if restored_state.window_maximized:
            # Permitted before the window is shown: GTK maximizes it as
            # soon as it appears onscreen. The size set above is still
            # what the window returns to if the user un-maximizes it.
            self.maximize()

        # The right pane is a Gtk.Stack: rendered view OR editor,
        # never both. The transition is a fade — the default — which
        # is faster than a slide and keeps the focus on content
        # rather than animation.
        self._right_pane_stack = Gtk.Stack.new()
        self._right_pane_stack.add_named(self._note_view, _STACK_NAME_VIEW)
        self._right_pane_stack.add_named(self._note_editor, _STACK_NAME_EDIT)
        # Initial visible child reflects the AppState's current mode.
        # NotesApplication leaves it at the ViewMode.VIEW default, so
        # this normally maps to the rendered view, but tests that
        # construct the window with a pre-set mode see the right
        # initial child.
        self._right_pane_stack.set_visible_child_name(
            _stack_name_for_mode(app_state.view_mode),
        )

        # Inner paned: note list | right-pane stack.
        inner_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        inner_paned.set_start_child(self._note_list)
        inner_paned.set_end_child(self._right_pane_stack)
        inner_paned.set_position(_NOTE_LIST_INITIAL_POSITION_PX)
        # The right pane should keep its width when the user drags
        # the *outer* divider — only the note list shrinks /
        # expands. Achieve that by letting the start child resize
        # but not shrink below its preferred minimum, and the end
        # child resize freely.
        inner_paned.set_resize_start_child(False)
        inner_paned.set_resize_end_child(True)
        inner_paned.set_shrink_start_child(False)
        inner_paned.set_shrink_end_child(False)

        # Outer paned: sidebar | (note list + right-pane stack).
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

        # Subscription 1 of 2: on every ``notify::view-mode`` the
        # handler flushes the editor's pending autosave (so any
        # just-typed edits hit disk under the current note id),
        # refreshes the view from the repository (so its buffer
        # reflects the just-saved source), and then swaps the stack's
        # visible child. Both flush and refresh are idempotent, so
        # doing them unconditionally keeps the dispatch branch-free.
        self._app_state.connect(
            "notify::view-mode",
            self._on_view_mode_changed,
        )

        # Register the window-scoped keyboard actions (New / focus search /
        # toggle mode / delete) and their accelerators. Done last, once the
        # toolbar the actions delegate to exists.
        self._install_window_actions(application)

    def _install_window_actions(self, application: Gtk.Application) -> None:
        """Register the ``win.*`` keyboard actions and their accelerators.

        One :class:`Gio.SimpleAction` per :class:`WindowAction`, added to
        this window's action group so each is addressable as ``win.<name>``.
        The behaviour each action performs lives in exactly one place —
        :class:`Toolbar` (New / focus-search / delete) or :class:`AppState`
        (toggle-mode) — so the accelerator and the matching toolbar button
        drive identical code and cannot drift.

        Accelerators come from :data:`_WINDOW_ACTION_ACCELERATORS`, which
        omits :attr:`WindowAction.DELETE_NOTE` on purpose: the note list
        binds ``Delete`` as a focus-local shortcut instead, so it never
        fires while the editor is focused.
        :meth:`Gtk.Application.set_accels_for_action` is an application-level
        call even for window actions, hence the ``application`` handle.

        The delete action's *enabled* state tracks the selection (disabled
        when no note is selected), so the accelerator is inert with nothing
        to delete, mirroring the toolbar's insensitive *Delete* button.
        """
        handlers: dict[
            WindowAction, Callable[[Gio.SimpleAction, object], None]
        ] = {
            WindowAction.NEW_NOTE: self._on_new_note_action,
            WindowAction.FOCUS_SEARCH: self._on_focus_search_action,
            WindowAction.TOGGLE_MODE: self._on_toggle_mode_action,
            WindowAction.DELETE_NOTE: self._on_delete_note_action,
        }
        for window_action, handler in handlers.items():
            action = Gio.SimpleAction.new(window_action, None)
            action.connect("activate", handler)
            self.add_action(action)
            if window_action is WindowAction.DELETE_NOTE:
                self._delete_note_action = action

        for window_action, accelerator in _WINDOW_ACTION_ACCELERATORS.items():
            application.set_accels_for_action(
                window_action_detailed_name(window_action),
                [accelerator],
            )

        # Keep the delete action's enabled state in step with the
        # selection, exactly as the toolbar keeps its Delete button.
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_id_changed,
        )
        self._refresh_delete_action_enabled()

    def _on_new_note_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
    ) -> None:
        """``win.new-note``: create a note pre-filled from the selection."""
        self._toolbar.create_note()

    def _on_focus_search_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
    ) -> None:
        """``win.focus-search``: open the header search and focus it."""
        self._toolbar.focus_search()

    def _on_toggle_mode_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
    ) -> None:
        """``win.toggle-mode``: flip between the rendered view and source.

        Writes :class:`AppState` directly — the same property the toolbar's
        View / Source toggle and the stack dispatch already observe — so the
        segmented toggle updates itself in response, with no extra wiring.
        """
        current = self._app_state.view_mode
        other = ViewMode.EDIT if current == ViewMode.VIEW else ViewMode.VIEW
        self._app_state.set_view_mode(other)

    def _on_delete_note_action(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
    ) -> None:
        """``win.delete-note``: confirm-then-delete the selected note.

        The same path as the toolbar's *Delete* button. Triggered by the
        note list's focus-local ``Delete`` shortcut (never an application
        accelerator). A no-op when nothing is selected, and the action is
        additionally disabled in that state.
        """
        self._toolbar.delete_selected()

    def _on_selected_note_id_changed(
        self,
        _app_state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        self._refresh_delete_action_enabled()

    def _refresh_delete_action_enabled(self) -> None:
        """Enable ``win.delete-note`` iff a note is selected."""
        self._delete_note_action.set_enabled(
            self._app_state.selected_note_id is not None,
        )

    def _on_view_mode_changed(
        self,
        _app_state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """Flush the editor, refresh the view, then swap the visible child.

        The flush ensures any pending debounced autosave hits disk
        under the current note id before the rendered view re-reads from
        the in-memory store. The refresh ensures the view's buffer
        reflects the just-saved source (without it the view would still
        show whatever was rendered at the last
        ``notify::selected-note-id``). Both calls are idempotent:
        :meth:`NoteEditor.flush_pending_save` is a no-op when no save is
        pending; :meth:`NoteView.refresh` re-renders from the store
        whose state may or may not have changed. Doing both
        unconditionally on every mode change is simpler and cheaper than
        gating on the direction of the transition, and keeps the path
        identical for VIEW→EDIT (where the flush is needed if the user
        toggled back-and-forth quickly) and EDIT→VIEW.
        """
        self._note_editor.flush_pending_save()
        self._note_view.refresh()
        self._right_pane_stack.set_visible_child_name(
            _stack_name_for_mode(self._app_state.view_mode),
        )

    def flush_editor(self) -> None:
        """Force any pending debounced autosave to disk immediately.

        Exposed for the application-shutdown path. Closing the window
        ends the process, so a save still sitting in the editor's
        :data:`giruntime.ui.note_editor.AUTOSAVE_DEBOUNCE_MS` window (the
        last-typed keystrokes) would otherwise be lost.
        :class:`giruntime.ui.application.NotesApplication` calls this from
        its ``close-request`` handler before quitting.

        Idempotent — a no-op when no save is pending — so it is safe to
        call unconditionally, exactly like the flush already performed on
        every view-mode change. Keeping it a named method on the window
        (rather than having the application reach into the editor) keeps
        :attr:`_note_editor` a private child of this window.
        """
        self._note_editor.flush_pending_save()

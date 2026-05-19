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
* The single signal subscription this widget owns is
  ``view-mode-changed``. On every mode change the window flushes the
  editor's pending autosave (so any just-typed edits hit disk under
  the current note id) and asks the view to refresh from the
  repository before the stack swap reveals it. Flush + refresh are
  both idempotent, so doing them on every mode change — not just on
  the EDIT→VIEW direction — keeps the dispatch branch-free without
  paying any extra cost on the no-op path. Every other signal
  subscription belongs to the panes themselves; the window's surface
  stays minimal, owning only the layout and the view-mode dispatch.
* :class:`NoteEditor` and :class:`NoteView` both stay constructed
  and live across mode switches. Tearing one down on every toggle
  would discard the editor's undo history and the view's child
  anchors (a non-trivial cost for images and tables once those land).
  GTK's :class:`Gtk.Stack` simply hides the inactive child — both
  remain wired to :class:`AppState` for selection updates so a
  freshly-revealed pane is always up-to-date.
* The editor pane subscribes to ``selected-note-changed`` like any
  other pane, but with the added invariant that selection-change
  flushes any pending auto-save *before* the buffer is overwritten.
  That guarantee lives inside :class:`NoteEditor`; this window is
  blissfully unaware of it.
* The construction signature is the long-term one: caller
  (:class:`NotesApplication`) passes ``application``,
  ``note_repository``, ``notebook_repository``, ``note_controller``,
  ``app_state``, and (from build step 11) ``attachment_store``, all
  keyword-only. ``attachment_store`` is optional with a ``None``
  default so the existing per-pane test suites that pre-date step
  11 keep constructing :class:`MainWindow` without a fourth
  injected dependency; in that mode :class:`NoteView` falls back
  to its placeholder image resolver. Future build steps that add
  toolbar / status-bar children extend the window's child set, but
  leave its signal subscriptions confined to the same single
  view-mode dispatch.
* Default sizes for the panes match the per-widget hints
  (:data:`_SIDEBAR_INITIAL_POSITION_PX`,
  :data:`_NOTE_LIST_INITIAL_POSITION_PX`). They are *initial*
  positions only; once the user drags either handle GTK records the
  new value internally and our defaults stop applying. Saving and
  restoring those positions across launches is a v2 feature — there
  is no settings store in v1 (decision 4 of the plan).
* The window owns no data. Everything it needs — repositories,
  controllers, app state — is reached through references that
  originate in :class:`NotesApplication`. Tests can construct the
  same widget with the same fake repositories the per-pane tests
  already use, without touching the file system or the database.
"""

from __future__ import annotations

from typing import Final

import gi

gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gtk  # noqa: E402

from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.enums import ViewMode
from notes_app.storage.protocols import (
    AttachmentStoreProtocol,
    NoteRepositoryProtocol,
    NotebookRepositoryProtocol,
)
from notes_app.ui.note_editor import NoteEditor
from notes_app.ui.note_list import NoteList
from notes_app.ui.note_view import NoteView
from notes_app.ui.sidebar import Sidebar
from notes_app.ui.toolbar import Toolbar


# ---------------------------------------------------------------------------
# Constants — window and paned-position defaults
# ---------------------------------------------------------------------------


_DEFAULT_WINDOW_WIDTH_PX: Final[int] = 1200
"""Initial window width.

Wide enough that the rendered article column gets its full
:data:`TARGET_CHARS_PER_LINE` × char-width allocation with slack on
both sides of the article column, so the wide-window centring branch
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
    composition root, holding refs to the four panes plus the four
    injected dependencies (two repositories, the note controller,
    and the app state) that those panes share. Hiding any of them
    behind a "Bundle" object would obscure rather than clarify.
    """

    _note_repository: NoteRepositoryProtocol
    _notebook_repository: NotebookRepositoryProtocol
    _note_controller: NoteController
    _app_state: AppState
    _attachment_store: AttachmentStoreProtocol | None
    _toolbar: Toolbar
    _sidebar: Sidebar
    _note_list: NoteList
    _note_view: NoteView
    _note_editor: NoteEditor
    _right_pane_stack: Gtk.Stack

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        application: Gtk.Application,
        note_repository: NoteRepositoryProtocol,
        notebook_repository: NotebookRepositoryProtocol,
        note_controller: NoteController,
        app_state: AppState,
        attachment_store: AttachmentStoreProtocol | None = None,
    ) -> None:
        super().__init__(application=application)
        self._note_repository = note_repository
        self._notebook_repository = notebook_repository
        self._note_controller = note_controller
        self._app_state = app_state
        self._attachment_store = attachment_store

        self.set_title(_WINDOW_TITLE)
        self.set_default_size(
            _DEFAULT_WINDOW_WIDTH_PX,
            _DEFAULT_WINDOW_HEIGHT_PX,
        )

        # Build the top header bar (toolbar) and install it as the
        # window's title bar. ``set_titlebar`` replaces the default
        # window decorations with our custom widget while preserving
        # the standard min/max/close buttons that the header bar
        # automatically adds to its end.
        self._toolbar = Toolbar(
            note_repository=note_repository,
            notebook_repository=notebook_repository,
            note_controller=note_controller,
            app_state=app_state,
        )
        self.set_titlebar(self._toolbar)

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
        # NoteView accepts the attachment store so its internal
        # image-bytes resolver can fetch attachment BLOBs by
        # filename. ``None`` is acceptable here — the resolver
        # falls back to the placeholder bytes contract from build
        # step 8 — and existing tests rely on that default.
        self._note_view = NoteView(
            note_repository=note_repository,
            app_state=app_state,
            attachments=attachment_store,
        )
        self._note_editor = NoteEditor(
            note_repository=note_repository,
            note_controller=note_controller,
            app_state=app_state,
        )

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

        # The single signal subscription this widget owns: on every
        # ``view-mode-changed`` the handler flushes the editor's
        # pending autosave (so any just-typed edits hit disk under the
        # current note id), refreshes the view from the repository
        # (so its buffer reflects the just-saved source), and then
        # swaps the stack's visible child. Both flush and refresh are
        # idempotent, so doing them unconditionally keeps the dispatch
        # branch-free.
        self._app_state.connect(
            "view-mode-changed",
            self._on_view_mode_changed,
        )

    def _on_view_mode_changed(self, _app_state: AppState) -> None:
        """Flush the editor, refresh the view, then swap the visible child.

        The flush ensures any pending debounced autosave hits disk
        under the current note id before the rendered view re-reads
        from the repository. The refresh ensures the view's buffer
        reflects the just-saved source (without it the view would
        still show whatever was rendered at the last
        ``selected-note-changed``). Both calls are idempotent:
        :meth:`NoteEditor.flush_pending_save` is a no-op when no save
        is pending; :meth:`NoteView.refresh` re-renders from the
        repository whose state may or may not have changed. Doing
        both unconditionally on every mode change is simpler and
        cheaper than gating on the direction of the transition, and
        keeps the path identical for VIEW→EDIT (where the flush is
        needed if the user toggled back-and-forth quickly) and
        EDIT→VIEW.
        """
        self._note_editor.flush_pending_save()
        self._note_view.refresh()
        self._right_pane_stack.set_visible_child_name(
            _stack_name_for_mode(self._app_state.view_mode),
        )

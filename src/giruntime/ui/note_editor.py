"""The source editor pane: GtkSourceView with debounced save + attachments.

Principles & invariants
-----------------------
* :class:`NoteEditor` is the pane the user types into — a vertical
  :class:`Gtk.Box` composed of a :class:`Gtk.ScrolledWindow` hosting a
  :class:`GtkSource.View` over a :class:`GtkSource.Buffer`, with the
  :class:`ui.attachments_panel.AttachmentsPanel` below it. The buffer's
  content is the authoritative live source for the currently selected
  note. There is **no edit toolbar**: the buttons it carried merely
  duplicated markup the user can type directly, so the markup surface
  is the source text itself, and the one capability the toolbar
  uniquely provided — attaching a file — moved to the panel's *Add
  file* button. Attaching never inserts anything into the buffer;
  ``image::`` macros and links stay author-typed.
* The widget is **stateless with respect to notes**: every change to
  :attr:`AppState.selected_note_id` reloads the buffer from the
  store. The editor never caches a copy of the source between
  selections — that would create the second source of truth the
  module docstring of :class:`AppState` warns against.
* The bundled :data:`LANGUAGE_ID` AsciiDoc grammar is what
  :class:`GtkSource.View` highlights against. The grammar covers only
  the supported subset because anything richer would highlight
  constructs the parser still rejects, and surfacing a green check on
  something the renderer crashes on is exactly the sort of dishonest
  UX feedback we are avoiding. The grammar extends in lock-step with
  the parser.
* The grammar is **always loaded from the compiled
  ``folio.gresource``** via a ``resource:///`` search path — never
  from a filesystem path — so a source checkout and the packaged
  ``folio.pyz`` behave identically (inside the zip a filesystem path
  would point *into* the archive and the OS could not open it). The
  GResource is built from the committed ``folio.gresource.xml`` +
  ``language_spec.lang`` by ``run`` / ``make`` before any entry point
  runs; the search-path URI is obtained from the shared
  :func:`giruntime.ui._gresource.resource_path` (see
  :func:`_configure_search_path`), keyed by
  :attr:`enums.GResourceSubtree.LANGUAGE_SPECS` — the same helper
  :mod:`giruntime.ui.application` uses to make the bundled icon
  resolvable, so the bundle is still registered from exactly one
  place even though two modules now need resources out of it, and
  obtaining a path is what triggers that registration rather than
  being a step either caller could forget. A *missing* resource is a
  hard :class:`FileNotFoundError`, not a silent fallback to
  plain-text highlighting.
* The :class:`GtkSource.LanguageManager` we build is *fresh* — not
  the process-global default. The default is shared with the rest of
  the desktop's text editing tooling and mutating its search path
  would leak our private grammar id into other applications running
  in the same gnome-shell session. A per-editor manager keeps our
  side-effects scoped.
* Auto-save is **debounced**: every buffer ``changed`` signal cancels
  the pending timer and reschedules a save 300 ms in the future.
  This matches typical editor latency (a brisk typist generates
  dozens of changes a second; saving on every one would hammer the
  database for no user benefit) while keeping the user's text
  on disk before they can switch tasks. The 300 ms value lives at
  module level (:data:`AUTOSAVE_DEBOUNCE_MS`) for a single source of
  truth and easy adjustment.
* The timeout *scheduler* and *canceller* are injected as
  :data:`TimeoutScheduler` / :data:`TimeoutCanceller` callables.
  Production wires them to :func:`GLib.timeout_add` and
  :func:`GLib.source_remove`; tests pass synchronous fakes that let
  them assert "the editor scheduled exactly one save 300 ms after
  the user typed" without spinning a real GLib main loop. This is
  the same dependency-injection pattern the renderer uses for
  ``image_bytes_for`` and ``column_width_px``.
* A "loading" guard flag prevents the programmatic buffer load that
  follows a selection change from itself triggering an auto-save.
  Without the guard, every reload would queue a redundant save of
  the note the user just stopped editing back into the same row,
  which is wasted work *and* would clobber a save in progress.
* Selection changes flush pending saves **immediately** before
  loading the new note. Otherwise a 0..300 ms window of typed
  changes would either be saved against the new note (wrong note
  id) or lost when the buffer is overwritten. The flush is always
  paired with a cancel of the pending timer so the timer cannot
  fire later for an already-flushed save.
* The attachments panel sits **below** the editor's ScrolledWindow
  and is built with the same injected collaborators (controller, app
  state, attachment store, file-dialog opener). The ScrolledWindow is
  the ``vexpand`` child; the panel takes only its natural height so it
  cannot starve the editing area on small windows. The panel hides
  itself while no note is selected — the same gating the old image
  button expressed as insensitivity.
* GTK 4 currency: :class:`GtkSource.Buffer` (not the deprecated
  :class:`GtkSource.Buffer` v3 paths), :meth:`Gtk.Box.append`. No
  deprecated-in-4.18 calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Final

from gi.repository import GLib, GObject, Gtk, GtkSource

from enums import GResourceSubtree
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui import _gresource
from giruntime.ui._file_picker import (
    FileDialogOpener,
    default_file_dialog_opener,
)
from giruntime.ui.attachments_panel import AttachmentsPanel
from storage.protocols import AttachmentStoreProtocol


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


LANGUAGE_ID: Final[str] = "notes-asciidoc"
"""Identifier the bundled language file declares.

Matches the ``id`` attribute of the ``<language>`` root element in
``ui/language_spec.lang``. Kept as a module-level
constant rather than a literal so the grep target for "where is the
language id used" is exactly one place.
"""

AUTOSAVE_DEBOUNCE_MS: Final[int] = 300
"""Debounce delay between the last buffer change and the auto-save.

A brisk typist generates dozens of buffer-changed signals a second;
saving on every one would hammer the database. 300 ms is a typical
editor latency budget — long enough to coalesce keystrokes, short
enough that the user does not notice a lag when they pause.
"""

# ---------------------------------------------------------------------------
# Type aliases for the injected timeout primitives
# ---------------------------------------------------------------------------


type TimeoutScheduler = Callable[[int, Callable[[], bool]], int]
"""Schedule ``callback`` to run once after ``delay_ms`` ms; return a
cancellable handle.

The callback's :class:`bool` return value follows GLib semantics:
returning :data:`GLib.SOURCE_REMOVE` (``False``) means "do not
re-fire". The editor never returns :data:`GLib.SOURCE_CONTINUE` —
auto-save is one-shot per debounce cycle.

Production wiring: :func:`GLib.timeout_add`. Test wiring: a fake that
records the call and returns a synthetic integer handle, plus a
:meth:`fire` helper that invokes the callback synchronously."""

type TimeoutCanceller = Callable[[int], None]
"""Cancel a previously-scheduled :data:`TimeoutScheduler` handle.

Production wiring: :func:`GLib.source_remove`. Test wiring: a fake
that records the cancelled handle so assertions can verify the
debounce really did cancel before rescheduling."""


def _default_timeout_scheduler(
    delay_ms: int,
    callback: Callable[[], bool],
) -> int:
    """Production scheduler — wraps :func:`GLib.timeout_add`.

    Defined as a free function rather than ``GLib.timeout_add``
    directly so the type annotation in :class:`NoteEditor`'s
    ``__init__`` can be the explicit :data:`TimeoutScheduler` alias.
    PyGObject's introspected signatures do not always satisfy mypy
    against arbitrary callable types.
    """
    handle: int = GLib.timeout_add(delay_ms, callback)
    return handle


def _default_timeout_canceller(handle: int) -> None:
    """Production canceller — wraps :func:`GLib.source_remove`."""
    GLib.source_remove(handle)


# ---------------------------------------------------------------------------
# Pure helpers — operate on Gtk.TextBuffer with no display required
# ---------------------------------------------------------------------------


def buffer_text(buffer: Gtk.TextBuffer) -> str:
    """Return the buffer's full text contents.

    Trivially-named convenience shared by the auto-save flush path
    and the tests, which also need to read the buffer's contents. The
    third argument to :meth:`Gtk.TextBuffer.get_text` is
    ``include_hidden_chars``; we pass ``False`` because hidden chars
    are an artefact of GtkSourceView's bookkeeping and are not part
    of the user's source.
    """
    start = buffer.get_start_iter()
    end = buffer.get_end_iter()
    text: str = buffer.get_text(start, end, False)
    return text


# ---------------------------------------------------------------------------
# Language file loading
# ---------------------------------------------------------------------------


def _configure_search_path(manager: GtkSource.LanguageManager) -> None:
    """Register the compiled GResource and point ``manager`` at it.

    The search-path URI comes from the shared, idempotent
    :func:`giruntime.ui._gresource.resource_path` — see that module
    for why the bundle is registered from exactly one place even
    though :mod:`giruntime.ui.application` also needs a resource out
    of it (the icon), and why obtaining a path is itself what
    triggers registration rather than being a separate step this
    function could get out of order. The manager's search path is
    then prepended with that URI (since GtkSourceView 5.4 the search
    path accepts ``resource:///`` directory URIs naming a folder
    inside a registered GResource).

    Runs at most once per process because the only caller,
    :func:`_get_language_manager`, is itself memoised behind
    :data:`_LANGUAGE_MANAGER`.
    """
    existing = list(manager.get_search_path() or ())
    language_specs_dir = _gresource.resource_path(GResourceSubtree.LANGUAGE_SPECS)
    # Prepend so our bundled grammar wins over a system-installed
    # ``notes-asciidoc`` (none ships, but defending against an id
    # collision is cheap and keeps surprising debug stories at bay).
    manager.set_search_path([language_specs_dir, *existing])


_LANGUAGE_MANAGER: GtkSource.LanguageManager | None = None
"""Module-level cache of the configured :class:`GtkSource.LanguageManager`.

GtkSourceView's highlighter holds a *weak* reference to the manager
that produced its :class:`GtkSource.Language`: if the manager is
garbage-collected before the buffer asks it for a syntax engine, the
warning ``_gtk_source_language_create_engine() is called after
language manager was finalized`` is logged and highlighting silently
breaks. We therefore retain the manager at module scope so it
outlives any individual :class:`NoteEditor`.

It is initialised lazily by :func:`_get_language_manager` (rather than
at import time) so importing this module on a system that has
GtkSourceView installed but mis-configured does not fail at load
time — :class:`NoteEditor` constructs the manager only when the first
editor instance actually needs the language."""


def _get_language_manager() -> GtkSource.LanguageManager:
    """Return the lazily-initialised module-level language manager.

    The first call constructs a fresh :class:`GtkSource.LanguageManager`
    (separate from the process-global default), registers the compiled
    GResource, and prepends its ``resource:///`` grammar directory to the
    search path via :func:`_configure_search_path`. Subsequent calls
    return the cached instance unchanged, so the GResource is registered
    exactly once per process.
    """
    global _LANGUAGE_MANAGER  # pylint: disable=global-statement
    if _LANGUAGE_MANAGER is not None:
        return _LANGUAGE_MANAGER
    manager = GtkSource.LanguageManager.new()
    _configure_search_path(manager)
    _LANGUAGE_MANAGER = manager
    return manager


def load_asciidoc_language() -> GtkSource.Language | None:
    """Return the bundled AsciiDoc :class:`GtkSource.Language`, or
    ``None`` if the language id does not resolve.

    Uses the module-cached :func:`_get_language_manager` so the
    underlying :class:`GtkSource.LanguageManager` outlives any single
    editor instance — see :data:`_LANGUAGE_MANAGER` for the lifecycle
    rationale. The compiled GResource is registered as a side effect of
    that call; a *missing* GResource is a hard
    :class:`FileNotFoundError` (see :func:`_configure_search_path`), not
    a silent ``None``. ``None`` is only returned if the registered
    grammar's ``<language id>`` fails to match :data:`LANGUAGE_ID`, which
    would indicate the committed grammar drifted from the constant.
    """
    return _get_language_manager().get_language(LANGUAGE_ID)


# ---------------------------------------------------------------------------
# NoteEditor widget
# ---------------------------------------------------------------------------


class NoteEditor(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """The source-editing pane.

    Construction signature is keyword-only and mirrors the other
    panes' shape: the in-memory note store (read-only here — the editor
    never mutates it directly), the controller that owns
    ``update_source`` and the attachment methods, the :class:`AppState`
    instance the rest of the UI is wired to, and the attachment store
    (metadata reads for the embedded panel; ``None`` follows the same
    optional-injection contract the note list and view honour). The
    body of the selected note is read from the store, not from the
    database.

    The two timeout primitives default to GLib's main-loop-backed
    versions; tests override them with fakes so debounce behaviour can
    be asserted synchronously. The file-dialog opener is forwarded to
    the embedded :class:`AttachmentsPanel`, whose Add-file button is
    its consumer.

    The instance-attribute count exceeds pylint's default ceiling of
    seven because the editor genuinely depends on five injected
    collaborators (store, controller, app state, scheduler, canceller)
    plus three pieces of widget state (buffer, source-view, attachments
    panel), the current-note id, and two flags tracking the
    save-pending and load-in-progress states. Splitting these into a
    helper would create a second class with no clear surface — every
    field is read or written from at least two methods.
    """

    _note_store: NoteListStore
    _note_controller: NoteController
    _app_state: AppState
    _schedule_timeout: TimeoutScheduler
    _cancel_timeout: TimeoutCanceller

    _buffer: GtkSource.Buffer
    _source_view: GtkSource.View
    _attachments_panel: AttachmentsPanel

    _current_note_id: str | None
    """Id of the note whose source is presently in the buffer.

    ``None`` means no note is selected — the buffer is empty and
    auto-save is a no-op. Updated only inside
    :meth:`_load_selected_note` so the relationship between the
    buffer's content and this id is invariant: the buffer always
    holds the source of the note identified here, edits in flight
    excepted (which auto-save flushes back to this id).
    """

    _pending_save_handle: int | None
    """Handle returned by the most recent :data:`TimeoutScheduler`
    call, or ``None`` when no save is pending.

    Set by :meth:`_schedule_save`; cleared inside the timer callback
    after firing, or by :meth:`flush_pending_save` if the save is
    forced to run before the timer expires.
    """

    _loading_note: bool
    """Guard flag suppressing auto-save during programmatic loads.

    Set to ``True`` by :meth:`_load_selected_note` while it overwrites
    the buffer with the freshly-fetched note source, and reset to
    ``False`` once the load completes. The buffer's ``changed``
    handler short-circuits while this is ``True`` so the load itself
    does not queue a redundant save back into the same row.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        note_store: NoteListStore,
        note_controller: NoteController,
        app_state: AppState,
        attachments: AttachmentStoreProtocol | None = None,
        schedule_timeout: TimeoutScheduler = _default_timeout_scheduler,
        cancel_timeout: TimeoutCanceller = _default_timeout_canceller,
        file_dialog_opener: FileDialogOpener = default_file_dialog_opener,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_store = note_store
        self._note_controller = note_controller
        self._app_state = app_state
        self._schedule_timeout = schedule_timeout
        self._cancel_timeout = cancel_timeout

        self._current_note_id = None
        self._pending_save_handle = None
        self._loading_note = False

        # Build the buffer first; the View is constructed from it. The
        # language is set on the buffer (not the view) — that is where
        # GtkSourceView 5 holds the highlighter.
        self._buffer = GtkSource.Buffer.new(None)
        language = load_asciidoc_language()
        if language is not None:
            self._buffer.set_language(language)
        self._buffer.set_highlight_syntax(True)
        # Highlight matching brackets is GtkSourceView's default off;
        # turning it on helps the user balance the inline-style
        # delimiters (``*``, ``_``, ``#``) which are otherwise easy
        # to leave half-open. The parser raises on unbalanced
        # delimiters, so giving the user a visual cue at edit time is
        # in keeping with the strict error policy.
        self._buffer.set_highlight_matching_brackets(True)

        self._source_view = GtkSource.View.new_with_buffer(self._buffer)
        self._source_view.set_show_line_numbers(True)
        self._source_view.set_highlight_current_line(True)
        self._source_view.set_monospace(True)
        self._source_view.set_auto_indent(True)
        # Tabs insert spaces (two spaces — matching the React
        # reference's ``onKeyDown`` Tab handler). This avoids tabs in
        # source bodies, which AsciiDoc's whitespace rules are
        # finicky about.
        self._source_view.set_insert_spaces_instead_of_tabs(True)
        self._source_view.set_tab_width(2)
        # Soft-wrap long prose lines. The parser is line-oriented
        # but readability wins over visual-line / source-line parity
        # in the editor; the renderer is the place where exact line
        # breaks matter.
        self._source_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._source_view.set_hexpand(True)
        self._source_view.set_vexpand(True)

        # The editor proper: GtkSource.View inside a ScrolledWindow —
        # the box's single ``vexpand`` child, so the attachments panel
        # below cannot starve the editing area on small windows.
        scrolled_window = Gtk.ScrolledWindow.new()
        scrolled_window.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled_window.set_child(self._source_view)
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)
        self.append(scrolled_window)

        # Attachment management below the editor. The panel shares the
        # editor's collaborators and owns the only add-attachment entry
        # point now that the toolbar's Image button is gone; it hides
        # itself while no note is selected.
        self._attachments_panel = AttachmentsPanel(
            note_controller=note_controller,
            app_state=app_state,
            attachments=attachments,
            file_dialog_opener=file_dialog_opener,
        )
        self.append(self._attachments_panel)

        # Wire up signals last so handlers don't fire mid-construction.
        self._buffer.connect("changed", self._on_buffer_changed)
        self._app_state.connect(
            "notify::selected-note-id",
            self._on_selected_note_changed,
        )

        # Pick up whatever ``selected_note_id`` is set to before the
        # editor was constructed — same pattern as :class:`NoteView`.
        self._load_selected_note()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def flush_pending_save(self) -> None:
        """Force any pending debounced save to fire immediately.

        Used by callers that need a guaranteed-on-disk state before
        proceeding — for example, the selection-change path (so the
        old note's last-300-ms changes are not lost when the buffer
        is overwritten with the new note's source) and a future
        application-shutdown hook.

        Idempotent: a call when no save is pending is a no-op.
        """
        if self._pending_save_handle is None:
            return
        # Cancel first, then save synchronously. The ordering matters:
        # if we saved first and the save raised, the timer would still
        # fire later for the same content. By cancelling first we
        # guarantee no repeat fire even if the synchronous save
        # surfaces an exception that the controller's signal handler
        # has already converted into a toast.
        handle = self._pending_save_handle
        self._pending_save_handle = None
        self._cancel_timeout(handle)
        self._save_now()

    @property
    def current_note_id(self) -> str | None:
        """The id of the note whose source is presently in the buffer.

        Read-only and exists primarily for tests and for future
        callers that want to render contextual UI without consulting
        :class:`AppState` directly. ``None`` when no note is
        selected — the buffer is empty in that case.
        """
        return self._current_note_id

    # ------------------------------------------------------------------
    # Selection / load handling
    # ------------------------------------------------------------------

    def _on_selected_note_changed(
        self,
        _app_state: AppState,
        _pspec: GObject.ParamSpec,
    ) -> None:
        """React to a selection change.

        Order of operations is the lossless one: flush any pending
        save (so the OLD note's in-flight edits hit disk under the
        OLD note's id), then load the new note. Reversing this order
        would either save the OLD edits under the NEW id (corrupting
        both notes) or simply discard them when the buffer is
        overwritten.
        """
        self.flush_pending_save()
        self._load_selected_note()

    def _load_selected_note(self) -> None:
        """Populate the buffer from :attr:`AppState.selected_note_id`.

        Mirrors :meth:`NoteView.refresh`'s missing-id behaviour: a
        ``None`` selection clears the buffer; an unknown id (e.g. a
        stale selection pointing at a deleted note) also clears it
        rather than raising. The note-list elsewhere will pick a new
        selection on its next refresh.

        While the load is in progress the ``_loading_note`` guard is
        set so the buffer's ``changed`` handler short-circuits; the
        load itself must not queue an auto-save back into the same
        row.

        The attachments panel needs no synchronisation from here: it
        subscribes to ``notify::selected-note-id`` itself and reloads
        (or hides on a ``None`` selection) on the same notification
        that triggered this load.
        """
        note_id = self._app_state.selected_note_id

        self._loading_note = True
        try:
            if note_id is None:
                self._buffer.set_text("")
                self._current_note_id = None
                return
            try:
                note = self._note_store.get_note(note_id)
            except KeyError:
                self._buffer.set_text("")
                self._current_note_id = None
                return
            self._buffer.set_text(note.source)
            self._current_note_id = note.id
        finally:
            self._loading_note = False

    # ------------------------------------------------------------------
    # Auto-save plumbing
    # ------------------------------------------------------------------

    def _on_buffer_changed(self, _buffer: GtkSource.Buffer) -> None:
        """Schedule (or reschedule) an auto-save 300 ms in the future.

        Ignored when the change was triggered by a programmatic load
        or when no note is currently being edited (``current_note_id``
        is ``None``). Otherwise any pending timer is cancelled and a
        fresh one is scheduled — that's the debounce behaviour: every
        keystroke pushes the save out, and the save fires only once
        the user pauses for :data:`AUTOSAVE_DEBOUNCE_MS` ms.
        """
        if self._loading_note:
            return
        if self._current_note_id is None:
            return
        self._schedule_save()

    def _schedule_save(self) -> None:
        """Cancel any pending auto-save and schedule a fresh one."""
        if self._pending_save_handle is not None:
            self._cancel_timeout(self._pending_save_handle)
            self._pending_save_handle = None
        self._pending_save_handle = self._schedule_timeout(
            AUTOSAVE_DEBOUNCE_MS,
            self._on_save_timer,
        )

    def _on_save_timer(self) -> bool:
        """GLib timer callback — perform the deferred save.

        Always returns :data:`GLib.SOURCE_REMOVE` (``False``) so the
        timer does not re-fire; the next save is scheduled by the
        next buffer change.
        """
        # Clear the handle BEFORE saving: if the controller's
        # ``update_source`` itself touches the buffer (it does not
        # today, but defence-in-depth), the resulting ``changed``
        # signal must be free to schedule a new timer rather than
        # trying to cancel the now-firing one.
        self._pending_save_handle = None
        self._save_now()
        # GLib source callbacks return a bool: SOURCE_REMOVE (False)
        # tears the timer down so it does not re-fire. Keep returning the
        # named constant rather than a bare ``False`` so the source
        # contract stays explicit.
        result: bool = GLib.SOURCE_REMOVE
        return result

    def _save_now(self) -> None:
        """Perform the save synchronously.

        Reads the buffer's current contents and routes them through
        :meth:`NoteController.update_source`. The controller emits
        toasts on database errors via its ``storage-error`` signal
        and re-raises; we catch :class:`sqlite3.DatabaseError` here
        so the timer callback does not propagate the exception into
        GLib's main loop (which would log a critical warning every
        time a save races a transient DB lock). The user-visible
        notification has already fired through the controller's
        signal — we are not silently swallowing, only declining to
        double-report.
        """
        if self._current_note_id is None:
            # Defensive: the schedule path already gated on this, but
            # an external caller could invoke ``flush_pending_save``
            # after the selection cleared. Saving with a ``None`` id
            # would crash the controller; bailing out is correct.
            return
        source = buffer_text(self._buffer)
        try:
            self._note_controller.update_source(
                self._current_note_id,
                source,
            )
        except sqlite3.DatabaseError:
            # The controller's ``capturing_storage_errors`` already
            # emitted the user-visible ``storage-error`` toast before
            # re-raising. Catching here is what keeps the GLib timer
            # callback's exception trail clean — the bug-finding
            # signal has already gone out.
            return

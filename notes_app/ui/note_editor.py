"""The source editor pane: GtkSourceView with a toolbar and debounced save.

Principles & invariants
-----------------------
* :class:`NoteEditor` is the pane the user types into. It mirrors
  :class:`NoteView`'s overall shape — a vertical :class:`Gtk.Box`
  composed of a toolbar at the top and a :class:`Gtk.ScrolledWindow`
  hosting the editing widget below — but the editing widget is a
  :class:`GtkSource.View` over a :class:`GtkSource.Buffer`, not a
  read-only :class:`Gtk.TextView`. The buffer's content is the
  authoritative live source for the currently selected note.
* The widget is **stateless with respect to notes**: every change to
  :attr:`AppState.selected_note_id` reloads the buffer from the
  repository. The editor never caches a copy of the source between
  selections — that would create the second source of truth the
  module docstring of :class:`AppState` warns against.
* The bundled :data:`LANGUAGE_ID` AsciiDoc grammar
  (``notes_app/asciidoc/language_spec.lang``) is what
  :class:`GtkSource.View` highlights against. The grammar covers only
  the step-4 subset (sections, lists, code blocks, image macros,
  bold, italic, strikethrough, underline) because anything richer
  would highlight constructs the parser still rejects, and surfacing
  a green check on something the renderer crashes on is exactly the
  sort of dishonest UX feedback we are avoiding. Steps 13–15 extend
  the grammar in lock-step with the parser.
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
* Toolbar button actions are extracted as **pure functions over
  Gtk.TextBuffer** (:func:`wrap_selection`, :func:`insert_block_line`)
  so they can be unit-tested with a vanilla :class:`Gtk.TextBuffer`
  built from scratch — no display, no GtkSourceView — and so each
  callback is a one-line lambda binding a fixed delimiter or
  template to those helpers.
* Toolbar surface is the **step-4 / step-10 core set**: Heading,
  Bold, Italic, Strikethrough, Underline, Bullet list, Numbered
  list, Code block, Image. Monospace and link buttons land at step
  13; table at step 14; admonition and blockquote at step 15.
  Adding all the buttons up-front would imply support the parser
  does not yet have, which the strict-mode policy of the parser
  would surface as a parse error the moment the user clicked the
  premature button.
* The image button at step 10 inserts a *placeholder* macro
  (``image::filename.png[]``); the :class:`Gtk.FileDialog` flow that
  attaches an actual file lands at step 11 alongside
  :class:`AttachmentStoreProtocol`. The placeholder gives the user
  a syntactically valid macro body to edit, and crucially does
  **not** depend on attachments being plumbed in.
* GTK 4 currency: :class:`GtkSource.Buffer` (not the deprecated
  :class:`GtkSource.Buffer` v3 paths), :meth:`Gtk.Box.append`,
  :meth:`Gtk.Button.set_child` (rather than the long-form box-of-
  icon-and-label). No deprecated-in-4.18 calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
# pylint: disable=wrong-import-position
from gi.repository import GLib, Gtk, GtkSource  # noqa: E402

from notes_app import asciidoc as _asciidoc_pkg
from notes_app.controllers.app_state import AppState
from notes_app.controllers.note_controller import NoteController
from notes_app.storage.protocols import NoteRepositoryProtocol


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


LANGUAGE_ID: Final[str] = "notes-asciidoc"
"""Identifier the bundled language file declares.

Matches the ``id`` attribute of the ``<language>`` root element in
``notes_app/asciidoc/language_spec.lang``. Kept as a module-level
constant rather than a literal so the grep target for "where is the
language id used" is exactly one place.
"""

LANGUAGE_FILE_NAME: Final[str] = "language_spec.lang"
"""File name of the bundled language definition.

Lives next to the AsciiDoc parser under
``notes_app/asciidoc/language_spec.lang``. The same package directory
is added to a per-editor :class:`GtkSource.LanguageManager` search
path so the file is discoverable at runtime.
"""

AUTOSAVE_DEBOUNCE_MS: Final[int] = 300
"""Debounce delay between the last buffer change and the auto-save.

A brisk typist generates dozens of buffer-changed signals a second;
saving on every one would hammer the database. 300 ms is a typical
editor latency budget — long enough to coalesce keystrokes, short
enough that the user does not notice a lag when they pause.
"""

AUTOSAVE_TOOLTIP: Final[str] = "Saves automatically as you type"
"""Static tooltip the trailing toolbar status label exposes.

Matches the design's ``<span>AsciiDoc</span>`` corner indicator —
the editor never needs an explicit save button because every change
is debounced into a save 300 ms later.
"""


# ---------------------------------------------------------------------------
# Toolbar button definitions — the step-10 core set
# ---------------------------------------------------------------------------
#
# Each tuple is rendered as one button. ``label`` is the text the
# button shows (no icons in step 10 — icons land at step 12 alongside
# the rest of the toolbar polish). ``tooltip`` is the hover hint.
# ``action_kind`` discriminates between the two action helpers:
# wrapping a selection in delimiters or inserting a block line. The
# remaining tuple slots carry the action's payload.

# A tuple-typed namespace would force pylint warnings about magic
# indexing; using simple dataclasses or plain functions is a heavier
# introduction than this file warrants. The toolbar has nine buttons,
# all listed below as plain literals — easy to read top-to-bottom and
# trivial to extend.

_HEADING_TEXT: Final[str] = "== Heading"
_BULLET_LIST_TEXT: Final[str] = "* item"
_NUMBERED_LIST_TEXT: Final[str] = ". item"
_CODE_BLOCK_TEMPLATE: Final[str] = "----\ncode\n----"
_IMAGE_PLACEHOLDER_TEXT: Final[str] = "image::filename.png[]"

_BOLD_DELIMITER: Final[str] = "*"
_ITALIC_DELIMITER: Final[str] = "_"
_STRIKETHROUGH_OPEN: Final[str] = "[.line-through]#"
_STRIKETHROUGH_CLOSE: Final[str] = "#"
_UNDERLINE_OPEN: Final[str] = "[.underline]#"
_UNDERLINE_CLOSE: Final[str] = "#"

_PLACEHOLDER_SELECTION_TEXT: Final[str] = "text"
"""Placeholder string inserted by :func:`wrap_selection` when the user
clicks a wrap button without first selecting any text. The wrap is
applied around the placeholder so the visible result is e.g.
``*text*`` rather than an empty pair of asterisks; the placeholder is
left selected so the user's next keystroke replaces it. The same
string is used by the React reference implementation
(``noteedit.jsx``)."""


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
    return GLib.timeout_add(delay_ms, callback)


def _default_timeout_canceller(handle: int) -> None:
    """Production canceller — wraps :func:`GLib.source_remove`."""
    GLib.source_remove(handle)


# ---------------------------------------------------------------------------
# Pure helpers — operate on Gtk.TextBuffer with no display required
# ---------------------------------------------------------------------------


def wrap_selection(  # pylint: disable=too-many-locals
    buffer: Gtk.TextBuffer,
    *,
    before: str,
    after: str,
) -> None:
    """Wrap the buffer's current selection in ``before`` / ``after``.

    If the user has no selection at the cursor, the literal string
    :data:`_PLACEHOLDER_SELECTION_TEXT` is inserted between the
    delimiters and selected, so a click on (e.g.) the Bold button on
    an empty cursor yields ``*text*`` with ``text`` highlighted ready
    to be typed over.

    Pure with respect to the buffer: a single ``begin_user_action`` /
    ``end_user_action`` envelope groups the change so a single Ctrl-Z
    undoes the whole wrap, matching the design's behaviour and
    GtkSourceView's undo grouping conventions.

    No display is required to call this — the function is unit-tested
    against a vanilla :class:`Gtk.TextBuffer` built from scratch.
    """
    bounds = buffer.get_selection_bounds()
    buffer.begin_user_action()
    try:
        if bounds is None or len(bounds) != 2:
            # No selection: insert the placeholder, then surround it.
            insert_mark = buffer.get_insert()
            cursor_iter = buffer.get_iter_at_mark(insert_mark)
            cursor_offset = cursor_iter.get_offset()
            buffer.insert(cursor_iter, _PLACEHOLDER_SELECTION_TEXT)
            # Re-acquire iters: `insert` invalidates them.
            placeholder_start = buffer.get_iter_at_offset(cursor_offset)
            placeholder_end = buffer.get_iter_at_offset(
                cursor_offset + len(_PLACEHOLDER_SELECTION_TEXT)
            )
            sel_start_offset = placeholder_start.get_offset()
            sel_end_offset = placeholder_end.get_offset()
        else:
            start, end = bounds
            sel_start_offset = start.get_offset()
            sel_end_offset = end.get_offset()

        # Insert the closing delimiter first, then the opening one. We
        # do it in this order so the offsets we recorded above for the
        # selection's start/end remain valid for the second insert:
        # inserting at the end first does not shift positions before
        # it, but inserting at the start first WOULD shift the end's
        # position forward by ``len(before)``.
        end_iter = buffer.get_iter_at_offset(sel_end_offset)
        buffer.insert(end_iter, after)
        start_iter = buffer.get_iter_at_offset(sel_start_offset)
        buffer.insert(start_iter, before)

        # Re-select the original selection text (now shifted by
        # ``len(before)``). This matches the React reference's
        # ``setSelectionRange`` after a wrap: the user's content
        # remains highlighted so they can continue editing it.
        new_start = buffer.get_iter_at_offset(sel_start_offset + len(before))
        new_end = buffer.get_iter_at_offset(sel_end_offset + len(before))
        buffer.select_range(new_start, new_end)
    finally:
        buffer.end_user_action()


def insert_block_line(buffer: Gtk.TextBuffer, *, text: str) -> None:
    """Insert ``text`` as a fresh block at the cursor.

    The semantics mirror the React reference's ``insertLine``:

    * If the cursor is at the start of an empty line, the block is
      inserted in place — no leading newline.
    * Otherwise a leading newline is inserted first, so the block
      lands on its own line below whatever the cursor was sitting on.
    * A trailing newline is always appended so the cursor lands on
      a blank line ready for the next paragraph.

    The cursor is repositioned at the end of the inserted text (just
    before the trailing newline), again matching the reference
    behaviour. Unit-testable against a vanilla :class:`Gtk.TextBuffer`.
    """
    insert_mark = buffer.get_insert()
    cursor_iter = buffer.get_iter_at_mark(insert_mark)
    cursor_offset = cursor_iter.get_offset()

    # Determine whether the cursor sits on an otherwise-empty line.
    # The line is "empty" for the purposes of this rule when the
    # characters between the line start and the cursor are entirely
    # whitespace. We rely on :class:`Gtk.TextIter.get_line_offset`
    # to find the column, then read those bytes.
    line_start = buffer.get_iter_at_offset(cursor_offset)
    line_start.set_line_offset(0)
    line_prefix = buffer.get_text(line_start, cursor_iter, False)
    leading_newline = "" if line_prefix.strip() == "" else "\n"

    payload = leading_newline + text + "\n"

    buffer.begin_user_action()
    try:
        buffer.insert(cursor_iter, payload)
        # Re-acquire the iter for cursor placement: the previous one
        # is invalidated by ``insert``. Place the cursor just before
        # the trailing newline so the user is positioned at the end
        # of the inserted block, ready to type continuation text.
        cursor_target = cursor_offset + len(leading_newline) + len(text)
        new_cursor = buffer.get_iter_at_offset(cursor_target)
        buffer.place_cursor(new_cursor)
    finally:
        buffer.end_user_action()


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
    return buffer.get_text(start, end, False)


# ---------------------------------------------------------------------------
# Language file loading
# ---------------------------------------------------------------------------


def _bundled_language_dir() -> Path:
    """Directory containing the bundled ``language_spec.lang`` file.

    Resolves at import time without touching ``sys.path`` games:
    :mod:`notes_app.asciidoc` is the package the file lives next to,
    so its ``__file__`` attribute's parent is the directory we want.
    """
    asciidoc_init = _asciidoc_pkg.__file__
    if asciidoc_init is None:
        # The package has no __file__ only in exotic packaging
        # scenarios (frozen executables, namespace packages). The
        # editor is shipped as a regular installable package so this
        # branch should be unreachable in practice — the assertion is
        # defence against future refactors that might break the
        # invariant silently.
        raise RuntimeError(
            "notes_app.asciidoc is missing __file__; cannot locate the "
            "bundled language definition."
        )
    return Path(asciidoc_init).parent


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
    (separate from the process-global default) and prepends the
    package's directory to its search path. Subsequent calls return
    the cached instance unchanged.
    """
    global _LANGUAGE_MANAGER  # pylint: disable=global-statement
    if _LANGUAGE_MANAGER is not None:
        return _LANGUAGE_MANAGER
    manager = GtkSource.LanguageManager.new()
    bundled_dir = str(_bundled_language_dir())
    existing = list(manager.get_search_path() or ())
    # Prepend so our bundled grammar wins over a system-installed
    # ``notes-asciidoc`` (none ships, but defending against an id
    # collision is cheap and keeps surprising debug stories at bay).
    manager.set_search_path([bundled_dir, *existing])
    _LANGUAGE_MANAGER = manager
    return manager


def load_asciidoc_language() -> GtkSource.Language | None:
    """Return the bundled AsciiDoc :class:`GtkSource.Language`, or
    ``None`` if it can't be loaded.

    Uses the module-cached :func:`_get_language_manager` so the
    underlying :class:`GtkSource.LanguageManager` outlives any single
    editor instance — see :data:`_LANGUAGE_MANAGER` for the lifecycle
    rationale. ``None`` is returned only when the file is missing or
    the language id does not match — both indicate a packaging bug,
    but the editor still works as a plain text editor in that
    degraded state, so we do not raise.
    """
    return _get_language_manager().get_language(LANGUAGE_ID)


# ---------------------------------------------------------------------------
# NoteEditor widget
# ---------------------------------------------------------------------------


class NoteEditor(Gtk.Box):  # pylint: disable=too-many-instance-attributes
    """The source-editing pane.

    Construction signature is keyword-only and mirrors the other
    panes' shape: a repository (read-only — the editor never writes
    directly), the controller that owns ``update_source``, and the
    :class:`AppState` instance the rest of the UI is wired to.

    The two timeout primitives default to GLib's main-loop-backed
    versions; tests override them with fakes so debounce behaviour can
    be asserted synchronously.

    The instance-attribute count exceeds pylint's default ceiling of
    seven because the editor genuinely depends on five injected
    collaborators (repository, controller, app state, scheduler,
    canceller) plus three short-lived pieces of state (buffer,
    source-view, current-note id), plus two booleans tracking the
    save-pending and load-in-progress flags. Splitting these into a
    helper would create a second class with no clear surface — every
    field is read or written from at least two methods.
    """

    _note_repository: NoteRepositoryProtocol
    _note_controller: NoteController
    _app_state: AppState
    _schedule_timeout: TimeoutScheduler
    _cancel_timeout: TimeoutCanceller

    _buffer: GtkSource.Buffer
    _source_view: GtkSource.View

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
        note_repository: NoteRepositoryProtocol,
        note_controller: NoteController,
        app_state: AppState,
        schedule_timeout: TimeoutScheduler = _default_timeout_scheduler,
        cancel_timeout: TimeoutCanceller = _default_timeout_canceller,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
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

        # Toolbar above the editor — built from the static button
        # spec list. Each button's ``clicked`` handler binds the
        # appropriate pure helper (wrap or insert).
        toolbar = self._build_toolbar()
        self.append(toolbar)

        # The editor proper: GtkSource.View inside a ScrolledWindow.
        scrolled_window = Gtk.ScrolledWindow.new()
        scrolled_window.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        scrolled_window.set_child(self._source_view)
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)
        self.append(scrolled_window)

        # Wire up signals last so handlers don't fire mid-construction.
        self._buffer.connect("changed", self._on_buffer_changed)
        self._app_state.connect(
            "selected-note-changed",
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
        callers that want to render contextual UI (e.g. the
        breadcrumb in the toolbar, step 12) without consulting
        :class:`AppState` directly. ``None`` when no note is
        selected — the buffer is empty in that case.
        """
        return self._current_note_id

    # ------------------------------------------------------------------
    # Toolbar construction
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> Gtk.Box:
        """Construct the editor's toolbar — the step-10 core set.

        The toolbar is a horizontal :class:`Gtk.Box`. Each button is
        a :class:`Gtk.Button` whose clicked signal binds a closure
        over the appropriate pure helper. Separators between groups
        (inline / lists / blocks / image) match the design's
        ``<div class="div"/>`` spacers.
        """
        toolbar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        # Inline-formatting group: heading, bold, italic, strike, underline.
        toolbar.append(
            self._make_insert_button(
                label="H",
                tooltip="Heading",
                text=_HEADING_TEXT,
            )
        )
        toolbar.append(
            self._make_wrap_button(
                label="B",
                tooltip="Bold",
                before=_BOLD_DELIMITER,
                after=_BOLD_DELIMITER,
            )
        )
        toolbar.append(
            self._make_wrap_button(
                label="I",
                tooltip="Italic",
                before=_ITALIC_DELIMITER,
                after=_ITALIC_DELIMITER,
            )
        )
        toolbar.append(
            self._make_wrap_button(
                label="S",
                tooltip="Strikethrough",
                before=_STRIKETHROUGH_OPEN,
                after=_STRIKETHROUGH_CLOSE,
            )
        )
        toolbar.append(
            self._make_wrap_button(
                label="U",
                tooltip="Underline",
                before=_UNDERLINE_OPEN,
                after=_UNDERLINE_CLOSE,
            )
        )

        toolbar.append(Gtk.Separator.new(Gtk.Orientation.VERTICAL))

        # Lists group.
        toolbar.append(
            self._make_insert_button(
                label="•",
                tooltip="Bullet list",
                text=_BULLET_LIST_TEXT,
            )
        )
        toolbar.append(
            self._make_insert_button(
                label="1.",
                tooltip="Numbered list",
                text=_NUMBERED_LIST_TEXT,
            )
        )

        toolbar.append(Gtk.Separator.new(Gtk.Orientation.VERTICAL))

        # Blocks group: code block + image-macro placeholder.
        toolbar.append(
            self._make_insert_button(
                label="</>",
                tooltip="Code block",
                text=_CODE_BLOCK_TEMPLATE,
            )
        )
        toolbar.append(
            self._make_insert_button(
                label="Image",
                tooltip="Insert image macro",
                text=_IMAGE_PLACEHOLDER_TEXT,
            )
        )

        # A horizontal-expanding spacer pushes the trailing AsciiDoc
        # label to the right edge.
        spacer = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, spacing=0)
        spacer.set_hexpand(True)
        toolbar.append(spacer)

        autosave_label = Gtk.Label.new("AsciiDoc")
        autosave_label.set_tooltip_text(AUTOSAVE_TOOLTIP)
        autosave_label.add_css_class("dim-label")
        toolbar.append(autosave_label)

        return toolbar

    def _make_wrap_button(
        self,
        *,
        label: str,
        tooltip: str,
        before: str,
        after: str,
    ) -> Gtk.Button:
        """Build a button that wraps the current selection in delimiters."""
        button = Gtk.Button.new_with_label(label)
        button.set_tooltip_text(tooltip)
        # The lambda below closes over ``before`` and ``after``
        # by-name, which is intentionally capture-by-reference: the
        # call is dispatched only when the user clicks, by which time
        # the loop variable contention that catches Python closures
        # has been resolved (these are not loop-bound — each call to
        # this method has its own ``before`` / ``after``).
        button.connect(
            "clicked",
            lambda _b: wrap_selection(
                self._buffer,
                before=before,
                after=after,
            ),
        )
        return button

    def _make_insert_button(
        self,
        *,
        label: str,
        tooltip: str,
        text: str,
    ) -> Gtk.Button:
        """Build a button that inserts a block-level line."""
        button = Gtk.Button.new_with_label(label)
        button.set_tooltip_text(tooltip)
        button.connect(
            "clicked",
            lambda _b: insert_block_line(self._buffer, text=text),
        )
        return button

    # ------------------------------------------------------------------
    # Selection / load handling
    # ------------------------------------------------------------------

    def _on_selected_note_changed(self, _app_state: AppState) -> None:
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
        """
        note_id = self._app_state.selected_note_id

        self._loading_note = True
        try:
            if note_id is None:
                self._buffer.set_text("")
                self._current_note_id = None
                return
            try:
                note = self._note_repository.get(note_id)
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
        return GLib.SOURCE_REMOVE

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

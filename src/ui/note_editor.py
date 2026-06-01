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
* The bundled :data:`LANGUAGE_ID` AsciiDoc grammar is what
  :class:`GtkSource.View` highlights against. The grammar covers only
  the step-4 subset (sections, lists, code blocks, image macros,
  bold, italic, strikethrough, underline) because anything richer
  would highlight constructs the parser still rejects, and surfacing
  a green check on something the renderer crashes on is exactly the
  sort of dishonest UX feedback we are avoiding. Steps 13–15 extend
  the grammar in lock-step with the parser.
* The grammar is **always loaded from the compiled
  ``folio.gresource``** via a ``resource:///`` search path — never
  from a filesystem path — so a source checkout and the packaged
  ``folio.pyz`` behave identically (inside the zip a filesystem path
  would point *into* the archive and the OS could not open it). The
  GResource is built from the committed ``folio.gresource.xml`` +
  ``language_spec.lang`` by ``run`` / ``make`` before any entry point
  runs; it is registered exactly once behind the cached
  :data:`_LANGUAGE_MANAGER` (see :func:`_configure_search_path`), and a
  *missing* resource is a hard :class:`FileNotFoundError`, not a silent
  fallback to plain-text highlighting.
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
* Toolbar surface as of step 15 covers Heading, Bold, Italic,
  Strikethrough, Underline, Monospace, Link, Bullet list, Numbered
  list, Code block, Image, Table, Admonition, and Blockquote.
  Adding all the buttons up-front would imply support the parser
  does not yet have, which the strict-mode policy of the parser
  would surface as a parse error the moment the user clicked the
  premature button.
* The image button opens a :class:`Gtk.FileDialog` (the GTK 4.10
  current API; :class:`Gtk.FileChooserDialog` is deprecated). On a
  successful pick it routes the chosen path through
  :meth:`NoteController.add_attachment`, which performs validation
  (size cap, MIME allow-list, readability) inside
  :class:`AttachmentStore`. On success the controller returns an
  :class:`Attachment`; the editor inserts an
  ``image::<filename>[]`` macro referring to the attachment's
  filename. On rejection the controller's
  ``attachment-rejected`` signal emits a typed reason for the
  outer toast layer to surface — the editor itself does not
  insert anything in that case (no half-formed macro left in the
  buffer).
* The dialog *opener* is injected as a :data:`FileDialogOpener`
  callable so tests can drive the post-pick code path
  synchronously, without spinning a real dialog. Production wires
  :func:`default_file_dialog_opener` from
  :mod:`ui._image_picker`, which constructs a
  :class:`Gtk.FileDialog` configured with image-only filters and
  invokes its asynchronous ``open()``. The injection pattern
  mirrors :data:`TimeoutScheduler` / :data:`TimeoutCanceller`.
* The image button is **disabled** while no note is currently
  selected. Without a note id, attachments cannot be associated
  with anything, and silently inserting a macro into an
  empty/unselectable buffer would be confusing. The button's
  sensitivity tracks :attr:`AppState.selected_note_id` via the
  same ``notify::selected-note-id`` subscription already wired for
  the buffer load.
* GTK 4 currency: :class:`GtkSource.Buffer` (not the deprecated
  :class:`GtkSource.Buffer` v3 paths), :meth:`Gtk.Box.append`,
  :meth:`Gtk.Button.set_child` (rather than the long-form box-of-
  icon-and-label). No deprecated-in-4.18 calls.
"""

# pylint: disable=too-many-lines
# Step 11 brought the editor just over pylint's default 1000-line
# ceiling because the image-button file-dialog flow adds wiring at
# every layer of the class: a new injected dependency, a new field,
# a button-construction helper, two click-handler methods, and
# selection-driven sensitivity tracking. The file-dialog opener
# itself was already extracted to ``_image_picker.py``; further
# splitting would scatter tightly coupled code across files for no
# readability benefit.

from __future__ import annotations

import importlib.resources
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
# pylint: disable=wrong-import-position
from gi.repository import Gio, GLib, GObject, Gtk, GtkSource  # noqa: E402

from controllers.app_state import AppState
from controllers.note_controller import NoteController
from storage.protocols import NoteRepositoryProtocol
from ui._image_picker import (
    FileDialogOpener,
    default_file_dialog_opener,
)


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

_GRESOURCE_NAME: Final[str] = "folio.gresource"
"""File name of the compiled GResource bundle shipped next to this module.

The bundle is a **generated artifact** (``src/ui/folio.gresource``,
gitignored) compiled from the committed ``folio.gresource.xml`` manifest
plus ``language_spec.lang`` by ``glib-compile-resources``. Every entry
point (``run``, ``make test``, ``make pyz``) builds it before launch, so
the file is present whether the app runs from a source checkout or from
inside ``folio.pyz``. It is read out of the package via
:func:`importlib.resources.files`, which resolves identically in both
cases because ``ui`` stays a real package at the archive root.
"""

_GRESOURCE_LANG_DIR: Final[str] = "resource:///org/folio/language-specs"
"""``resource:///`` directory URI the grammar is published under.

Matches the ``prefix`` of the ``<gresource>`` element in
``folio.gresource.xml``. Handed verbatim to
:meth:`GtkSource.LanguageManager.set_search_path`, which (since
GtkSourceView 5.4) accepts a ``resource:///`` directory naming a folder
inside a registered GResource as well as an on-disk directory. The
grammar's ``language_spec.lang`` resolves at
``resource:///org/folio/language-specs/language_spec.lang``.
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
_TABLE_TEMPLATE: Final[str] = (
    "|===\n"
    "|Column A|Column B\n"
    "|cell 1|cell 2\n"
    "|cell 3|cell 4\n"
    "|==="
)
"""AsciiDoc snippet inserted by the Table toolbar button.

A 2-column, 1-header-plus-2-row table — the smallest example that
shows the user the syntax of header / body rows and cell separators
without dropping a degenerate single-row table that hides the
arity-mismatch behaviour. The user adds, removes, or renames cells
from there.
"""

_ADMONITION_TEMPLATE: Final[str] = (
    "[NOTE]\n"
    "====\n"
    "Note text.\n"
    "===="
)
"""AsciiDoc snippet inserted by the Admonition toolbar button.

A block ``[NOTE]`` admonition rather than the single-line
``NOTE: text`` shape — the block form is the more general one and
shows the user the fence syntax. ``NOTE`` is the safest default
kind: it has no negative connotation (unlike ``WARNING`` /
``CAUTION``) and matches the design's reference admonition. The
user changes the kind by editing ``[NOTE]`` to ``[TIP]`` /
``[IMPORTANT]`` / ``[WARNING]`` / ``[CAUTION]`` directly.
"""

_BLOCKQUOTE_TEMPLATE: Final[str] = (
    "[quote, Author, Source]\n"
    "____\n"
    "Quoted text.\n"
    "____"
)
"""AsciiDoc snippet inserted by the Blockquote toolbar button.

A blockquote with the full ``[quote, Author, Source]`` directive so
the user sees the attribution shape. They delete the directive line
to drop attribution entirely, or remove the ``, Source`` portion to
keep just the author. Empty author / source fields raise a parse
error at render time, so the placeholders are non-empty literals
the user can edit in place.
"""


def _image_macro_for_filename(filename: str) -> str:
    """Build the AsciiDoc image macro that references ``filename``.

    The macro is the line ``image::<filename>[]`` — empty attribute
    list because the v1 parser ignores image attributes. Centralised
    so the editor's toolbar handler and any future "insert image"
    code path share one source of truth.
    """
    return f"image::{filename}[]"

_BOLD_DELIMITER: Final[str] = "*"
_ITALIC_DELIMITER: Final[str] = "_"
_STRIKETHROUGH_OPEN: Final[str] = "[.line-through]#"
_STRIKETHROUGH_CLOSE: Final[str] = "#"
_UNDERLINE_OPEN: Final[str] = "[.underline]#"
_UNDERLINE_CLOSE: Final[str] = "#"
_MONOSPACE_DELIMITER: Final[str] = "`"

# The Link button inserts a syntactically-valid ``link:`` macro
# template inline at the cursor. ``link:https://example.com[link text]``
# parses cleanly, so the user gets a working link they can edit in
# place rather than a half-formed shape that the rendered view
# immediately rejects. The URL placeholder is left selected on insert
# so the user's next keystroke overwrites it with a real URL — same
# UX pattern as the wrap buttons' ``text`` placeholder.
_LINK_PREFIX: Final[str] = "link:"
_LINK_PLACEHOLDER_URL: Final[str] = "https://example.com"
_LINK_PLACEHOLDER_LABEL: Final[str] = "link text"
_LINK_TEMPLATE: Final[str] = (
    f"{_LINK_PREFIX}{_LINK_PLACEHOLDER_URL}"
    f"[{_LINK_PLACEHOLDER_LABEL}]"
)
# Selection range (relative to the start of the inserted template)
# that highlights the URL portion. Computed at module-import time
# from the constant template so a future tweak to the placeholder
# text does not desynchronise the selection offsets.
_LINK_TEMPLATE_URL_SELECTION: Final[tuple[int, int]] = (
    len(_LINK_PREFIX),
    len(_LINK_PREFIX) + len(_LINK_PLACEHOLDER_URL),
)

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
    handle: int = GLib.timeout_add(delay_ms, callback)
    return handle


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


def insert_inline_text(
    buffer: Gtk.TextBuffer,
    *,
    text: str,
    select_within: tuple[int, int] | None = None,
) -> None:
    """Insert ``text`` at the cursor as inline content (no leading newline).

    Used by toolbar buttons that produce inline templates — the link
    button's ``link:URL[label]`` is the canonical example. Unlike
    :func:`insert_block_line`, the cursor's current line context is
    preserved: the inserted text becomes part of whatever paragraph
    the cursor was sitting in.

    ``select_within`` is an optional ``(start_offset, end_offset)``
    pair, **relative to the start of the inserted text**, identifying
    a substring to highlight after the insert. The link button uses
    this to leave the URL portion (``https://example.com``) selected
    so the user's first keystroke replaces the placeholder URL with
    a real one. When :data:`None`, the cursor is left at the end of
    the inserted text and nothing is selected.

    A single ``begin_user_action`` / ``end_user_action`` envelope
    groups the whole operation so one Ctrl-Z undoes the entire
    insert. Pure with respect to the buffer; unit-testable against
    a vanilla :class:`Gtk.TextBuffer`.
    """
    insert_mark = buffer.get_insert()
    cursor_iter = buffer.get_iter_at_mark(insert_mark)
    cursor_offset = cursor_iter.get_offset()

    buffer.begin_user_action()
    try:
        buffer.insert(cursor_iter, text)
        if select_within is None:
            new_cursor = buffer.get_iter_at_offset(cursor_offset + len(text))
            buffer.place_cursor(new_cursor)
            return
        start_rel, end_rel = select_within
        sel_start = buffer.get_iter_at_offset(cursor_offset + start_rel)
        sel_end = buffer.get_iter_at_offset(cursor_offset + end_rel)
        buffer.select_range(sel_start, sel_end)
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
    text: str = buffer.get_text(start, end, False)
    return text


# ---------------------------------------------------------------------------
# Language file loading
# ---------------------------------------------------------------------------


def _configure_search_path(manager: GtkSource.LanguageManager) -> None:
    """Register the compiled GResource and point ``manager`` at it.

    The grammar always loads from the compiled
    :data:`_GRESOURCE_NAME` blob — never from a filesystem path — so a
    source checkout and the packaged ``folio.pyz`` behave identically.
    The blob is read out of the ``ui`` package with
    :func:`importlib.resources.files`, which resolves whether ``ui`` is
    an on-disk directory (``src/ui/folio.gresource``) or a folder inside
    the zip. The bytes are registered as a process-global
    :class:`Gio.Resource`, after which the grammar is reachable at
    :data:`_GRESOURCE_LANG_DIR` and that ``resource:///`` directory is
    prepended to the manager's search path (since GtkSourceView 5.4 the
    search path accepts ``resource:///`` directory URIs).

    A missing :data:`_GRESOURCE_NAME` makes :meth:`read_bytes` raise
    :class:`FileNotFoundError`. That is the intended behaviour — a hard,
    obvious error pointing at "build the resource first" rather than a
    silent fallback to plain-text highlighting. Registration happens
    exactly once per process because the only caller,
    :func:`_get_language_manager`, is itself memoised behind
    :data:`_LANGUAGE_MANAGER`.
    """
    existing = list(manager.get_search_path() or ())
    blob = GLib.Bytes.new(
        importlib.resources.files("ui").joinpath(_GRESOURCE_NAME).read_bytes()
    )
    Gio.resources_register(Gio.Resource.new_from_data(blob))
    # Prepend so our bundled grammar wins over a system-installed
    # ``notes-asciidoc`` (none ships, but defending against an id
    # collision is cheap and keeps surprising debug stories at bay).
    manager.set_search_path([_GRESOURCE_LANG_DIR, *existing])


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
    _open_file_dialog: FileDialogOpener

    _buffer: GtkSource.Buffer
    _source_view: GtkSource.View
    _image_button: Gtk.Button

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
        file_dialog_opener: FileDialogOpener = default_file_dialog_opener,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._note_repository = note_repository
        self._note_controller = note_controller
        self._app_state = app_state
        self._schedule_timeout = schedule_timeout
        self._cancel_timeout = cancel_timeout
        self._open_file_dialog = file_dialog_opener

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
        """Construct the editor's toolbar — the step-15 core set.

        The toolbar is a horizontal :class:`Gtk.Box`. Each button is
        a :class:`Gtk.Button` whose clicked signal binds a closure
        over the appropriate pure helper. Separators between groups
        (inline / lists / blocks / image) match the design's
        ``<div class="div"/>`` spacers.

        Step 13 added the Monospace and Link buttons at the end of
        the inline group: monospace is a wrap-button with the
        backtick delimiter, and link is an inline-insert that drops
        a ``link:URL[label]`` template at the cursor with the URL
        portion preselected for immediate replacement.

        Step 14 added the Table button at the end of the blocks
        group: it inserts a small 2-column table template that the
        user can fill in.

        Step 15 adds the Admonition and Blockquote buttons at the
        end of the blocks group. The admonition button drops a
        ``[NOTE]``-fenced block; the blockquote button drops a
        ``[quote, Author, Source]``-attributed quote block. Both
        templates parse cleanly through the parser so the rendered
        view does not flash a parse error after the click.
        """
        toolbar = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_margin_top(4)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        # Inline-formatting group: heading, bold, italic, strike,
        # underline, monospace, link.
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
        toolbar.append(
            self._make_wrap_button(
                label="M",
                tooltip="Monospace",
                before=_MONOSPACE_DELIMITER,
                after=_MONOSPACE_DELIMITER,
            )
        )
        toolbar.append(
            self._make_inline_insert_button(
                label="🔗",
                tooltip="Link",
                text=_LINK_TEMPLATE,
                select_within=_LINK_TEMPLATE_URL_SELECTION,
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

        # Blocks group: code block + image + table.
        toolbar.append(
            self._make_insert_button(
                label="</>",
                tooltip="Code block",
                text=_CODE_BLOCK_TEMPLATE,
            )
        )
        # The image button is the one toolbar button whose handler is
        # not a one-line wrapper around the pure helpers — it opens
        # a file dialog, validates the pick through the controller,
        # and only then inserts a macro. The button reference is kept
        # on ``self`` so :meth:`_on_selected_note_changed` can toggle
        # its sensitivity (no selection → image button disabled).
        self._image_button = self._make_image_button()
        toolbar.append(self._image_button)
        toolbar.append(
            self._make_insert_button(
                label="⊞",
                tooltip="Table",
                text=_TABLE_TEMPLATE,
            )
        )
        toolbar.append(
            self._make_insert_button(
                label="ⓘ",
                tooltip="Admonition",
                text=_ADMONITION_TEMPLATE,
            )
        )
        toolbar.append(
            self._make_insert_button(
                label="❝",
                tooltip="Blockquote",
                text=_BLOCKQUOTE_TEMPLATE,
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

    def _make_inline_insert_button(
        self,
        *,
        label: str,
        tooltip: str,
        text: str,
        select_within: tuple[int, int] | None,
    ) -> Gtk.Button:
        """Build a button that inserts inline content at the cursor.

        Used by the Link button — the link macro is inline content,
        not a block, so :func:`insert_block_line` (which forces a
        new line) is the wrong primitive. ``select_within`` selects
        a range of the inserted text post-insert so the user can
        immediately overwrite a placeholder URL or label.
        """
        button = Gtk.Button.new_with_label(label)
        button.set_tooltip_text(tooltip)
        button.connect(
            "clicked",
            lambda _b: insert_inline_text(
                self._buffer,
                text=text,
                select_within=select_within,
            ),
        )
        return button

    def _make_image_button(self) -> Gtk.Button:
        """Build the toolbar's Image button.

        Unlike the other toolbar buttons, the image button's click
        handler opens an injected file dialog, awaits the result
        asynchronously, and on a non-cancelled pick routes the path
        through :meth:`NoteController.add_attachment`. A successful
        attachment add is followed by an ``image::<filename>[]``
        macro insertion at the cursor; a rejection (size cap, MIME
        type, unreadable file) emits the controller's typed
        ``attachment-rejected`` signal — already wired by upstream
        layers to a toast — and inserts nothing.

        Initial sensitivity tracks the current selection: no note
        selected → button disabled. The
        :meth:`_on_selected_note_changed` handler updates this on
        every selection change.
        """
        button = Gtk.Button.new_with_label("Image")
        button.set_tooltip_text("Insert image")
        button.set_sensitive(self._current_note_id is not None)
        button.connect("clicked", lambda _b: self._on_image_button_clicked())
        return button

    def _on_image_button_clicked(self) -> None:
        """Open the file dialog and queue the post-pick handler.

        The button's sensitivity already gates this on a non-None
        selection, but the defensive check inside is what protects
        against a programmatic ``button.emit("clicked")`` from a
        future caller that bypasses the sensitivity check.
        """
        if self._current_note_id is None:
            return
        # ``self`` is the parent widget for the dialog; the default
        # opener walks up to the root window.
        self._open_file_dialog(self, self._on_image_picked)

    def _on_image_picked(self, source_path: Path | None) -> None:
        """Handle the file-dialog result.

        ``source_path`` is :data:`None` when the user cancelled or
        when the dialog backend reported an error. In both cases we
        insert nothing — silence is the correct UX for "user changed
        their mind".

        On a real path, route through
        :meth:`NoteController.add_attachment`. The controller emits
        the typed rejection signal on failure (and returns
        :data:`None` to us); on success it returns an
        :class:`Attachment` whose filename we splice into the macro.
        """
        if source_path is None:
            return
        if self._current_note_id is None:
            # Selection cleared between the dialog opening and the
            # callback. Without a note id the attachment cannot be
            # associated with anything; silently bailing is the
            # least-surprising outcome.
            return
        attachment = self._note_controller.add_attachment(
            self._current_note_id,
            source_path,
        )
        if attachment is None:
            # Rejected — controller already emitted
            # ``attachment-rejected``. The toast layer reacts; the
            # editor leaves the buffer untouched.
            return
        # The macro references the *stored* filename (which equals
        # ``source_path.name``). Using the stored filename rather
        # than the original path means the macro stays valid even
        # if the source file is later moved or deleted.
        insert_block_line(
            self._buffer,
            text=_image_macro_for_filename(attachment.filename),
        )

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

        After the buffer is updated, the image button's sensitivity
        is synchronised to the current selection: a ``None`` id
        disables the button (no note to attach to), any other id
        enables it. The toggle happens *after* the load so the
        button's enabled state mirrors the actual editing target,
        not a transient mid-load state.
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
            # ``_image_button`` may not exist yet on the very first
            # ``__init__``-time call (the toolbar is built before
            # the load runs). Guard with ``hasattr``; the toolbar
            # construction sets the initial sensitivity itself, so
            # the very first load does not need to touch the button.
            if hasattr(self, "_image_button"):
                self._image_button.set_sensitive(
                    self._current_note_id is not None
                )

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

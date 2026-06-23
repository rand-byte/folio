"""Application-wide constants and the seed data used on first launch.

Principles & invariants
-----------------------
* Every numeric or string value the app reuses across modules lives here
  if it is plausibly tunable. This avoids the dual problem of magic
  numbers scattered through implementation files and of a single
  "settings" module that ends up importing half the application.
* ``MAX_ATTACHMENT_BYTES`` is the only quota the storage layer enforces.
  It is checked via :meth:`pathlib.Path.stat` before any bytes are read,
  so an over-limit file never enters memory. Changing the value affects
  in-flight rejections but never invalidates already-stored attachments.
* ``TARGET_CHARS_PER_LINE`` parameterises the rendered-view text column
  width. The pixel width is computed by the UI layer once per font as
  ``TARGET_CHARS_PER_LINE`` × measured glyph width and cached. Changing
  this value is a typography decision, not a runtime tuning knob, so it is
  intentionally not exposed in any settings panel in v1.
* The four ``ARTICLE_*`` multipliers parameterise the rendered-view
  ``Gtk.TextView``'s spacing. Three are breathing-space margins
  (``ARTICLE_TOP_MARGIN_LINES`` / ``ARTICLE_BOTTOM_MARGIN_LINES`` above
  and below the content, ``ARTICLE_INNER_HPADDING_CHARS`` between the
  article column's edge and the text); the fourth,
  ``ARTICLE_END_GAP_LINES``, is the minimum band of *desk* (the
  scroller's own background, not the white sheet) kept below the note
  when it is scrolled to its end, so a long note ends at a visible edge
  the way a short one already does. The view's actual ``bottom-margin``
  is the sum of ``ARTICLE_BOTTOM_MARGIN_LINES`` and
  ``ARTICLE_END_GAP_LINES`` (breathing sheet + scrollable desk gap),
  while the painted sheet stops after only the breathing part — the
  difference is the desk band. Values are font-relative: the line-based
  ones are multiples of the body font's measured line height, the inner
  horizontal padding a multiple of its "M" glyph width. Like
  ``TARGET_CHARS_PER_LINE`` these are typography decisions — measured
  once per font in the UI layer, cached for the container's lifetime,
  and intentionally not exposed in any settings panel.
* :data:`SEED_WELCOME_NOTE_SOURCE` is written to a fresh database by the
  v1 migration. It is never re-applied: a user who deletes the welcome
  note must not see it reappear on the next launch. The migration's own
  version-tracking enforces this.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Tunable numeric constants
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024
"""Hard upper bound on the size of a single image attachment, in bytes."""

TARGET_CHARS_PER_LINE: int = 66
"""Target text-column width in characters for the rendered view.

The 66-character target follows the typography literature's 45–75 range
for comfortable prose reading. The :class:`ArticleContainer` widget uses
this constant, multiplied by the measured glyph width of the current
font, as the fixed pixel width of the article column.
"""

ARTICLE_TOP_MARGIN_LINES: int = 4
"""Vertical breathing space above the first rendered content, expressed
as a multiple of the body font's line height. Applied as
``top-margin`` on the rendered-view ``Gtk.TextView``.
"""

ARTICLE_BOTTOM_MARGIN_LINES: int = 4
"""Vertical breathing space below the last rendered content, same units
as :data:`ARTICLE_TOP_MARGIN_LINES`. Kept symmetric so scrolling to the
end of a note does not slam the final paragraph into the viewport edge.
"""

ARTICLE_END_GAP_LINES: float = 1.5
"""Minimum band of *desk* kept below the note when it is scrolled to the end.

Expressed as a multiple of the body font's line height, like
:data:`ARTICLE_BOTTOM_MARGIN_LINES`. The rendered view paints an opaque
white *sheet* down to the end of the content plus the
:data:`ARTICLE_BOTTOM_MARGIN_LINES` breathing space; below that the
scroller's own background (the "desk") shows through. To make the sheet's
bottom edge reachable on a note taller than the viewport, the view's
``bottom-margin`` is set to ``ARTICLE_BOTTOM_MARGIN_LINES +
ARTICLE_END_GAP_LINES`` line heights — the extra ``ARTICLE_END_GAP_LINES``
is scrollable room the sheet does **not** claim, so scrolling to the very
end brings the seam into view with at least this much desk beneath it. A
short note already reveals desk; this guarantees a long one does too.

The value is a typographic choice: below ~1 line the gap reads as a clipped
last line rather than a deliberate end, above ~2.5 it wastes screen on long
notes. A non-integer value is intentional and allowed — the UI layer rounds
the pixel result.
"""

ARTICLE_INNER_HPADDING_CHARS: int = 8
"""Inner horizontal padding between the article column's edge and the
text, expressed as a multiple of the body font's ``"M"`` glyph width.
Applied as ``left-margin`` / ``right-margin`` on the rendered-view
``Gtk.TextView``. Doubled in the column's outer width calculation so the
text area stays at :data:`TARGET_CHARS_PER_LINE` characters wide.
"""

SNIPPET_MAX_CHARS: int = 200
"""Hard cap for a note's derived snippet length, in characters.

A presentation/derivation constant consumed by
:func:`asciidoc.summary.derive_summary`. Bounding the snippet
keeps the note-list query plan cheap and the rendered list cells at a
predictable height. ``config`` sits below ``asciidoc``, which imports
this value — there is no cycle.
"""

UNTITLED: str = "Untitled"
"""Fallback note title used when the source has no level-0 heading.

Part of the persistence contract: the repository writes this string
into ``notes.title`` whenever :func:`derive_summary` finds no usable
title, so the note list always has a non-empty title to show.
"""


# ---------------------------------------------------------------------------
# Seed identifiers
# ---------------------------------------------------------------------------

SEED_WELCOME_NOTE_ID: str = "seed-welcome-note"


# ---------------------------------------------------------------------------
# Seed welcome note
# ---------------------------------------------------------------------------

SEED_WELCOME_NOTE_SOURCE: str = """\
= Welcome to your notes
:tags: welcome

This is your first note. You can keep it, edit it, or delete it.

Notes are written in *AsciiDoc* — a plain-text format. The toolbar above
gives you formatting buttons for the most common things, but you can also
type the markup directly.

== A few features to try

* Type _italic_ or *bold* text inline
* Mark something as [.line-through]#done# or [.underline]#important#
* Group related ideas under a heading like the one above this list

== Step-by-step lists

. Click *New note* on the toolbar to create a note
. Add a ``:tags: foo, bar`` line under the title to file it
. Use the search box to find any note across the whole library

== Code blocks

----
def hello():
    print("Hello!")
----

Code is rendered verbatim — the editor highlights AsciiDoc itself, but
the rendered view shows the code as you wrote it.

When you are ready to start your own notes, you can safely delete this
one. It will not come back.
"""

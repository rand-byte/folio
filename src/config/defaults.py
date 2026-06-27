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
  ``ARTICLE_END_GAP_LINES``, is the band of *desk* (the scroller's own
  background, not the white sheet) kept **above and below** the note, so
  the sheet reads as a page floating on a desk with the *same* gap before
  and after the content. Both the view's ``top-margin`` and its
  ``bottom-margin`` are the sum of the matching breathing-space margin and
  ``ARTICLE_END_GAP_LINES`` (breathing sheet + desk gap), while the
  painted sheet starts after the top gap and stops before the bottom gap —
  the difference at each end is the desk band, marked by a 1-px seam. The
  gap is identical at both ends precisely because both are derived from
  this one constant, so they cannot drift. (At the bottom the band doubles
  as the scrollable room that makes a long note's end reachable; at the
  top, which is always reachable, it is purely the matching visual gap.)
  Values are font-relative: the line-based ones are multiples of the body
  font's measured line height, the inner horizontal padding a multiple of
  its "M" glyph width. Like ``TARGET_CHARS_PER_LINE`` these are typography
  decisions — measured once per font in the UI layer, cached for the
  container's lifetime, and intentionally not exposed in any settings
  panel.
* ``TABLE_CELL_HPADDING_PX`` is the symmetric horizontal padding inside
  every rendered-table cell. It is realised two ways that together read
  as one inset: the table-row paragraph tags carry it as their
  ``left-margin`` (so each column's *text* sits this far inside its
  column's left boundary), and the renderer reserves *twice* it as each
  cell's right-truncation budget (so a fitted cell ends the same
  distance short of the next column's boundary). The result is equal
  left and right padding inside the cell, while the column boundaries,
  the proportional ``[cols=…]`` geometry, and the header tint band / row
  hairline (which still span the full column) are all unchanged. The
  reserved right half also keeps a fitted cell short of its tab stop —
  the job the former per-column safety gutter did — so no separate
  gutter is subtracted. Like the other rendered-view constants it is a
  typography decision, tuned against the live column width in the UI
  layer, not exposed in settings.
* :data:`SEED_WELCOME_NOTE_ID` is the stable id of the welcome note the
  v1 migration seeds into a fresh database. The note's *source* is no
  longer kept here: it moved to the ``system_docs`` package
  (:data:`enums.SystemDocument.WELCOME`), the one config-tier home for
  system documents, so ``defaults.py`` carries only tunable constants
  and stable identifiers. The migration still applies the seed exactly
  once — its own version tracking, not anything in this module, is what
  keeps a deleted welcome note from reappearing.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Tunable numeric constants
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024
"""Hard upper bound on the size of a single image attachment, in bytes."""

MAX_LIST_DEPTH: int = 3
"""Maximum nesting depth for ordered and unordered lists.

AsciiDoc encodes list nesting as a repeated-marker run (``*``/``.`` =
level 1, ``**``/``..`` = level 2, ``***``/``...`` = level 3); this
constant is the deepest run the parser accepts. Going deeper is a hard
parse error (:data:`ParseErrorKind.LIST_NESTING_TOO_DEEP`) rather than a
silent reinterpretation. The two depth-indexed renderer tables (the
unordered-bullet glyphs and the ordered :class:`ListNumberStyle`
sequence) are sized to this value, and a renderer test asserts each
table's length equals it — so the cap and the presentation tables cannot
drift.
"""

TARGET_CHARS_PER_LINE: int = 66
"""Target text-column width in characters for the rendered view.

The 66-character target follows the typography literature's 45–75 range
for comfortable prose reading. The :class:`ArticleContainer` widget uses
this constant, multiplied by the measured glyph width of the current
font, as the fixed pixel width of the article column.
"""

ARTICLE_TOP_MARGIN_LINES: int = 4
"""Vertical breathing space above the first rendered content, expressed
as a multiple of the body font's line height. The view's ``top-margin``
is this breathing space plus the :data:`ARTICLE_END_GAP_LINES` desk band,
mirroring the bottom, so the same desk gap shows before and after a note.
"""

ARTICLE_BOTTOM_MARGIN_LINES: int = 4
"""Vertical breathing space below the last rendered content, same units
as :data:`ARTICLE_TOP_MARGIN_LINES`. Kept equal to the top breathing
space so scrolling to the end of a note does not slam the final
paragraph into the viewport edge. The desk band beyond it is the
separate :data:`ARTICLE_END_GAP_LINES`, applied identically at the top.
"""

ARTICLE_END_GAP_LINES: float = 1.5
"""Band of *desk* kept above and below the note, in body line heights.

Expressed as a multiple of the body font's line height, like
:data:`ARTICLE_BOTTOM_MARGIN_LINES`. The rendered view paints an opaque
white *sheet* over the content plus the :data:`ARTICLE_TOP_MARGIN_LINES`
/ :data:`ARTICLE_BOTTOM_MARGIN_LINES` breathing space on each side; beyond
that the scroller's own background (the "desk") shows through, with a 1-px
seam at each sheet edge. The same value is reserved at **both** ends so
the gap before the note matches the gap after it — the sheet reads as a
page floating on the desk rather than one butted against the top of the
viewport.

To reserve that room the view's ``top-margin`` and ``bottom-margin`` are
each set to the matching breathing-space margin **plus** this gap; the
painted sheet claims only the breathing part, so the extra band at each
end is desk. At the bottom this band is also the scrollable room that
brings a long note's end (and its seam) into view when scrolled down,
giving a long note the same visible end a short one has; the top is always
reachable, so there the band is purely the matching visual gap.

The value is a typographic choice: below ~1 line the gap reads as a clipped
edge rather than a deliberate margin, above ~2.5 it wastes screen on long
notes. A non-integer value is intentional and allowed — the UI layer rounds
the pixel result and applies the same rounded pixels at both ends.
"""

ARTICLE_INNER_HPADDING_CHARS: int = 8
"""Inner horizontal padding between the article column's edge and the
text, expressed as a multiple of the body font's ``"M"`` glyph width.
Applied as ``left-margin`` / ``right-margin`` on the rendered-view
``Gtk.TextView``. Doubled in the column's outer width calculation so the
text area stays at :data:`TARGET_CHARS_PER_LINE` characters wide.
"""

TABLE_CELL_HPADDING_PX: int = 16
"""Symmetric horizontal padding inside a rendered table cell, in pixels.

The rendered view lays each table row out as native buffer text whose
columns are aligned by a :class:`Pango.TabArray` (one tab stop per
column boundary). Cell *text* is inset from its column boundary by this
amount on **both** sides, so it reads as a padded cell rather than
butting against the column edge.

The padding is applied in two halves that together stay symmetric
without moving any tab stop:

* the **left** inset is the row paragraph tag's ``left-margin`` — because
  the tab stops are measured from the start of the line's text (after the
  paragraph left-margin), one ``left-margin`` shifts every column's text
  right by this amount relative to its boundary in a single stroke (the
  first column, which has no preceding tab, and every later column, which
  starts at a tab stop, inset equally);
* the **right** inset is realised by the renderer reserving ``2 ×`` this
  value as each cell's truncation budget, so a fitted cell ends this far
  short of the next column's boundary. That same reservation keeps the
  cell short of its tab stop (the old per-column gutter's role), so the
  row's column alignment still holds with no separate gutter subtracted.

The cell boundaries, the proportional ``[cols=…]`` math, and the header
tint band / row hairline (all painted full-column) are unchanged — only
the cell *content* moves inward. Consumed by
:mod:`giruntime.ui.note_render.tag_table` (the ``left-margin``) and
:mod:`giruntime.ui.note_render.textbuffer_renderer` (the ``2 ×`` right
reservation). Like the other rendered-view constants it is a typography
decision, not a runtime knob.
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
"""Stable id of the welcome note the v1 migration seeds on first launch.

The note's *source* lives in the ``system_docs`` package
(:data:`enums.SystemDocument.WELCOME`), read gi-free by
:func:`system_docs.load_text`; only this identifier stays here. The
application's initial-selection logic looks the note up by this id and
falls back to the newest note if the user has deleted it.
"""

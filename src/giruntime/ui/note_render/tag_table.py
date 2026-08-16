"""Builds the shared :class:`Gtk.TextTagTable` used by the rendered view.

Principles & invariants
-----------------------
* This module is the single owner of every styling tag that appears in
  the rendered view — its *structure*: geometry, typography, and tag
  identity. Other modules apply tags by *name* (looking them up on the
  table) so the visual definitions live in exactly one place. A tweak to
  "what bold looks like" is one edit here, not a hunt across the
  renderer.
* **Colour is not here.** Every foreground, block tint, hairline rule
  and the note sheet live in
  :mod:`giruntime.ui.note_render.palette` and arrive as a
  :class:`Palette`. The split is along what varies: colour is the one
  thing that changes at runtime (a theme flip re-colours a live tag
  table via :func:`apply_palette`), while every value here is fixed for
  the surface's life by the measured font. :func:`build_tag_table`
  therefore sets no colour itself — it builds the tags and delegates to
  :func:`apply_palette`, so the "build" and "re-theme" paths are one
  path and cannot drift.
* The note's *default* ink is deliberately **not** a tag here. Body text
  and headings carry no foreground, and the colour that saves them from
  inheriting the theme's is applied as CSS by
  :func:`giruntime.ui.note_render.article_text_view._apply_article_ink`
  — see that module for why a whole-buffer tag was the wrong mechanism
  (it re-invalidates the text layout and mis-paints the sheet).
* Tag *names* are exposed as :class:`TagName` enum members. The renderer
  and tests reference :data:`TagName.BOLD` rather than the string
  ``"bold"`` — the style rule against magic strings applies inside this
  package as much as anywhere else.
* The current tag set covers, in addition to the inline subset (bold,
  italic, strikethrough, underline, monospace, link) and the heading
  levels the parser produces, the **per-depth list-item geometry**
  (:data:`TagName.LIST_ITEM_1` … :data:`TagName.LIST_ITEM_3` —
  ``left-margin`` + a ``RIGHT`` tab stop + a ``LEFT`` tab stop + a negative
  ``indent``, so the marker right-aligns, wrapped lines hang under the text,
  and every list at a depth shares one text column regardless of marker
  width; see :func:`_make_list_item_tag`), the **block-level
  paragraph styling** for admonitions, blockquotes, and code blocks, the
  under-title metadata line, and the two lines of the in-place
  unread-source mark
  (:data:`TagName.UNREAD_SOURCE` / :data:`TagName.UNREAD_REASON`).
  Block-level tags carry
  only the *text position* (``accumulative-margin = True`` plus
  ``left-margin`` / ``right-margin`` = one M-width). That M-width is
  the card's *internal padding*: the admonition / blockquote / code
  card spans the full prose column (its wash inset is ``0``, the same
  as a table), and the block's text sits one M-width inside that card
  edge — there is no extra outer indent, so the card lines up with the
  surrounding prose rather than reading as a nested, indented island.
  The matching *tinted wash* is painted by ``ArticleTextView`` using
  :func:`build_wash_specs` to look up tint + inset per tag. This split exists because GTK's
  ``paragraph-background-rgba`` paints exactly between the paragraph's
  effective ``left-margin`` and ``right-margin`` — there is no
  property that decouples "where the wash paints" from "where the
  text starts", so a tinted card that is *wider* than the text (here
  by one M-width on each side) must be painted at snapshot time.
  Tables align the same way: a table fills the prose column and its
  per-cell text inset is an intra-table concern, not an outer indent.
  Those table-row tags additionally carry a
  ``left-margin`` of :data:`config.defaults.TABLE_CELL_HPADDING_PX`
  (``accumulative-margin = True``) that insets each column's cell *text*
  while the wash band / rule still span the full column — see
  :func:`_make_table_row_tag`.
* Admonition paragraph tags come in two roles per kind. The *label*
  paragraph carries the kind name on its own line; the *body* paragraph
  carries the prose. Both paragraphs share the per-kind wash spec so
  the block reads as one rectangle. The *kind character* tag adds the
  bold weight and the accent foreground colour to the kind text itself
  (``NOTE``, ``TIP``, …). Putting the visual properties on separate
  paragraph tags rather than overloading one is what lets a future tweak
  ("more space above admonitions") be a one-line edit.
* All sizing for headings is expressed via ``scale`` (a multiplier on
  the inherited font size) rather than absolute point sizes. This keeps
  the user's font preferences and OS accessibility settings composable —
  a user with a larger base font sees proportionally larger headings
  without any extra wiring.
* :func:`build_tag_table` returns a fresh :class:`Gtk.TextTagTable` on
  every call. Tag tables can only be associated with one
  :class:`Gtk.TextBuffer` at a time in some situations, and a fresh
  instance per buffer avoids accidental cross-buffer aliasing in tests.
  It requires the measured M-width of the body font as
  ``char_width_px`` so the paragraph-tag margins encode "inset + one
  M-width", and a :class:`Palette` for the colours — neither has a
  sensible default, so both parameters are required.
* :func:`build_wash_specs` returns the per-tag :class:`WashSpec`
  records the article TextView paints, tinted from the palette it is
  given. Tag names that don't paint a
  wash (e.g. :data:`TagName.BLOCKQUOTE_ATTRIBUTION`) are absent from
  the returned dict on purpose — the painter must paint nothing
  behind them. The :data:`TagName.METADATA` line and every
  :data:`TagName.TABLE_ROW` are the *hairline* washes: their
  :class:`WashSpec` carries ``shape = WashShape.HAIRLINE`` so the
  painter draws a thin 1-px rule at the bottom of the line (the divider
  between the metadata and the body, or between two table rows) rather
  than a full-height tinted fill. :data:`TagName.TABLE_HEADER` keeps
  the default ``WashShape.FILL`` so the header reads as a tint band.
  :data:`TagName.BLOCKQUOTE_BODY` uses ``WashShape.LEFT_BAR``: a thin
  vertical rule at the box's left edge with no fill, so a quote reads
  as unmistakably distinct from the filled admonition / code cards.
* This module imports ``gi`` because the tag table *is* a GTK object —
  there is no useful pure-Python representation of a tag. Its palette,
  by contrast, is plain data and imports no ``gi`` at all, so every
  colour invariant is testable without a display.
"""

# The module's size reflects the breadth of visual styles it is the sole
# owner of: inline, heading, and every block-level kind (admonitions,
# blockquotes, code blocks, tables, metadata, the error notice, the
# note sheet), each with its own tag builder, wash spec, and tuned
# constants. Splitting solely to satisfy the line counter would scatter
# constants and builders that share the "one style, one place" rule.
# pylint: disable=too-many-lines

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gi.repository import Gtk, Pango

from config.defaults import (
    LIST_MARKER_FIELD_CHARS,
    LIST_MARKER_GAP_CHARS,
    TABLE_CELL_HPADDING_PX,
)
from enums import AdmonitionKind, UnreadMarkPart, WashShape
from giruntime.ui.note_render.palette import Palette, Rgba


class TagName(StrEnum):
    """Names of every shared tag the rendered-view tag table contains.

    Values are the strings the underlying :class:`Gtk.TextTag` carries
    as its ``name`` property — :meth:`Gtk.TextTagTable.lookup` accepts a
    plain :class:`str`, so each enum member is usable directly as the
    lookup key.

    The heading members map to AST levels: :data:`HEADING_0` is the
    document title, and :data:`HEADING_2` … :data:`HEADING_6` are
    section levels. There is no :data:`HEADING_1` member because the
    parser does not produce level-1 headings outside of the document
    title — a mid-document ``=`` heading is rejected as an
    ``UNKNOWN_BLOCK``.

    The list-item members (:data:`LIST_ITEM_1` … :data:`LIST_ITEM_3`)
    map to nesting depth (1-based) and carry that depth's geometry —
    ``left-margin``, a ``RIGHT`` tab stop (marker/period column), a
    ``LEFT`` tab stop (text column), and a negative ``indent`` — so the
    marker right-aligns, wrapped lines hang under the text, and every
    list at a depth shares one text column regardless of marker width.
    Use :func:`list_item_tag_name` to look one up by depth. The depth
    range is :data:`config.defaults.MAX_LIST_DEPTH`, the same cap the
    renderer's bullet-glyph and ordinal-style tables are sized to.

    :data:`MONOSPACE` and :data:`LINK` provide the *visual* styling
    for those constructs. The link's *URL identity* is carried by
    a separate, anonymous (unnamed) :class:`Gtk.TextTag` per link,
    managed by the renderer — only one shared :data:`LINK` tag
    appears in this table because every link looks the same.

    Admonition members come as triples per kind: ``…_LABEL`` is the
    paragraph tag for the kind-label line, ``…_BODY`` is the paragraph
    tag for the body line(s), and ``…_KIND`` is the character tag
    applied to the kind-label text itself (bold + accent foreground).
    Use :func:`admonition_label_tag_name`,
    :func:`admonition_body_tag_name`, and
    :func:`admonition_kind_tag_name` to look these up by
    :class:`AdmonitionKind` rather than embedding string concatenation
    in the renderer.

    :data:`BLOCKQUOTE_BODY` and :data:`BLOCKQUOTE_ATTRIBUTION` are the
    two paragraph tags for blockquote bodies and their optional
    attribution line. The body's italic styling composes via the shared
    :data:`ITALIC` tag, applied by the renderer on top of the
    paragraph tag. The body paints as a ``WashShape.LEFT_BAR`` — a thin
    neutral-grey vertical rule at the box's left edge, no fill — so a
    quote reads as unmistakably distinct from the filled admonition /
    code cards; the attribution carries no wash at all, so the rule
    spans only the quoted body lines, not the citation.

    :data:`CODE_BLOCK` is the paragraph tag carrying the code-block's
    left/right paragraph margins; monospace family comes from the
    shared :data:`MONOSPACE` tag, layered on top by the renderer. It
    carries **zero** inter-line leading (``pixels-above-lines`` /
    ``pixels-below-lines`` / ``pixels-inside-wrap`` all ``0``) so
    consecutive code lines abut at the bare font line height and
    box-drawing glyphs connect into continuous rules. The block's
    vertical breathing room instead comes from two thin edge-only tags,
    :data:`CODE_BLOCK_TOP_PAD` (``pixels-above-lines`` only) and
    :data:`CODE_BLOCK_BOTTOM_PAD` (``pixels-below-lines`` only), which
    the renderer layers on top of :data:`CODE_BLOCK` across only the
    block's first and last logical line respectively — mirroring the
    admonition label/body padding-role split.

    :data:`TABLE_ROW` and :data:`TABLE_HEADER` are the two paragraph tags
    for the rows of a rendered table. Each table row is one logical
    buffer line whose cells are aligned by a per-table
    :class:`Pango.TabArray` (minted anonymously per render by the
    renderer, not carried here). Both tags set ``wrap-mode = NONE`` so a
    row stays on one line and its column alignment holds. The header row
    (``TABLE_HEADER``) paints a tint band behind the line (a
    ``WashShape.FILL`` :class:`WashSpec`) and the renderer makes its
    cell text bold; each data row (``TABLE_ROW``) paints a 1-px rule at
    the line's bottom (a ``WashShape.HAIRLINE`` :class:`WashSpec`, the
    same painter shape the metadata divider uses) to separate it from
    the next row.

    :data:`METADATA` is the character/paragraph tag applied to the
    dim-grey metadata line the rendered view inserts directly under the
    title (``Created … · Modified … · #tag …``). It carries a dim grey
    foreground, a slightly reduced scale, and ``pixels-below-lines`` to
    open a gap between the metadata text and the thin horizontal rule
    that the wash painter draws at the bottom of the line (see the
    ``WashShape.HAIRLINE`` :class:`WashSpec` returned for it by
    :func:`build_wash_specs`). It is a :class:`Gtk.TextTag` name only —
    it is never persisted to disk, so it needs no migration.

    :data:`UNREAD_SOURCE` and :data:`UNREAD_REASON` are the two lines of
    an in-place *unread-source mark*: the source a
    :class:`asciidoc.ast.UnreadBlock` carries, shown verbatim in
    monospace behind a ``WashShape.LEFT_BAR`` amber rule, with the
    kind-specific reason dimmed beneath it. Only a *structural* failure
    is marked — an :data:`enums.UnreadScope.LINE` node renders as an
    ordinary paragraph, wearing neither tag, because unreadable inline
    markup is prose and the reference renders it silently. Both take an
    explicit palette foreground so they read on the opaque sheet
    whichever colour scheme it is in; :data:`UNREAD_REASON` carries no
    wash and so is absent from :func:`build_wash_specs`.
    """

    BOLD = "bold"
    ITALIC = "italic"
    STRIKETHROUGH = "strikethrough"
    UNDERLINE = "underline"
    MONOSPACE = "monospace"
    LINK = "link"
    HEADING_0 = "heading_0"
    HEADING_2 = "heading_2"
    HEADING_3 = "heading_3"
    HEADING_4 = "heading_4"
    HEADING_5 = "heading_5"
    HEADING_6 = "heading_6"

    LIST_ITEM_1 = "list_item_1"
    LIST_ITEM_2 = "list_item_2"
    LIST_ITEM_3 = "list_item_3"
    # Admonition paragraph tags (per kind, role LABEL).
    ADMONITION_NOTE_LABEL = "admonition_note_label"
    ADMONITION_TIP_LABEL = "admonition_tip_label"
    ADMONITION_IMPORTANT_LABEL = "admonition_important_label"
    ADMONITION_WARNING_LABEL = "admonition_warning_label"
    ADMONITION_CAUTION_LABEL = "admonition_caution_label"
    # Admonition paragraph tags (per kind, role BODY).
    ADMONITION_NOTE_BODY = "admonition_note_body"
    ADMONITION_TIP_BODY = "admonition_tip_body"
    ADMONITION_IMPORTANT_BODY = "admonition_important_body"
    ADMONITION_WARNING_BODY = "admonition_warning_body"
    ADMONITION_CAUTION_BODY = "admonition_caution_body"
    # Admonition kind-label character tags (bold + accent foreground).
    ADMONITION_NOTE_KIND = "admonition_note_kind"
    ADMONITION_TIP_KIND = "admonition_tip_kind"
    ADMONITION_IMPORTANT_KIND = "admonition_important_kind"
    ADMONITION_WARNING_KIND = "admonition_warning_kind"
    ADMONITION_CAUTION_KIND = "admonition_caution_kind"
    # Blockquote paragraph tags.
    BLOCKQUOTE_BODY = "blockquote_body"
    BLOCKQUOTE_ATTRIBUTION = "blockquote_attribution"
    # Code-block paragraph tag.
    CODE_BLOCK = "code_block"
    # Code-block edge-padding tags. Carry only the one pixel-padding
    # property they need (pixels-above-lines / pixels-below-lines);
    # layered by the renderer on top of CODE_BLOCK across the block's
    # first and last logical line respectively, mirroring the admonition
    # label/body padding-role split.
    CODE_BLOCK_TOP_PAD = "code_block_top_pad"
    CODE_BLOCK_BOTTOM_PAD = "code_block_bottom_pad"
    # Table row paragraph tags. The header row (rows[0]) carries
    # ``TABLE_HEADER`` (a tint band + bold cell text); every data row
    # carries ``TABLE_ROW`` (a hairline bottom rule). Both also carry the
    # ``wrap-mode = NONE`` that keeps a row on one line so its
    # tab-array column alignment holds.
    TABLE_ROW = "table_row"
    TABLE_HEADER = "table_header"
    # Metadata line under the document title (Created / Modified / tags).
    METADATA = "metadata"
    # Parse-error notice lines, shown in the rendered surface itself when
    # a note's source fails to parse (the buffer is cleared first, so the
    # notice is the only content). Four centred lines: a large warning
    # glyph, a headline, the kind-specific message, and a recovery hint.
    UNREAD_SOURCE = "unread_source"
    UNREAD_REASON = "unread_reason"


@dataclass(frozen=True)
class WashSpec:
    """Wash-painting parameters for one block-level tinted paragraph tag.

    ``tint`` is the RGBA tuple painted behind (or, for :data:`WashShape.
    LEFT_BAR`, alongside) the paragraph. ``box_left_inset_px`` is the
    distance from the textview's widget ``left-margin`` to the box's left
    edge. ``box_right_inset_px`` is the corresponding distance on the
    right. The text lives one M-width inside both edges — that offset is
    encoded in the paragraph tag (added to its ``left-margin`` /
    ``right-margin``), not here, because the painter does not need
    M-width to paint: it only needs the inset.

    ``shape`` selects the painted shape. :data:`WashShape.FILL` (the
    default, used by admonitions and code blocks) fills the full vertical
    extent of the logical line — the tinted "card" behind the block.
    :data:`WashShape.HAIRLINE` (used by the metadata line and table data
    rows) draws a thin 1-px rule at the *bottom* of the line instead of a
    full fill. :data:`WashShape.LEFT_BAR` (used by blockquotes) draws a
    thin vertical rule of width ``bar_width_px`` at the box's *left*
    edge, with no fill — the horizontal extent otherwise computed from
    the two insets is not painted. The horizontal extent for FILL and
    HAIRLINE (driven by the two insets) is computed identically.

    ``bar_width_px`` is the width of the left rule and is only
    meaningful for :data:`WashShape.LEFT_BAR`; it is ``0`` for every
    other shape.
    """

    tint: Rgba
    box_left_inset_px: int
    box_right_inset_px: int
    shape: WashShape = WashShape.FILL
    bar_width_px: int = 0


@dataclass(frozen=True)
class SheetWash:
    """Colour for the note "sheet" — the page the rendered note sits on.

    The rendered note is drawn as a sheet of paper sitting on the
    scroller's background (the "desk"). Because the article text view is
    the vertical scrollport, its own background would otherwise fill the
    whole viewport, hiding the desk below a short note. The text view
    therefore paints its background *itself*: ``tint`` is drawn from the
    top down to the end of the content (the sheet), and below that the
    view is transparent so the **parent's** real background shows through
    — that is the desk, with no separately-invented colour to drift from
    the theme.

    ``tint`` is therefore opaque (it replaces the page background); the
    sheet meets the desk directly, with no rule drawn at the boundary.
    """

    tint: Rgba


# ---------------------------------------------------------------------------
# Scale multipliers for heading levels
# ---------------------------------------------------------------------------
#
# Indexed by AST heading level. The numbers follow a roughly geometric
# progression that matches typical web typography: the document title is
# 2× the body size, h2 is 1.6×, and h6 lands just at body size with bold
# weight to differentiate it from running text. Level 1 is absent on
# purpose (see TagName).

_HEADING_SCALES: dict[int, float] = {
    0: 2.0,
    2: 1.6,
    3: 1.4,
    4: 1.2,
    5: 1.1,
    6: 1.0,
}


# Asymmetric vertical spacing for *body* headings (levels 2-6): the gap
# above a heading is twice the gap below it, so the heading binds
# visually to the content beneath it rather than floating midway between
# two blocks. Applied as the heading paragraph tag's pixel padding so the
# ratio is exact and font-agnostic; the renderer pairs this with
# stripping the preceding block's trailing blank line and trimming the
# heading's own trailing separator to a single newline, so each gap is
# the tag's padding alone (see :func:`_make_heading_tag` and
# ``textbuffer_renderer._emit_section``). The document title (level 0)
# is out of scope — its spacing is governed by the title -> metadata-line
# -> body sequence in ``render_into`` and carries no padding here.
_HEADING_PIXELS_ABOVE_PX: int = 18
_HEADING_PIXELS_BELOW_PX: int = 9


def heading_tag_name(level: int) -> TagName:
    """Return the :class:`TagName` for a given heading level.

    Raises :class:`KeyError` for levels the parser never produces (1, or
    anything outside 0..6) — a misuse from the renderer's side that
    deserves to fail loudly rather than silently fall back to a default.
    """
    return _LEVEL_TO_TAG_NAME[level]


_LEVEL_TO_TAG_NAME: dict[int, TagName] = {
    0: TagName.HEADING_0,
    2: TagName.HEADING_2,
    3: TagName.HEADING_3,
    4: TagName.HEADING_4,
    5: TagName.HEADING_5,
    6: TagName.HEADING_6,
}


# ---------------------------------------------------------------------------
# List-item tag-name lookup (per nesting depth)
# ---------------------------------------------------------------------------
#
# One paragraph tag per 1-based nesting depth, sized to
# :data:`MAX_LIST_DEPTH` so the depth cap and this table cannot drift — a
# tag-table test asserts ``len(_DEPTH_TO_LIST_ITEM_TAG_NAME) ==
# MAX_LIST_DEPTH`` (mirroring the renderer's bullet-glyph / ordinal-style
# tables). Each tag carries only the right-aligned marker + hanging-indent
# geometry for its depth (see :func:`_make_list_item_tag`).
_DEPTH_TO_LIST_ITEM_TAG_NAME: dict[int, TagName] = {
    1: TagName.LIST_ITEM_1,
    2: TagName.LIST_ITEM_2,
    3: TagName.LIST_ITEM_3,
}


def list_item_tag_name(depth: int) -> TagName:
    """Return the :class:`TagName` for a list item at ``depth`` (1-based).

    Raises :class:`KeyError` for a depth the parser never produces (``< 1``
    or ``> MAX_LIST_DEPTH``) — a misuse from the renderer's side that
    deserves to fail loudly rather than silently fall back to a default,
    matching :func:`heading_tag_name`.
    """
    return _DEPTH_TO_LIST_ITEM_TAG_NAME[depth]


# ---------------------------------------------------------------------------
# Admonition tag-name lookups (per kind)
# ---------------------------------------------------------------------------
#
# Three separate per-kind mappings — one for each tag role. Each mapping
# is exhaustive over :class:`AdmonitionKind`; missing a kind would raise
# :class:`KeyError`, which is the right loud failure for a misuse from
# the renderer's side. The unit tests iterate :class:`AdmonitionKind`
# and assert every kind resolves in every mapping, so adding a new kind
# without extending these tables fails the test rather than producing a
# silently-unstyled admonition.


def admonition_label_tag_name(kind: AdmonitionKind) -> TagName:
    """Return the paragraph-tag name for an admonition's *label* line."""
    return _ADMONITION_LABEL_TAG_NAMES[kind]


def admonition_body_tag_name(kind: AdmonitionKind) -> TagName:
    """Return the paragraph-tag name for an admonition's *body* paragraph."""
    return _ADMONITION_BODY_TAG_NAMES[kind]


def admonition_kind_tag_name(kind: AdmonitionKind) -> TagName:
    """Return the character-tag name for an admonition's kind label text."""
    return _ADMONITION_KIND_TAG_NAMES[kind]


_ADMONITION_LABEL_TAG_NAMES: dict[AdmonitionKind, TagName] = {
    AdmonitionKind.NOTE: TagName.ADMONITION_NOTE_LABEL,
    AdmonitionKind.TIP: TagName.ADMONITION_TIP_LABEL,
    AdmonitionKind.IMPORTANT: TagName.ADMONITION_IMPORTANT_LABEL,
    AdmonitionKind.WARNING: TagName.ADMONITION_WARNING_LABEL,
    AdmonitionKind.CAUTION: TagName.ADMONITION_CAUTION_LABEL,
}

_ADMONITION_BODY_TAG_NAMES: dict[AdmonitionKind, TagName] = {
    AdmonitionKind.NOTE: TagName.ADMONITION_NOTE_BODY,
    AdmonitionKind.TIP: TagName.ADMONITION_TIP_BODY,
    AdmonitionKind.IMPORTANT: TagName.ADMONITION_IMPORTANT_BODY,
    AdmonitionKind.WARNING: TagName.ADMONITION_WARNING_BODY,
    AdmonitionKind.CAUTION: TagName.ADMONITION_CAUTION_BODY,
}

_ADMONITION_KIND_TAG_NAMES: dict[AdmonitionKind, TagName] = {
    AdmonitionKind.NOTE: TagName.ADMONITION_NOTE_KIND,
    AdmonitionKind.TIP: TagName.ADMONITION_TIP_KIND,
    AdmonitionKind.IMPORTANT: TagName.ADMONITION_IMPORTANT_KIND,
    AdmonitionKind.WARNING: TagName.ADMONITION_WARNING_KIND,
    AdmonitionKind.CAUTION: TagName.ADMONITION_CAUTION_KIND,
}


# ---------------------------------------------------------------------------
# Visual constants for monospace and block-level *structure*
# ---------------------------------------------------------------------------
#
# What remains here is geometry and typography: the values that depend on
# the measured font, not on the colour scheme. Every *colour* the
# rendered view paints — foregrounds, block tints, hairline rules, the
# sheet — lives in :mod:`giruntime.ui.note_render.palette` and reaches
# these builders as a :class:`Palette`, because colour is the one thing
# that varies at runtime (see :func:`apply_palette`).
#
# These are not exposed as enum values because they describe *visual*
# settings rather than categorical concepts — there's no closed set of
# legal monospace families or paddings, only one current choice each.

_MONOSPACE_FAMILY: str = "monospace"


# Paragraph metrics applied to admonition paragraph tags. ``HMARGIN``
# is the *box inset* from the textview's widget left/right margin to
# the tinted box's edge. It is ``0`` so the card spans the full prose
# column (the same as a table) rather than sitting indented from it.
# The text inside the box still sits one M-width inside the box on each
# side; that offset is added by the paragraph tag builder, not stored
# here. To re-introduce an outer indent, raise this above ``0`` — the
# paragraph margin and the wash inset both read it, so card and text
# move together.
#
# ``VPADDING`` is the card's *outer* breathing room: above the label and
# below the body. ``INNER_GAP`` is the gap *between* them, and is a
# separate constant because it plays the opposite role — the outer pair
# insets the card's contents from its tinted edge, while the inner one
# separates a label from the text it labels. It must be non-zero (with
# ``0`` the kind word sits flush on the first body line) and smaller
# than ``VPADDING``, so the label and its body read as one unit inside
# the card rather than as two stacked lines. Same pairing as the
# document title and its metadata line; ``test_tag_table.py`` asserts
# the ordering for both.
_ADMONITION_HMARGIN_PX: int = 0
_ADMONITION_VPADDING_PX: int = 8
_ADMONITION_INNER_GAP_PX: int = 6
_ADMONITION_LINE_GAP_PX: int = 2

# Paragraph metrics for blockquotes. The box insets are ``0`` so the
# quote's left rule aligns with the full prose column like a table; the
# same split as admonitions applies — text sits one M-width inside the
# box edge on each side, clearing the rule painted at the box's left
# edge. (Historically the left inset gave a quote a visible indent; the
# aligned-card design drops that outer indent.) ``_BLOCKQUOTE_BAR_WIDTH_PX``
# is the width of the left rule the wash painter draws — see
# :data:`WashSpec.bar_width_px`.
_BLOCKQUOTE_HMARGIN_PX: int = 0
_BLOCKQUOTE_RIGHT_MARGIN_PX: int = 0
_BLOCKQUOTE_VPADDING_PX: int = 6
_BLOCKQUOTE_LINE_GAP_PX: int = 2
_BLOCKQUOTE_BAR_WIDTH_PX: int = 3

# Paragraph metrics for code blocks. The box insets are ``0`` so the
# code card spans the full prose column; the monospace text still sits
# one M-width inside the card edge on each side. There is **no**
# inter-line leading (``pixels-above-lines`` / ``pixels-below-lines`` /
# ``pixels-inside-wrap`` are all ``0`` on the ``CODE_BLOCK`` tag itself —
# see :func:`_make_code_block_tag`) so consecutive code lines abut at
# the bare font line height and box-drawing glyphs connect into
# continuous rules. ``_CODE_BLOCK_EDGE_PADDING_PX`` is instead applied
# only at the block's top and bottom edge, via the
# :data:`TagName.CODE_BLOCK_TOP_PAD` / :data:`TagName.CODE_BLOCK_BOTTOM_PAD`
# tags the renderer layers across the block's first and last logical
# line respectively.
_CODE_BLOCK_HMARGIN_PX: int = 0
_CODE_BLOCK_EDGE_PADDING_PX: int = 8

# Scale multiplier for the blockquote attribution line. Slightly
# smaller than body text so the citation reads as secondary metadata.
_BLOCKQUOTE_ATTRIBUTION_SCALE: float = 0.9


# Table rows. A rendered table is native buffer text: each row is one
# logical line whose cells are aligned by a per-table ``Pango.TabArray``
# minted by the renderer. Two paragraph tags carry the row treatment.
# The *header* row paints a neutral tint band (a fill wash) behind the
# line; each *data* row paints a 1-px rule at its bottom (a hairline
# wash — the same painter shape the metadata divider uses) to separate
# it from the next row. Two distinct insets apply here and must not be
# confused. The **wash** inset (this box-inset constant) is zero so the
# band / rule span the *full* body text column (the table itself fills
# that column). The **text** inset is separate: the row tags carry
# ``TABLE_CELL_HPADDING_PX`` as a ``left-margin`` so each column's cell
# *text* sits that far inside its column boundary, while the band / rule
# behind it still reach both column edges. The vertical padding is
# applied *symmetrically* (``pixels-above-lines`` == ``pixels-below-lines``)
# on both row kinds, so a data row's hairline rule sits clear of the text
# on both sides — the gap below row N and above row N+1 each contribute,
# and the rule lands centred between them — and the header's text is
# centred within its tint band rather than hugging the top edge.
_TABLE_BOX_INSET_PX: int = 0
_TABLE_ROW_VPADDING_PX: int = 7
_TABLE_HEADER_VPADDING_PX: int = 8


# Metadata line (Created / Modified / tags) under the document title.
# A slightly reduced scale so it reads as secondary to the title and
# body, and a gap below the text that separates it from the hairline
# rule the wash painter draws. Both the text colour and the rule colour
# come from the palette.
#
# The gap *above* the metadata line is what binds it to the title. It
# must be non-zero: with no padding the gap is the font's own leading
# alone, which a descender in the title ("git", "python", "grep")
# consumes entirely, so the two lines collide. It must also stay
# *smaller* than ``_METADATA_PIXELS_BELOW_LINES_PX`` -- the two gaps
# bracket the metadata line, and if the one above is not the tighter of
# the pair the line floats midway between the title and the rule and
# reads as belonging to neither. That ordering, not the literal value,
# is what ``test_tag_table.py`` asserts.
#
# The value is roughly a fifth of the title's size (body x
# ``_HEADING_SCALES[0]``). Padding is geometric while a collision is
# optical, so it is tuned against a title *with* descenders -- the worst
# case; an ascender-only title sits a hair loose, which is invisible in
# practice where the collision is not.
_METADATA_SCALE: float = 0.85
_METADATA_PIXELS_ABOVE_LINES_PX: int = 6
_METADATA_PIXELS_BELOW_LINES_PX: int = 8
_METADATA_RULE_INSET_PX: int = 0


# Parse-error notice (the "empty state" shown in the rendered surface
# when a note's source fails to parse). Four centred lines: a large
# warning glyph, a headline, the kind-specific message, and a faint
# recovery hint. Scales are multipliers on the body size so the notice
# tracks the user's font; the pixel gaps set the rhythm between the four
# lines. Their foregrounds are palette-owned and always explicit — the
# notice sits on the sheet, whose colour the application chooses, so an
# inherited theme foreground could land invisibly on it (which is
# precisely what used to happen to body text under a dark theme).
_UNREAD_TAG_NAMES: dict[UnreadMarkPart, TagName] = {
    UnreadMarkPart.SOURCE: TagName.UNREAD_SOURCE,
    UnreadMarkPart.REASON: TagName.UNREAD_REASON,
}


# Paragraph metrics for the in-place unread-source mark. The box insets
# are ``0`` so the amber rule aligns with the prose column exactly as the
# blockquote's does; the source text sits one M-width inside the box edge,
# clearing the rule. ``_UNREAD_BAR_WIDTH_PX`` matches the blockquote bar,
# so the two left-ruled blocks read as one family.
_UNREAD_HMARGIN_PX: int = 0
_UNREAD_BAR_WIDTH_PX: int = 3
_UNREAD_SOURCE_VPADDING_PX: int = 4
_UNREAD_REASON_SCALE: float = 0.9
# The reason must read as annotation *about* the source above it, not as
# one more line of it. At the source's line spacing a 2px gap put it
# closer to the last source line than those lines are to each other,
# which grouped it with the quarantined text; 5px separates the two
# without detaching the reason from what it explains.
_UNREAD_REASON_PIXELS_ABOVE_PX: int = 5
_UNREAD_REASON_PIXELS_BELOW_PX: int = 6


def build_tag_table(
    *, char_width_px: int, palette: Palette,
) -> Gtk.TextTagTable:
    """Construct the rendered-view tag table for the current subset.

    ``char_width_px`` is the measured M-width of the body font in
    pixels. It is required (no default) because there is no sensible
    default — a wrong default would silently mis-size the inner inset
    on every block-level paragraph tag. Tests pass an explicit small
    int (e.g. ``9``); production passes the result of
    :meth:`ui.article_container.ArticleContainer.char_width_px`.

    ``palette`` supplies every colour. It is required for the same
    reason: defaulting to the light set would make "which sheet is this
    table for?" invisible at the call site.

    The returned table contains exactly one tag per :class:`TagName`
    member. Tag names are unique within a table, so callers that need
    a tag by name use :meth:`Gtk.TextTagTable.lookup` with the
    corresponding :class:`TagName` value.

    **This function sets no colour itself.** It builds the structural
    tags and then hands the table to :func:`apply_palette`, which is the
    single place any foreground is written. That is what keeps the
    "build" and "re-theme" paths from drifting: there is only one path.

    Note that link *identity* (which URL each link points at) is
    carried by a separate, anonymous :class:`Gtk.TextTag` per link,
    added to the table at render time and tracked by the renderer.
    The shared :data:`TagName.LINK` tag in this table only contributes
    the visual appearance — colour and underline — that every link
    shares.
    """
    table = Gtk.TextTagTable.new()
    table.add(_make_inline_tag(TagName.BOLD, weight=Pango.Weight.BOLD))
    table.add(_make_inline_tag(TagName.ITALIC, style=Pango.Style.ITALIC))
    table.add(_make_inline_tag(TagName.STRIKETHROUGH, strikethrough=True))
    table.add(_make_inline_tag(TagName.UNDERLINE, underline=Pango.Underline.SINGLE))
    table.add(_make_inline_tag(TagName.MONOSPACE, family=_MONOSPACE_FAMILY))
    table.add(
        _make_inline_tag(TagName.LINK, underline=Pango.Underline.SINGLE)
    )
    for level, scale in _HEADING_SCALES.items():
        table.add(
            _make_heading_tag(
                _LEVEL_TO_TAG_NAME[level], scale=scale, is_body=level != 0,
            )
        )
    for depth, list_item_name in _DEPTH_TO_LIST_ITEM_TAG_NAME.items():
        table.add(
            _make_list_item_tag(
                list_item_name, depth=depth, char_width_px=char_width_px,
            )
        )
    for kind in AdmonitionKind:
        table.add(
            _make_admonition_paragraph_tag(
                _ADMONITION_LABEL_TAG_NAMES[kind],
                is_label=True,
                char_width_px=char_width_px,
            )
        )
        table.add(
            _make_admonition_paragraph_tag(
                _ADMONITION_BODY_TAG_NAMES[kind],
                is_label=False,
                char_width_px=char_width_px,
            )
        )
        table.add(
            _make_inline_tag(
                _ADMONITION_KIND_TAG_NAMES[kind],
                weight=Pango.Weight.BOLD,
            )
        )
    table.add(
        _make_blockquote_body_tag(
            TagName.BLOCKQUOTE_BODY, char_width_px=char_width_px,
        )
    )
    table.add(
        _make_blockquote_attribution_tag(
            TagName.BLOCKQUOTE_ATTRIBUTION, char_width_px=char_width_px,
        )
    )
    table.add(
        _make_code_block_tag(TagName.CODE_BLOCK, char_width_px=char_width_px)
    )
    # Added after CODE_BLOCK so they take priority for the one pixel
    # property each carries — GTK resolves a paragraph property from the
    # highest-priority tag on the line that sets it, and later table
    # insertion order is higher priority.
    table.add(_make_code_block_pad_tag(TagName.CODE_BLOCK_TOP_PAD, is_top=True))
    table.add(
        _make_code_block_pad_tag(TagName.CODE_BLOCK_BOTTOM_PAD, is_top=False)
    )
    table.add(_make_table_row_tag(TagName.TABLE_ROW, is_header=False))
    table.add(_make_table_row_tag(TagName.TABLE_HEADER, is_header=True))
    table.add(_make_metadata_tag(TagName.METADATA))
    table.add(
        _make_unread_source_tag(
            TagName.UNREAD_SOURCE, char_width_px=char_width_px,
        )
    )
    table.add(_make_unread_reason_tag(TagName.UNREAD_REASON))
    apply_palette(table, palette)
    return table


def apply_palette(table: Gtk.TextTagTable, palette: Palette) -> None:
    """Write every colour in ``palette`` onto ``table``'s tags, in place.

    The single writer of every foreground the rendered view shows.
    :func:`build_tag_table` calls it last, and
    :meth:`ArticleTextView.do_css_changed` calls it again whenever the
    theme flips — so a re-theme takes the identical code path a fresh
    build does, and the two cannot drift apart.

    Re-colouring in place is not an optimisation but a requirement: a
    :class:`Gtk.TextBuffer` is bound to its tag table for life (the
    renderer raises on a mismatch), so a theme change cannot swap in a
    freshly built table without also rebuilding the buffer — that is, a
    full re-parse and re-render of the note. Mutating the existing tags
    restyles the live buffer instead: GTK repaints the ranges they cover,
    the buffer's text is untouched, and the reader keeps their scroll
    position.

    Only foregrounds live on the tags. The block tints, hairline rules
    and the sheet are painted by ``ArticleTextView`` from
    :func:`build_wash_specs` / :func:`build_sheet_wash`, which take the
    same palette, so the caller re-installs those alongside this call.

    Raises :class:`LookupError` if ``table`` is missing a tag the palette
    addresses — i.e. if it was not built by :func:`build_tag_table`.
    That is a wiring bug and deserves to fail loudly.
    """
    _set_foreground(table, TagName.LINK, palette.link_foreground)
    _set_foreground(table, TagName.METADATA, palette.metadata_foreground)
    for kind in AdmonitionKind:
        _set_foreground(
            table,
            _ADMONITION_KIND_TAG_NAMES[kind],
            palette.admonition_kind_foregrounds[kind],
        )
    for part in UnreadMarkPart:
        _set_foreground(
            table,
            _UNREAD_TAG_NAMES[part],
            palette.unread_foregrounds[part],
        )


def _set_foreground(
    table: Gtk.TextTagTable, name: TagName, foreground: str,
) -> None:
    """Set one tag's ``foreground``, looked up by name on ``table``."""
    tag = table.lookup(name.value)
    if tag is None:
        raise LookupError(f"tag {name.value!r} missing from tag table")
    tag.set_property("foreground", foreground)


def build_wash_specs(palette: Palette) -> dict[TagName, WashSpec]:
    """Return the per-tag wash spec the article TextView paints.

    Keys are :class:`TagName` values for every paragraph tag that
    carries a wash. Tag names that *don't* paint a wash (e.g.
    :data:`TagName.BLOCKQUOTE_ATTRIBUTION`) are absent on purpose —
    the painter must paint nothing behind them.

    The tints come from ``palette``; the box insets are structural and
    live here. A theme change therefore rebuilds this map and re-installs
    it on the view, which is why the map is returned fresh rather than
    cached.

    The admonition label and body for the same kind share an
    *identical* :class:`WashSpec` instance by design so they read as
    one rectangle: the painter walks logical lines independently, but
    the two paragraphs end up painted with the same colour at the
    same horizontal extents, so the two rectangles butt edge-to-edge
    and the user sees one block.
    """
    specs: dict[TagName, WashSpec] = {}
    for kind in AdmonitionKind:
        spec = WashSpec(
            tint=palette.admonition_tints[kind],
            box_left_inset_px=_ADMONITION_HMARGIN_PX,
            box_right_inset_px=_ADMONITION_HMARGIN_PX,
        )
        specs[_ADMONITION_LABEL_TAG_NAMES[kind]] = spec
        specs[_ADMONITION_BODY_TAG_NAMES[kind]] = spec
    specs[TagName.BLOCKQUOTE_BODY] = WashSpec(
        tint=palette.blockquote_bar_tint,
        box_left_inset_px=_BLOCKQUOTE_HMARGIN_PX,
        box_right_inset_px=_BLOCKQUOTE_RIGHT_MARGIN_PX,
        shape=WashShape.LEFT_BAR,
        bar_width_px=_BLOCKQUOTE_BAR_WIDTH_PX,
    )
    # The unread-source mark: an amber left rule, no fill -- the same
    # painter shape the blockquote uses, so a left-ruled block reads as
    # one visual family whatever put it there.
    specs[TagName.UNREAD_SOURCE] = WashSpec(
        tint=palette.unread_bar_tint,
        box_left_inset_px=_UNREAD_HMARGIN_PX,
        box_right_inset_px=_UNREAD_HMARGIN_PX,
        shape=WashShape.LEFT_BAR,
        bar_width_px=_UNREAD_BAR_WIDTH_PX,
    )
    specs[TagName.CODE_BLOCK] = WashSpec(
        tint=palette.code_block_tint,
        box_left_inset_px=_CODE_BLOCK_HMARGIN_PX,
        box_right_inset_px=_CODE_BLOCK_HMARGIN_PX,
    )
    # Table header: a tint band (full fill) spanning the body column.
    specs[TagName.TABLE_HEADER] = WashSpec(
        tint=palette.table_header_tint,
        box_left_inset_px=_TABLE_BOX_INSET_PX,
        box_right_inset_px=_TABLE_BOX_INSET_PX,
    )
    # Table data row: a 1-px rule at the line's bottom (hairline), the
    # same painter shape the metadata divider uses.
    specs[TagName.TABLE_ROW] = WashSpec(
        tint=palette.table_rule_tint,
        box_left_inset_px=_TABLE_BOX_INSET_PX,
        box_right_inset_px=_TABLE_BOX_INSET_PX,
        shape=WashShape.HAIRLINE,
    )
    specs[TagName.METADATA] = WashSpec(
        tint=palette.metadata_rule_tint,
        box_left_inset_px=_METADATA_RULE_INSET_PX,
        box_right_inset_px=_METADATA_RULE_INSET_PX,
        shape=WashShape.HAIRLINE,
    )
    return specs


def build_sheet_wash(palette: Palette) -> SheetWash:
    """Return the note sheet colour for ``palette``.

    The sheet is painted by the article text view behind the content
    (the view's CSS background is transparent so the desk shows below).
    It is opaque in every palette: it stands in for the page background,
    and the palette's foregrounds are tuned against it.
    """
    return SheetWash(tint=palette.sheet)


def _make_inline_tag(  # pylint: disable=too-many-arguments
    name: TagName,
    *,
    weight: Pango.Weight | None = None,
    style: Pango.Style | None = None,
    strikethrough: bool | None = None,
    underline: Pango.Underline | None = None,
    family: str | None = None,
) -> Gtk.TextTag:
    """Build a single inline-style tag with the requested visual rule.

    Only the property the caller passes is set; the rest are left at
    their inherited defaults so multiple tags on the same range
    compose without one tag erasing another's contribution. This is
    why ``LINK`` (colour + underline) and ``UNDERLINE`` (just
    underline) coexist cleanly when both apply to the same range.

    There is no ``foreground`` parameter: colour is
    :func:`apply_palette`'s alone to write, so the tags this builds
    (``LINK`` and the admonition kind labels among them) get their
    structure here and their colour there.

    The argument list grows one element each time we add an inline
    construct, which is unavoidable: each is a distinct
    :class:`Gtk.TextTag` property. Refactoring to a single ``props``
    mapping would lose the type-checked keyword surface — and there
    are only five properties total in the closed AsciiDoc subset, so
    the explicit list stays readable.
    """
    tag = Gtk.TextTag.new(name.value)
    if weight is not None:
        tag.set_property("weight", weight)
    if style is not None:
        tag.set_property("style", style)
    if strikethrough is not None:
        tag.set_property("strikethrough", strikethrough)
    if underline is not None:
        tag.set_property("underline", underline)
    if family is not None:
        tag.set_property("family", family)
    return tag


def _make_heading_tag(name: TagName, *, scale: float, is_body: bool) -> Gtk.TextTag:
    """Build a heading-style tag: bold weight at the given scale.

    ``is_body`` selects the asymmetric vertical spacing (2 : 1 above :
    below) that applies to body section headings (levels 2-6): when
    ``True`` the tag also carries ``pixels-above-lines`` /
    ``pixels-below-lines`` from :data:`_HEADING_PIXELS_ABOVE_PX` /
    :data:`_HEADING_PIXELS_BELOW_PX`, driving both gaps directly so the
    ratio is exact and independent of the surrounding block separators
    (see ``textbuffer_renderer._emit_section``, which strips the
    preceding blank line and trims the heading's own trailing separator
    to a single newline so nothing else contributes to either gap). The
    document title (level 0) passes ``False`` — its spacing is governed
    by the title -> metadata-line -> body sequence in ``render_into``.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("weight", Pango.Weight.BOLD)
    tag.set_property("scale", scale)
    if is_body:
        tag.set_property("pixels-above-lines", _HEADING_PIXELS_ABOVE_PX)
        tag.set_property("pixels-below-lines", _HEADING_PIXELS_BELOW_PX)
    return tag


def _make_list_item_tag(name: TagName, *, depth: int, char_width_px: int) -> Gtk.TextTag:
    """Build the right-aligned, hanging-indent paragraph tag for ``depth``.

    ``FIELD = LIST_MARKER_FIELD_CHARS * char_width_px`` is the marker field
    width, ``GAP = round(LIST_MARKER_GAP_CHARS * char_width_px)`` the
    marker-to-text gap, and ``STEP = FIELD + GAP`` the per-depth nesting step
    (see :data:`config.defaults.LIST_MARKER_FIELD_CHARS`). The renderer emits
    each item as ``\\t{marker}\\t{text}``, and this tag lays that line out
    (verified against GTK's / Pango's own tab and indent semantics):

    * ``left-margin = (depth - 1) * STEP`` positions this depth's marker
      field. With ``accumulative-margin = True`` it stacks on the widget's
      inner padding (like the block tags) rather than replacing it, and —
      because tab stops are measured from the line's text start *after* the
      left-margin (the convention the table rows rely on) — the two stops
      below are expressed relative to it.
    * a **RIGHT** tab stop at ``FIELD``: the leading tab right-aligns the
      marker so its trailing ``.`` lands on the ``FIELD`` column. Periods
      therefore align within a list, and because ``FIELD`` is fixed per depth
      the column is shared by every list at this depth.
    * a **LEFT** tab stop at ``FIELD + GAP``: the second tab drops the item
      text on the text column — identical for every list at this depth, so
      sibling sub-lists whose markers differ in width still align their text.
    * ``indent = -(FIELD + GAP)`` is a *hanging* indent: Pango insets every
      wrapped continuation line by one step, so wrapped prose hangs under the
      text column, not back at the marker.

    A marker wider than ``FIELD`` (only a pathologically long ordinal) still
    right-aligns, extending further left into the indent; that overflow is
    accepted, not designed around
    (see :data:`config.defaults.LIST_MARKER_FIELD_CHARS`).
    """
    field = LIST_MARKER_FIELD_CHARS * char_width_px
    gap = round(LIST_MARKER_GAP_CHARS * char_width_px)
    step = field + gap
    tabs = Pango.TabArray.new(2, True)
    tabs.set_tab(0, Pango.TabAlign.RIGHT, field)
    tabs.set_tab(1, Pango.TabAlign.LEFT, step)
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", (depth - 1) * step)
    tag.set_property("indent", -step)
    tag.set_property("tabs", tabs)
    return tag


def _make_admonition_paragraph_tag(
    name: TagName,
    *,
    is_label: bool,
    char_width_px: int,
) -> Gtk.TextTag:
    """Build an admonition paragraph tag (label or body role).

    The tag carries the *text position* only. Its ``left-margin`` and
    ``right-margin`` are set to ``_ADMONITION_HMARGIN_PX + char_width_px``
    — with the box inset now ``0`` that is just one M-width, so the
    tinted card spans the full prose column and the text sits one
    M-width inside the card's edge (the card's internal padding) rather
    than the whole block reading as indented from the prose.
    ``accumulative-margin = True`` makes those values *stack* on the
    textview's widget-level ``left-margin`` / ``right-margin`` instead
    of replacing them — without this flag a paragraph tag overrides
    the widget's margins and the text escapes the inner column. The
    matching tinted wash is painted separately by ``ArticleTextView``
    in :mod:`ui.note_view` (see :func:`build_wash_specs`).

    ``is_label`` selects which of the two roles the tag plays, and both
    of its vertical gaps follow from that. The card's *outer* padding
    (:data:`_ADMONITION_VPADDING_PX`) sits above the label and below the
    body, giving the block an even top and bottom margin. The gap
    *between* them (:data:`_ADMONITION_INNER_GAP_PX`) is contributed
    once, by the label's ``pixels-below-lines`` — the body's
    ``pixels-above-lines`` stays ``0`` so the two do not add up, since
    ``pixels-above-lines`` applies to every logical line of a multi-line
    body and putting the gap there would also space the body's own lines
    apart. Side margins and the in-wrap line spacing are shared.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _ADMONITION_HMARGIN_PX + char_width_px)
    tag.set_property("right-margin", _ADMONITION_HMARGIN_PX + char_width_px)
    tag.set_property(
        "pixels-above-lines",
        _ADMONITION_VPADDING_PX if is_label else 0,
    )
    tag.set_property(
        "pixels-below-lines",
        _ADMONITION_INNER_GAP_PX if is_label else _ADMONITION_VPADDING_PX,
    )
    tag.set_property("pixels-inside-wrap", _ADMONITION_LINE_GAP_PX)
    return tag


def _make_blockquote_body_tag(
    name: TagName, *, char_width_px: int,
) -> Gtk.TextTag:
    """Build the blockquote-body paragraph tag.

    Carries the text position only: with the box inset now ``0`` the
    left/right margins are just one M-width, so the left rule aligns
    with the full prose column and the text sits one M-width inside
    that edge, clearing the rule the wash painter draws there — the
    quote no longer carries an outer indent from the prose.
    ``accumulative-margin = True`` makes those margins stack on the
    textview's widget-level margins (see
    :func:`_make_admonition_paragraph_tag` for why this matters). The
    italic style is *not* set here — the renderer composes it by
    layering the shared :data:`TagName.ITALIC` tag across the body
    range so a future tweak to "what italic looks like" remains a
    one-line edit. The rule itself is painted separately by
    ``ArticleTextView`` in :mod:`ui.note_view`.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _BLOCKQUOTE_HMARGIN_PX + char_width_px)
    tag.set_property(
        "right-margin", _BLOCKQUOTE_RIGHT_MARGIN_PX + char_width_px,
    )
    tag.set_property("pixels-above-lines", _BLOCKQUOTE_VPADDING_PX)
    tag.set_property("pixels-below-lines", _BLOCKQUOTE_VPADDING_PX)
    tag.set_property("pixels-inside-wrap", _BLOCKQUOTE_LINE_GAP_PX)
    return tag


def _make_blockquote_attribution_tag(
    name: TagName, *, char_width_px: int,
) -> Gtk.TextTag:
    """Build the blockquote-attribution paragraph tag.

    Shares the body's left-margin so the attribution sits flush with
    the quote body's *text* (one M-width inside the tinted card's
    edge, which now spans the prose column), applies a smaller scale,
    and right-aligns the text so a typical ``— Author, Source`` line
    reads as a citation under the quote. There is no tint to remove
    (the attribution never carried one). ``accumulative-margin = True``
    is set for the same reason as the body tag — without it, the
    attribution paragraph would escape the inner column.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _BLOCKQUOTE_HMARGIN_PX + char_width_px)
    tag.set_property(
        "right-margin", _BLOCKQUOTE_RIGHT_MARGIN_PX + char_width_px,
    )
    tag.set_property("scale", _BLOCKQUOTE_ATTRIBUTION_SCALE)
    tag.set_property("justification", Gtk.Justification.RIGHT)
    return tag


def _make_code_block_tag(name: TagName, *, char_width_px: int) -> Gtk.TextTag:
    """Build the code-block paragraph tag.

    Carries the text position only: with the box inset now ``0`` the
    left/right margins are just one M-width, so the tinted card spans
    the full prose column and the monospace text sits one M-width
    inside the card edge (the card's internal padding).
    ``accumulative-margin = True`` makes those margins stack on the
    textview's widget-level margins. The monospace family comes from
    the shared :data:`TagName.MONOSPACE` tag, which the renderer
    applies on top of this one across the same range. The tint is
    painted separately by ``ArticleTextView`` in :mod:`ui.note_view`.

    ``pixels-above-lines`` / ``pixels-below-lines`` / ``pixels-inside-wrap``
    are all ``0`` — zero inter-line leading — so consecutive code lines
    abut at the bare font line height and box-drawing characters
    (``│ ├ └ ─``) in adjacent lines connect into continuous rules. The
    block's vertical breathing room comes instead from
    :func:`_make_code_block_pad_tag`, layered by the renderer on the
    block's first and last logical line only.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _CODE_BLOCK_HMARGIN_PX + char_width_px)
    tag.set_property("right-margin", _CODE_BLOCK_HMARGIN_PX + char_width_px)
    tag.set_property("pixels-above-lines", 0)
    tag.set_property("pixels-below-lines", 0)
    tag.set_property("pixels-inside-wrap", 0)
    return tag


def _make_code_block_pad_tag(name: TagName, *, is_top: bool) -> Gtk.TextTag:
    """Build a code-block edge-padding tag (top or bottom role).

    Carries only the one pixel-padding property its role needs —
    ``pixels-above-lines`` for the top-pad tag, ``pixels-below-lines``
    for the bottom-pad tag — from :data:`_CODE_BLOCK_EDGE_PADDING_PX`.
    The renderer layers this on top of :data:`TagName.CODE_BLOCK` across
    only the block's first (top role) or last (bottom role) logical
    line, mirroring the admonition label/body padding-role split. Not
    setting the other pixel properties leaves them to fall through to
    :data:`TagName.CODE_BLOCK`'s zero inter-line leading.
    """
    tag = Gtk.TextTag.new(name.value)
    if is_top:
        tag.set_property("pixels-above-lines", _CODE_BLOCK_EDGE_PADDING_PX)
    else:
        tag.set_property("pixels-below-lines", _CODE_BLOCK_EDGE_PADDING_PX)
    return tag


def _make_table_row_tag(name: TagName, *, is_header: bool) -> Gtk.TextTag:
    """Build a table-row paragraph tag (header or data role).

    A rendered table is native buffer text: every row is one logical
    line whose cells are aligned by a per-table :class:`Pango.TabArray`
    that the renderer mints anonymously and applies on top of this tag.
    This tag carries the *row-level* paragraph properties that are the
    same for every table:

    * ``wrap-mode = NONE`` so the row stays on a single line — wrapping
      would break the tab-array column alignment, so it is disabled here
      (overriding the view-level ``WORD_CHAR``). The renderer guarantees
      a row never exceeds the column by truncating each cell to its
      column width less ``2 × TABLE_CELL_HPADDING_PX`` (see below).
    * ``pixels-above-lines`` / ``pixels-below-lines`` open *symmetric*
      breathing room above and below the row, so a data row's hairline
      rule sits clear of the text on both sides (the rule lands centred
      in the gap between two rows) and the header text is centred within
      its tint band.

    The tag insets the cell *text* by
    :data:`config.defaults.TABLE_CELL_HPADDING_PX` on the left via
    ``left-margin``, with ``accumulative-margin = True`` so that inset
    *stacks* on the textview's widget-level ``left-margin`` rather than
    replacing it — without the flag a non-accumulative paragraph margin
    overrides the widget margin and the text escapes the body column to
    the left (the same reason the admonition / blockquote / code tags
    stack; verified empirically against this GTK build). Because the
    per-table :class:`Pango.TabArray` stops are measured from the start
    of the line's text — i.e. *after* this ``left-margin`` — the single
    ``left-margin`` shifts every column's text right by the padding
    relative to its boundary in one stroke (the first column, with no
    preceding tab, and every later column, which starts at a tab stop,
    inset equally). ``right-margin`` is left unset: the matching right
    padding is realised by the renderer reserving
    ``2 × TABLE_CELL_HPADDING_PX`` as each cell's truncation budget (see
    :func:`giruntime.ui.note_render.textbuffer_renderer._truncate_cell`),
    so left and right cell padding stay equal and a fitted cell still
    stops short of its tab stop.

    The tab stops themselves are **not** moved, so a table still fills
    the body text column and the wash band (header) or 1-px rule (data
    row) still spans the *full* column — only the cell *text* moves
    inward. The band / rule is painted separately by ``ArticleTextView``
    in :mod:`ui.note_view` via the :class:`WashSpec`
    :func:`build_wash_specs` returns for this tag (a *fill* for
    :data:`TagName.TABLE_HEADER`, a ``hairline`` for
    :data:`TagName.TABLE_ROW`), whose box insets stay zero. The header's
    bold cell text is layered by the renderer with the shared
    :data:`TagName.BOLD` tag, not set here.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("wrap-mode", Gtk.WrapMode.NONE)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", TABLE_CELL_HPADDING_PX)
    vpadding = _TABLE_HEADER_VPADDING_PX if is_header else _TABLE_ROW_VPADDING_PX
    tag.set_property("pixels-above-lines", vpadding)
    tag.set_property("pixels-below-lines", vpadding)
    return tag


def _make_metadata_tag(name: TagName) -> Gtk.TextTag:
    """Build the metadata-line tag (Created / Modified / tags).

    Carries the *non-colour* text appearance: a slightly reduced scale
    so the line reads as secondary to the title and body, plus the pair
    of pixel gaps that bracket it — ``pixels-above-lines`` binding it to
    the title above (without which a descender in the title collides
    with it) and ``pixels-below-lines`` separating it from the hairline
    rule the wash painter draws at the bottom of the line. The above gap
    is deliberately the smaller of the two; see the constants for why.
    The dim foreground is applied by :func:`apply_palette`. The line
    sits in the same column as the body, so it sets no left/right
    margins — unlike the block-level paragraph tags it is not inset.
    The rule itself is painted separately by ``ArticleTextView`` via the
    ``hairline`` :class:`WashSpec` returned for
    :data:`TagName.METADATA` by :func:`build_wash_specs`.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("scale", _METADATA_SCALE)
    tag.set_property("pixels-above-lines", _METADATA_PIXELS_ABOVE_LINES_PX)
    tag.set_property("pixels-below-lines", _METADATA_PIXELS_BELOW_LINES_PX)
    return tag


def _make_unread_source_tag(
    name: TagName, *, char_width_px: int,
) -> Gtk.TextTag:
    """Build the paragraph tag for verbatim unread source lines.

    Carries the same left-marked geometry as the blockquote body — box
    inset ``0`` so the amber rule aligns with the prose column, text one
    M-width inside it so the glyphs clear the rule — plus a monospace
    family, because the content is raw source and saying so with the font
    avoids needing a label.

    The rule itself is painted by ``ArticleTextView`` from the
    :class:`WashSpec` :func:`build_wash_specs` returns for this tag; like
    every other block-level tag here, this one carries text position only.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _UNREAD_HMARGIN_PX + char_width_px)
    tag.set_property("right-margin", _UNREAD_HMARGIN_PX + char_width_px)
    tag.set_property("pixels-above-lines", _UNREAD_SOURCE_VPADDING_PX)
    tag.set_property("family", _MONOSPACE_FAMILY)
    return tag


def _make_unread_reason_tag(name: TagName) -> Gtk.TextTag:
    """Build the paragraph tag for the reason line under unread source.

    Dimmed and slightly reduced, with a gap below that separates the mark
    from whatever block follows it. It paints no wash: the amber rule
    stops at the source lines, so the reason reads as annotation rather
    than as part of the quarantined content.

    Its foreground is the palette's amber — the same colour as the rule
    wherever contrast allows. On the light sheet the rule's own amber
    fails the contrast floor as text, so the light palette supplies a
    darker stop of the same hue; that split lives in
    :mod:`giruntime.ui.note_render.palette`, not here.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _UNREAD_HMARGIN_PX)
    tag.set_property("scale", _UNREAD_REASON_SCALE)
    tag.set_property("pixels-above-lines", _UNREAD_REASON_PIXELS_ABOVE_PX)
    tag.set_property("pixels-below-lines", _UNREAD_REASON_PIXELS_BELOW_PX)
    return tag

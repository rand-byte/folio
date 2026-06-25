"""Builds the shared :class:`Gtk.TextTagTable` used by the rendered view.

Principles & invariants
-----------------------
* This module is the single owner of every styling tag that appears in
  the rendered view. Other modules apply tags by *name* (looking them up
  on the table) so the visual definitions live in exactly one place. A
  tweak to "what bold looks like" is one edit here, not a hunt across
  the renderer.
* Tag *names* are exposed as :class:`TagName` enum members. The renderer
  and tests reference :data:`TagName.BOLD` rather than the string
  ``"bold"`` — the style rule against magic strings applies inside this
  package as much as anywhere else.
* The current tag set covers, in addition to the inline subset (bold,
  italic, strikethrough, underline, monospace, link) and the heading
  levels the parser produces, the **block-level paragraph styling** for
  admonitions, blockquotes, and code blocks, the under-title metadata
  line, and the four centred lines of the in-surface parse-error notice
  (:data:`TagName.ERROR_NOTICE_ICON` … :data:`TagName.ERROR_NOTICE_HINT`).
  Block-level tags carry
  only the *text position* (``accumulative-margin = True`` plus
  ``left-margin`` / ``right-margin`` = inset + one M-width); the
  matching *tinted wash* is painted by ``ArticleTextView`` in
  :mod:`ui.note_view` using :func:`build_wash_specs` to look
  up tint + inset per tag. This split exists because GTK's
  ``paragraph-background-rgba`` paints exactly between the paragraph's
  effective ``left-margin`` and ``right-margin`` — there is no
  property that decouples "where the wash paints" from "where the
  text starts", so a tinted box that is *wider* than the text on each
  side must be painted at snapshot time. Tables are no longer an
  exception: a rendered table is native buffer text whose rows carry the
  :data:`TagName.TABLE_HEADER` (tint band) / :data:`TagName.TABLE_ROW`
  (bottom hairline) paragraph tags and whose columns are aligned by a
  per-table :class:`Pango.TabArray` the renderer mints — no child widget
  is involved.
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
  M-width" — there is no sensible default, so the parameter is
  required.
* :func:`build_wash_specs` returns the per-tag :class:`WashSpec`
  records the article TextView paints. Tag names that don't paint a
  wash (e.g. :data:`TagName.BLOCKQUOTE_ATTRIBUTION`) are absent from
  the returned dict on purpose — the painter must paint nothing
  behind them. The :data:`TagName.METADATA` line and every
  :data:`TagName.TABLE_ROW` are the *hairline* washes: their
  :class:`WashSpec` carries ``hairline = True`` so the painter draws a
  thin 1-px rule at the bottom of the line (the divider between the
  metadata and the body, or between two table rows) rather than a
  full-height tinted fill. :data:`TagName.TABLE_HEADER` keeps the
  default full *fill* so the header reads as a tint band.
* This module imports ``gi`` because the tag table *is* a GTK object —
  there is no useful pure-Python representation of a tag. The renderer
  is the only other place in :mod:`asciidoc` that imports
  ``gi``; everything else stays display-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gi.repository import Gtk, Pango

from enums import AdmonitionKind


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
    paragraph tag.

    :data:`CODE_BLOCK` is the paragraph tag carrying the code-block's
    left/right paragraph margins; monospace family comes from the
    shared :data:`MONOSPACE` tag, layered on top by the renderer.

    :data:`TABLE_ROW` and :data:`TABLE_HEADER` are the two paragraph tags
    for the rows of a rendered table. Each table row is one logical
    buffer line whose cells are aligned by a per-table
    :class:`Pango.TabArray` (minted anonymously per render by the
    renderer, not carried here). Both tags set ``wrap-mode = NONE`` so a
    row stays on one line and its column alignment holds. The header row
    (``TABLE_HEADER``) paints a tint band behind the line (a *fill*
    :class:`WashSpec`) and the renderer makes its cell text bold; each
    data row (``TABLE_ROW``) paints a 1-px rule at the line's bottom (a
    ``hairline`` :class:`WashSpec`, the same painter shape the metadata
    divider uses) to separate it from the next row.

    :data:`METADATA` is the character/paragraph tag applied to the
    dim-grey metadata line the rendered view inserts directly under the
    title (``Created … · Modified … · #tag …``). It carries a dim grey
    foreground, a slightly reduced scale, and ``pixels-below-lines`` to
    open a gap between the metadata text and the thin horizontal rule
    that the wash painter draws at the bottom of the line (see the
    ``hairline`` :class:`WashSpec` returned for it by
    :func:`build_wash_specs`). It is a :class:`Gtk.TextTag` name only —
    it is never persisted to disk, so it needs no migration.

    :data:`ERROR_NOTICE_ICON` … :data:`ERROR_NOTICE_HINT` are the four
    centred lines of the in-surface parse-error notice the rendered view
    shows when a note's source fails to parse. The view clears the
    buffer and inserts a large amber warning glyph
    (:data:`ERROR_NOTICE_ICON`), a headline (:data:`ERROR_NOTICE_TITLE`),
    the kind-specific message (:data:`ERROR_NOTICE_DETAIL`), and a faint
    recovery hint (:data:`ERROR_NOTICE_HINT`). All four set
    ``justification = CENTER`` and an explicit foreground so they read on
    the opaque white sheet regardless of OS theme — like
    :data:`METADATA`, they are buffer-tag names only and carry **no**
    wash (they are absent from :func:`build_wash_specs`).
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
    ERROR_NOTICE_ICON = "error_notice_icon"
    ERROR_NOTICE_TITLE = "error_notice_title"
    ERROR_NOTICE_DETAIL = "error_notice_detail"
    ERROR_NOTICE_HINT = "error_notice_hint"


@dataclass(frozen=True)
class WashSpec:
    """Wash-painting parameters for one block-level tinted paragraph tag.

    ``tint`` is the RGBA tuple painted behind the paragraph.
    ``box_left_inset_px`` is the distance from the textview's widget
    ``left-margin`` to the box's left edge. ``box_right_inset_px`` is
    the corresponding distance on the right. The text lives one
    M-width inside both edges — that offset is encoded in the
    paragraph tag (added to its ``left-margin`` / ``right-margin``),
    not here, because the painter does not need M-width to paint: it
    only needs the inset.

    ``hairline`` selects between two paint shapes. When ``False`` (the
    default, used by admonitions, blockquotes, and code blocks) the
    painter fills the full vertical extent of the logical line — the
    tinted "card" behind the block. When ``True`` (used by the
    metadata line) the painter draws a thin 1-px rule at the *bottom*
    of the line instead of a full fill, producing the hairline divider
    between the metadata and the body. The horizontal extent (driven by
    the two insets) is computed identically in both cases.
    """

    tint: tuple[float, float, float, float]
    box_left_inset_px: int
    box_right_inset_px: int
    hairline: bool = False


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

    tint: tuple[float, float, float, float]


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
# Visual constants for monospace, link, and block-level styling
# ---------------------------------------------------------------------------
#
# These are not exposed as enum values because they describe *visual*
# settings rather than categorical concepts — there's no closed set of
# legal monospace families or tint colours, only one current choice
# each. They live as module constants so a one-line edit changes the
# look across every rendered note.

_MONOSPACE_FAMILY: str = "monospace"

# A blue close to the GTK Adwaita "accent" colour. Encoded as a CSS-style
# RGB string because :class:`Gtk.TextTag`'s ``foreground`` property
# accepts that form directly. If the renderer ever gains a dark-mode
# variant we'll switch this to a callable that picks per-theme.
_LINK_FOREGROUND: str = "#1a73e8"


# Per-kind tint for admonition paragraph backgrounds. RGBA tuples; the
# alpha is intentionally low so the tint reads as a wash, not a fill.
# The values follow the same palette validated by the rendering
# harness in ``admonition_test/render_admonition.py`` (option A).
_ADMONITION_TINTS: dict[AdmonitionKind, tuple[float, float, float, float]] = {
    AdmonitionKind.NOTE: (0.96, 0.78, 0.55, 0.35),
    AdmonitionKind.TIP: (0.55, 0.85, 0.65, 0.30),
    AdmonitionKind.IMPORTANT: (0.85, 0.55, 0.85, 0.30),
    AdmonitionKind.WARNING: (0.95, 0.65, 0.45, 0.35),
    AdmonitionKind.CAUTION: (0.95, 0.55, 0.55, 0.35),
}

# Per-kind foreground for the bold kind-label text (NOTE / TIP / …).
# Darker shades of each kind's tint so the kind name reads as the
# accent within the tinted block.
_ADMONITION_KIND_FOREGROUNDS: dict[AdmonitionKind, str] = {
    AdmonitionKind.NOTE: "#8a5a00",
    AdmonitionKind.TIP: "#1f6a3a",
    AdmonitionKind.IMPORTANT: "#6a2d6a",
    AdmonitionKind.WARNING: "#a04018",
    AdmonitionKind.CAUTION: "#a02828",
}

# Neutral grey tint for blockquote bodies and code blocks. Both share
# a similar wash so the visual weight is consistent; only blockquotes
# add an italic style (composed via :data:`TagName.ITALIC`) and a
# left-margin indent on top.
_BLOCKQUOTE_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.12)
_CODE_BLOCK_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.08)

# Paragraph metrics applied to admonition paragraph tags. ``HMARGIN``
# is the *box inset* from the textview's widget left/right margin to
# the tinted box's edge. The text inside the box sits one M-width
# inside the box on each side; that offset is added by the paragraph
# tag builder, not stored here.
_ADMONITION_HMARGIN_PX: int = 12
_ADMONITION_VPADDING_PX: int = 8
_ADMONITION_LINE_GAP_PX: int = 2

# Paragraph metrics for blockquotes. The left-margin is the visual
# indent that distinguishes a quote from running prose; the same
# split as admonitions applies — text sits one M-width inside the box
# edge on each side.
_BLOCKQUOTE_HMARGIN_PX: int = 30
_BLOCKQUOTE_RIGHT_MARGIN_PX: int = 12
_BLOCKQUOTE_VPADDING_PX: int = 6
_BLOCKQUOTE_LINE_GAP_PX: int = 2

# Paragraph metrics for code blocks. Slightly narrower margins than a
# blockquote since the monospace font already sits inside whitespace
# from its own glyphs.
_CODE_BLOCK_HMARGIN_PX: int = 16
_CODE_BLOCK_VPADDING_PX: int = 8

# Scale multiplier for the blockquote attribution line. Slightly
# smaller than body text so the citation reads as secondary metadata.
_BLOCKQUOTE_ATTRIBUTION_SCALE: float = 0.9


# Table rows. A rendered table is native buffer text: each row is one
# logical line whose cells are aligned by a per-table ``Pango.TabArray``
# minted by the renderer. Two paragraph tags carry the row treatment.
# The *header* row paints a neutral tint band (a fill wash) behind the
# line; each *data* row paints a 1-px rule at its bottom (a hairline
# wash — the same painter shape the metadata divider uses) to separate
# it from the next row. Both insets are zero so the band / rule span the
# full body text column (the table itself fills that column). The
# header's tint shares the neutral-grey family of the code block, a hair
# stronger so the header reads as a band; the rule reuses the metadata
# rule's grey so the two hairlines match. The vertical padding is applied
# *symmetrically* (``pixels-above-lines`` == ``pixels-below-lines``) on
# both row kinds, so a data row's hairline rule sits clear of the text on
# both sides — the gap below row N and above row N+1 each contribute, and
# the rule lands centred between them — and the header's text is centred
# within its tint band rather than hugging the top edge.
_TABLE_HEADER_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.16)
_TABLE_RULE_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.30)
_TABLE_BOX_INSET_PX: int = 0
_TABLE_ROW_VPADDING_PX: int = 7
_TABLE_HEADER_VPADDING_PX: int = 8


# Metadata line (Created / Modified / tags) under the document title.
# A neutral dim grey for the text, a slightly reduced scale so it reads
# as secondary to the title and body, and a gap below the text that
# separates it from the hairline rule the wash painter draws. The rule
# itself is a light grey RGBA painted as a 1-px band spanning the text
# column — its colour lives here so the whole metadata treatment is one
# place, matching the "one place per visual style" invariant.
_METADATA_FOREGROUND: str = "#808080"
_METADATA_SCALE: float = 0.85
_METADATA_PIXELS_BELOW_LINES_PX: int = 8
_METADATA_RULE_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.30)
_METADATA_RULE_INSET_PX: int = 0


# Parse-error notice (the "empty state" shown in the rendered surface
# when a note's source fails to parse). Four centred lines on the normal
# white sheet: a large warning glyph, a headline, the kind-specific
# message, and a faint recovery hint. The accent is amber — it reads as
# a fixable warning and matches the inline notice this replaced; flip
# ``_ERROR_NOTICE_ICON_FOREGROUND`` to a red (e.g. ``"#c0392b"``) to read
# as a harder error. The title/detail/hint foregrounds are explicit
# (not inherited) because the sheet is an opaque light paper regardless
# of OS theme, so a theme-default foreground could land light-on-white;
# the dim greys mirror :data:`_METADATA_FOREGROUND`. Scales are
# multipliers on the body size so the notice tracks the user's font.
_ERROR_NOTICE_ICON_FOREGROUND: str = "#d4a017"
_ERROR_NOTICE_ICON_SCALE: float = 3.0
_ERROR_NOTICE_ICON_PIXELS_ABOVE_PX: int = 24
_ERROR_NOTICE_TITLE_FOREGROUND: str = "#2c2c2a"
_ERROR_NOTICE_TITLE_SCALE: float = 1.2
_ERROR_NOTICE_TITLE_PIXELS_ABOVE_PX: int = 8
_ERROR_NOTICE_DETAIL_FOREGROUND: str = "#5f5e5a"
_ERROR_NOTICE_DETAIL_SCALE: float = 1.0
_ERROR_NOTICE_DETAIL_PIXELS_ABOVE_PX: int = 6
_ERROR_NOTICE_HINT_FOREGROUND: str = "#888780"
_ERROR_NOTICE_HINT_SCALE: float = 0.9
_ERROR_NOTICE_HINT_PIXELS_ABOVE_PX: int = 12


# Note "sheet". The sheet is the paper the rendered note sits on; it is
# painted by the article text view itself (its CSS background is
# transparent) from the top down to the end of the content, so that below
# the content the view is transparent and the scroller's own background —
# the "desk" — shows through. The sheet is therefore an *opaque* colour
# (it stands in for the page background); the rendered foregrounds and
# block tints are all tuned for this light paper. The sheet meets the desk
# directly, with no rule painted at the boundary.
_SHEET_BACKGROUND: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)


def build_tag_table(*, char_width_px: int) -> Gtk.TextTagTable:
    """Construct the rendered-view tag table for the current subset.

    ``char_width_px`` is the measured M-width of the body font in
    pixels. It is required (no default) because there is no sensible
    default — a wrong default would silently mis-size the inner inset
    on every block-level paragraph tag. Tests pass an explicit small
    int (e.g. ``9``); production passes the result of
    :meth:`ui.note_view.ArticleContainer.char_width_px`.

    The returned table contains exactly one tag per :class:`TagName`
    member. Tag names are unique within a table, so callers that need
    a tag by name use :meth:`Gtk.TextTagTable.lookup` with the
    corresponding :class:`TagName` value.

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
        _make_inline_tag(
            TagName.LINK,
            foreground=_LINK_FOREGROUND,
            underline=Pango.Underline.SINGLE,
        )
    )
    for level, scale in _HEADING_SCALES.items():
        table.add(_make_heading_tag(_LEVEL_TO_TAG_NAME[level], scale=scale))
    for kind in _ADMONITION_TINTS:
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
                foreground=_ADMONITION_KIND_FOREGROUNDS[kind],
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
    table.add(_make_table_row_tag(TagName.TABLE_ROW, is_header=False))
    table.add(_make_table_row_tag(TagName.TABLE_HEADER, is_header=True))
    table.add(_make_metadata_tag(TagName.METADATA))
    table.add(
        _make_error_notice_tag(
            TagName.ERROR_NOTICE_ICON,
            foreground=_ERROR_NOTICE_ICON_FOREGROUND,
            scale=_ERROR_NOTICE_ICON_SCALE,
            pixels_above_px=_ERROR_NOTICE_ICON_PIXELS_ABOVE_PX,
        )
    )
    table.add(
        _make_error_notice_tag(
            TagName.ERROR_NOTICE_TITLE,
            foreground=_ERROR_NOTICE_TITLE_FOREGROUND,
            scale=_ERROR_NOTICE_TITLE_SCALE,
            pixels_above_px=_ERROR_NOTICE_TITLE_PIXELS_ABOVE_PX,
            weight=Pango.Weight.SEMIBOLD,
        )
    )
    table.add(
        _make_error_notice_tag(
            TagName.ERROR_NOTICE_DETAIL,
            foreground=_ERROR_NOTICE_DETAIL_FOREGROUND,
            scale=_ERROR_NOTICE_DETAIL_SCALE,
            pixels_above_px=_ERROR_NOTICE_DETAIL_PIXELS_ABOVE_PX,
        )
    )
    table.add(
        _make_error_notice_tag(
            TagName.ERROR_NOTICE_HINT,
            foreground=_ERROR_NOTICE_HINT_FOREGROUND,
            scale=_ERROR_NOTICE_HINT_SCALE,
            pixels_above_px=_ERROR_NOTICE_HINT_PIXELS_ABOVE_PX,
        )
    )
    return table


def build_wash_specs() -> dict[TagName, WashSpec]:
    """Return the per-tag wash spec the article TextView paints.

    Keys are :class:`TagName` values for every paragraph tag that
    carries a wash. Tag names that *don't* paint a wash (e.g.
    :data:`TagName.BLOCKQUOTE_ATTRIBUTION`) are absent on purpose —
    the painter must paint nothing behind them.

    The admonition label and body for the same kind share an
    *identical* :class:`WashSpec` instance by design so they read as
    one rectangle: the painter walks logical lines independently, but
    the two paragraphs end up painted with the same colour at the
    same horizontal extents, so the two rectangles butt edge-to-edge
    and the user sees one block.
    """
    specs: dict[TagName, WashSpec] = {}
    for kind, tint in _ADMONITION_TINTS.items():
        spec = WashSpec(
            tint=tint,
            box_left_inset_px=_ADMONITION_HMARGIN_PX,
            box_right_inset_px=_ADMONITION_HMARGIN_PX,
        )
        specs[_ADMONITION_LABEL_TAG_NAMES[kind]] = spec
        specs[_ADMONITION_BODY_TAG_NAMES[kind]] = spec
    specs[TagName.BLOCKQUOTE_BODY] = WashSpec(
        tint=_BLOCKQUOTE_TINT,
        box_left_inset_px=_BLOCKQUOTE_HMARGIN_PX,
        box_right_inset_px=_BLOCKQUOTE_RIGHT_MARGIN_PX,
    )
    specs[TagName.CODE_BLOCK] = WashSpec(
        tint=_CODE_BLOCK_TINT,
        box_left_inset_px=_CODE_BLOCK_HMARGIN_PX,
        box_right_inset_px=_CODE_BLOCK_HMARGIN_PX,
    )
    # Table header: a tint band (full fill) spanning the body column.
    specs[TagName.TABLE_HEADER] = WashSpec(
        tint=_TABLE_HEADER_TINT,
        box_left_inset_px=_TABLE_BOX_INSET_PX,
        box_right_inset_px=_TABLE_BOX_INSET_PX,
    )
    # Table data row: a 1-px rule at the line's bottom (hairline), the
    # same painter shape the metadata divider uses.
    specs[TagName.TABLE_ROW] = WashSpec(
        tint=_TABLE_RULE_TINT,
        box_left_inset_px=_TABLE_BOX_INSET_PX,
        box_right_inset_px=_TABLE_BOX_INSET_PX,
        hairline=True,
    )
    specs[TagName.METADATA] = WashSpec(
        tint=_METADATA_RULE_TINT,
        box_left_inset_px=_METADATA_RULE_INSET_PX,
        box_right_inset_px=_METADATA_RULE_INSET_PX,
        hairline=True,
    )
    return specs


def build_sheet_wash() -> SheetWash:
    """Return the note sheet colour.

    The sheet is painted by the article text view behind the content
    (the view's CSS background is transparent so the desk shows below).
    Sourced here so every rendered-view colour lives in this one module,
    the same way :func:`build_wash_specs` owns the paragraph washes.
    """
    return SheetWash(tint=_SHEET_BACKGROUND)


def _make_inline_tag(  # pylint: disable=too-many-arguments
    name: TagName,
    *,
    weight: Pango.Weight | None = None,
    style: Pango.Style | None = None,
    strikethrough: bool | None = None,
    underline: Pango.Underline | None = None,
    family: str | None = None,
    foreground: str | None = None,
) -> Gtk.TextTag:
    """Build a single inline-style tag with the requested visual rule.

    Only the property the caller passes is set; the rest are left at
    their inherited defaults so multiple tags on the same range
    compose without one tag erasing another's contribution. This is
    why ``LINK`` (foreground + underline) and ``UNDERLINE`` (just
    underline) coexist cleanly when both apply to the same range.

    The argument list grows one element each time we add an inline
    construct, which is unavoidable: each is a distinct
    :class:`Gtk.TextTag` property. Refactoring to a single ``props``
    mapping would lose the type-checked keyword surface — and there
    are only six properties total in the closed AsciiDoc subset, so
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
    if foreground is not None:
        tag.set_property("foreground", foreground)
    return tag


def _make_heading_tag(name: TagName, *, scale: float) -> Gtk.TextTag:
    """Build a heading-style tag: bold weight at the given scale."""
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("weight", Pango.Weight.BOLD)
    tag.set_property("scale", scale)
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
    so the text sits one M-width inside the tinted box's edge.
    ``accumulative-margin = True`` makes those values *stack* on the
    textview's widget-level ``left-margin`` / ``right-margin`` instead
    of replacing them — without this flag a paragraph tag overrides
    the widget's margins and the text escapes the inner column. The
    matching tinted wash is painted separately by ``ArticleTextView``
    in :mod:`ui.note_view` (see :func:`build_wash_specs`).

    ``is_label`` toggles where the block padding sits: the *label*
    paragraph gets padding above so the block has an even top margin,
    and the *body* paragraph gets padding below for a symmetric bottom
    margin. Side margins and the in-wrap line spacing are shared.
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
        0 if is_label else _ADMONITION_VPADDING_PX,
    )
    tag.set_property("pixels-inside-wrap", _ADMONITION_LINE_GAP_PX)
    return tag


def _make_blockquote_body_tag(
    name: TagName, *, char_width_px: int,
) -> Gtk.TextTag:
    """Build the blockquote-body paragraph tag.

    Carries the text position only: the left/right margins are the
    box inset plus one M-width so the text sits one M-width inside
    the tinted box. ``accumulative-margin = True`` makes those margins
    stack on the textview's widget-level margins (see
    :func:`_make_admonition_paragraph_tag` for why this matters). The
    italic style is *not* set here — the renderer composes it by
    layering the shared :data:`TagName.ITALIC` tag across the body
    range so a future tweak to "what italic looks like" remains a
    one-line edit. The tint is painted separately by
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
    the quote body's *text* (one M-width inside the tinted box's
    edge), applies a smaller scale, and right-aligns the text so a
    typical ``— Author, Source`` line reads as a citation under the
    quote. There is no tint to remove (the attribution never carried
    one). ``accumulative-margin = True`` is set for the same reason
    as the body tag — without it, the attribution paragraph would
    escape the inner column.
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

    Carries the text position only: the left/right margins are the
    box inset plus one M-width so the monospace text sits one M-width
    inside the tinted box. ``accumulative-margin = True`` makes those
    margins stack on the textview's widget-level margins. The
    monospace family comes from the shared :data:`TagName.MONOSPACE`
    tag, which the renderer applies on top of this one across the
    same range. The tint is painted separately by ``ArticleTextView``
    in :mod:`ui.note_view`.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("accumulative-margin", True)
    tag.set_property("left-margin", _CODE_BLOCK_HMARGIN_PX + char_width_px)
    tag.set_property("right-margin", _CODE_BLOCK_HMARGIN_PX + char_width_px)
    tag.set_property("pixels-above-lines", _CODE_BLOCK_VPADDING_PX)
    tag.set_property("pixels-below-lines", _CODE_BLOCK_VPADDING_PX)
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
      column width less :data:`config.defaults.TABLE_CELL_GUTTER_PX`.
    * ``pixels-above-lines`` / ``pixels-below-lines`` open *symmetric*
      breathing room above and below the row, so a data row's hairline
      rule sits clear of the text on both sides (the rule lands centred
      in the gap between two rows) and the header text is centred within
      its tint band.

    The tag sets **no** left/right margin: a table fills the body text
    column, and the tab stops position the columns within it. The tinted
    band (header) or 1-px rule (data row) is painted separately by
    ``ArticleTextView`` in :mod:`ui.note_view` via the
    :class:`WashSpec` :func:`build_wash_specs` returns for this tag — a
    *fill* for :data:`TagName.TABLE_HEADER`, a ``hairline`` for
    :data:`TagName.TABLE_ROW`. The header's bold cell text is layered by
    the renderer with the shared :data:`TagName.BOLD` tag, not set here.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("wrap-mode", Gtk.WrapMode.NONE)
    vpadding = _TABLE_HEADER_VPADDING_PX if is_header else _TABLE_ROW_VPADDING_PX
    tag.set_property("pixels-above-lines", vpadding)
    tag.set_property("pixels-below-lines", vpadding)
    return tag


def _make_metadata_tag(name: TagName) -> Gtk.TextTag:
    """Build the metadata-line tag (Created / Modified / tags).

    Carries the *text* appearance only — a dim grey foreground and a
    slightly reduced scale so the line reads as secondary to the title
    and body. ``pixels-below-lines`` opens the gap that separates the
    text from the hairline rule the wash painter draws at the bottom of
    the line. The line sits in the same column as the body, so it sets
    no left/right margins — unlike the block-level paragraph tags it
    is not inset. The rule itself is painted separately by
    ``ArticleTextView`` in :mod:`ui.note_view` via the ``hairline``
    :class:`WashSpec` returned for :data:`TagName.METADATA` by
    :func:`build_wash_specs`.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("foreground", _METADATA_FOREGROUND)
    tag.set_property("scale", _METADATA_SCALE)
    tag.set_property("pixels-below-lines", _METADATA_PIXELS_BELOW_LINES_PX)
    return tag


def _make_error_notice_tag(
    name: TagName,
    *,
    foreground: str,
    scale: float,
    pixels_above_px: int,
    weight: Pango.Weight | None = None,
) -> Gtk.TextTag:
    """Build one centred line of the in-surface parse-error notice.

    Each notice line is its own paragraph, so the tag carries both the
    *paragraph* property (``justification = CENTER``, plus
    ``pixels-above-lines`` for the gap above the line) and the
    *character* appearance (``foreground`` + ``scale``, and an optional
    ``weight`` for the headline). The foreground is always explicit
    because the rendered note sits on an opaque light sheet whatever the
    OS theme — an inherited theme-default foreground could be invisible.
    Unlike the block-level tags these set no margins (the notice sits in
    the body column) and paint no wash, so they never appear in
    :func:`build_wash_specs`.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("justification", Gtk.Justification.CENTER)
    tag.set_property("foreground", foreground)
    tag.set_property("scale", scale)
    tag.set_property("pixels-above-lines", pixels_above_px)
    if weight is not None:
        tag.set_property("weight", weight)
    return tag

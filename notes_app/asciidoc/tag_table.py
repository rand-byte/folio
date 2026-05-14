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
  admonitions, blockquotes, and code blocks. Block-level styling lives
  on paragraph tags (``paragraph-background-rgba`` for the tint plus
  ``left-margin`` / ``right-margin`` / ``pixels-above/below-lines`` for
  the spacing). Tables remain the one exception — they are still drawn
  by a child widget — because :class:`Gtk.TextTag` has no grid primitive.
* Admonition paragraph tags come in two roles per kind. The *label*
  paragraph carries the kind name on its own line; the *body* paragraph
  carries the prose. Both paragraphs share the per-kind tint so the
  block reads as one rectangle. The *kind character* tag adds the bold
  weight and the accent foreground colour to the kind text itself
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
* This module imports ``gi`` because the tag table *is* a GTK object —
  there is no useful pure-Python representation of a tag. The renderer
  is the only other place in :mod:`notes_app.asciidoc` that imports
  ``gi``; everything else stays display-agnostic.
"""

from __future__ import annotations

from enum import StrEnum

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from notes_app.enums import AdmonitionKind


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
    tint and left/right paragraph margins; monospace family comes from
    the shared :data:`MONOSPACE` tag, layered on top by the renderer.
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
# the same wash so the visual weight is consistent; only blockquotes
# add an italic style (composed via :data:`TagName.ITALIC`) and a
# left-margin indent on top.
_BLOCKQUOTE_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.12)
_CODE_BLOCK_TINT: tuple[float, float, float, float] = (0.5, 0.5, 0.5, 0.08)

# Paragraph metrics applied to admonition paragraph tags. Padding around
# the tinted block reads as one visual rectangle when the same numbers
# are used for the label paragraph's pixels-above and the body's
# pixels-below — this mirrors the layout validated by option A.
_ADMONITION_HMARGIN_PX: int = 12
_ADMONITION_VPADDING_PX: int = 8
_ADMONITION_LINE_GAP_PX: int = 2

# Paragraph metrics for blockquotes. The left-margin is the visual
# indent that distinguishes a quote from running prose.
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


def build_tag_table() -> Gtk.TextTagTable:
    """Construct the rendered-view tag table for the current subset.

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
    for kind, tint in _ADMONITION_TINTS.items():
        table.add(
            _make_admonition_paragraph_tag(
                _ADMONITION_LABEL_TAG_NAMES[kind],
                tint=tint,
                is_label=True,
            )
        )
        table.add(
            _make_admonition_paragraph_tag(
                _ADMONITION_BODY_TAG_NAMES[kind],
                tint=tint,
                is_label=False,
            )
        )
        table.add(
            _make_inline_tag(
                _ADMONITION_KIND_TAG_NAMES[kind],
                weight=Pango.Weight.BOLD,
                foreground=_ADMONITION_KIND_FOREGROUNDS[kind],
            )
        )
    table.add(_make_blockquote_body_tag(TagName.BLOCKQUOTE_BODY))
    table.add(_make_blockquote_attribution_tag(TagName.BLOCKQUOTE_ATTRIBUTION))
    table.add(_make_code_block_tag(TagName.CODE_BLOCK))
    return table


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
    tint: tuple[float, float, float, float],
    is_label: bool,
) -> Gtk.TextTag:
    """Build an admonition paragraph tag (label or body role).

    The tint applies to the whole paragraph. ``is_label`` toggles where
    the block padding sits: the *label* paragraph gets padding above
    so the block has an even top margin, and the *body* paragraph gets
    padding below for a symmetric bottom margin. Side margins and the
    in-wrap line spacing are shared.
    """
    tag = Gtk.TextTag.new(name.value)
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = tint
    tag.set_property("paragraph-background-rgba", rgba)
    tag.set_property("left-margin", _ADMONITION_HMARGIN_PX)
    tag.set_property("right-margin", _ADMONITION_HMARGIN_PX)
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


def _make_blockquote_body_tag(name: TagName) -> Gtk.TextTag:
    """Build the blockquote-body paragraph tag.

    Carries the tint, the indent left-margin, and balanced top/bottom
    padding so a multi-paragraph quote reads as one block. The italic
    style is *not* set here — the renderer composes it by layering the
    shared :data:`TagName.ITALIC` tag across the body range so a
    future tweak to "what italic looks like" remains a one-line edit.
    """
    tag = Gtk.TextTag.new(name.value)
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = _BLOCKQUOTE_TINT
    tag.set_property("paragraph-background-rgba", rgba)
    tag.set_property("left-margin", _BLOCKQUOTE_HMARGIN_PX)
    tag.set_property("right-margin", _BLOCKQUOTE_RIGHT_MARGIN_PX)
    tag.set_property("pixels-above-lines", _BLOCKQUOTE_VPADDING_PX)
    tag.set_property("pixels-below-lines", _BLOCKQUOTE_VPADDING_PX)
    tag.set_property("pixels-inside-wrap", _BLOCKQUOTE_LINE_GAP_PX)
    return tag


def _make_blockquote_attribution_tag(name: TagName) -> Gtk.TextTag:
    """Build the blockquote-attribution paragraph tag.

    Shares the body's left-margin so the attribution sits flush with
    the quote, applies a smaller scale, and right-aligns the text so
    a typical ``— Author, Source`` line reads as a citation under the
    quote.
    """
    tag = Gtk.TextTag.new(name.value)
    tag.set_property("left-margin", _BLOCKQUOTE_HMARGIN_PX)
    tag.set_property("right-margin", _BLOCKQUOTE_RIGHT_MARGIN_PX)
    tag.set_property("scale", _BLOCKQUOTE_ATTRIBUTION_SCALE)
    tag.set_property("justification", Gtk.Justification.RIGHT)
    return tag


def _make_code_block_tag(name: TagName) -> Gtk.TextTag:
    """Build the code-block paragraph tag.

    Carries the subtle background tint and balanced left/right margins
    so the block is visually offset from running prose. The monospace
    family comes from the shared :data:`TagName.MONOSPACE` tag, which
    the renderer applies on top of this one across the same range.
    """
    tag = Gtk.TextTag.new(name.value)
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = _CODE_BLOCK_TINT
    tag.set_property("paragraph-background-rgba", rgba)
    tag.set_property("left-margin", _CODE_BLOCK_HMARGIN_PX)
    tag.set_property("right-margin", _CODE_BLOCK_HMARGIN_PX)
    tag.set_property("pixels-above-lines", _CODE_BLOCK_VPADDING_PX)
    tag.set_property("pixels-below-lines", _CODE_BLOCK_VPADDING_PX)
    return tag

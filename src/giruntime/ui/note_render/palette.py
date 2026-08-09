"""Every colour the rendered view paints, in a light and a dark set.

Principles & invariants
-----------------------
* This module is the single owner of rendered-view *colour*.
  :mod:`giruntime.ui.note_render.tag_table` owns the matching
  *structure* — geometry, typography, tag identity — and consumes a
  :class:`Palette` for the colours. A change to "what a NOTE admonition
  looks like" is one edit here (its tint) or one edit there (its
  padding), never a hunt across both.
* Colour is separated from structure because it, alone, varies at
  runtime: the same tag table is re-coloured in place when the theme
  changes (see :func:`tag_table.apply_palette`), while every geometry
  value is fixed for the life of the surface by the measured font.
* :data:`LIGHT_PALETTE` and :data:`DARK_PALETTE` are the only two
  instances. They are ``Final`` module constants rather than a factory
  because there is no per-note or per-window variation — two palettes
  exist because two *sheets* exist.
* **Both palettes must be complete.** ``Palette`` groups its per-kind
  colours in enum-keyed mappings, which — unlike a fixed-field type —
  can be short a member and only fail at lookup time. The completeness
  tests in ``test_palette.py`` are therefore load-bearing, not garnish:
  they are what stands between a half-filled palette and a
  :class:`KeyError` in front of a user.
* Two colour representations, on purpose. A :class:`str` field is a
  CSS-style colour literal (``"#d4a017"``) destined for a
  :class:`Gtk.TextTag` ``foreground`` property, which accepts that form
  directly. An :data:`Rgba` field is RGBA in 0-1 destined for the
  snapshot painter, which needs a :class:`Gdk.RGBA` for
  ``append_color``. Foreground ⇒ string, wash/tint/sheet ⇒ tuple.
* The sheet is *opaque* in both palettes: it stands in for the page
  background, and every foreground here is tuned for contrast against
  it. The "desk" the sheet sits on is deliberately **not** here — it is
  the parent's real background, i.e. the theme's, so the reading
  surface always frames itself with a colour the OS chose.
* This module is GTK-free and display-free: it holds data and one pure
  classification rule, so every palette invariant is testable without a
  compositor. The :meth:`Gtk.Widget.get_color` call that feeds
  :func:`scheme_for_foreground` lives in ``ArticleTextView``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from enums import AdmonitionKind, ColorScheme, UnreadMarkPart


type Rgba = tuple[float, float, float, float]
"""Red, green, blue, alpha in 0-1 — the form
:meth:`Gtk.Snapshot.append_color` needs, via :class:`Gdk.RGBA`.

Used for everything the wash painter draws (block tints, hairline rules,
the sheet). Foregrounds are plain :class:`str` instead, because they are
set on a :class:`Gtk.TextTag`, which parses CSS-style colour strings.
"""


@dataclass(frozen=True)
class Palette:  # pylint: disable=too-many-instance-attributes
    """Every colour the rendered view paints, for one colour scheme.

    Every field is required: a palette that omits a colour cannot be
    constructed, so "the dark set forgot the link colour" is a type
    error rather than a blank link. The enum-keyed mappings are the
    exception the module docstring calls out — their *members* are
    checked by test, not by the type system.

    Field groups, in painting order: the ``sheet`` the note sits on;
    the foregrounds drawn on it; the tints the wash painter fills,
    bars, and rules with.

    The field count is over pylint's ceiling by design. This is a record
    of the colours the renderer needs, and the count is set by that
    renderer, not by this class taking on responsibilities: there are
    thirteen because the rendered view paints thirteen distinguishable
    things. Grouping them into sub-records to satisfy the ceiling would
    add a layer of naming (``palette.foregrounds.link``) that buys
    nothing — every field is read exactly once, by the one function that
    paints it.
    """

    sheet: Rgba
    body_foreground: str
    link_foreground: str
    metadata_foreground: str
    metadata_rule_tint: Rgba
    admonition_tints: Mapping[AdmonitionKind, Rgba]
    admonition_kind_foregrounds: Mapping[AdmonitionKind, str]
    code_block_tint: Rgba
    blockquote_bar_tint: Rgba
    table_header_tint: Rgba
    table_rule_tint: Rgba
    unread_foregrounds: Mapping[UnreadMarkPart, str]
    unread_bar_tint: Rgba


# ---------------------------------------------------------------------------
# The light palette — the application's original (and default) look
# ---------------------------------------------------------------------------
#
# White paper, dark ink. Every value here is the constant that used to
# live in ``tag_table.py``, moved unchanged, so the light rendering is
# pixel-identical to what it always was.
#
# ``body_foreground`` is the one genuinely new value: body and heading
# text previously set no foreground at all and inherited the theme's,
# which is correct only for as long as the theme's ink happens to suit
# an always-white sheet. It is a near-black rather than pure black, the
# usual choice for long-form reading on paper white.

LIGHT_PALETTE: Final[Palette] = Palette(
    sheet=(1.0, 1.0, 1.0, 1.0),
    body_foreground="#1a1a18",
    # A blue close to the GTK Adwaita "accent" colour.
    link_foreground="#1a73e8",
    metadata_foreground="#808080",
    metadata_rule_tint=(0.5, 0.5, 0.5, 0.30),
    # Low alpha so each tint reads as a wash, not a fill.
    admonition_tints={
        AdmonitionKind.NOTE: (0.96, 0.78, 0.55, 0.35),
        AdmonitionKind.TIP: (0.55, 0.85, 0.65, 0.30),
        AdmonitionKind.IMPORTANT: (0.85, 0.55, 0.85, 0.30),
        AdmonitionKind.WARNING: (0.95, 0.65, 0.45, 0.35),
        AdmonitionKind.CAUTION: (0.95, 0.55, 0.55, 0.35),
    },
    # Darker shades of each kind's tint, so the kind name reads as the
    # accent within the tinted block.
    admonition_kind_foregrounds={
        AdmonitionKind.NOTE: "#8a5a00",
        AdmonitionKind.TIP: "#1f6a3a",
        AdmonitionKind.IMPORTANT: "#6a2d6a",
        AdmonitionKind.WARNING: "#a04018",
        AdmonitionKind.CAUTION: "#a02828",
    },
    code_block_tint=(0.5, 0.5, 0.5, 0.08),
    blockquote_bar_tint=(0.5, 0.5, 0.5, 0.5),
    table_header_tint=(0.5, 0.5, 0.5, 0.16),
    table_rule_tint=(0.5, 0.5, 0.5, 0.30),
    # Amber accent: the notice reads as a fixable warning rather than a
    # hard error. The other three mirror the metadata grey's role —
    # secondary text on the sheet.
    # Amber: an unread block reads as a fixable warning, not a hard
    # error. The bar carries the accent at full strength; the reason
    # line uses a darker stop of the same hue because the bar's own
    # ``#d4a017`` measures 2.4:1 as text on this white sheet, under the
    # 4.5:1 floor ``test_palette.py`` enforces. ``#8a5a00`` is not a new
    # colour -- it is the NOTE admonition's accent, reused.
    unread_foregrounds={
        UnreadMarkPart.SOURCE: "#1a1a18",
        UnreadMarkPart.REASON: "#8a5a00",
    },
    unread_bar_tint=(0.83, 0.63, 0.09, 1.0),
)


# ---------------------------------------------------------------------------
# The dark palette
# ---------------------------------------------------------------------------
#
# Not an inversion of the light set — a re-derivation, because three of
# its rules do not survive inversion:
#
# 1. The sheet stays a *page on a desk*. It is a dark neutral chosen to
#    sit a step LIGHTER than the desk it lies on, so the note still
#    reads as paper laid on a surface rather than a hole cut in one.
#    The desk is the theme's window background — plain GTK 4's
#    Adwaita dark paints it ``#353535`` (relative luminance 0.036), so
#    the sheet is picked just above that. Measured from a screenshot,
#    not assumed: an earlier value chosen against libadwaita's darker
#    ``#242424`` came out *below* the real desk and the page read as a
#    hole. GTK offers no supported way to probe a widget's resolved
#    background (``get_style_context`` is deprecated since 4.10 and
#    ``get_color`` returns the foreground), so this is a tuned constant
#    and the relationship is verified by eye — see the screenshot
#    recipe in ``dev-environment.md``.
#
#    Raising the sheet costs foreground contrast, which is why every
#    ink here is lighter than a first pass would suggest; the contrast
#    floors in ``test_palette.py`` are what keep the two in balance.
# 2. Ink is off-white, never pure white: full-strength white on a dark
#    ground haloes and is tiring over a long note.
# 3. The chromatic tints are darkened and *raised* in alpha rather than
#    reused. A pale pastel at 0.3 alpha over a dark sheet turns muddy
#    grey and loses its identity; these are deep, saturated versions of
#    the same five hues, and their kind labels are correspondingly
#    lightened so the label still reads as the accent within the block.
#
# The neutral greys (code block, table band/rule, quote bar, metadata
# rule) keep the same mid-grey family: a grey wash lightens a dark sheet
# exactly as it darkens a light one. Only their alphas are raised, since
# the same alpha carries less contrast against a dark ground.

DARK_PALETTE: Final[Palette] = Palette(
    sheet=(0.23, 0.23, 0.22, 1.0),
    body_foreground="#e3e1dc",
    # Lifted well above the light set's blue: #1a73e8 on a dark sheet is
    # near-illegible, and a link must stay distinguishable from body ink
    # without becoming the brightest thing on the page.
    link_foreground="#7fb4f5",
    metadata_foreground="#9a9892",
    metadata_rule_tint=(0.5, 0.5, 0.5, 0.45),
    admonition_tints={
        AdmonitionKind.NOTE: (0.55, 0.40, 0.10, 0.40),
        AdmonitionKind.TIP: (0.15, 0.45, 0.28, 0.40),
        AdmonitionKind.IMPORTANT: (0.42, 0.22, 0.45, 0.40),
        AdmonitionKind.WARNING: (0.55, 0.30, 0.12, 0.40),
        AdmonitionKind.CAUTION: (0.55, 0.20, 0.20, 0.40),
    },
    admonition_kind_foregrounds={
        AdmonitionKind.NOTE: "#f0c05a",
        AdmonitionKind.TIP: "#7fd8a0",
        AdmonitionKind.IMPORTANT: "#dfa6df",
        AdmonitionKind.WARNING: "#f5a97a",
        AdmonitionKind.CAUTION: "#f28b8b",
    },
    code_block_tint=(0.5, 0.5, 0.5, 0.14),
    blockquote_bar_tint=(0.6, 0.6, 0.6, 0.55),
    table_header_tint=(0.5, 0.5, 0.5, 0.22),
    table_rule_tint=(0.5, 0.5, 0.5, 0.45),
    # On the dark sheet the bar's own amber clears the contrast floor as
    # text (5.7:1), so the reason line and the bar are literally the same
    # colour here -- the split in the light palette is a contrast
    # concession, not a design difference.
    unread_foregrounds={
        UnreadMarkPart.SOURCE: "#e3e1dc",
        UnreadMarkPart.REASON: "#e0b243",
    },
    unread_bar_tint=(0.88, 0.70, 0.26, 1.0),
)


_SCHEME_TO_PALETTE: dict[ColorScheme, Palette] = {
    ColorScheme.LIGHT: LIGHT_PALETTE,
    ColorScheme.DARK: DARK_PALETTE,
}


def palette_for(scheme: ColorScheme) -> Palette:
    """Return the palette for ``scheme``.

    Exhaustive over :class:`ColorScheme` by construction — a new member
    added without a palette raises :class:`KeyError` here, which is the
    right loud failure rather than a silent fall back to the light set.
    """
    return _SCHEME_TO_PALETTE[scheme]


_LIGHT_FOREGROUND_LUMINANCE_THRESHOLD: Final[float] = 0.5
"""Relative luminance above which a theme's text colour counts as *light*.

A light foreground means the surface the theme expects behind it is
dark, which is what :func:`scheme_for_foreground` actually decides. The
midpoint is deliberate: real themes cluster hard at the ends (Adwaita's
light and dark foregrounds measure 0.0 and 1.0), so no plausible theme
sits near enough to the boundary for a finer rule to matter.
"""


_SRGB_LINEAR_CUTOFF: Final[float] = 0.04045
_SRGB_LINEAR_DIVISOR: Final[float] = 12.92
_SRGB_GAMMA_OFFSET: Final[float] = 0.055
_SRGB_GAMMA_DIVISOR: Final[float] = 1.055
_SRGB_GAMMA_EXPONENT: Final[float] = 2.4
_LUMINANCE_RED_WEIGHT: Final[float] = 0.2126
_LUMINANCE_GREEN_WEIGHT: Final[float] = 0.7152
_LUMINANCE_BLUE_WEIGHT: Final[float] = 0.0722


def relative_luminance(red: float, green: float, blue: float) -> float:
    """Return the WCAG relative luminance of an sRGB colour in 0-1.

    Each channel is linearised out of sRGB's transfer curve and then
    weighted for the eye's differing sensitivity to the three primaries
    — which is why a mid-green reads far lighter than a mid-blue of the
    same numeric value, and why a naive channel average would classify
    some themes wrongly.

    Alpha is not a parameter: this answers "how light is this colour",
    and a theme foreground is opaque.
    """
    return (
        _LUMINANCE_RED_WEIGHT * _linearise(red)
        + _LUMINANCE_GREEN_WEIGHT * _linearise(green)
        + _LUMINANCE_BLUE_WEIGHT * _linearise(blue)
    )


def _linearise(channel: float) -> float:
    """Undo the sRGB transfer curve for one 0-1 channel value.

    The ``float(...)`` is not decoration: ``float.__pow__`` is typed as
    returning :class:`Any` (it may yield a ``complex`` for a negative
    base), and this module does not return ``Any``.
    """
    if channel <= _SRGB_LINEAR_CUTOFF:
        return channel / _SRGB_LINEAR_DIVISOR
    return float(
        ((channel + _SRGB_GAMMA_OFFSET) / _SRGB_GAMMA_DIVISOR)
        ** _SRGB_GAMMA_EXPONENT
    )


def scheme_for_foreground(
    red: float, green: float, blue: float,
) -> ColorScheme:
    """Classify a theme's resolved *text* colour into a colour scheme.

    The rule is deliberately indirect: it measures the foreground the
    theme paints text in, and infers the scheme the *surface* wants. A
    light foreground means the theme expects a dark background behind
    it, so the rendered view answers with its dark sheet.

    Measuring beats asking. Neither ``Gtk.Settings`` probe is reliable —
    under ``GTK_THEME=Adwaita:dark`` the theme name reads ``"Default"``
    and ``gtk-application-prefer-dark-theme`` reads :data:`False`, while
    the resolved foreground reads pure white. Because this rule reads
    the *outcome*, it is correct for every route to a dark chrome (the
    settings portal, ``GTK_THEME``, a ``settings.ini``, a third-party
    theme) with no D-Bus, no gsettings, and no libadwaita.
    """
    if (
        relative_luminance(red, green, blue)
        > _LIGHT_FOREGROUND_LUMINANCE_THRESHOLD
    ):
        return ColorScheme.DARK
    return ColorScheme.LIGHT

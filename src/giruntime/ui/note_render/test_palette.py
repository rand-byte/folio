"""Tests for :mod:`giruntime.ui.note_render.palette`.

No display and no GTK: the palette is plain data plus one pure
classification rule, which is the point of keeping it out of
``tag_table.py``. Everything here runs in a bare interpreter.
"""

from __future__ import annotations

import unittest

from enums import AdmonitionKind, ColorScheme, UnreadMarkPart
from giruntime.ui.note_render.palette import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    Palette,
    Rgba,
    palette_for,
    relative_luminance,
    scheme_for_foreground,
)


_ALL_PALETTES: tuple[tuple[str, Palette], ...] = (
    ("light", LIGHT_PALETTE),
    ("dark", DARK_PALETTE),
)
"""Both shipped palettes, labelled so a failure names the culprit.

Iterating them is the one place this file loops rather than writing
straight-line cases: the assertions below are *invariants every palette
must hold*, so a new palette should be covered by construction rather
than by someone remembering to copy a test.
"""


_ALPHA_INDEX: int = 3
_OPAQUE_ALPHA: float = 1.0


def _parse_hex_color(value: str) -> tuple[float, float, float]:
    """Return the 0-1 RGB of a ``#rrggbb`` literal.

    The palette stores foregrounds in the CSS-style form
    :class:`Gtk.TextTag` accepts; the contrast assertions need numbers.
    """
    red = int(value[1:3], 16) / 255.0
    green = int(value[3:5], 16) / 255.0
    blue = int(value[5:7], 16) / 255.0
    return red, green, blue


def _contrast_ratio(foreground: str, sheet: Rgba) -> float:
    """WCAG contrast ratio between a foreground literal and a sheet.

    Both luminances get the ``+ 0.05`` flare term the WCAG formula
    specifies, and the lighter of the two goes on top.
    """
    fg_luminance = relative_luminance(*_parse_hex_color(foreground))
    sheet_luminance = relative_luminance(sheet[0], sheet[1], sheet[2])
    lighter = max(fg_luminance, sheet_luminance)
    darker = min(fg_luminance, sheet_luminance)
    return (lighter + 0.05) / (darker + 0.05)


_MIN_TEXT_CONTRAST_RATIO: float = 4.5
"""WCAG AA for body text — the floor for everything meant to be *read*.

Applied to the admonition kind labels too, which the standard would let
off as large text: they are short, coloured, and the easiest thing to
get wrong when hand-picking a dark variant, so the strict floor is where
the value is.
"""


_MIN_SECONDARY_CONTRAST_RATIO: float = 3.0
"""The floor for the two deliberately dim, secondary lines.

The metadata line and the notice's recovery hint are designed to recede;
the *shipped light palette* has always sat below AA on both (measured
3.95 and 3.61 against white), and this work moved those values
unchanged, so holding them to 4.5 here would be asserting a redesign
nobody agreed to. The floor is therefore set where it catches a
regression — a future edit that makes either materially fainter than it
already is — rather than where it would fail the code as it stands. The
dark values chosen here clear the strict text floor anyway.
"""


class PaletteLookupTests(unittest.TestCase):
    def test_light_scheme_resolves_to_light_palette(self) -> None:
        self.assertIs(palette_for(ColorScheme.LIGHT), LIGHT_PALETTE)

    def test_dark_scheme_resolves_to_dark_palette(self) -> None:
        self.assertIs(palette_for(ColorScheme.DARK), DARK_PALETTE)

    def test_every_scheme_resolves(self) -> None:
        # A ColorScheme member added without a palette must fail loudly
        # here rather than silently fall back to the light set.
        for scheme in ColorScheme:
            with self.subTest(scheme=scheme):
                self.assertIsInstance(palette_for(scheme), Palette)


class PaletteCompletenessTests(unittest.TestCase):
    """The guard that replaces a construction-time guarantee.

    ``Palette`` groups its per-kind colours in enum-keyed mappings, which
    — unlike fixed fields — can be short a member and only fail at
    lookup time, in front of a user. These tests are what stand in that
    gap, so they are load-bearing rather than garnish.
    """

    def test_every_admonition_kind_has_a_tint(self) -> None:
        for label, palette in _ALL_PALETTES:
            for kind in AdmonitionKind:
                with self.subTest(palette=label, kind=kind):
                    self.assertIn(kind, palette.admonition_tints)

    def test_every_admonition_kind_has_a_label_foreground(self) -> None:
        for label, palette in _ALL_PALETTES:
            for kind in AdmonitionKind:
                with self.subTest(palette=label, kind=kind):
                    self.assertIn(kind, palette.admonition_kind_foregrounds)

    def test_every_error_notice_line_has_a_foreground(self) -> None:
        for label, palette in _ALL_PALETTES:
            for line in UnreadMarkPart:
                with self.subTest(palette=label, line=line):
                    self.assertIn(line, palette.unread_foregrounds)

    def test_no_mapping_carries_an_unknown_key(self) -> None:
        # The mirror of the above: a stale key left behind by a renamed
        # enum member would otherwise sit unnoticed.
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertEqual(
                    set(palette.admonition_tints), set(AdmonitionKind),
                )
                self.assertEqual(
                    set(palette.admonition_kind_foregrounds),
                    set(AdmonitionKind),
                )
                self.assertEqual(
                    set(palette.unread_foregrounds),
                    set(UnreadMarkPart),
                )


class SheetTests(unittest.TestCase):
    def test_every_sheet_is_opaque(self) -> None:
        # The sheet stands in for the page background: a translucent one
        # would let the desk bleed through and change every contrast
        # ratio asserted below.
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertEqual(palette.sheet[_ALPHA_INDEX], _OPAQUE_ALPHA)

    def test_dark_sheet_is_darker_than_light_sheet(self) -> None:
        self.assertLess(
            relative_luminance(*DARK_PALETTE.sheet[:3]),
            relative_luminance(*LIGHT_PALETTE.sheet[:3]),
        )

    def test_dark_sheet_is_not_pure_black(self) -> None:
        # It has to sit a step lighter than a dark theme's window
        # background so the note still reads as a page on a desk rather
        # than a hole cut in one.
        self.assertGreater(relative_luminance(*DARK_PALETTE.sheet[:3]), 0.0)

    def test_dark_body_ink_is_not_pure_white(self) -> None:
        # Full-strength white on a dark ground haloes and tires the eye
        # over a long note.
        red, green, blue = _parse_hex_color(DARK_PALETTE.body_foreground)
        self.assertLess(relative_luminance(red, green, blue), 1.0)


class ContrastTests(unittest.TestCase):
    """Every foreground must be legible on its own palette's sheet.

    This is the assertion that makes the sheet safe to hard-code: the
    contrast that decides legibility is ink-against-*sheet*, and the
    palette owns both sides of it. Ink against the *desk* is never
    measured here because the desk is the theme's, not ours.
    """

    def test_body_foreground_clears_the_text_floor(self) -> None:
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertGreaterEqual(
                    _contrast_ratio(palette.body_foreground, palette.sheet),
                    _MIN_TEXT_CONTRAST_RATIO,
                )

    def test_link_foreground_clears_the_text_floor(self) -> None:
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertGreaterEqual(
                    _contrast_ratio(palette.link_foreground, palette.sheet),
                    _MIN_TEXT_CONTRAST_RATIO,
                )

    def test_admonition_kind_foregrounds_clear_the_text_floor(self) -> None:
        for label, palette in _ALL_PALETTES:
            for kind in AdmonitionKind:
                with self.subTest(palette=label, kind=kind):
                    self.assertGreaterEqual(
                        _contrast_ratio(
                            palette.admonition_kind_foregrounds[kind],
                            palette.sheet,
                        ),
                        _MIN_TEXT_CONTRAST_RATIO,
                    )

    def test_unread_source_clears_the_text_floor(self) -> None:
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertGreaterEqual(
                    _contrast_ratio(
                        palette.unread_foregrounds[UnreadMarkPart.SOURCE],
                        palette.sheet,
                    ),
                    _MIN_TEXT_CONTRAST_RATIO,
                )

    def test_unread_reason_clears_the_text_floor(self) -> None:
        # The reason line is the amber the bar carries, wherever contrast
        # allows. On the light sheet the bar's own amber measures 2.4
        # against white, so the light palette drops to a darker stop of
        # the same hue; this is the assertion that forced that split, and
        # the one that would catch a well-meaning "make them match"
        # edit that silently made the reason unreadable.
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertGreaterEqual(
                    _contrast_ratio(
                        palette.unread_foregrounds[UnreadMarkPart.REASON],
                        palette.sheet,
                    ),
                    _MIN_TEXT_CONTRAST_RATIO,
                )

    def test_metadata_foreground_clears_the_secondary_floor(self) -> None:
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertGreaterEqual(
                    _contrast_ratio(
                        palette.metadata_foreground, palette.sheet,
                    ),
                    _MIN_SECONDARY_CONTRAST_RATIO,
                )


class UnreadBarTests(unittest.TestCase):
    """The amber rule painted beside unread source."""

    def test_bar_tint_is_opaque_in_both_palettes(self) -> None:
        # Unlike the block tints (low-alpha washes read *through*), the
        # unread rule is a solid marker: it has to stay legible as a rule
        # rather than blend into the sheet behind it.
        for label, palette in _ALL_PALETTES:
            with self.subTest(palette=label):
                self.assertEqual(palette.unread_bar_tint[3], 1.0)


class RelativeLuminanceTests(unittest.TestCase):
    def test_white_is_full_luminance(self) -> None:
        self.assertAlmostEqual(relative_luminance(1.0, 1.0, 1.0), 1.0)

    def test_black_is_zero_luminance(self) -> None:
        self.assertAlmostEqual(relative_luminance(0.0, 0.0, 0.0), 0.0)

    def test_green_outweighs_blue_at_equal_value(self) -> None:
        # The eye is far more sensitive to green than to blue, which is
        # why a naive channel average would misclassify some themes.
        self.assertGreater(
            relative_luminance(0.0, 1.0, 0.0),
            relative_luminance(0.0, 0.0, 1.0),
        )

    def test_low_channel_uses_the_linear_segment(self) -> None:
        # sRGB is linear below the cutoff; a value there must not go
        # through the gamma branch (which would return a larger number).
        self.assertAlmostEqual(
            relative_luminance(0.02, 0.02, 0.02), 0.02 / 12.92,
        )


class SchemeForForegroundTests(unittest.TestCase):
    """The rule: a *light* text colour means the surface is dark."""

    def test_black_foreground_selects_the_light_scheme(self) -> None:
        # What a default Adwaita theme resolves to (measured).
        self.assertIs(
            scheme_for_foreground(0.0, 0.0, 0.0), ColorScheme.LIGHT,
        )

    def test_white_foreground_selects_the_dark_scheme(self) -> None:
        # What GTK_THEME=Adwaita:dark resolves to (measured).
        self.assertIs(
            scheme_for_foreground(1.0, 1.0, 1.0), ColorScheme.DARK,
        )

    def test_off_white_foreground_selects_the_dark_scheme(self) -> None:
        self.assertIs(
            scheme_for_foreground(0.92, 0.91, 0.89), ColorScheme.DARK,
        )

    def test_dark_grey_foreground_selects_the_light_scheme(self) -> None:
        self.assertIs(
            scheme_for_foreground(0.18, 0.18, 0.17), ColorScheme.LIGHT,
        )

    def test_just_below_the_threshold_selects_the_light_scheme(self) -> None:
        # 0.73 in each channel lands just under 0.5 luminance.
        self.assertIs(
            scheme_for_foreground(0.73, 0.73, 0.73), ColorScheme.LIGHT,
        )

    def test_just_above_the_threshold_selects_the_dark_scheme(self) -> None:
        # 0.74 lands just over it.
        self.assertIs(
            scheme_for_foreground(0.74, 0.74, 0.74), ColorScheme.DARK,
        )


if __name__ == "__main__":
    unittest.main()

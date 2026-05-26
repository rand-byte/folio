"""Tests for :mod:`ui.dialogs`."""

from __future__ import annotations

import unittest
from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gtk  # noqa: E402

from enums import NotebookIcon
from ui.dialogs import (
    IconPickerPopover,
    _NOTEBOOK_ICON_NAMES,
    _PICKER_FALLBACK_ICON_NAME,
    _icon_name_for,
    default_confirm_dialog_presenter,
)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Pure helpers — no GTK display required
# ---------------------------------------------------------------------------


class IconNameMappingTests(unittest.TestCase):
    """The icon-name mapping is exhaustive over the enum."""

    def test_every_enum_member_has_a_mapped_icon_name(self) -> None:
        """Every :class:`NotebookIcon` member must resolve to a non-empty
        FreeDesktop icon name. A future addition to the enum that
        forgets the mapping is caught here, not silently falling
        back at runtime.
        """
        for icon in NotebookIcon:
            with self.subTest(icon=icon):
                self.assertIn(icon, _NOTEBOOK_ICON_NAMES)
                self.assertTrue(_NOTEBOOK_ICON_NAMES[icon])

    def test_lookup_helper_returns_mapped_name_for_known_icon(self) -> None:
        self.assertEqual(
            _icon_name_for(NotebookIcon.HOME),
            _NOTEBOOK_ICON_NAMES[NotebookIcon.HOME],
        )

    def test_fallback_constant_is_a_non_empty_freedesktop_name(self) -> None:
        # The fallback is reachable only via a hypothetical future
        # enum addition that lands without updating the mapping;
        # for now it should at least be a usable icon name.
        self.assertTrue(_PICKER_FALLBACK_ICON_NAME)
        self.assertTrue(_PICKER_FALLBACK_ICON_NAME.endswith("symbolic"))


# ---------------------------------------------------------------------------
# Confirm-dialog presenter — production wrapper
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultConfirmDialogPresenterTests(unittest.TestCase):
    """The production presenter constructs without raising.

    The asynchronous ``Gtk.AlertDialog.choose`` cannot be driven
    inside a unit test without a real main loop, so the deeper
    behaviour of the production presenter is exercised by the
    fake-driven tests inside :mod:`test_toolbar`. These tests pin
    the construction surface only.
    """

    def test_construction_does_not_raise_with_a_parent(self) -> None:
        # The presenter sets ``modal=True`` and calls
        # ``Gtk.AlertDialog.choose``, which maps GTK's internal dialog
        # window synchronously. A modal dialog with no parent maps
        # *without a transient parent* — GTK prints "GtkDialog mapped
        # without a transient parent. This is discouraged." Production
        # always supplies the top-level window (toolbar / sidebar pass
        # the ``MainWindow``), so the test mirrors that by anchoring
        # the dialog to a real toplevel. The window is torn down on
        # cleanup, which also disposes the still-pending dialog.
        captured: list[bool] = []

        def on_result(value: bool) -> None:
            captured.append(value)

        parent = Gtk.Window()
        parent.present()
        self.addCleanup(parent.destroy)

        # Should not raise.
        default_confirm_dialog_presenter(
            parent,
            "Delete \"x\"?",
            "This cannot be undone.",
            "Delete",
            on_result,
        )
        # The result has not arrived yet (no main loop is driven to
        # completion) — that is the expected state for this test.
        self.assertEqual(captured, [])


# ---------------------------------------------------------------------------
# Icon picker popover
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class IconPickerPopoverTests(unittest.TestCase):
    """The picker renders one button per enum member and forwards clicks."""

    def _build_popover(
        self,
        *,
        on_pick: Callable[[NotebookIcon], None] | None = None,
        current_icon: NotebookIcon | None = None,
    ) -> IconPickerPopover:
        # Default callback: discard. Tests that need to observe
        # picks pass their own.
        callback: Callable[[NotebookIcon], None] = (
            on_pick if on_pick is not None else lambda _icon: None
        )
        return IconPickerPopover(
            on_icon_picked=callback,
            current_icon=current_icon,
        )

    def test_one_button_per_enum_member(self) -> None:
        popover = self._build_popover()
        self.assertEqual(
            set(popover.icon_buttons.keys()),
            set(NotebookIcon),
        )

    def test_buttons_are_radio_grouped(self) -> None:
        # When set_group is in effect, activating one button
        # deactivates its peers. We verify by activating two in
        # sequence and asserting only the second is active.
        popover = self._build_popover()
        first_icon, second_icon = list(NotebookIcon)[:2]
        first = popover.icon_buttons[first_icon]
        second = popover.icon_buttons[second_icon]

        first.set_active(True)
        self.assertTrue(first.get_active())
        self.assertFalse(second.get_active())

        second.set_active(True)
        self.assertFalse(first.get_active())
        self.assertTrue(second.get_active())

    def test_current_icon_is_pre_pressed(self) -> None:
        popover = self._build_popover(current_icon=NotebookIcon.MAP)
        self.assertTrue(popover.icon_buttons[NotebookIcon.MAP].get_active())
        # All other buttons stay inactive.
        for icon, button in popover.icon_buttons.items():
            if icon == NotebookIcon.MAP:
                continue
            with self.subTest(icon=icon):
                self.assertFalse(button.get_active())

    def test_no_current_icon_means_no_button_pressed(self) -> None:
        popover = self._build_popover(current_icon=None)
        for icon, button in popover.icon_buttons.items():
            with self.subTest(icon=icon):
                self.assertFalse(button.get_active())

    def test_clicking_a_button_invokes_callback_with_its_icon(self) -> None:
        picked: list[NotebookIcon] = []
        popover = self._build_popover(on_pick=picked.append)

        # ``emit("clicked")`` synthesises the same path the user's
        # click would take, including the popover's
        # ``_on_icon_button_clicked`` handler.
        popover.icon_buttons[NotebookIcon.STAR].emit("clicked")

        self.assertEqual(picked, [NotebookIcon.STAR])

    def test_each_click_fires_the_callback_exactly_once(self) -> None:
        picked: list[NotebookIcon] = []
        popover = self._build_popover(on_pick=picked.append)

        popover.icon_buttons[NotebookIcon.HEART].emit("clicked")
        popover.icon_buttons[NotebookIcon.STAR].emit("clicked")

        self.assertEqual(picked, [NotebookIcon.HEART, NotebookIcon.STAR])


if __name__ == "__main__":
    unittest.main()

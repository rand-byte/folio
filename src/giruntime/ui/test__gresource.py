"""Tests for :mod:`giruntime.ui._gresource`."""

from __future__ import annotations

import unittest

from gi.repository import Gio

from enums import GResourceSubtree
from giruntime.ui import _gresource


class ResourcePathTests(unittest.TestCase):
    """``resource_path`` both registers the bundle and returns a usable path."""

    def test_returns_the_subtree_s_own_value(self) -> None:
        """The returned path is exactly the enum member's value.

        ``resource_path`` is the single source of these strings — a
        caller must not need (or be able) to hardcode its own copy.
        """
        self.assertEqual(
            _gresource.resource_path(GResourceSubtree.ICONS),
            GResourceSubtree.ICONS.value,
        )

    def test_registers_the_bundled_icon_resource(self) -> None:
        """Asking for the icon subtree's path makes the icon SVG resolve.

        This is the property that matters: obtaining a path is what
        triggers registration, so a caller can never end up with a
        path to unregistered content.
        """
        _gresource.resource_path(GResourceSubtree.ICONS)

        found, _size, _flags = Gio.resources_get_info(
            "/org/folio/icons/scalable/apps/io.github.rand_byte.Folio.svg",
            Gio.ResourceLookupFlags.NONE,
        )
        self.assertTrue(found)

    def test_registers_the_bundled_grammar_resource(self) -> None:
        """Asking for the icon subtree's path *also* makes the grammar resolve.

        Both resources ship in the same compiled bundle, so
        registering it while resolving one subtree's path must make
        every subtree reachable — that is the point of sharing one
        registration across independent callers instead of each
        caller registering its own copy.
        """
        _gresource.resource_path(GResourceSubtree.ICONS)

        found, _size, _flags = Gio.resources_get_info(
            "/org/folio/language-specs/language_spec.lang",
            Gio.ResourceLookupFlags.NONE,
        )
        self.assertTrue(found)

    def test_second_call_returns_the_same_path_without_raising(self) -> None:
        """Calling twice (as two independent callers now do) is harmless."""
        first = _gresource.resource_path(GResourceSubtree.LANGUAGE_SPECS)
        second = _gresource.resource_path(GResourceSubtree.LANGUAGE_SPECS)

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

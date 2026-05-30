"""Tests for :mod:`ui.sidebar`.

The pre-tags sidebar tests covered the notebook tree, icon
picker, and notebook-name editing — all of which the
tag-based sidebar removes. The new sidebar's surface (count
of untagged notes, tag-selection plumbing) is covered through
the controller-level tests in
:mod:`controllers.test_app_state` and the pure helpers in
:mod:`ui.sidebar` itself.
"""

from __future__ import annotations

import unittest

from ui.sidebar import count_untagged


class CountUntaggedTests(unittest.TestCase):
    """:func:`count_untagged` is a pure helper — verifiable
    without GTK."""

    def test_empty_list(self) -> None:
        self.assertEqual(count_untagged([]), 0)

    def test_counts_notes_with_empty_tags_tuple(self) -> None:
        class _Stub:  # pylint: disable=too-few-public-methods
            def __init__(self, tags: tuple[str, ...]) -> None:
                self.tags = tags

        notes = [_Stub(()), _Stub(("a",)), _Stub(()), _Stub(("b", "c"))]
        self.assertEqual(count_untagged(notes), 2)

    def test_tags_attribute_optional(self) -> None:
        # Anything without a ``tags`` attribute is treated as
        # untagged — matches the SmartFilter.UNTAGGED predicate.
        notes = [object(), object()]
        self.assertEqual(count_untagged(notes), 2)


if __name__ == "__main__":
    unittest.main()

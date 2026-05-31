"""Tests for :mod:`ui._dates`."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from ui._dates import format_date_long, format_date_short


class FormatDateShortTests(unittest.TestCase):
    """:func:`format_date_short` is ``"<Mon> <day>"`` with no year."""

    def test_basic(self) -> None:
        value = datetime(2026, 4, 14, 9, 30, tzinfo=UTC)
        self.assertEqual(format_date_short(value), "Apr 14")

    def test_january_is_first_month(self) -> None:
        # Guards the ``month - 1`` indexing off-by-one.
        value = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(format_date_short(value), "Jan 1")

    def test_december_is_last_month(self) -> None:
        value = datetime(2026, 12, 31, tzinfo=UTC)
        self.assertEqual(format_date_short(value), "Dec 31")

    def test_no_year_present(self) -> None:
        value = datetime(1999, 5, 26, tzinfo=UTC)
        self.assertNotIn("1999", format_date_short(value))


class FormatDateLongTests(unittest.TestCase):
    """:func:`format_date_long` adds ``, <year>`` to the short form."""

    def test_basic(self) -> None:
        value = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
        self.assertEqual(format_date_long(value), "May 26, 2026")

    def test_january_is_first_month(self) -> None:
        value = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(format_date_long(value), "Jan 1, 2026")

    def test_december_is_last_month(self) -> None:
        value = datetime(2030, 12, 31, tzinfo=UTC)
        self.assertEqual(format_date_long(value), "Dec 31, 2030")

    def test_long_is_short_plus_year(self) -> None:
        # The long form must agree with the short form on month/day so a
        # change to the month table fans out to both consistently.
        value = datetime(2026, 4, 14, tzinfo=UTC)
        self.assertTrue(
            format_date_long(value).startswith(format_date_short(value)),
        )
        self.assertTrue(format_date_long(value).endswith(", 2026"))


if __name__ == "__main__":
    unittest.main()

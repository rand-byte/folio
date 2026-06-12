"""Tests for :mod:`ui._filesize`."""

from __future__ import annotations

import unittest

from config.defaults import MAX_ATTACHMENT_BYTES
from giruntime.ui._filesize import format_byte_size


class FormatByteSizeBytesRangeTests(unittest.TestCase):
    """Sizes under one kilobyte render as plain bytes."""

    def test_zero(self) -> None:
        self.assertEqual(format_byte_size(0), "0 B")

    def test_one_byte(self) -> None:
        self.assertEqual(format_byte_size(1), "1 B")

    def test_just_under_one_kilobyte(self) -> None:
        self.assertEqual(format_byte_size(1023), "1023 B")


class FormatByteSizeKilobyteRangeTests(unittest.TestCase):
    def test_exactly_one_kilobyte(self) -> None:
        # 1.0 KB trims the trailing ".0" — the mock-up shows "1 KB".
        self.assertEqual(format_byte_size(1024), "1 KB")

    def test_small_value_keeps_one_decimal(self) -> None:
        self.assertEqual(format_byte_size(1536), "1.5 KB")

    def test_decimal_rounds_to_one_place(self) -> None:
        # 2.34… KB rounds to 2.3 KB.
        self.assertEqual(format_byte_size(2400), "2.3 KB")

    def test_value_at_ten_or_above_drops_the_decimal(self) -> None:
        self.assertEqual(format_byte_size(180 * 1024), "180 KB")

    def test_rounding_just_below_ten_promotes_to_integer_form(self) -> None:
        # 9.96 KB rounds (to one decimal) to 10.0, which renders as the
        # integer form rather than "10.0 KB".
        self.assertEqual(format_byte_size(10199), "10 KB")

    def test_just_under_one_megabyte_stays_in_kilobytes(self) -> None:
        self.assertEqual(format_byte_size(1024 * 1024 - 1), "1024 KB")


class FormatByteSizeMegabyteRangeTests(unittest.TestCase):
    def test_exactly_one_megabyte(self) -> None:
        self.assertEqual(format_byte_size(1024 * 1024), "1 MB")

    def test_mockup_two_point_three_megabytes(self) -> None:
        # 2.3 MB — the design mock-up's largest example.
        self.assertEqual(format_byte_size(int(2.3 * 1024 * 1024)), "2.3 MB")

    def test_attachment_cap_reads_as_ten_megabytes(self) -> None:
        # The unit convention (1024) is what makes the project's quota
        # constant read back as the documented "10 MB" limit.
        self.assertEqual(format_byte_size(MAX_ATTACHMENT_BYTES), "10 MB")


class FormatByteSizeGigabyteRangeTests(unittest.TestCase):
    def test_exactly_one_gigabyte(self) -> None:
        self.assertEqual(format_byte_size(1024 ** 3), "1 GB")

    def test_values_beyond_the_table_stay_in_gigabytes(self) -> None:
        # GB is the last unit; a terabyte-scale count is still
        # expressed in (many) gigabytes rather than failing.
        self.assertEqual(format_byte_size(2048 * 1024 ** 3), "2048 GB")


class FormatByteSizeErrorTests(unittest.TestCase):
    def test_negative_count_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            format_byte_size(-1)


if __name__ == "__main__":
    unittest.main()

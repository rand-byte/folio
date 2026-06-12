"""Human-readable byte-size formatting shared by attachment widgets.

Principles & invariants
-----------------------
* This module is the single home of the app's human-facing byte-size
  formatting, mirroring the :mod:`ui._dates` sibling-helper pattern:
  a private, pure helper imported by widgets rather than duplicated
  across them.
* The unit convention is **binary** — 1 KB = 1024 bytes, 1 MB =
  1024 KB, and so on — matching the project's quota constant
  (``MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024`` reads as "10 MB").
  The labels use the conventional short forms (``KB`` / ``MB`` /
  ``GB``) rather than the IEC ``KiB`` spellings, matching the
  design mock-up (``1 KB``, ``180 KB``, ``2.3 MB``).
* Scaled values below 10 keep one decimal place (``2.3 MB``) so small
  sizes stay informative; a trailing ``.0`` is trimmed (``1 KB``, not
  ``1.0 KB``); values of 10 and above round to a whole number
  (``180 KB``). Sizes under 1 KB render as plain bytes (``512 B``).
* Pure function — no GTK, no clock, no locale. A negative byte count
  is a programming error and raises :class:`ValueError` rather than
  formatting nonsense.
"""

from __future__ import annotations

from typing import Final

_BYTES_PER_STEP: Final[int] = 1024
"""Binary scaling factor between adjacent units (1 KB = 1024 B)."""

_SCALED_UNIT_LABELS: Final[tuple[str, ...]] = ("KB", "MB", "GB")
"""Unit labels in ascending order, applied after each 1024 division.

A formatting table in the spirit of ``_dates._MONTH_ABBREVIATIONS``:
the single source of unit spelling for every size the UI renders.
``GB`` is the ceiling — attachment sizes are capped far below a
terabyte, so values that large simply stay expressed in gigabytes.
"""

_ONE_DECIMAL_CEILING: Final[int] = 10
"""Scaled values below this keep one decimal place; at or above it
they round to a whole number (``2.3 MB`` vs ``180 KB``)."""


def format_byte_size(byte_count: int) -> str:
    """Return ``byte_count`` as a human-readable string like ``2.3 MB``.

    Examples: ``0`` → ``"0 B"``, ``1024`` → ``"1 KB"``,
    ``1536`` → ``"1.5 KB"``, ``184320`` → ``"180 KB"``,
    ``2411724`` → ``"2.3 MB"``. Raises :class:`ValueError` for a
    negative count.
    """
    if byte_count < 0:
        raise ValueError(f"byte_count must be non-negative, got {byte_count}")
    if byte_count < _BYTES_PER_STEP:
        return f"{byte_count} B"

    value = float(byte_count)
    unit = _SCALED_UNIT_LABELS[0]
    for unit in _SCALED_UNIT_LABELS:
        value /= _BYTES_PER_STEP
        if value < _BYTES_PER_STEP:
            break

    if value >= _ONE_DECIMAL_CEILING:
        return f"{round(value)} {unit}"
    scaled = round(value, 1)
    if scaled == int(scaled):
        return f"{int(scaled)} {unit}"
    return f"{scaled:.1f} {unit}"

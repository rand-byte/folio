"""Tests for :mod:`giruntime.ui._parse_error_messages`."""

from __future__ import annotations

import unittest

from config.defaults import MAX_INLINE_DEPTH
from enums import ParseErrorKind
from giruntime.ui._parse_error_messages import _message_for


class MessageForTests(unittest.TestCase):
    """Pin the user-facing message helper used by the parse-error
    notice. Exhaustiveness over :class:`ParseErrorKind` is enforced
    so a new error kind cannot ship without a notice message.
    """

    def test_every_parse_error_kind_has_a_message(self) -> None:
        # Iterating the enum is what makes this an exhaustiveness
        # check — a member with no entry in ``_message_for`` would
        # raise on the ``match`` (pattern-match exhaustiveness via
        # the missing case at runtime is by design here, since
        # Python doesn't enforce exhaustiveness at type-check time
        # for non-Literal enums without external tooling).
        for kind in ParseErrorKind:
            with self.subTest(kind=kind):
                message = _message_for(kind, 42)
                self.assertIsInstance(message, str)
                self.assertTrue(message)
                # The line number must appear in the message — the
                # notice is the only context the user has, so the
                # location has to be visible.
                self.assertIn("42", message)

    def test_unsupported_link_scheme_message_lists_supported_schemes(self) -> None:
        # This message must name the schemes the user *can* use, so
        # pin its content explicitly.
        message = _message_for(ParseErrorKind.UNSUPPORTED_LINK_SCHEME, 39)
        self.assertIn("39", message)
        for scheme in ("http", "https", "mailto"):
            self.assertIn(scheme, message)

    def test_message_does_not_leak_internal_message(self) -> None:
        # Smoke check: the developer-oriented strings (square
        # brackets around `cols=` or specific quotes) don't leak
        # into the user-facing copy. The notice is consumer copy,
        # not a developer dump.
        message = _message_for(ParseErrorKind.BAD_COLS_DIRECTIVE, 7)
        self.assertNotIn("'", message)

    def test_inline_nesting_message_states_the_configured_cap(self) -> None:
        # The copy interpolates MAX_INLINE_DEPTH rather than spelling
        # the number out, so retuning the cap cannot leave the notice
        # quoting a figure the parser no longer enforces.
        message = _message_for(ParseErrorKind.INLINE_NESTING_TOO_DEEP, 12)
        self.assertIn("Line 12", message)
        self.assertIn(str(MAX_INLINE_DEPTH), message)

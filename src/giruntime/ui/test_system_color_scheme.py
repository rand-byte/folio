"""Tests for :mod:`giruntime.ui.system_color_scheme`.

No portal and no display: the policy — which wire value means what, and
which preference produces which instruction to GTK — is pure, and the
watcher takes its one effect as an injected setter. What is left
untested here is the D-Bus plumbing itself, which needs a running
``xdg-desktop-portal`` to exercise; that is checked by hand against a
real desktop.
"""

from __future__ import annotations

import unittest

from gi.repository import Gio, GLib

from enums import DesktopColorSchemePreference
from giruntime.ui.system_color_scheme import (
    SystemColorSchemeWatcher,
    _wire_value_from,
    preference_for_wire_value,
    prefers_dark,
)


class _RecordingSetter:
    """Captures every boolean the watcher hands to the toolkit.

    Records the whole sequence rather than the latest value, because
    the behaviour that matters most is that a *second* preference
    produces a *second* write — a watcher that only ever set the flag
    to ``True`` would pass any assertion that looked only at the end
    state after going dark.
    """

    applied: list[bool]

    def __init__(self) -> None:
        self.applied = []

    def __call__(self, prefer_dark: bool) -> None:
        self.applied.append(prefer_dark)


class PreferenceForWireValueTests(unittest.TestCase):
    def test_zero_is_no_preference(self) -> None:
        self.assertIs(
            preference_for_wire_value(0),
            DesktopColorSchemePreference.NO_PREFERENCE,
        )

    def test_one_is_prefer_dark(self) -> None:
        # The value GNOME's Dark Style switch reports.
        self.assertIs(
            preference_for_wire_value(1),
            DesktopColorSchemePreference.PREFER_DARK,
        )

    def test_two_is_prefer_light(self) -> None:
        self.assertIs(
            preference_for_wire_value(2),
            DesktopColorSchemePreference.PREFER_LIGHT,
        )

    def test_an_unknown_value_degrades_to_no_preference(self) -> None:
        # The portal specification reserves further values and requires
        # clients to degrade this way. A desktop that grows a third
        # mode must not take the application down with it.
        self.assertIs(
            preference_for_wire_value(3),
            DesktopColorSchemePreference.NO_PREFERENCE,
        )

    def test_every_preference_round_trips_through_its_wire_value(self) -> None:
        # The enum's values are the portal's, so a renumbering would
        # silently mistranslate every preference.
        for preference in DesktopColorSchemePreference:
            with self.subTest(preference=preference):
                self.assertIs(
                    preference_for_wire_value(preference.value), preference,
                )


class PrefersDarkTests(unittest.TestCase):
    def test_prefer_dark_asks_for_dark(self) -> None:
        self.assertTrue(
            prefers_dark(DesktopColorSchemePreference.PREFER_DARK)
        )

    def test_prefer_light_does_not(self) -> None:
        self.assertFalse(
            prefers_dark(DesktopColorSchemePreference.PREFER_LIGHT)
        )

    def test_no_preference_does_not(self) -> None:
        # Distinct from PREFER_LIGHT as a preference, identical as an
        # instruction: "has not asked for dark" is not a reason to give
        # them dark.
        self.assertFalse(
            prefers_dark(DesktopColorSchemePreference.NO_PREFERENCE)
        )


class ApplyPreferenceTests(unittest.TestCase):
    """The watcher's one effect, driven without a portal."""

    setter: _RecordingSetter
    watcher: SystemColorSchemeWatcher

    def setUp(self) -> None:
        self.setter = _RecordingSetter()
        self.watcher = SystemColorSchemeWatcher(self.setter, _no_portal)

    def test_dark_preference_applies_true(self) -> None:
        self.watcher.apply_preference(
            DesktopColorSchemePreference.PREFER_DARK
        )
        self.assertEqual(self.setter.applied, [True])

    def test_light_preference_applies_false(self) -> None:
        self.watcher.apply_preference(
            DesktopColorSchemePreference.PREFER_LIGHT
        )
        self.assertEqual(self.setter.applied, [False])

    def test_switching_back_to_light_applies_false(self) -> None:
        # The bug this test exists for: GTK treats an application's
        # write as an override of what it read from the environment, so
        # a watcher that only ever wrote True would leave the app stuck
        # dark for the rest of the session.
        self.watcher.apply_preference(
            DesktopColorSchemePreference.PREFER_DARK
        )
        self.watcher.apply_preference(
            DesktopColorSchemePreference.PREFER_LIGHT
        )
        self.assertEqual(self.setter.applied, [True, False])

    def test_clearing_the_preference_applies_false(self) -> None:
        # Turning the desktop switch back to "default" is a return to
        # light, not a reason to stay dark.
        self.watcher.apply_preference(
            DesktopColorSchemePreference.PREFER_DARK
        )
        self.watcher.apply_preference(
            DesktopColorSchemePreference.NO_PREFERENCE
        )
        self.assertEqual(self.setter.applied, [True, False])

    def test_every_preference_produces_exactly_one_write(self) -> None:
        for preference in DesktopColorSchemePreference:
            with self.subTest(preference=preference):
                setter = _RecordingSetter()
                SystemColorSchemeWatcher(
                    setter, _no_portal,
                ).apply_preference(preference)
                self.assertEqual(len(setter.applied), 1)


def _no_portal() -> None:
    """A :data:`PortalConnector` for a machine with no settings portal."""
    return None


class _SilentProxy:
    """A proxy for a bus name nobody owns: connects, never answers.

    Stands in for the real gap — ``Gio.DBusProxy`` construction
    succeeds for an unowned name, so "we have a proxy" does not mean
    "there is a portal".
    """

    connected: list[str]

    def __init__(self) -> None:
        self.connected = []

    def connect(self, detail: str, _handler: object) -> int:
        """Record the change subscription :meth:`start` installs."""
        self.connected.append(detail)
        return 1

    def call_sync(
        self,
        _method: str,
        _arguments: GLib.Variant,
        _flags: Gio.DBusCallFlags,
        _timeout_msec: int,
        _cancellable: Gio.Cancellable | None,
    ) -> GLib.Variant:
        raise GLib.Error("no such interface")


class WireValueFromReplyTests(unittest.TestCase):
    """Unpacking the portal's reply, against real ``GLib.Variant`` values.

    These exist because the first version of this module hand-unwrapped
    the reply with ``result[0]`` and ``get_type_string()``, which raised
    ``AttributeError`` against a live portal: PyGObject's indexing
    already unpacks, so the code was handed an ``int`` and asked it for
    variant methods. Nothing that mocked the D-Bus layer would have
    caught it — the bug was in the assumption about the reply's shape,
    so the fix is tested against reply values built the way the portal
    builds them.
    """

    def test_reads_the_single_wrapped_shape(self) -> None:
        # What ReadOne returns: the value in one variant.
        reply = GLib.Variant("(v)", (GLib.Variant("u", 1),))
        self.assertEqual(_wire_value_from(reply), 1)

    def test_reads_the_double_wrapped_shape(self) -> None:
        # What the older Read returns: the value in two.
        reply = GLib.Variant(
            "(v)", (GLib.Variant("v", GLib.Variant("u", 1)),),
        )
        self.assertEqual(_wire_value_from(reply), 1)

    def test_reads_a_light_preference(self) -> None:
        reply = GLib.Variant("(v)", (GLib.Variant("u", 2),))
        self.assertEqual(_wire_value_from(reply), 2)

    def test_a_non_integer_reply_reads_as_none(self) -> None:
        # The portal declares this key as `u`; a string means a portal
        # that is not speaking the protocol, which must read as "said
        # nothing" rather than as a preference for light.
        reply = GLib.Variant("(v)", (GLib.Variant("s", "prefer-dark"),))
        self.assertIsNone(_wire_value_from(reply))


class ReadPreferenceTests(unittest.TestCase):
    def test_a_portal_that_does_not_answer_reads_as_none(self) -> None:
        # Not NO_PREFERENCE: silence is not an opinion, and flattening
        # it into one would write "light" over a dark variant the user
        # selected by another route.
        self.assertIsNone(
            SystemColorSchemeWatcher._read_preference(_SilentProxy()),
        )


class StartWithoutAPortalTests(unittest.TestCase):
    """A box with no portal keeps the pre-existing behaviour.

    Absence is *injected*, never inferred from the machine running the
    suite. An earlier version of this test simply called ``start()`` and
    asserted nothing was written, which passed only because the
    container it was written in had no session bus — on any real desktop
    the portal answered and the assertion failed. What is under test is
    this module's response to a missing portal, not the tester's
    environment.
    """

    def test_start_without_a_portal_touches_nothing(self) -> None:
        # Not merely "does not raise": writing a default would override
        # a dark GTK theme the user had set by other means, which the
        # article view can still detect on its own.
        setter = _RecordingSetter()
        SystemColorSchemeWatcher(setter, _no_portal).start()
        self.assertEqual(setter.applied, [])

    def test_start_with_a_silent_portal_touches_nothing(self) -> None:
        # Connected but unanswering: a proxy can be built for a bus name
        # nobody owns, so this is a distinct path from having no portal.
        setter = _RecordingSetter()
        SystemColorSchemeWatcher(setter, _SilentProxy).start()
        self.assertEqual(setter.applied, [])

    def test_a_silent_portal_is_still_subscribed_to(self) -> None:
        # It may start answering later — a portal that appears after the
        # app has launched should still be able to flip the theme.
        proxy = _SilentProxy()
        SystemColorSchemeWatcher(_RecordingSetter(), lambda: proxy).start()
        self.assertEqual(proxy.connected, ["g-signal"])


if __name__ == "__main__":
    unittest.main()

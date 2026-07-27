"""Follow the desktop's dark/light preference, which GTK 4 does not.

Principles & invariants
-----------------------
* **The problem this exists to solve.** GNOME's "Dark Style" switch sets
  ``org.gnome.desktop.interface color-scheme`` to ``prefer-dark`` and
  leaves ``gtk-theme`` at ``Adwaita``. Plain GTK 4 does not act on that
  key — libadwaita does, which is why a GNOME-shipped terminal follows
  the switch and this application, before this module, did not. Nothing
  about the resolved theme changed, so there was nothing for
  :class:`ArticleTextView`'s luminance probe to notice: the chrome
  stayed light and the note stayed light with it, correctly and
  uselessly.
* **This module supplies the missing signal, and nothing else.** It
  reads the preference and hands it to GTK; GTK restyles the chrome;
  the restyle changes the foreground the article view resolves; the
  view's existing ``css_changed`` probe re-themes the note. Each step is
  something that already worked — the chain simply had no first link.
* **The preference is read, never written.** The portal call is
  read-only and the change subscription is a listener. The application
  does not modify the user's desktop settings, and this module holds no
  ``Gio.Settings`` at all.
* **The GTK setting it writes is process-local.** Writing
  ``Gtk.Settings:gtk-application-prefer-dark-theme`` styles *this
  application's* widgets. It does not reach dconf, ``settings.ini``, or
  any other process, and does not survive a restart. It is the
  supported lever for a plain GTK 4 application; a port to libadwaita
  would replace it with ``AdwStyleManager:color-scheme``, which is the
  one thing that would make this module wrong rather than merely
  redundant.
* **Both directions, always.** GTK treats an application's write as an
  override of what it read from the environment, so a watcher that only
  ever set the flag to :data:`True` would leave the application stuck
  dark for the rest of the session. Every preference maps to an
  explicit boolean and every change applies one.
* **The portal is the only source.** No ``org.gnome.desktop.interface``
  fallback: it would be GNOME-only, and ``Gio.Settings.new`` aborts the
  process when a schema is missing, which is a poor trade for a
  cosmetic preference. When the portal is unreachable — no
  ``xdg-desktop-portal``, no session bus — the application keeps its
  previous behaviour and follows the GTK theme alone (``GTK_THEME``, a
  ``settings.ini``, a dark ``gtk-theme``), all of which the article
  view's probe still detects on its own.
* **The policy is pure; only the plumbing needs a bus.** Mapping a wire
  value to a preference, and a preference to a boolean, are free
  functions with no GTK and no D-Bus, so the behaviour that matters is
  testable without either.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gio, GLib

from enums import DesktopColorSchemePreference


type PreferDarkSetter = Callable[[bool], None]
"""Apply "should this application style itself dark?" to the toolkit.

Injected rather than reaching for :meth:`Gtk.Settings.get_default`
inside the watcher, so the decision path is testable without a display
and the one line that touches GTK's global state stays visible at the
call site in :mod:`giruntime.ui.application`.
"""


_PORTAL_BUS_NAME: str = "org.freedesktop.portal.Desktop"
_PORTAL_OBJECT_PATH: str = "/org/freedesktop/portal/desktop"
_PORTAL_SETTINGS_INTERFACE: str = "org.freedesktop.portal.Settings"

_APPEARANCE_NAMESPACE: str = "org.freedesktop.appearance"
_COLOR_SCHEME_KEY: str = "color-scheme"

_READ_ONE_METHOD: str = "ReadOne"
_READ_METHOD: str = "Read"
"""The two spellings of the portal's read call.

``ReadOne`` arrived with version 2 of the interface and returns the
value directly; the older ``Read`` returns it wrapped in a second
variant. Trying the new name first and falling back is three lines and
covers portals older than the one on the development box — cheaper than
version-negotiating, and the unwrapping helper handles either shape.
"""

_SETTING_CHANGED_SIGNAL: str = "SettingChanged"
_DBUS_SIGNAL_DETAIL: str = "g-signal"


_WIRE_VALUE_TO_PREFERENCE: dict[int, DesktopColorSchemePreference] = {
    preference.value: preference for preference in DesktopColorSchemePreference
}


def preference_for_wire_value(value: int) -> DesktopColorSchemePreference:
    """Map a portal ``color-scheme`` value to a preference.

    Unknown values resolve to
    :attr:`DesktopColorSchemePreference.NO_PREFERENCE` rather than
    raising. This is the one place in the codebase that deliberately
    accepts an out-of-range input, and the reason is that the input is
    not ours: the portal specification reserves further values and
    requires clients to treat anything unrecognised as "no preference",
    so a desktop that grows a third mode must not take the application
    down with it.
    """
    return _WIRE_VALUE_TO_PREFERENCE.get(
        value, DesktopColorSchemePreference.NO_PREFERENCE,
    )


def prefers_dark(preference: DesktopColorSchemePreference) -> bool:
    """Should the application style itself dark for ``preference``?

    :attr:`NO_PREFERENCE` answers :data:`False`, the same as an explicit
    :attr:`PREFER_LIGHT`. The two are distinct *preferences* but the
    same *instruction*: GTK offers one boolean, and "the user has not
    asked for dark" is not a reason to give them dark.
    """
    return preference is DesktopColorSchemePreference.PREFER_DARK


class SystemColorSchemeWatcher:
    """Applies the desktop's colour-scheme preference, and keeps it applied.

    Constructed with the setter that carries the decision to GTK, then
    :meth:`start`ed once during application startup. If the portal is
    unreachable, :meth:`start` leaves the toolkit untouched and the
    watcher inert — the application then behaves exactly as it did
    before this module existed.
    """

    _apply_prefer_dark: PreferDarkSetter
    _proxy: Gio.DBusProxy | None

    def __init__(self, apply_prefer_dark: PreferDarkSetter) -> None:
        self._apply_prefer_dark = apply_prefer_dark
        self._proxy = None

    def start(self) -> None:
        """Read the current preference, apply it, and subscribe to changes.

        Applying *before* subscribing is deliberate: the preference is
        already set when the application launches, and a listener alone
        would only notice the user toggling it afterwards — the app
        would start light in a dark session and correct itself only on
        the next change.
        """
        proxy = self._connect()
        if proxy is None:
            return
        self._proxy = proxy
        proxy.connect(_DBUS_SIGNAL_DETAIL, self._on_dbus_signal)
        preference = self._read_preference(proxy)
        if preference is None:
            # The portal did not answer — which is *not* the same as it
            # answering "no preference". Applying a default here would
            # write ``False`` over a dark variant the user had selected
            # by another route (a ``settings.ini``), turning a silent
            # non-feature into a visible regression. Say nothing, and
            # leave the subscription in place in case the portal starts
            # answering later.
            return
        self.apply_preference(preference)

    def apply_preference(
        self, preference: DesktopColorSchemePreference,
    ) -> None:
        """Hand one preference to the toolkit, in whichever direction.

        The seam the tests drive, so the "switch back to light must
        actually switch back" behaviour can be pinned without a portal.
        """
        self._apply_prefer_dark(prefers_dark(preference))

    @staticmethod
    def _connect() -> Gio.DBusProxy | None:
        """Return a proxy for the portal's settings interface, or ``None``.

        A missing portal or an unavailable session bus is an ordinary
        environment, not an error: a headless test box, a minimal WM, a
        container. Only :class:`GLib.Error` is caught — a failure to
        *use* a proxy that did connect is a real bug and is left to
        propagate.
        """
        try:
            return Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                _PORTAL_BUS_NAME,
                _PORTAL_OBJECT_PATH,
                _PORTAL_SETTINGS_INTERFACE,
                None,
            )
        except GLib.Error:
            return None

    @staticmethod
    def _read_preference(
        proxy: Gio.DBusProxy,
    ) -> DesktopColorSchemePreference | None:
        """Read ``color-scheme`` from the portal, or ``None`` if it won't say.

        The :data:`None` is load-bearing and distinct from
        :attr:`NO_PREFERENCE`: a proxy can be constructed for a bus name
        nobody owns, so "connected" does not imply "answered". A desktop
        with no settings portal, or one implementing the interface
        without the appearance namespace, has expressed no opinion —
        which must leave the toolkit alone rather than be flattened into
        an opinion of "light".
        """
        arguments = GLib.Variant(
            "(ss)", (_APPEARANCE_NAMESPACE, _COLOR_SCHEME_KEY),
        )
        for method in (_READ_ONE_METHOD, _READ_METHOD):
            try:
                result = proxy.call_sync(
                    method, arguments, Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.Error:
                continue
            wire_value = _wire_value_from(result)
            if wire_value is None:
                return None
            return preference_for_wire_value(wire_value)
        return None

    def _on_dbus_signal(
        self,
        _proxy: Gio.DBusProxy,
        _sender_name: str | None,
        signal_name: str,
        parameters: GLib.Variant,
    ) -> None:
        """Apply a live change to the appearance colour scheme.

        The proxy relays every signal the interface emits, so this
        filters by name *and* by which setting changed — a portal that
        also reports, say, an accent-colour change must not be read as a
        colour-scheme change.
        """
        if signal_name != _SETTING_CHANGED_SIGNAL:
            return
        namespace, key, value = parameters.unpack()
        if namespace != _APPEARANCE_NAMESPACE or key != _COLOR_SCHEME_KEY:
            return
        self.apply_preference(preference_for_wire_value(value))


def _wire_value_from(result: GLib.Variant) -> int | None:
    """Extract the ``color-scheme`` integer from a portal reply.

    Both spellings of the read call return the value boxed in a
    variant — ``ReadOne`` in one, the older ``Read`` in two —
    so the nesting depth is not fixed. :meth:`GLib.Variant.unpack`
    descends through every layer of that boxing, which makes the two
    shapes indistinguishable here and removes the need to know which
    method answered.

    Returns :data:`None` for anything that is not an integer. The
    portal declares this key as ``u``, so a non-integer is a portal
    that is not speaking the protocol; treating it as "said nothing"
    keeps a malformed reply from being read as a preference for light.
    """
    value = result.unpack()[0]
    if isinstance(value, int):
        return value
    return None

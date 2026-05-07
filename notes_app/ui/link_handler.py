"""Cursor / motion / click handling for link tags in the rendered view.

Principles & invariants
-----------------------
* This module is the bridge between the renderer's per-link URL tags
  and the system's URL launcher. Every link the user sees in the
  rendered :class:`Gtk.TextView` flows through one
  :class:`LinkHandler` instance.
* Link *identity* (which URL each link points at) lives on per-link
  anonymous tags managed by :class:`TextBufferRenderer` and recovered
  via :meth:`TextBufferRenderer.url_for_tags`. This module is the sole
  caller of that method outside of the renderer's own tests.
* :meth:`LinkHandler.install` attaches two GTK 4 event controllers to
  the controlled :class:`Gtk.TextView`:

  * :class:`Gtk.EventControllerMotion` — drives cursor switching
    (``pointer`` over a link, default elsewhere). The ``leave``
    callback resets the cursor when the pointer exits the view so it
    never gets stuck in the link state.
  * :class:`Gtk.GestureClick` — bound to ``Gdk.BUTTON_PRIMARY`` and
    listens to ``released`` (rather than ``pressed``) so the click
    behaves like every other clickable thing in the platform: the
    launch fires only if the press *and* release land on the same
    link.

* The URI launcher is built through an injected factory so tests can
  pass a fake. Production wires the factory to
  :func:`Gtk.UriLauncher.new`, whose ``launch(parent, cancellable,
  callback)`` hands the URL off to the OS. Only schemes in
  :class:`LinkScheme` ever reach this module — the parser's
  scheme allow-list is the security boundary; the launcher trusts
  whatever URL the renderer gives it.
* No hover preview in v1.
* Cursor toggles are guarded with an inequality check so a steady
  hover does not repeatedly call :meth:`Gtk.Widget.set_cursor`. The
  internal :attr:`_showing_link_cursor` flag is the source of truth
  for "what cursor is currently displayed".
* This module imports ``gi`` because it is, at its core, a GTK 4
  controller. Production wiring (in :class:`NoteView`) and tests
  always go through :meth:`install`; the launch and URL-resolution
  helpers stay testable as plain Python methods on the class.
* The URL-resolution pipeline is split into two halves so it stays
  testable without a realised widget: :meth:`_iter_at_widget_coords`
  performs the GTK-side coord-to-iter translation (which requires a
  laid-out view), and :meth:`_url_at_iter` performs the pure
  iter-to-URL lookup via the renderer. The controller callbacks
  compose them. Tests drive :meth:`_url_at_iter` directly with a
  buffer-constructed iter, leaving the coord step out of scope.
* :class:`Gtk.TextView` installs its own cursor (the I-beam over
  text content) before this module is constructed. Restoring "the
  default" therefore means restoring *that* cursor, not blanking
  the view by passing :data:`None` to :meth:`Gtk.Widget.set_cursor`
  — which would erase the I-beam. :class:`LinkHandler` captures the
  cursor at construction time in :attr:`_default_cursor` and
  restores it on link-leave.
* GTK 4 deprecations explicitly avoided: this module uses
  :class:`Gtk.UriLauncher` (not the deprecated :func:`Gtk.show_uri`),
  modern event controllers (not deprecated input-event signals on
  :class:`Gtk.Widget`), and :meth:`Gtk.Widget.add_controller` (the
  current API for attaching controllers).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from notes_app.asciidoc.textbuffer_renderer import TextBufferRenderer


_LINK_CURSOR_NAME: str = "pointer"
"""CSS-style cursor name for the hand/finger cursor shown over links.

``pointer`` is the standard name across GTK / CSS / Wayland cursor
themes for "this is clickable". Using a named cursor (rather than a
custom-rendered one) keeps the appearance consistent with the rest of
the platform — when the user changes their cursor theme the link
cursor follows.
"""


class UriLauncherProtocol(Protocol):
    """The :class:`Gtk.UriLauncher` surface :class:`LinkHandler` uses.

    Defining the surface as a :class:`typing.Protocol` lets test fakes
    substitute for the concrete launcher without inheriting from it.
    The single method matches the signature
    :class:`Gtk.UriLauncher` already exposes, so the production
    factory needs no adapter.

    The ``parent`` argument is the toplevel window that should be
    treated as the launch's parent for transient-modal purposes;
    ``None`` is acceptable when no parent can be resolved.

    The ``callback`` argument is the GIO async-completion callback;
    :class:`LinkHandler` always passes ``None`` (fire-and-forget),
    which is what the plan's policy of "no error UI in v1" calls for.
    """

    def launch(
        self,
        parent: Gtk.Window | None,
        cancellable: Gio.Cancellable | None,
        callback: Callable[..., None] | None,
    ) -> None:
        ...


type UriLauncherFactory = Callable[[str], UriLauncherProtocol]
"""Build a launcher for a given URL.

Production wires this to :func:`default_launcher_factory` (which
delegates to :func:`Gtk.UriLauncher.new`). Tests pass a recording
fake so they can assert on the URL handed in *and* on the subsequent
``launch`` call.

Wrapping the factory rather than the launcher itself is what the
plan's testing guidance calls for: "Inject a fake launcher factory;
assert the URL passed to ``UriLauncher.new`` and that ``launch`` was
called."
"""


def default_launcher_factory(url: str) -> UriLauncherProtocol:
    """The production :data:`UriLauncherFactory`.

    Calls :func:`Gtk.UriLauncher.new` with the URL. The returned
    :class:`Gtk.UriLauncher` already conforms structurally to
    :class:`UriLauncherProtocol`, so no adapter is needed.
    """
    return Gtk.UriLauncher.new(url)


class LinkHandler:
    """Wires a :class:`Gtk.TextView`'s link tags to cursor + launcher.

    Construction does *not* attach event controllers; call
    :meth:`install` to do that. Splitting the steps lets tests
    construct a handler, drive its private helpers directly, and only
    perform the controller wiring in smoke tests where a display is
    available.

    Construction-time arguments:

    * ``text_view`` — the read-only :class:`Gtk.TextView` whose buffer
      the controller targets. The handler reads its iter-at-location
      and writes its cursor.
    * ``renderer`` — the :class:`TextBufferRenderer` whose
      :meth:`url_for_tags` is consulted to resolve a click position
      to a URL. The handler does *not* render; it only reads tags.
    * ``launcher_factory`` — the :data:`UriLauncherFactory` that
      builds a launcher for a URL. Production passes
      :func:`default_launcher_factory`; tests pass a recording fake.
    """

    _text_view: Gtk.TextView
    _renderer: TextBufferRenderer
    _launcher_factory: UriLauncherFactory
    _link_cursor: Gdk.Cursor
    _default_cursor: Gdk.Cursor | None
    _showing_link_cursor: bool

    def __init__(
        self,
        *,
        text_view: Gtk.TextView,
        renderer: TextBufferRenderer,
        launcher_factory: UriLauncherFactory,
    ) -> None:
        self._text_view = text_view
        self._renderer = renderer
        self._launcher_factory = launcher_factory
        # The cursor is built once and reused. ``Gdk.Cursor`` is a
        # cheap value-like object; the same instance can be applied to
        # any number of widgets without sharing state hazards.
        self._link_cursor = Gdk.Cursor.new_from_name(_LINK_CURSOR_NAME, None)
        # Capture whatever cursor the text view already has at
        # construction time. ``Gtk.TextView`` installs its own I-beam
        # cursor over text content, and that is the cursor we want to
        # restore when the pointer leaves a link — *not* :data:`None`,
        # which would erase the I-beam GTK provides for text
        # selection.
        self._default_cursor = text_view.get_cursor()
        self._showing_link_cursor = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def install(self) -> None:
        """Attach motion and click controllers to the text view.

        This is *not* idempotent: a second :meth:`install` would
        attach a second pair of controllers that fire alongside the
        first. The expected lifecycle is one :class:`LinkHandler` per
        :class:`Gtk.TextView` for the lifetime of the view.

        The motion controller is attached for cursor handling; the
        click gesture for URL launching. Both are constructed locally
        and handed to :meth:`Gtk.Widget.add_controller` — that
        transfers ownership to the widget so the handler does not
        need to keep its own references.
        """
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self._text_view.add_controller(motion)

        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        # ``released`` rather than ``pressed`` matches platform
        # convention: a press-then-drag-away cancels the click.
        click.connect("released", self._on_released)
        self._text_view.add_controller(click)

    # ------------------------------------------------------------------
    # URL resolution and activation — testable units
    # ------------------------------------------------------------------

    def _iter_at_widget_coords(
        self,
        widget_x: float,
        widget_y: float,
    ) -> Gtk.TextIter | None:
        """Translate widget-relative coords to a buffer iter.

        Returns :data:`None` when the point is outside the laid-out
        text — :meth:`Gtk.TextView.get_iter_at_location` reports
        ``False`` for that case. An unrealised text view also reports
        ``False`` (the layout has never been computed); production
        only ever invokes this method from controller callbacks, by
        which time the view is realised, so the unrealised case is
        an unreachable defence-in-depth.

        Splitting this from :meth:`_url_at_iter` keeps the coord
        pipeline isolated to one method: tests that don't have a
        realised widget can drive :meth:`_url_at_iter` directly with
        a buffer-constructed iter, leaving the coord step out of
        scope.
        """
        buffer_x, buffer_y = self._text_view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET,
            int(widget_x),
            int(widget_y),
        )
        ok, iterator = self._text_view.get_iter_at_location(buffer_x, buffer_y)
        if not ok:
            return None
        return iterator

    def _url_at_iter(self, iterator: Gtk.TextIter) -> str | None:
        """Look up the URL carried by the link tag covering ``iterator``.

        Pulls the iter's tags via :meth:`Gtk.TextIter.get_tags`, then
        defers to :meth:`TextBufferRenderer.url_for_tags` for the
        per-link URL lookup. Returns :data:`None` when no link tag
        covers the iter — which is the common case (most of the
        document is not a link).

        This is the seam every URL-resolving entry point routes
        through. Both controller callbacks and tests reach the
        renderer through here.
        """
        return self._renderer.url_for_tags(list(iterator.get_tags()))

    def _activate_url(self, url: str | None) -> None:
        """Hand a URL off to the launcher factory and launch it.

        ``url`` is :data:`None` when there is no link at the click
        position; the method is a no-op in that case so callers can
        forward whatever :meth:`_url_at_widget_coords` returned
        without their own ``None``-check. This unifies the click and
        any future programmatic-activation entry points.

        For a real URL, the factory is asked to build a launcher,
        and the launcher is asked to launch immediately. The launch
        is fire-and-forget: ``callback=None`` means we do not
        observe the GIO async result. That matches the plan's
        no-error-toast policy for v1; if a launch fails the OS will
        typically still surface the failure to the user via the
        default URI handler.
        """
        if url is None:
            return
        launcher = self._launcher_factory(url)
        launcher.launch(self._resolve_parent_window(), None, None)

    def _set_cursor_to_link(self, want_link: bool) -> None:
        """Toggle the text view's cursor with idempotency.

        When ``want_link`` is :data:`True` the link cursor is
        applied; otherwise the cursor captured at construction time
        (:attr:`_default_cursor`) is restored. Restoring — rather
        than clearing to :data:`None` — preserves whatever cursor
        :class:`Gtk.TextView` had set for itself: the I-beam over
        text content for selectable read-only views, or a custom
        cursor a future caller might have set before construction.

        Idempotency: if the requested state matches
        :attr:`_showing_link_cursor`, this method does nothing. A
        steady hover over a link therefore makes exactly one
        ``set_cursor`` call across the entire hover, not one per
        motion event.
        """
        if want_link == self._showing_link_cursor:
            return
        self._text_view.set_cursor(
            self._link_cursor if want_link else self._default_cursor
        )
        self._showing_link_cursor = want_link

    def _resolve_parent_window(self) -> Gtk.Window | None:
        """Return the toplevel window owning the text view, if any.

        :meth:`Gtk.UriLauncher.launch` accepts :data:`None`, so an
        un-rooted view (e.g. during construction-time tests) still
        launches; resolving the parent only when present matches
        platform expectations for transient-modal behaviour without
        forcing test code to provide a window.
        """
        root = self._text_view.get_root()
        if isinstance(root, Gtk.Window):
            return root
        return None

    # ------------------------------------------------------------------
    # Controller callbacks — adapters between GTK signals and helpers
    # ------------------------------------------------------------------

    def _on_motion(
        self,
        _controller: Gtk.EventControllerMotion,
        x: float,
        y: float,
    ) -> None:
        """``motion`` callback: keep the cursor in sync with hover.

        The controller delivers widget-relative coordinates. We
        resolve them to a buffer iter, and from there to a URL via
        :meth:`_url_at_iter`. A non-:data:`None` URL means the
        pointer is over a link.

        When the coords resolve to no iter (outside the laid-out
        text), we treat that as "not over a link" — the default
        cursor is the right state.
        """
        iterator = self._iter_at_widget_coords(x, y)
        url = self._url_at_iter(iterator) if iterator is not None else None
        self._set_cursor_to_link(url is not None)

    def _on_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        """``leave`` callback: clear the link cursor unconditionally.

        Without this the cursor would stick in its last in-view
        state after the pointer exits the widget. A leave event
        always resets to the default cursor.
        """
        self._set_cursor_to_link(False)

    def _on_released(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        """``released`` callback: launch a URL if the click was on a link.

        ``_n_press`` is the click count; a double-click is delivered
        as a separate ``released`` with ``_n_press == 2``. Treating
        every release the same (forwarding to :meth:`_activate_url`)
        is the right behaviour: a double-click on a link should
        still launch it, not nothing.
        """
        iterator = self._iter_at_widget_coords(x, y)
        if iterator is None:
            return
        self._activate_url(self._url_at_iter(iterator))

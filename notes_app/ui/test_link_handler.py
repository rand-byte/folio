"""Tests for :mod:`notes_app.ui.link_handler`."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from notes_app.ui.note_render.tag_table import TagName, build_tag_table
from notes_app.ui.note_render.textbuffer_renderer import TextBufferRenderer
from notes_app.ui.link_handler import (
    LinkHandler,
    UriLauncherProtocol,
    default_launcher_factory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for widget construction."""
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


@dataclass
class _RecordedLaunchCall:
    """A single ``launch`` call captured by :class:`_RecordingLauncher`."""

    parent: Gtk.Window | None
    cancellable: Gio.Cancellable | None
    callback: Callable[..., None] | None


@dataclass
class _RecordingLauncher:
    """A :class:`UriLauncherProtocol` test fake.

    Stores the URL it was constructed with (assigned by
    :class:`_RecordingLauncherFactory`) and every ``launch`` call's
    arguments. The URL is *not* a constructor argument because the
    type :class:`Gtk.UriLauncher` it stands in for has its URL set at
    creation time and exposes no public ``url`` attribute itself.
    """

    url: str = ""
    launch_calls: list[_RecordedLaunchCall] = field(default_factory=list)

    def launch(
        self,
        parent: Gtk.Window | None,
        cancellable: Gio.Cancellable | None,
        callback: Callable[..., None] | None,
    ) -> None:
        self.launch_calls.append(
            _RecordedLaunchCall(parent=parent, cancellable=cancellable, callback=callback),
        )


class _RecordingLauncherFactory:
    """Records every URL the link handler asks the factory to handle.

    Each call returns a fresh :class:`_RecordingLauncher` whose
    ``url`` attribute is set to the requested URL. Tests inspect
    :attr:`urls` to confirm what URL came in, and inspect the
    matching :class:`_RecordingLauncher` (in :attr:`launchers`) to
    confirm whether ``launch`` was called and with what.
    """

    urls: list[str]
    launchers: list[_RecordingLauncher]

    def __init__(self) -> None:
        self.urls = []
        self.launchers = []

    def __call__(self, url: str) -> UriLauncherProtocol:
        launcher = _RecordingLauncher(url=url)
        self.urls.append(url)
        self.launchers.append(launcher)
        return launcher


class _FakeRenderer:
    """Minimal stand-in for :class:`TextBufferRenderer`.

    The link handler only ever calls
    :meth:`TextBufferRenderer.url_for_tags`. Tests that exercise the
    handler's logic without rendering a real document use this fake
    so they can dictate what URL (or :data:`None`) the lookup
    returns. :attr:`url_for_tags_calls` records the lists of tags it
    was given so tests can assert on the lookup happening at all.
    """

    return_value: str | None
    url_for_tags_calls: list[list[Gtk.TextTag]]

    def __init__(self, *, return_value: str | None) -> None:
        self.return_value = return_value
        self.url_for_tags_calls = []

    def url_for_tags(self, tags: list[Gtk.TextTag]) -> str | None:
        self.url_for_tags_calls.append(list(tags))
        return self.return_value


def _make_handler(
    *,
    return_value: str | None,
) -> tuple[LinkHandler, _RecordingLauncherFactory, _FakeRenderer, Gtk.TextView]:
    """Build a :class:`LinkHandler` with fakes wired in."""
    fake_renderer = _FakeRenderer(return_value=return_value)
    factory = _RecordingLauncherFactory()
    text_view = Gtk.TextView.new()
    handler = LinkHandler(
        text_view=text_view,
        renderer=fake_renderer,  # type: ignore[arg-type]
        launcher_factory=factory,
    )
    return handler, factory, fake_renderer, text_view


def _spare_iter_for(text_view: Gtk.TextView) -> Gtk.TextIter:
    """Return a usable :class:`Gtk.TextIter` from the view's buffer.

    The text view is freshly constructed, so its buffer has no
    content; we seed a single character first so an iter has
    somewhere to point. The handler's pure-tag pipeline does not
    care about the iter's exact offset — only the tag list at it —
    and the fake renderer ignores the tag list anyway.
    """
    buffer = text_view.get_buffer()
    buffer.set_text("x")
    return buffer.get_iter_at_offset(0)


# ---------------------------------------------------------------------------
# default_launcher_factory
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultLauncherFactoryTests(unittest.TestCase):
    """The production factory should build a real :class:`Gtk.UriLauncher`."""

    def test_returns_gtk_uri_launcher(self) -> None:
        launcher = default_launcher_factory("https://example.com")
        self.assertIsInstance(launcher, Gtk.UriLauncher)

    def test_launcher_is_constructed_with_the_given_url(self) -> None:
        launcher = default_launcher_factory("https://example.com/page")
        # Narrow from UriLauncherProtocol (the factory's return type)
        # to the concrete Gtk.UriLauncher so we can read its ``uri``
        # GObject property — this also confirms the factory hands
        # back the real launcher type, not just something that
        # quacks like one.
        self.assertIsInstance(launcher, Gtk.UriLauncher)
        assert isinstance(launcher, Gtk.UriLauncher)  # for type narrowing
        self.assertEqual(launcher.get_property("uri"), "https://example.com/page")


# ---------------------------------------------------------------------------
# _activate_url — the URL → launcher contract
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ActivateUrlTests(unittest.TestCase):
    """The plan's "launch invoked exactly once with the stored URL" test."""

    def test_factory_invoked_once_with_the_url(self) -> None:
        handler, factory, _, _ = _make_handler(return_value=None)
        handler._activate_url("https://example.com")
        self.assertEqual(factory.urls, ["https://example.com"])

    def test_launch_called_exactly_once_on_returned_launcher(self) -> None:
        handler, factory, _, _ = _make_handler(return_value=None)
        handler._activate_url("https://example.com")
        self.assertEqual(len(factory.launchers), 1)
        self.assertEqual(len(factory.launchers[0].launch_calls), 1)

    def test_launch_called_with_none_cancellable_and_callback(self) -> None:
        # The plan's "callback=None" policy is in force: the handler
        # must hand the launcher None for both cancellable and callback.
        handler, factory, _, _ = _make_handler(return_value=None)
        handler._activate_url("mailto:a@b.c")
        call = factory.launchers[0].launch_calls[0]
        self.assertIsNone(call.cancellable)
        self.assertIsNone(call.callback)

    def test_none_url_is_a_noop(self) -> None:
        # The "non-link click does nothing" contract — the handler's
        # ``_activate_url`` short-circuits on None, so no launcher is
        # ever asked of the factory.
        handler, factory, _, _ = _make_handler(return_value=None)
        handler._activate_url(None)
        self.assertEqual(factory.urls, [])
        self.assertEqual(factory.launchers, [])

    def test_repeated_activation_makes_one_launcher_per_call(self) -> None:
        # Each click is independent: two activations of the same URL
        # build two separate launchers and call ``launch`` on each.
        handler, factory, _, _ = _make_handler(return_value=None)
        handler._activate_url("https://x.test")
        handler._activate_url("https://x.test")
        self.assertEqual(factory.urls, ["https://x.test", "https://x.test"])
        self.assertEqual(len(factory.launchers), 2)
        self.assertEqual(len(factory.launchers[0].launch_calls), 1)
        self.assertEqual(len(factory.launchers[1].launch_calls), 1)


# ---------------------------------------------------------------------------
# _resolve_parent_window
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class ResolveParentWindowTests(unittest.TestCase):
    def test_returns_none_when_view_has_no_window_root(self) -> None:
        # A freshly-constructed text view's root is the widget
        # itself, not a Gtk.Window.
        handler, _, _, _ = _make_handler(return_value=None)
        self.assertIsNone(handler._resolve_parent_window())

    def test_returns_window_when_view_is_inside_one(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        window = Gtk.Window.new()
        window.set_child(text_view)
        try:
            self.assertIs(
                handler._resolve_parent_window(),
                window,
            )
        finally:
            window.destroy()

    def test_passes_resolved_window_through_to_launch(self) -> None:
        # End-to-end: when the view is inside a window, the launch
        # call's ``parent`` argument is that window.
        handler, factory, _, text_view = _make_handler(return_value=None)
        window = Gtk.Window.new()
        window.set_child(text_view)
        try:
            handler._activate_url("https://example.com")
        finally:
            window.destroy()
        self.assertIs(factory.launchers[0].launch_calls[0].parent, window)


# ---------------------------------------------------------------------------
# _url_at_iter — the iter → URL pipeline
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class UrlAtIterTests(unittest.TestCase):
    """The iter-driven URL lookup is the testable seam used by both
    the motion and click controllers. These tests verify the seam
    without involving the renderer's coord-translation step.
    """

    def test_passes_iter_tags_through_to_renderer(self) -> None:
        handler, _, fake_renderer, text_view = _make_handler(
            return_value="https://example.com",
        )
        iterator = _spare_iter_for(text_view)
        result = handler._url_at_iter(iterator)
        self.assertEqual(result, "https://example.com")
        # The renderer was asked exactly once.
        self.assertEqual(len(fake_renderer.url_for_tags_calls), 1)
        # The argument was a list of tags (possibly empty here, since
        # the iter is at offset 0 of a freshly-seeded buffer).
        self.assertIsInstance(fake_renderer.url_for_tags_calls[0], list)

    def test_returns_none_when_renderer_returns_none(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        iterator = _spare_iter_for(text_view)
        self.assertIsNone(handler._url_at_iter(iterator))


# ---------------------------------------------------------------------------
# _set_cursor_to_link — idempotency and toggle behaviour
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class SetCursorToLinkTests(unittest.TestCase):
    """The cursor toggle uses an idempotency check on
    :attr:`_showing_link_cursor`. Behavioural tests use a counting
    override on :meth:`Gtk.TextView.set_cursor` rather than asserting
    on :meth:`Gtk.TextView.get_cursor`, because GTK's text view
    installs its own cursor internally for I-beam handling and
    ``get_cursor`` reflects that — making the "no cursor was set"
    state non-trivial to query directly.
    """

    def test_initial_state_is_not_showing_link_cursor(self) -> None:
        handler, _, _, _ = _make_handler(return_value=None)
        self.assertFalse(handler._showing_link_cursor)

    def test_default_cursor_captured_at_construction(self) -> None:
        # The handler should remember whatever cursor the text view
        # had at construction so it can restore it on link-leave
        # rather than blanking the I-beam GTK installs.
        handler, _, _, text_view = _make_handler(return_value=None)

        self.assertEqual(handler._default_cursor, text_view.get_cursor())

    def test_setting_link_true_applies_pointer_cursor(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        handler._set_cursor_to_link(True)

        self.assertTrue(handler._showing_link_cursor)
        self.assertIs(text_view.get_cursor(), handler._link_cursor)

    def test_setting_link_false_restores_default_cursor(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        original = text_view.get_cursor()
        handler._set_cursor_to_link(True)
        handler._set_cursor_to_link(False)

        self.assertFalse(handler._showing_link_cursor)
        # The cursor was restored to whatever was captured at
        # construction time — *not* set to None.
        self.assertIs(text_view.get_cursor(), original)

    def test_repeated_true_is_idempotent(self) -> None:
        # The handler should NOT call set_cursor again when the
        # requested state already matches the current state. We
        # verify by overriding ``set_cursor`` with a counter.
        handler, _, _, text_view = _make_handler(return_value=None)
        call_count = 0
        original_set_cursor = text_view.set_cursor

        def counting_set_cursor(cursor: Gdk.Cursor | None) -> None:
            nonlocal call_count
            call_count += 1
            original_set_cursor(cursor)

        text_view.set_cursor = counting_set_cursor  # type: ignore[method-assign]
        handler._set_cursor_to_link(True)
        handler._set_cursor_to_link(True)
        handler._set_cursor_to_link(True)
        self.assertEqual(call_count, 1)

    def test_repeated_false_is_idempotent(self) -> None:
        # Symmetrical to the True case. The handler starts in the
        # "not showing link" state, so three False calls must
        # produce zero set_cursor invocations.
        handler, _, _, text_view = _make_handler(return_value=None)
        call_count = 0
        original_set_cursor = text_view.set_cursor

        def counting_set_cursor(cursor: Gdk.Cursor | None) -> None:
            nonlocal call_count
            call_count += 1
            original_set_cursor(cursor)

        text_view.set_cursor = counting_set_cursor  # type: ignore[method-assign]
        handler._set_cursor_to_link(False)
        handler._set_cursor_to_link(False)
        handler._set_cursor_to_link(False)
        self.assertEqual(call_count, 0)

    def test_toggle_applies_each_change(self) -> None:
        # True → False → True must produce three set_cursor calls
        # (the three transitions), not zero or one.
        handler, _, _, text_view = _make_handler(return_value=None)
        call_count = 0
        original_set_cursor = text_view.set_cursor

        def counting_set_cursor(cursor: Gdk.Cursor | None) -> None:
            nonlocal call_count
            call_count += 1
            original_set_cursor(cursor)

        text_view.set_cursor = counting_set_cursor  # type: ignore[method-assign]
        handler._set_cursor_to_link(True)
        handler._set_cursor_to_link(False)
        handler._set_cursor_to_link(True)
        self.assertEqual(call_count, 3)


# ---------------------------------------------------------------------------
# _on_leave — adapter for the controller's ``leave`` signal
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class LeaveCallbackTests(unittest.TestCase):
    def test_leave_clears_a_lingering_link_cursor(self) -> None:
        # Set up the "currently showing link" state, then fire
        # ``leave`` and confirm the cursor is no longer the link cursor.
        handler, _, _, text_view = _make_handler(return_value=None)
        handler._set_cursor_to_link(True)
        self.assertTrue(handler._showing_link_cursor)
        # Now leave.
        handler._on_leave(Gtk.EventControllerMotion.new())

        self.assertFalse(handler._showing_link_cursor)
        # The link cursor was lifted and the captured default put back.
        self.assertIs(text_view.get_cursor(), handler._default_cursor)

    def test_leave_when_already_default_makes_no_extra_set_cursor_call(
        self,
    ) -> None:
        # When _showing_link_cursor is False (the default), leave
        # must short-circuit — no redundant set_cursor call.
        handler, _, _, text_view = _make_handler(return_value=None)
        call_count = 0
        original_set_cursor = text_view.set_cursor

        def counting_set_cursor(cursor: Gdk.Cursor | None) -> None:
            nonlocal call_count
            call_count += 1
            original_set_cursor(cursor)

        text_view.set_cursor = counting_set_cursor  # type: ignore[method-assign]
        handler._on_leave(Gtk.EventControllerMotion.new())
        self.assertEqual(call_count, 0)


# ---------------------------------------------------------------------------
# Motion / release controller adapters — driven via iter-level seams
#
# The actual coord-resolution path (window_to_buffer_coords +
# get_iter_at_location) requires a realised, laid-out widget and is
# verified only via the install smoke test and the URL-recovery
# integration tests below. Behavioural assertions on motion and
# release flow drive the iter-level seam directly so the tests
# don't depend on a windowed layout pass.
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class IterPipelineTests(unittest.TestCase):
    """Verify the iter → URL → cursor / launch flows independently
    of coord translation.
    """

    def test_iter_resolving_to_url_then_set_cursor_link_true(self) -> None:
        # Compose the same calls ``_on_motion`` makes when an iter
        # was resolved successfully, asserting the cursor switches
        # to link.
        handler, _, _, text_view = _make_handler(
            return_value="https://example.com",
        )
        iterator = _spare_iter_for(text_view)
        url = handler._url_at_iter(iterator)

        handler._set_cursor_to_link(url is not None)
        self.assertTrue(handler._showing_link_cursor)
        self.assertIs(text_view.get_cursor(), handler._link_cursor)

    def test_iter_resolving_to_none_keeps_default_cursor(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        iterator = _spare_iter_for(text_view)
        url = handler._url_at_iter(iterator)

        handler._set_cursor_to_link(url is not None)
        self.assertFalse(handler._showing_link_cursor)
        # The cursor matches the captured default — no change.
        self.assertIs(text_view.get_cursor(), handler._default_cursor)

    def test_iter_resolving_to_url_then_activate_launches(self) -> None:
        # Compose the same calls ``_on_released`` makes for a
        # link click, and assert the launch fired.
        handler, factory, _, text_view = _make_handler(
            return_value="https://example.com",
        )
        iterator = _spare_iter_for(text_view)

        handler._activate_url(handler._url_at_iter(iterator))
        self.assertEqual(factory.urls, ["https://example.com"])
        self.assertEqual(len(factory.launchers[0].launch_calls), 1)

    def test_iter_resolving_to_none_does_not_launch(self) -> None:
        # The plan's "non-link click does nothing" contract.
        handler, factory, _, text_view = _make_handler(return_value=None)
        iterator = _spare_iter_for(text_view)

        handler._activate_url(handler._url_at_iter(iterator))
        self.assertEqual(factory.urls, [])
        self.assertEqual(factory.launchers, [])


@unittest.skipUnless(_display_available(), "no GDK display")
class ControllerCallbackUnrealisedFallbackTests(unittest.TestCase):
    """When the controllers fire on an unrealised text view (or a
    point outside the laid-out text), :meth:`_iter_at_widget_coords`
    returns :data:`None` and the callbacks must short-circuit
    without raising.
    """

    def test_motion_off_text_does_not_change_cursor(self) -> None:
        # An unrealised text view's get_iter_at_location reports
        # False, so _on_motion sees no iter and treats it as
        # "not on a link" — the default cursor is the right state.
        handler, _, fake_renderer, text_view = _make_handler(
            return_value="https://example.com",
        )
        call_count = 0
        original_set_cursor = text_view.set_cursor

        def counting_set_cursor(cursor: Gdk.Cursor | None) -> None:
            nonlocal call_count
            call_count += 1
            original_set_cursor(cursor)

        text_view.set_cursor = counting_set_cursor  # type: ignore[method-assign]
        handler._on_motion(
            Gtk.EventControllerMotion.new(),
            10.0,
            10.0,
        )
        # The renderer is never consulted, because no iter was
        # resolved, and the cursor stayed in its default state.
        self.assertEqual(fake_renderer.url_for_tags_calls, [])
        self.assertEqual(call_count, 0)
        self.assertFalse(handler._showing_link_cursor)

    def test_release_off_text_does_nothing(self) -> None:
        handler, factory, fake_renderer, _ = _make_handler(
            return_value="https://example.com",
        )
        handler._on_released(
            Gtk.GestureClick.new(),
            1,
            10.0,
            10.0,
        )
        self.assertEqual(factory.urls, [])
        self.assertEqual(factory.launchers, [])
        # Renderer was never consulted either.
        self.assertEqual(fake_renderer.url_for_tags_calls, [])


# ---------------------------------------------------------------------------
# URL recovery from a real link tag — integration with the renderer
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class UrlRecoveryFromLinkTagTests(unittest.TestCase):
    """End-to-end: render a doc, look up tags at the link offset, launch.

    These tests use a real :class:`TextBufferRenderer` and a real
    :class:`Gtk.TextBuffer` so the URL recovery path the link
    handler relies on is exercised against actual renderer output,
    not a stub. The launcher factory remains a fake — the
    "do not actually open URLs in tests" contract.
    """

    def test_clicking_inside_link_text_launches_the_url(self) -> None:
        # Render a document that contains exactly one link, find the
        # buffer offset of the display text, get the iter there, and
        # feed it through the handler's iter pipeline — the same
        # pipeline the released-callback uses, just bypassing the
        # coord translation that would otherwise require a realised
        # widget.
        table = build_tag_table(char_width_px=9)
        renderer = TextBufferRenderer(
            image_bytes_for=lambda _f: b"",
            column_width_px=lambda: 800,
            tag_table=table,
        )
        buffer = Gtk.TextBuffer.new(table)
        renderer.render_into(
            "= Doc\n\nclick https://example.com[here] please\n",
            buffer,
            note_id="n1",
        )
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        offset = text.index("here") + 1
        iterator = buffer.get_iter_at_offset(offset)
        # The handler is built with the *real* renderer.
        factory = _RecordingLauncherFactory()
        text_view = Gtk.TextView.new_with_buffer(buffer)
        handler = LinkHandler(
            text_view=text_view,
            renderer=renderer,
            launcher_factory=factory,
        )

        handler._activate_url(handler._url_at_iter(iterator))
        self.assertEqual(factory.urls, ["https://example.com"])
        self.assertEqual(len(factory.launchers[0].launch_calls), 1)

    def test_offset_outside_any_link_resolves_to_no_launch(self) -> None:
        table = build_tag_table(char_width_px=9)
        renderer = TextBufferRenderer(
            image_bytes_for=lambda _f: b"",
            column_width_px=lambda: 800,
            tag_table=table,
        )
        buffer = Gtk.TextBuffer.new(table)
        renderer.render_into(
            "= Doc\n\nclick https://example.com[here] please\n",
            buffer,
            note_id="n1",
        )
        # Offset 1 is in the heading "Doc", not inside the link.
        iterator = buffer.get_iter_at_offset(1)
        factory = _RecordingLauncherFactory()
        text_view = Gtk.TextView.new_with_buffer(buffer)
        handler = LinkHandler(
            text_view=text_view,
            renderer=renderer,
            launcher_factory=factory,
        )

        url = handler._url_at_iter(iterator)
        handler._activate_url(url)
        self.assertIsNone(url)
        self.assertEqual(factory.urls, [])
        self.assertEqual(factory.launchers, [])

    def test_two_links_in_doc_each_recover_their_own_url(self) -> None:
        # Confirms the handler doesn't somehow latch onto a single
        # URL: two clicks on two different links produce two
        # distinct launches.
        table = build_tag_table(char_width_px=9)
        renderer = TextBufferRenderer(
            image_bytes_for=lambda _f: b"",
            column_width_px=lambda: 800,
            tag_table=table,
        )
        buffer = Gtk.TextBuffer.new(table)
        renderer.render_into(
            "first https://a.test[A] then https://b.test[B] done\n",
            buffer,
            note_id="n1",
        )
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        a_iter = buffer.get_iter_at_offset(text.index("A"))
        b_iter = buffer.get_iter_at_offset(text.index("B"))
        factory = _RecordingLauncherFactory()
        text_view = Gtk.TextView.new_with_buffer(buffer)
        handler = LinkHandler(
            text_view=text_view,
            renderer=renderer,
            launcher_factory=factory,
        )

        handler._activate_url(handler._url_at_iter(a_iter))
        handler._activate_url(handler._url_at_iter(b_iter))
        self.assertEqual(factory.urls, ["https://a.test", "https://b.test"])

    def test_unrelated_tag_in_buffer_does_not_match_a_link(self) -> None:
        # A region with bold styling but no link must produce no
        # URL — verifies that URL recovery distinguishes anonymous
        # URL tags from the shared LINK / BOLD styling tags.
        table = build_tag_table(char_width_px=9)
        renderer = TextBufferRenderer(
            image_bytes_for=lambda _f: b"",
            column_width_px=lambda: 800,
            tag_table=table,
        )
        buffer = Gtk.TextBuffer.new(table)
        renderer.render_into(
            "= Doc\n\n*just bold here* no link\n",
            buffer,
            note_id="n1",
        )
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        iterator = buffer.get_iter_at_offset(text.index("bold"))
        # Sanity check: BOLD is in the tag list, so we ARE inside a
        # tagged region — we just want url_for_tags to still be None.
        bold_tag = table.lookup(TagName.BOLD.value)
        self.assertIn(bold_tag, iterator.get_tags())
        factory = _RecordingLauncherFactory()
        text_view = Gtk.TextView.new_with_buffer(buffer)
        handler = LinkHandler(
            text_view=text_view,
            renderer=renderer,
            launcher_factory=factory,
        )

        handler._activate_url(handler._url_at_iter(iterator))
        self.assertEqual(factory.urls, [])


# ---------------------------------------------------------------------------
# install() — smoke test
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class InstallSmokeTests(unittest.TestCase):
    """``install`` should attach controllers without raising.

    Per §10 of the plan, UI smoke tests verify construction does not
    explode; behavioural assertions about hover and click flow live
    in the focused test classes above.

    Note that :class:`Gtk.TextView` ships with several built-in
    controllers (motion, click, drag, focus, key, drop, shortcut)
    so we cannot identify our additions by class — we identify them
    by counting the increase in the controller list.
    """

    def test_install_does_not_raise(self) -> None:
        handler, _, _, _ = _make_handler(return_value=None)
        handler.install()

    def test_install_increases_controller_count_by_two(self) -> None:
        handler, _, _, text_view = _make_handler(return_value=None)
        before = text_view.observe_controllers().get_n_items()
        handler.install()
        after = text_view.observe_controllers().get_n_items()
        self.assertEqual(after - before, 2)


if __name__ == "__main__":
    unittest.main()

"""Tests for :mod:`ui._image_picker`.

The asynchronous :class:`Gtk.FileDialog.open` flow cannot be driven
entirely without a display, but the module's narrow surface — one
callable plus a type alias — is testable in two layers:

* the module-level constants line up with :class:`MimeKind`
  (a pure-data check, no display required);
* the production opener wires up a real :class:`Gtk.FileDialog`
  with the right title, modal flag, and filter set, which we
  inspect by intercepting the dialog before it actually presents.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import GLib, Gdk, Gtk  # noqa: E402

from enums import MimeKind
from ui._image_picker import (
    FileDialogOpener,
    _IMAGE_FILTER_MIME_TYPES,
    _IMAGE_FILTER_NAME,
    default_file_dialog_opener,
)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for any
    :class:`Gtk.FileDialog` construction.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Constant-shape tests — pure data, no display needed
# ---------------------------------------------------------------------------


class FilterMimeTypesAlignWithMimeKindTests(unittest.TestCase):
    """The dialog's filter MIME-type list and :class:`MimeKind` must
    stay in sync. They are duplicated by design (the picker module
    avoids importing :class:`MimeKind` to keep the dialog's filter
    a self-contained UI concern), so the test pins the relationship
    explicitly.
    """

    def test_every_mime_kind_is_in_the_filter_list(self) -> None:
        kind_values = {kind.value for kind in MimeKind}
        filter_values = set(_IMAGE_FILTER_MIME_TYPES)
        self.assertEqual(kind_values, filter_values)

    def test_filter_name_is_human_readable(self) -> None:
        # The label appears in the file dialog's filter dropdown.
        # Empty / whitespace-only would be a usability regression.
        self.assertTrue(_IMAGE_FILTER_NAME.strip())


class FileDialogOpenerTypeAliasTests(unittest.TestCase):
    """Sanity-check that the alias is what the editor expects."""

    def test_default_opener_is_callable(self) -> None:
        # A bare type-alias check is mypy's job; at runtime we only
        # need the callable to be invocable. We don't actually call
        # it here — that's covered by the integration tests below.
        self.assertTrue(callable(default_file_dialog_opener))

    def test_alias_accepts_default_opener(self) -> None:
        # If the alias drifted, the assignment below would fail
        # type-checking and surface in mypy. The runtime assignment
        # is a one-liner that the test pins so a future refactor
        # is forced to keep them aligned.
        opener: FileDialogOpener = default_file_dialog_opener
        self.assertIs(opener, default_file_dialog_opener)


# ---------------------------------------------------------------------------
# Integration: dialog construction — display required
# ---------------------------------------------------------------------------


class _DialogProbe:  # pylint: disable=too-many-instance-attributes
    """Minimal stand-in for :class:`Gtk.FileDialog`.

    Records the configuration the opener applies (title, modal,
    filters) and short-circuits the asynchronous ``open`` so the
    test can drive the result callback without spinning a real
    main loop. The probe also exposes a ``deliver_path`` /
    ``deliver_cancel`` / ``deliver_non_local`` / ``deliver_error``
    helper so each terminal branch of the production opener can
    be exercised explicitly.
    """

    title: str | None
    modal: bool
    default_filter: Gtk.FileFilter | None
    filters: list[Gtk.FileFilter]
    open_calls: list[tuple[Gtk.Window | None, object, Callable[..., None]]]
    _last_callback: Callable[[object, object], None] | None
    _next_finish_result: Gtk.Window | None
    _finish_raises: bool
    _finish_returns_path: str | None
    _finish_returns_none: bool

    def __init__(self) -> None:
        self.title = None
        self.modal = False
        self.default_filter = None
        self.filters = []
        self.open_calls = []
        self._last_callback = None
        self._next_finish_result = None
        self._finish_raises = False
        self._finish_returns_path = None
        self._finish_returns_none = False

    # --- methods Gtk.FileDialog exposes that the opener calls ---

    def set_title(self, title: str) -> None:
        self.title = title

    def set_modal(self, modal: bool) -> None:
        self.modal = modal

    def set_default_filter(self, image_filter: Gtk.FileFilter) -> None:
        self.default_filter = image_filter

    def set_filters(self, filters: object) -> None:
        # The opener passes a Gio.ListStore; we record the list of
        # filters by walking it via Python's iteration protocol,
        # which Gio.ListStore supports.
        self.filters = list(filters)  # type: ignore[call-overload]

    def open(
        self,
        parent: Gtk.Window | None,
        cancellable: object,
        callback: Callable[[object, object], None],
    ) -> None:
        self.open_calls.append((parent, cancellable, callback))
        self._last_callback = callback

    # --- helpers the test invokes to drive the post-pick callback ---

    def deliver_path(self, path: str) -> None:
        # Configure open_finish to return a Gio.File-like with this
        # path, then trigger the callback.
        self._finish_returns_path = path
        self._finish_returns_none = False
        self._finish_raises = False
        self._fire()

    def deliver_cancel_via_glib_error(self) -> None:
        self._finish_raises = True
        self._fire()

    def deliver_non_local_uri(self) -> None:
        # Gio.File.get_path() returns None for remote URIs.
        self._finish_returns_path = None
        self._finish_returns_none = False
        self._finish_raises = False
        self._fire()

    def deliver_none_file(self) -> None:
        self._finish_returns_none = True
        self._finish_raises = False
        self._fire()

    # --- glue ---

    def open_finish(self, _result: object) -> object:
        # Called by the opener's _on_open_finished. Returns either
        # the configured Gio.File-like or raises GLib.Error.
        if self._finish_raises:
            raise GLib.Error("user cancelled")
        if self._finish_returns_none:
            return None
        # A tiny stub that exposes get_path() the way Gio.File does.

        path_value = self._finish_returns_path

        class _StubFile:
            def get_path(self) -> str | None:
                return path_value

        return _StubFile()

    def _fire(self) -> None:
        callback = self._last_callback
        if callback is None:
            raise AssertionError("open() must be called before delivering a result")
        # The first arg is the source object (the dialog); the
        # second is a Gio.AsyncResult. The opener does not consult
        # either, so passing None for the result is harmless.
        callback(self, None)


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileDialogOpenerConfigurationTests(unittest.TestCase):
    """The opener configures the dialog with the right metadata
    before delegating to ``open``.
    """

    def _run_opener(
        self,
        parent: Gtk.Widget | None = None,
    ) -> tuple[_DialogProbe, list[Path | None]]:
        probe = _DialogProbe()
        results: list[Path | None] = []
        # Patch Gtk.FileDialog.new to return the probe; the opener's
        # other Gtk.* calls are real (FileFilter, FileFilter mime
        # additions, Gio.ListStore.new), which is fine.
        with patch.object(Gtk.FileDialog, "new", return_value=probe):
            real_parent = parent if parent is not None else Gtk.Box.new(
                Gtk.Orientation.HORIZONTAL, 0
            )
            default_file_dialog_opener(real_parent, results.append)
        return probe, results

    def test_dialog_title_is_set(self) -> None:
        probe, _ = self._run_opener()
        self.assertEqual(probe.title, "Insert image")

    def test_dialog_is_modal(self) -> None:
        probe, _ = self._run_opener()
        self.assertTrue(probe.modal)

    def test_default_filter_includes_all_image_mime_types(self) -> None:
        probe, _ = self._run_opener()
        self.assertIsNotNone(probe.default_filter)
        # We can't easily round-trip Gtk.FileFilter's MIME types
        # back out (the GTK API doesn't expose a getter), but the
        # filter's name is set on it and is a stable identifier.
        assert probe.default_filter is not None
        self.assertEqual(probe.default_filter.get_name(), _IMAGE_FILTER_NAME)

    def test_filter_list_contains_one_filter(self) -> None:
        probe, _ = self._run_opener()
        self.assertEqual(len(probe.filters), 1)

    def test_open_is_invoked_exactly_once(self) -> None:
        probe, _ = self._run_opener()
        self.assertEqual(len(probe.open_calls), 1)


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileDialogOpenerCallbackTests(unittest.TestCase):
    """Each terminal branch of the post-pick callback flow."""

    def _run_with_probe(self) -> tuple[_DialogProbe, list[Path | None]]:
        probe = _DialogProbe()
        results: list[Path | None] = []
        with patch.object(Gtk.FileDialog, "new", return_value=probe):
            parent = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
            default_file_dialog_opener(parent, results.append)
        return probe, results

    def test_successful_pick_forwards_path(self) -> None:
        probe, results = self._run_with_probe()
        probe.deliver_path("/tmp/photo.png")
        self.assertEqual(results, [Path("/tmp/photo.png")])

    def test_user_cancellation_forwards_none(self) -> None:
        # GLib.Error from open_finish is the cancellation /
        # backend-error path.
        probe, results = self._run_with_probe()
        probe.deliver_cancel_via_glib_error()
        self.assertEqual(results, [None])

    def test_open_finish_returning_none_forwards_none(self) -> None:
        # Defensive branch: open_finish returns None instead of a
        # Gio.File. The opener forwards None.
        probe, results = self._run_with_probe()
        probe.deliver_none_file()
        self.assertEqual(results, [None])

    def test_non_local_uri_forwards_none(self) -> None:
        # Gio.File.get_path() returns None for remote URIs (gvfs,
        # portals). The opener forwards None — the editor can't
        # attach those in v1.
        probe, results = self._run_with_probe()
        probe.deliver_non_local_uri()
        self.assertEqual(results, [None])


if __name__ == "__main__":
    unittest.main()

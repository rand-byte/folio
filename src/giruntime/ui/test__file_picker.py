"""Tests for :mod:`ui._file_picker`.

The asynchronous :class:`Gtk.FileDialog.open` flow cannot be driven
entirely without a display, but the module's narrow surface — one
callable plus a type alias — is testable by intercepting the dialog
before it actually presents: the production opener wires up a real
:class:`Gtk.FileDialog` with the right title and modal flag (and no
file filter — any file may be attached), and each terminal branch of
the post-pick callback is driven synchronously through a probe.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from unittest.mock import patch

from gi.repository import GLib, Gdk, Gtk

from giruntime.ui._file_picker import (
    FileDialogOpener,
    FileSaveDialogOpener,
    _DIALOG_TITLE,
    _SAVE_DIALOG_TITLE,
    default_file_dialog_opener,
    default_file_save_dialog_opener,
)


def _display_available() -> bool:
    """True iff a GDK display can be opened — required for any
    :class:`Gtk.FileDialog` construction.
    """
    Gtk.init_check()
    return Gdk.Display.get_default() is not None


# ---------------------------------------------------------------------------
# Type-alias surface — pure data, no display needed
# ---------------------------------------------------------------------------


class FileDialogOpenerTypeAliasTests(unittest.TestCase):
    """Sanity-check that the alias is what the callers expect."""

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


class _FinishKind(Enum):
    """What a probe's asynchronous ``*_finish`` call should do when fired."""

    RAISES = auto()  # raise GLib.Error (user cancel / backend error)
    RETURNS_NONE = auto()  # return None (open_finish yielded no file)
    RETURNS_FILE = auto()  # return a Gio.File-like whose get_path() is `path`


@dataclass(frozen=True)
class _FinishOutcome:
    """One terminal outcome of a probe's asynchronous finish call.

    A single value replacing the former three co-dependent fields. ``path``
    is only meaningful for :attr:`_FinishKind.RETURNS_FILE`: a string models a
    local pick, :data:`None` models a remote URI whose ``Gio.File.get_path()``
    yields :data:`None`.
    """

    kind: _FinishKind
    path: str | None = None


class _StubFile:
    """Minimal :class:`Gio.File` stand-in exposing only ``get_path()``."""

    _path: str | None

    def __init__(self, path: str | None) -> None:
        self._path = path

    def get_path(self) -> str | None:
        return self._path


def _resolve_finish(outcome: _FinishOutcome) -> object:
    """Produce the value a ``*_finish`` call returns, or raise for the
    cancellation branch — the shared body both probes delegate to.
    """
    if outcome.kind is _FinishKind.RAISES:
        raise GLib.Error("user cancelled")
    if outcome.kind is _FinishKind.RETURNS_NONE:
        return None
    return _StubFile(outcome.path)


class _DialogProbe:
    """Minimal stand-in for :class:`Gtk.FileDialog`.

    Records the configuration the opener applies (title, modal,
    filters) and short-circuits the asynchronous ``open`` so the
    test can drive the result callback without spinning a real
    main loop. The probe also exposes ``deliver_*`` helpers so each
    terminal branch of the production opener can be exercised
    explicitly. The filter setters are recorded (not omitted) so the
    suite can pin that the opener no longer applies any filter.
    """

    title: str | None
    modal: bool
    default_filter_calls: list[Gtk.FileFilter]
    set_filters_calls: list[object]
    open_calls: list[tuple[Gtk.Window | None, object, Callable[..., None]]]
    _last_callback: Callable[[object, object], None] | None
    _outcome: _FinishOutcome | None

    def __init__(self) -> None:
        self.title = None
        self.modal = False
        self.default_filter_calls = []
        self.set_filters_calls = []
        self.open_calls = []
        self._last_callback = None
        self._outcome = None

    # --- methods Gtk.FileDialog exposes that the opener may call ---

    def set_title(self, title: str) -> None:
        self.title = title

    def set_modal(self, modal: bool) -> None:
        self.modal = modal

    def set_default_filter(self, file_filter: Gtk.FileFilter) -> None:
        self.default_filter_calls.append(file_filter)

    def set_filters(self, filters: object) -> None:
        self.set_filters_calls.append(filters)

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
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_FILE, path)
        self._fire()

    def deliver_cancel_via_glib_error(self) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RAISES)
        self._fire()

    def deliver_non_local_uri(self) -> None:
        # Gio.File.get_path() returns None for remote URIs.
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_FILE, None)
        self._fire()

    def deliver_none_file(self) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_NONE)
        self._fire()

    # --- glue ---

    def open_finish(self, _result: object) -> object:
        # Called by the opener's _on_open_finished. Returns the
        # configured Gio.File-like or raises GLib.Error.
        outcome = self._outcome
        if outcome is None:
            raise AssertionError("a deliver_* helper must run before open_finish")
        return _resolve_finish(outcome)

    def _fire(self) -> None:
        callback = self._last_callback
        if callback is None:
            raise AssertionError("open() must be called before delivering a result")
        # The first arg is the source object (the dialog); the
        # second is a Gio.AsyncResult. The opener does not consult
        # either, so passing None for the result is harmless.
        callback(self, None)


def _run_opener_with_probe() -> tuple[_DialogProbe, list[Path | None]]:
    probe = _DialogProbe()
    results: list[Path | None] = []
    # Patch Gtk.FileDialog.new to return the probe; the opener's
    # other Gtk.* calls are real, which is fine.
    with patch.object(Gtk.FileDialog, "new", return_value=probe):
        parent = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        default_file_dialog_opener(parent, results.append)
    return probe, results


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileDialogOpenerConfigurationTests(unittest.TestCase):
    """The opener configures the dialog with the right metadata
    before delegating to ``open``.
    """

    def test_dialog_title_is_set(self) -> None:
        probe, _ = _run_opener_with_probe()
        self.assertEqual(probe.title, _DIALOG_TITLE)

    def test_dialog_is_modal(self) -> None:
        probe, _ = _run_opener_with_probe()
        self.assertTrue(probe.modal)

    def test_no_file_filter_is_applied(self) -> None:
        # Attachments are opaque blobs — the image-only MIME filter is
        # gone, so the dialog must offer all files. Pin that neither
        # filter setter was invoked.
        probe, _ = _run_opener_with_probe()
        self.assertEqual(probe.default_filter_calls, [])
        self.assertEqual(probe.set_filters_calls, [])

    def test_open_is_invoked_exactly_once(self) -> None:
        probe, _ = _run_opener_with_probe()
        self.assertEqual(len(probe.open_calls), 1)


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileDialogOpenerCallbackTests(unittest.TestCase):
    """Each terminal branch of the post-pick callback flow."""

    def test_successful_pick_forwards_path(self) -> None:
        probe, results = _run_opener_with_probe()
        probe.deliver_path("/tmp/notes.pdf")
        self.assertEqual(results, [Path("/tmp/notes.pdf")])

    def test_user_cancellation_forwards_none(self) -> None:
        # GLib.Error from open_finish is the cancellation /
        # backend-error path.
        probe, results = _run_opener_with_probe()
        probe.deliver_cancel_via_glib_error()
        self.assertEqual(results, [None])

    def test_open_finish_returning_none_forwards_none(self) -> None:
        # Defensive branch: open_finish returns None instead of a
        # Gio.File. The opener forwards None.
        probe, results = _run_opener_with_probe()
        probe.deliver_none_file()
        self.assertEqual(results, [None])

    def test_non_local_uri_forwards_none(self) -> None:
        # Gio.File.get_path() returns None for remote URIs (gvfs,
        # portals). The opener forwards None — the panel can't
        # attach those in v1.
        probe, results = _run_opener_with_probe()
        probe.deliver_non_local_uri()
        self.assertEqual(results, [None])


class _SaveDialogProbe:
    """Minimal stand-in for :class:`Gtk.FileDialog` in *save* mode.

    The save opener's shape mirrors the open opener's, so this probe
    mirrors :class:`_DialogProbe`: it records the configuration the
    opener applies (title, modal, initial name) and short-circuits the
    asynchronous ``save`` so each terminal branch of the result callback
    can be driven synchronously.
    """

    title: str | None
    modal: bool
    initial_name: str | None
    save_calls: list[tuple[Gtk.Window | None, object, Callable[..., None]]]
    _last_callback: Callable[[object, object], None] | None
    _outcome: _FinishOutcome | None

    def __init__(self) -> None:
        self.title = None
        self.modal = False
        self.initial_name = None
        self.save_calls = []
        self._last_callback = None
        self._outcome = None

    def set_title(self, title: str) -> None:
        self.title = title

    def set_modal(self, modal: bool) -> None:
        self.modal = modal

    def set_initial_name(self, name: str) -> None:
        self.initial_name = name

    def save(
        self,
        parent: Gtk.Window | None,
        cancellable: object,
        callback: Callable[[object, object], None],
    ) -> None:
        self.save_calls.append((parent, cancellable, callback))
        self._last_callback = callback

    def deliver_path(self, path: str) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_FILE, path)
        self._fire()

    def deliver_cancel_via_glib_error(self) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RAISES)
        self._fire()

    def deliver_non_local_uri(self) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_FILE, None)
        self._fire()

    def deliver_none_file(self) -> None:
        self._outcome = _FinishOutcome(_FinishKind.RETURNS_NONE)
        self._fire()

    def save_finish(self, _result: object) -> object:
        outcome = self._outcome
        if outcome is None:
            raise AssertionError("a deliver_* helper must run before save_finish")
        return _resolve_finish(outcome)

    def _fire(self) -> None:
        callback = self._last_callback
        if callback is None:
            raise AssertionError("save() must be called before a result")
        callback(self, None)


def _run_save_opener_with_probe(
    suggested_name: str = "report.pdf",
) -> tuple[_SaveDialogProbe, list[Path | None]]:
    probe = _SaveDialogProbe()
    results: list[Path | None] = []
    with patch.object(Gtk.FileDialog, "new", return_value=probe):
        parent = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
        default_file_save_dialog_opener(parent, suggested_name, results.append)
    return probe, results


class FileSaveDialogOpenerTypeAliasTests(unittest.TestCase):
    def test_default_save_opener_is_callable(self) -> None:
        self.assertTrue(callable(default_file_save_dialog_opener))

    def test_alias_accepts_the_default_save_opener(self) -> None:
        opener: FileSaveDialogOpener = default_file_save_dialog_opener
        self.assertIs(opener, default_file_save_dialog_opener)


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileSaveDialogOpenerConfigurationTests(unittest.TestCase):
    def test_dialog_title_is_the_save_title(self) -> None:
        probe, _ = _run_save_opener_with_probe()
        self.assertEqual(probe.title, _SAVE_DIALOG_TITLE)

    def test_dialog_is_modal(self) -> None:
        probe, _ = _run_save_opener_with_probe()
        self.assertTrue(probe.modal)

    def test_initial_name_is_the_suggested_name(self) -> None:
        probe, _ = _run_save_opener_with_probe("budget-2026.xlsx")
        self.assertEqual(probe.initial_name, "budget-2026.xlsx")

    def test_save_is_invoked_once(self) -> None:
        probe, _ = _run_save_opener_with_probe()
        self.assertEqual(len(probe.save_calls), 1)

    def test_no_cancellable_is_passed(self) -> None:
        probe, _ = _run_save_opener_with_probe()
        self.assertIsNone(probe.save_calls[0][1])


@unittest.skipUnless(_display_available(), "no GDK display")
class DefaultFileSaveDialogOpenerResultTests(unittest.TestCase):
    """The three "no path" outcomes all forward :data:`None`."""

    def test_chosen_path_is_forwarded_as_a_path(self) -> None:
        probe, results = _run_save_opener_with_probe()
        probe.deliver_path("/tmp/out.pdf")
        self.assertEqual(results, [Path("/tmp/out.pdf")])

    def test_cancellation_forwards_none(self) -> None:
        probe, results = _run_save_opener_with_probe()
        probe.deliver_cancel_via_glib_error()
        self.assertEqual(results, [None])

    def test_non_local_uri_forwards_none(self) -> None:
        probe, results = _run_save_opener_with_probe()
        probe.deliver_non_local_uri()
        self.assertEqual(results, [None])

    def test_none_file_forwards_none(self) -> None:
        probe, results = _run_save_opener_with_probe()
        probe.deliver_none_file()
        self.assertEqual(results, [None])


if __name__ == "__main__":
    unittest.main()

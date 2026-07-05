"""Tests for :mod:`giruntime.ui.help_window` and the help content."""

from __future__ import annotations

import typing
import unittest

from gi.repository import Gdk, GLib, Gtk

from asciidoc import ast as A
from asciidoc.ast import BlockNode, InlineNode
from asciidoc.parser import parse
from enums import HelpSection, SystemDocument
from giruntime.ui.application import NotesApplication
from giruntime.ui.help_window import HelpWindow, _section_mark_name
from giruntime.ui.link_handler import UriLauncherProtocol
from giruntime.ui.note_render.tag_table import build_wash_specs
from giruntime.ui.note_view import ArticleContainer, ArticleTextView
from giruntime.ui.test_main_window import _display_available, _test_application
from models.parse_error import ParseError
from system_docs import load_bytes, load_text


_HELP_SOURCE: str = load_text(SystemDocument.HELP)


# ---------------------------------------------------------------------------
# AST walkers — collect the node kinds present in a parsed document
# ---------------------------------------------------------------------------


def _union_member_names(alias: object) -> set[str]:
    """Return the class names in a PEP 695 ``type X = A | B`` union alias.

    Reads the alias's runtime value via :func:`getattr` (rather than a
    direct ``alias.__value__`` access) so the coverage assertions stay
    exhaustive over the union — adding a new AST node kind forces a help
    update — without tripping the linter's attribute check on the type
    alias.
    """
    value = getattr(alias, "__value__")
    return {member.__name__ for member in typing.get_args(value)}


def _walk_inlines(nodes: tuple[InlineNode, ...], seen: set[str]) -> None:
    for node in nodes:
        seen.add(type(node).__name__)
        children = getattr(node, "children", None)
        if isinstance(children, tuple):
            _walk_inlines(children, seen)
        text = getattr(node, "text", None)
        if isinstance(text, tuple):
            _walk_inlines(text, seen)


def _walk_blocks(
    blocks: tuple[BlockNode, ...],
    block_seen: set[str],
    inline_seen: set[str],
) -> None:
    for block in blocks:
        block_seen.add(type(block).__name__)
        if isinstance(block, A.Section):
            _walk_inlines(block.title, inline_seen)
            _walk_blocks(block.blocks, block_seen, inline_seen)
        elif isinstance(block, A.Paragraph):
            _walk_inlines(block.inlines, inline_seen)
        elif isinstance(block, (A.OrderedList, A.UnorderedList)):
            for item in block.items:
                _walk_inlines(item.inlines, inline_seen)
                _walk_blocks(item.children, block_seen, inline_seen)
        elif isinstance(block, (A.Blockquote, A.Admonition)):
            _walk_blocks(block.blocks, block_seen, inline_seen)
        elif isinstance(block, A.Table):
            for row in block.rows:
                for cell in row.cells:
                    _walk_inlines(cell.inlines, inline_seen)


# ---------------------------------------------------------------------------
# Help content — parses clean, covers the whole subset (no display needed)
# ---------------------------------------------------------------------------


class HelpContentTests(unittest.TestCase):
    """The help source dogfoods the subset and exercises every node kind."""

    def test_help_parses_clean(self) -> None:
        # If the help drifts outside the supported subset, this raises.
        try:
            document = parse(_HELP_SOURCE)
        except ParseError as exc:  # pragma: no cover - failure path
            self.fail(f"help.adoc failed to parse: {exc.kind}")
        self.assertIsNotNone(document.title)

    def test_help_covers_every_block_node_kind(self) -> None:
        document = parse(_HELP_SOURCE)
        block_seen: set[str] = set()
        inline_seen: set[str] = set()
        _walk_blocks(document.blocks, block_seen, inline_seen)
        expected = _union_member_names(BlockNode)
        missing = expected - block_seen
        self.assertEqual(
            missing,
            set(),
            f"help.adoc is missing block construct(s): {sorted(missing)}",
        )

    def test_help_covers_every_inline_node_kind(self) -> None:
        document = parse(_HELP_SOURCE)
        block_seen: set[str] = set()
        inline_seen: set[str] = set()
        if document.title is not None:
            _walk_inlines(document.title, inline_seen)
        _walk_blocks(document.blocks, block_seen, inline_seen)
        expected = _union_member_names(InlineNode)
        missing = expected - inline_seen
        self.assertEqual(
            missing,
            set(),
            f"help.adoc is missing inline construct(s): {sorted(missing)}",
        )

    def test_help_contains_an_image_macro(self) -> None:
        # The coverage test above already forces an Image node, but pin
        # the demo-image reference explicitly: the macro must target the
        # bundled demo image so the resolver returns real bytes.
        document = parse(_HELP_SOURCE)
        images = _collect_images(document.blocks)
        self.assertTrue(images, "help.adoc has no image:: macro")
        self.assertIn(
            SystemDocument.HELP_DEMO_IMAGE.value,
            {image.filename for image in images},
        )

    def test_demo_image_decodes_as_a_real_image(self) -> None:
        # The help must demo the image capability with a *real* rendered
        # image, not the grey placeholder — so the bundled bytes must
        # decode through the same path the renderer uses.
        data = load_bytes(SystemDocument.HELP_DEMO_IMAGE)
        # ``GLib.Bytes.new`` is a GObject-introspected member; the linter
        # cannot always see it (same as the renderer's own decode path).
        # pylint: disable-next=no-member
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        self.assertGreater(texture.get_width(), 0)
        self.assertGreater(texture.get_height(), 0)

    def test_section_headings_match_help_section_values(self) -> None:
        # Every HelpSection value must appear as a level-2 heading, and
        # there must be no extra level-2 buckets — so the nav can map
        # one-to-one.
        document = parse(_HELP_SOURCE)
        headings = _level_two_heading_texts(document.blocks)
        self.assertEqual(headings, [section.value for section in HelpSection])


def _collect_images(blocks: tuple[BlockNode, ...]) -> list[A.Image]:
    images: list[A.Image] = []
    for block in blocks:
        if isinstance(block, A.Image):
            images.append(block)
        elif isinstance(block, A.Section):
            images.extend(_collect_images(block.blocks))
    return images


def _level_two_heading_texts(blocks: tuple[BlockNode, ...]) -> list[str]:
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, A.Section):
            if block.level == 2:
                texts.append(
                    "".join(
                        getattr(node, "content", "")
                        for node in block.title
                    ),
                )
            texts.extend(_level_two_heading_texts(block.blocks))
    return texts


# ---------------------------------------------------------------------------
# Fake URI launcher factory (for the link-handler wiring)
# ---------------------------------------------------------------------------


class _FakeUriLauncher:
    """Records the URL it was built for; never opens anything."""

    url: str

    def __init__(self, url: str) -> None:
        self.url = url

    def launch(
        self,
        parent: Gtk.Window | None,
        cancellable: object | None,
        callback: object | None,
    ) -> None:  # pragma: no cover - not invoked without a real click
        raise NotImplementedError


def _fake_launcher_factory(url: str) -> UriLauncherProtocol:
    return _FakeUriLauncher(url)


# ---------------------------------------------------------------------------
# Help window — construction, navigation marks, contents sidebar (display)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class HelpWindowTests(unittest.TestCase):
    def _build_window(self) -> HelpWindow:
        window = HelpWindow(
            application=_test_application(),
            launcher_factory=_fake_launcher_factory,
        )
        self.addCleanup(window.destroy)
        return window

    def test_window_renders_help_text(self) -> None:
        window = self._build_window()
        text = window.rendered_text
        self.assertNotEqual(text.strip(), "")
        # The three bucket titles render into the buffer.
        for section in HelpSection:
            self.assertIn(section.value, text)

    def test_section_marks_cover_every_section(self) -> None:
        window = self._build_window()
        self.assertEqual(set(window.section_marks), set(HelpSection))

    def test_section_marks_named_off_the_enum(self) -> None:
        window = self._build_window()
        for section, mark in window.section_marks.items():
            self.assertEqual(mark.get_name(), _section_mark_name(section))

    def test_contents_list_has_one_row_per_section(self) -> None:
        window = self._build_window()
        rows = []
        index = 0
        while True:
            row = window.contents_list.get_row_at_index(index)
            if row is None:
                break
            rows.append(row)
            index += 1
        self.assertEqual(len(rows), len(list(HelpSection)))

    def test_scroll_to_each_section_does_not_raise(self) -> None:
        window = self._build_window()
        for section in HelpSection:
            window.scroll_to_section(section)

    def test_contents_list_holds_no_selection(self) -> None:
        # Core honesty invariant: the sidebar is navigation commands, not
        # a selection, so nothing is ever selected — not even on build.
        # A persistent highlight would falsely claim to track the reading
        # position once the user scrolls away from the marked heading.
        window = self._build_window()
        self.assertEqual(
            window.contents_list.get_selection_mode(),
            Gtk.SelectionMode.NONE,
        )
        self.assertIsNone(window.contents_list.get_selected_row())

    def test_rows_are_activatable_commands(self) -> None:
        # Each row is an activatable, non-selectable command: activatable
        # is what keeps ``row-activated`` firing with selection off, and
        # non-selectable is what guarantees no row can stick lit.
        window = self._build_window()
        index = 0
        while True:
            row = window.contents_list.get_row_at_index(index)
            if row is None:
                break
            self.assertTrue(row.get_activatable())
            self.assertFalse(row.get_selectable())
            index += 1
        self.assertEqual(index, len(list(HelpSection)))

    def test_activating_a_row_jumps_without_selecting(self) -> None:
        # Activation still runs the jump command (the path is unchanged),
        # and it leaves no sticky highlight behind — emitting
        # ``row-activated`` must not select the row.
        window = self._build_window()
        row = window.contents_list.get_row_at_index(0)
        self.assertIsNotNone(row)
        window.contents_list.emit("row-activated", row)
        self.assertIsNone(window.contents_list.get_selected_row())

    def test_demo_image_renders_inline_as_a_paintable(self) -> None:
        # A successful image render inserts a *paintable* into the buffer.
        # Scanning for a paintable proves the image path ran and produced
        # a real paintable — a missing filename would have raised
        # ``KeyError`` during construction, and the decode test above
        # proves those bytes are a real image rather than the grey
        # placeholder.
        window = self._build_window()
        buffer = window.buffer
        iterator = buffer.get_start_iter()
        found_paintable = iterator.get_paintable() is not None
        while not found_paintable and iterator.forward_char():
            if iterator.get_paintable() is not None:
                found_paintable = True
        self.assertTrue(
            found_paintable,
            "the demo image did not render as an inline paintable",
        )

    def test_view_is_the_shared_painted_view(self) -> None:
        # The help renders into the same painted subclass the note view
        # uses (:class:`ArticleTextView`), which is what paints the
        # opaque "paper" sheet behind the content. A plain
        # :class:`Gtk.TextView` — the pre-fix state — painted no sheet,
        # so the help looked flat rather than like a rendered note.
        window = self._build_window()
        self.assertIsInstance(window.text_view, ArticleTextView)

    def test_view_is_wrapped_in_a_fixed_width_column(self) -> None:
        # Fidelity with the note view's paper-on-desk look depends on the
        # text view living inside the shared fixed-width
        # :class:`ArticleContainer` (which centres the column on a desk and
        # gives the washes the column geometry they are painted against).
        # Before this, the help put the bare text view straight in the
        # scroller, so it filled the pane edge-to-edge with no desk and
        # over-wide tints.
        window = self._build_window()
        self.assertIsInstance(window.text_view.get_parent(), ArticleContainer)

    def test_block_tints_are_installed(self) -> None:
        # Block tints (admonition / blockquote / code-block washes) are
        # painted by the view from an installed wash-spec map, *not* by
        # the tag table (which only positions text). The pre-fix help
        # installed no map, so its admonitions rendered untinted — the
        # exact symptom in the bug report. Assert every spec from the
        # single source resolved against the help's tag table and got
        # installed, so no block kind can render flat.
        window = self._build_window()
        view = window.text_view
        assert isinstance(view, ArticleTextView)
        installed = view._wash_specs_by_tag
        self.assertEqual(len(installed), len(build_wash_specs()))

    def test_window_hides_rather_than_destroys_on_close(self) -> None:
        # Reuse-and-raise (owned by the application) only holds if the one
        # built window *hides* on close instead of being destroyed —
        # otherwise the cached reference becomes a disposed, chrome-less
        # window whose close button is dead on the second open.
        window = self._build_window()
        self.assertTrue(window.get_hide_on_close())

    def test_buffer_survives_a_close(self) -> None:
        # Concretely: closing the (hide-on-close) window leaves the same
        # instance alive with its rendered content intact, ready to be
        # re-presented — the behaviour the destroy-on-close default broke.
        window = self._build_window()
        before = window.rendered_text
        window.close()
        self.assertEqual(window.rendered_text, before)


# ---------------------------------------------------------------------------
# Application wiring — the help action exists and the window is reused
# ---------------------------------------------------------------------------


@unittest.skipUnless(_display_available(), "no GDK display")
class HelpActionTests(unittest.TestCase):
    """The app registers ``help`` (F1) and reuses one help window.

    These drive the process's single registered ``NotesApplication`` — the
    shared ``_test_application`` — because the :class:`HelpWindow` that
    :meth:`NotesApplication._ensure_help_window` builds is a
    ``Gtk.ApplicationWindow``, and such a window may only be added to a
    *registered* application (one whose ``startup`` has fired). GTK permits
    exactly one registered ``GtkApplication`` per process, so that owner has
    to be the shared instance; a fresh, unregistered ``NotesApplication``
    would make GTK warn and silently drop the window instead of owning it.

    The shared app is process-lived, so each test clears the
    ``_help_window`` cache first and destroys any window it builds, keeping
    the build-once/reuse observation isolated from its neighbours.
    """

    app: NotesApplication

    def setUp(self) -> None:
        self.app = _test_application()
        self.app._help_window = None
        self.addCleanup(self._discard_help_window)

    def _discard_help_window(self) -> None:
        """Destroy any help window this test built and clear the cache.

        The shared application outlives the test; a window left cached
        (or destroyed but still referenced) would leak into the next one,
        so this restores the pre-test empty-cache state.
        """
        window = self.app._help_window
        if window is not None:
            window.destroy()
            self.app._help_window = None

    def test_install_help_action_registers_action_and_accel(self) -> None:
        self.app._install_help_action()
        self.assertIsNotNone(self.app.lookup_action("help"))
        self.assertIn("F1", self.app.get_accels_for_action("app.help"))

    def test_ensure_help_window_builds_once_and_reuses(self) -> None:
        first = self.app._ensure_help_window()
        second = self.app._ensure_help_window()
        self.assertIs(first, second)

    def test_ensure_help_window_renders_the_buffer(self) -> None:
        window = self.app._ensure_help_window()
        self.assertEqual(set(window.section_marks), set(HelpSection))
        self.assertNotEqual(window.rendered_text.strip(), "")


if __name__ == "__main__":
    unittest.main()

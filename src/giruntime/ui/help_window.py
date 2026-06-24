"""The standalone AsciiDoc help window.

Principles & invariants
-----------------------
* :class:`HelpWindow` is a single, **non-modal** top-level window that
  documents Folio's supported AsciiDoc subset. Non-modal is the whole
  point: the reference is meant to sit beside the editor while the user
  keeps typing, so it must never block the main window. It is a
  :class:`Gtk.ApplicationWindow` owned by the :class:`Gtk.Application`,
  which keeps the **single** instance and raises it on re-open (the
  reuse-and-raise lives in the application, not here).
* The help **dogfoods the format**: its content is authored in the
  supported subset itself (``system_docs/help.adoc``) and rendered
  through the very same pipeline a note uses —
  :func:`asciidoc.parser.parse` →
  :class:`giruntime.ui.note_render.textbuffer_renderer.TextBufferRenderer`
  → the shared :mod:`giruntime.ui.note_render.tag_table` styling. There
  is no second renderer and no second tag table; the help looks exactly
  like a rendered note because it *is* one.
* System documents (the help source and its demo image) are read gi-free
  from the ``system_docs`` package via :func:`system_docs.load_text` /
  :func:`system_docs.load_bytes`. The window never touches the database
  or the gresource — the help is package data, not user content.
* The renderer's two injected dependencies are wired here:

  * the :data:`~storage.protocols.ImageBytesResolver` is a small map from
    the help's image filename(s) to the bundled demo bytes. The help
    *must* demonstrate the image capability with a real, decodable image
    (the §7 coverage test forces an ``image::`` macro into the source),
    so the resolver returns real bytes — and a filename the help does not
    bundle is a help-authoring bug that surfaces as a ``KeyError`` rather
    than a silent grey placeholder.
  * the :data:`~storage.protocols.ColumnWidthResolver` returns a fixed
    reading-column width derived from the measured body font, so images
    and tables lay out against a stable column without depending on the
    window being realised at render time.

* Only **tables** escape to an anchored widget (the renderer's
  :data:`~giruntime.ui.note_render.textbuffer_renderer.WidgetAttacher`
  hook); everything else — including the demo image — renders inline into
  the buffer. The attacher is wired to
  :meth:`Gtk.TextView.add_child_at_anchor`, exactly as the note view does.
* **Navigation is single-page + a contents sidebar.** The page is one
  scrolling buffer; the sidebar lists the three top-level buckets, keyed
  off the :class:`HelpSection` enum. Selecting a row scrolls the buffer
  to a :class:`Gtk.TextMark` placed at that section's heading. The marks
  are dropped in a post-render pass that matches each rendered level-2
  heading line (already tagged by the tag table) against the enum's
  values, so the sidebar list and the scroll targets are driven by the
  *same* source of truth and cannot drift. A heading that matches no
  member, or a member with no heading, fails loudly at build time.
* Example links inside the help are live: a :class:`LinkHandler` is
  installed on the read-only text view exactly as it would be on a note's
  read view, so the rendered example URLs open in the system browser.
* This module lives under ``giruntime/ui`` because it owns a widget tree —
  the only layer permitted to. It is thin and unit-testable: the
  renderer, the launcher factory, and the system-document bytes are all
  injectable, and every navigation seam is a plain method tests can drive.
* GTK 4.18 currency: :class:`Gtk.ApplicationWindow`, :class:`Gtk.Paned`,
  :class:`Gtk.ListBox`, :meth:`Gtk.TextView.scroll_to_mark`,
  :meth:`Gtk.TextBuffer.create_mark` — no methods deprecated in 4.18 or
  earlier.
"""

from __future__ import annotations

from gi.repository import Gtk, Pango

from config.defaults import TARGET_CHARS_PER_LINE
from enums import HelpSection, SystemDocument
from giruntime.ui.link_handler import (
    LinkHandler,
    UriLauncherFactory,
    default_launcher_factory,
)
from giruntime.ui.note_render.tag_table import TagName, build_tag_table
from giruntime.ui.note_render.textbuffer_renderer import TextBufferRenderer
from system_docs import load_bytes, load_text


# ---------------------------------------------------------------------------
# Window-level constants
# ---------------------------------------------------------------------------

_WINDOW_TITLE: str = "Folio Help"
"""Title shown in the help window's title bar."""

_DEFAULT_WINDOW_WIDTH_PX: int = 760
_DEFAULT_WINDOW_HEIGHT_PX: int = 680
"""Initial size of the help window. Wide enough for the fixed reading
column plus the contents sidebar; tall enough to read a section without
immediate scrolling."""

_SIDEBAR_WIDTH_PX: int = 200
"""Initial width of the contents sidebar pane."""

_HELP_NOTE_ID: str = "help"
"""Synthetic ``note_id`` handed to the renderer.

The renderer's :meth:`TextBufferRenderer.render_into` takes a ``note_id``
for future caching/diagnostics; the help is not a database note, so a
stable synthetic id documents the call site.
"""

_TEXT_VIEW_HMARGIN_PX: int = 24
_TEXT_VIEW_VMARGIN_PX: int = 18
"""Breathing-space margins around the rendered help text."""

_MEASUREMENT_GLYPH: str = "M"
"""Reference glyph for the body-font width measurement.

Matches the rendered note view's measurement glyph so the help's tag
table (whose block margins encode an ``M``-width inset) is built against
the same metric the renderer assumes.
"""


def _section_mark_name(section: HelpSection) -> str:
    """Return the :class:`Gtk.TextMark` name for a help section.

    Keyed off the enum member's stable :attr:`name`, so the mark a
    section scrolls to is uniquely and deterministically named without a
    second registry to keep in sync with :class:`HelpSection`.
    """
    return f"help-section-{section.name.lower()}"


class HelpWindow(  # pylint: disable=too-many-instance-attributes
    Gtk.ApplicationWindow,
):
    """A non-modal window rendering the AsciiDoc help reference.

    Construction renders the help source into a read-only text view,
    drops a navigation mark at each top-level section heading, and wires
    the contents sidebar so selecting a bucket scrolls to it. The window
    is built once and reused by the application (reuse-and-raise).

    Injectable seams (all defaulted for production):

    * ``launcher_factory`` — the :data:`UriLauncherFactory` the installed
      :class:`LinkHandler` uses to open example links. Tests pass a
      recording fake; production uses :func:`default_launcher_factory`.

    The instance-attribute count exceeds pylint's default ceiling of
    seven because rendering the help end-to-end legitimately needs the
    buffer, the view, the tag table, the renderer, the link handler, the
    image-bytes map, the fixed column width, and the navigation triple
    (the ordered sections, their marks, and the contents list). Splitting
    them into a helper would obscure the plain "the window holds what it
    needs to render and navigate" relationship — the same trade-off
    :class:`giruntime.ui.note_view.NoteView` makes.
    """

    _buffer: Gtk.TextBuffer
    _text_view: Gtk.TextView
    _tag_table: Gtk.TextTagTable
    _renderer: TextBufferRenderer
    _link_handler: LinkHandler
    _image_bytes: dict[str, bytes]
    _column_width_px: int
    _sections: tuple[HelpSection, ...]
    _section_marks: dict[HelpSection, Gtk.TextMark]
    _contents_list: Gtk.ListBox

    def __init__(
        self,
        *,
        application: Gtk.Application,
        launcher_factory: UriLauncherFactory = default_launcher_factory,
    ) -> None:
        super().__init__(application=application)
        self.set_title(_WINDOW_TITLE)
        self.set_default_size(
            _DEFAULT_WINDOW_WIDTH_PX,
            _DEFAULT_WINDOW_HEIGHT_PX,
        )
        # Non-modal is the default for a top-level window; stated
        # explicitly because it is load-bearing (see the module docstring).
        self.set_modal(False)

        # The image map is built before the renderer so the resolver
        # closure can read it. The help bundles exactly one demo image.
        self._image_bytes = {
            SystemDocument.HELP_DEMO_IMAGE.value: load_bytes(
                SystemDocument.HELP_DEMO_IMAGE,
            ),
        }

        # ----- Read-only rendered view -----
        self._text_view = Gtk.TextView.new()
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_left_margin(_TEXT_VIEW_HMARGIN_PX)
        self._text_view.set_right_margin(_TEXT_VIEW_HMARGIN_PX)
        self._text_view.set_top_margin(_TEXT_VIEW_VMARGIN_PX)
        self._text_view.set_bottom_margin(_TEXT_VIEW_VMARGIN_PX)
        self._text_view.set_hexpand(True)
        self._text_view.set_vexpand(True)

        # Measure the body font once; the tag table's block margins and
        # the fixed reading column both derive from it.
        char_width_px = self._measure_char_width_px()
        self._column_width_px = TARGET_CHARS_PER_LINE * char_width_px

        self._tag_table = build_tag_table(char_width_px=char_width_px)
        self._buffer = Gtk.TextBuffer.new(self._tag_table)
        self._text_view.set_buffer(self._buffer)

        # ----- Renderer (shared pipeline) -----
        self._renderer = TextBufferRenderer(
            image_bytes_for=self._resolve_image_bytes,
            column_width_px=self._text_column_width,
            tag_table=self._tag_table,
        )

        # ----- Live example links -----
        self._link_handler = LinkHandler(
            text_view=self._text_view,
            renderer=self._renderer,
            launcher_factory=launcher_factory,
        )
        self._link_handler.install()

        # ----- Render the help, then place navigation marks -----
        self._renderer.render_into(
            load_text(SystemDocument.HELP),
            self._buffer,
            note_id=_HELP_NOTE_ID,
            attach_widget=self._attach_child_widget,
        )
        self._sections = tuple(HelpSection)
        self._section_marks = self._place_section_marks()

        # ----- Two-pane layout: contents sidebar | rendered view -----
        self._contents_list = self._build_contents_list()
        self.set_child(self._build_layout())

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _measure_char_width_px(self) -> int:
        """Return the body font's reference-glyph width in pixels.

        Lays out :data:`_MEASUREMENT_GLYPH` in the text view's Pango
        context (available at construction, before realisation) and
        returns its logical pixel width — the same metric the rendered
        note view measures, so the shared tag table's ``M``-width insets
        match what the renderer assumes.
        """
        layout: Pango.Layout = self._text_view.create_pango_layout(
            _MEASUREMENT_GLYPH,
        )
        _, log_rect = layout.get_pixel_extents()
        return int(log_rect.width)

    def _build_contents_list(self) -> Gtk.ListBox:
        """Build the contents sidebar listing the top-level buckets.

        One row per :class:`HelpSection`, in declaration order (which is
        document order). Activating a row scrolls the buffer to that
        section's mark. The first row starts selected so the sidebar
        always reflects a current position.
        """
        contents = Gtk.ListBox.new()
        contents.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for section in self._sections:
            row = Gtk.ListBoxRow.new()
            label = Gtk.Label.new(section.value)
            label.set_xalign(0.0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(12)
            label.set_margin_end(12)
            row.set_child(label)
            contents.append(row)
        contents.connect("row-activated", self._on_contents_row_activated)
        first_row = contents.get_row_at_index(0)
        if first_row is not None:
            contents.select_row(first_row)
        return contents

    def _build_layout(self) -> Gtk.Paned:
        """Compose the sidebar and the scrolled rendered view.

        A :class:`Gtk.Paned` lets the reader widen either pane; the
        sidebar starts at :data:`_SIDEBAR_WIDTH_PX`. Both panes scroll
        independently.
        """
        sidebar_scroller = Gtk.ScrolledWindow.new()
        sidebar_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        sidebar_scroller.set_child(self._contents_list)

        text_scroller = Gtk.ScrolledWindow.new()
        text_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        text_scroller.set_child(self._text_view)
        text_scroller.set_hexpand(True)
        text_scroller.set_vexpand(True)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(sidebar_scroller)
        paned.set_end_child(text_scroller)
        paned.set_position(_SIDEBAR_WIDTH_PX)
        # The sidebar should not vanish or swallow the reading column.
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        return paned

    def _place_section_marks(self) -> dict[HelpSection, Gtk.TextMark]:
        """Drop one navigation mark per top-level section heading.

        Walks the rendered buffer line by line. A line carrying the
        shared :data:`TagName.HEADING_2` tag at its start is a top-level
        section heading; its text is matched against the
        :class:`HelpSection` values, and a left-gravity mark is created
        at the line start. The match keys both the nav list and the
        scroll targets off the same enum, so they cannot drift.

        Raises :class:`ValueError` if any member ends up without a mark
        (a heading was renamed or removed in ``help.adoc``) — a
        build-time failure rather than a silently dead nav row.
        """
        buffer = self._buffer
        heading_tag = self._tag_table.lookup(TagName.HEADING_2.value)
        by_text = {section.value: section for section in HelpSection}
        marks: dict[HelpSection, Gtk.TextMark] = {}
        for line_no in range(buffer.get_line_count()):
            found, line_start = buffer.get_iter_at_line(line_no)
            if not found:
                continue
            if heading_tag is None or not line_start.has_tag(heading_tag):
                continue
            line_end = line_start.copy()
            if not line_end.ends_line():
                line_end.forward_to_line_end()
            text = buffer.get_text(line_start, line_end, False).strip()
            section = by_text.get(text)
            if section is None or section in marks:
                continue
            marks[section] = buffer.create_mark(
                _section_mark_name(section),
                line_start,
                True,
            )
        missing = [s for s in HelpSection if s not in marks]
        if missing:
            names = ", ".join(s.name for s in missing)
            raise ValueError(
                f"help.adoc is missing a heading for section(s): {names}",
            )
        return marks

    # ------------------------------------------------------------------
    # Renderer wiring
    # ------------------------------------------------------------------

    def _attach_child_widget(
        self,
        anchor: Gtk.TextChildAnchor,
        widget: Gtk.Widget,
    ) -> None:
        """Adapter for the renderer's ``WidgetAttacher`` contract.

        The renderer passes ``(anchor, widget)``; GTK 4's
        :meth:`Gtk.TextView.add_child_at_anchor` takes ``(widget,
        anchor)``. Only tables reach this path. Mirrors the note view's
        adapter so the order swap is hidden from the renderer.
        """
        self._text_view.add_child_at_anchor(widget, anchor)

    def _resolve_image_bytes(self, filename: str) -> bytes:
        """Serve the help's demo image bytes by filename.

        The :data:`ImageBytesResolver` the renderer calls for every
        ``image::`` macro. The help bundles its images, so this returns
        real, decodable bytes. A filename the help does not bundle is a
        help-authoring bug — the :class:`KeyError` from the lookup
        propagates rather than masking it as a grey placeholder.
        """
        return self._image_bytes[filename]

    def _text_column_width(self) -> int:
        """Return the fixed reading-column width in pixels.

        The :data:`ColumnWidthResolver` the renderer consults for image
        and table sizing. A fixed value (body-font width ×
        :data:`config.defaults.TARGET_CHARS_PER_LINE`) keeps layout
        stable regardless of whether the window is realised at render
        time.
        """
        return self._column_width_px

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_contents_row_activated(
        self,
        _list_box: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        """Scroll to the section a sidebar row stands for."""
        index = row.get_index()
        if 0 <= index < len(self._sections):
            self.scroll_to_section(self._sections[index])

    def scroll_to_section(self, section: HelpSection) -> None:
        """Scroll the rendered view so ``section``'s heading is at the top.

        Public so tests can drive navigation without synthesising a row
        click. Uses the section's mark and top-aligns it.
        """
        mark = self._section_marks[section]
        self._text_view.scroll_to_mark(mark, 0.0, True, 0.0, 0.0)

    # ------------------------------------------------------------------
    # Read-only properties exposed for tests
    # ------------------------------------------------------------------

    @property
    def text_view(self) -> Gtk.TextView:
        """The read-only view rendering the help."""
        return self._text_view

    @property
    def buffer(self) -> Gtk.TextBuffer:
        """The buffer the help is rendered into."""
        return self._buffer

    @property
    def contents_list(self) -> Gtk.ListBox:
        """The contents sidebar list box."""
        return self._contents_list

    @property
    def section_marks(self) -> dict[HelpSection, Gtk.TextMark]:
        """The per-section navigation marks (one per :class:`HelpSection`)."""
        return dict(self._section_marks)

    @property
    def rendered_text(self) -> str:
        """The full plain text of the rendered help buffer."""
        text: str = self._buffer.get_text(
            self._buffer.get_start_iter(),
            self._buffer.get_end_iter(),
            False,
        )
        return text

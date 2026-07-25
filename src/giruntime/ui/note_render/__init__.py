"""A parsed AsciiDoc document's *appearance* as GTK.

Charter
-------
Given a parsed :class:`~asciidoc.ast.Document` and the resolvers it needs
(image bytes, column width, attachment list), this sub-package delivers the
document's complete **visual presentation** — the styled buffer content
**and** the on-buffer painting that finishes it — up to but **not**
including how that content is sized, scrolled, or placed on screen. The
column geometry and scrolling are the pane's: they live in
:mod:`giruntime.ui.article_container` and are assembled with the renderer by
:func:`giruntime.ui.note_view.build_article_surface`.

Modules
-------
* :mod:`~ui.note_render.tag_table` — every visual style, defined exactly once.
* :mod:`~ui.note_render.textbuffer_renderer` — the AST → ``Gtk.TextBuffer``
  builder (no construct escapes to a widget).
* :mod:`~ui.note_render.attachment_table` — the pure ``attachments::[]``
  AST → AST expansion.
* :mod:`~ui.note_render.article_text_view` — the read view that *paints* the
  tag table's block-tint washes and the note sheet; the appearance completion
  of the pipeline, living with the :class:`WashSpec` / :class:`SheetWash` it
  consumes.

These live under ``ui`` because they are the only consumers that need ``gi``
and ``storage.protocols``; keeping them here lets :mod:`asciidoc` stay a pure,
GTK-free format library. The sub-package imports nothing upward from ``ui`` and
touches no concrete ``storage``. The renderer depends on a column width only
abstractly, through an injected
:data:`~storage.protocols.ColumnWidthResolver`, so it never names the
container that supplies it.
"""

from __future__ import annotations

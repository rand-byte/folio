"""Centralised enums for the entire application.

Principles & invariants
-----------------------
* This module is the single home for every categorical constant the
  application uses. It exists so that no other module must define string
  literals or magic numbers for these concepts, and so that imports of
  shared categorical types never go through a higher layer.
* The module imports nothing from the rest of the package — keeping it
  free of cycles is what lets every other layer depend on it.
* Every enum has stable, finite, well-known membership. Adding a member is
  a deliberate change that ripples to parsers, renderers, and storage; it
  is never a hotfix shortcut.
* Enums whose values are persisted (``LinkScheme``, ``AdmonitionKind``)
  inherit from ``StrEnum`` and use explicit values
  that match what is written to disk or to source. Their values must
  never change once shipped — migrations would be required.
* Enums whose values are purely in-memory may use ``auto()``; their
  on-disk representation is undefined.
"""

from __future__ import annotations

from enum import Enum, StrEnum, auto


class ViewMode(StrEnum):
    """Whether the right pane shows the rendered article or the source."""

    VIEW = auto()
    EDIT = auto()


class HeaderCentrePage(StrEnum):
    """Named pages of the header bar's centre stack.

    The toolbar's centre is a two-page :class:`Gtk.Stack` — the current
    note's title, or the expanded search entry. Stack children are
    addressed by name in :meth:`Gtk.Stack.set_visible_child_name`, so
    each page gets a stable member here rather than a string literal at
    the call sites. In-memory only, never persisted.
    """

    TITLE = auto()
    SEARCH = auto()


class NoteSortKey(StrEnum):
    """Sort keys exposed by the note-list dropdown."""

    MODIFIED = auto()
    CREATED = auto()
    TITLE = auto()


class SmartFilter(StrEnum):
    """Built-in filters that surface notes across all tags."""

    ALL = auto()
    UNTAGGED = auto()


class SelectionKind(StrEnum):
    """Discriminator for what is currently selected in the sidebar."""

    SMART = auto()
    TAG = auto()


class NodeKind(StrEnum):
    """Discriminator for AST nodes in :mod:`asciidoc.ast`.

    Block kinds describe document structure; inline kinds describe spans
    inside a single line of text. Tooling that walks the AST may switch on
    this kind rather than ``isinstance`` so that adding a new node type
    forces every walker to be updated.
    """

    # Document root
    DOCUMENT = auto()
    # Block-level
    SECTION = auto()
    PARAGRAPH = auto()
    LIST_ORDERED = auto()
    LIST_UNORDERED = auto()
    LIST_ITEM = auto()
    CODE_BLOCK = auto()
    IMAGE = auto()
    TABLE = auto()
    TABLE_ROW = auto()
    TABLE_CELL = auto()
    ADMONITION = auto()
    BLOCKQUOTE = auto()
    # Inline
    TEXT = auto()
    BOLD = auto()
    ITALIC = auto()
    STRIKETHROUGH = auto()
    UNDERLINE = auto()
    MONOSPACE = auto()
    LINK = auto()


class TokenKind(StrEnum):
    """Lexer token classifications used by :mod:`asciidoc.lexer`."""

    HEADING = auto()
    LIST_BULLET = auto()
    LIST_NUMBER = auto()
    CODE_FENCE = auto()
    CODE_DIRECTIVE = auto()
    IMAGE_MACRO = auto()
    TABLE_FENCE = auto()
    COLS_DIRECTIVE = auto()
    ADMONITION_DIRECTIVE = auto()
    ADMONITION_FENCE = auto()
    SINGLE_ADMONITION = auto()
    QUOTE_DIRECTIVE = auto()
    QUOTE_FENCE = auto()
    ATTRIBUTE_ENTRY = auto()
    BLANK = auto()
    LINE = auto()


class ParseErrorKind(StrEnum):
    """Discriminator for parser-raised :class:`ParseError`s.

    Each kind maps to exactly one syntactic failure mode in the AsciiDoc
    subset described in the implementation plan. The UI may render a
    different help message per kind without re-parsing the message string.
    """

    UNTERMINATED_CODE_BLOCK = auto()
    UNKNOWN_BLOCK = auto()
    BAD_IMAGE_MACRO = auto()
    BAD_INLINE_SPAN = auto()
    EMPTY_HEADING = auto()
    UNTERMINATED_TABLE = auto()
    EMPTY_TABLE = auto()
    TABLE_ROW_ARITY_MISMATCH = auto()
    BAD_COLS_DIRECTIVE = auto()
    UNTERMINATED_ADMONITION = auto()
    UNKNOWN_ADMONITION_TYPE = auto()
    UNTERMINATED_BLOCKQUOTE = auto()
    BAD_BLOCKQUOTE_DIRECTIVE = auto()
    UNSUPPORTED_LINK_SCHEME = auto()
    BAD_LINK_MACRO = auto()
    UNTERMINATED_MONOSPACE = auto()
    UNTERMINATED_PASSTHROUGH = auto()
    BAD_ATTRIBUTE_ENTRY = auto()
    BLOCK_INSIDE_INLINE_ONLY_CONTAINER = auto()
    BAD_TAG_VALUE = auto()
    DUPLICATE_TAG_ATTRIBUTE = auto()
    LIST_STARTS_BELOW_TOP_LEVEL = auto()
    LIST_NESTING_SKIPS_LEVEL = auto()
    LIST_NESTING_TOO_DEEP = auto()


class HeadingTrailing(StrEnum):
    """Trailing whitespace a heading emits after its text.

    Consumed by the GTK ``TextBuffer`` renderer to decide how many
    newline characters follow a heading's text in the buffer. A
    body-section heading emits the full block separator so the next
    block drops a blank line below it; the document **title** emits a
    single newline so the tag-chip row can hug it on the very next
    line, the renderer then completing the block gap *after* the chip
    anchor. The values are the literal separator strings — the renderer
    inserts ``member.value`` verbatim — and, like ``AdmonitionKind``'s
    header text, are used directly rather than translated. They are
    never persisted, so no migration is implied by their content.
    """

    BLOCK_SEPARATOR = "\n\n"
    SINGLE_NEWLINE = "\n"


class ListNumberStyle(Enum):
    """The marker style an ordered list uses at a given nesting depth.

    The GTK ``TextBuffer`` renderer keeps a depth-indexed table mapping
    each ordered-list nesting level to one of these styles — arabic at
    level 1, lower-alpha at level 2, lower-roman at level 3 — and a
    ``_format_ordinal`` helper turns a 1-based item index into the visible
    ordinal (``1.`` / ``a.`` / ``i.``) by ``match``-ing on the style. It is
    an enum rather than a bare table so the renderer's ``match`` is
    exhaustive: adding a style forces every consumer to handle it.

    Plain :class:`enum.Enum` with :func:`auto` values because the style is
    a purely in-memory presentation choice — it is never persisted, so its
    on-disk representation is undefined and no migration is implied.
    """

    ARABIC = auto()
    LOWER_ALPHA = auto()
    LOWER_ROMAN = auto()


class WashShape(Enum):
    """Paint shape for a block-level wash behind a rendered paragraph.

    The rendered-view wash painter (``ArticleTextView`` in
    :mod:`ui.note_view`) walks the buffer one logical line at a time and,
    for each line whose first iter carries a wash-bearing tag, paints one
    coloured rectangle. This enum selects which rectangle: a full tinted
    card behind admonitions / code blocks / the table header, a thin 1-px
    rule at the bottom of the line for the metadata line and table data
    rows, or a thin vertical rule at the left edge for blockquotes. It is
    an enum rather than a boolean (or a second boolean bolted on) so the
    painter's shape dispatch is exhaustive: adding a shape forces every
    consumer — the painter and the :class:`WashSpec` construction sites —
    to handle it.

    Plain :class:`enum.Enum` with :func:`auto` values because the shape is
    a purely in-memory presentation choice — it is never persisted, so its
    on-disk representation is undefined and no migration is implied.
    """

    FILL = auto()       # full tinted card (admonition, code, table header)
    HAIRLINE = auto()   # 1-px bottom rule (metadata line, table data row)
    LEFT_BAR = auto()   # thin left vertical rule, no fill (blockquote)


class AdmonitionKind(StrEnum):
    """The five admonition labels recognised by the parser.

    Values are exactly the case-sensitive tokens that appear in source —
    e.g. ``NOTE: foo`` or ``[NOTE]\\n====\\n…\\n====``. The renderer also
    uses the value directly as the visible header text.
    """

    NOTE = "NOTE"
    TIP = "TIP"
    IMPORTANT = "IMPORTANT"
    WARNING = "WARNING"
    CAUTION = "CAUTION"


class LinkScheme(StrEnum):
    """The allow-listed URL schemes for embedded links.

    Both the parser (rejection of out-of-list schemes) and the link launcher
    (refusal to hand a URL to ``Gtk.UriLauncher``) consult this enum. Values
    match the lowercase scheme as it appears in URLs, so membership testing
    is a direct ``LinkScheme(scheme)`` call.
    """

    HTTP = "http"
    HTTPS = "https"
    MAILTO = "mailto"


class AttachmentRejectionReason(Enum):
    """Why an attachment add was refused.

    Carried by :class:`AttachmentRejected` (defined in the storage layer
    later in the build). Plain :class:`enum.Enum` rather than ``StrEnum``
    because the value is never persisted — it is only ever consumed by the
    UI to choose a toast message.
    """

    EXCEEDS_SIZE_LIMIT = auto()
    UNREADABLE_SOURCE = auto()


class SystemDocument(StrEnum):
    """The application's bundled "system documents".

    These are the texts and the one image the application ships as
    package data (not user content): the seed welcome note inserted on
    first launch, and the AsciiDoc help reference (plus the small image
    its ``image::`` example demonstrates). They live in the
    ``system_docs`` package and are read gi-free via
    :func:`importlib.resources` by the shared loader in
    :mod:`system_docs`.

    Each member's *value is the package-relative filename* the loader
    joins onto the ``system_docs`` package — so the enum is the single
    home for "which file backs which system document". The values are not
    persisted to disk (they only locate package data at runtime), but
    they are stable: the files ship under exactly these names, and
    renaming one is a deliberate change to both the file and this member.
    """

    WELCOME = "welcome.adoc"
    HELP = "help.adoc"
    HELP_DEMO_IMAGE = "help-demo.png"


class HelpSection(StrEnum):
    """The navigable top-level sections of the AsciiDoc help reference.

    The help window renders a single scrolling page whose top-level
    buckets match the parser's structure/inline/block split. Both the
    contents-sidebar entries **and** the ``Gtk.TextMark`` placed at each
    section's heading are keyed off this enum, so the navigation list and
    the scroll targets cannot drift: a member with no matching heading (or
    a heading matching no member) is caught at window-build time.

    Each member's *value is the exact heading text* it labels in
    ``help.adoc`` — the contents sidebar shows it as the row label, and
    the post-render mark-placement pass matches a rendered level-2 heading
    line against it. The order of declaration is the order the rows appear
    in the sidebar, matching the document order of the headings. The
    values are in-memory only (never persisted), so they carry no
    migration implication.
    """

    STRUCTURE = "Structure"
    TEXT_AND_EMPHASIS = "Text & emphasis"
    BLOCKS = "Blocks"


class GResourceSubtree(StrEnum):
    """``resource://`` subtrees published by the compiled ``folio.gresource``.

    ``folio.gresource`` is one compiled artifact but bundles more than
    one thing at runtime — today the GtkSourceView grammar and the
    application icon — each published under its own ``prefix`` in
    ``giruntime/ui/folio.gresource.xml``. This enum is the single home
    for "what does the bundle contain and where does each part live":
    a new bundled subtree needs a member here *and* the matching
    ``<gresource prefix=...>`` in the manifest, so a consumer can never
    hardcode a path the manifest does not actually publish.

    Each member's *value is the resource path itself*, in whatever form
    the GTK API that consumes it expects — :class:`LANGUAGE_SPECS` is a
    full ``resource:///`` URI (what
    :meth:`GtkSource.LanguageManager.set_search_path` takes), while
    :class:`ICONS` is a bare resource path with no ``resource://``
    scheme (what :meth:`Gtk.IconTheme.add_resource_path` takes) — this
    is a real difference between the two GTK APIs, not an
    inconsistency to paper over. Values are in-memory only (never
    persisted), so they carry no migration implication; they must,
    however, stay in sync with the manifest's ``prefix`` attributes.
    """

    LANGUAGE_SPECS = "resource:///org/folio/language-specs"
    ICONS = "/org/folio/icons"

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
* Enums whose values are persisted (``NotebookIcon``, ``MimeKind``,
  ``LinkScheme``, ``AdmonitionKind``) inherit from ``StrEnum`` and use
  explicit values that match what is written to disk or to source. Their
  values must never change once shipped — migrations would be required.
* Enums whose values are purely in-memory may use ``auto()``; their
  on-disk representation is undefined.
"""

from __future__ import annotations

from enum import Enum, StrEnum, auto


class ViewMode(StrEnum):
    """Whether the right pane shows the rendered article or the source."""

    VIEW = auto()
    EDIT = auto()


class NoteSortKey(StrEnum):
    """Sort keys exposed by the note-list dropdown."""

    MODIFIED = auto()
    CREATED = auto()
    TITLE = auto()


class SmartFilter(StrEnum):
    """Built-in filters that surface notes across all notebooks."""

    ALL = auto()
    RECENT = auto()


class SelectionKind(StrEnum):
    """Discriminator for what is currently selected in the sidebar."""

    SMART = auto()
    NOTEBOOK = auto()


class NotebookIcon(StrEnum):
    """Symbolic icon attached to a notebook.

    Values are persisted in the ``notebooks.icon`` column. Adding members is
    fine; renaming or removing requires a schema migration.
    """

    HOME = auto()
    BOOK = auto()
    MAP = auto()
    BRAIN = auto()
    ARCHIVE = auto()
    BRIEFCASE = auto()
    HEART = auto()
    STAR = auto()
    FOLDER = auto()
    INBOX = auto()
    GRADUATION_CAP = auto()


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


class MimeKind(StrEnum):
    """Image MIME types accepted by the attachment store.

    Values are the canonical MIME strings so they can be written verbatim to
    the ``attachments.mime_type`` column and read back without translation.
    """

    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"
    GIF = "image/gif"


class AttachmentRejectionReason(Enum):
    """Why an attachment add was refused.

    Carried by :class:`AttachmentRejected` (defined in the storage layer
    later in the build). Plain :class:`enum.Enum` rather than ``StrEnum``
    because the value is never persisted — it is only ever consumed by the
    UI to choose a toast message.
    """

    EXCEEDS_SIZE_LIMIT = auto()
    UNSUPPORTED_MIME_TYPE = auto()
    UNREADABLE_SOURCE = auto()

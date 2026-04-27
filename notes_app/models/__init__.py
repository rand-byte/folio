"""Pure data classes shared across the application.

Modules here define the immutable shapes (`Note`, `Notebook`, `Attachment`,
`ParseError`) that flow between storage, controllers, and UI. They never
import from `storage/`, `controllers/`, `ui/`, or `asciidoc/` — so a
dataclass change cannot accidentally drag in `gi` or `sqlite3`.
"""

# folio — Developer Guide

A GTK 4 / Python 3.13 note-taking application backed by SQLite. Notes are
written in a strict subset of AsciiDoc and rendered into a `Gtk.TextBuffer`.

This README is a **navigation map**: read it to find the right file, then open
that file. Every non-trivial module opens with a `"""Principles & invariants`
docstring that states the rules it must obey — **that docstring is the source
of truth for the module's contract; this README only points you at it.** So
the entries below say *what a module is for* and *where to start for a task*,
not *how it works internally*.

---

## 1. Run, test, lint

| Goal | Command |
| --- | --- |
| Launch app | `./run` (dev — builds the grammar resource, then runs `python3 -B src/__main__.py`) or `python folio.pyz` (distributed zipapp) |
| Run all tests | `make test` (preferred — builds the grammar resource and provides a headless display) or `python3 -B -m unittest discover -s src -t src -v` when a display is already available |
| Type-check | `mypy src` — requires **`mypy >= 1.16`** (earlier releases mis-widen `StrEnum` members to `str`, [python/mypy#18587](https://github.com/python/mypy/pull/18587)); pinned in `pyproject.toml`'s dev group |
| Lint (non-test) | `PYTHONPATH=src pylint --disable=missing-module-docstring,missing-function-docstring,missing-class-docstring --enable=useless-suppression --min-public-methods=1 src` |
| Lint (test files) | additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines` |

**System packages:** `gir1.2-gtk-4.0`, `gir1.2-gtksource-5` (GtkSourceView **≥ 5.4**, see §7) plus equivalents elsewhere, and `glib-compile-resources` (ships with the GLib dev tooling) to build the editor grammar bundle. Python **≥ 3.13**. The only Python runtime dependency is `PyGObject>=3.50`; SQLite is in the standard library.

**Full headless suite** (e.g. CI) additionally needs `weston`: the widget-level UI tests are gated behind a display guard and only run when a GDK display can be opened. `make test` supplies one by launching a headless Weston compositor (see §5). Without a display those tests **skip rather than fail**, so a plain `unittest` run with no display can report `OK` while exercising none of the GTK widgets.

---

## 2. Layered architecture

Layers may only import **downward**. There are no cycles; the import table at
the end of this section is the enforcement boundary.

```
                  ┌──────────────────────────────────┐
        UI ───────│ giruntime/ui (GTK 4)             │  imports gi at runtime
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
   controllers ───│ giruntime/controllers            │  no widgets, no SQL
                  └────────┬────────────────┬────────┘
                           ▼                ▼
                  ┌────────────────┐  ┌───────────────────┐
   pure helpers ──│ search/        │  │ storage.protocols │  ← Protocol classes only
                  └───────┬────────┘  └─────────┬─────────┘
                          │      ┌──────────────┘
                          ▼      ▼
                  ┌──────────────────────────────────┐
      storage ────│ storage (SQLite impls)           │  imports sqlite3 + asciidoc
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
    asciidoc  ────│ asciidoc (pure core)             │  text→AST→summary; no GTK,
   (text→AST)     │   lexer → inline_parser → parser │  no storage.
   (AST→summary)  │   → ast → summary                │
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
      models   ───│ models (frozen data)             │  pure dataclasses
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
      enums    ───│ enums                            │  no internal imports
                  └──────────────────────────────────┘
              ┌──────────────────────────────────────┐
      config  │ config (constants, paths)            │  used by storage / ui
              └──────────────────────────────────────┘
```

The GTK `TextBuffer` renderer and tag table live under `giruntime/ui/note_render/`
(not in `asciidoc/`), so the whole `asciidoc/` core stays a pure format library.

| Layer | May import | May **not** import |
| --- | --- | --- |
| `enums` | nothing internal | anything else (it must stay leaf) |
| `models` | `enums` | `storage`, `controllers`, `ui`, `asciidoc`, `search` |
| `config` | `enums`, `models` | `storage`, `controllers`, `ui`, `asciidoc` |
| `system_docs` | `enums` (+ stdlib `importlib.resources`) | `storage`, `controllers`, `ui`, `gi`, `asciidoc` |
| `asciidoc` (`ast`, `lexer`, `inline_parser`, `parser`, `summary`) | `enums`, `models`, `config` | `storage`, `controllers`, `ui`, `gi`, `storage.protocols` |
| `storage.protocols` | `enums`, `models` (uses `gi` only under `TYPE_CHECKING`) | everything else |
| `storage` (concrete) | `enums`, `models`, `config`, `system_docs`, `storage.protocols`, `sqlite3`, `asciidoc` (pure core) | `gi`, `controllers`, `ui` |
| `search` | `enums`, `models` | `storage` (concrete), `controllers`, `ui`, `gi` |
| `giruntime/controllers` | `enums`, `models`, `config`, `search`, `storage.protocols`, `gi` (`GObject` / `Gio`) | concrete `storage`, `giruntime/ui`, **`asciidoc`** |
| `giruntime/ui` | everything below | — |

**`gi` (GTK) is configured once** in `giruntime/__init__.py` (the sole
`gi.require_version` site) **and consumed only under `giruntime/ui/*` and
`giruntime/controllers/*`.** Anywhere else — including all of `asciidoc/*` — it
is a bug.

---

## 3. "Where do I touch to do X?"

Each row points at the file/symbol to start from. Open that file's docstring
for the rules; open its `test_*.py` sibling for the behaviour it must keep.

| Change | Start here | Likely also touches |
| --- | --- | --- |
| Add a new enum value | `enums.py` | consumers that pattern-match the enum; a `Migration` for `StrEnum`s persisted to disk |
| Add a new AsciiDoc construct | `asciidoc/ast.py` (node) → `lexer.py` → `parser.py` → `note_render/textbuffer_renderer.py` → `note_render/tag_table.py` (tag) → `giruntime/ui/language_spec.lang` (highlight) → `asciidoc/summary.py` (snippet treatment). The `match` ladders in the renderer and `summary.py` are exhaustive, so an unhandled kind is a type error. | `enums.py` (`NodeKind`, maybe `ParseErrorKind` / a presentation enum) |
| Add a parse-error variant | `enums.py` `ParseErrorKind` → the parser site that raises it | `giruntime/ui/note_view._message_for` (exhaustive) + `_insert_error_notice` |
| Change DB schema | append a **new** `Migration` to `storage/migrations.py` `ALL_MIGRATIONS` — never edit a shipped one | the repository that reads/writes the new column |
| Add a note-level user action | `giruntime/controllers/note_controller.py` (call the store, wrap in `capturing_storage_errors`, mutate `AppState`) | its UI caller (`toolbar.py` / `note_editor.py` / `attachments_panel.py`); `note_list_store.py`; the repository protocol |
| Change persistence ordering | `giruntime/controllers/note_list_store.py` — the **DB-first** invariant lives here | `test_note_list_store.py` `DbFirstTests` |
| Change tag parsing / validation | `asciidoc/parser.py` `parse_tags_value` (shared by the parser and `summary.py`'s fallback) | `test_parser.py` / `test_summary.py`; a re-derive `Migration` if existing notes reparse |
| Change rendered-view styling | `giruntime/ui/note_render/tag_table.py` — every visual style lives here exactly once (tints, insets, wash shapes, sheet colour) | `test_tag_table.py`; rarely `textbuffer_renderer.py` for layout |
| Change block tint colours / insets | `giruntime/ui/note_render/tag_table.py` (the tint/inset constants + `WashSpec`s) | `test_tag_table.py`, `test_note_view.py` wash-rect tests |
| Change body-heading vertical spacing | `giruntime/ui/note_render/tag_table.py` (`_make_heading_tag`) | `test_tag_table.py`; `test_textbuffer_renderer.py` |
| Tune article column margins | `config/defaults.py` (the `ARTICLE_*` multipliers) | none — `note_view.py` reads them once at `NoteView.__init__` |
| Change rendered-view layout / scrolling | `giruntime/ui/note_view.py` `ArticleContainer` (a `Gtk.Widget` + `Gtk.Scrollable`) | `test_note_view.py` `ArticleContainer*` tests |
| Change the under-title metadata line | `giruntime/ui/note_view.py` (`_insert_metadata_after_title`) + `note_render/tag_table.py` (`TagName.METADATA`); dates in `giruntime/ui/_dates.py` | `test_note_view.py`, `test_textbuffer_renderer.py` |
| Change application chrome / CSS | `giruntime/ui/css/app.css` | none — the zipapp archives `src/` directly, so new assets ship automatically |
| Change the application icon | `giruntime/ui/icons/scalable/apps/org.folio.Folio.svg` (the file name **is** the icon name) | `folio.gresource.xml` + `Makefile` only if adding/renaming a size variant |
| Change the initial window size | `giruntime/ui/main_window.py` (used only when no size was restored) | `test_main_window.py`, `test_note_view.py` column-width tests |
| Change restored session state | `models/session_state.py` → `storage/session_state_store.py` (bump `_SCHEMA_VERSION`) → `giruntime/ui/application.py` → `main_window.py`. No window-position restore (GTK 4 has no API). | `storage/test_session_state_store.py`; `test_application.py`; `test_main_window.py` |
| Change source-editor syntax highlight | `giruntime/ui/language_spec.lang` | rebuild the resource (`./run` / `make resource` do this); the `.xml` manifest only if adding/renaming grammar files |
| Tune a constant (sizes, quotas) | `config/defaults.py` | none — that is the point of this module |
| Change paths / XDG behaviour | `config/paths.py` | `config/test_paths.py` |
| Add a sort key / smart filter | `enums.py` (`NoteSortKey` / `SmartFilter`) → `search/note_filter.py` → `giruntime/ui/note_list.py` and/or `sidebar.py` | `search/test_note_filter.py` |
| Change note-list row title/snippet | derivation in `asciidoc/summary.py`; presentation in `giruntime/ui/note_list.py` + `css/app.css` | `storage/note_repository.py` if the cached-column contract changes |
| Change the sidebar Tags section | `giruntime/controllers/tag_counts_model.py` + `giruntime/ui/sidebar.py` | `test_tag_counts_model.py`; `test_sidebar.py` |
| Change selection / view-mode plumbing | `giruntime/controllers/app_state.py` (a GObject property + rule-bearing mutator). `MainWindow._on_view_mode_changed` is the single view-mode orchestrator. | every UI widget that subscribes via `notify::<prop>` |
| Add a new dialog | `giruntime/ui/dialogs.py` | its opener |
| Change link/URL handling | `giruntime/ui/link_handler.py`; allowlist in `enums.LinkScheme` | `asciidoc/inline_parser.py` for scheme validation |
| Change attachment rules | `storage/attachment_store.py`; size cap in `config/defaults.MAX_ATTACHMENT_BYTES` | `giruntime/controllers/note_controller.py` for toast wiring |
| Change the attachments panel | `giruntime/ui/attachments_panel.py`; size formatting in `_filesize.py`; picker in `_file_picker.py` | `note_controller.py` (`attachments-changed`); `note_list.py` (📎 badge) |
| Edit the help reference text | `system_docs/help.adoc` (must stay inside the supported subset; §7 coverage test requires every node kind to appear) | `enums.py` (`HelpSection`) if buckets change; `test_help_window.py` |
| Add a bundled system document | `enums.py` (`SystemDocument` member) → drop the file under `system_docs/` → read via `system_docs.load_text` / `load_bytes` | its consumer (`migrations.py` seed / `help_window.py`); `system_docs/test___init__.py` |
| Change the help window | `giruntime/ui/help_window.py` (builds its pane from the shared `note_view.build_article_surface()`) | `note_view.py`; `application.py` (`app.help` action + `F1`); `toolbar.py` (Help button) |

---

## 4. Module reference

Test files (`test_*.py`) sit next to their subject — `test_M.py` covers `M.py` —
and are omitted below.

### `src/` — source root

`src/` is the source root, **not** an importable package: it has no
`__init__.py`, and its contents sit at the root of the `folio.pyz` archive, so
the GI-free top-level modules import by bare name (`config`, `enums`, `models`,
`search`, `storage`, `asciidoc`). The two GI-dependent layers live under the
real `giruntime` package, which pins the GObject-Introspection versions once.

- **`__main__.py`** — entry point (dev and zipapp); builds `NotesApplication`, runs it, returns the exit code.
- **`enums.py`** — single home for every categorical constant. Persisted enums use `StrEnum` with stable values; transient ones use `auto()`.

### `config/` — constants + paths

- **`defaults.py`** — tunable constants (attachment/list/article/table limits and multipliers, snippet limits) and stable identifiers (`SEED_WELCOME_NOTE_ID`).
- **`paths.py`** — `data_directory()` / `database_path()` / `session_state_path()`, XDG-aware. Pure except for `mkdir`.

### `system_docs/` — bundled system documents (gi-free, config-tier)

Content the app ships rather than the user authoring: the seed welcome note and
the AsciiDoc help reference (plus its demo image). Plain package data read
gi-free via `importlib.resources` — **not** gresource content. Read by both
`storage` (seed) and `giruntime` (help).

- **`__init__.py`** — the shared loader keyed by the `SystemDocument` enum: `load_text(...) -> str`, `load_bytes(...) -> bytes`.
- **`welcome.adoc`** — seed welcome note source (v1 seeds it; a golden test pins its exact bytes).
- **`help.adoc`** — the help reference, authored in the supported subset (tested to parse clean and to exercise every node kind).
- **`help-demo.png`** — demo image served to the help's `image::` example.

### `models/` — frozen dataclasses

- **`note.py`** — `Note` + the frozen `NoteSummary` `(title, snippet, tags)`. Tag/summary derivation lives in `asciidoc/summary.py`, not here.
- **`attachment.py`** — `Attachment` metadata; no `data` field (bytes live in the BLOB column) and no type field (attachments are opaque blobs).
- **`parse_error.py`** — `ParseError`, the only exception raised by the lexer / parser / inline parser; carries `kind` + `line` + `column`.
- **`session_state.py`** — `SessionState` + `DEFAULT_SESSION_STATE`. Pure value type, no I/O.

### `asciidoc/` — text ⇒ AST ⇒ summary

A **pure** format library: GTK-free and storage-free, importing only `enums` /
`models` / `config`.

- **`lexer.py`** — `tokenize(source) -> tuple[Token, ...]`. Line-based, context-free, **permissive** (never raises on grammar issues).
- **`inline_parser.py`** — `parse_inline(line, line_no) -> tuple[InlineNode, ...]`. **Strict**: unpaired markers raise.
- **`parser.py`** — `parse(source) -> Document`. Recursive-descent, strict, exhaustive over tokens; each failure maps to a specific `ParseErrorKind`.
- **`ast.py`** — frozen dataclasses for every AST node. `BlockNode` / `InlineNode` are closed unions; children are `tuple[...]`.
- **`summary.py`** — `derive_summary(source) -> NoteSummary`. The single source of truth for note-list title/snippet/tags. **Never raises** — falls back to permissive extraction so a mid-edit note stays saveable.

### `storage/` — SQLite persistence

`protocols.py` is the typing surface every higher layer imports; concrete
classes are siblings.

- **`protocols.py`** — repository / attachment-store / session-state / renderer protocols, plus the `AttachmentRejected` exception and resolver aliases. Pure typing — no `sqlite3` or `gi` at runtime.
- **`database.py`** — owns the single `sqlite3.Connection` (`autocommit=True`, `foreign_keys=ON`, composable `transaction()` via savepoints).
- **`migrations.py`** — all schema statements in an append-only `ALL_MIGRATIONS`; `apply_pending()` is idempotent. See the live schema below.
- **`note_repository.py`** — SQLite-backed repository and **single owner of the `source → cached state` mapping**: `insert` / `update_source` derive title/snippet/tags, write the cached columns and `note_tags`, and return the persisted derived `Note`.
- **`attachment_store.py`** — BLOB-backed store. Attachments are opaque blobs; the only add-time gates are the `MAX_ATTACHMENT_BYTES` cap (checked before any bytes are read) and source readability.
- **`session_state_store.py`** — JSON-file-backed store at `paths.session_state_path()`. `load()` never raises (any error resolves to `DEFAULT_SESSION_STATE`); `save()` writes atomically.

**Live schema** (defined in `migrations.py`):

- `notes(id PK, title, source, snippet, created_at, modified_at)` + index on `modified_at DESC`.
- `note_tags(note_id FK→notes ON DELETE CASCADE, tag, PRIMARY KEY (note_id, tag))` + index on `tag`. Populated by the repository on every `insert` / `update_source`.
- `attachments(id PK, note_id FK→notes ON DELETE CASCADE, filename, byte_size, data BLOB)` + index on `note_id`.
- `schema_version(version PK)` records applied migrations.

Migrations are append-only, so v1's original CREATE statements still ship for
upgrade paths even though later migrations reshape the schema; a freshly
reset/deleted database re-runs v1 from scratch (the welcome note always comes
back on a true reset, which `_select_initial_note`'s fallback relies on).

### `search/` — pure filters

- **`note_filter.py`** — `filter_by_selection` / `filter_by_query` / `sort_notes`, and the `Selection` union (`SmartSelection` / `TagSelection`). Multi-tag selection is **AND**. No clock dependency.

### `giruntime/` — GI-pinned layer root

- **`__init__.py`** — the **single** `gi.require_version` site. Pins versions only; must not import a `gi.repository` namespace, so importing the package loads no typelib.

### `giruntime/controllers/` — UI⇄storage mediators

The only place where storage calls + signal emission live together. Widgets
never call repositories — they bind to the in-memory note store. May import
`gi` (`GObject` / `Gio`, never `Gtk`); must **not** import `asciidoc`.

- **`app_state.py`** — `AppState` GObject holding the only in-memory navigational state (`selection`, `selected-note-id`, `view-mode`, `query`) as properties observed via `notify::<prop>`, with rule-bearing mutators.
- **`note_item.py`** — `NoteItem`, the element type of `NoteListStore`; wraps one immutable `Note`. Never mutated in place.
- **`note_list_store.py`** — `NoteListStore(Gio.ListStore)`, the UI's in-memory write-through source of truth for full notes. Persists **DB-first**, then commits the in-memory change + `items-changed`. Owns the clock + id-gen; does not catch storage errors.
- **`tag_counts_model.py`** — `TagCountsModel(Gio.ListModel)`, a derived model aggregating live tag counts off the note store.
- **`note_controller.py`** — the note-level user actions. Delegates persistence to `NoteListStore`, wraps store calls in `capturing_storage_errors(...)`, and mutates `AppState`. Signals: `attachment-rejected`, `attachments-changed` (narrow per-note), `storage-error`. There is **no** `notes-changed` — panes observe the store.
- **`_storage_errors.py`** — the shared `capturing_storage_errors(emit)` context manager (catch `sqlite3.DatabaseError`, emit a toast signal, re-raise).

**Signal flow:**

```
user gesture (UI)
       │
       ▼
controller method
       │  ── store.create/update/delete (in capturing_storage_errors)
       │        └─ NoteListStore: persist DB-first ─► then items-changed
       │             └─► FilterListModel → SortListModel → ListView (note list)
       │             └─► TagCountsModel → SortListModel → ListView (sidebar tags)
       │  ── mutate AppState                          ─► notify::<prop>
       ▼
widgets refresh by observing the store's items-changed + AppState
```

Attachment mutations are the one change `items-changed` cannot carry (adding /
removing an attachment never touches the note source), so they ride the
controller's narrow per-note `attachments-changed` signal instead.

### `giruntime/ui/` — GTK 4 widgets

The only layer that owns widget trees. Every widget is thin and unit-testable
with fake controllers/repositories.

- **`application.py`** — `NotesApplication(Gtk.Application)`: composes the storage/controller stack, presents `MainWindow`, loads/saves `SessionState`, selects the initial note, registers the `help` action (`F1`) and the bundled application icon. App lifetime is bound to the main window.
- **`help_window.py`** — `HelpWindow`, the standalone non-modal help reference. Builds its reading pane from the shared `note_view.build_article_surface()` so help renders identically to a note. Hide-on-close (one cached instance).
- **`main_window.py`** — the three-pane shell (sidebar │ note list │ `Gtk.Stack(view ↔ editor)`). Takes an optional `restored_state`. Owns a **single** subscription, `AppState:notify::view-mode`.
- **`sidebar.py`** — flat library navigation: a **Library** section (`All notes` / `Untagged`) and a model-driven **Tags** section (multi-select, AND semantics). Selection rules owned by `AppState`; counts update live off the store.
- **`note_list.py`** — middle pane: a `ListView` over `SingleSelection(SortListModel(FilterListModel(NoteListStore)))`, reusing the `search.note_filter` predicates. Selection is one source of truth (`AppState`).
- **`note_view.py`** — read pane. `ArticleContainer` (a `Gtk.Widget` + `Gtk.Scrollable`) enforces the fixed-width column and owns scrolling; `ArticleTextView` paints the sheet + washes. Exposes the shared `build_article_surface()` and `make_cell_width_measurer()`. Renders from the in-memory store; parse errors render in-surface.
- **`note_editor.py`** — source pane (`GtkSource.View`) with the `AttachmentsPanel` embedded below it. Debounced autosave routes through `NoteController.update_source`. Loads grammar via `_gresource.resource_path(...)`.
- **`attachments_panel.py`** — per-note attachment management (header, add-file, one card per attachment). Add/remove route through `NoteController`; inserts nothing into the note body.
- **`toolbar.py`** — top `Gtk.HeaderBar`: *New*, search entry bound to `AppState:query`, View/Source toggle, *Delete*, and a *Help* button targeting `app.help`.
- **`dialogs.py`** — shared modal dialogs (confirm-delete only). Production wires `Gtk.AlertDialog`; tests drive callbacks synchronously.
- **`link_handler.py`** — `LinkHandler.install(...)` wiring motion/click controllers; URIs launched via an injected launcher, allowlisted by `enums.LinkScheme`.
- **`_file_picker.py`** — the `FileDialogOpener` callable wrapping `Gtk.FileDialog.open` (offers all files; the size cap in `AttachmentStore` is the gate).
- **`_filesize.py`** — shared human-readable byte-size formatting (binary convention). Pure.
- **`_dates.py`** — shared locale-independent date formatting (`format_date_short` / `format_date_long`). Pure.
- **`_gresource.py`** — `resource_path(GResourceSubtree) -> str`, the only way to obtain a path into the compiled `folio.gresource`; registers the bundle idempotently as a side effect. A missing bundle raises `FileNotFoundError`.
- **`css/app.css`** — application stylesheet, read via `importlib.resources`; ships in `folio.pyz`.
- **`language_spec.lang`** — GtkSourceView 5 grammar; compiled into `folio.gresource` and loaded via a `resource:///` search path (build input only, not shipped raw).
- **`folio.gresource.xml`** — committed GResource manifest publishing the grammar and the application icon; compiled by `glib-compile-resources`.

#### `giruntime/ui/note_render/` — AST ⇒ TextBuffer (GTK)

The GTK rendering of a parsed document. These are the only consumers that need
`gi` + `storage.protocols`, so they live here and keep `asciidoc` pure.

- **`tag_table.py`** — builds the shared `Gtk.TextTagTable`. **Every visual style lives here, exactly once** (inline, heading, block-level, table, metadata, list geometry, error-notice, and the note-sheet colour). Block tags carry text position only; tints are painted by `ArticleTextView` from the `WashSpec`s this module exposes.
- **`textbuffer_renderer.py`** — `TextBufferRenderer.render_into(document, buffer, ...)`. Rebuilds the buffer each call; **no construct escapes to a widget** (tables, images, admonitions, blockquotes, code blocks are all native buffer content). Image bytes flow through an injected `ImageBytesResolver`; an optional `post_title_hook` lets `NoteView` insert the metadata line.

---

## 5. Testing

- Tests use the standard-library `unittest`; there is no extra runner. A module `M.py` is tested in the sibling `test_M.py` (no global `tests/` directory).
- **Storage** tests run against a real `Database.in_memory()` with the schema applied. **Controllers** are tested against in-memory **fakes** of the storage protocols plus a fake clock and counter id-gen. **UI** tests instantiate widgets directly and drive them with fakes; asynchronous GTK dialogs are wrapped behind callable type aliases so tests pass a synchronous fake.
- **UI tests need a real GDK display.** Each is decorated `@unittest.skipUnless(_display_available(), ...)`. With no display they *skip*, so a green run without one proves nothing about the widgets.
- **`make test` wires the display**: it launches `weston --backend headless` in the **background** (chaining with `&&` would block forever), waits for its socket, then runs the suite with `WAYLAND_DISPLAY` and `GSK_RENDERER=cairo` exported, and kills Weston on exit. Running the suite directly against your own display must export the same two variables.
- **`GSK_RENDERER=cairo` is mandatory, not cosmetic.** The cairo software renderer never touches GL/Vulkan/EGL, so it cannot segfault inside a missing/broken GPU driver when a UI test presents a real toplevel. The deeper rationale (and the single-shared-application requirement below) lives in the docstrings of the relevant UI test modules.
- **The UI suite shares one registered application.** GTK allows exactly one *registered* `GtkApplication` per process (a second crashes), and a `Gtk.ApplicationWindow` may only be added to a registered application — an unregistered owner both warns (`New application windows must be added after the GApplication::startup signal…`) and silently drops the window. So the suite builds one application, registers it once (which is what emits `startup`), and passes it as the `application=` owner for every window. That shared instance (`_test_application` in `giruntime/ui/test_main_window.py`) is a real `NotesApplication` under an isolated test id, so the display-gated help tests can drive its app-scoped seams (`_ensure_help_window`, `_install_help_action`) against a registered owner; registering it does not open the database (that is `do_activate`'s job, never invoked here). Tests that only need window-lifetime *logic* with no real widgets (`test_application.py`) instead build an unregistered `NotesApplication` with duck-typed fake windows and never add a real window to it.
- **The search `query ↔ text` binding is tested by driving the entry with `set_text` (and by writing `AppState.query`), not by simulating per-character typing.** Simulating typing via `Gtk.Editable.insert_text` + `get_position()` is GTK-runtime-fragile: on GTK 4.14 `insert_text` at an explicit position does **not** advance the widget cursor, so re-read positions stay at 0 and the text reverses (`"test"` → `"tset"`) even with no binding at all. The reverse-echo/cursor-reset property that a typing simulation aimed to pin is now structural — `GObject.BindingFlags.BIDIRECTIONAL` suppresses the re-entrant `set_text` (see the binding in `toolbar.py`) — so the binding is exercised directly instead.
- For pylint, test files additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines`.

---

## 6. Conventions cheat sheet

Project-wide style rules; every module has its own additional invariants in its
docstring.

- **Python 3.13.** No 3.13-deprecated features; PEP 695 `type X = ...` aliases are preferred for callable types.
- **`from __future__ import annotations`** at the top of every module, after the docstring. Forward declarations rely on it, not string literals.
- All imports at the top of the module. No conditional imports except `if TYPE_CHECKING:` to keep pure layers gi-free.
- **Class attributes are declared in the class body** before being assigned in `__init__`.
- **Enums for every categorical concept.** No raw strings or magic numbers. Add the enum to `enums.py` before writing the logic that uses it.
- **Frozen dataclasses** for data shapes; children are `tuple[...]`, never `list`.
- **Specific type annotations only.** No `Any`, no `object`. Use the minimum type that conveys the requirement (`Iterable[T]` over `list[T]` when only iteration is needed).
- **No `except Exception`.** Catch by name. Storage errors go through `capturing_storage_errors(...)`.
- **GTK 4.18 compliant.** No methods deprecated in 4.18 or earlier.
- **GI versions are pinned centrally** in `giruntime/__init__.py`; no module carries its own `require_version`.
- **When parsing text**, never assume it is well-formed: raise a specific `ParseErrorKind` rather than silently ignoring a syntax error (except `summary.py`, whose documented job is a permissive fallback).

If a change would break one of these, that is the signal to discuss the design —
not to silently drop the invariant.

---

## 7. Packaging & distribution

`folio` ships as a **zipapp** — a single `folio.pyz` run with `python folio.pyz`.
There is no wheel and no `[build-system]`; `pyproject.toml` carries only project
metadata and tool config. `build_pyz.py` archives the `src/` tree directly,
filtering out `__pycache__`, `test_*.py`, and the grammar *sources*
(`language_spec.lang`, `folio.gresource.xml`). Everything else — `css/*.css`,
the compiled `folio.gresource`, and the `system_docs/*` files — rides along.
`src/__main__.py` lands at the archive root and is the implicit entry point.

**Build dependency: `glib-compile-resources`** (ships with the GLib dev tooling).
It compiles `giruntime/ui/folio.gresource.xml` + `language_spec.lang` (+ the icon)
into the generated, gitignored `giruntime/ui/folio.gresource`. One `Makefile`
rule (`resource`) builds it; `./run`, `make test`, and `make pyz` all depend on
it, so dev, test, and prod build the artifact the same way.

**Runtime floor: GtkSourceView ≥ 5.4** — the grammar is loaded via a
`resource:///` search path, which `set_search_path` only accepts from 5.4 on.
This is a system typelib, not a pip dependency, so it lives in the GTK 4.18
target environment rather than `pyproject.toml`.

**One GResource load path.** Both a source checkout and the packaged `folio.pyz`
load bundled resources from the compiled `folio.gresource` via `resource:///`
URIs — never from a filesystem path. `giruntime/ui/_gresource.py`'s
`resource_path(...)` is the only way to obtain such a path, and it registers the
bundle (exactly once per process) as a side effect. A missing resource is a hard
`FileNotFoundError` — the fix is always to build it (`./run` / `make`).

**Generated / gitignored artifacts:** `giruntime/ui/folio.gresource` and
`folio.pyz`. `make clean` removes both.

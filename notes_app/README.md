# notes-app — Developer Guide

A GTK 4 / Python 3.13 note-taking application backed by SQLite. Notes are
written in a strict subset of AsciiDoc and rendered into a
`Gtk.TextBuffer`. This README is a navigation map of the codebase — read
it first so you can find the right file before opening it.

> **For the contract of each module** open its source. Every non-trivial
> module begins with a `"""Principles & invariants` docstring that
> states the rules it must obey. That docstring is the source of truth;
> this README only points you at the right one.

---

## 1. Run, test, lint

| Goal | Command |
| --- | --- |
| Launch app | `python -m notes_app` (or `notes-app` after `pip install -e .`) |
| Run all tests | `make test` (preferred — sets up a headless display) or, with a display already available, `python -m unittest discover -s notes_app -v` |
| Type-check | `mypy notes_app` |
| Lint (non-test) | `pylint --disable=missing-module-docstring,missing-function-docstring,missing-class-docstring --enable=useless-suppression --min-public-methods=1 notes_app` |
| Lint (test files) | additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines` |

System packages required: `gir1.2-gtk-4.0`, `gir1.2-gtksource-5` (Debian/Ubuntu) plus equivalents elsewhere. Python ≥ 3.13. The only Python runtime dependency is `PyGObject>=3.50` (see `pyproject.toml`); SQLite is in the standard library.

To run the **full** test suite headlessly (e.g. in CI), `weston` is also required: the widget-level UI tests are gated behind a `_display_available()` guard and only run when a GDK display can be opened. `make test` provides one by launching a headless Weston compositor; see section 5 for the mechanics. Without a display those UI tests skip rather than fail, so a `python -m unittest …` run with no display reports `OK` while silently exercising none of the GTK widgets.

---

## 2. Layered architecture

Layers may only import **downward**. Every arrow below points from caller to callee — there are no cycles, and the table at the end of this section is the enforcement boundary.

```
                  ┌──────────────────────────────────┐
        UI ───────│ notes_app.ui          (GTK 4)    │  imports gi at runtime
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
   controllers ───│ notes_app.controllers            │  no widgets, no SQL
                  └────────┬────────────────┬────────┘
                           ▼                ▼
                  ┌────────────────┐  ┌───────────────────┐
   pure helpers ──│ search/        │  │ storage.protocols │  ← Protocol classes only
                  └───────┬────────┘  └─────────┬─────────┘
                          │      ┌──────────────┘
                          ▼      ▼
                  ┌──────────────────────────────────┐
    asciidoc  ────│ notes_app.asciidoc               │  pure parsing + GTK renderer
   (text→AST)     │   lexer → inline_parser → parser │  (renderer + tag_table are the
   (AST→buffer)   │   → ast → textbuffer_renderer    │   only GTK consumers here)
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
      storage ────│ notes_app.storage (SQLite impls) │  imports sqlite3
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
      models   ───│ notes_app.models (frozen data)   │  pure dataclasses
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
      enums    ───│ notes_app.enums                  │  no internal imports
                  └──────────────────────────────────┘
              ┌──────────────────────────────────────┐
      config  │ notes_app.config (constants, paths)  │  used by storage / ui
              └──────────────────────────────────────┘
```

| Layer | May import | May **not** import |
| --- | --- | --- |
| `enums` | nothing internal | anything else (it must stay leaf) |
| `models` | `enums` | `storage`, `controllers`, `ui`, `asciidoc`, `search` |
| `config` | `enums`, `models` | `storage`, `controllers`, `ui`, `asciidoc` |
| `asciidoc` (pure: `ast`, `lexer`, `inline_parser`, `parser`) | `enums`, `models`, `config` | `storage` (concrete), `controllers`, `ui`, `gi` |
| `asciidoc.textbuffer_renderer`, `asciidoc.tag_table` | the above + `gi` + `storage.protocols` (type aliases) | concrete `storage`, `controllers`, `ui` |
| `storage.protocols` | `enums`, `models` (uses `gi` only in `TYPE_CHECKING`) | everything else |
| `storage` (concrete) | `enums`, `models`, `config`, `storage.protocols`, `sqlite3` | `gi`, `controllers`, `ui` |
| `search` | `enums`, `models` | `storage` (concrete), `controllers`, `ui`, `gi` |
| `controllers` | `enums`, `models`, `config`, `search`, `storage.protocols`, `gi` (for `GObject`) | concrete `storage`, `ui` |
| `ui` | everything below | — |

**`gi` (GTK) is allowed only in:** `ui/*`, `controllers/*` (for `GObject` signals), `asciidoc/textbuffer_renderer.py`, `asciidoc/tag_table.py`. Anywhere else it is a bug.

---

## 3. "Where do I touch to do X?"

| Change | Start here | Likely also touches |
| --- | --- | --- |
| Add a new enum value (icon, link scheme, etc.) | `notes_app/enums.py` | any consumer that pattern-matches the enum; for `StrEnum`s persisted to disk, also add a migration |
| Add a new AsciiDoc construct | `asciidoc/ast.py` (new node) → `asciidoc/lexer.py` → `asciidoc/parser.py` → `asciidoc/textbuffer_renderer.py` → `asciidoc/tag_table.py` (new tag) → `asciidoc/language_spec.lang` (editor highlight) (purely structural inline nodes — e.g. `SoftBreak`, the parser-emitted soft-line-break joiner — skip the lexer and `language_spec.lang` and need only the AST union plus both renderer dispatch ladders) | `enums.py` (new `NodeKind`, possibly `ParseErrorKind`) |
| Add a parse error variant | `notes_app/enums.py` `ParseErrorKind` → the parser site that detects it → `parser.py` tests | gutter rendering in `ui/note_view.py` |
| Change DB schema | **new** `Migration` appended to `storage/migrations.py` `ALL_MIGRATIONS` — never edit a shipped one | the repository that reads/writes the new column |
| Add a note-level user action | `controllers/note_controller.py` (mutate + emit signal) → caller in `ui/toolbar.py` or `ui/note_editor.py` | repository protocol if storage shape changes |
| Add a notebook-level user action | `controllers/notebook_controller.py` → caller in `ui/sidebar.py` | `storage/notebook_repository.py` if storage shape changes |
| Change rendered-view styling | `asciidoc/tag_table.py` (tag definitions) — every visual style lives in exactly one place, including block-level paragraph styling for admonitions / blockquotes / code blocks. Block-level *tints* are painted at snapshot time by `_ArticleTextView` in `ui/note_view.py`, driven by `tag_table.build_wash_specs()` — see the next row for the constants. | rarely `asciidoc/textbuffer_renderer.py` for layout (only table sizing escapes to widget land) |
| Change block-level tint colours or insets | `asciidoc/tag_table.py` — `_ADMONITION_TINTS`, `_BLOCKQUOTE_TINT`, `_CODE_BLOCK_TINT` for colours; `_ADMONITION_HMARGIN_PX`, `_BLOCKQUOTE_HMARGIN_PX`, `_BLOCKQUOTE_RIGHT_MARGIN_PX`, `_CODE_BLOCK_HMARGIN_PX` for insets. The same constants feed both the paragraph tag margins (text position, `accumulative-margin = True`) and the `WashSpec` records (wash painter), so the two cannot drift. | `test_tag_table.py` `WashSpecTests`, `test_note_view.py` `ArticleTextViewWashRectTests` |
| Tune article column margins | `config/defaults.py` (the three `ARTICLE_*` multipliers) | none — `ui/note_view.py` reads the constants once at `NoteView.__init__` and applies them to the inner `Gtk.TextView`'s four margins |
| Change rendered-view layout sizing | `ui/note_view.py` `ArticleContainer` — note that it must remain a `Gtk.Widget` subclass; `Gtk.Box` silently disables `do_measure`/`do_size_allocate` overrides because its `BoxLayout` layout manager intercepts them. Because it is a bare `Gtk.Widget` that parents its child by hand (`set_parent`), it must also unparent that child at teardown or GTK warns *"Finalizing … but it still has children left"*; PyGObject does not expose `dispose`, so it does this from `do_unroot` (rooted/production teardown) plus a `__del__` net (never-rooted standalone instances, e.g. tests), both via the guarded `_release_child` | `ui/test_note_view.py` `ArticleContainer*` tests (incl. `ArticleContainerTeardownTests`) |
| Change application chrome / CSS | `ui/css/app.css` | bumping `pyproject.toml` `package-data` if a new asset is added |
| Change the initial window size | `ui/main_window.py` — height is `_DEFAULT_WINDOW_HEIGHT_PX`; width is computed by `_default_window_width(...)` from `_SIDEBAR_INITIAL_POSITION_PX` + `_NOTE_LIST_INITIAL_POSITION_PX` + `_PANED_HANDLE_ALLOWANCE_PX` + the rendered article column + `_ARTICLE_SIDE_SLACK_PX`, clamped up to `_MIN_DEFAULT_WINDOW_WIDTH_PX`. The column term is `NoteView.preferred_column_width_px()`, so the default width tracks the body font and the column always opens fully visible / centred rather than overflowing into a horizontal scroll. | `ui/test_main_window.py` `DefaultWindowWidthTests` + `test_constructs_and_reports_default_size`; `ui/test_note_view.py` `NoteViewPreferredColumnWidthTests` |
| Change source-editor syntax highlight | `asciidoc/language_spec.lang` (GtkSourceView grammar) | nothing else; the file is data |
| Tune a constant (sizes, quotas) | `config/defaults.py` | none — that is the point of this module |
| Change paths / XDG behaviour | `config/paths.py` | tests under `config/test_paths.py` |
| Add a new sort key / smart filter | `enums.py` (`NoteSortKey` / `SmartFilter`) → `search/note_filter.py` → `ui/note_list.py` (dropdown) | tests in `search/test_note_filter.py` |
| Change selection / view-mode plumbing | `controllers/app_state.py` (add a field + signal). Every UI widget that reacts to it. **The MainWindow's `_on_view_mode_changed` handler is the single place that orchestrates editor-flush + view-refresh across the toggle — see the corresponding invariant in `ui/main_window.py`.** | every UI widget that reacts to it |
| Add a new dialog | `ui/dialogs.py` | the controller or widget that opens it |
| Change link/URL handling | `ui/link_handler.py`; allowlist in `enums.LinkScheme` | `asciidoc/inline_parser.py` for scheme validation |
| Change image attachment rules | `storage/attachment_store.py`; size cap in `config/defaults.MAX_ATTACHMENT_BYTES`; MIME set in `enums.MimeKind` | `controllers/note_controller.py` for the toast wiring |

---

## 4. Module reference

Test files (`test_*.py`) sit next to their subject — `test_M.py` covers `M.py`. They are omitted from the table below.

### `notes_app/` — package root

| File | LOC | One-line summary |
| --- | ---: | --- |
| `__init__.py` | 7 | Package marker — exposes nothing on purpose; every import is explicit at the call site. |
| `__main__.py` | 43 | `python -m notes_app` entry; builds `NotesApplication`, runs it, returns the exit code. |
| `enums.py` | 213 | **Single home** for every categorical constant. Persisted enums use `StrEnum` with stable values; transient ones use `auto()`. |

### `notes_app/config/` — constants + paths

| File | LOC | One-line summary |
| --- | ---: | --- |
| `defaults.py` | 187 | Tunable constants (`MAX_ATTACHMENT_BYTES`, `TARGET_CHARS_PER_LINE`, and the three `ARTICLE_*` margin multipliers `ARTICLE_TOP_MARGIN_LINES` / `ARTICLE_BOTTOM_MARGIN_LINES` / `ARTICLE_INNER_HPADDING_CHARS`) and the seed `SEED_NOTEBOOKS` / `SEED_WELCOME_NOTE_SOURCE` written by the v1 migration. |
| `paths.py` | 76 | `data_directory()`, `database_path()` — XDG-aware filesystem resolution. Each call is pure; mkdir is the only side effect. |

### `notes_app/models/` — frozen dataclasses

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note.py` | 162 | `Note` dataclass + pure `derive_title` / `derive_snippet`. The dataclass is frozen; updates produce new instances via the repository. |
| `notebook.py` | 53 | `Notebook` dataclass. Two-level hierarchy invariant is enforced in `storage`, not here. |
| `attachment.py` | 56 | `Attachment` metadata — deliberately has **no `data` field**; bytes live only in the `attachments.data` BLOB column. |
| `parse_error.py` | 58 | `ParseError`, the **only** exception type raised by the AsciiDoc lexer / parser / inline parser. Carries `kind: ParseErrorKind` + `line` + `column`. |

### `notes_app/asciidoc/` — text ⇒ AST ⇒ TextBuffer

The pipeline. Everything from `lexer` through `parser` is pure (no GTK, no I/O). The renderer is the one place that imports `gi`.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `lexer.py` | 899 | `tokenize(source) -> tuple[Token, ...]`. **Line-based, context-free, permissive** — never raises on grammar issues; that is the parser's job. Public token dataclasses listed at the top. |
| `inline_parser.py` | 790 | `parse_inline(line, line_no) -> tuple[InlineNode, ...]`. **Strict** — every formatting marker must be paired; otherwise raises `ParseErrorKind.BAD_INLINE_SPAN` (or `UNTERMINATED_MONOSPACE`). |
| `parser.py` | 1353 | `parse(source) -> Document`. Recursive-descent, strict, exhaustive over tokens. Each syntactic failure maps to a specific `ParseErrorKind`. |
| `ast.py` | 434 | Frozen dataclasses for every AST node (`Document`, `Section`, `Paragraph`, `OrderedList`, …, `Bold`, `Italic`, `Link`, …). Children are `tuple[...]` for true immutability. `BlockNode` and `InlineNode` are closed unions. |
| `tag_table.py` | 379 | Builds the shared `Gtk.TextTagTable`. **Every visual style lives here, exactly once.** Tag names are exposed as `TagName` enum members. Holds inline styles (bold / italic / strikethrough / underline / monospace / link), heading styles, **plus the paragraph-tag styling for admonitions (per-kind label and body tags + a kind-label character tag), blockquotes (body + attribution), and code blocks** — all the block-level styling that used to live in widget builders. Block-level tags carry only the *text position* (`accumulative-margin = True`); the matching tinted wash is painted by `_ArticleTextView` in `ui/note_view.py` using `build_wash_specs()`. |
| `textbuffer_renderer.py` | 869 | `TextBufferRenderer.render_into(document, buffer, ...)`. Image bytes flow through an injected `ImageBytesResolver`. Rebuilds the buffer from scratch on each call. **Block-level constructs render as styled paragraphs in the buffer wherever the styling primitive set allows; only tables escape to an anchored widget** (which is sized via `set_size_request` because anchored children ignore `hexpand`). Images use the private `_ScaledImagePaintable` to cap intrinsic width at the column width; decode failures fall through to `_PlaceholderImagePaintable`. |
| `language_spec.lang` | 353 | GtkSourceView 5 grammar that drives source-editor syntax highlighting. Pure data, loaded by `ui/note_editor.py`. |

### `notes_app/storage/` — SQLite persistence

`protocols.py` is the typing surface every higher layer imports. Concrete classes are siblings.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `protocols.py` | 267 | `NoteRepositoryProtocol`, `NotebookRepositoryProtocol`, `AttachmentStoreProtocol`, `RendererProtocol`; the `AttachmentRejected` / `NestingTooDeep` exceptions; PEP 695 resolver aliases `ImageBytesResolver` / `ColumnWidthResolver`. **Pure typing — no `sqlite3` or `gi` at runtime.** |
| `database.py` | 170 | Owns the single `sqlite3.Connection`. `autocommit=True`, `PRAGMA foreign_keys=ON`, composable `transaction()` (nested calls become `SAVEPOINT`). |
| `migrations.py` | 252 | All `CREATE TABLE` / `CREATE INDEX` / `CREATE TRIGGER` statements. Append-only `ALL_MIGRATIONS` tuple; `apply_pending()` is idempotent. v1 also seeds notebooks + welcome note. |
| `note_repository.py` | 207 | SQLite-backed `NoteRepositoryProtocol`. Row↔dataclass conversion lives in exactly one place per direction. Timestamps round-trip via ISO-8601. |
| `notebook_repository.py` | 187 | SQLite-backed `NotebookRepositoryProtocol`. Catches the `RAISE(ABORT, 'NestingTooDeep')` trigger and re-raises as `NestingTooDeep`. `delete_and_reparent_notes` is one transaction. |
| `attachment_store.py` | 266 | BLOB-backed `AttachmentStoreProtocol`. Enforces `MAX_ATTACHMENT_BYTES` via `Path.stat()` **before** any bytes are read. Rejections raise `AttachmentRejected(reason=…)`. |
| `_notebook_writes.py` | 55 | Private helper sharing the `INSERT INTO notebooks` statement between migrations and the repository. Do not import from outside the storage package. |

**v1 schema (live in `migrations.py`):**

- `notebooks(id PK, name, parent_id FK→notebooks ON DELETE RESTRICT, icon, sort_order)` + two `BEFORE INSERT/UPDATE` triggers enforcing two-level depth.
- `notes(id PK, title, notebook_id FK→notebooks ON DELETE RESTRICT, source, snippet, created_at, modified_at)` + indices on `notebook_id` and `modified_at DESC`.
- `attachments(id PK, note_id FK→notes ON DELETE CASCADE, filename, byte_size, mime_type, data BLOB)` + index on `note_id`.
- `schema_version(version, applied_at)` records which migrations have been applied.

### `notes_app/search/` — pure filters

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note_filter.py` | 213 | `filter_by_selection`, `filter_by_query`, `sort_notes`. The `Selection` discriminated union (`SmartSelection` / `NotebookSelection`) lives here. `RECENT_WINDOW_DAYS = 7`. `now` is injected. |

### `notes_app/controllers/` — UI⇄storage mediators

Controllers are the only place where storage calls + signal emission live together. Widgets never call repositories.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `app_state.py` | 187 | `AppState` GObject. Holds the **only** in-memory navigational state: `selection`, `selected_note_id`, `view_mode`, `query`. Emits `selection-changed`, `selected-note-changed`, `view-mode-changed`, `query-changed` (all payload-free). |
| `note_controller.py` | 379 | `create_note`, `duplicate_note`, `request_delete`, `update_source`, `move_to_notebook`, `add_attachment`, `remove_attachment`. Emits `notes-changed`, `attachment-rejected`, `storage-error`. Clock + id-gen are injected callables. |
| `notebook_controller.py` | 208 | `create_notebook`, `rename`, `set_icon`, `delete` (with reparent). Emits `notebooks-changed`, `storage-error`. |
| `_storage_errors.py` | 69 | Shared `capturing_storage_errors(emit)` context manager — single home for the *catch `sqlite3.DatabaseError`, emit a toast signal, re-raise* pattern. Private to the controllers package. |

**Signal flow at a glance:**

```
user gesture (UI)
       │
       ▼
controller method
       │  ── storage call (in `capturing_storage_errors(...)`)
       │  ── emit "(notes|notebooks)-changed"     ─► listeners re-query repository
       │  ── mutate AppState                       ─► AppState emits its own signal
       ▼
widgets refresh by reading from repositories + AppState
```

### `notes_app/ui/` — GTK 4 widgets

This is the only layer that owns widget trees. Every widget is thin and unit-testable with fake controllers/repositories.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `application.py` | 286 | `NotesApplication(Gtk.Application)` — composes `Database`, repositories, `AttachmentStore`, `AppState`, controllers, then presents `MainWindow`. Single-instance via `FLAGS_NONE`. |
| `main_window.py` | 328 | `MainWindow` — the three-pane shell: sidebar │ note list │ `Gtk.Stack(view ↔ editor)`. Toolbar is set as the title bar. The initial window width is derived from the rendered article column (`_default_window_width` + `NoteView.preferred_column_width_px()`) so the fixed-width column opens fully visible. |
| `sidebar.py` | 846 | Notebook tree on the left, rendered with `Gtk.ListView` + `Gtk.TreeListModel` + `Gtk.TreeExpander` (one `ListView`/`SingleSelection` per section). Click → mutate `AppState.selection`. Expansion state is widget-local (intentional — different windows could disagree), snapshotted across `refresh()`. Icon-column alignment depends on the `treeexpander indent` rule in `css/app.css` — the two are a matched pair. |
| `note_list.py` | 621 | Middle pane: header + sortable, filtered list. `compute_display_notes(...)` is a free function so tests don't need widgets. |
| `note_view.py` | 933 | Read pane. `ArticleContainer` enforces the fixed-width text column; `preferred_column_width_px()` exposes that column's outer width so `MainWindow` can size the initial window to it. Calls `TextBufferRenderer.render_into` on every change. `_ArticleTextView` paints the wider tinted wash behind admonition / blockquote / code-block paragraphs (see `tag_table.WashSpec`). |
| `note_editor.py` | 1260 | Source pane (`GtkSource.View` + `GtkSource.Buffer`). Debounced autosave (`AUTOSAVE_DEBOUNCE_MS`). Stateless w.r.t. notes — reloads from repo on selection change. |
| `toolbar.py` | 702 | Top `Gtk.HeaderBar` — *New* button, search entry, breadcrumb, View/Source toggle, More menu (Duplicate/Delete). `resolve_target_notebook`, `compute_breadcrumb`, `format_breadcrumb` are extracted as free functions. |
| `dialogs.py` | 363 | Shared modal dialogs — confirm-delete (a callable matching `ConfirmDialogPresenter`) and `IconPickerPopover`. Production wires `Gtk.AlertDialog`; tests drive callbacks synchronously. |
| `link_handler.py` | 386 | `LinkHandler.install(textview, ...)` — wires `EventControllerMotion` (cursor) + `GestureClick` (open on `released`). URI is launched via an injected `UriLauncherProtocol`; allowlist is `enums.LinkScheme`. |
| `_image_picker.py` | 152 | `FileDialogOpener` callable + `default_file_dialog_opener` wrapping `Gtk.FileDialog.open`. MIME filters mirror `enums.MimeKind`. Module is private so `note_editor.py` stays under pylint's `max-module-lines`. |
| `css/app.css` | 91 | Application stylesheet — loaded by `NotesApplication`. Styles the note-view parse-error banner and the library sidebar (inset rounded selection pill + icon-column alignment, palette-safe via geometry/opacity only). Asset is shipped via `pyproject.toml` `package-data`. |

---

## 5. Testing

- Tests use the standard library `unittest`. There is no extra runner.
- A module `M.py` is tested in the sibling file `test_M.py`. No global `tests/` directory.
- Storage tests run against a real `Database.in_memory()` with the v1 schema applied — the in-memory backend is the unit under test alongside the repository.
- Controllers are tested against dataclass-backed in-memory **fakes** of the storage protocols, plus a **fake clock** and **counter id-gen** for determinism. No GTK display, no temp directories.
- UI tests instantiate widgets directly and drive them with fake controllers/protocols. Asynchronous GTK 4 dialogs (`Gtk.FileDialog.open`, `Gtk.AlertDialog`) are wrapped behind callable type aliases (`FileDialogOpener`, `ConfirmDialogPresenter`) so tests pass a synchronous fake.
- **UI tests need a real GDK display.** Each such test (and several whole classes) is decorated `@unittest.skipUnless(_display_available(), "no GDK display")`, where `_display_available()` is true iff `Gdk.Display.get_default()` opens. With no display they *skip*, so a green run without one proves nothing about the widgets. The `make test` target supplies a display by running a headless Weston compositor; on the reference environment this is the difference between ~312 skipped and 1 skipped.
- **How `make test` wires the display** (see the comment in the `Makefile`): it launches `weston --backend headless --socket=test_notes` in the **background** (Weston is a long-running compositor — chaining it with `&&` would block forever and never reach the tests), waits for the `$XDG_RUNTIME_DIR/test_notes` socket to appear, then runs the suite with `WAYLAND_DISPLAY=test_notes` and `GSK_RENDERER=cairo` exported (the socket name alone is not enough — without `WAYLAND_DISPLAY` GTK opens no display) and kills Weston on exit. Requires the `weston` package. Running the suite directly (`python -m unittest …`) against your own display should export the same two variables.
- **`GSK_RENDERER=cairo` is mandatory, not cosmetic.** A few UI tests `present()` a real toplevel (e.g. `ui/test_sidebar.py` `IconColumnAlignmentTests`, which needs realised geometry to compare icon x-origins) and then pump the GLib main loop. Presenting a window makes GTK build its GPU renderer — GL before GTK 4.16, Vulkan from 4.16 on — against the headless Weston surface. On a host whose GL/Vulkan stack does not cleanly fall back to software, that renderer **segfaults inside the driver during the next main-loop iteration** (the crash surfaces in `GLib.MainContext.iteration`, not in any project code). The cairo software renderer never touches GL/Vulkan/EGL, so forcing it makes these tests deterministic and crash-proof everywhere.
- **The `MainWindow` tests share one registered `Gtk.Application`** (`ui/test_main_window.py` `_test_application()`, memoised with `functools.cache`). GTK supports a single registered `GtkApplication` per process — the first to register becomes `g_application_get_default()` and installs process-global state, and a second *registered* one is unsupported and crashes (segfault). Building a fresh application per test therefore must be avoided; the suite registers one application once and reuses it for every window (a `Gtk.ApplicationWindow` may share its application with others). Registering once before any window is added also suppresses GTK's "added before startup" warning. A per-test id (unique or shared) is the wrong axis: a *shared* id collides on the session bus (`An object is already exported …`), while *unique* ids let every application register and reintroduce the multiple-registered-application crash — only a single shared application avoids both.
- For pylint, test files additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines`.

---

## 6. Conventions cheat sheet

These are the project-wide style rules; every module has its own additional invariants in its docstring.

- **Python 3.13.** No 3.13-deprecated features; PEP 695 `type X = ...` aliases are preferred for callable types.
- **`from __future__ import annotations`** at the top of every module, after the docstring.
- All imports at the top of the module. No conditional imports except for `if TYPE_CHECKING:` to avoid a runtime `gi` dependency in pure layers (see `storage/protocols.py`).
- **Class attributes are declared in the class body** before being assigned in `__init__`.
- **Enums for every categorical concept.** No raw strings or magic numbers. If you need a new category, add it to `enums.py` before writing the logic that uses it.
- **Frozen dataclasses** for data shapes. Children are `tuple[...]`, never `list`, so equality and hashing are well-defined.
- **Specific type annotations only.** No `Any`, no `object`. Use the minimum type that conveys the requirement (`Iterable[T]` over `list[T]` when only iteration is needed).
- **No `except Exception`.** Catch by name. Storage errors go through `capturing_storage_errors(...)` so the controllers don't drift.
- **GTK 4.18 compliant.** No methods deprecated in 4.18 or earlier (e.g. `Gtk.Paned.pack1/pack2`, pre-4.10 dialog APIs).
- **Forward declarations** rely on `from __future__ import annotations`, not string literals.

Every module begins with a `"""Principles & invariants` docstring. If a change you are making would break one of those bullets, that is the signal to discuss the design — not to silently drop the invariant.

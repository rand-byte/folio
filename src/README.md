# folio — Developer Guide

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
| Launch app | `./run` (dev — builds the grammar resource, then runs `python3 -B src/__main__.py`) or `python folio.pyz` (distributed zipapp) |
| Run all tests | `make test` (preferred — builds the grammar resource and sets up a headless display) or, with a display already available, `python3 -B -m unittest discover -s src -t src -v` |
| Type-check | `mypy src` — **requires `mypy >= 1.16`** (earlier releases mis-widen `StrEnum` members to `str` under `enumerate`/`list`, [python/mypy#18587](https://github.com/python/mypy/pull/18587); pinned in `pyproject.toml`'s `[dependency-groups]` `dev`). The `[tool.mypy]` `mypy_path = "src"` + `explicit_package_bases = true` keys handle the package-less `src` layout. |
| Lint (non-test) | `PYTHONPATH=src pylint --disable=missing-module-docstring,missing-function-docstring,missing-class-docstring --enable=useless-suppression --min-public-methods=1 src` (`PYTHONPATH=src` puts the source root on the path so intra-tree imports resolve) |
| Lint (test files) | additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines` |

System packages required: `gir1.2-gtk-4.0`, `gir1.2-gtksource-5` (Debian/Ubuntu — **GtkSourceView ≥ 5.4**, see the Packaging notes in section 8) plus equivalents elsewhere, and `glib-compile-resources` (ships with the GLib dev tooling) to build the editor grammar bundle. Python ≥ 3.13. The only Python runtime dependency is `PyGObject>=3.50` (see `pyproject.toml`); SQLite is in the standard library.

To run the **full** test suite headlessly (e.g. in CI), `weston` is also required: the widget-level UI tests are gated behind a `_display_available()` guard and only run when a GDK display can be opened. `make test` provides one by launching a headless Weston compositor; see section 5 for the mechanics. Without a display those UI tests skip rather than fail, so a `python3 -B -m unittest …` run with no display reports `OK` while silently exercising none of the GTK widgets.

---

## 2. Layered architecture

Layers may only import **downward**. Every arrow below points from caller to callee — there are no cycles, and the table at the end of this section is the enforcement boundary.

```
                  ┌──────────────────────────────────┐
        UI ───────│ ui          (GTK 4)              │  imports gi at runtime
                  └─────────────────┬────────────────┘
                                    ▼
                  ┌──────────────────────────────────┐
   controllers ───│ controllers                      │  no widgets, no SQL
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
   (text→AST)     │   lexer → inline_parser → parser │  no storage. The GTK renderer
   (AST→summary)  │   → ast → summary                │  now lives in ui/note_render.
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

| Layer | May import | May **not** import |
| --- | --- | --- |
| `enums` | nothing internal | anything else (it must stay leaf) |
| `models` | `enums` | `storage`, `controllers`, `ui`, `asciidoc`, `search` |
| `config` | `enums`, `models` | `storage`, `controllers`, `ui`, `asciidoc` |
| `asciidoc` (pure: `ast`, `lexer`, `inline_parser`, `parser`, `summary`) | `enums`, `models`, `config` | `storage`, `controllers`, `ui`, `gi`, `storage.protocols` |
| `storage.protocols` | `enums`, `models` (uses `gi` only in `TYPE_CHECKING`) | everything else |
| `storage` (concrete) | `enums`, `models`, `config`, `storage.protocols`, `sqlite3`, `asciidoc` (pure core, for `derive_summary`) | `gi`, `controllers`, `ui` |
| `search` | `enums`, `models` | `storage` (concrete), `controllers`, `ui`, `gi` |
| `controllers` | `enums`, `models`, `config`, `search`, `storage.protocols`, `gi` (for `GObject`) | concrete `storage`, `ui` |
| `ui` | everything below | — |

**`gi` (GTK) is allowed only in:** `ui/*` (including `ui/note_render/*`) and `controllers/*` (for `GObject` signals). Anywhere else — including the whole of `asciidoc/*`, now a pure format library — it is a bug.

---

## 3. "Where do I touch to do X?"

| Change | Start here | Likely also touches |
| --- | --- | --- |
| Add a new enum value (icon, link scheme, etc.) | `enums.py` | any consumer that pattern-matches the enum; for `StrEnum`s persisted to disk, also add a migration |
| Add a new AsciiDoc construct | `asciidoc/ast.py` (new node) → `asciidoc/lexer.py` → `asciidoc/parser.py` → `ui/note_render/textbuffer_renderer.py` → `ui/note_render/tag_table.py` (new tag) → `ui/language_spec.lang` (editor highlight) → **decide its snippet treatment in `asciidoc/summary.py`** (the `match` over `BlockNode`/`InlineNode` is exhaustive, so an unhandled new kind is a type error there). Purely structural inline nodes — e.g. `SoftBreak`, the parser-emitted soft-line-break joiner — skip the lexer and `language_spec.lang` and need only the AST union plus both renderer dispatch ladders and the summary flattener. | `enums.py` (new `NodeKind`, possibly `ParseErrorKind`) |
| Add a parse error variant | `enums.py` `ParseErrorKind` → the parser site that detects it → `parser.py` tests | gutter rendering in `ui/note_view.py` |
| Change DB schema | **new** `Migration` appended to `storage/migrations.py` `ALL_MIGRATIONS` — never edit a shipped one | the repository that reads/writes the new column |
| Add a note-level user action | `controllers/note_controller.py` (mutate + emit signal) → caller in `ui/toolbar.py` or `ui/note_editor.py` | repository protocol if storage shape changes |
| Change tag parsing or validation | `asciidoc/parser.py` `parse_tags_value` (strict path used by the parser; raises `ParseError(BAD_TAG_VALUE)` / `DUPLICATE_TAG_ATTRIBUTE`) — the same helper is reused by the permissive `_fallback_tags` arm of `asciidoc/summary.py`, so a single charset / normalisation rule covers both | `asciidoc/test_parser.py` `Tags*Tests`; `asciidoc/test_summary.py` `DeriveSummaryTags*Tests`; if the rule change affects how existing notes parse, a `Migration` that re-derives `note_tags` |
| Change rendered-view styling | `ui/note_render/tag_table.py` (tag definitions) — every visual style lives in exactly one place, including block-level paragraph styling for admonitions / blockquotes / code blocks. Block-level *tints* are painted at snapshot time by `_ArticleTextView` in `ui/note_view.py`, driven by `tag_table.build_wash_specs()` — see the next row for the constants. | rarely `ui/note_render/textbuffer_renderer.py` for layout (only table sizing escapes to widget land) |
| Change block-level tint colours or insets | `ui/note_render/tag_table.py` — `_ADMONITION_TINTS`, `_BLOCKQUOTE_TINT`, `_CODE_BLOCK_TINT` for colours; `_ADMONITION_HMARGIN_PX`, `_BLOCKQUOTE_HMARGIN_PX`, `_BLOCKQUOTE_RIGHT_MARGIN_PX`, `_CODE_BLOCK_HMARGIN_PX` for insets. The same constants feed both the paragraph tag margins (text position, `accumulative-margin = True`) and the `WashSpec` records (wash painter), so the two cannot drift. | `test_tag_table.py` `WashSpecTests`, `test_note_view.py` `ArticleTextViewWashRectTests` |
| Tune article column margins | `config/defaults.py` (the three `ARTICLE_*` multipliers) | none — `ui/note_view.py` reads the constants once at `NoteView.__init__` and applies them to the inner `Gtk.TextView`'s four margins |
| Change rendered-view layout sizing | `ui/note_view.py` `ArticleContainer` — note that it must remain a `Gtk.Widget` subclass; `Gtk.Box` silently disables `do_measure`/`do_size_allocate` overrides because its `BoxLayout` layout manager intercepts them. Because it is a bare `Gtk.Widget` that parents its child by hand (`set_parent`), it must also unparent that child at teardown or GTK warns *"Finalizing … but it still has children left"*; PyGObject does not expose `dispose`, so it does this from `do_unroot` (rooted/production teardown) plus a `__del__` net (never-rooted standalone instances, e.g. tests), both via the guarded `_release_child` | `ui/test_note_view.py` `ArticleContainer*` tests (incl. `ArticleContainerTeardownTests`) |
| Move the tag-chip row inside / outside the article column | `ui/note_view.py` (chip-row construction + `_attach_chip_row_after_title`) | `ui/note_render/textbuffer_renderer.py` (`post_title_hook` semantics); `ui/test_note_view.py` `NoteViewChipPlacementTests`; `ui/note_render/test_textbuffer_renderer.py` `PostTitleHookTests` |
| Change application chrome / CSS | `ui/css/app.css` | no packaging change needed — the zipapp build archives `src/` directly, so any new asset under `ui/` ships automatically (see section 8) |
| Change the initial window size | `ui/main_window.py` — height is `_DEFAULT_WINDOW_HEIGHT_PX`; width is computed by `_default_window_width(...)` from `_SIDEBAR_INITIAL_POSITION_PX` + `_NOTE_LIST_INITIAL_POSITION_PX` + `_PANED_HANDLE_ALLOWANCE_PX` + the rendered article column + `_ARTICLE_SIDE_SLACK_PX`, clamped up to `_MIN_DEFAULT_WINDOW_WIDTH_PX`. The column term is `NoteView.preferred_column_width_px()`, so the default width tracks the body font and the column always opens fully visible / centred rather than overflowing into a horizontal scroll. | `ui/test_main_window.py` `DefaultWindowWidthTests` + `test_constructs_and_reports_default_size`; `ui/test_note_view.py` `NoteViewPreferredColumnWidthTests` |
| Change source-editor syntax highlight | `ui/language_spec.lang` (GtkSourceView grammar) | the grammar is compiled into `folio.gresource`, so rebuild it (`./run` / `make resource` / `make test` do this automatically) for edits to take effect; the `.xml` manifest only changes if you add/rename grammar files |
| Tune a constant (sizes, quotas) | `config/defaults.py` | none — that is the point of this module |
| Change paths / XDG behaviour | `config/paths.py` | tests under `config/test_paths.py` |
| Add a new sort key / smart filter | `enums.py` (`NoteSortKey` / `SmartFilter`, e.g. the existing `ALL` / `UNTAGGED`) → `search/note_filter.py` → `ui/note_list.py` (dropdown) and / or `ui/sidebar.py` (Library section row) | tests in `search/test_note_filter.py` |
| Change the note-list row title/snippet | the *derivation* in `asciidoc/summary.py` (`derive_summary`); the *presentation* in `ui/note_list.py` `_make_note_row` + classes in `ui/css/app.css` (`.note-title` / `.note-snippet` / `.note-meta`) | `storage/note_repository.py` only if the cached-column contract changes; a backfill migration if existing rows must be rewritten |
| Change selection / view-mode plumbing | `controllers/app_state.py` (add a field + signal). Every UI widget that reacts to it. **The MainWindow's `_on_view_mode_changed` handler is the single place that orchestrates editor-flush + view-refresh across the toggle — see the corresponding invariant in `ui/main_window.py`.** | every UI widget that reacts to it |
| Add a new dialog | `ui/dialogs.py` | the controller or widget that opens it |
| Change link/URL handling | `ui/link_handler.py`; allowlist in `enums.LinkScheme` | `asciidoc/inline_parser.py` for scheme validation |
| Change image attachment rules | `storage/attachment_store.py`; size cap in `config/defaults.MAX_ATTACHMENT_BYTES`; MIME set in `enums.MimeKind` | `controllers/note_controller.py` for the toast wiring |

---

## 4. Module reference

Test files (`test_*.py`) sit next to their subject — `test_M.py` covers `M.py`. They are omitted from the table below.

### `src/` — source root

`src/` is the source root, **not** an importable package — it has no
`__init__.py`, and its contents sit at the root of the `folio.pyz` archive,
so top-level modules are imported by their bare names (`config`, `ui`, …).

| File | LOC | One-line summary |
| --- | ---: | --- |
| `__main__.py` | 43 | `python3 -B src/__main__.py` (dev) / `python folio.pyz` (zipapp) entry; builds `NotesApplication`, runs it, returns the exit code. |
| `enums.py` | 213 | **Single home** for every categorical constant. Persisted enums use `StrEnum` with stable values; transient ones use `auto()`. |

### `config/` — constants + paths

| File | LOC | One-line summary |
| --- | ---: | --- |
| `defaults.py` | 134 | Tunable constants (`MAX_ATTACHMENT_BYTES`, `TARGET_CHARS_PER_LINE`, the three `ARTICLE_*` margin multipliers, plus `SNIPPET_MAX_CHARS` and `UNTITLED` consumed by `asciidoc/summary.py`) and `SEED_WELCOME_NOTE_SOURCE` (which now carries a `:tags: welcome` header so the seed note classifies on first launch). |
| `paths.py` | 76 | `data_directory()`, `database_path()` — XDG-aware filesystem resolution. Each call is pure; mkdir is the only side effect. |

### `models/` — frozen dataclasses

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note.py` | 87 | `Note` dataclass + the frozen `NoteSummary` `(title, snippet, tags)` value type. Both are frozen; updates produce new instances via the repository. `tags` is a sorted lowercase `tuple[str, ...]` derived from the source's `:tags:` header. Derivation lives in `asciidoc/summary.py`, not here (single classifier). |
| `attachment.py` | 56 | `Attachment` metadata — deliberately has **no `data` field**; bytes live only in the `attachments.data` BLOB column. |
| `parse_error.py` | 58 | `ParseError`, the **only** exception type raised by the AsciiDoc lexer / parser / inline parser. Carries `kind: ParseErrorKind` + `line` + `column`. |

### `asciidoc/` — text ⇒ AST ⇒ summary

A **pure** format library: every module is GTK-free and storage-free, importing only `enums` / `models` / `config`. The GTK `TextBuffer` renderer and tag table moved to `ui/note_render/`; the editor grammar moved to `ui/`.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `lexer.py` | 899 | `tokenize(source) -> tuple[Token, ...]`. **Line-based, context-free, permissive** — never raises on grammar issues; that is the parser's job. Public token dataclasses listed at the top. |
| `inline_parser.py` | 790 | `parse_inline(line, line_no) -> tuple[InlineNode, ...]`. **Strict** — every formatting marker must be paired; otherwise raises `ParseErrorKind.BAD_INLINE_SPAN` (or `UNTERMINATED_MONOSPACE`). |
| `parser.py` | 1410 | `parse(source) -> Document`. Recursive-descent, strict, exhaustive over tokens. Each syntactic failure maps to a specific `ParseErrorKind`. Header-attribute consumption captures `:tags:` and validates it via the shared `parse_tags_value` helper (`BAD_TAG_VALUE` on a malformed entry, `DUPLICATE_TAG_ATTRIBUTE` on a repeated `:tags:`); every other attribute name is still discarded. |
| `ast.py` | 460 | Frozen dataclasses for every AST node (`Document`, `Section`, `Paragraph`, `OrderedList`, …, `Bold`, `Italic`, `Link`, …). Children are `tuple[...]` for true immutability. `BlockNode` and `InlineNode` are closed unions. `Document` carries the parsed `tags: tuple[str, ...]` (sorted, lowercase, deduplicated) alongside `title` and `blocks`. |
| `summary.py` | 320 | `derive_summary(source) -> NoteSummary`. Parses once and reads title + snippet + tags off the AST (prose vs structure decided by an exhaustive `match`). **Never raises** — catches `ParseError` only and falls back to a permissive extraction so a mid-edit note stays saveable; the tag arm of the fallback walks the lexer's `AttributeEntryToken` stream and re-uses `parse_tags_value`, resolving any failure to empty tags. The single source of truth for the note-list summary and tag classification. |

### `storage/` — SQLite persistence

`protocols.py` is the typing surface every higher layer imports. Concrete classes are siblings.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `protocols.py` | 209 | `NoteRepositoryProtocol` (incl. `list_tags()` for the sidebar's *Tags* section), `AttachmentStoreProtocol` (now incl. `count_for_note` — a BLOB-free `SELECT COUNT(*)` for the note-list badge), `RendererProtocol`; the `AttachmentRejected` exception; PEP 695 resolver aliases `ImageBytesResolver` / `ColumnWidthResolver`. **Pure typing — no `sqlite3` or `gi` at runtime.** |
| `database.py` | 170 | Owns the single `sqlite3.Connection`. `autocommit=True`, `PRAGMA foreign_keys=ON`, composable `transaction()` (nested calls become `SAVEPOINT`). |
| `migrations.py` | 297 | All `CREATE TABLE` / `CREATE INDEX` / `CREATE TRIGGER` statements. Append-only `ALL_MIGRATIONS` tuple; `apply_pending()` is idempotent. v1 created the now-demolished notebooks schema + seed welcome note (title/snippet via `derive_summary`); v2 backfilled every note's cached `title`/`snippet` from `derive_summary`; v3 drops the notebook triggers / `notebook_id` column / `notebooks` table, creates the `note_tags` junction table, and re-derives every existing note's tag set via `derive_summary` to backfill `note_tags` (permissive — notes whose `:tags:` line is malformed land with zero tags). |
| `note_repository.py` | 220 | SQLite-backed `NoteRepositoryProtocol`. **Single owner of the `source → cached state` mapping**: `insert` and `update_source` derive `title`/`snippet`/`tags` from the source via `derive_summary`, write the cached columns, and replace the note's rows in `note_tags` (DELETE + INSERT) in the same transaction. Reads join `note_tags` so `Note.tags` is populated in one round trip — no N+1. `list_tags()` returns `((tag, count), …)` alphabetically for the sidebar. Row↔dataclass conversion lives in one place per direction; timestamps round-trip via ISO-8601. |
| `attachment_store.py` | 280 | BLOB-backed `AttachmentStoreProtocol`. Enforces `MAX_ATTACHMENT_BYTES` via `Path.stat()` **before** any bytes are read. Rejections raise `AttachmentRejected(reason=…)`. `count_for_note` is a BLOB-free `SELECT COUNT(*)` for the note-list badge. |

**Live schema (post-v3, defined in `migrations.py`):**

- `notes(id PK, title, source, snippet, created_at, modified_at)` + an index on `modified_at DESC`. No `notebook_id` column.
- `note_tags(note_id FK→notes ON DELETE CASCADE, tag, PRIMARY KEY (note_id, tag))` + an index on `tag`. Populated by the repository on every `insert` / `update_source`; the `ON DELETE CASCADE` removes a note's tag rows when the note is deleted.
- `attachments(id PK, note_id FK→notes ON DELETE CASCADE, filename, byte_size, mime_type, data BLOB)` + index on `note_id`.
- `schema_version(version PK)` records which migrations have been applied.

The pre-v3 `notebooks` table and the `notes.notebook_id` column are gone; v1's CREATE statements still ship in `migrations.py` for the benefit of upgrade paths but are immediately undone by v3 on any database newer than v0.

### `search/` — pure filters

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note_filter.py` | 192 | `filter_by_selection`, `filter_by_query`, `sort_notes`. The `Selection` discriminated union (`SmartSelection` over `SmartFilter.ALL` / `SmartFilter.UNTAGGED`, or `TagSelection` carrying a non-empty `frozenset[str]`) lives here. Multi-tag selection has **AND** semantics — a note appears iff every selected tag is on it. No clock dependency. |

### `controllers/` — UI⇄storage mediators

Controllers are the only place where storage calls + signal emission live together. Widgets never call repositories.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `app_state.py` | 220 | `AppState` GObject. Holds the **only** in-memory navigational state: `selection` (a `SmartSelection` / `TagSelection` union from `search.note_filter`), `selected_note_id`, `view_mode`, `query`. Selection mutators are restricted to `set_smart(SmartFilter)` and `toggle_tag(name)`; the controller owns the rules (smart filter wipes tag set; toggling the last tag off returns to `SmartSelection(ALL)`). Emits `selection-changed`, `selected-note-changed`, `view-mode-changed`, `query-changed` (all payload-free). |
| `note_controller.py` | 391 | `create_note`, `duplicate_note`, `request_delete`, `update_source`, `add_attachment`, `remove_attachment`. Also exports the free function `make_initial_source(selection)` — returns a seed source pre-filled with `:tags: …` from the current `TagSelection` (or just a title line for a `SmartSelection`) so the toolbar's *+New* hands tag intent through to the new note. Emits `notes-changed`, `attachment-rejected`, `storage-error`. Clock + id-gen are injected callables. |
| `_storage_errors.py` | 69 | Shared `capturing_storage_errors(emit)` context manager — single home for the *catch `sqlite3.DatabaseError`, emit a toast signal, re-raise* pattern. Private to the controllers package. |

**Signal flow at a glance:**

```
user gesture (UI)
       │
       ▼
controller method
       │  ── storage call (in `capturing_storage_errors(...)`)
       │  ── emit "notes-changed"                    ─► listeners re-query repository
       │  ── mutate AppState                          ─► AppState emits its own signal
       ▼
widgets refresh by reading from repositories + AppState
```

### `ui/` — GTK 4 widgets

This is the only layer that owns widget trees. Every widget is thin and unit-testable with fake controllers/repositories.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `application.py` | 277 | `NotesApplication(Gtk.Application)` — composes `Database`, `NoteRepository`, `AttachmentStore`, `AppState`, `NoteController`, then presents `MainWindow`. Single-instance via `FLAGS_NONE`. |
| `main_window.py` | 423 | `MainWindow` — the three-pane shell: sidebar │ note list │ `Gtk.Stack(view ↔ editor)`. Toolbar is set as the title bar. The initial window width is derived from the rendered article column (`_default_window_width` + `NoteView.preferred_column_width_px()`) so the fixed-width column opens fully visible. No notebook plumbing — the window is wired with the single note repository. |
| `sidebar.py` | 735 | Flat library navigation. Two model-driven sections: **Library** (a `Gtk.SingleSelection` over a `Gio.ListStore` of `All notes` + `Untagged`) and **Tags** (a `Gtk.MultiSelection` over the alphabetised `note_repository.list_tags()` output). Selection in one section clears the other — the rule is owned by `AppState`, both `ListView`s observe it. The *Tags* header reads `"Tags (N selected)"` when N > 0, with the parenthetical styled via the `.selection-count` accent class. Tag rows reserve column width for the leading ✓ so labels stay aligned across selected and unselected rows. Refresh on `notes-changed` rebuilds the tag store and drops tags from the selection that no longer exist. |
| `note_list.py` | 700 | Middle pane: header (`"{N} notes"` + sort dropdown — no notebook lead-in, no filter chips) and a sortable, filtered list. `compute_display_notes(...)` is a free function so tests don't need widgets. Each row has a bold title, a two-line dimmed snippet, an optional third line of dim `#tag` chips when `note.tags` is non-empty, and a right-aligned `📎 N │ date` meta line; per-note attachment counts come from the injected `AttachmentStoreProtocol` (`count_for_note`). |
| `note_view.py` | 1482 | Read pane. `ArticleContainer` enforces the fixed-width text column; `preferred_column_width_px()` exposes that column's outer width so `MainWindow` can size the initial window to it. Calls `TextBufferRenderer.render_into` on every change. Under the title, in VIEW mode only, renders a row of pill chips (one per tag, `.tag-chip-article`) — anchored inside the rendered text view via the renderer's `post_title_hook`, so it lays out between the title and the first body block; hidden in SOURCE mode where the raw `:tags:` line is visible in the editor. `_ArticleTextView` paints the wider tinted wash behind admonition / blockquote / code-block paragraphs (see `tag_table.WashSpec`). |
| `note_editor.py` | 1304 | Source pane (`GtkSource.View` + `GtkSource.Buffer`). Debounced autosave (`AUTOSAVE_DEBOUNCE_MS`). Stateless w.r.t. notes — reloads from repo on selection change. |
| `toolbar.py` | 359 | Top `Gtk.HeaderBar` — *New* button (calls `make_initial_source(app_state.selection)` so the new note inherits the current tag selection's `:tags:` line), search entry, an empty centre slot (no breadcrumb in the flat library), View/Source toggle, More menu (Duplicate/Delete). |
| `dialogs.py` | 124 | Shared modal dialogs — confirm-delete only (a callable matching `ConfirmDialogPresenter`). Production wires `Gtk.AlertDialog`; tests drive callbacks synchronously. The pre-tags `IconPickerPopover` is gone with the notebook UI. |
| `link_handler.py` | 386 | `LinkHandler.install(textview, ...)` — wires `EventControllerMotion` (cursor) + `GestureClick` (open on `released`). URI is launched via an injected `UriLauncherProtocol`; allowlist is `enums.LinkScheme`. |
| `_image_picker.py` | 152 | `FileDialogOpener` callable + `default_file_dialog_opener` wrapping `Gtk.FileDialog.open`. MIME filters mirror `enums.MimeKind`. Module is private so `note_editor.py` stays under pylint's `max-module-lines`. |
| `css/app.css` | 189 | Application stylesheet — loaded by `NotesApplication`. Styles the note-view parse-error banner, the library sidebar, the note-list rows (`.note-title` bold; `.note-snippet` / `.note-meta` / `.note-meta-separator` dimmed; `.tag-chip-row` dim third-line chips), and the article-view pill chips (`.tag-chip-article`). Sidebar tag selection reads as the leading ✓: `.sidebar .tag-list row:selected` (scoped to the Tags list by the `tag-list` class) replaces the theme's bold accent fill with a faint `alpha(currentColor, …)` tint plus a `color: inherit` reset, and `.tag-row-check` styles that ✓ image. The Tags header's `(N selected)` count uses `.selection-count` with the section-header font metrics (`font-size: 11px; letter-spacing: 0.06em`) so it sizes/aligns with `Tags`, coloured by the locally `@define-color`'d `@folio_selection_accent` (plain GTK 4 has no libadwaita `@accent_color`, which here resolved white-on-white). Most rules stay palette-safe via geometry/opacity; the named exception is that single `@folio_selection_accent` literal. Read via `importlib.resources`; ships in `folio.pyz` because the zipapp archives `src/` directly. |
| `language_spec.lang` | 353 | GtkSourceView 5 grammar driving source-editor syntax highlighting. Pure data, but **not** loaded from disk: it is compiled into `folio.gresource` (via `folio.gresource.xml`) and loaded at runtime through a `resource:///` search path — see section 8 and the `note_editor.py` invariants. The raw `.lang` is a build input only; it is *not* shipped in the zipapp. |
| `folio.gresource.xml` | 5 | Committed GResource manifest. Publishes `language_spec.lang` under `resource:///org/folio/language-specs`; `glib-compile-resources` compiles it to the generated (gitignored) `folio.gresource` that ships in the zipapp. |

#### `ui/note_render/` — AST ⇒ TextBuffer (GTK)

The GTK rendering of a parsed document. These two modules are the only consumers that need `gi` + `storage.protocols`, so they live under `ui` and keep `asciidoc` pure. The "tag table and note view must not drift" invariant is now an intra-`ui` concern.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `tag_table.py` | 619 | Builds the shared `Gtk.TextTagTable`. **Every visual style lives here, exactly once** (inline + heading + block-level admonition/blockquote/code styling). Block tags carry only text position; the tinted wash is painted by `_ArticleTextView` in `ui/note_view.py` via `build_wash_specs()`. |
| `textbuffer_renderer.py` | 1150 | `TextBufferRenderer.render_into(document, buffer, ...)`. Image bytes flow through an injected `ImageBytesResolver`; rebuilds the buffer each call. Only tables escape to an anchored widget (via the `WidgetAttacher` hook). An optional `post_title_hook` fires once per successful render with the anchor at the title/body boundary, used by `NoteView` to position the tag-chip row directly below the title. `_ScaledImagePaintable` caps image width at the column; decode failures fall through to `_PlaceholderImagePaintable`. |

---

## 5. Testing

- Tests use the standard library `unittest`. There is no extra runner.
- A module `M.py` is tested in the sibling file `test_M.py`. No global `tests/` directory.
- Storage tests run against a real `Database.in_memory()` with the v1 schema applied — the in-memory backend is the unit under test alongside the repository.
- Controllers are tested against dataclass-backed in-memory **fakes** of the storage protocols, plus a **fake clock** and **counter id-gen** for determinism. No GTK display, no temp directories.
- UI tests instantiate widgets directly and drive them with fake controllers/protocols. Asynchronous GTK 4 dialogs (`Gtk.FileDialog.open`, `Gtk.AlertDialog`) are wrapped behind callable type aliases (`FileDialogOpener`, `ConfirmDialogPresenter`) so tests pass a synchronous fake.
- **UI tests need a real GDK display.** Each such test (and several whole classes) is decorated `@unittest.skipUnless(_display_available(), "no GDK display")`, where `_display_available()` is true iff `Gdk.Display.get_default()` opens. With no display they *skip*, so a green run without one proves nothing about the widgets. The `make test` target supplies a display by running a headless Weston compositor; on the reference environment this is the difference between ~312 skipped and 1 skipped.
- **How `make test` wires the display** (see the comment in the `Makefile`): it launches `weston --backend headless --socket=test_notes` in the **background** (Weston is a long-running compositor — chaining it with `&&` would block forever and never reach the tests), waits for the `$XDG_RUNTIME_DIR/test_notes` socket to appear, then runs the suite with `WAYLAND_DISPLAY=test_notes` and `GSK_RENDERER=cairo` exported (the socket name alone is not enough — without `WAYLAND_DISPLAY` GTK opens no display) and kills Weston on exit. Requires the `weston` package. Running the suite directly (`python3 -B -m unittest …`) against your own display should export the same two variables.
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

---

## 7. Packaging & distribution

`folio` ships as a **zipapp** — a single `folio.pyz` run with `python folio.pyz`. There is no wheel, no console script, and no `[build-system]` in `pyproject.toml`; that file carries only project metadata and tool config. The zipapp is built from the `src/` tree directly (no staging copy) by `build_pyz.py`, which uses `zipapp.create_archive`'s API `filter` to drop `__pycache__`, `test_*.py`, and the grammar *sources* (`language_spec.lang`, `folio.gresource.xml`). Everything else — including `css/*.css` and the compiled `folio.gresource` — rides along. Because `src/__main__.py` lands at the archive root, zipapp uses it as the implicit entry point.

**Build dependency: `glib-compile-resources`** (ships with the GLib dev tooling, present on any GTK build host). It compiles the committed manifest `src/ui/folio.gresource.xml` + `src/ui/language_spec.lang` into the **generated, gitignored** bundle `src/ui/folio.gresource`. One shared `Makefile` rule (`$(GRES)`, exposed as the `resource` alias) builds it; `./run` calls `make resource`, and `make test` / `make pyz` depend on `$(GRES)` directly — so dev, test, and prod all build the artifact the same way.

**Runtime floor: GtkSourceView ≥ 5.4.** The grammar is loaded via a `resource:///` search path, which `GtkSource.LanguageManager.set_search_path` only accepts from 5.4 onward. This is a system typelib, not a pip dependency, so it cannot be expressed in `pyproject.toml`; it is satisfied by the project's GTK 4.18 target environment (5.4 long predates it).

**One grammar load path (the §1 invariant).** Both a source checkout and the packaged `folio.pyz` load the grammar from the compiled `folio.gresource` via the `resource:///` URI — *never* from a filesystem path (inside the zip such a path would point into the archive and the OS could not open it). The resource is registered exactly once behind the cached `LanguageManager` in `ui/note_editor.py`. A **missing** resource is a hard error (`FileNotFoundError`), not a silent fallback to plain-text highlighting — the fix is always "run `./run` / `make` so the resource is built". Because dev and prod share this single path, the unit suite (which depends on `$(GRES)`) already exercises the real loader; running `python folio.pyz` and confirming highlighting is a final check on the zip-packaged copy.

**Generated / gitignored artifacts:** `src/ui/folio.gresource` and `folio.pyz`. `make clean` removes both.

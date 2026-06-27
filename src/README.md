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
   (text→AST)     │   lexer → inline_parser → parser │  no storage. The GTK renderer
   (AST→summary)  │   → ast → summary                │  now lives in giruntime/ui/note_render.
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
| `system_docs` | `enums` (+ stdlib `importlib.resources`) | `storage`, `controllers`, `ui`, `gi`, `asciidoc` |
| `asciidoc` (pure: `ast`, `lexer`, `inline_parser`, `parser`, `summary`) | `enums`, `models`, `config` | `storage`, `controllers`, `ui`, `gi`, `storage.protocols` |
| `storage.protocols` | `enums`, `models` (uses `gi` only in `TYPE_CHECKING`) | everything else |
| `storage` (concrete) | `enums`, `models`, `config`, `system_docs` (gi-free seed source), `storage.protocols`, `sqlite3`, `asciidoc` (pure core, for `derive_summary`) | `gi`, `controllers`, `ui` |
| `search` | `enums`, `models` | `storage` (concrete), `controllers`, `ui`, `gi` |
| `giruntime/controllers` | `enums`, `models`, `config`, `search`, `storage.protocols`, `gi` (`GObject` / `Gio` — holds `NoteListStore` / `TagCountsModel`) | concrete `storage`, `giruntime/ui`, **`asciidoc`** (so the store cannot derive — the repository returns the derived `Note`) |
| `giruntime/ui` | everything below | — |

**`gi` (GTK) is configured once in `giruntime/__init__.py`** (the sole `gi.require_version` site) **and consumed only under `giruntime/ui/*`** (including `giruntime/ui/note_render/*`) **and `giruntime/controllers/*`** (for `GObject` signals). Anywhere else — including the whole of `asciidoc/*`, now a pure format library — it is a bug.

---

## 3. "Where do I touch to do X?"

| Change | Start here | Likely also touches |
| --- | --- | --- |
| Add a new enum value (icon, link scheme, etc.) | `enums.py` | any consumer that pattern-matches the enum; for `StrEnum`s persisted to disk, also add a migration |
| Add a new AsciiDoc construct | `asciidoc/ast.py` (new node) → `asciidoc/lexer.py` → `asciidoc/parser.py` → `giruntime/ui/note_render/textbuffer_renderer.py` → `giruntime/ui/note_render/tag_table.py` (new tag) → `giruntime/ui/language_spec.lang` (editor highlight) → **decide its snippet treatment in `asciidoc/summary.py`** (the `match` over `BlockNode`/`InlineNode` is exhaustive, so an unhandled new kind is a type error there). Purely structural inline nodes — e.g. `SoftBreak`, the parser-emitted soft-line-break joiner — skip the lexer and `language_spec.lang` and need only the AST union plus both renderer dispatch ladders and the summary flattener. A construct that adds its own parse errors also needs a `ParseErrorKind` member **and** a matching `giruntime/ui/note_view._message_for` arm (that `match` is exhaustive too); a tunable limit it enforces (e.g. a depth cap) belongs in `config/defaults.py`, not inline. Nesting that lives on an existing node (lists gained `ListItem.children`) needs no new `NodeKind` and may need no new tag — but every AST walker (`summary.py`, the renderer ladders, the help-coverage walker in `giruntime/ui/test_help_window.py`) must recurse into the new child axis. | `enums.py` (new `NodeKind`, possibly `ParseErrorKind` and/or a presentation enum like `ListNumberStyle`) |
| Add a parse error variant | `enums.py` `ParseErrorKind` → the parser site that detects it → `parser.py` tests | the user-facing copy in `_message_for` and the in-surface notice rendering (`_insert_error_notice`) in `giruntime/ui/note_view.py` |
| Change DB schema | **new** `Migration` appended to `storage/migrations.py` `ALL_MIGRATIONS` — never edit a shipped one | the repository that reads/writes the new column |
| Add a note-level user action | `giruntime/controllers/note_controller.py` (call the store, wrap in `capturing_storage_errors`, mutate `AppState`) → caller in `giruntime/ui/toolbar.py`, `giruntime/ui/note_editor.py`, or `giruntime/ui/attachments_panel.py` | `giruntime/controllers/note_list_store.py` if a new store mutation is needed; repository protocol if storage shape changes |
| Change persistence ordering (when the UI may show a change) | `giruntime/controllers/note_list_store.py` — the **DB-first** invariant lives here: persist through the repository first, commit in-memory + `items-changed` only on success; an edit is a `splice` replace at the same position | `giruntime/controllers/test_note_list_store.py` `DbFirstTests` |
| Change tag parsing or validation | `asciidoc/parser.py` `parse_tags_value` (strict path used by the parser; raises `ParseError(BAD_TAG_VALUE)` / `DUPLICATE_TAG_ATTRIBUTE`) — the same helper is reused by the permissive `_fallback_tags` arm of `asciidoc/summary.py`, so a single charset / normalisation rule covers both | `asciidoc/test_parser.py` `Tags*Tests`; `asciidoc/test_summary.py` `DeriveSummaryTags*Tests`; if the rule change affects how existing notes parse, a `Migration` that re-derives `note_tags` |
| Change rendered-view styling | `giruntime/ui/note_render/tag_table.py` (tag definitions) — every visual style lives in exactly one place, including block-level paragraph styling for admonitions / blockquotes / code blocks and the under-title metadata line (`TagName.METADATA`: dim-grey text + a `hairline` `WashSpec` rule). Block-level *tints* and the metadata hairline are painted at snapshot time by `ArticleTextView` in `giruntime/ui/note_view.py`, driven by `tag_table.build_wash_specs()`; the admonition / blockquote / code cards span the full prose column (wash insets `0`, the same as tables) with their text padded one M-width inside the card edge, so the cards line up with the surrounding prose rather than sitting indented. The same subclass also paints the note **sheet**: because the text view is the vertical scrollport, its CSS background is made transparent (the `article-text-view` class in `css/app.css`) and `do_snapshot` instead paints an opaque sheet covering the content plus the breathing part of the top and bottom margins, with a 1-px seam at each edge that meets the desk — so above and below the content the scroller's own background (the "desk") shows through with an **equal gap before and after** the note, and a note meets the desk at a visible edge at both ends, with no separately-invented colour to drift from the theme. Both the top and bottom margins are sized as `ARTICLE_TOP_MARGIN_LINES` / `ARTICLE_BOTTOM_MARGIN_LINES` + `ARTICLE_END_GAP_LINES` (`config/defaults.py`): the sheet claims only the breathing lines, so the same `ARTICLE_END_GAP_LINES` band is desk at each end (and at the bottom it doubles as the scrollable room that makes a note taller than the viewport end at a visible edge too) — wired by `NoteView.__init__` via `ArticleTextView.set_top_gap_px()` / `set_end_gap_px()`. Driven by `tag_table.build_note_end_wash()` and the pure `note_view._sheet_rect_for()` / `_top_seam_rect_for()` / `_bottom_seam_rect_for()` helpers. | rarely `giruntime/ui/note_render/textbuffer_renderer.py` for layout (the renderer is now fully pure-buffer — no construct escapes to a widget) |
| Change block-level tint colours or insets | `giruntime/ui/note_render/tag_table.py` — `_ADMONITION_TINTS`, `_BLOCKQUOTE_TINT`, `_CODE_BLOCK_TINT` for colours; `_ADMONITION_HMARGIN_PX`, `_BLOCKQUOTE_HMARGIN_PX`, `_BLOCKQUOTE_RIGHT_MARGIN_PX`, `_CODE_BLOCK_HMARGIN_PX` for the block box insets (all `0` today, so the cards span the full prose column like tables; the paragraph tags still add one M-width of internal padding so the text sits inside the card edge — raise a constant above `0` to re-introduce an outer indent for that kind); `_METADATA_*` (foreground / scale / gap / `_METADATA_RULE_TINT`) for the metadata line + its hairline; `_SHEET_BACKGROUND` (opaque sheet) + `_NOTE_END_RULE_TINT` (translucent seam), exposed via `build_note_end_wash()` → `NoteEndWash`, for the note sheet + its end edge. For **tables**, distinguish the two insets: the *text* inset is `config/defaults.TABLE_CELL_HPADDING_PX` (applied as the row tag's `left-margin` + a `2 ×` right-truncation reservation), while the *wash* inset (`_TABLE_BOX_INSET_PX`) stays `0` so the header band / row hairline still span the full column. The block constants feed both the paragraph tag margins (text position, `accumulative-margin = True`) and the `WashSpec` records (wash painter), so the two cannot drift; the metadata line adds `WashSpec.hairline` to switch the painter from a full fill to a 1-px bottom rule. | `test_tag_table.py` `WashSpecTests` + `BuildNoteEndWashTests`, `test_note_view.py` `ArticleTextViewWashRectTests` + `SheetAndSeamRectTests` + `ArticleTextViewSheetBottomTests` + `ArticleTextViewSheetTopTests` |
| Tune article column margins | `config/defaults.py` (the four `ARTICLE_*` multipliers — top / bottom / inner-hpadding, plus `ARTICLE_END_GAP_LINES`, the desk band reserved equally above and below the note) | none — `giruntime/ui/note_view.py` reads the constants once at `NoteView.__init__` and applies them to the inner `Gtk.TextView`'s four margins; the top and bottom margins are each `ARTICLE_TOP_MARGIN_LINES` / `ARTICLE_BOTTOM_MARGIN_LINES + ARTICLE_END_GAP_LINES` line heights, and the same gap pixels are passed to `ArticleTextView.set_top_gap_px()` / `set_end_gap_px()` so the painted sheet stops one desk band short of the scrollable top and bottom |
| Change rendered-view layout sizing | `giruntime/ui/note_view.py` `ArticleContainer` — a `Gtk.Widget` that also implements `Gtk.Scrollable` (Option C of the scrollbar fix plan), so the parent `Gtk.ScrolledWindow` keeps it as its **direct** child and interposes **no** `Gtk.Viewport`. It must stay a `Gtk.Widget` subclass (not `Gtk.Box`, whose `BoxLayout` layout manager silently intercepts `do_measure`/`do_size_allocate`). The two scroll axes have different owners: **vertical** is pass-through — the `vadjustment`/`vscroll-policy` are forwarded to the scrollable text view, which becomes the vertical scrollport and owns the v-extent (this is what fixes the first-launch missing-scrollbar bug for an image-last note); **horizontal** is container-owned — `do_size_allocate` configures the container's own `hadjustment` (`upper` = column, `page` = viewport), centres the column when the viewport is wider and offsets it by `−hadjustment.value` when narrower, with `Overflow.HIDDEN` clipping and a `value-changed` → `queue_allocate` re-layout. Because it parents its child by hand (`set_parent`), it must also unparent that child at teardown or GTK warns *"Finalizing … but it still has children left"*; PyGObject does not expose `dispose`, so it does this from `do_unroot` (rooted/production teardown) plus a `__del__` net (never-rooted standalone instances, e.g. tests), both via the guarded `_release_child`, and the same two hooks drop the `hadjustment` `value-changed` subscription via `_disconnect_hadjustment` | `giruntime/ui/test_note_view.py` `ArticleContainer*` tests (incl. `ArticleContainerScrollableTests`, `ArticleContainerScrollbarRegressionTests`, `ArticleContainerTeardownTests`) |
| Change the under-title metadata line (Created · Modified · tags) | `giruntime/ui/note_view.py` (`_insert_metadata_after_title` + `_format_metadata_line`; the `METADATA` hairline branch in `ArticleTextView._wash_rect_for_line`) and `giruntime/ui/note_render/tag_table.py` (`TagName.METADATA` tag + its `WashSpec`). Dates are formatted by `giruntime/ui/_dates.py` (`format_date_long`). | `giruntime/ui/note_render/textbuffer_renderer.py` (`post_title_hook` now inserts buffer text, no anchor); `giruntime/ui/test_note_view.py` `NoteViewMetadataTests`; `giruntime/ui/note_render/test_textbuffer_renderer.py` `PostTitleHookTests` |
| Change application chrome / CSS | `giruntime/ui/css/app.css` | no packaging change needed — the zipapp build archives `src/` directly, so any new asset under `giruntime/ui/` ships automatically (see section 8) |
| Change the initial window size | `giruntime/ui/main_window.py` — height is `_DEFAULT_WINDOW_HEIGHT_PX`; width is computed by `_default_window_width(...)` from `_SIDEBAR_INITIAL_POSITION_PX` + `_NOTE_LIST_INITIAL_POSITION_PX` + `_PANED_HANDLE_ALLOWANCE_PX` + the rendered article column + `_ARTICLE_SIDE_SLACK_PX`, clamped up to `_MIN_DEFAULT_WINDOW_WIDTH_PX`. The column term is `NoteView.preferred_column_width_px()`, so the default width tracks the body font and the column always opens fully visible / centred rather than overflowing into a horizontal scroll. | `giruntime/ui/test_main_window.py` `DefaultWindowWidthTests` + `test_constructs_and_reports_default_size`; `giruntime/ui/test_note_view.py` `NoteViewPreferredColumnWidthTests` |
| Change source-editor syntax highlight | `giruntime/ui/language_spec.lang` (GtkSourceView grammar) | the grammar is compiled into `folio.gresource`, so rebuild it (`./run` / `make resource` / `make test` do this automatically) for edits to take effect; the `.xml` manifest only changes if you add/rename grammar files |
| Tune a constant (sizes, quotas) | `config/defaults.py` | none — that is the point of this module |
| Change paths / XDG behaviour | `config/paths.py` | tests under `config/test_paths.py` |
| Add a new sort key / smart filter | `enums.py` (`NoteSortKey` / `SmartFilter`, e.g. the existing `ALL` / `UNTAGGED`) → `search/note_filter.py` (extend `matches_selection` / `comparator_for`, which the note list's `Gtk.CustomFilter` / `Gtk.CustomSorter` reuse) → `giruntime/ui/note_list.py` (dropdown) and / or `giruntime/ui/sidebar.py` (Library section row) | tests in `search/test_note_filter.py` |
| Change the note-list row title/snippet | the *derivation* in `asciidoc/summary.py` (`derive_summary`); the *presentation* in `giruntime/ui/note_list.py` (`_populate_row_box`, the `SignalListItemFactory` bind) + classes in `giruntime/ui/css/app.css` (`.note-title` / `.note-snippet` / `.note-meta`) | `storage/note_repository.py` only if the cached-column contract changes; a backfill migration if existing rows must be rewritten |
| Change the sidebar Tags section | `giruntime/controllers/tag_counts_model.py` (the derived count model) + `giruntime/ui/sidebar.py` (the `SortListModel` / factory binding) | `giruntime/controllers/test_tag_counts_model.py`; `giruntime/ui/test_sidebar.py` |
| Change selection / view-mode plumbing | `giruntime/controllers/app_state.py` (add a field as a GObject property + a rule-bearing mutator that calls `notify(...)`). Every UI widget that reacts subscribes via `notify::<prop>` (handlers take a trailing `GObject.ParamSpec`). **The MainWindow's `_on_view_mode_changed` handler is the single place that orchestrates editor-flush + view-refresh across the toggle — see the corresponding invariant in `giruntime/ui/main_window.py`.** | every UI widget that reacts to it |
| Add a new dialog | `giruntime/ui/dialogs.py` | the controller or widget that opens it |
| Change link/URL handling | `giruntime/ui/link_handler.py`; allowlist in `enums.LinkScheme` | `asciidoc/inline_parser.py` for scheme validation |
| Change attachment rules | `storage/attachment_store.py`; size cap in `config/defaults.MAX_ATTACHMENT_BYTES` — the only add-time gate (the old `MimeKind` type allow-list is removed; attachments are opaque blobs) | `giruntime/controllers/note_controller.py` for the toast wiring |
| Change the attachments panel (list / Add file / remove) | `giruntime/ui/attachments_panel.py` — header, cards, add/remove flows; size formatting in `giruntime/ui/_filesize.py`; the all-files dialog in `giruntime/ui/_file_picker.py` | `giruntime/controllers/note_controller.py` (`attachments-changed` emission); `giruntime/ui/note_list.py` if the 📎 badge refresh changes |
| Edit the help reference text | `src/system_docs/help.adoc` (authored in the supported subset). The §7 coverage test requires every `BlockNode` / `InlineNode` kind to appear, and the parse-clean test keeps it inside the subset; a new top-level bucket also needs a `HelpSection` enum member whose value is the new `==` heading text | `src/enums.py` (`HelpSection`) if buckets change; `giruntime/ui/test_help_window.py` |
| Add a new system document (bundled text/image) | `src/enums.py` (`SystemDocument` member → its package-relative filename) → drop the file under `src/system_docs/` → read it via `system_docs.load_text` / `load_bytes` | the consumer (`storage/migrations.py` for seed data, `giruntime/ui/help_window.py` for help assets); `src/system_docs/test___init__.py` |
| Change the help window (layout / navigation / links) | `giruntime/ui/help_window.py` — builds its reading pane from the shared `note_view.build_article_surface()` (the same fixed-width `ArticleContainer` + painted `ArticleTextView` the note view uses), so it gets the identical paper-on-desk column and correctly-placed block tints; it owns only its renderer (help image resolver), navigation marks, contents sidebar, and `link_handler`. Navigation marks + sidebar are keyed off `HelpSection`. It is **hide-on-close** so the application's single cached instance survives a close | `giruntime/ui/note_view.py` (`build_article_surface` / `ArticleSurface` — the shared surface) and `giruntime/ui/application.py` (the `app.help` action + `F1` accel + reuse-and-raise) and `giruntime/ui/toolbar.py` (the primary-menu Help item) |

---

## 4. Module reference

Test files (`test_*.py`) sit next to their subject — `test_M.py` covers `M.py`. They are omitted from the table below.

### `src/` — source root

`src/` is the source root, **not** an importable package — it has no
`__init__.py`, and its contents sit at the root of the `folio.pyz` archive,
so the GI-free top-level modules are imported by their bare names
(`config`, `enums`, `models`, `search`, `storage`, `asciidoc`). The two
GI-dependent layers instead live under the real `giruntime` package
(`giruntime.ui`, `giruntime.controllers`), which pins the GObject-Introspection
versions once in `giruntime/__init__.py`.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `__main__.py` | 43 | `python3 -B src/__main__.py` (dev) / `python folio.pyz` (zipapp) entry; builds `NotesApplication`, runs it, returns the exit code. |
| `enums.py` | 260 | **Single home** for every categorical constant. Persisted enums use `StrEnum` with stable values; transient ones use `auto()`. `MimeKind` is gone (attachments carry no classification), and `AttachmentRejectionReason` lost its `UNSUPPORTED_MIME_TYPE` member with it. `SystemDocument` (value = the package-relative filename of each bundled system document) and `HelpSection` (value = the exact `==` heading text of each help bucket) key the `system_docs` loader and the help window's navigation respectively. |

### `config/` — constants + paths

| File | LOC | One-line summary |
| --- | ---: | --- |
| `defaults.py` | 199 | Tunable constants (`MAX_ATTACHMENT_BYTES`, `TARGET_CHARS_PER_LINE`, the four `ARTICLE_*` multipliers — top / bottom / inner-hpadding margins plus `ARTICLE_END_GAP_LINES`, the desk band reserved equally above and below the note — `TABLE_CELL_HPADDING_PX` (symmetric horizontal cell padding for rendered tables: the row tag's `left-margin` insets the cell text, the renderer reserves `2 ×` it as the right-truncation budget; column boundaries and the full-column header band / row hairline are unchanged), plus `SNIPPET_MAX_CHARS` and `UNTITLED` consumed by `asciidoc/summary.py`) and the stable `SEED_WELCOME_NOTE_ID`. The welcome note's *source* no longer lives here — it moved to `system_docs/welcome.adoc` (`SystemDocument.WELCOME`), leaving `defaults.py` to tunable constants + identifiers only. |
| `paths.py` | 76 | `data_directory()`, `database_path()` — XDG-aware filesystem resolution. Each call is pure; mkdir is the only side effect. |

### `system_docs/` — bundled system documents (gi-free, config-tier)

The one home for the application's *system documents* — content the app
ships rather than the user authoring: the seed welcome note and the
AsciiDoc help reference (plus the small image the help's `image::`
example demonstrates). They are plain package data read gi-free via
`importlib.resources` — exactly how `giruntime/ui/css/app.css` ships —
**not** gresource content (only the editor grammar needs the gresource).
Config-tier: both `storage` (seed) and `giruntime` (help) read it.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `__init__.py` | 95 | The shared loader, keyed by the `SystemDocument` enum: `load_text(SystemDocument) -> str` for the `.adoc` sources, `load_bytes(SystemDocument) -> bytes` for the demo image. gi-free (`importlib.resources` only), imports only `enums`. `storage/migrations.py` reads `WELCOME`; `giruntime/ui/help_window.py` reads `HELP` + `HELP_DEMO_IMAGE`. |
| `welcome.adoc` | — | Seed welcome note source, byte-identical to the constant it replaced in `config/defaults.py`; v1 seeds it (a golden test pins its exact bytes). |
| `help.adoc` | — | The help reference, authored in the supported subset. Tested to parse clean and to exercise **every** `BlockNode` / `InlineNode` kind (so a new construct forces a help update) and to carry a real `image::` macro. |
| `help-demo.png` | — | Small demo image served to the help's `image::` example by the window's `ImageBytesResolver`; tested to decode as a real image (not the grey placeholder). |

### `models/` — frozen dataclasses

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note.py` | 87 | `Note` dataclass + the frozen `NoteSummary` `(title, snippet, tags)` value type. Both are frozen; updates produce new instances via the repository. `tags` is a sorted lowercase `tuple[str, ...]` derived from the source's `:tags:` header. Derivation lives in `asciidoc/summary.py`, not here (single classifier). |
| `attachment.py` | 54 | `Attachment` metadata (`id, note_id, filename, byte_size`) — deliberately has **no `data` field** (bytes live only in the `attachments.data` BLOB column) and **no type field** (attachments are opaque blobs; v4 dropped `mime_type`). |
| `parse_error.py` | 58 | `ParseError`, the **only** exception type raised by the AsciiDoc lexer / parser / inline parser. Carries `kind: ParseErrorKind` + `line` + `column`. |

### `asciidoc/` — text ⇒ AST ⇒ summary

A **pure** format library: every module is GTK-free and storage-free, importing only `enums` / `models` / `config`. The GTK `TextBuffer` renderer and tag table moved to `giruntime/ui/note_render/`; the editor grammar moved to `giruntime/ui/`.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `lexer.py` | 925 | `tokenize(source) -> tuple[Token, ...]`. **Line-based, context-free, permissive** — never raises on grammar issues; that is the parser's job. Public token dataclasses listed at the top. List-marker lines are matched as a *run* of `*`/`.` followed by whitespace; the run length rides on the token as `depth` (the lexer stays depth-policy-free — the cap and legal-transition rules are the parser's). |
| `inline_parser.py` | 790 | `parse_inline(line, line_no) -> tuple[InlineNode, ...]`. **Strict** — every formatting marker must be paired; otherwise raises `ParseErrorKind.BAD_INLINE_SPAN` (or `UNTERMINATED_MONOSPACE`). |
| `parser.py` | 1609 | `parse(source) -> Document`. Recursive-descent, strict, exhaustive over tokens. Each syntactic failure maps to a specific `ParseErrorKind`. Header-attribute consumption captures `:tags:` and validates it via the shared `parse_tags_value` helper (`BAD_TAG_VALUE` on a malformed entry, `DUPLICATE_TAG_ATTRIBUTE` on a repeated `:tags:`); every other attribute name is still discarded. Lists nest up to `config.defaults.MAX_LIST_DEPTH`: a stack-based `_parse_list` turns the flat run of depth-tagged list tokens into a recursive `ListItem.children` tree (mixed ordered/unordered allowed), raising `LIST_STARTS_BELOW_TOP_LEVEL` / `LIST_NESTING_SKIPS_LEVEL` / `LIST_NESTING_TOO_DEEP` in that precedence; a *top-level* marker of differing kind ends the run and starts a sibling list block (the original bullet→number split). |
| `ast.py` | 482 | Frozen dataclasses for every AST node (`Document`, `Section`, `Paragraph`, `OrderedList`, …, `Bold`, `Italic`, `Link`, …). Children are `tuple[...]` for true immutability. `BlockNode` and `InlineNode` are closed unions. `Document` carries the parsed `tags: tuple[str, ...]` (sorted, lowercase, deduplicated) alongside `title` and `blocks`. `ListItem` carries its own `inlines` **and** a `children` tuple of nested `OrderedList`/`UnorderedList` (`()` when a leaf), so list nesting is a recursive tree on the item rather than a flat level tag. |
| `summary.py` | 339 | `derive_summary(source) -> NoteSummary`. Parses once and reads title + snippet + tags off the AST (prose vs structure decided by an exhaustive `match`). **Never raises** — catches `ParseError` only and falls back to a permissive extraction so a mid-edit note stays saveable; the tag arm of the fallback walks the lexer's `AttributeEntryToken` stream and re-uses `parse_tags_value`, resolving any failure to empty tags. The list arm recurses into each item's `children`, so nested item text still reaches the snippet. The single source of truth for the note-list summary and tag classification. |

### `storage/` — SQLite persistence

`protocols.py` is the typing surface every higher layer imports. Concrete classes are siblings.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `protocols.py` | 209 | `NoteRepositoryProtocol` (`insert` / `update_source` now **return the persisted, derived `Note`** so `NoteListStore` can wrap it without re-reading or re-deriving), `AttachmentStoreProtocol` (incl. `count_for_note` — a BLOB-free `SELECT COUNT(*)` for the note-list badge), `RendererProtocol`; the `AttachmentRejected` exception; PEP 695 resolver aliases `ImageBytesResolver` / `ColumnWidthResolver`. `search()` / `list_modified_since()` / `list_tags()` are documented as **no longer on any UI path** (the note list filters in memory; the sidebar derives tag counts from the store). **Pure typing — no `sqlite3` or `gi` at runtime.** |
| `database.py` | 170 | Owns the single `sqlite3.Connection`. `autocommit=True`, `PRAGMA foreign_keys=ON`, composable `transaction()` (nested calls become `SAVEPOINT`). |
| `migrations.py` | 380 | All `CREATE TABLE` / `CREATE INDEX` / `CREATE TRIGGER` statements. Append-only `ALL_MIGRATIONS` tuple; `apply_pending()` is idempotent. v1 created the now-demolished notebooks schema + seed welcome note (its source read gi-free from `system_docs/welcome.adoc` via `SystemDocument.WELCOME`; title/snippet via `derive_summary`); v2 backfilled every note's cached `title`/`snippet` from `derive_summary`; v3 drops the notebook triggers / `notebook_id` column / `notebooks` table, creates the `note_tags` junction table, and re-derives every existing note's tag set via `derive_summary` to backfill `note_tags` (permissive — notes whose `:tags:` line is malformed land with zero tags); v4 drops the unused `attachments.mime_type` column (rows and BLOBs preserved). v1 stays frozen: reading the seed from a file preserves its data behaviour, and a golden test pins the exact seeded bytes. |
| `note_repository.py` | 220 | SQLite-backed `NoteRepositoryProtocol`. **Single owner of the `source → cached state` mapping**: `insert` and `update_source` derive `title`/`snippet`/`tags` from the source via `derive_summary`, write the cached columns, replace the note's `note_tags` rows (DELETE + INSERT) in the same transaction, **and return the persisted derived `Note`** (`update_source` recovers `created_at` via `UPDATE … RETURNING created_at`). `list_all()` is the one-time load source for `NoteListStore`. Reads join `note_tags` so `Note.tags` is populated in one round trip — no N+1. `search()` / `list_modified_since()` / `list_tags()` remain but are no longer consumed by the UI (annotated as legacy). Row↔dataclass conversion lives in one place per direction; timestamps round-trip via ISO-8601. |
| `attachment_store.py` | 244 | BLOB-backed `AttachmentStoreProtocol`. Attachments are **opaque blobs** — no type gate; the only add-time validations are the `MAX_ATTACHMENT_BYTES` cap, enforced via `Path.stat()` **before** any bytes are read, and source readability. Rejections raise `AttachmentRejected(reason=…)` with `EXCEEDS_SIZE_LIMIT` or `UNREADABLE_SOURCE` (`UNSUPPORTED_MIME_TYPE` is gone with the allow-list). `count_for_note` is a BLOB-free `SELECT COUNT(*)` for the note-list badge. |

**Live schema (post-v4, defined in `migrations.py`):**

- `notes(id PK, title, source, snippet, created_at, modified_at)` + an index on `modified_at DESC`. No `notebook_id` column.
- `note_tags(note_id FK→notes ON DELETE CASCADE, tag, PRIMARY KEY (note_id, tag))` + an index on `tag`. Populated by the repository on every `insert` / `update_source`; the `ON DELETE CASCADE` removes a note's tag rows when the note is deleted.
- `attachments(id PK, note_id FK→notes ON DELETE CASCADE, filename, byte_size, data BLOB)` + index on `note_id`. The v1 `mime_type` column is dropped by v4 — attachments carry no content-type classification.
- `schema_version(version PK)` records which migrations have been applied.

The pre-v3 `notebooks` table and the `notes.notebook_id` column are gone; v1's CREATE statements still ship in `migrations.py` for the benefit of upgrade paths but are immediately undone by v3 on any database newer than v0.

### `search/` — pure filters

| File | LOC | One-line summary |
| --- | ---: | --- |
| `note_filter.py` | 192 | `filter_by_selection`, `filter_by_query`, `sort_notes`. The `Selection` discriminated union (`SmartSelection` over `SmartFilter.ALL` / `SmartFilter.UNTAGGED`, or `TagSelection` carrying a non-empty `frozenset[str]`) lives here. Multi-tag selection has **AND** semantics — a note appears iff every selected tag is on it. No clock dependency. |

### `giruntime/` — GI-pinned layer root

The package that contains every module importing `gi` at runtime
(`giruntime.ui` and `giruntime.controllers`). Importing any submodule runs
this `__init__` first, so the GObject-Introspection versions are pinned once
per process before any `from gi.repository import …` executes — on the app
entry path and on every test that imports a `giruntime.*` module.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `__init__.py` | 29 | **Single** `gi.require_version` site. Pins the full namespace union used anywhere in the tree (`GLib`, `GObject`, `Gio`, `Gdk`, `Gsk`, `Gtk`, `Pango`, `Graphene`, `GtkSource`). Pins versions only — it must **not** import a `gi.repository` namespace, so merely importing the package loads no typelib. No per-module `require_version` exists anywhere else. |

### `giruntime/controllers/` — UI⇄storage mediators

Controllers are the only place where storage calls + signal emission live together. Widgets never call repositories — they bind to the in-memory note store (and models derived from it). `controllers` may import `gi` (`GObject` / `Gio`, never `Gtk`) but must **not** import `asciidoc`.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `app_state.py` | 220 | `AppState` GObject. Holds the **only** in-memory navigational state, exposed as GObject **properties** observed via `notify::<prop>`: `selection` (a `SmartSelection` / `TagSelection` union from `search.note_filter`), `selected-note-id`, `view-mode`, `query`. `selection` / `selected-note-id` / `view-mode` are **read-only** with rule-bearing mutators (`set_smart(SmartFilter)`, `toggle_tag(name)`, `set_selected_note_id`, `set_view_mode`) that enforce the rules then `notify(...)`; the controller owns the selection rules (smart filter wipes tag set; toggling the last tag off returns to `SmartSelection(ALL)`). `query` is read/write and **bound bidirectionally** to the toolbar search entry (so it is stored verbatim — no normalisation — which the binding's correctness depends on). |
| `note_item.py` | 75 | `NoteItem(GObject.Object)` — the element type of `NoteListStore`. Wraps one immutable `Note`, exposing `note-id` / `title` / `snippet` as `READABLE` GObject properties for the row factory and the full value via a plain `note` property for filter/sort/body reads. Never mutated in place — an edit produces a replacement item (see the store). |
| `note_list_store.py` | 215 | `NoteListStore(Gio.ListStore)` — the UI's **in-memory, write-through source of truth** for full notes (body included). `load()` populates it once from `repository.list_all()`; `create` / `update` / `delete` persist through the repository **DB-first** (the returned derived `Note` is wrapped) and only then commit the in-memory change + `items-changed` (an edit is a `splice` replace, never an in-place mutation). `get_note(id)` is the resident body read (raises `KeyError` like the repository). Owns the injected clock + id-gen (moved off the controller). Does **not** catch storage errors — they propagate so the store never gets ahead of disk. |
| `tag_counts_model.py` | 170 | `TagCountsModel(Gio.ListModel)` + `TagItem` — a derived model that aggregates live tag counts off the note store. Keeps a `_shadow` of each row's tag set (because `items-changed` omits the removed items), incrementing/decrementing on every source change; a `0↔1` transition adds/removes a `TagItem` row, anything else is a count-only `notify::count`. The sidebar binds a `SortListModel` over it. |
| `note_controller.py` | 256 | `create_note`, `duplicate_note`, `request_delete`, `update_source`, `add_attachment`, `remove_attachment(attachment_id, note_id)`. Persistence is delegated to `NoteListStore` (DB-first); the controller wraps store calls in `capturing_storage_errors(...)` for the `storage-error` toast and mutates `AppState` (select the new note, clear selection on delete). Also exports the free function `make_initial_source(selection)` — a seed source pre-filled with `:tags: …` from the current `TagSelection` (or just a title line for a `SmartSelection`). Signals: `attachment-rejected`, `attachments-changed` (a **narrow per-note event** carrying the note id, emitted after a successful attachment add/remove — attaching no longer touches the note source, so this is what refreshes the attachments panel and the note-list 📎 badge), `storage-error` — there is **no** `notes-changed` (panes observe the store). |
| `_storage_errors.py` | 69 | Shared `capturing_storage_errors(emit)` context manager — single home for the *catch `sqlite3.DatabaseError`, emit a toast signal, re-raise* pattern. Private to the controllers package. |

**Signal flow at a glance:**

```
user gesture (UI)
       │
       ▼
controller method
       │  ── store.create/update/delete (in `capturing_storage_errors(...)`)
       │        └─ NoteListStore: persist DB-first ─► then items-changed
       │             └─► FilterListModel → SortListModel → ListView (note list)
       │             └─► TagCountsModel → SortListModel → ListView (sidebar tags)
       │  ── mutate AppState                          ─► AppState fires notify::<prop>
       ▼
widgets refresh by observing the store's items-changed + AppState
```

There is **no** `notes-changed` signal. The note list binds a
`Gtk.FilterListModel` / `Gtk.SortListModel` / `Gtk.ListView` chain over the
`NoteListStore`, and the sidebar binds a `TagCountsModel` over the same store,
so a create / edit / delete ripples to both panes through `items-changed`
without `MainWindow` arbitrating. `NoteView` re-renders on a store
`items-changed` that touches the displayed note. `MainWindow` therefore owns a
**single** subscription — `AppState:notify::view-mode` (flush editor + refresh
view, then swap the stack).

Attachment mutations are the one change `items-changed` cannot carry: adding
or removing an attachment never touches the note source (the panel inserts no
macro), so no store replace happens. They ride the controller's
`attachments-changed` signal instead — a **narrow per-note event** (the
affected note id), not a coarse fan-out. Two observers: the attachments panel
reloads when the changed id is the selected note, and the note list
re-populates that note's bound row so the 📎 badge recomputes via
`count_for_note`.

`AppState` exposes its four navigational fields as GObject properties;
widgets subscribe to `notify::selection` / `notify::selected-note-id` /
`notify::view-mode` / `notify::query` rather than to bespoke signals
(each handler takes a trailing `GObject.ParamSpec`). The toolbar search
entry's `text` is bound *bidirectionally* to `AppState:query`, so the
truth updates per keystroke; the note list invalidates its in-memory
`Gtk.CustomFilter` on each change (no throttle — re-filtering the resident
list is cheap).

### `giruntime/ui/` — GTK 4 widgets

This is the only layer that owns widget trees. Every widget is thin and unit-testable with fake controllers/repositories.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `application.py` | 408 | `NotesApplication(Gtk.Application)` — composes `Database`, `NoteRepository`, `NoteListStore` (built then `load()`-ed once), `AttachmentStore`, `AppState`, `NoteController`, then presents `MainWindow`. The initial-note selection reads the welcome/newest note from the **store**. Single-instance via `FLAGS_NONE`. Also registers the app-scoped `help` action (`F1`) and owns the single, non-modal `HelpWindow` (reuse-and-raise via `_ensure_help_window`; the window is hide-on-close so the cached instance survives a close). **App lifetime is bound to the main window**, not the registered-window set: the main window's `close-request` calls `Gtk.Application.quit` (`_on_main_window_close_request`), because the hide-on-close help window would otherwise stay registered-but-hidden and keep the process alive after the main window closed. Tested in `giruntime/ui/test_application.py`. |
| `help_window.py` | 471 | `HelpWindow(Gtk.ApplicationWindow)` — the standalone, non-modal AsciiDoc help reference. Builds its reading pane from the shared **`note_view.build_article_surface()`** (fixed-width `ArticleContainer` + painted `ArticleTextView` + tag table + buffer, washes installed, font-relative margins applied), then renders `system_docs/help.adoc` through the shared pipeline (`asciidoc.parser` → `note_render.TextBufferRenderer` → `note_render.tag_table`) into it — so the help gets the **same fixed-width paper-on-desk column** and the same correctly-placed block tints (admonition / blockquote / code washes) a rendered note has, rather than a flat full-width buffer. The window default width is sized from the surface's `outer_column_width_px` so the column opens framed by desk. It is **hide-on-close** (`set_hide_on_close(True)`): the application keeps one cached instance and re-`present`s it, so closing must hide rather than destroy (destroy-on-close left the reused instance a chrome-less, dead-close-button window). Two-pane: a `HelpSection`-keyed contents sidebar — a list of navigation **commands** (activating a row scrolls to a `Gtk.TextMark` placed at that section's heading in a post-render pass); it holds **no selection** (`Gtk.SelectionMode.NONE`, rows activatable + non-selectable) because a persistent highlight would misrepresent the scroll position once the reader scrolls away — │ the surface's `ArticleContainer` in a `Gtk.ScrolledWindow`. Wires the renderer's `CellWidthMeasurer` (from the shared article view's Pango context, via `note_view.make_cell_width_measurer` — tables render as inline tab-array buffer text, no widget) and an `ImageBytesResolver` mapping the help's image filename to the bundled demo bytes; reuses `LinkHandler` so example links open. |
| `main_window.py` | 434 | `MainWindow` — the three-pane shell: sidebar │ note list │ `Gtk.Stack(view ↔ editor)`. Toolbar is set as the title bar. The initial window width is derived from the rendered article column (`_default_window_width` + `NoteView.preferred_column_width_px()`) so the fixed-width column opens fully visible. Threaded with the single `NoteListStore` (no repository). Owns a **single** signal subscription: `AppState:notify::view-mode` (flush editor + refresh view, then swap the stack). There is no `notes-changed` fan-out — the panes self-update by observing the store's `items-changed`. |
| `sidebar.py` | 730 | Flat library navigation. Two model-driven sections: **Library** (a `Gtk.SingleSelection` over a `Gio.ListStore` of `All notes` + `Untagged`, counts derived from the note store) and **Tags** (a `Gtk.MultiSelection` over a `Gtk.SortListModel` wrapping the derived `TagCountsModel` — alphabetised by a `Gtk.StringSorter` on the tag name). Selection in one section clears the other — the rule is owned by `AppState`, both `ListView`s observe it. A selected tag row reads as the **theme selection pill** (no leading ✓). A plain **single click** toggles a tag additively (no Shift/Ctrl) via a per-row `Gtk.GestureClick` → `_on_tag_row_clicked`; tags **AND** together and the truth flows through `AppState`. The *Tags* header reads `"Tags (N selected)"` when N > 0. Counts and membership update **live** off the store's `items-changed`; when a tag's last note goes away the `TagCountsModel` drops the row and the sidebar drops it from the `AppState` selection. |
| `note_list.py` | 544 | Middle pane: header (`"{N} notes"` over the **filtered** model + sort dropdown) and a `Gtk.ListView` bound to `SingleSelection(SortListModel(FilterListModel(NoteListStore)))`. The `Gtk.CustomFilter` / `Gtk.CustomSorter` reuse the per-item predicates in `search.note_filter` (`matches_selection`, `matches_query`, `comparator_for`); a `notify::query` re-normalises the needle and invalidates the filter (no throttle — in-memory re-filter is cheap). Selection is one source of truth (`AppState`): a row click writes through `SingleSelection::notify::selected`, a programmatic selection mirrors back. Each row (a `SignalListItemFactory`) has a bold title, a two-line dimmed snippet, an optional `#tag` chip row, and a right-aligned `📎 N │ date` meta line; the attachment count comes from the injected `AttachmentStoreProtocol` (`count_for_note`), dates from `giruntime/ui/_dates.py` (`format_date_short`). Now also injected with the `NoteController`: the factory's `bind`/`unbind` pair tracks bound rows by note id, and the controller's `attachments-changed` re-populates the affected bound row so the 📎 badge stays live without a synthetic `items-changed`. |
| `note_view.py` | 2118 | Read pane. `ArticleContainer` (a `Gtk.Widget` + `Gtk.Scrollable`) enforces the fixed-width text column **and** owns the pane's scrolling: it is the `ScrolledWindow`'s direct child (no interposed viewport), forwarding the vertical adjustment to the text view and configuring its own horizontal adjustment for the column. `preferred_column_width_px()` exposes that column's outer width so `MainWindow` can size the initial window to it. The body is read from the **in-memory `NoteListStore`** (`get_note`), never the database. Calls `TextBufferRenderer.render_into` on every change. Re-renders on `notify::selected-note-id` and on a store `items-changed` that touches the displayed note (an edit replaces that note's row). Directly under the title it inserts a dim-grey **metadata line** — `Created <date>  ·  Modified <date>  ·  #tag …` — via the renderer's `post_title_hook`. When the source fails to parse, `refresh` clears the buffer and renders an **in-surface error notice** into it (`_insert_error_notice`: a centred amber warning glyph, headline, the `_message_for` message, and a recovery hint, styled by the `TagName.ERROR_NOTICE_*` tags) — so there is no always-present banner widget reserving space above the pane, and `_error_message` mirrors what is on screen. Image BLOBs are the only on-demand disk read, fetched through `AttachmentStoreProtocol.get_bytes()` when the renderer hits an image macro. The fixed-width reading surface — the painted `ArticleTextView` parented into an `ArticleContainer`, with the tag table, buffer, washes (`install_wash_specs_from_table`), and font-relative margins all wired — is assembled by the module-level **`build_article_surface()` → `ArticleSurface`**, the single constructor `help_window.py` shares so a note and the help reference render identically. The module also exports **`make_cell_width_measurer()`** — the production `CellWidthMeasurer` (a `Pango.Layout`-backed text-width probe off the article view's context) both panes wire into their renderer so table cells fit their columns identically. `ArticleTextView` (public) paints the tinted wash + the metadata hairline; its CSS background is transparent (the `article-text-view` class) so `do_snapshot` paints the note **sheet** itself — opaque over the content plus the breathing part of the top and bottom margins — letting the scroller's background (the "desk") show through with an equal gap above and below the sheet; the sheet meets the desk directly, with no rule drawn at the boundary. The top and bottom margins are each `ARTICLE_TOP_MARGIN_LINES` / `ARTICLE_BOTTOM_MARGIN_LINES + ARTICLE_END_GAP_LINES`; the sheet covers only the breathing part, so the same desk band (`set_top_gap_px()` / `set_end_gap_px()`) frames the note before and after it (and at the bottom gives a long note a visible end too). Via `build_sheet_wash()` + the pure `_sheet_rect_for()` helper. |
| `note_editor.py` | 664 | Source pane (`GtkSource.View` + `GtkSource.Buffer`) with the `AttachmentsPanel` embedded below the editor's `ScrolledWindow`. The edit toolbar is **removed** (its buttons duplicated typeable markup; attaching moved to the panel's Add-file button), which also removed the wrap/insert pure helpers and dropped the file under pylint's line ceiling. Debounced autosave (`AUTOSAVE_DEBOUNCE_MS`) routes through `NoteController.update_source` → the store. Stateless w.r.t. notes — reloads the selected note's body from the **store** (`note_store.get_note`) on selection change. Accepts the optional attachment store and the `FileDialogOpener`, both forwarded to the panel. |
| `attachments_panel.py` | 326 | Per-note attachment management embedded in the editor pane: `ATTACHMENTS · N` header, an *Add file* button, and one card per attachment (one **generic** icon — attachments carry no type — filename, human-readable size, remove button). Hidden while no note is selected. Add routes through `NoteController.add_attachment` and **inserts nothing into the note body**; remove is immediate (no confirm). Reloads on `notify::selected-note-id` and on `attachments-changed` for the selected note, plus synchronously after its own calls. |
| `toolbar.py` | 425 | Top `Gtk.HeaderBar` — *New* button (calls `make_initial_source(app_state.selection)` so the new note inherits the current tag selection's `:tags:` line), search entry whose `text` is **bound bidirectionally** to `AppState:query` (GObject's own echo-suppression replaces the old guard flag and removes the cursor-reset that reversed typed characters), an empty centre slot (no breadcrumb in the flat library), View/Source toggle (kept on explicit `notify::view-mode` handlers fenced by `_suppress_signal_writeback`, since the enum maps to two toggle buttons), the note-scoped More menu (Duplicate/Delete), and an app-scoped primary (hamburger) menu whose `Gio.Menu` model surfaces *Help* → the application's `app.help` action. The delete confirm reads the target note from the **store** (`get_note`), not the repository. |
| `dialogs.py` | 124 | Shared modal dialogs — confirm-delete only (a callable matching `ConfirmDialogPresenter`). Production wires `Gtk.AlertDialog`; tests drive callbacks synchronously. The pre-tags `IconPickerPopover` is gone with the notebook UI. |
| `link_handler.py` | 386 | `LinkHandler.install(textview, ...)` — wires `EventControllerMotion` (cursor) + `GestureClick` (open on `released`). URI is launched via an injected `UriLauncherProtocol`; allowlist is `enums.LinkScheme`. |
| `_file_picker.py` | 115 | `FileDialogOpener` callable + `default_file_dialog_opener` wrapping `Gtk.FileDialog.open` (renamed from `_image_picker.py`). Offers **all files** — the image MIME filters went with the `MimeKind` allow-list; the size cap inside `AttachmentStore` is the authoritative gate. Private helper consumed by `attachments_panel.py` (injected through `note_editor.py`). |
| `_filesize.py` | 70 | Shared human-readable byte-size formatting — `format_byte_size` (`1 KB`, `180 KB`, `2.3 MB`; **binary** convention, 1 KB = 1024 B, matching `MAX_ATTACHMENT_BYTES` reading as "10 MB"). Mirrors the `_dates.py` sibling-helper pattern. Pure — no GTK. |
| `_dates.py` | 55 | Shared locale-independent date formatting — `format_date_short` (`Apr 14`, note-list meta) and `format_date_long` (`Apr 14, 2026`, rendered-view metadata line), both off one `_MONTH_ABBREVIATIONS` table. Private helper imported by `note_list.py` and `note_view.py` so the two sibling widgets don't cross-import presentation helpers. Pure — no GTK, no clock. |
| `css/app.css` | 132 | Application stylesheet — loaded by `NotesApplication`. Styles the article text view (`.article-text-view` + its `text` node have a **transparent** background, so `ArticleTextView.do_snapshot` can paint the note sheet itself and let the scroller's background show below a short note — see `note_view.py`), the library sidebar, and the note-list rows (`.note-title` bold; `.note-snippet` / `.note-meta` / `.note-meta-separator` dimmed; `.tag-chip-row` dim third-line chips). The note-view parse error has **no** CSS here — it is rendered as tagged buffer text (`TagName.ERROR_NOTICE_*` in `tag_table.py`), so there is no `.note-view-banner` rule. Sidebar tag rows carry **no** selection styling of their own — a selected row falls through to the generic `.sidebar row:selected` theme pill (same as the Library list), so there is no `.tag-list` override and no `.tag-row-check`. The rendered-view metadata line is buffer text + a painted hairline (in `tag_table.py` / `note_view.py`), so there is no `.tag-chip-article` rule either. The Tags header's `(N selected)` count uses `.selection-count` with the section-header font metrics (`font-size: 11px; letter-spacing: 0.06em`) so it sizes/aligns with `Tags`, coloured by the locally `@define-color`'d `@folio_selection_accent` (plain GTK 4 has no libadwaita `@accent_color`, which here resolved white-on-white). Most rules stay palette-safe via geometry/opacity; the named exception is that single `@folio_selection_accent` literal. Read via `importlib.resources`; ships in `folio.pyz` because the zipapp archives `src/` directly. |
| `language_spec.lang` | 353 | GtkSourceView 5 grammar driving source-editor syntax highlighting. Pure data, but **not** loaded from disk: it is compiled into `folio.gresource` (via `folio.gresource.xml`) and loaded at runtime through a `resource:///` search path — see section 8 and the `note_editor.py` invariants. The raw `.lang` is a build input only; it is *not* shipped in the zipapp. |
| `folio.gresource.xml` | 5 | Committed GResource manifest. Publishes `language_spec.lang` under `resource:///org/folio/language-specs`; `glib-compile-resources` compiles it to the generated (gitignored) `folio.gresource` that ships in the zipapp. |

#### `giruntime/ui/note_render/` — AST ⇒ TextBuffer (GTK)

The GTK rendering of a parsed document. These two modules are the only consumers that need `gi` + `storage.protocols`, so they live under `giruntime/ui` and keep `asciidoc` pure. The "tag table and note view must not drift" invariant is now an intra-`giruntime/ui` concern.

| File | LOC | One-line summary |
| --- | ---: | --- |
| `tag_table.py` | 965 | Builds the shared `Gtk.TextTagTable`. **Every visual style lives here, exactly once** (inline + heading + block-level admonition/blockquote/code styling, the table-row tags `TagName.TABLE_HEADER` / `TagName.TABLE_ROW` — `wrap-mode = NONE`, a `left-margin` of `TABLE_CELL_HPADDING_PX` (`accumulative-margin = True`) that insets the cell *text* while the wash still spans the full column, plus a fill / hairline `WashSpec`, the under-title `TagName.METADATA` line, and the four centred lines of the in-surface parse-error notice, `TagName.ERROR_NOTICE_ICON` … `ERROR_NOTICE_HINT` — centred, explicitly coloured, no wash). Block tags carry only text position; the tinted wash — the table header band, and the metadata line's / table data rows' 1-px `hairline` rule — are painted by `ArticleTextView` in `giruntime/ui/note_view.py` via `build_wash_specs()` (table box insets stay `0`, so the band / rule reach both column edges regardless of the text inset). The note sheet colour (`SheetWash`, via `build_sheet_wash()`: an opaque `_SHEET_BACKGROUND`) lives here too, painted by the same subclass as the page behind a note's content. |
| `textbuffer_renderer.py` | 1544 | `TextBufferRenderer.render_into(document, buffer, ...)`. Image bytes flow through an injected `ImageBytesResolver`; rebuilds the buffer each call. **No construct escapes to a widget** — tables now render as native, selectable/copyable buffer text: each row is one logical line of tab-separated cells aligned by a per-table `Pango.TabArray` (minted as an anonymous tag and swept each render, like the per-link URL tags), tagged `TagName.TABLE_HEADER` (a tint band + bold) or `TagName.TABLE_ROW` (a bottom hairline). Cells can't wrap (the row tags set `wrap-mode = NONE`) and are padded symmetrically: the row tag's `left-margin` insets the cell text by `TABLE_CELL_HPADDING_PX`, and each cell is measured through the injected `CellWidthMeasurer` and truncated with an ellipsis to its column width less `2 × TABLE_CELL_HPADDING_PX` (the reserved right padding, which also keeps a fitted cell short of its tab stop so the rest of the row stays aligned); copying a truncated cell therefore yields the truncated display text (the rendered buffer is a read-only projection of the source). Nested lists emit recursively with a 1-based `depth`: each line is indented by `_LIST_ITEM_INDENT × depth` (literal spaces), unordered items step through a depth→glyph table (`•`/`◦`/`▪`) and ordered items through a depth→`ListNumberStyle` table (arabic/lower-alpha/lower-roman) via `_format_ordinal`, both tables sized to `MAX_LIST_DEPTH`; only the top-level list appends the trailing blank line. An optional `post_title_hook` fires once per successful render with the **buffer** positioned at the title/body boundary; `NoteView` uses it to *insert* the metadata line's text there. `_ScaledImagePaintable` caps image width at the column; decode failures fall through to `_PlaceholderImagePaintable`. |

---

## 5. Testing

- Tests use the standard library `unittest`. There is no extra runner.
- A module `M.py` is tested in the sibling file `test_M.py`. No global `tests/` directory.
- Storage tests run against a real `Database.in_memory()` with the v1 schema applied — the in-memory backend is the unit under test alongside the repository.
- Controllers are tested against dataclass-backed in-memory **fakes** of the storage protocols, plus a **fake clock** and **counter id-gen** for determinism. No GTK display, no temp directories.
- UI tests instantiate widgets directly and drive them with fake controllers/protocols. Asynchronous GTK 4 dialogs (`Gtk.FileDialog.open`, `Gtk.AlertDialog`) are wrapped behind callable type aliases (`FileDialogOpener`, `ConfirmDialogPresenter`) so tests pass a synchronous fake.
- **UI tests need a real GDK display.** Each such test (and several whole classes) is decorated `@unittest.skipUnless(_display_available(), "no GDK display")`, where `_display_available()` is true iff `Gdk.Display.get_default()` opens. With no display they *skip*, so a green run without one proves nothing about the widgets. The `make test` target supplies a display by running a headless Weston compositor; on the reference environment this is the difference between ~312 skipped and 1 skipped.
- **How `make test` wires the display** (see the comment in the `Makefile`): it launches `weston --backend headless --socket=test_notes` in the **background** (Weston is a long-running compositor — chaining it with `&&` would block forever and never reach the tests), waits for the `$XDG_RUNTIME_DIR/test_notes` socket to appear, then runs the suite with `WAYLAND_DISPLAY=test_notes` and `GSK_RENDERER=cairo` exported (the socket name alone is not enough — without `WAYLAND_DISPLAY` GTK opens no display) and kills Weston on exit. Requires the `weston` package. Running the suite directly (`python3 -B -m unittest …`) against your own display should export the same two variables.
- **`GSK_RENDERER=cairo` is mandatory, not cosmetic.** A few UI tests `present()` a real toplevel (e.g. `giruntime/ui/test_sidebar.py` `IconColumnAlignmentTests`, which needs realised geometry to compare icon x-origins) and then pump the GLib main loop. Presenting a window makes GTK build its GPU renderer — GL before GTK 4.16, Vulkan from 4.16 on — against the headless Weston surface. On a host whose GL/Vulkan stack does not cleanly fall back to software, that renderer **segfaults inside the driver during the next main-loop iteration** (the crash surfaces in `GLib.MainContext.iteration`, not in any project code). The cairo software renderer never touches GL/Vulkan/EGL, so forcing it makes these tests deterministic and crash-proof everywhere.
- **The `MainWindow` tests share one registered `Gtk.Application`** (`giruntime/ui/test_main_window.py` `_test_application()`, memoised with `functools.cache`). GTK supports a single registered `GtkApplication` per process — the first to register becomes `g_application_get_default()` and installs process-global state, and a second *registered* one is unsupported and crashes (segfault). Building a fresh application per test therefore must be avoided; the suite registers one application once and reuses it for every window (a `Gtk.ApplicationWindow` may share its application with others). Registering once before any window is added also suppresses GTK's "added before startup" warning. A per-test id (unique or shared) is the wrong axis: a *shared* id collides on the session bus (`An object is already exported …`), while *unique* ids let every application register and reintroduce the multiple-registered-application crash — only a single shared application avoids both.
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
- **GI versions are pinned centrally.** `gi.require_version(...)` is called exactly once, in `giruntime/__init__.py`; no module carries its own `require_version` (or the `wrong-import-position` / `E402` suppressions that used to accompany it). A `from gi.repository import …` lands in the normal import group.
- **Forward declarations** rely on `from __future__ import annotations`, not string literals.

Every module begins with a `"""Principles & invariants` docstring. If a change you are making would break one of those bullets, that is the signal to discuss the design — not to silently drop the invariant.

---

## 7. Packaging & distribution

`folio` ships as a **zipapp** — a single `folio.pyz` run with `python folio.pyz`. There is no wheel, no console script, and no `[build-system]` in `pyproject.toml`; that file carries only project metadata and tool config. The zipapp is built from the `src/` tree directly (no staging copy) by `build_pyz.py`, which uses `zipapp.create_archive`'s API `filter` to drop `__pycache__`, `test_*.py`, and the grammar *sources* (`language_spec.lang`, `folio.gresource.xml`). Everything else — including `css/*.css`, the compiled `folio.gresource`, and the `system_docs/*` files (the welcome / help sources and the demo image) — rides along; the system documents are the runtime artifact, so they need **no** `build_pyz.py` change (only the loader's own `test_*.py` is filtered out). Because `src/__main__.py` lands at the archive root, zipapp uses it as the implicit entry point.

**Build dependency: `glib-compile-resources`** (ships with the GLib dev tooling, present on any GTK build host). It compiles the committed manifest `src/giruntime/ui/folio.gresource.xml` + `src/giruntime/ui/language_spec.lang` into the **generated, gitignored** bundle `src/giruntime/ui/folio.gresource`. One shared `Makefile` rule (`$(GRES)`, exposed as the `resource` alias) builds it; `./run` calls `make resource`, and `make test` / `make pyz` depend on `$(GRES)` directly — so dev, test, and prod all build the artifact the same way.

**Runtime floor: GtkSourceView ≥ 5.4.** The grammar is loaded via a `resource:///` search path, which `GtkSource.LanguageManager.set_search_path` only accepts from 5.4 onward. This is a system typelib, not a pip dependency, so it cannot be expressed in `pyproject.toml`; it is satisfied by the project's GTK 4.18 target environment (5.4 long predates it).

**One grammar load path (the §1 invariant).** Both a source checkout and the packaged `folio.pyz` load the grammar from the compiled `folio.gresource` via the `resource:///` URI — *never* from a filesystem path (inside the zip such a path would point into the archive and the OS could not open it). The resource is registered exactly once behind the cached `LanguageManager` in `giruntime/ui/note_editor.py`. A **missing** resource is a hard error (`FileNotFoundError`), not a silent fallback to plain-text highlighting — the fix is always "run `./run` / `make` so the resource is built". Because dev and prod share this single path, the unit suite (which depends on `$(GRES)`) already exercises the real loader; running `python folio.pyz` and confirming highlighting is a final check on the zip-packaged copy.

**Generated / gitignored artifacts:** `src/giruntime/ui/folio.gresource` and `folio.pyz`. `make clean` removes both.

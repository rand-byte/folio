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
| Run all tests | `make test` (preferred — builds the grammar resource and provides a headless display; runs the `src` suite **and** the `build-aux` build-tooling tests) or `python3 -B -m unittest discover -s src -t src -v` when a display is already available |
| Type-check | `make type` (runs `mypy` over the source and test files) — requires **`mypy >= 1.16`** (earlier releases mis-widen `StrEnum` members to `str`, [python/mypy#18587](https://github.com/python/mypy/pull/18587)); pinned in `pyproject.toml`'s dev group. `warn_unused_ignores` is on, so a `# type: ignore` that no longer suppresses anything is itself an error |
| Lint | `make lint` — requires **`pylint < 4`** (pinned in the dev group; pylint 4/astroid 4 mis-counts branches and re-introduces `gi` false positives). All pylint options live in `pyproject.toml` (`[tool.pylint.*]`), not on the command line: the `missing-*-docstring` disables, `enable = ["useless-suppression"]`, `min-public-methods = 1`, and the `generated-members` whitelist for `gi.repository` (so GTK call sites need no per-site `no-member` disables). `too-many-arguments` is enforced (a handful of dependency-injection constructors and renderer methods carry a justified local `disable`). The test-file pass additionally disables `too-many-public-methods,protected-access,duplicate-code,too-many-lines,too-few-public-methods` (the one set of flags still passed on the command line, in the `Makefile` `lint:` target) |
| Validate everything | `make validate` (= `type` + `lint` + `test`) |
| Check the version sites agree | `make version-check` (`build-aux/check_version.py`, see §7) |
| Build the `.deb` | `make deb` → `build/deb/folio_<version>_all.deb`; `make deb-lint` also runs `lintian`; `make deb-clean` removes the tree (§7) |
| Build everything | `make all` (= `validate` + `pyz` + `deb`) |

**System packages:** `gir1.2-gtk-4.0`, `gir1.2-gtksource-5` (GtkSourceView **≥ 5.4**, see §7) plus equivalents elsewhere, and `glib-compile-resources` (ships with the GLib dev tooling) to build the editor grammar bundle. Python **≥ 3.13**. The only Python runtime dependency is `PyGObject>=3.50`; SQLite is in the standard library.

**Full headless suite** (e.g. CI) additionally needs `weston`: the widget-level UI tests are gated behind a display guard and only run when a GDK display can be opened. `make test` supplies one by launching a headless Weston compositor (see §5). Without a display those tests **skip rather than fail** — so a plain `unittest` run with no display can report `OK` while exercising none of the GTK widgets. `make test` closes that hole by exporting `FOLIO_REQUIRE_DISPLAY=1`, which turns a missing display into a named failure (see §5); a hand-run leaves it unset and skips as before.

**CI** runs `make type`, `make lint` and `make test` on every push and pull request — `.github/workflows/validate.yml`, in a `debian:trixie` container (the platform `debian/control` targets, and the one whose system Python satisfies `requires-python >= 3.13` while still carrying `python3-gi`). The workflow only provisions packages and calls `make`, so the Makefile stays the single definition of what each target does. A second workflow, `.github/workflows/package.yml`, runs `make deb` on the same trigger and uploads the resulting `.deb` as a run artifact (§7).

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

The GTK rendering of a document's appearance — tag table, `TextBuffer` renderer,
and the view that paints them — lives under `giruntime/ui/note_render/` (not in
`asciidoc/`), so the whole `asciidoc/` core stays a pure format library.

| Layer | May import | May **not** import |
| --- | --- | --- |
| `enums` | nothing internal | anything else (it must stay leaf) |
| `models` | `enums` | `storage`, `controllers`, `ui`, `asciidoc`, `search` |
| `config` | `enums`, `models` | `storage`, `controllers`, `ui`, `asciidoc` |
| `system_docs` | `enums` (+ stdlib `importlib.resources`) | `storage`, `controllers`, `ui`, `gi`, `asciidoc` |
| `asciidoc` (`ast`, `lexer`, `inline_parser`, `parser`, `summary`) | `enums`, `models`, `config` | `storage`, `controllers`, `ui`, `gi`, `storage.protocols` |
| `storage.protocols` | `enums`, `models` | everything else (**no `gi` at all**, not even under `TYPE_CHECKING`) |
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
| Add a new AsciiDoc construct | `asciidoc/ast.py` (node **and** the relevant `InlineNode` / `BlockNode` union) → `lexer.py` → `parser.py` → `note_render/textbuffer_renderer.py` → `note_render/tag_table.py` (tag) → `giruntime/ui/language_spec.lang` (highlight, with fixtures in `test_language_spec.py`) → `asciidoc/summary.py` (snippet treatment). Every node-union walker is **statically exhaustive** — `summary.py` via `match`/`assert_never`, the renderer via `isinstance` cascades closed by `assert_never` — so a kind added to a union but not handled everywhere is a `mypy` error, and membership itself is forced by the parser's return types. There is deliberately no runtime "is it in the union" test; see `asciidoc/ast.py`'s module docstring for the invariant. | possibly `enums.py` (`ParseErrorKind` / a presentation enum) |
| Add a parse-error variant | `enums.py` `ParseErrorKind` → the parser site that raises it | `giruntime/ui/_parse_error_messages.message_for` (exhaustive) — the sentence shown beside unread source in the rendered view |
| Remove a parse-error variant | the parser site that stopped raising it → `enums.py` `ParseErrorKind` (a member exists only while a site raises it) → `_parse_error_messages.message_for` | in that order, so no kind ever outlives its raising site; fixtures that used the kind as their "unreadable" input need a construct that still fails |
| Change inline marker rules (constrained / unconstrained) | `enums.py` `MarkerForm` → `asciidoc/inline_parser.py` (`_OPEN_SPANS`, `_opens_at` / `_closes_at`) → `giruntime/ui/language_spec.lang` (the constrained contexts and their unconstrained twins, dispatched doubled-first) | `system_docs/help.adoc` (*Text & emphasis* documents the rule to users); a re-derive `Migration`, since cached `title`/`snippet`/`note_tags` are a function of the source **under these rules** |
| Change a parser limit (list or inline nesting depth) | `config/defaults.py` (`MAX_LIST_DEPTH` / `MAX_INLINE_DEPTH`) — the parser and `_message_for` both read the constant, so the enforced cap and the user-facing sentence cannot disagree | `test_parser.py` / `test_inline_parser.py` cap-edge tests; the depth-indexed renderer tables sized to `MAX_LIST_DEPTH` |
| Change DB schema | append a **new** `Migration` to `storage/migrations.py` `ALL_MIGRATIONS` — never edit a shipped one | the repository that reads/writes the new column |
| Add a note-level user action | `giruntime/controllers/note_controller.py` (call the store, wrap in `capturing_storage_errors`, mutate `AppState`) | its UI caller (`toolbar.py` / `note_editor.py` / `attachments_panel.py`); `note_list_store.py`; the repository protocol |
| Change persistence ordering | `giruntime/controllers/note_list_store.py` — the **DB-first** invariant lives here | `test_note_list_store.py` `DbFirstTests` |
| Change tag parsing / validation | `asciidoc/parser.py` `parse_tags_value` (shared by the parser and `summary.py`'s fallback) | `test_parser.py` / `test_summary.py`; a re-derive `Migration` if existing notes reparse |
| Change rendered-view styling | `giruntime/ui/note_render/tag_table.py` — every visual **structure** lives here exactly once (insets, wash shapes, scales, geometry). **Colour is not here**: it lives in `note_render/palette.py` | `test_tag_table.py`; rarely `textbuffer_renderer.py` for layout |
| Change a rendered-view **colour** (ink, link, tint, sheet, notice) | `giruntime/ui/note_render/palette.py` — edit the field in **both** `LIGHT_PALETTE` and `DARK_PALETTE`; the frozen dataclass makes a missing field a type error, the completeness + contrast tests catch the rest | `test_palette.py`; `test_tag_table.py` if a *new* colour needs a tag to carry it |
| Change block tint insets / wash shape | `giruntime/ui/note_render/tag_table.py` (`build_wash_specs` — the inset/shape half; the tint comes from the palette) | `test_tag_table.py`, `note_render/test_article_text_view.py` wash-rect tests |
| Change the **editor's** dark/light colours | `giruntime/ui/note_editor.py` — `_STYLE_SCHEME_IDS` pairs `ColorScheme` with a GtkSourceView style scheme (`classic` / `classic-dark`). GtkSourceView does **no** dark switching of its own: a fresh buffer is given `classic` and keeps it, which is why the editor once kept light syntax colours and a bright current-line band under a dark theme | `test_note_editor.py` (`StyleSchemeLookupTests`, `StyleSchemeFollowsThemeTests`) |
| Change how dark mode is **detected** | Two links in one chain. The desktop's *preference* comes from the XDG portal in `giruntime/ui/system_color_scheme.py` and is pushed into `Gtk.Settings:gtk-application-prefer-dark-theme` (process-local) so the **chrome** restyles; the article view then measures the theme's resolved foreground in `note_render/article_text_view.py` (`do_css_changed` → `_scheme_from_style`) via `palette.scheme_for_foreground` — reading its **parent**, not itself, because its own colour is the palette's ink and a self-reading probe would latch on its own output. Do **not** replace the second with a `Gtk.Settings` read: under `GTK_THEME=Adwaita:dark` the theme-name reads `"Default"` and the prefer-dark property reads `False`, so only the measurement is correct for *every* route to a dark chrome | `test_system_color_scheme.py` (preference policy), `test_palette.py` (luminance rule), `note_render/test_article_text_view.py` (`ColorSchemeReThemeTests`, `ThemeChangeTests`). The same `do_css_changed` + luminance rule drives the editor's style scheme, so the two panes cannot disagree about which scheme is in effect |
| Change body-heading vertical spacing | `giruntime/ui/note_render/tag_table.py` (`_make_heading_tag`) | `test_tag_table.py`; `test_textbuffer_renderer.py` |
| Tune article column margins | `config/defaults.py` (the `ARTICLE_*` multipliers) | none — `note_view.py` / `article_container.py` read them once at construction |
| Change rendered-view layout / scrolling | `giruntime/ui/article_container.py` `ArticleContainer` (a `Gtk.Widget` + `Gtk.Scrollable`) | `test_article_container.py` |
| Change the under-title metadata line | `giruntime/ui/note_view.py` (`_insert_metadata_after_title`) + `note_render/tag_table.py` (`TagName.METADATA`); dates in `giruntime/ui/_dates.py` | `test_note_view.py`, `test_textbuffer_renderer.py` |
| Change the header-bar title / collapsible search | `giruntime/ui/toolbar.py` (centre stack, search toggle, title tracking); page names in `enums.py` `HeaderCentrePage` | `test_toolbar.py` `HeaderSearchTests` / `CentreTitleTests` |
| Change application chrome / CSS | `giruntime/ui/css/app.css` | none — the zipapp archives `src/` directly, so new assets ship automatically |
| Change the application icon | `giruntime/ui/icons/scalable/apps/io.github.rand_byte.Folio.svg` (the file name **is** the icon name, and the icon name **is** the app id) | `folio.gresource.xml` + `Makefile` only if adding/renaming a size variant; `meson.build` installs this same file to `hicolor` for the dock/menu (§7) |
| Change the initial window size | `giruntime/ui/main_window.py` (used only when no size was restored) | `test_main_window.py`, `test_article_container.py` column-width tests |
| Change restored session state | `models/session_state.py` → `storage/session_state_store.py` (bump `_SCHEMA_VERSION`) → `giruntime/ui/application.py` → `main_window.py`. No window-position restore (GTK 4 has no API). | `storage/test_session_state_store.py`; `test_application.py`; `test_main_window.py` |
| Change source-editor syntax highlight | `giruntime/ui/language_spec.lang` | `test_language_spec.py` — every context has pinned positive/negative fixtures, and a new context must be added to the dispatch list or the wiring tests fail; rebuild the resource (`./run` / `make resource` do this); the `.xml` manifest only if adding/renaming grammar files |
| Tune a constant (sizes, quotas) | `config/defaults.py` | none — that is the point of this module |
| Change paths / XDG behaviour | `config/paths.py` | `config/test_paths.py` |
| Add a sort key / smart filter | `enums.py` (`NoteSortKey` / `SmartFilter`) → `search/note_filter.py` → `giruntime/ui/note_list.py` and/or `sidebar.py` | `search/test_note_filter.py`; `note_list.py` `_selection_empty_reason` if the new filter can match nothing |
| Change what search matches | `search/note_filter.py` `matches_query` — the whole rule, one function. Matching `tags` here is a **rejected** design, not a gap (tag filtering is the sidebar's explicit AND-semantics affordance); caching the case-fold per item is a **measured and rejected** optimisation. Both are recorded in the module docstring — read it before "improving" either. | `search/test_note_filter.py` |
| Tune the search debounce | `giruntime/ui/note_list.py` `SEARCH_DEBOUNCE_MS` — module-local because it has one consumer, like `note_editor.py`'s `AUTOSAVE_DEBOUNCE_MS`; the injected timer *seam* is shared (`ui/_timeouts.py`) | `test_note_list.py` `SearchDebounceTests` |
| Change the note-list empty state | `enums.py` `NoteListEmptyReason` (one member per **reachable** state — its docstring records which states cannot occur and why) → `giruntime/ui/note_list.py` `_EMPTY_STATE_LABELS` (a tuple of **authored lines** per reason) / `_message_text` (joins them into the one label) / `_empty_reason`. The label's wrap is **load-bearing**, not cosmetic — see the module docstring: a non-wrapping label's minimum width becomes the pane's, because the `Gtk.Paned` is built `shrink_start_child=False`. Layout constants (`_EMPTY_STATE_PADDING_PX`) live next to the wrap settings they move with; only the dim (`.note-list-empty`) is in `css/app.css` | `test_note_list.py` `EmptyStateMessageTests` (pure, un-gated), `EmptyStateLabelWidthTests`, `EmptyStateLayoutTests`, `NoteListEmptyStateTests`, `SelectionEmptyReasonTests` |
| Change note-list row title/snippet | derivation in `asciidoc/summary.py`; presentation in `giruntime/ui/note_list.py` + `css/app.css` | `storage/note_repository.py` if the cached-column contract changes |
| Change the sidebar Tags section | `giruntime/controllers/tag_counts_model.py` + `giruntime/ui/sidebar.py` | `test_tag_counts_model.py`; `test_sidebar.py` |
| Change selection / view-mode plumbing | `giruntime/controllers/app_state.py` (a GObject property + rule-bearing mutator). `MainWindow._on_view_mode_changed` is the single view-mode orchestrator. | every UI widget that subscribes via `notify::<prop>` |
| Add a new dialog | `giruntime/ui/dialogs.py` | its opener |
| Change link/URL handling | `giruntime/ui/link_handler.py`; allowlist in `enums.LinkScheme` | `asciidoc/inline_parser.py` for scheme validation |
| Change where a URL or address **ends** | `asciidoc/inline_parser.py` — `_URL_STOP_CHARACTERS` / `_URL_STOP_SEQUENCES` / `_URL_TRAILING_PUNCTUATION` and the `_url_extent` + `_strip_trailing_punctuation` pair for URLs; `_EMAIL_RE` and `_try_consume_email` for bare addresses. The three rules that are easy to get wrong: an enclosing span ends a URL at the position where its marker *validly closes* (not its first occurrence), a doubled marker ends one only when it **pairs** later on the line, and the trailing-punctuation peel applies to the bare form only — the labelled form's bracket already marks the end | `giruntime/ui/language_spec.lang` (`bare-url`, `url-with-text`, `email`) + `test_language_spec.py` fixtures; `system_docs/help.adoc` (*Links* documents both rules to users). **No** re-derive `Migration`: these changes move the boundary between a link's display text and the text around it, never the characters, so `summary._flatten` is invariant — pinned by `test_summary.py` `UrlAndAddressSnippetTests` |
| Change click handling for *anything* clickable | `giruntime/ui/link_handler.py` — it dispatches the renderer's closed `ActivationTarget` union (`UrlTarget` → launcher, `AttachmentTarget` → the injected `AttachmentActivator`) with `assert_never`, so a third activatable thing is a type error until every consumer handles it | `note_render/textbuffer_renderer.py` (`target_for_tags`); `note_view.py` / `help_window.py` (the activators) |
| Change attachment rules | `storage/attachment_store.py`; size cap in `config/defaults.MAX_ATTACHMENT_BYTES`; export (`export_to`) is the outbound mirror of `add_for_note` | `giruntime/controllers/note_controller.py` for toast wiring (`attachment-rejected` / `attachment-export-failed`) |
| Change the `attachment:` save link | `asciidoc/inline_parser.py` (the macro) → `note_render/textbuffer_renderer.py` (`_emit_activatable`) → `link_handler.py` (dispatch) → `note_view.py` `_activate_attachment` (resolve → save dialog → `NoteController.export_attachment`) | `_file_picker.py` (`FileSaveDialogOpener`); `help_window.py` (its demo activator) |
| Change the `attachments::[]` table | `asciidoc/parser.py` `_parse_attachment_table` (the `cols` attrlist) + `enums.AttachmentTableColumn` → `note_render/attachment_table.py` (the pure AST → AST expansion: header labels, cell builders, the empty-list paragraph) | nothing in the renderer: the expansion produces an ordinary `Table`, so `_emit_table` is reused by construction |
| Change the attachments panel | `giruntime/ui/attachments_panel.py`; size formatting in `_filesize.py`; picker in `_file_picker.py` | `note_controller.py` (`attachments-changed`); `note_list.py` (📎 badge) |
| Edit the help reference text | `system_docs/help.adoc` (must stay inside the supported subset; §7 coverage test requires every node kind to appear) | `enums.py` (`HelpSection`) if buckets change; `test_help_window.py` |
| Add a bundled system document | `enums.py` (`SystemDocument` member) → drop the file under `system_docs/` → read via `system_docs.load_text` / `load_bytes` | its consumer (`migrations.py` seed / `help_window.py`); `system_docs/test___init__.py` |
| Change the help window | `giruntime/ui/help_window.py` (builds its pane from the shared `note_view.build_article_surface()`) | `note_view.py`; `application.py` (`app.help` action + `F1`); `toolbar.py` (Help button) |
| Add a keyboard shortcut / accelerator | For a window action: `enums.py` (`WindowAction` + `window_action_detailed_name`) → register it and its accel in `giruntime/ui/main_window.py` (`_install_window_actions` / `_WINDOW_ACTION_ACCELERATORS`), delegating to the behaviour's single home (`toolbar.py` public method, or `app_state.py`). App-scoped keys (help, quit) live in `giruntime/ui/application.py`. A **focus-local** key (e.g. `Delete`) is a `Gtk.ShortcutController` at `LOCAL` scope on the owning pane (`note_list.py`), never an application accelerator — a window-global one would fire inside the editor. | `test_main_window.py` `MainWindowKeyboardActionTests`; `test_toolbar.py` `KeyboardActionMethodTests`; `test_note_list.py` `NoteListDeleteShortcutTests`; `test_application.py` `QuitActionTests` |

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

- **`defaults.py`** — tunable constants (attachment/list/article/table limits and multipliers, snippet limits, the SQLite `journal_mode`/`busy_timeout` connection tunables) and stable identifiers (`SEED_WELCOME_NOTE_ID`).
- **`paths.py`** — `data_directory()` / `database_path()` / `session_state_path()`, XDG-aware. Pure except for `mkdir`.

### `system_docs/` — bundled system documents (gi-free, config-tier)

Content the app ships rather than the user authoring: the seed welcome note and
the AsciiDoc help reference (plus its demo image). Plain package data read
gi-free via `importlib.resources` — **not** gresource content. Read by both
`storage` (seed) and `giruntime` (help).

- **`__init__.py`** — the shared loader keyed by the `SystemDocument` enum: `load_text(...) -> str`, `load_bytes(...) -> bytes`.
- **`welcome.adoc`** — seed welcome note source (v1 seeds it; a golden test pins its exact bytes).
- **`help.adoc`** — the help reference, authored in the supported subset (tested to parse clean and to exercise every node kind).
- **`help-demo.png`** — demo image served to the help's `image::` example, and (via `HelpWindow`'s static demo attachment list) to its `attachment:` / `attachments::[]` examples.

### `models/` — frozen dataclasses

- **`note.py`** — `Note` + the frozen `NoteSummary` `(title, snippet, tags)`. Tag/summary derivation lives in `asciidoc/summary.py`, not here.
- **`attachment.py`** — `Attachment` metadata; no `data` field (bytes live in the BLOB column) and no type field (attachments are opaque blobs).
- **`parse_error.py`** — `ParseError`, the only exception raised by the lexer / parser / inline parser; carries `kind` + `line` + `column`.
- **`session_state.py`** — `SessionState` + `DEFAULT_SESSION_STATE`. Pure value type, no I/O.

### `asciidoc/` — text ⇒ AST ⇒ summary

A **pure** format library: GTK-free and storage-free, importing only `enums` /
`models` / `config`.

- **`lexer.py`** — `tokenize(source) -> tuple[Token, ...]`. Line-based, context-free, **permissive** (never raises on grammar issues).
- **`inline_parser.py`** — `parse_inline(line, line_no) -> tuple[InlineNode, ...]`. A bare URL ends at whitespace, a bracket, a doubled marker that pairs later on the line, or the enclosing span's valid closer — nothing else, so `https://x/a_b_c` links whole — and trailing sentence punctuation is peeled off the target rather than swallowed. A bare email address autolinks to a `mailto:` target under the language's own shape (final domain label of two to five letters); the `mailto:` prefix itself activates only in its macro form. A match never retreats to a shorter address, so folio declines where the reference would link a truncated one. **Total over formatting markers**: a `*`, `_`, `#` or backtick that does not resolve to a span is returned as `Text`, never an error — an opener with no valid closer backtracks. Errors are reserved for constructs the source *reached for*: the `link:` / `attachment:` macros, an unterminated passthrough, and the nesting cap. Which positions a marker may open and close at is decided by `enums.MarkerForm` (constrained vs unconstrained/doubled). Nesting is bounded by `MAX_INLINE_DEPTH` (one Python frame per level), so a pathological line raises `ParseError` rather than `RecursionError`; backtracking stays linear via the per-line closer index, which is an invariant of the design and not an optimisation to be removed.
- **`parser.py`** — `parse(source) -> Document`, recursive-descent, strict, exhaustive over tokens; each failure maps to a specific `ParseErrorKind`. Alongside it `parse_recovering(source) -> Document` is **total**: where strict parsing raises, it quarantines the offending source into an `ast.UnreadBlock` at the position it occupied and continues, so a note with a syntax error still renders in full. The two are selected by `enums.ParseMode` and are held to three tested invariants — *agreement* (identical trees whenever strict succeeds), *totality* (never raises, always terminates) and *losslessness* (no source line is dropped). The renderer calls the recovering form; `summary.py` still parses strictly (switching it would rewrite cached columns and needs a re-derive `Migration`).
- **`ast.py`** — frozen dataclasses for every AST node. `BlockNode` / `InlineNode` are closed unions; children are `tuple[...]`. `UnreadBlock` is the one member no well-formed source can produce — only `parse_recovering` builds one — so `test_help_window.py`'s "every node kind appears in help.adoc" coverage assertions subtract it explicitly.
- **`summary.py`** — `derive_summary(source) -> NoteSummary`. The single source of truth for note-list title/snippet/tags. **Never raises** — falls back to permissive extraction so a mid-edit note stays saveable.

### `storage/` — SQLite persistence

`protocols.py` is the typing surface every higher layer imports; concrete
classes are siblings.

- **`protocols.py`** — the repository and attachment-store protocols, plus the `AttachmentRejected` / `AttachmentExportFailed` exceptions. Pure typing — no `sqlite3`, and no `gi` **at all** (not even under `TYPE_CHECKING`). A protocol lives here only while a call site is annotated with it: `SessionStateProtocol` and `RendererProtocol` were deleted because nothing ever was — `application.py` names the concrete `SessionStateStore`, `note_view.py` names the concrete renderer, and the one renderer surface that *is* typed structurally (`target_for_tags`) is declared next to its consumer in `ui/link_handler.py`. The renderer's construction-time aliases (`ImageBytesResolver`, `AttachmentListResolver`, `ColumnWidthMeasurer`) live in `ui/note_render/textbuffer_renderer.py` for the same reason — a `Callable` alias belongs with the module that consumes it. Read the module docstring before adding either kind back.
- **`database.py`** — owns the single `sqlite3.Connection` (`autocommit=True`, composable `transaction()` via savepoints) and applies its connection settings from one declarative table (`_CONNECTION_PRAGMAS`): `foreign_keys=ON` (required — a silent failure breaks `ON DELETE CASCADE`), `journal_mode=WAL` (best-effort — `:memory:` and shared-memory-less filesystems keep their mode), and `busy_timeout`. `close()` is an owner responsibility (the app calls it on shutdown so WAL sidecars are checkpointed away); it is idempotent and backs the context-manager protocol.
- **`migrations.py`** — all schema statements in an append-only `ALL_MIGRATIONS`; `apply_pending()` is idempotent. See the live schema below.
- **`note_repository.py`** — SQLite-backed repository and **single owner of the `source → cached state` mapping**: `insert` / `update_source` derive title/snippet/tags, write the cached columns and `note_tags`, and return the persisted derived `Note`.
- **`attachment_store.py`** — BLOB-backed store. Attachments are opaque blobs; the only add-time gates are the `MAX_ATTACHMENT_BYTES` cap (a `stat` check before any bytes are read, plus a bounded `cap + 1` read re-checked against the cap, so a file that grows after the stat can't smuggle in an over-limit blob) and source readability.
- **`session_state_store.py`** — JSON-file-backed store at `paths.session_state_path()`. `load()` never raises (any error resolves to `DEFAULT_SESSION_STATE`); `save()` writes atomically.

**Live schema** (defined in `migrations.py`):

- `notes(id PK, title, source, snippet, created_at, modified_at)` + index on `modified_at DESC`.
- `note_tags(note_id FK→notes ON DELETE CASCADE, tag, PRIMARY KEY (note_id, tag))` + index on `tag`. Populated by the repository on every `insert` / `update_source`.
- `attachments(id PK, note_id FK→notes ON DELETE CASCADE, filename, byte_size, data BLOB)` + index on `note_id`.
- `schema_version(version PK)` records applied migrations. Latest is **v5** (re-derives `title` / `snippet` / `note_tags` for every note after the inline marker rules changed; timestamps deliberately untouched).

Migrations are append-only, so v1's original CREATE statements still ship for
upgrade paths even though later migrations reshape the schema; a freshly
reset/deleted database re-runs v1 from scratch (the welcome note always comes
back on a true reset, which `_select_initial_note`'s fallback relies on).

### `search/` — pure filters

- **`note_filter.py`** — `filter_by_selection` / `filter_by_query` / `sort_notes`, and the `Selection` union (`SmartSelection` / `TagSelection`). Multi-tag selection is **AND**. No clock dependency. `matches_query` is the **sole** definition of what a query matches (the repository's SQL `LIKE` search is gone). `tags` is deliberately not matched, and the case-fold is deliberately not cached — both decisions, with their measurements, are in the module docstring.

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

- **`application.py`** — `NotesApplication(Gtk.Application)`: composes the storage/controller stack, presents `MainWindow`, loads/saves `SessionState`, selects the initial note, registers the app-scoped `help` (`F1`) and `quit` (`Ctrl+Q`) actions and the bundled application icon. App lifetime is bound to the main window; its `close-request` handler flushes the editor's pending autosave (`MainWindow.flush_editor`) before quitting, so keystrokes still inside the debounce window are not lost on close — and `quit` routes through that same close path (it closes the stored main window) rather than calling `Gtk.Application.quit` directly, so `Ctrl+Q` keeps the flush + save guarantees. It owns the `Database` connection end to end: opened lazily on first activation, closed in `do_shutdown` (after the loop stops and all widgets are gone, so nothing can still touch it — and so WAL sidecars are checkpointed away).
- **`help_window.py`** — `HelpWindow`, the standalone non-modal help reference. Builds its reading pane from the shared `note_view.build_article_surface()` so help renders identically to a note. Hide-on-close (one cached instance).
- **`system_color_scheme.py`** — follows the desktop's dark/light preference, which plain GTK 4 does **not**: GNOME's Dark Style switch sets `color-scheme` to `prefer-dark` and leaves `gtk-theme` at `Adwaita`, and acting on that key is libadwaita's job. Reads `org.freedesktop.appearance`/`color-scheme` from the XDG portal (read-only; the app never writes the user's settings) and pushes the answer into `Gtk.Settings:gtk-application-prefer-dark-theme`, which is **process-local** — it styles this app, not the desktop. That restyles the chrome, which changes the foreground `ArticleTextView` resolves, which re-themes the note. Writes **both** directions: GTK treats an app's write as an override, so only ever writing `True` would leave the app stuck dark. No portal (or no answer from it) means no write at all, and the app falls back to following the GTK theme alone.
- **`main_window.py`** — the three-pane shell (sidebar │ note list │ `Gtk.Stack(view ↔ editor)`). Takes an optional `restored_state`. Owns the `AppState:notify::view-mode` subscription, and registers the window-scoped keyboard actions (`win.new-note` / `win.focus-search` / `win.toggle-mode` / `win.delete-note`, from `enums.WindowAction`) with their `Ctrl+N` / `Ctrl+F` / `Ctrl+E` accelerators — `win.delete-note` gets **no** accelerator here (the note list binds `Delete` focus-locally). Each action delegates to the behaviour's single home (`Toolbar` methods, or `AppState.set_view_mode`), so a key and its toolbar button never diverge; `win.delete-note`'s enabled state tracks the selection.
- **`sidebar.py`** — flat library navigation: a **Library** section (`All notes` / `Untagged`) and a model-driven **Tags** section (multi-select, AND semantics). Selection rules owned by `AppState`; counts update live off the store.
- **`note_list.py`** — middle pane: a `ListView` over `SingleSelection(SortListModel(FilterListModel(NoteListStore)))`, reusing the `search.note_filter` predicates. Selection is one source of truth (`AppState`). Search is **debounced** (`SEARCH_DEBOUNCE_MS`, timers injected from `_timeouts.py`; clearing the box bypasses it), and query invalidation passes GTK a monotone `Gtk.FilterChange` hint (`MORE_STRICT` / `LESS_STRICT`) so a keystroke re-tests a subset rather than the whole model. When empty, the pane says *why* (`enums.NoteListEmptyReason`), which is why it retains the unfiltered store. That message is authored as **lines** (`_EMPTY_STATE_LABELS`, joined by `_message_text`) rendered in one **wrapping** label that replaces the scroller — the scroller is hidden rather than emptied because it carries `vexpand`, and the wrap is what stops a long message from widening the whole pane through the `Gtk.Paned`'s `shrink_start_child=False`. Carries the focus-local `Delete` shortcut (a `Gtk.ShortcutController` at `LOCAL` scope activating `win.delete-note`), so `Delete` deletes the selected note only while this pane has focus, never inside the editor.
- **`article_container.py`** — `ArticleContainer`, a `Gtk.Widget` + `Gtk.Scrollable` that *establishes and enforces* the fixed-width text column and owns scrolling — the **geometry** half of the reading surface (the appearance half is `note_render/article_text_view.py`). Vertical scrolling is pass-through to the text view; horizontal is container-owned (centre when wider, pan when narrower). The horizontal extent it publishes is never smaller than the page (`upper = max(column, viewport)`): a sub-page `upper` is read by `Gtk.ScrolledWindow` as a permanent overshoot, which paints the theme's `overshoot.right` glow across the right of the desk. Column width and the four article margins derive from injected Pango measurers (`CharWidthMeasurer` / `LineHeightMeasurer`, defined here), each measured once and cached. Names no `Document`, renderer, or tag-table symbol: the renderer reads the width it exposes only through an injected `ColumnWidthMeasurer` (defined in `note_render/textbuffer_renderer.py`). The `Gtk.Scrollable` base + zero vertical `do_measure` are what stop `Gtk.ScrolledWindow` interposing a `Gtk.Viewport` that would cache a stale extent and hide the vertical scrollbar on first launch; `test_article_container.py`'s `ArticleContainerScrollbarRegressionTests` guards that end to end (real toplevel, real main loop — the extent is only committed after a frame-clock tick) from the container plus a bare scrollable child, with **no** `NoteView` involved, so nothing but the container can be responsible for the outcome.
- **`note_view.py`** — read pane (`NoteView`) + the shared reading-surface factory. Assembles the surface from `article_container.ArticleContainer` (geometry) and `note_render.article_text_view.ArticleTextView` (appearance), exposing `build_article_surface()` and `make_cell_width_measurer()`. Renders from the in-memory store. It never handles a parse failure: the renderer parses with `parse_recovering`, so a note that will not parse still renders with the unreadable source marked in place — `unread_block_count` reports how many marks are on screen (structural only; an inline failure renders as ordinary prose and carries none). The store-driven (`items-changed`) re-render is **deferred while the pane is hidden** (the EDIT-mode stack child): it marks a render owed instead of rebuilding an off-screen buffer on every debounced autosave, and renders once on the next map / view-mode reveal. Visibility is an injected `PaneVisibilityPredicate` (defaults to `get_mapped`; tests pass a fake); selection-change renders are not deferred.
- **`note_editor.py`** — source pane (`GtkSource.View`) with the `AttachmentsPanel` embedded below it. Debounced autosave (`AUTOSAVE_DEBOUNCE_MS`, timers injected from `_timeouts.py`) routes through `NoteController.update_source`. Loads grammar via `_gresource.resource_path(...)`.
- **`attachments_panel.py`** — per-note attachment management (header, add-file, one card per attachment). The header's add control is a **single `+` icon button** (`list-add-symbolic`): GTK's stock `circular` class gives the round shape and a small `attachment-icon-button` rule in `css/app.css` adds a soft, theme-derived grey fill (the stock fill is nearly invisible on the white panel), so it reads like the platform's `+`/`-` steppers. It sits **directly beside** the dimmed `ATTACHMENTS · N` label — a left-aligned row with a gap (`_HEADER_GAP_PX`), not pushed to the far edge — so the label and button read as one unit; a tooltip carries the *Attach a file* affordance. The header row carries vertical padding (`_HEADER_VPAD_PX`) so the round `+` isn't cramped. Cards are two-line (filename over size), framed by the `attachment-card` class in `css/app.css`, in a **capped card grid**: a `Gtk.FlowBox` inside a `Gtk.ScrolledWindow` whose height stops at 2.5 measured card rows (`scroll_cap_height`), so attachments cost grid *rows* and can never starve the editor above. Each card's remove button (a `user-trash-symbolic` bin) wears that **same** `circular` + `attachment-icon-button` style, so add and remove read as one matched family. Add/remove route through `NoteController`; inserts nothing into the note body.
- **`toolbar.py`** — top `Gtk.HeaderBar`: *New*, *Delete*, a search toggle expanding a collapsible centre search (title ↔ full-width entry stack, pages named by `enums.HeaderCentrePage`; the entry is bound to `AppState:query`), the selected note's title in the centre, a View/Source toggle, and a *Help* button targeting `app.help`. *New* / search-focus / *Delete* also expose public methods (`create_note` / `focus_search` / `delete_selected`) that the window's `win.*` accelerators call — the same code the button handlers call, so keys and buttons cannot drift.
- **`dialogs.py`** — shared modal dialogs (confirm-delete only). Production wires `Gtk.AlertDialog`; tests drive callbacks synchronously.
- **`link_handler.py`** — `LinkHandler.install(...)` wiring motion/click controllers. Resolves a click to the renderer's closed `ActivationTarget` union and dispatches it: a `UrlTarget` to an injected launcher (allowlisted by `enums.LinkScheme`), an `AttachmentTarget` to an injected `AttachmentActivator`. Installed by both `note_view.py` and `help_window.py`.
- **`_file_picker.py`** — the two file-dialog openers: `FileDialogOpener` wrapping `Gtk.FileDialog.open` (attach a file; offers all files, the size cap in `AttachmentStore` is the gate) and `FileSaveDialogOpener` wrapping `set_initial_name` + `save` (save an attachment back out). Both collapse cancel / backend error / non-local URI to `None`.
- **`_parse_error_messages.py`** — `message_for(kind, line)`: the exhaustive `ParseErrorKind` → user-facing sentence table. Read by the *renderer*, which places the sentence beside the source it explains. Not underscore-prefixed despite the module being private, matching `_dates` / `_filesize` / `_timeouts`. Pure — no GTK, no storage.
- **`_filesize.py`** — shared human-readable byte-size formatting (binary convention). Pure.
- **`_dates.py`** — shared locale-independent date formatting (`format_date_short` / `format_date_long`). Pure.
- **`_timeouts.py`** — the injected one-shot timeout seam (`TimeoutScheduler` / `TimeoutCanceller`, their GLib-backed defaults, and `TIMEOUT_REMOVE`) shared by the two debouncing panes: the editor's autosave and the note list's search. Owns the *seam*, not the delays — each pane keeps its own interval next to the behaviour it belongs to. The only place in the UI layer that names GLib's timeout primitives.
- **`_gresource.py`** — `resource_path(GResourceSubtree) -> str`, the only way to obtain a path into the compiled `folio.gresource`; registers the bundle idempotently as a side effect. A missing bundle raises `FileNotFoundError`.
- **`css/app.css`** — application stylesheet, read via `importlib.resources`; ships in `folio.pyz`. Holds the sidebar, note-list row, note-list empty-state, and attachment-card rules; colours are theme-derived (`opacity` / `alpha(currentColor, …)`), never named.
- **`language_spec.lang`** — GtkSourceView 5 grammar; compiled into `folio.gresource` and loaded via a `resource:///` search path (build input only, not shipped raw). Being data rather than a module, its `test_*.py` sibling is named for the file: `test_language_spec.py` (§5).
- **`folio.gresource.xml`** — committed GResource manifest publishing the grammar and the application icon; compiled by `glib-compile-resources`.

#### `giruntime/ui/note_render/` — a document's *appearance* (GTK)

Given a parsed `Document` and its resolvers, this sub-package delivers the document's complete
**visual presentation** — the styled buffer content **and** the on-buffer painting that finishes
it — up to but **not** including how that content is sized, scrolled, or placed. Geometry is the
pane's (`ui/article_container.py`, assembled by `note_view.build_article_surface()`). These are the
only consumers that need `gi` + the storage protocols, so they live here and keep `asciidoc` pure.

- **`palette.py`** — every rendered-view **colour**, as `LIGHT_PALETTE` and `DARK_PALETTE` (`Palette`, a frozen dataclass: sheet, foregrounds, block tints). Also `scheme_for_foreground`, the pure luminance rule that classifies a theme's resolved text colour into a `ColorScheme`. Imports no `gi` — plain data plus one rule, so every colour invariant is testable with no display. The dark sheet is deliberately a step *lighter* than the theme's window background so a note still reads as a page on a desk; that relationship is verified by screenshot, since GTK exposes no supported way to probe a widget's resolved background.
- **`tag_table.py`** — builds the shared `Gtk.TextTagTable`. **Every visual *structure* lives here, exactly once** (inline, heading, block-level, table, metadata, list geometry, error-notice). It sets **no colour itself**: it builds the tags and delegates to `apply_palette`, the single writer of every foreground, which `ArticleTextView` calls again on a theme change — so the build and re-theme paths are one path and cannot drift. The note's *default ink* is deliberately **not** a tag here — it is CSS, set by `article_text_view._apply_article_ink` (see that module for why a whole-buffer tag mis-painted the sheet). Block tags carry text position only; tints are painted by `ArticleTextView` from the `WashSpec`s this module exposes.
- **`article_text_view.py`** — also the home of the note's **default ink**: `_apply_article_ink` writes the palette's `body_foreground` as CSS `color` on the article view's own style class (widget node *and* `text` node — the glyphs take the widget's colour). Body text and headings set no foreground of their own, so without it they inherit the theme's, which is invisible on the application-painted sheet whenever the two disagree. It is CSS rather than a lowest-priority tag applied across the buffer because that tag re-invalidated the text layout after the content was inserted, and the next paint sized the sheet and the block washes from estimated line heights — a note switched to from a shorter one was painted a block short until an unrelated redraw fixed it.
- **`article_text_view.py`** — `ArticleTextView`, the read-only `Gtk.TextView` that *paints* the appearance the tag table defines: the block-tint **washes** (extending one M-width beyond the text on each side — the padded-card effect `paragraph-background-rgba` cannot produce) and the note **sheet** (its own CSS background is transparent because the view is the vertical scrollport). Consumes `tag_table`'s `WashSpec` / `SheetWash`; owns no geometry. It also **decides the colour scheme**, because it is the widget that can measure it: `do_css_changed` → the luminance of its own `get_color()` → `apply_palette` + re-installed washes + a new sheet + `queue_draw`. Re-theming touches no text, so there is no re-parse, no re-render, and no lost scroll position; both surfaces (note view and help window) get it without knowing about it. A `ColorSchemeProbe` seam (`install_scheme_probe`) lets tests force a scheme.
- **`textbuffer_renderer.py`** — `TextBufferRenderer.render_into(document, buffer, ...)`. Rebuilds the buffer each call; **no construct escapes to a widget** (tables, images, admonitions, blockquotes, code blocks are all native buffer content). Image bytes flow through an injected `ImageBytesResolver`, the current note's attachment metadata through an injected `AttachmentListResolver`, and the article column's pixel width through an injected `ColumnWidthMeasurer` — all three aliases, plus `CellWidthMeasurer` and `PostTitleHook`, are defined in this module; an optional `post_title_hook` lets `NoteView` insert the metadata line. Clickable ranges carry an anonymous per-link tag mapped to an `ActivationTarget` (`UrlTarget | AttachmentTarget`), recovered by `target_for_tags` — one styling tag (`TagName.LINK`), one lookup, one dispatch.
- **`attachment_table.py`** — `expand_attachment_tables(document, attachments)`: the pure AST → AST transform that replaces every `AttachmentTable` node (`attachments::[]`) with an ordinary `Table` — header labels, one row per attachment, the name cell an `AttachmentLink` — or, for a note with no attachments, an italic *"No attachments."* paragraph. Called by `render_into` before the emit walk, so the generated table reuses `_emit_table`'s column geometry by construction. No GTK, no storage: unit-testable with no display.

---

## 5. Testing

- Tests use the standard-library `unittest`; there is no extra runner. A module `M.py` is tested in the sibling `test_M.py` (no global `tests/` directory).
- **`make test` runs two discovery passes.** The first is over `build-aux/` — the build tooling (`check_version.py`) is GTK-free and display-free, and `discover -s src` cannot see it; the second is the application suite over `src/`, under the compositor described below. Both must be green.
- **Storage** tests run against a real `Database.in_memory()` with the schema applied. **Controllers** are tested against in-memory **fakes** of the storage protocols plus a fake clock and counter id-gen. **UI** tests instantiate widgets directly and drive them with fakes; asynchronous GTK dialogs are wrapped behind callable type aliases so tests pass a synchronous fake.
- **UI tests need a real GDK display.** Each is decorated `@unittest.skipUnless(display_available(), ...)`, importing that one probe from `giruntime/ui/test_display_guard.py` — there is no per-module copy, so what counts as "a display" cannot drift. With no display they *skip*, so a green run without one proves nothing about the widgets.
- **`FOLIO_REQUIRE_DISPLAY=1` is the tripwire for that.** `make test` sets it on the `src` pass because it launched the compositor itself and therefore knows a display *should* exist; `DisplayRequirementTests` then fails — naming the cause — instead of letting ~90 widget test classes skip in silence. Checking the environment is not enough on its own: the recipe exports `WAYLAND_DISPLAY` before it knows whether Weston came up, so the guard actually opens a display. Only `make test` sets the variable; running the suite by hand is unaffected. The module is named `test_*` so `build_pyz` keeps it out of `folio.pyz` and the `.deb`, which is also why its env-var constants live there rather than in `enums.py` — they are test scaffolding, not application vocabulary.
- **`make test` wires the display**: it launches `weston --backend headless` in the **background** (chaining with `&&` would block forever), waits for its socket, then runs the suite with `WAYLAND_DISPLAY`, `GSK_RENDERER=cairo` and `FOLIO_REQUIRE_DISPLAY=1` exported, and kills Weston on exit. Running the suite directly against your own display must export the first two by hand (the third is `make test`'s alone — see above).
- **`GSK_RENDERER=cairo` is mandatory, not cosmetic.** The cairo software renderer never touches GL/Vulkan/EGL, so it cannot segfault inside a missing/broken GPU driver when a UI test presents a real toplevel. The deeper rationale (and the single-shared-application requirement below) lives in the docstrings of the relevant UI test modules.
- **The UI suite shares one registered application.** GTK allows exactly one *registered* `GtkApplication` per process (a second crashes), and a `Gtk.ApplicationWindow` may only be added to a registered application — an unregistered owner both warns (`New application windows must be added after the GApplication::startup signal…`) and silently drops the window. So the suite builds one application, registers it once (which is what emits `startup`), and passes it as the `application=` owner for every window. That shared instance (`_test_application` in `giruntime/ui/test_main_window.py`) is a real `NotesApplication` under an isolated test id, so the display-gated help tests can drive its app-scoped seams (`_ensure_help_window`, `_install_help_action`) against a registered owner; registering it does not open the database (that is `do_activate`'s job, never invoked here). Tests that only need window-lifetime *logic* with no real widgets (`test_application.py`) instead build an unregistered `NotesApplication` with duck-typed fake windows and never add a real window to it.
- **The search `query ↔ text` binding is tested by driving the entry with `set_text` (and by writing `AppState.query`), not by simulating per-character typing.** Simulating typing via `Gtk.Editable.insert_text` + `get_position()` is GTK-runtime-fragile: on GTK 4.14 `insert_text` at an explicit position does **not** advance the widget cursor, so re-read positions stay at 0 and the text reverses (`"test"` → `"tset"`) even with no binding at all. The reverse-echo/cursor-reset property that a typing simulation aimed to pin is now structural — `GObject.BindingFlags.BIDIRECTIONAL` suppresses the re-entrant `set_text` (see the binding in `toolbar.py`) — so the binding is exercised directly instead.
- **The editor grammar is tested as data, through Python's `re`.** `giruntime/ui/test_language_spec.py` parses the committed `language_spec.lang` and exercises each context's regex directly (`re.MULTILINE` reproduces GtkSourceView's line-oriented matching), plus the structural invariants no fixture can see: every defined context reachable from the dispatch list, every `style-ref` declared (and every declared style used), and the documented precedence rules (block before inline, bracketed link forms before `bare-url`). Driving GtkSourceView's own engine instead was considered and rejected — its context engine applies tags anonymously, so an engine-level test could assert little beyond "something was highlighted". The residue that leaves — a pattern GRegex rejects but Python accepts — is caught by `test_note_editor.py`: a grammar GtkSourceView cannot load yields no `GtkSource.Language`. **Known divergence:** `table-cell-separator`'s second alternative carries no line anchor, so a `|` in ordinary prose is styled as a cell separator, contrary to that context's own comment; the suite pins the current behaviour rather than silently correcting the grammar.
- For pylint, test files additionally disable `too-many-public-methods,protected-access,duplicate-code,too-many-lines,too-few-public-methods`. `consider-using-with` is *not* disabled wholesale for tests — the handful of legitimate cases (a temp dir that must outlive the `with` block) carry a locally-justified `disable-next` instead.

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
- **When parsing text**, never assume it is well-formed: raise a specific `ParseErrorKind` rather than silently ignoring a syntax error. Two documented exceptions, both of which *record* the error rather than ignoring it: `summary.py`'s permissive fallback, and `parser.parse_recovering`, which places the offending source in the tree as an `UnreadBlock` instead of raising. Strict `parse` is unchanged and accepts exactly what it always did.

If a change would break one of these, that is the signal to discuss the design —
not to silently drop the invariant.

---

## 7. Packaging & distribution

`folio` ships as a **zipapp** — a single `folio.pyz` run with `python folio.pyz`.
There is no wheel and no `[build-system]`; `pyproject.toml` carries only project
metadata and tool config. `build_pyz.py` archives the `src/` tree directly,
filtering out `__pycache__`, `test_*.py`, the grammar *sources*
(`language_spec.lang`, `folio.gresource.xml`), and developer documentation
(`*.md` — i.e. this file). Everything else — `css/*.css`, the compiled
`folio.gresource`, and the `system_docs/*` files — rides along.
`src/__main__.py` lands at the archive root and is the implicit entry point.

`build_pyz._included` is the **single definition of "what ships"** — the `.deb`
imports the same predicate (see below), so the two channels cannot drift. Note
the asymmetry it encodes: `*.md` is *documentation* and never ships, while
`system_docs/*.adoc` is *content the app reads* and always does.

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
`folio.pyz`. `make clean` removes both. Compiled bundles are gitignored by
*pattern* (`*.gresource`), not by path: a hardcoded path is what let a stale
`src/ui/folio.gresource` survive the `src/ui` → `src/giruntime/ui` move and get
committed.

### The Debian package (`.deb`) — a second, parallel route

The zipapp above is unchanged and still the dev/release path. Alongside it, an
**upstream Meson build** produces a `folio_<version>_all.deb`. The two paths
share their inputs and never interfere:

| | zipapp | `.deb` |
| --- | --- | --- |
| Driver | `make pyz` → `build_pyz.py` | `meson` + `ninja` (debhelper drives them) |
| GResource | `make resource` builds it in-tree | `gnome.compile_resources` builds it in the build dir |
| "What ships" | `build_pyz._included` (no `test_*.py`, no `*.md`, no grammar sources) | **the same** `build_pyz._included` |
| Result | `folio.pyz` (`src/` at the archive root) | `/usr/share/folio/` (`src/` as a private `sys.path` root) |

**Not in the Debian archive.** An ITP was filed and declined, so `debian/` exists
for exactly one purpose: building the `.deb` published on the Releases page.
Archive-facing files are deliberately absent — `debian/watch`,
`Standards-Version`, `debian/upstream/metadata`, `debian/salsa-ci.yml`,
autopkgtests under `debian/tests/`. The package is **native**
(`debian/source/format` is `3.0 (native)`) for the same reason: upstream and
packaging are one repository, so there is no Debian revision. Do not re-add any
of it — a lintian tag asking for one of those files is an archive tag and does
not apply here.

- **Private directory, not `dist-packages`.** The app installs its whole tree
  into `/usr/share/folio` and `/usr/bin/folio` runs *that directory* as
  `__main__` (`runpy.run_path`) — the same mechanism as `python folio.pyz`. This
  is what preserves the package-less `src/` import model (§4): the generic
  top-level names (`config`, `models`, `search`, `storage`, …) never enter the
  shared Python library namespace, and the in-package data files
  (`folio.gresource`, `css/app.css`, `system_docs/*`) keep the committed relative
  paths `importlib.resources` resolves.
- **One definition of "what ships".** `build-aux/install_python_tree.py` (the
  Meson install script) imports `build_pyz._included` rather than restating the
  exclusion rules, so the `.deb` and the zipapp carry the same files. It adds one
  exclusion of its own: compiled `*.gresource` bundles are build *outputs* — Meson
  installs the one it compiled, so a source-tree copy is never installed over it.
- **`debian/rules` must say `--buildsystem=meson`.** Auto-detection would pick
  `makefile` (debhelper looks for a `Makefile` before a `meson.build`, and this
  repo keeps its dev `Makefile` at the root), which silently builds an **empty
  package**. `--with python3` is only post-processing: `dh_python3` byte-compiles
  the private dir and fills `${python3:Depends}`.
- **Desktop integration** ships with the package: `data/*.desktop`,
  `data/*.metainfo.xml`, and the app icon installed into `hicolor`. All three are
  named for the app id.

**App id: `io.github.rand_byte.Folio`** — one string is the `GtkApplication`
application-id (`giruntime/ui/application.py`), the icon *file* name, the
`.desktop` basename and the AppStream `<id>`, so the dock icon and the in-window
icon resolve through the same name. The **GResource prefixes are a separate
namespace** and deliberately still read `/org/folio/…` (`enums.GResourceSubtree`
+ the manifest): they are internal mount points, not the app id.

**Host requirements** (documented, never installed by the `Makefile`):
`dpkg-dev`, `debhelper (>= 13)`, `dh-python`, `meson`, `ninja-build`,
`libglib2.0-dev-bin`, `git`; plus `lintian` for `make deb-lint`. `debian/control`
build-depends on `python3 (>= 3.13)`, so the build-dependency check **fails on
Ubuntu 24.04** (python3.12) — the package targets **Debian 13 *trixie* or
newer**. `DEB_BUILD_FLAGS` is the escape hatch:
`make deb DEB_BUILD_FLAGS="-us -uc -b -d"`.

#### Build the package (`make deb` — the primary path)

```sh
make deb        # -> build/deb/folio_0.9.2~rc1_all.deb
make deb-lint   # + lintian
make deb-clean  # remove build/deb
```

Two invariants make this route what it is:

- **Every artifact lives in `build/deb/`.** The tree is exported there with
  `git archive` and `dpkg-buildpackage` runs *inside* that export, so the
  working tree never collects debhelper's staging litter (`debian/files`,
  `debian/folio/`, `debian/*.substvars`) and nothing lands in `..`. `build/` is
  already gitignored; `make deb-clean` (and `make clean`) removes it.
- **The package is `HEAD`, not the working tree.** `make deb` **fails** on a
  dirty tree (`git diff --quiet HEAD`) rather than warning, so "the `.deb` is
  exactly HEAD" is a hard rule. It is also what guarantees the package holds
  committed content only — the gitignored in-tree `folio.gresource` can never
  leak into it, and Meson's own compiled bundle is the one installed.

`make deb` deliberately depends on **neither** `type`/`lint`/`test` **nor**
`resource`: packaging and validation are orthogonal (`make all` composes them),
forcing the GTK suite into a package build would drag `weston` into the
build-host requirements for no packaging benefit (which is why `debian/rules`
already disables `dh_auto_test`), and the dev GResource in the source tree has
no relationship to the one Meson compiles. It *does* depend on `version-check`.

Binary-only (`-us -uc -b`): `dpkg-source -b` never runs, so **`make deb`
produces no source package**.

#### The same build in CI (`.github/workflows/package.yml`)

`make deb` also runs on every push and pull request, in the same `debian:trixie`
container `validate.yml` uses, and the `.deb` is uploaded as a **run artifact**
(`folio-deb`). The workflow provisions a host and calls the target — it restates
neither the build recipe nor the build-dependency list, so it cannot drift from
the `Makefile` or from `debian/control`.

Three things about that environment are load-bearing, all recorded in the file
itself:

- **`git` is installed *before* `actions/checkout`.** The trixie image has none,
  and `actions/checkout` silently falls back to a REST tarball when git is
  missing — leaving no `.git`, which breaks *both* of `make deb`'s git-shaped
  preconditions (the dirty-tree guard and `git archive HEAD`). A tarball
  checkout fails the job at `make deb`, not at checkout, so the cause is not
  obvious from the failure.
- **`git config --global --add safe.directory`** on the workspace: container
  jobs trip git's dubious-ownership check, which would fail the dirty-tree
  guard before it could evaluate anything.
- **`mk-build-deps --install --remove … debian/control`** installs
  `Build-Depends` rather than an inline apt list. `dpkg-dev`, installed
  alongside `git`, is *not* a restatement of it — it backs the `Makefile`'s own
  `deb-tools` guard (`dpkg-parsechangelog`, `dpkg-buildpackage`).

Consequently the packaging job needs **no GTK runtime, no typelibs and no
weston**: the `.deb` build never imports `gi` (Meson's install script pulls only
`build_pyz._included`) and `debian/rules` disables `dh_auto_test`, so the widget
suite stays entirely `validate.yml`'s job. Because `make deb` depends on
`version-check`, a violation of the version table below is a red job on every
push rather than a surprise at release time.

Run artifacts are **not a distribution channel** — they expire and require a
GitHub login. Releases remain the published route; the artifact exists so that a
build failure is caught per-commit and so a reviewer can install the exact
package a branch produces.

**What a release carries.** Since `0.9.2~rc1` a GitHub Release attaches *both*
artifacts — `folio.pyz` and `folio_<version>_all.deb` — so the `.deb` is a thing
users download, not only a thing they can build. The user-facing install
instructions for both live in the top-level [`README.md`](../README.md) §3,
which also records the floor the package's `python3 (>= 3.13)` dependency
implies: Debian 13 (*trixie*) or newer, Ubuntu 25.04 or newer; on Ubuntu 24.04
LTS (Python 3.12) `apt` refuses the package and the zipapp is the only route.
Keep that section in step when the artifact set changes.

#### Manual orchestration (the documented fallback)

Use the recipe below when you need what `make deb` knowingly does not do:
iterate on packaging from a dirty tree, pass `-d` on a host that cannot satisfy
the build-deps, or build a **source** package. The package is native, so a
source build needs no orig tarball — drop the `-b` and `dpkg-source` packs the
tree as it stands. It runs in the working tree, so it litters `debian/` and `..`
with the staging files `make deb` exists to avoid.

```sh
dpkg-buildpackage -us -uc -b
lintian -i -I ../folio_*_all.deb
```

**Versioning — one release, two dialects.** `pyproject.toml` holds the version
and everything else mirrors it. Pre-releases are where the dialects diverge, so
the mapping matters:

| Where | 0.9.2 release candidate | Final 0.9.2 |
| --- | --- | --- |
| `pyproject.toml` (**source of truth**) | `0.9.2rc1` (PEP 440) | `0.9.2` |
| `meson.build` (`version:`) | `0.9.2rc1` (PEP 440) | `0.9.2` |
| `data/io.github.rand_byte.Folio.metainfo.xml` (`<release version=…>`) | `0.9.2rc1` (PEP 440) | `0.9.2` |
| `debian/folio.1` (`.TH` source string) | `0.9.2rc1` (PEP 440) | `0.9.2` |
| git tag | `v0.9.2-rc1` | `v0.9.2` |
| `debian/changelog` | `0.9.2~rc1` | `0.9.2` |

**`build-aux/check_version.py` enforces this table** (`make version-check`, and a
prerequisite of `make deb`): it reads all five files, treats `pyproject.toml` as
the source of truth, and exits non-zero with one line per disagreement. It
*reads and reports, never rewrites* — which file is wrong is a human decision.
The site list is closed and enumerated (`VersionSource`), so a new version-
bearing file is guarded by adding one enum member and one parser. The man page's
`.TH` title line states the *upstream* spelling — it names the program, not the
package — so it mirrors `pyproject.toml` verbatim; only the changelog takes the
Debian dialect.

It parses `debian/changelog`'s first line itself rather than shelling out to
`dpkg-parsechangelog`, so it runs on a host with no Debian tooling. The version
is compared verbatim: the package is native, so a `-<revision>` suffix written
by hand is reported as a mismatch rather than stripped.

The **tilde is not cosmetic**: `~` is the only character that sorts *before* the
empty string in dpkg's comparison, so `0.9.2~rc1` < `0.9.2`, which is what makes
the RC upgradable to the final release. Spelling it `0.9.2-rc1` instead would
sort *after* `0.9.2` and apt would never offer the upgrade. AppStream marks the
same release `type="development"`, which becomes `type="stable"` for the final
release.

**There is no Debian revision.** The package is native (§7's *Not in the Debian
archive*), so `debian/changelog` carries the version and nothing else — the
`-1`/`-2` suffix that would let a packaging-only rebuild supersede its
predecessor does not exist here. A rebuilt `.deb` that must install *over* the
previous one therefore needs an upstream bump in `pyproject.toml`, and so in all
five sites above. With one repository holding both the app and its packaging,
there is no packaging-only change that is not also a source change. One visible
consequence in the built package: debhelper installs the changelog as
`/usr/share/doc/folio/changelog.gz`, not `changelog.Debian.gz` — for a native
package the two are the same file.

Files: `meson.build`, `folio.in` (launcher template),
`build-aux/install_python_tree.py`, `build-aux/check_version.py` (+ its
`test_check_version.py`), `data/`, `debian/`, `.github/workflows/package.yml`,
and the `deb*` / `version-check` targets in the `Makefile`. None of them live inside `src/` — `src/` is installed
wholesale. `build-aux/` is build tooling: never shipped, but type-checked,
linted and tested like everything else (the `Makefile` globs it, so a new script
is covered by construction).

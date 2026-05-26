"""The :class:`Gtk.Application` subclass — wires everything together.

Principles & invariants
-----------------------
* :class:`NotesApplication` is the single point in the program that
  composes the layered architecture into a runnable system. It owns
  the :class:`Database`, the repositories, the :class:`AttachmentStore`,
  :class:`AppState`, and the controllers that the UI layer's editor
  and (future) buttons drive. Every other module receives those as
  parameters. No other module reaches for a global "the database" or
  "the app state" — composition flows top-down from here.
* :meth:`do_activate` is the only :class:`Gtk.Application` vfunc this
  class overrides. The application is registered as a
  :class:`Gio.ApplicationFlags.FLAGS_NONE` (single-instance) app; a
  second ``./run`` while one is already running raises
  the existing window rather than opening a new one. That matches the
  design's single-window assumption.
* Long-lived resources (the :class:`Database`, the repositories,
  the :class:`AttachmentStore`, :class:`AppState`, the
  :class:`NoteController`) are initialised once on the first
  activation and reused on every subsequent activation. The
  migration runner is invoked exactly once per process lifetime;
  it is itself idempotent, but skipping the work avoids redundant
  version reads on activations past the first.
* The application's CSS bundle is loaded on the first activation and
  attached to the default :class:`Gdk.Display` at
  :data:`Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION` — the lowest priority
  band that still wins over the theme's own provider. Theme files,
  user overrides, and runtime style providers all stack above it,
  which is exactly the precedence the GTK 4 documentation recommends
  for application-bundled CSS. The bundle is read via
  :mod:`importlib.resources` so it works both from a source checkout
  and from inside the ``folio.pyz`` zipapp — there is no filesystem path
  assumption.
* The seeded welcome note is loaded by id (:data:`SEED_WELCOME_NOTE_ID`).
  If the user has deleted it, the application falls back to the most
  recently modified note in the library (:meth:`NoteRepositoryProtocol.list_all`
  is sorted by ``modified_at DESC``). If no notes exist at all, the
  selection stays ``None`` and the right pane renders empty — exactly
  the policy :class:`NoteView.refresh` already implements.
* Database errors during activation surface through
  :class:`sqlite3.DatabaseError`; we let them propagate. A failure to
  open the database is fatal for v1 (no data → nothing to show) and
  GTK 4 turns an unhandled exception in :meth:`do_activate` into a
  process-level crash with the original traceback. That is the right
  failure mode at this layer — better than swallowing the error and
  showing an empty window with no explanation.
* From build step 11 the :class:`AttachmentStore` is wired in for
  real: image attachments now flow end-to-end through the editor's
  Image button → :class:`Gtk.FileDialog` → :class:`NoteController.add_attachment`
  → :class:`AttachmentStore.add_for_note` → SQLite BLOB; and the
  rendered view's image-bytes resolver (built inside
  :class:`NoteView`) closes over the same store to fetch bytes by
  filename when an image macro is encountered. The step-10
  :class:`_PlaceholderAttachmentStore` is removed.
"""

from __future__ import annotations

import importlib.resources

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
# pylint: disable=wrong-import-position
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from config.defaults import SEED_WELCOME_NOTE_ID
from config.paths import database_path
from controllers.app_state import AppState
from controllers.note_controller import NoteController
from storage.attachment_store import AttachmentStore
from storage.database import Database
from storage.migrations import apply_pending
from storage.note_repository import NoteRepository
from storage.notebook_repository import NotebookRepository
from ui.main_window import MainWindow


_APPLICATION_ID: str = "org.folio.Folio"
"""Reverse-DNS-shaped identifier registered with the session bus.

GTK uses this to enforce single-instance behaviour and to name the
application's resource bundles. The string is fixed across releases —
changing it would orphan any per-application user settings the OS may
record under it.
"""


_APPLICATION_CSS_PACKAGE: str = "ui.css"
"""The package containing bundled application CSS resources.

Loaded via :mod:`importlib.resources` rather than a filesystem path
so the bundle resolves correctly whether the app is run from a
source checkout (``src/ui/css``) or from inside the ``folio.pyz``
zipapp (where ``ui`` sits at the archive root). The zipapp build
archives ``src/`` directly, so ``css/*.css`` rides along without any
separate packaging declaration.
"""


_APPLICATION_CSS_FILENAME: str = "app.css"
"""The single CSS file at the root of :data:`_APPLICATION_CSS_PACKAGE`.

v1 has one CSS file; if more are added, this loader is the place to
iterate them. The file styles the note-view parse-error banner (and
later, any other application-level visuals that need theming).
"""


class NotesApplication(Gtk.Application):
    """The application's :class:`Gtk.Application` subclass.

    Holds the long-lived dependencies — database, repositories,
    attachment store, app state, and the editor's
    :class:`NoteController` — and presents a :class:`MainWindow` on
    activation.
    """

    _database: Database | None
    _note_repository: NoteRepository | None
    _notebook_repository: NotebookRepository | None
    _attachment_store: AttachmentStore | None
    _app_state: AppState | None
    _note_controller: NoteController | None

    def __init__(self) -> None:
        super().__init__(
            application_id=_APPLICATION_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._database = None
        self._note_repository = None
        self._notebook_repository = None
        self._attachment_store = None
        self._app_state = None
        self._note_controller = None

    def do_activate(self) -> None:  # pylint: disable=arguments-differ
        """Build the world if it does not yet exist, then present a
        window.

        Called by GTK on the first ``run()`` and on every subsequent
        activation request (e.g. a user re-launching the app's
        ``.desktop`` entry while it is already running). The
        single-instance flag keeps that re-launch from spawning a
        second process, but the activation itself fires every time.
        """
        if self._database is None:
            self._initialise_runtime()

        # Reuse the existing window when one is already open
        # (subsequent activations) — otherwise build the first one.
        window = self.get_active_window()
        if window is None:
            window = self._build_initial_window()
        window.present()

    def _initialise_runtime(self) -> None:
        """Open the database, run migrations, build repositories,
        the attachment store, the app state, and the note controller.

        Runs exactly once per process. The database path is resolved
        through :func:`database_path`, which honours
        ``XDG_DATA_HOME``. Migrations are idempotent on a current
        database, so re-invocation across activations would still be
        safe — but skipping the call avoids the redundant version
        read.

        The bundled application CSS is loaded here too — once per
        process, attached to the default :class:`Gdk.Display` at
        :data:`Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION`. This must
        run *after* :meth:`Gtk.Application.__init__` (so that GTK
        is initialised and a default display exists) but *before*
        any window is built (so the styling is in place for the
        first paint).
        """
        self._database = Database(database_path())
        apply_pending(self._database)
        self._note_repository = NoteRepository(self._database)
        self._notebook_repository = NotebookRepository(self._database)
        self._attachment_store = AttachmentStore(self._database)
        self._app_state = AppState()
        self._note_controller = NoteController(
            repository=self._note_repository,
            attachments=self._attachment_store,
            app_state=self._app_state,
        )
        _load_application_css()

    def _build_initial_window(self) -> MainWindow:
        """Construct the first :class:`MainWindow` and seed the
        selection.

        After construction, the welcome note is selected if it is
        still in the library; otherwise the most recently modified
        note is selected; otherwise nothing is selected (the right
        pane renders empty). Selection happens *after* window
        construction so that the :class:`NoteView`'s
        ``selected-note-changed`` handler is already wired by the
        time the signal fires.
        """
        # Local non-None aliases — narrows ``Optional`` for the type
        # checker and documents the precondition that
        # :meth:`_initialise_runtime` ran first.
        assert self._note_repository is not None
        assert self._notebook_repository is not None
        assert self._attachment_store is not None
        assert self._app_state is not None
        assert self._note_controller is not None

        window = MainWindow(
            application=self,
            note_repository=self._note_repository,
            notebook_repository=self._notebook_repository,
            note_controller=self._note_controller,
            app_state=self._app_state,
            attachment_store=self._attachment_store,
        )
        self._select_initial_note(self._note_repository, self._app_state)
        return window

    @staticmethod
    def _select_initial_note(
        repository: NoteRepository,
        app_state: AppState,
    ) -> None:
        """Pick the welcome note, or fall back to the newest note.

        The two-step fallback (welcome → newest → none) is what keeps
        the first-launch experience predictable while not breaking the
        app for a user who has cleaned out their library. The fallback
        chain is documented at the module level — keep them in sync.
        """
        try:
            welcome = repository.get(SEED_WELCOME_NOTE_ID)
        except KeyError:
            welcome = None

        if welcome is not None:
            app_state.set_selected_note_id(welcome.id)
            return

        # ``list_all`` is sorted by ``modified_at DESC`` — the first
        # entry is the most recently touched note.
        all_notes = repository.list_all()
        if all_notes:
            app_state.set_selected_note_id(all_notes[0].id)
            return
        # No notes at all — leave the selection empty. ``NoteView``
        # renders an empty buffer in that case.


def _load_application_css() -> None:
    """Read the bundled application CSS and attach it to the default display.

    The CSS bundle is loaded via :mod:`importlib.resources` so it
    resolves correctly across source checkout, installed wheel, and
    frozen-bundle deployments. The provider is added at
    :data:`Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION` — the
    documented band for application-bundled styles, sitting *below*
    user overrides and the theme but *above* the GTK fallback. This
    is the precedence the GTK 4 docs recommend for application CSS.

    The function returns silently when the default display is not
    available — a defensive guard for embedded or test contexts
    where :class:`Gtk.Application` runs without a display. Production
    always has one by the time :meth:`_initialise_runtime` runs.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    css_source = (
        importlib.resources
        .files(_APPLICATION_CSS_PACKAGE)
        .joinpath(_APPLICATION_CSS_FILENAME)
        .read_text(encoding="utf-8")
    )
    provider = Gtk.CssProvider.new()
    provider.load_from_string(css_source)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

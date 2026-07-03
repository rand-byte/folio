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
* The application icon is registered the same way, once, on first
  activation: :func:`_register_application_icon_resources` adds the
  gresource-bundled SVG to the default display's :class:`Gtk.IconTheme`
  and sets it as every window's fallback icon
  (:meth:`Gtk.Window.set_default_icon_name`). This is **in-app**
  plumbing only — it makes the icon resolvable by name (e.g. by a
  future :class:`Gtk.AboutDialog`) with no install step, but GTK 4 has
  no API to set a taskbar/dock icon directly (Wayland compositors
  resolve that from a ``.desktop`` file's ``Icon=`` key via the
  ``hicolor`` theme instead); that OS-level packaging is not done here.
* The seeded welcome note is loaded by id (:data:`SEED_WELCOME_NOTE_ID`).
  The restored-from-last-run note (see below) is tried first; if there
  is none, or it no longer exists, the welcome note is tried next; if
  the user has deleted it, the application falls back to the most
  recently modified note in the library (:meth:`NoteRepositoryProtocol.list_all`
  is sorted by ``modified_at DESC``). If no notes exist at all, the
  selection stays ``None`` and the right pane renders empty — exactly
  the policy :class:`NoteView.refresh` already implements.
* The last-open note and the main window's size/maximized state persist
  across launches through :class:`SessionStateStore`
  (:mod:`storage.session_state_store`), a plain JSON file at
  :func:`session_state_path` — not GSettings, since the app ships as an
  installer-less zipapp with no step that would install a compiled
  GSettings schema. The state is loaded once, in
  :meth:`_build_initial_window`, before the window and the initial
  selection are built, and saved once, in
  :meth:`_on_main_window_close_request`, right before the process ends
  — there is no intermediate autosave, since nothing between those two
  points needs to be recovered. Both the load and the save degrade
  gracefully: a missing, unreadable, or malformed state file behaves
  exactly like "no prior run" (:meth:`SessionStateStore.load` never
  raises), and a restored note id that no longer exists (the note was
  deleted, or the database was reset and rebuilt) falls through to the
  pre-existing welcome/newest/none chain rather than leaving the
  selection empty or crashing.
* Database errors during activation surface through
  :class:`sqlite3.DatabaseError`; we let them propagate. A failure to
  open the database is fatal for v1 (no data → nothing to show) and
  GTK 4 turns an unhandled exception in :meth:`do_activate` into a
  process-level crash with the original traceback. That is the right
  failure mode at this layer — better than swallowing the error and
  showing an empty window with no explanation.
* From build step 11 the :class:`AttachmentStore` is wired in for
  real: attachments flow end-to-end through the editor pane's
  attachments-panel *Add file* button → :class:`Gtk.FileDialog` →
  :class:`NoteController.add_attachment`
  → :class:`AttachmentStore.add_for_note` → SQLite BLOB; and the
  rendered view's image-bytes resolver (built inside
  :class:`NoteView`) closes over the same store to fetch bytes by
  filename when an image macro is encountered. The step-10
  :class:`_PlaceholderAttachmentStore` is removed.
* The application owns the single, non-modal :class:`HelpWindow`. It
  registers an app-scoped ``help`` action (``F1``) on first activation
  and, when that action fires (from the accelerator or the toolbar's
  Help button), builds the help window once and reuses it thereafter —
  re-opening :meth:`Gtk.Window.present`-s the existing window rather than
  spawning a duplicate. The help is app-scoped, so the action and the
  window live here rather than on any per-note widget. Reuse depends on
  the window being **hide-on-close** (set in :class:`HelpWindow` itself):
  closing it hides rather than destroys it, so the cached reference stays
  a live window across close/re-open.
* The application's lifetime is bound to its **main window**, not to the
  set of all registered windows. Because the help window is hide-on-close
  it stays registered (just hidden) after a close, and a registered window
  keeps :class:`Gtk.Application` running — so "quit when no windows remain"
  would leave the process alive once help had been opened. The main
  window's ``close-request`` therefore calls :meth:`Gtk.Application.quit`
  (see :meth:`_on_main_window_close_request`), which also tears down the
  hidden help window. This is sound precisely because of the single-window
  assumption above: there is only ever one primary window to key off.
"""

from __future__ import annotations

import importlib.resources
from typing import Protocol

from gi.repository import Gdk, Gio, Gtk

from config.defaults import SEED_WELCOME_NOTE_ID
from config.paths import database_path, session_state_path
from enums import GResourceSubtree
from giruntime.controllers.app_state import AppState
from giruntime.controllers.note_controller import NoteController
from giruntime.controllers.note_list_store import NoteListStore
from giruntime.ui import _gresource
from giruntime.ui.help_window import HelpWindow
from giruntime.ui.main_window import MainWindow
from models.session_state import SessionState
from storage.attachment_store import AttachmentStore
from storage.database import Database
from storage.migrations import apply_pending
from storage.note_repository import NoteRepository
from storage.session_state_store import SessionStateStore


_APPLICATION_ID: str = "org.folio.Folio"
"""Reverse-DNS-shaped identifier registered with the session bus.

GTK uses this to enforce single-instance behaviour and to name the
application's resource bundles. The string is fixed across releases —
changing it would orphan any per-application user settings the OS may
record under it.
"""


_APPLICATION_CSS_PACKAGE: str = "giruntime.ui.css"
"""The package containing bundled application CSS resources.

Loaded via :mod:`importlib.resources` rather than a filesystem path
so the bundle resolves correctly whether the app is run from a
source checkout (``src/giruntime/ui/css``) or from inside the ``folio.pyz``
zipapp (where ``giruntime`` sits under the archive root). The zipapp build
archives ``src/`` directly, so ``css/*.css`` rides along without any
separate packaging declaration.
"""


_APPLICATION_CSS_FILENAME: str = "app.css"
"""The single CSS file at the root of :data:`_APPLICATION_CSS_PACKAGE`.

v1 has one CSS file; if more are added, this loader is the place to
iterate them. The file styles the note-view parse-error banner (and
later, any other application-level visuals that need theming).
"""


_HELP_ACTION_NAME: str = "help"
"""Name of the application-level action that opens the help window.

Registered on the :class:`Gio.ApplicationActionGroup` as ``app.help`` and
bound to :data:`_HELP_ACCELERATOR`. Window-independent on purpose — the
help is app-scoped, not tied to any one note or window.
"""

_HELP_ACTION_DETAILED_NAME: str = "app.help"
"""The detailed action name used for the accelerator binding and menus."""

_HELP_ACCELERATOR: str = "F1"
"""The keyboard accelerator that triggers ``app.help`` — the platform
convention for help."""


class _WindowGeometryProtocol(Protocol):
    """The two read-only bits of window state
    :meth:`NotesApplication._save_session_state` needs.

    :class:`MainWindow` (a :class:`Gtk.ApplicationWindow`) satisfies this
    structurally with no extra wiring — it is exactly the call surface
    :class:`Gtk.Window` already exposes. Naming it narrows
    ``_save_session_state``'s parameter to only what it actually calls
    (per the project's "minimum necessary type" rule) rather than the
    full :class:`MainWindow` widget, and lets tests substitute a
    display-free fake instead of constructing a real window.
    """

    def get_default_size(self) -> tuple[int, int]: ...

    def is_maximized(self) -> bool: ...


class NotesApplication(  # pylint: disable=too-many-instance-attributes
    Gtk.Application,
):
    """The application's :class:`Gtk.Application` subclass.

    Holds the long-lived dependencies — database, repositories,
    attachment store, app state, and the editor's
    :class:`NoteController` — and presents a :class:`MainWindow` on
    activation.
    """

    _database: Database | None
    _note_repository: NoteRepository | None
    _note_store: NoteListStore | None
    _attachment_store: AttachmentStore | None
    _app_state: AppState | None
    _note_controller: NoteController | None
    _help_window: HelpWindow | None
    _session_state_store: SessionStateStore | None

    def __init__(self) -> None:
        super().__init__(
            application_id=_APPLICATION_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._database = None
        self._note_repository = None
        self._note_store = None
        self._attachment_store = None
        self._app_state = None
        self._note_controller = None
        self._help_window = None
        self._session_state_store = None

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
        first paint). The application icon is registered with the
        icon theme right after, for the same before-any-window-exists
        reason.

        The :class:`SessionStateStore` is constructed here too (its
        path resolved through :func:`session_state_path`, which — like
        :func:`database_path` — honours ``XDG_DATA_HOME``) but not read
        from yet; :meth:`_build_initial_window` is what calls
        :meth:`SessionStateStore.load`, once the note store it needs to
        validate a restored note id against already exists.
        """
        self._database = Database(database_path())
        apply_pending(self._database)
        self._note_repository = NoteRepository(self._database)
        self._note_store = NoteListStore(repository=self._note_repository)
        self._note_store.load()
        self._attachment_store = AttachmentStore(self._database)
        self._app_state = AppState()
        self._note_controller = NoteController(
            note_store=self._note_store,
            attachments=self._attachment_store,
            app_state=self._app_state,
        )
        self._session_state_store = SessionStateStore(session_state_path())
        self._install_help_action()
        _load_application_css()
        _register_application_icon_resources()

    def _install_help_action(self) -> None:
        """Register the app-level ``help`` action and its ``F1`` accel.

        The action is window-independent (app-scoped), so it lives on the
        :class:`Gtk.Application` rather than any window — both the
        accelerator and any menu item route to the same action. Runs once
        per process, alongside the rest of the one-time runtime setup.
        """
        help_action = Gio.SimpleAction.new(_HELP_ACTION_NAME, None)
        help_action.connect("activate", self._on_help_activated)
        self.add_action(help_action)
        self.set_accels_for_action(
            _HELP_ACTION_DETAILED_NAME,
            [_HELP_ACCELERATOR],
        )

    def _build_initial_window(self) -> MainWindow:
        """Construct the first :class:`MainWindow` and seed the
        selection.

        The session state saved on the previous run — if any — is
        loaded here (once, before the window exists) and used two
        ways: :meth:`_select_initial_note` tries its ``selected_note_id``
        first, ahead of the welcome/newest/none fallback; and
        :class:`MainWindow` is constructed with the whole
        :class:`SessionState` so it can restore the saved window
        size/maximized state instead of always using its computed
        default. A missing or unparsable saved state resolves to
        :data:`models.session_state.DEFAULT_SESSION_STATE`
        (:meth:`SessionStateStore.load` never raises), which is
        indistinguishable from "no prior run" to both consumers —
        first launch is not a special case here.

        After construction, the restored note is selected if it still
        exists; otherwise the welcome note is selected if it is still
        in the library; otherwise the most recently modified note is
        selected; otherwise nothing is selected (the right pane renders
        empty). Selection happens *after* window construction so that
        the :class:`NoteView`'s ``selected-note-changed`` handler is
        already wired by the time the signal fires.
        """
        # Local non-None aliases — narrows ``Optional`` for the type
        # checker and documents the precondition that
        # :meth:`_initialise_runtime` ran first.
        assert self._note_store is not None
        assert self._attachment_store is not None
        assert self._app_state is not None
        assert self._note_controller is not None
        assert self._session_state_store is not None

        restored_state = self._session_state_store.load()

        window = MainWindow(
            application=self,
            note_store=self._note_store,
            note_controller=self._note_controller,
            app_state=self._app_state,
            attachment_store=self._attachment_store,
            restored_state=restored_state,
        )
        # The main window is the application's primary window: closing it
        # must end the program even when the hide-on-close help window is
        # still registered (and merely hidden). See
        # :meth:`_on_main_window_close_request`.
        window.connect("close-request", self._on_main_window_close_request)
        self._select_initial_note(
            self._note_store,
            self._app_state,
            restored_state.selected_note_id,
        )
        return window

    def _on_main_window_close_request(
        self,
        window: _WindowGeometryProtocol,
    ) -> bool:
        """Save session state, then quit the application.

        ``Gtk.Application`` keeps its main loop alive while *any* window
        is registered with it, and registration tracks windows by
        existence, not visibility. The :class:`HelpWindow` is
        **hide-on-close**, so closing it only hides it; the cached
        instance stays a registered (hidden) window. A plain
        "quit when the last window closes" rule therefore never fires
        once help has been opened — the hidden help window keeps the
        application running after the main window is gone, and the
        process hangs.

        Binding the lifetime to the main window removes that hang:
        :meth:`Gtk.Application.quit` stops the loop and tears down the
        lingering hidden help window. This relies on the design's
        single-window assumption (exactly one :class:`MainWindow` plus an
        optional :class:`HelpWindow`); if multiple primary windows were
        ever introduced, this would need to quit on the *last* one
        instead.

        The session-state save happens first, and only here: this is
        the one point in the app's lifetime that is both after the
        user's last possible interaction and guaranteed to run before
        the process exits, so there is nothing later to capture and
        nothing earlier that would need re-saving.

        Returns ``False`` so GTK's default handler still runs and
        destroys the window — the veto path (returning ``True``) is never
        wanted here.
        """
        self._save_session_state(window)
        self.quit()
        return False

    def _save_session_state(self, window: _WindowGeometryProtocol) -> None:
        """Persist the current note selection and window geometry.

        ``Gtk.Window.get_default_size`` is GTK's own documented
        round-trip getter for this purpose: unlike the live allocated
        size, it keeps reporting the pre-maximize size while the window
        is maximized, so saving it (alongside the separate
        :meth:`Gtk.Window.is_maximized` flag) and restoring both on the
        next launch puts an un-maximized window back exactly where the
        user last left it. A ``(0, 0)`` reading — GTK's documented
        signal that no explicit size was ever set — is treated as "no
        size to save" rather than persisted verbatim; every real
        :class:`MainWindow` calls :meth:`Gtk.Window.set_default_size`
        during construction, so this only guards a degenerate case, the
        same role :data:`_MIN_DEFAULT_WINDOW_WIDTH_PX` plays in
        :mod:`giruntime.ui.main_window`.
        """
        assert self._app_state is not None
        assert self._session_state_store is not None

        width, height = window.get_default_size()
        window_size = (width, height) if width > 0 and height > 0 else None
        state = SessionState(
            selected_note_id=self._app_state.selected_note_id,
            window_size=window_size,
            window_maximized=window.is_maximized(),
        )
        self._session_state_store.save(state)

    def _on_help_activated(
        self,
        _action: Gio.SimpleAction,
        _parameter: object,
    ) -> None:
        """Open (or raise) the help window when ``app.help`` activates.

        Wired to both the ``F1`` accelerator and the toolbar's Help
        button. The ``_parameter`` is unused — the action carries no
        target — but it is part of the ``activate`` signal signature.
        """
        self._present_help_window()

    def _present_help_window(self) -> None:
        """Show the single help window, building it on first use.

        Single-instance reuse-and-raise: the application keeps the one
        :class:`HelpWindow`; a second open request
        :meth:`Gtk.Window.present`-s the existing one rather than
        spawning a duplicate, so the non-modal reference never stacks up.
        """
        self._ensure_help_window().present()

    def _ensure_help_window(self) -> HelpWindow:
        """Return the single help window, building it on first call.

        The reuse seam: the first call constructs the window and caches
        it; every later call returns that same instance. Kept separate
        from the :meth:`Gtk.Window.present` in :meth:`_present_help_window`
        so the build-once contract is testable without a window-raising
        side effect.
        """
        if self._help_window is None:
            self._help_window = HelpWindow(application=self)
        return self._help_window

    @staticmethod
    def _select_initial_note(
        store: NoteListStore,
        app_state: AppState,
        restored_note_id: str | None,
    ) -> None:
        """Pick the restored note, the welcome note, or the newest note.

        Reads the in-memory store — the same truth the panes bind to —
        rather than the repository, so startup selection cannot diverge
        from what the views show. ``restored_note_id`` (the last-open
        note from a previous run, or ``None`` if there isn't one — see
        :meth:`_build_initial_window`) is tried first; a ``KeyError``
        from :meth:`NoteListStore.get_note` (the note was deleted, or
        the database was reset and rebuilt without it) falls through to
        the pre-existing three-step chain unchanged: welcome → newest →
        none. That chain keeps the first-launch experience predictable
        while not breaking the app for a user who has cleaned out their
        library, or reset their database, or both. The fallback chain
        is documented at the module level — keep them in sync.
        """
        if restored_note_id is not None:
            try:
                restored = store.get_note(restored_note_id)
            except KeyError:
                restored = None
            if restored is not None:
                app_state.set_selected_note_id(restored.id)
                return

        try:
            welcome = store.get_note(SEED_WELCOME_NOTE_ID)
        except KeyError:
            welcome = None

        if welcome is not None:
            app_state.set_selected_note_id(welcome.id)
            return

        # The store loads in ``modified_at DESC`` order (the repository's
        # ``list_all`` ordering), so item 0 is the most recently touched.
        if store.get_n_items() > 0:
            first = store.get_item(0)
            app_state.set_selected_note_id(first.note.id)
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


def _register_application_icon_resources() -> None:
    """Make the bundled application icon resolvable by icon name.

    :func:`giruntime.ui._gresource.resource_path` both registers the
    compiled ``folio.gresource`` bundle (shared with
    :mod:`giruntime.ui.note_editor`'s grammar loading, so the bundle is
    still read from exactly one place) and returns
    :attr:`enums.GResourceSubtree.ICONS`'s path in one call — obtaining
    the path is what triggers registration, so there is no separate
    "did I register yet" step to get wrong. That path is added to the
    default display's :class:`Gtk.IconTheme`, which requires the
    registered subtree to follow the ``hicolor`` theme's own layout —
    hence the icon lives under ``scalable/apps/`` beneath it in
    ``folio.gresource.xml``, one level above where the icon *name* (the
    file's basename, sans extension) is looked up. The icon's name is
    :data:`_APPLICATION_ID` — the ``hicolor`` convention that an
    application's icon file is named after its application id, so the
    same string that registers the app with the session bus also looks
    up its icon; it is set as the fallback icon name for every window
    that does not set its own (:meth:`Gtk.Window.set_default_icon_name`)
    — every window in this process, today. This is in-app plumbing
    only: it lets :class:`Gtk.Image` and :class:`Gtk.Window` resolve
    the icon by name with no install step, but it is not OS-level
    desktop integration (a taskbar/dock icon additionally requires
    installing the icon under the host's ``hicolor`` theme and a
    ``.desktop`` file naming it — out of scope here).

    Returns silently when the default display is not available — the
    same defensive guard :func:`_load_application_css` uses for
    embedded or test contexts where :class:`Gtk.Application` runs
    without a display. Production always has one by the time
    :meth:`_initialise_runtime` runs.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    icon_theme = Gtk.IconTheme.get_for_display(display)
    icon_theme.add_resource_path(_gresource.resource_path(GResourceSubtree.ICONS))
    Gtk.Window.set_default_icon_name(_APPLICATION_ID)

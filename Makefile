.PHONY: all validate test type lint clean resource pyz

PY_SRC := build_pyz.py $(shell find src -type f -name "*.py" ! -name "test_*")
PY_TST := $(shell find src -type f -name "test_*.py")

# Distribution + generated-artifact paths. ``folio.gresource`` is a
# generated artifact (gitignored): every entry point builds it from the
# committed manifest before launch, so dev, test, and prod all load the
# grammar through the same compiled bundle.
PYZ      := folio.pyz
GRES     := src/ui/folio.gresource
GRES_XML := src/ui/folio.gresource.xml
GRES_SRC := src/ui/language_spec.lang
SHEBANG  := /usr/bin/env python3

# compile grammar -> GResource (sourcedir = where the manifest's <file> resolves)
$(GRES): $(GRES_XML) $(GRES_SRC)
	glib-compile-resources --sourcedir=src/ui --target=$@ $(GRES_XML)

# named alias so `run` (and humans) need not know the artifact path
resource: $(GRES)


# The UI tests build real GTK 4 widgets and are gated behind a
# `@skipUnless(_display_available())` guard, so they only run when a GDK
# display can be opened. Gnome/GTK 4 talk Wayland, so we provide one with a
# headless Weston compositor.
#
# Three things this recipe must get:
#   1. Weston is a long-running compositor — it never exits on its own. It
#      must run in the BACKGROUND; chaining it with `&&` blocks forever and
#      the tests never start.
#   2. The test process finds the compositor via WAYLAND_DISPLAY (socket name)
#      and XDG_RUNTIME_DIR (socket directory). Without WAYLAND_DISPLAY set,
#      GTK opens no display and every UI test silently SKIPS.
#   3. The socket is not created instantly, so we wait for it before launching
#      python, and we kill Weston on exit so it does not leak.
#
# Make runs each recipe line in its own shell, so the whole thing is one
# logical line joined with `\`.
#
# The target depends on $(GRES): the single grammar load path (see
# ui/note_editor.py) reads the compiled GResource, so it must exist before
# discovery. Discovery uses `-t src` so `src` is the top-level dir and test
# modules import as `config.test_paths` etc., now that `src` is not a package.
test: $(GRES)
	@export XDG_RUNTIME_DIR=$${XDG_RUNTIME_DIR:-$$(mktemp -d)}; \
		chmod 700 "$$XDG_RUNTIME_DIR"; \
		weston --backend headless --socket=test_notes --idle-time=0 >/dev/null 2>&1 & \
		weston_pid=$$!; \
		trap "kill $$weston_pid 2>/dev/null" EXIT; \
		for _ in $$(seq 1 50); do \
			[ -S "$$XDG_RUNTIME_DIR/test_notes" ] && break; \
			sleep 0.1; \
		done; \
		WAYLAND_DISPLAY=test_notes GDK_BACKEND=wayland \
			python3 -B -m unittest discover -s src -t src -f

type:
	mypy $(PY_SRC) $(PY_TST)

# pylint resolves intra-tree imports off ``src`` on the path. The former
# package layout let pylint add the package parent to sys.path
# automatically; now that ``src`` is the package-less source root,
# PYTHONPATH=src is what lets ``import config``/``import ui`` etc. resolve.
# The disable/enable flags are unchanged from the package layout.
lint:
	pylint $(PY_SRC)
	pylint $(PY_TST) --disable=too-many-public-methods,protected-access,duplicate-code,too-many-lines

# Build the distributable zipapp. Depends on the compiled GResource so the
# packaged grammar is the same artifact dev/test load. The exclusions
# (``__pycache__``, ``test_*.py``, grammar sources) live in build_pyz.py
# because the zipapp CLI has no filter flag but its API does.
pyz: $(GRES)
	python3 build_pyz.py src $(PYZ) "$(SHEBANG)"

clean:
	rm -f $(PYZ) $(GRES)

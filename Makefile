.PHONY: all validate test type lint clean resource pyz \
        deb deb-lint deb-clean deb-tools version-check

# build-aux/*.py is build tooling: never shipped, but type-checked, linted and
# tested like everything else. Globbed rather than listed so a new script is
# covered by construction -- this is how check_version.py joins the install
# script. The test_* files are filtered out of PY_SRC because PY_TST claims
# them (mypy rejects a module passed twice; pylint would lint them without the
# test-file exemptions).
PY_SRC := build_pyz.py \
          $(filter-out build-aux/test_%.py, $(wildcard build-aux/*.py)) \
          $(shell find src -type f -name "*.py" ! -name "test_*")
PY_TST := $(wildcard build-aux/test_*.py) $(shell find src -type f -name "test_*.py")

# --- aggregates --------------------------------------------------------------
validate: type lint test

all: validate pyz deb

# Distribution + generated-artifact paths. ``folio.gresource`` is a
# generated artifact (gitignored): every entry point builds it from the
# committed manifest before launch, so dev, test, and prod all load the
# grammar through the same compiled bundle.
PYZ       := folio.pyz
GRES      := src/giruntime/ui/folio.gresource
GRES_XML  := src/giruntime/ui/folio.gresource.xml
GRES_SRC  := src/giruntime/ui/language_spec.lang
GRES_ICON := src/giruntime/ui/icons/scalable/apps/io.github.rand_byte.Folio.svg
SHEBANG   := /usr/bin/env python3

# compile grammar + icon -> GResource (sourcedir = where the manifest's <file> resolves)
$(GRES): $(GRES_XML) $(GRES_SRC) $(GRES_ICON)
	glib-compile-resources --sourcedir=src/giruntime/ui --target=$@ $(GRES_XML)

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
#   4. GSK_RENDERER=cairo forces the software renderer. The UI tests present
#      real toplevels; the cairo renderer never touches GL/Vulkan/EGL, so it
#      cannot segfault inside a missing/broken GPU driver. This is mandatory,
#      not cosmetic (see src/README.md section 5 and dev-environment.md) --
#      the suite may pass on a host with no GPU stack, but on one with a
#      half-working driver the crash it guards against is live.
#
# Make runs each recipe line in its own shell, so the whole thing is one
# logical line joined with `\`.
#
# The target depends on $(GRES): the single grammar load path (see
# giruntime/ui/note_editor.py) reads the compiled GResource, so it must exist before
# discovery. Discovery uses `-t src` so `src` is the top-level dir and test
# modules import as `config.test_paths` etc., now that `src` is not a package.
# Two discovery passes, because the two trees have nothing in common but the
# runner: `build-aux/` is display-free, GTK-free build tooling and runs first
# and cheaply (it is also what makes test_check_version.py visible at all --
# the `src` pass cannot see it); `src` is the application suite that needs the
# compositor below.
test: $(GRES)
	python3 -B -m unittest discover -s build-aux -t build-aux -f
	@export XDG_RUNTIME_DIR=$${XDG_RUNTIME_DIR:-$$(mktemp -d)}; \
		chmod 700 "$$XDG_RUNTIME_DIR"; \
		weston --backend headless --socket=test_notes --idle-time=0 >/dev/null 2>&1 & \
		weston_pid=$$!; \
		trap "kill $$weston_pid 2>/dev/null" EXIT; \
		for _ in $$(seq 1 50); do \
			[ -S "$$XDG_RUNTIME_DIR/test_notes" ] && break; \
			sleep 0.1; \
		done; \
		WAYLAND_DISPLAY=test_notes GDK_BACKEND=wayland GSK_RENDERER=cairo \
			python3 -B -m unittest discover -s src -t src -f

type:
	mypy $(PY_SRC) $(PY_TST)

# pylint resolves intra-tree imports off ``src`` on the path. The former
# package layout let pylint add the package parent to sys.path
# automatically; now that ``src`` is the package-less source root,
# PYTHONPATH=src is what lets ``import config``/``import giruntime.ui`` etc. resolve.
# The disable/enable flags are unchanged from the package layout.
lint:
	python3 -B -m pylint $(PY_SRC)
	python3 -B -m pylint $(PY_TST) --disable=too-many-public-methods,protected-access,duplicate-code,too-many-lines,too-few-public-methods

# Build the distributable zipapp. Depends on the compiled GResource so the
# packaged grammar is the same artifact dev/test load. The exclusions
# (``__pycache__``, ``test_*.py``, grammar sources) live in build_pyz.py
# because the zipapp CLI has no filter flag but its API does.
pyz: $(GRES)
	python3 build_pyz.py src $(PYZ) "$(SHEBANG)"

# --- Debian package ----------------------------------------------------------
# The build never runs in the working tree: `git archive HEAD` is exported into
# build/deb/ and dpkg-buildpackage runs there. That keeps debhelper's staging
# litter out of the tree (no debian/files, debian/folio/, debian/*.substvars),
# keeps every artifact inside the gitignored build/ directory rather than `..`,
# and guarantees the package holds COMMITTED content only -- in particular never
# the gitignored in-tree folio.gresource, which Meson compiles for itself.
#
# The version is NOT restated here: debian/changelog owns the Debian version,
# and the upstream version is that string minus the -<revision> suffix. Both are
# recursive (`=`, not `:=`) on purpose, so dpkg-parsechangelog runs only when a
# deb target expands them -- `make test` on a host without dpkg-dev must not
# shell out and fail.
DEB_VERSION   = $(shell dpkg-parsechangelog -l debian/changelog -S Version)
DEB_UPSTREAM  = $(shell dpkg-parsechangelog -l debian/changelog -S Version | sed 's/-[^-]*$$//')
DEB_DIR      := build/deb
DEB_STAGE     = $(DEB_DIR)/folio-$(DEB_UPSTREAM)

# Binary-only (Architecture: all) and unsigned: dpkg-source never runs, so no
# orig tarball is needed. Override to skip the build-dependency check on a host
# that cannot satisfy python3 (>= 3.13):  make deb DEB_BUILD_FLAGS="-us -uc -b -d"
DEB_BUILD_FLAGS ?= -us -uc -b

DEB_TOOLS := git dpkg-parsechangelog dpkg-buildpackage

deb-tools:
	@for tool in $(DEB_TOOLS); do \
		command -v $$tool >/dev/null 2>&1 || { \
			echo "missing: $$tool - apt-get install dpkg-dev git"; exit 1; }; \
	done

# The four files that state the version must agree (README section 7's "one
# release, three dialects" rule, made executable).
version-check:
	python3 -B build-aux/check_version.py

# `deb` depends on neither validation nor `resource`: packaging and validation
# stay orthogonal (`make all` composes them), and the .deb's GResource is the
# one Meson compiles in its build dir, never the dev artifact in the tree.
#
# The package IS HEAD, so an uncommitted change is an error, not a warning: it
# would otherwise be silently absent from the artifact. (`git diff --quiet HEAD`
# does not see untracked files -- which `git archive` omits anyway, so the
# artifact still equals HEAD.)
deb: deb-tools version-check
	@git diff --quiet HEAD || { \
		echo "working tree is dirty - commit or stash: the .deb is built from HEAD"; \
		exit 1; }
	rm -rf $(DEB_DIR)
	mkdir -p $(DEB_DIR)
	git archive --prefix=folio-$(DEB_UPSTREAM)/ --format=tar HEAD | tar -x -C $(DEB_DIR)
	cd $(DEB_STAGE) && dpkg-buildpackage $(DEB_BUILD_FLAGS)
	@ls -1 $(DEB_DIR)/*.deb

deb-lint: deb
	lintian -i -I $(DEB_DIR)/*.deb

deb-clean:
	rm -rf $(DEB_DIR)

clean: deb-clean
	rm -f $(PYZ) $(GRES)

# Upstream build system for the archive-ready .deb (and any other
# distro package). It is an *additional, parallel* path to the zipapp:
# `Makefile` / `build_pyz.py` / `folio.pyz` are untouched and keep working.
#
# Shape: a GTK desktop application written in Python installs its Python
# tree into a PRIVATE directory (`/usr/share/folio`) rather than the shared
# `dist-packages` namespace. Two facts force this and one confirms it:
#
#   * `src/` is a package-less source root (no `__init__.py`); its modules
#     import by bare name, so the installed tree must sit on its own
#     `sys.path` entry — exactly what the zipapp root gives them.
#   * runtime data (the compiled GResource, `css/app.css`, `system_docs/*`)
#     is loaded as in-package resources via `importlib.resources`, so it must
#     keep its committed relative paths inside the installed tree.
#   * the top-level module names (`config`, `models`, `search`, `storage`, …)
#     are generic and would collide in a shared library namespace.
#
# Debhelper auto-detects this file and runs meson + ninja + `meson install`,
# so MESON does the installing — no `debian/*.install` file-copying.

project(
  'folio',
  version: '0.9.2rc1',
  license: 'GPL-3.0-only',
  meson_version: '>= 1.0.0',
)

gnome = import('gnome')
python = import('python')

py = python.find_installation('python3')

# `pkgdatadir_relative` is what the install script needs (it resolves against
# MESON_INSTALL_DESTDIR_PREFIX, so staged installs land in the staging tree);
# `pkgdatadir` is the absolute path baked into the launcher.
pkgdatadir_relative = get_option('datadir') / meson.project_name()
pkgdatadir = get_option('prefix') / pkgdatadir_relative

# The GResource is a *generated* artifact: the same manifest and the same
# `glib-compile-resources` the dev tree uses (`make resource`), compiled here
# into the build dir and installed at its in-package path, where
# `giruntime/ui/_gresource.py` loads it from via `importlib.resources`.
gnome.compile_resources(
  'folio',
  'src/giruntime/ui/folio.gresource.xml',
  source_dir: 'src/giruntime/ui',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir / 'giruntime' / 'ui',
)

# The Python tree. `install_subdir` cannot glob `test_*.py`, so a small install
# script copies `src/` -> pkgdatadir, reusing `build_pyz._included` as the
# single definition of "what ships".
#
# `build_pyz` sits at the source root, so the script is run with PYTHONPATH
# pointing there — which keeps that import a plain top-level one instead of
# `sys.path` surgery inside the script. It goes through `env(1)` rather than
# `add_install_script`'s `env:` keyword because that keyword is too new to rely
# on (it is absent from the meson in Ubuntu 24.04, for one), while `env` is
# available anywhere a Debian package is built.
#
# No quoting is needed around the path: meson spawns install scripts with an
# argv *list*, never a shell, so spaces and shell metacharacters in the source
# root are passed through intact. The one character that cannot survive is `:`,
# which PYTHONPATH itself reads as its entry separator — and PYTHONPATH has no
# escape syntax for it, so a colon in the path silently splits into two bogus
# entries and `import build_pyz` fails. Unfixable, therefore refused up front:
if meson.project_source_root().contains(':')
  error(
    'The source directory path contains ":", which is the PYTHONPATH entry',
    'separator and cannot be escaped. Build from a path without a colon:',
    meson.project_source_root(),
  )
endif

meson.add_install_script(
  find_program('env'),
  'PYTHONPATH=' + meson.project_source_root(),
  'PYTHONDONTWRITEBYTECODE=1',
  py.full_path(),
  files('build-aux' / 'install_python_tree.py'),
  'src',
  pkgdatadir_relative,
)

# The launcher runs the install directory as `__main__` — the zipapp mechanism
# pointed at the unpacked tree — so it needs no app logic of its own.
launcher_conf = configuration_data()
launcher_conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'folio.in',
  output: 'folio',
  configuration: launcher_conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# Desktop integration. The icon is the *single* art source: it is installed to
# `hicolor` for the dock/menu here and bundled into the GResource above for the
# in-window icon — one file, one name (the app id), no second copy.
install_data(
  'data' / 'io.github.rand_byte.Folio.desktop',
  install_dir: get_option('datadir') / 'applications',
)

install_data(
  'data' / 'io.github.rand_byte.Folio.metainfo.xml',
  install_dir: get_option('datadir') / 'metainfo',
)

install_data(
  'src' / 'giruntime' / 'ui' / 'icons' / 'scalable' / 'apps' / 'io.github.rand_byte.Folio.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps',
)

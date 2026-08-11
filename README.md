# Folio

A small, native note-taking app for GNOME. You write your notes in plain
text — marked up with a handful of punctuation characters using a lightweight
format called **AsciiDoc** — and Folio renders them into a clean, readable
page. Everything lives locally on your machine in a single file, with no
account, no cloud, and no telemetry.

> **New to the project?** This page is for *using* Folio. If you want to work
> on the code, start with [`src/README.md`](src/README.md), the developer
> guide.

---

## 1. What Folio is

Folio is a desktop note app built with GTK 4, so it looks and behaves like a
native GNOME application rather than a web page in a window. It is deliberately
focused: a place to write, organise, and re-read text notes, and not much
else.

A few things define it:

- **Plain-text notes in AsciiDoc.** You type lightweight markup — `*bold*`,
  `_italic_`, headings, lists, links, tables, code blocks, quotes, callouts —
  and Folio renders it. Because the source is just readable text, your notes
  stay legible even before they are rendered, and they are never locked inside
  a proprietary binary format.
- **Two ways to look at every note.** A toggle in the toolbar flips the right
  pane between **Source** (the raw markup you edit) and **View** (the rendered
  page you read).
- **A strict, predictable subset.** Folio understands a carefully chosen slice
  of AsciiDoc, plus two additions of its own for attachments. A marker that
  does not form a pair is simply the text you typed, and source Folio cannot
  read is shown as you wrote it and marked where it sits rather than replacing
  the page — so a stray character never costs you the rest of the note.
- **Tag-based organisation.** Instead of folders, notes are filed with tags.
  The sidebar lets you filter by one or more tags, plus built-in "All notes"
  and "Untagged" views, and a search box filters the whole library as you
  type.
- **Attachments.** Each note can carry attached files (up to 10 MB each),
  managed from a panel under the editor.
- **Local and self-contained.** Your entire library is one SQLite database
  under `~/.local/share/folio/`. There is no second place Folio reads from, so
  backing up or moving your notes is just copying one directory.
- **Autosave.** There is no save button. Notes are written to disk as you type,
  including while a line is half-finished.

Folio is intentionally minimal and focused — there is no sync, no mobile app,
and no encryption. The trade-offs that follow from that are spelled out below.

---

## 2. How Folio compares to other GNOME note apps

The GNOME ecosystem already has several note-takers, and plenty of
cross-platform apps run well on GNOME too. Folio is not trying to replace the
heavyweights — it occupies a specific niche: a *native, plain-text, local-only*
note app with *structured* markup. Here is roughly where it sits.

| App | Markup | Storage | Sync | Native GTK | Niche |
| --- | --- | --- | --- | --- | --- |
| **Folio** | AsciiDoc (subset) | One SQLite file | No | Yes (GTK 4) | Structured plain-text notes, offline |
| GNOME Notes (Bijiben) | Rich text | Local files | Optional | Yes | Minimal quick notes |
| Gnote | Rich text + wiki links | Local files | Optional | Yes | Tomboy-style linked notes |
| Notejot / Paper | Rich text (WYSIWYG) | Notebooks | No | Yes | Pretty, simple notebooks |
| Joplin | Markdown | App database | Yes (many backends) | No (Electron) | Full-featured Evernote replacement |
| Obsidian / Logseq | Markdown | Folder of files | Paid / plugins | No (Electron) | Knowledge bases, backlinks, graphs |

### Where Folio has the edge

**It is a genuine native app.** Folio is GTK 4 and ships as a tiny program with
one runtime dependency (PyGObject). It starts fast and stays light, in contrast
to the popular Markdown powerhouses — Joplin, Obsidian, Logseq — which are
Electron apps that bundle a whole browser.

**Your data is plain text you own.** The note *source* is readable AsciiDoc, and
the whole library is a single SQLite file you can copy, version, or inspect.
There is no account to create and nothing phones home.

**The markup is structured, not just decorative.** Compared with the rich-text
GNOME options (GNOME Notes, Notejot, Paper), Folio gives you real document
constructs: section headings, ordered/unordered lists nested up to three deep,
tables, fenced code blocks, block quotes with attribution, five kinds of
callout (note/tip/important/warning/caution), links, and inline images.

**It tells you when your markup is wrong.** Most editors render malformed markup
as best they can and leave you guessing. Folio's parser is strict on purpose: a
broken marker produces a precise error message in the View, so the rendered
page is never quietly wrong.

**Filtering combines tags.** Selecting several tags narrows to notes that carry
*all* of them, which makes a flat, tag-only library scale further than it
sounds.

### Where Folio falls short

Be clear-eyed about the limits before you commit your notes to it:

- **AsciiDoc, and only a subset of it.** Most of the note-taking world speaks
  Markdown. Folio uses AsciiDoc — a fine format, but a smaller ecosystem — and
  it supports only a strict subset, so advanced AsciiDoc features are
  unavailable. It also adds two attachment macros of its own, so a note using
  those is not portable to other AsciiDoc tools. If you want Markdown, Joplin
  or Obsidian are the natural homes.
- **No sync, no mobile, no web, no encryption.** Folio runs on one Linux desktop
  and stays there. If you need your notes on a phone, synced across machines,
  or encrypted at rest, look at Joplin, Standard Notes, or Notesnook.
- **No folders or notebooks.** Organisation is tags only. If you prefer a
  notebook/folder hierarchy, Notejot, Paper, or Gnote offer that.
- **Notes live in a database, not as individual files.** Unlike Obsidian's
  folder of `.md` files, you cannot point another editor at a single note on
  disk. (Backing up the whole library is still trivial — it is one directory.)
- **No export yet.** There is no built-in PDF or HTML export.
- **Not in any distribution's repositories.** There is a `.deb` for
  Debian-family systems and a runnable archive for everything else, but no
  Flatpak on Flathub and no entry in a distro archive — so installing means
  fetching a release yourself and updating the same way (next section).

In short: choose Folio if you want a fast, native, offline GNOME app for
structured plain-text notes you keep on one machine. Choose something else if
sync, mobile access, or Markdown is essential.

---

## 3. Getting started

### What you need

Folio targets a modern GNOME / GTK 4 system. You will need:

- **Python 3.13 or newer**
- **PyGObject 3.50+** (the Python binding for GTK; the package is usually
  `python3-gi`)
- **GTK 4** runtime and typelib
- **GtkSourceView 5** (version 5.4 or newer), which drives the source editor

On **Debian or Ubuntu**, the system packages are:

```sh
sudo apt-get install \
    python3-gi \
    gir1.2-gtk-4.0 libgtk-4-1 \
    gir1.2-gtksource-5 libgtksourceview-5-0
```

Other distributions ship the same libraries under their own names (look for the
GTK 4, GtkSourceView 5, and PyGObject packages). SQLite is part of Python's
standard library, so there is nothing extra to install for storage.

Installing the `.deb` below does **not** need that `apt-get` line — the package
declares these dependencies and `apt` pulls them in. The list matters for the
`folio.pyz` archive and for running from source, neither of which installs
anything on your behalf.

### Get and run Folio

Every release on the
[Releases page](https://github.com/rand-byte/folio/releases) carries two
downloads: a Debian package (`folio_<version>_all.deb`) and a runnable archive
(`folio.pyz`). Which one you want depends on your distribution.

#### Debian 13 (*trixie*) or newer — the `.deb`

This is the smoothest route where it applies. Download the `.deb` from the
Releases page, then:

```sh
sudo apt install ./folio_*_all.deb
```

`apt` installs the runtime dependencies for you and Folio lands as a normal
application: an entry in your app menu, an icon, and a `folio` command with a
man page. Remove it with `sudo apt remove folio`. To upgrade, install the newer
`.deb` the same way — release-candidate versions are ordered *below* the final
release, so `0.9.2~rc1` upgrades cleanly to `0.9.2`.

The package requires **Python 3.13 or newer**, which is why Debian 13 is the
floor. Ubuntu 24.04 LTS ships Python 3.12 and cannot install it; use the
archive below instead (Ubuntu 25.04 and later are fine).

#### Any other distribution — the archive

```sh
# 1. Download folio.pyz from the repository's Releases page, then:
chmod +x folio.pyz
./folio.pyz
```

Assets downloaded from GitHub Releases do not reliably preserve the POSIX
executable bit, so `chmod +x` after downloading is needed once. The runtime
prerequisites above (Python 3.13+, GTK 4, GtkSourceView 5, PyGObject) still
apply — the archive bundles only Folio's own code, not its system
dependencies, and installs nothing.

#### Alternative: build from source

Both published artifacts can be built yourself from a checkout. The archive
needs `glib-compile-resources`, which ships with the GLib development tooling
on any GTK system.

```sh
# 1. Get the source
git clone https://github.com/rand-byte/folio.git
cd folio

# 2. Build the runnable archive
make pyz

# 3. Run it
./folio.pyz
```

A from-source `make pyz` build does not need the `chmod` — the executable bit
is already set locally.

On Debian 13 (*trixie*) or newer you can also build the Debian package — the
same one attached to each release — rather than downloading it:

```sh
make deb        # -> build/deb/folio_<version>_all.deb
sudo apt install ./build/deb/folio_*_all.deb
```

The `.deb` is built from the last commit (`HEAD`), so `make deb` stops if the
working tree has uncommitted changes. See
[`src/README.md`](src/README.md) §7 for the details and the build-host
requirements.

If you would rather run straight from the source tree (handy while trying it
out), the bundled `run` script builds what it needs and launches the app:

```sh
./run
```

### First launch

The first time Folio opens it creates its database and seeds a short **welcome
note** that explains the basics. You can keep it, edit it, or delete it — once
deleted it does not come back.

Your notes are stored in:

```
~/.local/share/folio/notes.db
```

(or under `$XDG_DATA_HOME/folio/` if you have set that variable). To back up or
move your entire library, copy that folder. That is the whole of it — there is
no hidden state anywhere else.

---

## 4. Using Folio efficiently

A handful of habits make Folio noticeably nicer to live in.

**Learn the small markup vocabulary once.** Folio's whole markup language fits
on one page. Press **F1** (or click the **Syntax** button in the toolbar) to
open the built-in help — itself a Folio note written in the very subset it
documents — which walks through every construct with a rendered example and its
source side by side. Twenty minutes there covers everything Folio understands.

**Write in Source, read in View.** Use the toolbar toggle to flip between
editing the raw markup and reading the rendered page. If a note shows an error
in View, switch back to Source: the message tells you which line and which
marker is malformed.

**Let tags do the filing — and pre-tag new notes for free.** Add a `:tags:` line
just under a note's title to file it:

```
= Project kickoff
:tags: work, meetings
```

Then, in the sidebar, click tags to filter. Clicking several tags narrows to
notes carrying *all* of them; the **Untagged** and **All notes** views are
always there as escape hatches. A useful trick: a **new note inherits whatever
tags are currently selected**. Select the `work` tag, click **New**, and your
note already starts with `:tags: work` — so capturing into the right place is a
single click.

**Search filters the whole library live.** The search box at the top filters
across every note as you type, independent of the current tag selection — good
for "where did I write that thing about…" moments.

**Sort the list to match the task.** The note list can be ordered by **Modified**
(what you touched last), **Created**, or **Title**, via the dropdown above it.

**Attach files where they belong.** In the Source view, the attachments panel
below the editor has an **Add file** button. Attachments ride along with the
note (up to 10 MB each) and never clutter the note body.

**Don't think about saving.** Folio autosaves continuously, so there is no save
step and no lost work if you close the window mid-sentence.

**Keyboard shortcuts.** The common actions all have keys, so you rarely need the
mouse:

| Shortcut | Action |
| --- | --- |
| `Ctrl+N` | New note (inherits the selected tags, opens in Source) |
| `Ctrl+F` | Focus the search box |
| `Ctrl+E` | Toggle between View and Source |
| `Delete` | Delete the selected note (when the note list is focused) — asks first |
| `Esc` | Clear the search and close the search box |
| `F1` | Open the syntax help |
| `Ctrl+Q` | Quit |

`Delete` only fires while the note list has focus, so it never eats a character
while you are editing in Source.

**Back up by copying one folder.** Because everything is in
`~/.local/share/folio/`, a backup is `cp -r` (or a sync tool of your choice
pointed at that directory). Restoring is copying it back.

Reach for the right markup for the job: callouts (`NOTE:`, `TIP:`, …) to flag
the important line, tables for small comparisons, code blocks for anything you
want shown verbatim, and headings to give a long note a skimmable structure.
The help page (**F1**) has the exact syntax for each.

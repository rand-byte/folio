.PHONY: all validate test clean build package

package:
	rm -f note_src_arc.zip && \
		zip note_src_arc.zip -r Makefile notes_app tests pyproject.toml

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
test:
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
			python3 -B -m unittest discover -s notes_app -f

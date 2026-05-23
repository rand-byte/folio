.PHONY: all validate test clean build package

package:
	rm -f note_src_arc.zip && \
		zip note_src_arc.zip -r Makefile notes_app tests pyproject.toml

test:
	weston --backend headless --socket test_1 && \
		python3 -B -m unittest discover -s notes_app -f

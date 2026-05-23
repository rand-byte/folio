.PHONY: all validate test clean build package

package:
	rm -f note_src_arc.zip && zip note_src_arc.zip -r notes_src tests pyproject.toml

# Phase 5 review (2026-10-21)

Commit 5aa633c (picture editor) reviewed on Opus 5.5 by the main session plus four
pr-review-toolkit agents: code-reviewer, silent-failure-hunter, pr-test-analyzer and
type-design-analyzer. Each fix below has a regression test in tests/test_editing.py.

## Fixed

| # | Finding | Fix |
|---|---------|-----|
| 1 | Close skipped the prompt after save → undo → a different edit (dirty = step *count*), losing the edit | `Document.mark_clean()` / `modified` compare step identity with the saved tuple |
| 2 | Choosing Save in the close prompt after undoing to the original kept the window open | `save()` returns True when there's nothing new to save |
| 3 | Title `*` came back after saving; a second Ctrl+S wrote "(edited) (2)" | title and save both use `doc.modified` |
| 4 | Render cache keyed on `len(steps)`: undo + add returned a stale (possibly un-redacted) image | cache cleared on add/undo/redo; `steps` is a read-only tuple |
| 5 | Saved PNGs kept the original's tEXt/iTXt chunks (XMP, comments, GPS) | `_pixels_only()` paints into a fresh QImage before writing; docstring corrected |
| 6 | An OS error while saving (read-only folder, missing folder, disk full) escaped as a raw exception; from the close prompt the window closed and edits were lost | `save_copy` raises ConversionError; `closeEvent` fails closed |
| 7 | Started from Dolphin, load errors went to an invisible stderr | `_report()` shows a dialog when stderr isn't a terminal; non-ConversionError per file caught |
| 8 | Bugs in Qt event handlers vanished | `sys.excepthook` logs to ~/.cache/hala/editor.log and shows a dialog |
| 9 | `hala install-menus` imported PySide6 through hala.editing | servicemenus uses `formats.IMAGE_IN` directly |
| 10 | Redo kept a pending crop rectangle drawn on the old geometry | `_refresh()` drops it after every document change |
| 11 | Pictures over 256 MB decoded failed with a generic error, then decoded twice via vips | allocation limit raised to 2 GB; Qt's error string reported; vips only for unsupported formats |
| 12 | vips fallback errors all looked the same | missing vips, timeout and vips stderr reported separately |
| 13 | HEIC/AVIF offered even without a Qt writer; write failures gave no reason | `save_exts()` built from QImageWriter; `errorString()` in the message |
| 14 | Save as: confirmed overwrite silently became "name (2)" | dialog no longer offers overwrite; renames are announced; per-format filters and a default extension |
| 15 | `Step.kind` was a free string; unknown kinds drew nothing | `Kind` Literal (pyright-checked); unknown kinds raise |

## Deliberately not changed

- Closing with Save after a Save as still writes "name (edited).ext" beside the original rather
  than reusing the Save-as folder; the prompt now names the file it will write.
- Step stays one flat record rather than a class per kind (only Canvas builds steps).

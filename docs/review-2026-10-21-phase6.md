# Phase 6 review (2026-10-21)

Commit 545ce85 (shelf, action bubbles, KDE Connect) reviewed on Opus 5.5 by the main session
plus four pr-review-toolkit agents: code-reviewer, silent-failure-hunter, pr-test-analyzer and
type-design-analyzer. Each fix below has a regression test (tests/test_actions.py).

## Fixed

| # | Finding | Fix |
|---|---------|-----|
| 1 | "Move to" on a symlink moved the file it points to (inputs were `resolve()`d); on one disk a moved link became a hard link to its target; a link to a folder failed | actions use absolute, unresolved paths; `_place` and the new `_rename_into` link with `follow_symlinks=False`; copy recreates links as links |
| 2 | A move that copied but couldn't delete the original (read-only card, 0555 subfolder) or couldn't unlink after a hard link was reported as a plain failure, hiding the copy | `PartialMove` carries the placed path; the notification offers Show in folder and says what's left |
| 3 | Moves between bind mounts of one disk (same st_dev, EXDEV) were refused | EXDEV from the rename path falls back to copy-then-delete |
| 4 | Zipping a folder together with a file inside it put the archive and a `.hala-` scratch folder inside the zip | zip's `tidy` drops items inside selected folders |
| 5 | 7z read a file named `@name` as a list file even after `--`, and flattened `./sub/x` paths | zip now written with Python's zipfile |
| 6 | Bubbles ran off short screens (1366x768, 125% scaled 1080p) | `place()` keeps the bubble row whole on any screen; swept in a test |
| 7 | With the row above the ring, a drop on a bubble's label converted to the petal behind it | labels sit on the far side from the ring and count as part of the bubble |
| 8 | Shelf start-up race: the socket was bound after Qt start-up; a second caller gave up after 2 s and its files were lost (stderr only) | socket bound before Qt; callers wait 8 s, then a visible error |
| 9 | `put()` reported success even if the spawned shelf never came up | waits for it to listen or exit; notifies on failure |
| 10 | A cut-off socket message could shelve a parent folder | `decode` drops a final line without its newline |
| 11 | Socket accept errors could spin the notifier; the read could stall the shelf for 2 s | errors logged and the notifier paused; 0.5 s read timeout |
| 12 | `prune()` emptied the shelf when a NAS mount hiccuped (`exists()` is False on any OSError) | only FileNotFoundError from `lstat` removes an item |
| 13 | The shelf's "ignore our own drags" guard never matched (drags start from the shelf, not the list) | compares against both |
| 14 | Undecodable file names crashed notifications after the work was done | names made printable; notify failures fall back to kdialog |
| 15 | "Open KDE Connect" button: missing kdeconnect-app escaped the D-Bus callback | caught; the button is only offered when the app exists |
| 16 | KDE Connect errors showed a command line; "Sent" overclaimed; a failure stopped the batch silently | stderr reason shown; per-file results ("Sending 2 of 5"); worded as queued |
| 17 | `_setup_log()` ran twice for zip, two "w" handlers garbling the log | runs once per process |
| 18 | Crashes in detached commands (run/action/shelf/edit) went to an unseen stderr | `cli.main` logs to ~/.cache/hala/crash.log and shows a kdialog |
| 19 | Action names in three places; two `ANY` markers | CLI choices from `ACTIONS`; tools use `registry.ANY` |
| 20 | `shutil.Error` messages were a huge repr; "None" in some errors | summarised; `strerror or exc` |
| 21 | Tray's Show shelf could freeze the drop zone while waiting | runs `hala action shelf` detached |

## Deliberately not changed

- Copy has no progress bar (moves within a disk are instant; long cross-disk copies just take
  their time before the notification).
- Paths dropped onto the shelf aren't resolved, so one file reached through two symlinked
  folders could show twice. Harmless.

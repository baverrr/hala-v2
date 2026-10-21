# Review 2026-10-20 — phases 1-2 (commit 43d2872): FIXED, uncommitted

Sources: built-in /code-review (high) + pr-review-toolkit (code-reviewer, silent-failure-hunter,
pr-test-analyzer, comment-analyzer, type-design-analyzer). All items below were fixed; full PyGate
gate passes (167 tests).

## Bugs
- pandoc relative images lost → `--resource-path src.parent` (documents.py)
- odd-sized video failed in 4:2:0 encoders → `EVEN_SIZE` scale filter on every transcode
- dotted folder names truncated ("project-v1.2" → "project-v1.zip") → stem_of keeps dir names
- satsuma.desktop Exec had no subcommand → `run %F`; `run` without -t says what files can become
- 16-bit images flattened onto near-black → background 65535 for ushort
- CMYK read as RGBA → colourspace srgb first
- tar.zst skipped the link check (GNU tar already guards; added for consistency)
- 7z: "-name" read as switch, `?`/`*` wildcards and `@listfile` expanded → `-spd` + `./name`
- txt/document menus shown on .py/.json/.log → excludes now computed against the whole MIME db
- `.ass` glob also claims audio/aac (subtitle menu on AAC files); `.ts` also Qt Linguist →
  MimeDb uses top glob weight + family media tie-break
- no-clobber not atomic for folders / no-hardlink filesystems → mkdir / O_EXCL reservation
- duplicated ext normalisation → formats.normalize_ext + split_name

## Silent failures
- Notify D-Bus failure hid every error → log file ~/.cache/satsuma/last-run.log, kdialog fallback,
  "Details" action
- least useful ffmpeg line shown → `_error_detail` skips generic sign-off lines
- cancel during pure-Python steps reported success → check before placement + in tar/gzip loops
- satsuma killed → tools orphaned + scratch left → SIGTERM/SIGHUP cancel (CLI + GLibUnix), stale
  `.satsuma-*` (>24 h) swept
- stdout callback exception leaked the process → killpg in run()
- read-only unpacked folders left scratch → `_remove_tree` with chmod retry
- engine crash tracebacks dropped → logged
- cancelled batch with partial results gave no notification → now notifies
- Open/Show buttons failed silently → caught, falls back to opening the folder
- LibreOffice lock ignored cancel, no timeout → cancellable lock wait, 10-min timeout
- remux fallback swallowed Cancelled / reason → re-raise, logged
- empty pdftoppm output counted as success → error
- nice/ionice not checked by require(); EROFS etc. not translated; Ctrl+C silent on queued jobs

## Types / comments
- Status.is_terminal; Job output set before status; Route.sources frozenset + non-empty check;
  re-pack route no longer lists "folder"; ANY used everywhere; Context frozen, `_proc` removed;
  MimeDb tables private; KIND_OF/kind_of/ARCHIVE_OUT removed; `-j 0` rejected
- Comment fixes: nice level, images_to_pdf docstring, registry docstring, desktop docstring,
  _on_closed reason, archives docstring, worker comment, KILLABLE, svp, start_new_session, etc.

## Not done (judgement calls)
- `Ext` NewType for extensions, frozen Job snapshots for listeners, PR_SET_PDEATHSIG for SIGKILL of
  satsuma itself (preexec_fn is unsafe with threads; the stale sweep covers the leftovers).

## Live desktop check (2026-10-20 ~18:00, under Do Not Disturb)
Driven on the real session bus; buttons pressed with org.kde.NotificationManager.InvokeAction.
Verified: job view (requestView/setPercent/terminate), success + Show in folder, success + Open,
partial failure + Details (log opened), no-target "Can become", SIGTERM mid-conversion (ffmpeg
killed, no scratch/output) and while waiting (exits in 0.1 s), kdialog fallback.
Found and fixed live: (1) a button press didn't end the process — Plasma doesn't close the
notification under DND, so it waited the full 15 min → now closes it and exits; (2) SIGTERM after
the batch finished was ignored → quits; (3) failure text repeated the full input path → name only.
Not drivable from outside: Plasma's own Cancel button on the job entry (cancelRequested comes
from plasmashell); same code path as SIGTERM, which was verified.

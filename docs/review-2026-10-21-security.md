# Security review, 2026-10-21 (after phase 6)

Threat model: Hala runs as the user on files that came from anywhere (downloads, mail,
archives). A hostile file must not read other files into its output, run code, or reach the
network; local sockets must not let other accounts in.

## Fixed

| # | Severity | Issue | Fix |
|---|----------|-------|-----|
| 1 | High | pandoc include directives (`.. include::` in rst, `#+INCLUDE` in org, `\input` in tex) read any file, e.g. `~/.ssh/id_rsa`, into the converted output. Reproduced with `/etc/hostname`. | `documents.pandoc`: pass 1 parses with `--sandbox` to JSON, pass 2 writes from that tree. |
| 2 | High | Markdown/HTML pictures with absolute or `../` paths, raw `<video src>`/`<img src>`, and `cover-image`/`css` metadata were embedded into docx/epub (local file disclosure); URLs were fetched. | `_confine` keeps only pictures inside the document's folder (or extracted media); others become their alt text; file-naming metadata dropped. |
| 3 | Medium | A PostScript (or MVG/MSL) file named `.png`/`.jpg` went to ImageMagick through libvips (vips picks the loader by content), and ImageMagick's policy here is "open", so Ghostscript ran on it. | `images.check_picture`: the bytes must be a real raster/SVG signature (mislabelled pictures still work). Called before vips, zbarimg and Qt. |
| 4 | Medium | LibreOffice profile ran with default macro level and on-request link updates. | `lock_down_profile()` writes macros off, security level 3, block untrusted referer links, no active content / OLE automation, never update links (Calc, Writer). |
| 5 | Low | `python -m hala.cli` (drop zone, shelf) puts the current folder first on the import path; a folder named `hala/` there would run instead of Hala. | `hala.SELF` spawns with `-I`; the installed `/usr/bin/hala` uses `python3 -I`. |
| 6 | Low | ffmpeg/ffprobe could open network protocols named inside media files. ffmpeg 8 already refuses disguised HLS (tested), so this is defence in depth. | `runner.local_inputs` adds `-protocol_whitelist file,pipe` to every input (also the dialog/metadata probes). |
| 7 | Low | Join's concat list: a file name with a line break could add list directives. | Refused with a clear error. |
| 8 | Low | Shelf socket/lock fell back to the shared `/tmp` without `$XDG_RUNTIME_DIR`. | Falls back to a 0700 `~/.cache/hala/run`. |
| 9 | Low | `Exec=` lines quoted the program path without the desktop-entry escapes. | `servicemenus.exec_arg`. |

Tests: `tests/test_security.py` (13), `tests/test_packaging.py`.

## Checked, no change needed

- Archives: 7-Zip 26 drops `../` and refuses links that leave the folder; GNU tar's delayed
  links stop write-through for tar.zst; Python tarfile uses `filter="data"`. All three were
  tested with crafted archives; `_check_links` refuses escaping links afterwards.
- Every external tool gets an absolute, resolved path, so names like `-rf` can't become options.
  `7z` gets `-spd`, and zip is written with zipfile.
- Notifications escape file names (Plasma renders markup); the shelf socket is 0600 and only
  accepts absolute paths.
- Ghostscript runs with `-dSAFER`; exiftool and qpdf get resolved paths only.
- No secrets, private IPs, MACs or Gmail in the tree or history.

## Not fixed (accepted)

- Zip bombs and huge images can fill the disk or use a lot of memory (scratch dir is removed on
  failure; Qt's allocation limit covers the editor).
- ImageMagick's system policy stays "open": Hala no longer hands it anything but BMP, JPEG,
  PNG, GIF and TIFF bytes. Tightening `/etc/ImageMagick-7/policy.xml` would protect other
  apps too.

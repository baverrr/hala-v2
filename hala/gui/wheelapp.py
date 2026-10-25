"""`hala wheel`: the format wheel without dragging to the screen edge.

Dolphin's "Hala…" menu entry opens it for the selected files; the global shortcut (Meta+Shift+H
by default) opens it for the files last copied with Ctrl+C. It's the drop zone's wheel in
click mode, on every screen: it appears where the pointer is, and a click picks. Esc, a click
outside the wheel, or two idle minutes close it.

The clipboard is read with wl-paste, which works without focus (KWin offers the data-control
protocol); Klipper's D-Bus interface is the fallback.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ..i18n import _, ngettext
from . import translate
from .dropzone import launch
from .overlay import Overlay

IDLE_MS = 120_000
URI_LIST = "text/uri-list"

log = logging.getLogger("hala")


def parse_uri_list(text: str, skipped: list[str] | None = None) -> list[Path]:
    """Local files and folders named in a text/uri-list (or Klipper's space-separated URLs);
    anything else, and paths that don't exist, are dropped. URLs of other computers (smb://,
    sftp://, https://...) are added to skipped."""
    found: list[Path] = []
    for token in text.split():
        if token.startswith("#"):
            continue
        url = urlparse(token)
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            if url.scheme and skipped is not None:  # plain words (Klipper's text) aren't URLs
                skipped.append(token)
            continue
        path = Path(unquote(url.path))
        try:
            usable = path.is_absolute() and path.exists()
        except (OSError, ValueError):  # "%00" in the name, or a name too long
            usable = False
        if usable and path not in found:
            found.append(path)
    return found


def clipboard_files(skipped: list[str] | None = None) -> list[Path]:
    """The files last copied (Ctrl+C in Dolphin and most file managers), if any; copied
    files of other computers go to skipped."""
    if shutil.which("wl-paste"):
        try:
            listed = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                                    text=True, timeout=3)
            if listed.returncode != 0:
                log.warning("wl-paste --list-types exited %d: %s", listed.returncode,
                            listed.stderr.strip())
            if URI_LIST in listed.stdout.split():
                pasted = subprocess.run(["wl-paste", "--no-newline", "--type", URI_LIST],
                                        capture_output=True, text=True, timeout=3)
                if pasted.returncode != 0:
                    log.warning("wl-paste exited %d: %s", pasted.returncode,
                                pasted.stderr.strip())
                return parse_uri_list(pasted.stdout, skipped)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("wl-paste failed: %s", exc)
    try:
        from gi.repository import Gio, GLib  # pyright: ignore[reportAttributeAccessIssue]
    except ImportError:
        return []
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        reply = bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
                              "getClipboardContents", None, GLib.VariantType("(s)"),
                              Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error as exc:  # no Klipper or no session bus: no files, then
        log.warning("couldn't read the clipboard through Klipper: %s", exc.message)
        return []
    return parse_uri_list(reply.unpack()[0], skipped)


def main(files: list[Path], from_clipboard: bool = False) -> int:
    remote: list[str] = []
    if from_clipboard:
        files = clipboard_files(remote)
    files = [p.expanduser().absolute() for p in files]
    if not files:
        from ..desktop import notify

        if remote:
            body = ngettext("Only files on this computer can be used ({n} network file "
                            "skipped).", "Only files on this computer can be used ({n} network "
                            "files skipped).", len(remote)).format(n=len(remote))
        elif from_clipboard:
            body = _("Copy files first (Ctrl+C in Dolphin), then press the shortcut again.")
        else:
            body = _("No files were given.")
        notify(_("Nothing to convert"), body, "dialog-information")
        return 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)
    translate.install(app)
    app.setApplicationName("hala")
    app.setDesktopFileName("hala")
    overlays = [Overlay(screen, launch) for screen in app.screens()]

    def placed(chosen: Overlay) -> None:
        # The pointer is on this screen: the others needn't stay dimmed.
        for other in overlays:
            if other is not chosen:
                other.on_close = None
                other.hide()

    def closed(_overlay: Overlay) -> None:
        app.quit()

    for overlay in overlays:
        overlay.on_place, overlay.on_close = placed, closed
        overlay.begin(files, click_mode=True)
    QTimer.singleShot(IDLE_MS, app.quit)
    return app.exec()

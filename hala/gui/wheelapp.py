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

from .dropzone import launch
from .overlay import Overlay

IDLE_MS = 120_000
URI_LIST = "text/uri-list"

log = logging.getLogger("hala")


def parse_uri_list(text: str) -> list[Path]:
    """Local files and folders named in a text/uri-list (or Klipper's space-separated URLs);
    anything else, and paths that don't exist, are dropped."""
    found: list[Path] = []
    for token in text.split():
        if token.startswith("#"):
            continue
        url = urlparse(token)
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            continue
        path = Path(unquote(url.path))
        if path.is_absolute() and path.exists() and path not in found:
            found.append(path)
    return found


def clipboard_files() -> list[Path]:
    """The files last copied (Ctrl+C in Dolphin and most file managers), if any."""
    if shutil.which("wl-paste"):
        try:
            types = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                                   text=True, timeout=3).stdout.split()
            if URI_LIST in types:
                return parse_uri_list(subprocess.run(
                    ["wl-paste", "--no-newline", "--type", URI_LIST], capture_output=True,
                    text=True, timeout=3).stdout)
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
    return parse_uri_list(reply.unpack()[0])


def main(files: list[Path], from_clipboard: bool = False) -> int:
    if from_clipboard:
        files = clipboard_files()
    files = [p.expanduser().absolute() for p in files]
    if not files:
        from ..desktop import notify

        notify("Nothing to convert", "Copy files first (Ctrl+C in Dolphin), then press the "
               "shortcut again." if from_clipboard else "No files were given.",
               "dialog-information")
        return 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)
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

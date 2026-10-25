"""`hala dropzone`: a slim strip on a screen edge. Drag files onto it and the format wheel
opens; let go over a format to convert.

On Wayland an app can't see a drag until it reaches one of its own windows (so there's no
"hold Shift anywhere" shortcut); the strip is that window. Strip and wheel are
layer-shell surfaces, so they sit above other windows at a fixed edge on every screen.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import IO

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPaintEvent,
    QPalette,
    QScreen,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from .. import SELF
from ..i18n import N_, _
from ..servicemenus import exec_arg, localize
from . import layershell as ls
from . import translate
from .geometry import ACTION_PREFIX, TOOL_PREFIX
from .overlay import Overlay, local_files

STRIP_WIDTH, STRIP_HEIGHT = 8, 180
LOCK_FILE = Path.home() / ".cache" / "hala" / "dropzone.lock"
AUTOSTART = Path.home() / ".config" / "autostart" / "hala-dropzone.desktop"
AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=Hala drop zone
Comment=Drag files to the screen edge to convert them
Icon=hala
Exec="{exe}" dropzone --edge {edge}
X-KDE-autostart-phase=2
NoDisplay=true
"""
# Translated into the autostart entry by servicemenus.localize().
AUTOSTART_TEXT = (N_("Hala drop zone"), N_("Drag files to the screen edge to convert them"))


def launch(target: str, files: list[Path]) -> None:
    """Hand the batch to `hala run` (or `hala action`), which owns progress, notifications
    and cancel (and, for a tool, its settings dialog; for an action, its questions)."""
    if target.startswith(ACTION_PREFIX):
        what = ["action", target.removeprefix(ACTION_PREFIX)]
    elif target.startswith(TOOL_PREFIX):
        what = ["run", "--tool", target.removeprefix(TOOL_PREFIX)]
    else:
        what = ["run", "-t", target]
    subprocess.Popen([*SELF, *what, *map(str, files)],
                     stdin=subprocess.DEVNULL, start_new_session=True)


class Strip(QWidget):
    def __init__(self, screen: QScreen, overlay: Overlay, edge: str):
        super().__init__()
        self.overlay, self.edge, self.hot = overlay, edge, False
        self.setWindowTitle(_("Hala drop zone"))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setToolTip(_("Drag files here to convert them"))
        self.resize(STRIP_WIDTH, STRIP_HEIGHT)
        self.winId()
        handle = self.windowHandle()
        anchor = ls.Anchor.LEFT if edge == "left" else ls.Anchor.RIGHT
        if handle is None or not ls.configure(handle, anchors=anchor, layer=ls.Layer.TOP,
                                              screen=screen):
            geo = screen.geometry()
            x = geo.left() if edge == "left" else geo.right() - STRIP_WIDTH + 1
            self.move(x, geo.center().y() - STRIP_HEIGHT // 2)
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)

    def entry_point(self) -> QPointF:
        """Where on the overlay's screen the pointer is when it's on the strip."""
        width, height = self.overlay.width(), self.overlay.height()
        return QPointF(0 if self.edge == "left" else width - 1, height / 2)

    def paintEvent(self, _event: QPaintEvent) -> None:
        colour = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        colour.setAlpha(240 if self.hot else 150)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawRoundedRect(self.rect(), 3, 3)

    def enterEvent(self, _event: object) -> None:
        self.hot = True
        self.update()

    def leaveEvent(self, _event: object) -> None:
        self.hot = False
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not event.mimeData().hasUrls():
            return
        event.acceptProposedAction()
        # The overlay covers the strip, so the compositor hands the rest of the drag to it.
        self.overlay.begin(local_files(event), self.entry_point())

    def dropEvent(self, event: QDropEvent) -> None:
        # Only if the overlay didn't take over the drag: let the user pick by clicking.
        event.acceptProposedAction()
        self.overlay.begin(local_files(event), self.entry_point(), click_mode=True)


class DropZone:
    def __init__(self, app: QApplication, edge: str):
        self.app, self.edge = app, edge
        self.pairs: list[tuple[Strip, Overlay]] = []
        for screen in app.screens():
            self._add_screen(screen)
        app.screenAdded.connect(self._add_screen)
        app.screenRemoved.connect(self._remove_screen)
        self.tray = self._make_tray()

    def _add_screen(self, screen: QScreen) -> None:
        overlay = Overlay(screen, launch)
        strip = Strip(screen, overlay, self.edge)
        strip.show()
        self.pairs.append((strip, overlay))

    def _remove_screen(self, screen: QScreen) -> None:
        for strip, overlay in list(self.pairs):
            if strip.screen() is screen:
                strip.close()
                overlay.close()
                self.pairs.remove((strip, overlay))

    def _make_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(QIcon.fromTheme("hala"), self.app)
        tray.setToolTip(_("Hala: drag files to the screen edge to convert them"))
        menu = QMenu()
        toggle = QAction(_("Hide drop zone"), menu)
        toggle.triggered.connect(lambda: self._toggle(toggle))
        menu.addAction(toggle)
        # Through `hala action shelf`, not shelf.put(): that waits for a shelf to start (and
        # reports it if one doesn't), and the tray mustn't freeze meanwhile.
        menu.addAction(QIcon.fromTheme("view-list-icons"), _("Show shelf"),
                       lambda: launch(ACTION_PREFIX + "shelf", []))
        menu.addAction(QIcon.fromTheme("hala"), _("Wheel for copied files"),
                       lambda: subprocess.Popen([*SELF, "wheel", "--clipboard"],
                                                stdin=subprocess.DEVNULL,
                                                start_new_session=True))
        menu.addSeparator()
        menu.addAction(_("Quit"), self.app.quit)
        tray.setContextMenu(menu)
        tray.show()
        self._menu = menu  # QSystemTrayIcon doesn't own it
        return tray

    def _toggle(self, action: QAction) -> None:
        visible = not self.pairs[0][0].isVisible() if self.pairs else True
        for strip, _overlay in self.pairs:
            strip.setVisible(visible)
        action.setText(_("Hide drop zone") if visible else _("Show drop zone"))


def _single_instance() -> IO[str] | None:
    """Hold a lock for as long as this process lives; None if another drop zone has it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")  # noqa: SIM115  # held until the process exits
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def set_autostart(enabled: bool, exe: str, edge: str) -> Path:
    if enabled:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(localize(AUTOSTART_ENTRY.format(exe=exec_arg(exe), edge=edge)))
    else:
        AUTOSTART.unlink(missing_ok=True)
    return AUTOSTART


# main()'s exit code when another drop zone already has the lock.
ALREADY_RUNNING = 3


def main(edge: str = "right") -> int:
    lock = _single_instance()
    if lock is None:
        print(_("the drop zone is already running"), file=sys.stderr)
        return ALREADY_RUNNING
    # HALA_DEBUG=1 also logs every hover change, for checking the wheel follows a drag.
    logging.basicConfig(filename=LOCK_FILE.parent / "dropzone.log", filemode="w",
                        format="%(asctime)s %(levelname)s %(message)s",
                        level=logging.DEBUG if os.environ.get("HALA_DEBUG") else logging.INFO)
    app = QApplication(sys.argv[:1])
    translate.install(app)
    app.setApplicationName("hala")
    app.setDesktopFileName("hala")
    app.setQuitOnLastWindowClosed(False)
    zone = DropZone(app, edge)
    try:
        return app.exec()
    finally:
        del zone
        lock.close()

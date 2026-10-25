"""`hala shelf`: a small floating shelf that holds files until you drag them somewhere else.

Drop files on it (or send them with the wheel's Shelf bubble, the Dolphin menu, the drop
zone's tray menu, `hala action shelf FILES` or `hala shelf FILES`); drag one, a selection, or
the whole lot back out to any app. Files stay on the shelf after a drag (a target that moved
one makes it drop off). Only one shelf runs: later calls hand their files to it over a Unix
socket in $XDG_RUNTIME_DIR (without one, a private ~/.cache/hala/run, never the shared /tmp).
Its log: ~/.cache/hala/shelf.log.
"""

from __future__ import annotations

import fcntl
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import IO

from PySide6.QtCore import QFileInfo, QMimeData, QPoint, QSize, QSocketNotifier, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import SELF
from ..i18n import _, ngettext
from . import layershell as ls
from . import translate
from .preview import human_size, total_size

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache" / "hala" / "run")
SOCKET = RUNTIME / "hala-shelf.sock"
LOCK = RUNTIME / "hala-shelf.lock"
WIDTH = 300
ROW = 40
MAX_ROWS = 7
MARGINS = (0, 64, 16, 0)  # left, top, right, bottom: below the panel, off the screen edge
PRUNE_MS = 3000  # how often files that were moved or deleted elsewhere are dropped
ROLE_PATH = Qt.ItemDataRole.UserRole
START_WAIT_S = 8.0  # a cold Qt start on a busy desktop can take a few seconds
LOG = Path.home() / ".cache" / "hala" / "shelf.log"

log = logging.getLogger("hala.shelf")


# --- handing files to the running shelf -----------------------------------------------------


def encode(files: Iterable[Path]) -> bytes:
    """One absolute path per line, each ending in a newline (so a cut-off message is
    recognisable). A path containing a newline can't be sent this way and is skipped."""
    lines = []
    for p in files:
        if "\n" in str(p):
            log.warning("can't shelve %r: its name contains a line break", str(p))
        else:
            lines.append(f"{p}\n")
    return "".join(lines).encode(errors="surrogateescape")


def decode(data: bytes) -> list[Path]:
    text = data.decode(errors="surrogateescape")
    if not text.endswith("\n"):
        # Cut off mid-path: "/home/me/Pic" must not shelve a different (or parent) path.
        text = text[:text.rfind("\n") + 1]
    return [Path(line) for line in text.splitlines() if line.startswith("/")]


def send(files: list[Path], path: Path = SOCKET) -> bool:
    """Give files to a running shelf (and bring it up); False if none is running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(str(path))
            conn.sendall(encode(files))
        return True
    except OSError:
        return False


def put(files: list[Path]) -> int:
    """The wheel's, Dolphin's and the tray's way in (via `hala action shelf`): hand the files
    to the running shelf, or start one holding them - and say so if it never comes up."""
    from ..desktop import notify

    files = [Path(os.path.abspath(p.expanduser())) for p in files]
    if send(files):
        return 0
    try:
        proc = subprocess.Popen([*SELF, "shelf", *map(str, files)],
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        notify(_("Couldn't open the shelf"), str(exc), "dialog-warning", problem=True)
        return 1
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:  # 0: it handed the files to a shelf that won a race
            if code == 0:
                return 0
            break
        if send([]):  # listening: it already holds the files from its command line
            return 0
        time.sleep(0.1)
    notify(_("Couldn't open the shelf"), _("It didn't start. Details: {log}").format(log=LOG),
           "dialog-warning", problem=True)
    return 1


# --- the window -----------------------------------------------------------------------------


class ShelfList(QListWidget):
    """The files. Drags out carry their URLs; drops from other apps add files."""

    def __init__(self, shelf: Shelf):
        super().__init__()
        self.shelf = shelf
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setIconSize(QSize(28, 28))
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setStyleSheet("QListWidget { background: transparent; }")

    def paths(self, items: Iterable[QListWidgetItem] | None = None) -> list[Path]:
        chosen = items if items is not None else (self.item(i) for i in range(self.count()))
        return [Path(item.data(ROLE_PATH)) for item in chosen]

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:  # type: ignore[override]
        return urls_data(self.paths(items))

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        items = self.selectedItems()
        if items:
            self.shelf.drag_out(self.paths(items))

    # Drops land on the shelf as a whole (the list would otherwise insert or reorder rows).
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self.shelf.dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self.shelf.dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.shelf.dropEvent(event)

    def _menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
        chosen = self.paths(self.selectedItems())
        menu = QMenu(self)
        menu.addAction(QIcon.fromTheme("document-open"), "Open",
                       lambda: [QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                                for p in chosen])
        menu.addAction(QIcon.fromTheme("folder-open"), _("Show in folder"),
                       lambda: show_in_folder(chosen))
        menu.addSeparator()
        menu.addAction(QIcon.fromTheme("list-remove"), _("Remove from shelf"),
                       lambda: self.shelf.remove(chosen))
        menu.exec(self.viewport().mapToGlobal(pos))


class DragAll(QToolButton):
    """Grab this to drag every file on the shelf at once."""

    def __init__(self, shelf: Shelf):
        super().__init__()
        self.shelf = shelf
        self.setIcon(QIcon.fromTheme("transform-move"))
        self.setToolTip(_("Drag all the files"))
        self.setAutoRaise(True)
        self._pressed: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._pressed = event.position().toPoint()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pressed is None:
            return
        if (event.position().toPoint() - self._pressed).manhattanLength() \
                >= QApplication.startDragDistance():
            self._pressed = None
            self.shelf.drag_out(self.shelf.files())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._pressed = None


class Shelf(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_("Hala shelf"))
        self.setWindowIcon(QIcon.fromTheme("hala"))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.icons = QFileIconProvider()
        self.hot = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 10)
        header = QHBoxLayout()
        self.title = QLabel()
        header.addWidget(self.title, 1)
        self.drag_all = DragAll(self)
        header.addWidget(self.drag_all)
        for icon, tip, slot in (("edit-clear-all", _("Clear the shelf"), self.clear),
                                ("window-close", _("Close the shelf"), self.close)):
            button = QToolButton()
            button.setIcon(QIcon.fromTheme(icon))
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.clicked.connect(slot)
            header.addWidget(button)
        layout.addLayout(header)
        self.list = ShelfList(self)
        layout.addWidget(self.list)
        self.hint = QLabel(_("Drop files here to hold them,\nthen drag them anywhere."))
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setEnabled(False)
        layout.addWidget(self.hint)

        self._prune_timer = QTimer(self, interval=PRUNE_MS)
        self._prune_timer.timeout.connect(self.prune)
        self._prune_timer.start()
        self._place()
        self._refresh()

    def _place(self) -> None:
        self.setFixedWidth(WIDTH)
        self.winId()
        handle = self.windowHandle()
        if handle is None or not ls.configure(handle, anchors=ls.Anchor.TOP | ls.Anchor.RIGHT,
                                              layer=ls.Layer.TOP, margins=MARGINS):
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
            screen = QApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(geo.right() - WIDTH - MARGINS[2], geo.top() + MARGINS[1])

    # --- contents ---------------------------------------------------------------------------

    def files(self) -> list[Path]:
        return self.list.paths()

    def add(self, paths: Iterable[Path]) -> int:
        """Add files and folders not already here; returns how many were new."""
        have = set(self.files())
        added = 0
        for path in paths:
            if path in have:
                continue
            if not os.path.lexists(path):
                log.warning("not shelved, it doesn't exist: %s", path)
                continue
            item = QListWidgetItem(self.icons.icon(QFileInfo(str(path))), path.name)
            item.setData(ROLE_PATH, str(path))
            item.setToolTip(str(path))
            self.list.addItem(item)
            have.add(path)
            added += 1
        self._refresh()
        return added

    def remove(self, paths: Iterable[Path]) -> None:
        gone = {str(p) for p in paths}
        for row in reversed(range(self.list.count())):
            if self.list.item(row).data(ROLE_PATH) in gone:
                self.list.takeItem(row)
        self._refresh()

    def clear(self) -> None:
        self.list.clear()
        self._refresh()

    def prune(self) -> None:
        """Drop files that no longer exist (moved by a drag target, deleted elsewhere). Only
        "not found" counts: a NAS that's briefly unreachable mustn't empty the shelf."""
        missing = []
        for path in self.files():
            try:
                os.lstat(path)
            except FileNotFoundError:
                missing.append(path)
            except OSError:
                pass
        if missing:
            self.remove(missing)

    def _refresh(self) -> None:
        files = self.files()
        count = len(files)
        size = total_size(files) if files else None
        detail = f" · {human_size(size)}" if size is not None else ""
        items = ngettext("{n} item", "{n} items", count).format(n=count)
        self.title.setText(f"<b>{_('Shelf')}</b>" + (f" · {items}{detail}" if count else ""))
        self.list.setVisible(bool(count))
        self.hint.setVisible(not count)
        self.drag_all.setEnabled(bool(count))
        self.list.setFixedHeight(ROW * min(max(count, 1), MAX_ROWS) + 4)
        self.adjustSize()

    # --- dragging out -----------------------------------------------------------------------

    def drag_out(self, paths: list[Path]) -> None:
        paths = [p for p in paths if p.exists()]
        if not paths:
            self.prune()
            return
        drag = QDrag(self)
        drag.setMimeData(urls_data(paths))
        icon = self.icons.icon(QFileInfo(str(paths[0])))
        drag.setPixmap(icon.pixmap(48, 48))
        # Copy unless the target asks otherwise; the files stay on the shelf either way
        # (one a target moved away is dropped by prune()).
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
                  | Qt.DropAction.LinkAction, Qt.DropAction.CopyAction)
        self.prune()

    # --- dropping in ------------------------------------------------------------------------

    def _wants(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        # Our own items dragged back onto the shelf aren't news (drags start from the shelf).
        return event.mimeData().hasUrls() and event.source() not in (self, self.list)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._wants(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.hot = True
            self.update()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._wants(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dragLeaveEvent(self, _event: object) -> None:
        self.hot = False
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:
        self.hot = False
        if self._wants(event):
            # Copy: the shelf only remembers where files are; the source must not delete them.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.add(Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile())
        self.update()

    # --- look -------------------------------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        fill = QColor(palette.color(QPalette.ColorRole.Window))
        fill.setAlpha(238)
        accent = palette.color(QPalette.ColorRole.Highlight)
        edge = QColor(accent if self.hot else palette.color(QPalette.ColorRole.Mid))
        p.setPen(edge)
        p.setBrush(fill)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)


def urls_data(paths: list[Path]) -> QMimeData:
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return data


def show_in_folder(paths: list[Path]) -> None:
    try:
        from gi.repository import Gio, GLib  # pyright: ignore[reportAttributeAccessIssue]

        from ..desktop import FILE_MANAGER

        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        bus.call_sync(*FILE_MANAGER, "ShowItems",
                      GLib.Variant("(ass)", ([p.as_uri() for p in paths], "")),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
    except Exception as exc:  # no gi, no bus, no file manager: open the folder instead
        log.warning("Show in folder via D-Bus failed: %s", exc)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0].parent))):
            log.error("couldn't open %s either", paths[0].parent)


# --- serving --------------------------------------------------------------------------------


def listen(path: Path = SOCKET) -> socket.socket:
    """Bind the shelf's socket. Done before Qt starts, so callers can connect at once: the
    backlog holds them until the window exists and the notifier picks them up."""
    path.unlink(missing_ok=True)  # left by a shelf that was killed; we hold the lock
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_mask = os.umask(0o177)  # 0600 from the start
    try:
        sock.bind(str(path))
    finally:
        os.umask(old_mask)
    sock.listen(16)
    sock.setblocking(False)
    return sock


class Server:
    """Hands what arrives on the socket to the shelf."""

    def __init__(self, shelf: Shelf, sock: socket.socket, path: Path = SOCKET):
        self.shelf, self.sock, self.path = shelf, sock, path
        self.notifier = QSocketNotifier(sock.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self._accept)

    def _accept(self) -> None:
        try:
            conn, _addr = self.sock.accept()
        except BlockingIOError:
            return
        except OSError as exc:
            # Out of file handles and the like: the notifier would fire again at once, so
            # pause it rather than spin.
            log.error("shelf socket: %s", exc)
            self.notifier.setEnabled(False)
            QTimer.singleShot(1000, lambda: self.notifier.setEnabled(True))
            return
        chunks = []
        with conn:
            conn.settimeout(0.5)  # local senders write a few hundred bytes and hang up
            try:
                while chunk := conn.recv(65536):
                    chunks.append(chunk)
            except OSError as exc:
                log.warning("shelf socket: a sender stopped mid-way (%s)", exc)
        self.shelf.add(decode(b"".join(chunks)))
        self.shelf.show()
        self.shelf.raise_()

    def close(self) -> None:
        self.notifier.setEnabled(False)
        self.sock.close()
        self.path.unlink(missing_ok=True)


def _lock() -> IO[str] | None:
    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(LOCK, "w")  # noqa: SIM115  # held until the process exits
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def main(files: list[Path]) -> int:
    from ..runner import ConversionError

    _setup_log()
    files = [Path(os.path.abspath(p.expanduser())) for p in files]
    if send(files):
        return 0
    lock = _lock()
    if lock is None:
        # Another shelf is starting right now: give it a moment to listen, then hand over.
        deadline = time.monotonic() + START_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if send(files):
                return 0
        raise ConversionError(_("the shelf is starting but isn't answering; try again"))
    sock = listen()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)
    translate.install(app)
    app.setApplicationName("hala")
    app.setDesktopFileName("hala")
    shelf = Shelf()
    server = Server(shelf, sock)
    shelf.add(files)
    shelf.show()
    log.info("shelf open with %d item(s)", len(shelf.files()))
    try:
        return app.exec()
    finally:
        server.close()
        lock.close()


def _setup_log() -> None:
    if log.handlers:
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(LOG, mode="w")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

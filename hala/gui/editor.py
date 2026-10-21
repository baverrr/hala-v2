"""`hala edit`: a small editor for pictures and PDFs - crop, rotate, annotate, redact.

The window only collects gestures; every change is a Step on a Document (hala/editing.py), so
undo is trivial and what you see is exactly what gets saved. Saving never touches the original.
A PDF is edited page by page (each page its own Document); crop, rotate and flip are left out
for PDFs, as they'd change the page size, and edited pages are flattened when saved.
"""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QIcon,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from .. import formats as f
from ..editing import (
    DEFAULT_COLOR,
    Document,
    Kind,
    PdfPages,
    Step,
    apply,
    load_image,
    output_ext,
    save_copy,
    save_exts,
    save_pdf_copy,
)
from ..engines.pdf import PasswordNeeded, WrongPassword
from ..runner import ConversionError

LOG = Path.home() / ".cache" / "hala" / "editor.log"

# (key, label, theme icon, hint)
TOOLS: list[tuple[Kind, str, str, str]] = [
    ("crop", "Crop", "transform-crop", "Drag the area to keep, then press Enter"),
    ("arrow", "Arrow", "draw-arrow", "Drag from the tail to the point"),
    ("line", "Line", "draw-line", "Drag"),
    ("rect", "Rectangle", "draw-rectangle", "Drag"),
    ("ellipse", "Ellipse", "draw-ellipse", "Drag"),
    ("pen", "Pen", "draw-freehand", "Draw freely"),
    ("text", "Text", "draw-text", "Click where the text should start"),
    ("black", "Redact (black box)", "edit-clear",
     "Drag over what to hide; it is blacked out for good"),
    ("pixelate", "Redact (pixelate)", "view-grid",
     "Drag over what to hide; it is pixelated for good"),
]
SHAPES: set[Kind] = {"rect", "ellipse", "black", "pixelate", "crop"}
ASPECTS = {"Free": None, "1:1": 1.0, "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9, "3:4": 3 / 4,
           "2:3": 2 / 3, "9:16": 9 / 16}
MARGIN = 12
MIN_DRAG = 3  # image pixels; a click smaller than this isn't a shape


class Canvas(QWidget):
    changed = Signal()

    def __init__(self, doc: Document):
        super().__init__()
        self.doc = doc
        self.tool: Kind = "arrow"
        self.color = QColor(DEFAULT_COLOR)
        self.width_px = 6
        self.text_px = 32
        self.aspect: float | None = None
        self._start: QPointF | None = None
        self._now: QPointF | None = None
        self._path: list[QPointF] = []
        self.crop_rect: QRect | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(480, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # --- coordinates ------------------------------------------------------------------------

    def _image(self) -> QImage:
        return self.doc.render()

    def _scale_and_origin(self) -> tuple[float, QPointF]:
        img = self._image()
        avail_w, avail_h = self.width() - 2 * MARGIN, self.height() - 2 * MARGIN
        scale = min(avail_w / img.width(), avail_h / img.height())
        scale = max(scale, 0.01)
        origin = QPointF((self.width() - img.width() * scale) / 2,
                         (self.height() - img.height() * scale) / 2)
        return scale, origin

    def to_image(self, pos: QPointF) -> QPointF:
        scale, origin = self._scale_and_origin()
        img = self._image()
        x = min(max((pos.x() - origin.x()) / scale, 0.0), float(img.width()))
        y = min(max((pos.y() - origin.y()) / scale, 0.0), float(img.height()))
        return QPointF(x, y)

    # --- the pending shape ------------------------------------------------------------------

    def _pending_rect(self) -> QRect | None:
        if self._start is None or self._now is None:
            return None
        a, b = self._start, self._now
        if self.tool == "crop" and self.aspect:
            dx, dy = b.x() - a.x(), b.y() - a.y()
            width = max(abs(dx), abs(dy) * self.aspect)
            height = width / self.aspect
            b = QPointF(a.x() + width * (1 if dx >= 0 else -1),
                        a.y() + height * (1 if dy >= 0 else -1))
            img = self._image()
            if not (0 <= b.x() <= img.width() and 0 <= b.y() <= img.height()):
                # Keep the ratio but shrink to fit inside the picture.
                room_x = (img.width() - a.x()) if dx >= 0 else a.x()
                room_y = (img.height() - a.y()) if dy >= 0 else a.y()
                width = min(room_x, room_y * self.aspect)
                height = width / self.aspect
                b = QPointF(a.x() + width * (1 if dx >= 0 else -1),
                            a.y() + height * (1 if dy >= 0 else -1))
        return QRectF(a, b).normalized().toAlignedRect().intersected(self._image().rect())

    def _pending_step(self) -> Step | None:
        kind = self.tool
        style = {"color": self.color, "width": float(self.width_px)}
        if kind in SHAPES:
            rect = self._pending_rect()
            if rect is None or rect.width() < MIN_DRAG or rect.height() < MIN_DRAG:
                return None
            return Step(kind, rect=rect, **style)
        if kind in ("arrow", "line") and self._start and self._now:
            if (self._now - self._start).manhattanLength() < MIN_DRAG:
                return None
            return Step(kind, points=(self._start, self._now), **style)
        if kind == "pen" and self._path:
            return Step("pen", points=tuple(self._path), **style)
        return None

    # --- painting ---------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        palette = self.palette()
        p.fillRect(self.rect(), palette.window())
        image = self._image()
        scale, origin = self._scale_and_origin()
        target = QRectF(origin, QSize(round(image.width() * scale), round(image.height() * scale)))
        self._checkerboard(p, target)
        shown = image
        pending = self._pending_step()
        if pending is not None and pending.kind != "crop":
            shown = apply(image.copy(), pending)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(target, shown)
        p.setPen(QPen(palette.mid().color(), 1))
        p.drawRect(target.adjusted(-0.5, -0.5, 0.5, 0.5))
        crop = pending.rect if pending is not None and pending.kind == "crop" else self.crop_rect
        if self.tool == "crop" and crop is not None:
            self._paint_crop(p, crop, target, scale, origin)
        p.end()

    @staticmethod
    def _checkerboard(p: QPainter, target: QRectF) -> None:
        # Behind see-through pictures, so transparency is visible.
        size = 10
        p.save()
        p.setClipRect(target)
        for row in range(int(target.height() // size) + 1):
            for col in range(int(target.width() // size) + 1):
                shade = QColor(200, 200, 200) if (row + col) % 2 else QColor(255, 255, 255)
                p.fillRect(QRectF(target.x() + col * size, target.y() + row * size, size, size),
                           shade)
        p.restore()

    @staticmethod
    def _paint_crop(p: QPainter, crop: QRect, target: QRectF, scale: float,
                    origin: QPointF) -> None:
        box = QRectF(origin.x() + crop.x() * scale, origin.y() + crop.y() * scale,
                     crop.width() * scale, crop.height() * scale)
        dim = QColor(0, 0, 0, 140)
        p.fillRect(QRectF(target.left(), target.top(), target.width(),
                          box.top() - target.top()), dim)
        p.fillRect(QRectF(target.left(), box.bottom(), target.width(),
                          target.bottom() - box.bottom()), dim)
        p.fillRect(QRectF(target.left(), box.top(), box.left() - target.left(), box.height()), dim)
        p.fillRect(QRectF(box.right(), box.top(), target.right() - box.right(), box.height()), dim)
        p.setPen(QPen(QColor("white"), 2, Qt.PenStyle.DashLine))
        p.drawRect(box)
        p.setPen(QColor("white"))
        label = f"{crop.width()} x {crop.height()}"
        metrics = p.fontMetrics()
        p.fillRect(QRectF(box.left(), box.bottom() + 4, metrics.horizontalAdvance(label) + 10,
                          metrics.height() + 4), QColor(0, 0, 0, 170))
        p.drawText(QPointF(box.left() + 5, box.bottom() + 4 + metrics.ascent() + 2), label)

    # --- mouse ------------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        pos = self.to_image(event.position())
        if self.tool == "text":
            self._add_text(pos)
            return
        self._start = self._now = pos
        self._path = [pos] if self.tool == "pen" else []
        if self.tool == "crop":
            self.crop_rect = None
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is None:
            return
        self._now = self.to_image(event.position())
        if self.tool == "pen":
            self._path.append(self._now)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._now = self.to_image(event.position())
        step = self._pending_step()
        if self.tool == "pen" and not step and self._path:
            step = Step("pen", points=tuple(self._path), color=self.color,
                        width=float(self.width_px))  # a single dot
        self._start = self._now = None
        self._path = []
        if step is not None:
            if step.kind == "crop":
                self.crop_rect = step.rect  # wait for Enter
            else:
                self.doc.add(step)
                self.changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.tool == "crop" and self.crop_rect is not None:
            self.apply_crop()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.tool == "crop" and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.apply_crop()
        elif event.key() == Qt.Key.Key_Escape and self.crop_rect is not None:
            self.crop_rect = None
            self.update()
        else:
            super().keyPressEvent(event)

    # --- actions ----------------------------------------------------------------------------

    def apply_crop(self) -> None:
        if self.crop_rect is None:
            return
        self.doc.add(Step("crop", rect=self.crop_rect))
        self.crop_rect = None
        self.changed.emit()
        self.update()

    def _add_text(self, pos: QPointF) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Add text", "Text:")
        if ok and text.strip():
            self.doc.add(Step("text", points=(pos,), text=text, color=self.color,
                              size=self.text_px))
            self.changed.emit()
            self.update()

    def transform(self, kind: Kind, amount: int) -> None:
        self.crop_rect = None
        self.doc.add(Step(kind, amount=amount))
        self.changed.emit()
        self.update()

    def set_tool(self, tool: Kind) -> None:
        self.tool = tool
        self.crop_rect = None
        self.setCursor(Qt.CursorShape.IBeamCursor if tool == "text"
                       else Qt.CursorShape.CrossCursor)
        self.update()


class Editor(QMainWindow):
    def __init__(self, path: Path, password: str | None = None):
        super().__init__()
        self.path = path
        self.pdf = PdfPages(path, password) if f.ext_of(path) == "pdf" else None
        self.page_index = 0
        try:
            self.doc = self.pdf.page(0) if self.pdf else Document(load_image(path))
        except BaseException:
            if self.pdf:
                self.pdf.close()  # its scratch folder may hold an unlocked copy
            raise
        self.canvas = Canvas(self.doc)
        self.setCentralWidget(self.canvas)
        self.setWindowIcon(QIcon.fromTheme("hala"))
        self.saved_as: Path | None = None
        self._build_toolbars()
        self.canvas.changed.connect(self._refresh)
        self.canvas.set_tool("arrow")
        self._refresh()
        self.resize(1000, 720)

    # --- UI ---------------------------------------------------------------------------------

    def _action(self, text: str, icon: str, slot: object, shortcut: str = "") -> QAction:
        action = QAction(QIcon.fromTheme(icon), text, self)
        action.triggered.connect(slot)  # type: ignore[arg-type]
        if shortcut:
            action.setShortcut(shortcut)
        action.setToolTip(f"{text} ({shortcut})" if shortcut else text)
        return action

    def _build_toolbars(self) -> None:
        top = QToolBar("Tools")
        top.setMovable(False)
        top.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(top)
        group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for key, label, icon, hint in TOOLS:
            if key == "crop" and self.pdf:  # a PDF page keeps its size
                continue
            action = QAction(QIcon.fromTheme(icon), label, self)
            action.setCheckable(True)
            action.setToolTip(f"{label} - {hint}")
            action.triggered.connect(self._tool_slot(key))
            group.addAction(action)
            self.tool_actions[key] = action
            top.addAction(action)
        self.tool_actions["arrow"].setChecked(True)

        bar = QToolBar("Options")
        bar.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(bar)
        self.color_button = QToolButton()
        self.color_button.setToolTip("Colour for shapes and text")
        self.color_button.clicked.connect(self._pick_color)
        self._paint_color_button()
        bar.addWidget(self.color_button)
        self.width_box = QSpinBox()
        self.width_box.setRange(1, 60)
        self.width_box.setValue(self.canvas.width_px)
        self.width_box.setPrefix("Line ")
        self.width_box.setSuffix(" px")
        self.width_box.valueChanged.connect(lambda v: setattr(self.canvas, "width_px", v))
        bar.addWidget(self.width_box)
        self.text_box = QSpinBox()
        self.text_box.setRange(8, 400)
        self.text_box.setValue(self.canvas.text_px)
        self.text_box.setPrefix("Text ")
        self.text_box.setSuffix(" px")
        self.text_box.valueChanged.connect(lambda v: setattr(self.canvas, "text_px", v))
        bar.addWidget(self.text_box)
        self.aspect_box = QComboBox()
        self.aspect_box.addItems(list(ASPECTS))
        self.aspect_box.setToolTip("Crop shape")
        self.aspect_box.currentTextChanged.connect(self._pick_aspect)
        self.aspect_action = bar.addWidget(self.aspect_box)
        bar.addSeparator()
        self.crop_apply = self._action("Apply crop", "dialog-ok-apply", self.canvas.apply_crop)
        bar.addAction(self.crop_apply)
        bar.addSeparator()
        if self.pdf is None:
            bar.addAction(self._action("Rotate left", "object-rotate-left",
                                       lambda: self.canvas.transform("rotate", 270), "Ctrl+["))
            bar.addAction(self._action("Rotate right", "object-rotate-right",
                                       lambda: self.canvas.transform("rotate", 90), "Ctrl+]"))
            bar.addAction(self._action("Flip horizontally", "object-flip-horizontal",
                                       lambda: self.canvas.transform("flip", 1)))
            bar.addAction(self._action("Flip vertically", "object-flip-vertical",
                                       lambda: self.canvas.transform("flip", 2)))
            bar.addSeparator()
        self.undo_action = self._action("Undo", "edit-undo", self._undo, "Ctrl+Z")
        self.redo_action = self._action("Redo", "edit-redo", self._redo, "Ctrl+Shift+Z")
        bar.addAction(self.undo_action)
        bar.addAction(self.redo_action)
        bar.addSeparator()
        bar.addAction(self._action("Save copy", "document-save", self.save, "Ctrl+S"))
        bar.addAction(self._action("Save as…", "document-save-as", self.save_as,
                                   "Ctrl+Shift+S"))
        bar.addAction(self._action("Copy", "edit-copy", self.copy, "Ctrl+C"))
        if self.pdf is not None:
            bar.addSeparator()
            self.prev_page = self._action("Previous page", "go-previous",
                                          lambda: self.go_to(self.page_index - 1), "PgUp")
            self.next_page = self._action("Next page", "go-next",
                                          lambda: self.go_to(self.page_index + 1), "PgDown")
            self.page_label = QLabel()
            bar.addAction(self.prev_page)
            bar.addWidget(self.page_label)
            bar.addAction(self.next_page)
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)

    def _paint_color_button(self) -> None:
        swatch = QPixmap(18, 18)
        swatch.fill(self.canvas.color)
        self.color_button.setIcon(QIcon(swatch))
        self.color_button.setText("Colour")
        self.color_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    def _tool_slot(self, key: Kind) -> Callable[[], None]:
        return lambda: self._pick_tool(key)

    def _pick_tool(self, key: Kind) -> None:
        self.canvas.set_tool(key)
        hint = next(h for k, _l, _i, h in TOOLS if k == key)
        self.statusBar().showMessage(hint, 6000)
        self._refresh()

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self.canvas.color, self, "Colour")
        if color.isValid():
            self.canvas.color = color
            self._paint_color_button()

    def _pick_aspect(self, name: str) -> None:
        self.canvas.aspect = ASPECTS[name]

    # --- state ------------------------------------------------------------------------------

    def _modified(self) -> bool:
        return self.pdf.modified if self.pdf else self.doc.modified

    def _edited(self) -> bool:
        return self.pdf.edited if self.pdf else self.doc.edited

    def _mark_clean(self) -> None:
        (self.pdf or self.doc).mark_clean()

    def go_to(self, index: int) -> None:
        """Show another page of a PDF (each keeps its own edits and undo history)."""
        if self.pdf is None or not 0 <= index < self.pdf.count:
            return
        try:
            doc = self.pdf.page(index)
        except ConversionError as exc:
            QMessageBox.warning(self, "Couldn't show the page", str(exc))
            return
        self.pdf.forget(self.page_index)  # unless it was edited
        self.page_index, self.doc, self.canvas.doc = index, doc, doc
        self._refresh()

    def _refresh(self) -> None:
        """After any change to the document: a pending crop was drawn on the old geometry."""
        self.canvas.crop_rect = None
        self.undo_action.setEnabled(self.doc.can_undo)
        self.redo_action.setEnabled(self.doc.can_redo)
        self.crop_apply.setVisible(self.canvas.tool == "crop")
        self.aspect_action.setVisible(self.canvas.tool == "crop")
        image = self.doc.render()
        size = f"{image.width()} x {image.height()}"
        if self.pdf is not None:
            self.page_label.setText(f" Page {self.page_index + 1} of {self.pdf.count} ")
            self.prev_page.setEnabled(self.page_index > 0)
            self.next_page.setEnabled(self.page_index < self.pdf.count - 1)
            edited = len(self.pdf.edited_pages())
            size += f" · {edited} page{'s' * (edited != 1)} edited" if edited else ""
        self.status.setText(size)
        self.setWindowTitle(f"{'* ' if self._modified() else ''}{self.path.name} - Hala")
        self.canvas.update()

    def _undo(self) -> None:
        if self.doc.undo():
            self._refresh()

    def _redo(self) -> None:
        if self.doc.redo():
            self._refresh()

    # --- saving -----------------------------------------------------------------------------

    def _write(self, dest: Path | None) -> Path | None:
        try:
            out = (save_pdf_copy(self.pdf, dest=dest) if self.pdf
                   else save_copy(self.doc.render(), self.path, dest=dest))
        except ConversionError as exc:
            QMessageBox.warning(self, "Couldn't save", str(exc))
            return None
        self.saved_as = out
        self._mark_clean()
        self._refresh()
        if dest is not None and out.name != dest.name:
            QMessageBox.information(self, "Saved under another name",
                                    f"{dest.name} already exists, so the edit was saved as "
                                    f"{out.name}. Hala never overwrites files.")
        note = (" (it opens without a password; the original keeps its own)"
                if self.pdf is not None and self.pdf.unlocked else "")
        self.statusBar().showMessage(f"Saved {out}{note}", 8000)
        return out

    def save(self) -> bool:
        """Save a copy beside the original. True when there's nothing left unsaved."""
        if not self._modified():
            self.statusBar().showMessage("Nothing new to save", 4000)
            return True
        if not self._edited():  # undone back to the original after an earlier save
            self._mark_clean()
            self._refresh()
            return True
        return self._write(None) is not None

    def save_as(self) -> bool:
        exts = ("pdf",) if self.pdf else save_exts()
        filters = [f"{e.upper()} (*.{e}{' *.jpeg' if e == 'jpg' else ''})" for e in exts]
        own = "pdf" if self.pdf else output_ext(self.path)
        suggested = self.path.with_name(f"{f.stem_of(self.path)} (edited).{own}")
        name, chosen = QFileDialog.getSaveFileName(
            self, "Save as", str(suggested), ";;".join(filters), filters[exts.index(own)],
            QFileDialog.Option.DontConfirmOverwrite)  # we never overwrite; see _write
        if not name:
            return False
        dest = Path(name)
        if not f.ext_of(dest):
            picked = exts[filters.index(chosen)] if chosen in filters else own
            dest = dest.with_name(f"{dest.name}.{picked}")
        return self._write(dest) is not None

    def copy(self) -> None:
        QApplication.clipboard().setImage(self.doc.render())
        self.statusBar().showMessage("Copied to the clipboard", 4000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close(event)
        if event.isAccepted() and self.pdf is not None:
            self.pdf.close()  # the rendered pages

    def _close(self, event: QCloseEvent) -> None:
        if not self._modified():
            event.accept()
            return
        own = "pdf" if self.pdf else output_ext(self.path)
        target = f"{f.stem_of(self.path)} (edited).{own}"
        answer = QMessageBox.question(
            self, "Unsaved edits", f"Save your edits as {target} before closing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Discard:
            event.accept()
            return
        # Fail closed: anything but a confirmed save keeps the window (and the edits) open.
        saved = False
        if answer == QMessageBox.StandardButton.Save:
            try:
                saved = self.save()
            except Exception as exc:  # shown, and the window stays open: edits never lost
                QMessageBox.critical(self, "Couldn't save", str(exc))
        if saved:
            event.accept()
        else:
            event.ignore()


def _report(problems: list[str]) -> None:
    """Errors on stderr for the terminal, and in a dialog when started from Dolphin."""
    for line in problems:
        print(f"✗ {line}", file=sys.stderr)
    if not sys.stderr.isatty():
        QMessageBox.warning(None, "Hala", "\n".join(problems))


def _excepthook(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    """A bug in an event handler is logged and shown instead of vanishing into stderr."""
    text = "".join(traceback.format_exception(kind, exc, tb))
    sys.stderr.write(text)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as log:
            log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}")
    except OSError:
        pass
    if getattr(_excepthook, "showing", False):
        return  # a failing paintEvent would otherwise stack dialogs forever
    _excepthook.showing = True  # type: ignore[attr-defined]
    try:
        QMessageBox.critical(None, "Hala", f"Something went wrong: {exc}\n\nYour edits are "
                             f"still open; try Save as. Details: {LOG}")
    finally:
        _excepthook.showing = False  # type: ignore[attr-defined]


def _open(path: Path) -> Editor | None:
    """An editor for path; a password-protected PDF's password is asked for (again, if it
    was wrong). None if that question is cancelled."""
    try:
        return Editor(path)
    except PasswordNeeded:
        pass
    prompt = f"{path.name} is password-protected.\n\nPassword:"
    while True:
        password, ok = QInputDialog.getText(None, "Password", prompt,
                                            QLineEdit.EchoMode.Password)
        if not ok:
            return None
        try:
            return Editor(path, password)
        except WrongPassword:
            prompt = f"That password doesn't open {path.name}. Try again:"


def main(files: list[Path]) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("hala")
    sys.excepthook = _excepthook
    windows: list[Editor] = []
    problems: list[str] = []
    for path in files:
        if f.ext_of(path) not in (*f.IMAGE_IN, "pdf"):
            problems.append(f"{path.name}: not a picture or PDF Hala can edit")
            continue
        try:
            window = _open(path)
            if window is None:  # a password was asked for and cancelled
                continue
        except ConversionError as exc:
            problems.append(str(exc))
            continue
        except Exception as exc:  # out of memory, /tmp full...: report it, open the rest
            problems.append(f"{path.name}: {exc}")
            continue
        window.show()
        windows.append(window)
    if problems:
        _report(problems)
    if not windows:
        return 1
    return app.exec()

